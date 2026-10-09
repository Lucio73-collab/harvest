"""Extraction: rule-based (CSS/XPath), automatic (metadata, text, tables, JSON-LD), and LLM."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin

from parsel import Selector

from .config import ExtractConfig, FieldRule

BOILERPLATE = "script, style, noscript, template, svg, nav, footer, header, aside, form, iframe"


# ---------------------------------------------------------------- rules

def _coerce(value: Any, rule: FieldRule, base_url: str) -> Any:
    if value is None:
        return rule.default
    if isinstance(value, str):
        if rule.strip:
            value = re.sub(r"\s+", " ", value).strip()
        if rule.regex:
            m = re.search(rule.regex, value)
            if not m:
                return rule.default
            value = m.group(1) if m.groups() else m.group(0)
        if value == "":
            return rule.default
    try:
        if rule.type == "int":
            return int(re.sub(r"[^\d-]", "", str(value)))
        if rule.type == "float":
            s = str(value).replace(" ", "")
            # handle "1.234,56" (EU) and "1,234.56" (US)
            if re.search(r"\d\.\d{3},\d", s) or re.fullmatch(r"[^\d]*\d+,\d{1,2}[^\d]*", s):
                s = s.replace(".", "").replace(",", ".")
            else:
                s = s.replace(",", "")
            m = re.search(r"-?\d+(\.\d+)?", s)
            return float(m.group(0)) if m else rule.default
        if rule.type == "bool":
            return str(value).strip().lower() not in ("", "0", "false", "no", "none")
        if rule.type == "url":
            return urljoin(base_url, str(value))
    except (ValueError, TypeError):
        return rule.default
    return value


def _select(sel: Selector, rule: FieldRule) -> list[str]:
    if rule.type == "html":
        nodes = sel.css(rule.css) if rule.css else sel.xpath(rule.xpath)
        return [n.get() for n in nodes]
    if rule.css:
        css = rule.css
        # a bare selector with no ::text / ::attr means "the text of that element"
        if "::" not in css:
            return [" ".join(t.strip() for t in n.css("*::text").getall() if t.strip()) for n in sel.css(css)]
        return sel.css(css).getall()
    return sel.xpath(rule.xpath).getall()


def apply_rules(sel: Selector, fields: dict[str, FieldRule], base_url: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, rule in fields.items():
        try:
            vals = _select(sel, rule)
        except Exception as e:  # bad selector should not kill the run
            out[name] = rule.default
            out.setdefault("_errors", []).append(f"{name}: {e}")
            continue
        if rule.multiple:
            coerced = [c for c in (_coerce(v, rule, base_url) for v in vals) if c not in (None, "")]
            out[name] = rule.join.join(map(str, coerced)) if rule.join is not None else coerced
        else:
            out[name] = _coerce(vals[0] if vals else None, rule, base_url)
    return out


def extract_rules(html: str, url: str, cfg: ExtractConfig) -> list[dict[str, Any]]:
    sel = Selector(text=html, base_url=url)
    if cfg.item or cfg.item_xpath:
        containers = sel.css(cfg.item) if cfg.item else sel.xpath(cfg.item_xpath)
        items = [apply_rules(c, cfg.fields, url) for c in containers]
        # drop items where every field is empty
        return [i for i in items if any(v not in (None, "", []) for k, v in i.items() if k != "_errors")]
    item = apply_rules(sel, cfg.fields, url)
    return [item] if any(v not in (None, "", []) for k, v in item.items() if k != "_errors") else []


# ---------------------------------------------------------------- auto

def _meta(sel: Selector, *names: str) -> str | None:
    for n in names:
        v = sel.css(f'meta[name="{n}"]::attr(content), meta[property="{n}"]::attr(content)').get()
        if v:
            return v.strip()
    return None


def main_text(html: str) -> str:
    """Rough readability: prefer <main>/<article>, otherwise the densest text block in <body>."""
    sel = Selector(text=html)
    for b in sel.css(BOILERPLATE):
        b.drop()
    for scope in ("main", "article", '[role="main"]', "#content", ".content"):
        node = sel.css(scope)
        if node:
            text = _text_of(node[0])
            if len(text) > 200:
                return text
    best, best_len = "", 0
    for block in sel.css("body div, body section, body"):
        t = _text_of(block)
        # density: favour blocks with lots of text and few links
        link_chars = sum(len(x) for x in block.css("a *::text, a::text").getall())
        score = len(t) - 2 * link_chars
        if score > best_len:
            best, best_len = t, score
    return best


def _text_of(node: Selector) -> str:
    parts = []
    for el in node.xpath(".//*[self::p or self::h1 or self::h2 or self::h3 or self::h4 or self::li or self::td or self::th or self::pre or self::blockquote or self::dd or self::dt]"):
        t = " ".join(x.strip() for x in el.xpath(".//text()").getall() if x.strip())
        if t:
            parts.append(t)
    if not parts:
        parts = [" ".join(x.strip() for x in node.css("*::text").getall() if x.strip())]
    # dedupe consecutive repeats from nested elements
    out: list[str] = []
    for p in parts:
        if not out or p not in out[-1]:
            out.append(p)
    return "\n".join(out).strip()


def tables(sel: Selector, limit: int = 10) -> list[list[dict[str, str]]]:
    result = []
    for t in sel.css("table")[:limit]:
        rows = t.css("tr")
        if len(rows) < 2:
            continue
        header = [" ".join(c.css("*::text").getall()).strip() for c in rows[0].css("th, td")]
        if not any(header):
            continue
        header = [h or f"col{i+1}" for i, h in enumerate(header)]
        data = []
        for r in rows[1:]:
            cells = [" ".join(x.strip() for x in c.css("*::text").getall() if x.strip()) for c in r.css("td, th")]
            if cells and any(cells):
                data.append(dict(zip(header, cells)))
        if data:
            result.append(data)
    return result


def json_ld(sel: Selector) -> list[Any]:
    out = []
    for raw in sel.css('script[type="application/ld+json"]::text').getall():
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "@graph" in data:
            out.extend(data["@graph"])
        elif isinstance(data, list):
            out.extend(data)
        else:
            out.append(data)
    return out


def extract_links(html: str, base_url: str) -> list[str]:
    sel = Selector(text=html, base_url=base_url)
    base = sel.css("base::attr(href)").get()
    root = urljoin(base_url, base) if base else base_url
    links = []
    for href in sel.css("a::attr(href), link[rel=next]::attr(href)").getall():
        href = href.strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
            continue
        links.append(urljoin(root, href))
    return links


def extract_auto(html: str, url: str) -> dict[str, Any]:
    sel = Selector(text=html, base_url=url)
    text = main_text(html)
    og = {k.split(":", 1)[1]: v for k, v in (
        (m.css("::attr(property)").get() or "", m.css("::attr(content)").get() or "")
        for m in sel.css('meta[property^="og:"]')) if v}
    return {
        "title": (sel.css("title::text").get() or "").strip() or og.get("title"),
        "description": _meta(sel, "description", "og:description", "twitter:description"),
        "canonical": sel.css('link[rel="canonical"]::attr(href)').get(),
        "lang": sel.css("html::attr(lang)").get(),
        "author": _meta(sel, "author", "article:author"),
        "published": _meta(sel, "article:published_time", "date", "pubdate"),
        "h1": [h.strip() for h in sel.css("h1 *::text, h1::text").getall() if h.strip()][:5],
        "headings": [" ".join(h.css("*::text").getall()).strip() for h in sel.css("h2, h3")][:40],
        "text": text,
        "word_count": len(text.split()),
        "images": [urljoin(url, s) for s in sel.css("img::attr(src)").getall()[:50]],
        "links_count": len(sel.css("a[href]")),
        "opengraph": og,
        "json_ld": json_ld(sel),
        "tables": tables(sel),
    }


# ---------------------------------------------------------------- entry

async def extract(html: str, url: str, cfg: ExtractConfig, llm_fn=None) -> list[dict[str, Any]]:
    """Return a list of items for this page. `llm_fn(text, url)` is an async callable returning list[dict]."""
    items: list[dict[str, Any]]
    if cfg.mode == "rules":
        items = extract_rules(html, url, cfg)
    elif cfg.mode == "auto":
        items = [extract_auto(html, url)]
    elif cfg.mode == "llm":
        items = await llm_fn(main_text(html) or html, url) if llm_fn else []
    else:  # hybrid: rules first, LLM fills in missing fields
        items = extract_rules(html, url, cfg) if cfg.fields else []
        if llm_fn and (not items or any(v in (None, "", []) for i in items for v in i.values())):
            llm_items = await llm_fn(main_text(html) or html, url)
            if not items:
                items = llm_items
            elif len(llm_items) == 1:
                for i in items:
                    for k, v in llm_items[0].items():
                        if i.get(k) in (None, "", []):
                            i[k] = v
    if cfg.include_meta or cfg.mode == "auto":
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for i in items:
            i.setdefault("_url", url)
            i.setdefault("_fetched_at", now)
    return items
