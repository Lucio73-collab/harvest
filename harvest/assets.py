"""Save pages to disk: the HTML, its external CSS/JS, and its inline <script>/<style> blocks.

Layout per job:
    <dir>/<job>/<host>/<page-slug>/
        index.html        page with CSS/JS links rewritten to the local copies
        original.html     page exactly as fetched (or as rendered, in browser mode)
        inline/           style-1.css, script-1.js, data-1.json ... (one file per inline block)
        manifest.json     every asset: original URL, local path, status, bytes, sha256
    <dir>/<job>/_assets/<host>/   external CSS/JS, shared and deduplicated across pages
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

import httpx
from parsel import Selector

from .config import OutputConfig
from .politeness import HostLimiter, RobotsCache

CSS_IMPORT = re.compile(r"""@import\s+(?:url\(\s*)?(['"]?)([^'")\s;]+)\1\s*\)?""", re.I)
JSON_TYPES = ("application/ld+json", "application/json", "importmap", "speculationrules")


def _safe(s: str, limit: int = 80) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", unquote(s)).strip("._")
    return s[:limit] or "x"


def page_slug(url: str) -> str:
    p = urlsplit(url)
    path = p.path.strip("/") or "index"
    slug = _safe(path.replace("/", "__"), 100)
    if p.query:
        slug += "_" + hashlib.sha1(p.query.encode()).hexdigest()[:8]
    return slug


def _asset_name(url: str, kind: str) -> str:
    p = urlsplit(url)
    base = _safe(Path(p.path).name or "file", 60)
    ext = "." + kind
    if not base.lower().endswith(ext):
        base = re.sub(r"\.(css|js|mjs)$", "", base, flags=re.I) + ext
    return f"{hashlib.sha1(url.encode()).hexdigest()[:10]}-{base}"


def _rel(target: Path, from_dir: Path) -> str:
    return os.path.relpath(target, from_dir).replace(os.sep, "/")


class AssetSaver:
    def __init__(self, job_name: str, cfg: OutputConfig, client: httpx.AsyncClient,
                 limiter: HostLimiter | None = None, robots: RobotsCache | None = None):
        self.cfg = cfg
        self.root = Path(cfg.dir) / _safe(job_name)
        self.assets_root = self.root / "_assets"
        self.client = client
        self.limiter = limiter
        self.robots = robots
        self.max_bytes = int(cfg.max_asset_mb * 1024 * 1024)
        self._cache: dict[str, asyncio.Task] = {}   # url -> task returning asset record

    # ------------------------------------------------------------ downloads
    async def _download(self, url: str, kind: str, depth: int = 0) -> dict[str, Any]:
        rec: dict[str, Any] = {"url": url, "type": kind}
        if self.robots and not await self.robots.allowed(url):
            rec["error"] = "disallowed by robots.txt"
            return rec
        try:
            if self.limiter:
                async with self.limiter.acquire(url):
                    r = await self.client.get(url)
            else:
                r = await self.client.get(url)
        except httpx.HTTPError as e:
            rec["error"] = f"{type(e).__name__}: {e}"
            return rec
        rec["status"] = r.status_code
        if r.status_code >= 400:
            rec["error"] = f"HTTP {r.status_code}"
            return rec
        body = r.content
        if len(body) > self.max_bytes:
            rec["error"] = f"too large ({len(body) // 1024} KB)"
            return rec
        path = self.assets_root / _safe(urlsplit(url).netloc) / _asset_name(url, kind)
        path.parent.mkdir(parents=True, exist_ok=True)
        if kind == "css":
            text = body.decode(r.encoding or "utf-8", "replace")
            text, imports = await self._resolve_css_imports(text, url, path.parent, depth)
            rec["imports"] = imports
            body = text.encode("utf-8")
        path.write_bytes(body)
        rec.update(path=str(path), bytes=len(body), sha256=hashlib.sha256(body).hexdigest())
        return rec

    def fetch(self, url: str, kind: str, depth: int = 0) -> asyncio.Task:
        """Deduplicated: each asset URL is downloaded once per run, however many pages use it."""
        t = self._cache.get(url)
        if t is None:
            t = asyncio.ensure_future(self._download(url, kind, depth))
            self._cache[url] = t
        return t

    async def _resolve_css_imports(self, css: str, base_url: str, css_dir: Path, depth: int):
        """Download @import-ed stylesheets and point the @import at the local copy."""
        if depth >= 3 or not self.cfg.assets:
            return css, []
        found = [(m.group(0), m.group(2)) for m in CSS_IMPORT.finditer(css)]
        recs = []
        for whole, ref in found:
            if ref.startswith("data:"):
                continue
            absu = urljoin(base_url, ref)
            rec = await self.fetch(absu, "css", depth + 1)
            recs.append({k: rec.get(k) for k in ("url", "path", "error")})
            if rec.get("path"):
                css = css.replace(whole, f'@import url("{_rel(Path(rec["path"]), css_dir)}")', 1)
        return css, recs

    # ------------------------------------------------------------ pages
    async def save(self, html: str, url: str, method: str = "http") -> Path:
        host = _safe(urlsplit(url).netloc)
        page_dir = self.root / host / page_slug(url)
        page_dir.mkdir(parents=True, exist_ok=True)
        (page_dir / "original.html").write_text(html, encoding="utf-8")

        sel = Selector(text=html, base_url=url)
        base = sel.css("base::attr(href)").get()
        root = urljoin(url, base) if base else url
        manifest: dict[str, Any] = {"url": url, "fetched_via": method, "external": [], "inline": []}

        # external stylesheets + scripts
        refs: list[tuple[str, str]] = []   # (raw attribute value, kind)
        for link in sel.css("link"):
            rel = (link.attrib.get("rel") or "").lower().split()
            href = link.attrib.get("href")
            as_ = (link.attrib.get("as") or "").lower()
            if not href:
                continue
            if "stylesheet" in rel or ("preload" in rel and as_ == "style"):
                refs.append((href, "css"))
            elif "modulepreload" in rel or ("preload" in rel and as_ == "script"):
                refs.append((href, "js"))
        for s in sel.css("script[src]"):
            refs.append((s.attrib["src"], "js"))
        refs = [(r, k) for r, k in dict.fromkeys(refs) if not r.strip().startswith(("data:", "blob:", "javascript:"))]

        rewritten = html
        if self.cfg.assets and refs:
            tasks = [self.fetch(urljoin(root, raw.strip()), kind) for raw, kind in refs]
            results = await asyncio.gather(*tasks)
            for (raw, kind), rec in zip(refs, results):
                entry = {k: rec.get(k) for k in ("url", "type", "status", "bytes", "sha256", "error", "imports") if rec.get(k) is not None}
                if rec.get("path"):
                    local = _rel(Path(rec["path"]), page_dir)
                    entry["local"] = local
                    if self.cfg.rewrite:
                        for q in ('"', "'"):
                            rewritten = rewritten.replace(f"={q}{raw}{q}", f"={q}{local}{q}")
                manifest["external"].append(entry)

        # inline blocks
        if self.cfg.inline:
            idir = page_dir / "inline"
            n = {"css": 0, "js": 0, "json": 0}
            for st in sel.css("style"):
                code = st.xpath("string()").get() or ""
                if code.strip():
                    n["css"] += 1
                    code, imports = await self._resolve_css_imports(code, root, idir, 0)
                    self._write_inline(idir, f"style-{n['css']}.css", code, manifest, imports)
            for sc in sel.css("script:not([src])"):
                code = sc.xpath("string()").get() or ""
                if not code.strip():
                    continue
                typ = (sc.attrib.get("type") or "").lower()
                kind = "json" if any(t in typ for t in JSON_TYPES) else "js"
                if kind == "js" and typ and not any(t in typ for t in ("javascript", "module", "ecmascript")):
                    kind = "txt"   # templates, text/x-handlebars, etc.
                    n.setdefault("txt", 0)
                n[kind] += 1
                label = {"js": "script", "json": "data", "txt": "template"}[kind]
                self._write_inline(idir, f"{label}-{n[kind]}.{kind}", code, manifest, type_attr=typ or None)
            # style="" attributes, collected into one file for reference
            attrs = [f"/* {el.root.tag}{('#' + el.attrib['id']) if el.attrib.get('id') else ''} */ {{ {el.attrib['style']} }}"
                     for el in sel.css("[style]")]
            if attrs:
                self._write_inline(idir, "style-attributes.css", "\n".join(attrs), manifest, count=len(attrs))

        if self.cfg.rewrite and base:
            rewritten = re.sub(r"<base\b[^>]*>", "", rewritten, count=1, flags=re.I)
        (page_dir / "index.html").write_text(rewritten, encoding="utf-8")
        (page_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        return page_dir

    def _write_inline(self, idir: Path, name: str, code: str, manifest: dict, imports=None, **extra):
        idir.mkdir(parents=True, exist_ok=True)
        (idir / name).write_text(code, encoding="utf-8")
        entry = {"file": f"inline/{name}", "bytes": len(code.encode()), **{k: v for k, v in extra.items() if v is not None}}
        if imports:
            entry["imports"] = imports
        manifest["inline"].append(entry)
