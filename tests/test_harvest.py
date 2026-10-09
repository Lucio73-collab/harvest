import json

import pytest
from fastapi.testclient import TestClient

from harvest import export
from harvest.config import Job, parse_job_text
from harvest.crawler import normalize, run_job
from harvest.extract import extract_auto, extract_rules
from harvest.schedule import next_run
from harvest.storage import Store

from .conftest import PRODUCTS, build_site


def shop_job(base, **over):
    data = {
        "name": "shop",
        "start_urls": [f"{base}/shop/page-1.html"],
        "render": "http",
        "crawl": {"max_pages": 20, "max_depth": 0, "pagination": "li.next a"},
        "extract": {
            "mode": "rules", "item": "article.product", "key": "url",
            "fields": {
                "name": "h2",
                "url": {"css": "h2 a::attr(href)", "type": "url"},
                "price": {"css": ".price::text", "type": "float"},
                "stock": ".stock",
            },
        },
        "politeness": {"delay": 0, "concurrency": 3},
    }
    for k, v in over.items():
        data[k] = {**data.get(k, {}), **v} if isinstance(v, dict) else v
    return Job.model_validate(data)


async def test_rules_pagination_and_change_tracking(site, tmp_path):
    store = Store(tmp_path / "h.db")
    run = await run_job(shop_job(site["base"]), store)
    assert run["status"] == "done"
    assert run["pages"] == 3 and run["items"] == 12 and run["new_items"] == 12
    items = store.items("shop")
    assert {i["name"] for i in items} == {p["name"] for p in PRODUCTS}
    assert items[0]["price"] == pytest.approx(11.5)
    assert items[0]["url"].startswith(site["base"])

    # change a price, drop a product, run again
    changed = [dict(p) for p in PRODUCTS if p["id"] != 12]
    changed[0]["price"] = 99.0
    build_site(site["root"], changed)
    run2 = await run_job(shop_job(site["base"]), store)
    assert run2["new_items"] == 0
    assert run2["changed_items"] == 1
    assert run2["removed_items"] == 1
    ch = store.changes("shop")
    diff = next(c for c in ch if c["change"] == "changed")["diff"]
    assert diff["price"] == [11.5, 99.0]


async def test_robots_respected_and_depth(site, tmp_path):
    store = Store(tmp_path / "h.db")
    job = Job.model_validate({"name": "home", "start_urls": [site["base"] + "/"], "render": "http",
                              "crawl": {"max_depth": 1, "max_pages": 50},
                              "politeness": {"delay": 0}})
    run = await run_job(job, store)
    pages = store.list_pages(run["id"])
    urls = {p["url"] for p in pages}
    assert any(u.endswith("/private/secret.html") and p["status"] == -1 for p in pages for u in [p["url"]])
    assert run["blocked"] == 1
    assert f"{site['base']}/shop/page-1.html" in urls
    assert not any("item-" in u for u in urls)   # depth 2 not reached


async def test_sitemap_and_auto_extraction(site, tmp_path):
    store = Store(tmp_path / "h.db")
    job = Job.model_validate({"name": "sm", "start_urls": [site["base"] + "/"], "render": "http",
                              "crawl": {"max_depth": 0, "max_pages": 30, "sitemap": True},
                              "extract": {"mode": "auto", "only_urls": ["/item-"]},
                              "politeness": {"delay": 0}})
    run = await run_job(job, store)
    assert run["items"] == 12
    it = store.items("sm")[0]
    assert it["description"] == "A fine widget"
    assert it["json_ld"][0]["@type"] == "Product"
    assert it["tables"][0][0]["Spec"] == "Weight"
    assert it["word_count"] > 50


async def test_auto_render_uses_browser_for_js_shell(site, tmp_path):
    store = Store(tmp_path / "h.db")
    job = Job.model_validate({"name": "js", "start_urls": [site["base"] + "/app.html"], "render": "auto",
                              "crawl": {"max_depth": 0},
                              "extract": {"mode": "rules", "item": ".card",
                                          "fields": {"title": "h3", "value": {"css": ".v::text", "type": "int"}}},
                              "politeness": {"delay": 0}})
    run = await run_job(job, store)
    pages = store.list_pages(run["id"])
    assert pages[0]["method"] == "browser", pages
    vals = sorted(i["value"] for i in store.items("js"))
    assert vals == [7, 14, 21, 28]


def test_extract_helpers():
    html = "<div class=p><b>1.234,50 EUR</b><i>x</i></div><div class=p><b>$2,000.75</b></div>"
    from harvest.config import ExtractConfig
    cfg = ExtractConfig(mode="rules", item=".p", fields={"price": {"css": "b::text", "type": "float"}})
    assert [i["price"] for i in extract_rules(html, "http://x/", cfg)] == [1234.5, 2000.75]
    a = extract_auto("<html lang=nl><title>T</title><body><article><p>" + "woord " * 60 + "</p></article></body></html>", "http://x/")
    assert a["title"] == "T" and a["lang"] == "nl" and a["word_count"] == 60


def test_normalize_and_schedule():
    assert normalize("HTTP://Ex.com/a?utm_source=x&b=1#frag") == "http://ex.com/a?b=1"
    assert next_run("every 30m", 1000) == 1000 + 1800
    assert next_run("0 8 * * *", 0) > 0
    with pytest.raises(ValueError):
        next_run("whenever")


def test_export_formats(tmp_path):
    items = [{"a": 1, "b": ["x", "y"], "c": {"k": 1}, "_url": "http://x"}, {"a": 2, "d": "z"}]
    for f in export.FORMATS:
        p = export.write(items, tmp_path / f"o.{f}")
        assert p.stat().st_size > 0
    assert json.loads((tmp_path / "o.json").read_text())[0]["b"] == ["x", "y"]
    assert "x | y" in (tmp_path / "o.csv").read_text(encoding="utf-8-sig")


def test_job_validation():
    with pytest.raises(Exception):
        parse_job_text("name: x\nstart_urls: [http://a]\nextract: {fields: {t: {regex: 'x'}}}")
    j = parse_job_text("start_urls: http://a\nextract: {mode: rules, fields: {t: h1}}", "fb")
    assert j.name == "fb" and j.extract.fields["t"].css == "h1"


def test_dashboard_api(site, tmp_path):
    from harvest.web.app import create_app
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    app = create_app(str(jobs), str(tmp_path / "d.db"), with_schedule=False)
    with TestClient(app) as c:
        assert "Harvest" in c.get("/").text
        r = c.post("/api/jobs", json={"name": "t1", "template": "auto", "url": site["base"] + "/"})
        assert r.status_code == 200
        assert site["base"] in c.get("/api/jobs/t1/source").json()["text"]
        assert c.post("/api/jobs", json={"name": "bad name!"}).status_code == 400
        q = c.post("/api/quick", json={"url": site["base"] + "/shop/page-1.html", "render": "http",
                                       "selectors": "name = h2\nprice = .price::text", "item": "article.product"}).json()
        assert len(q["items"]) == 5 and q["items"][0]["name"] == "Widget 1"
        bad = c.put("/api/jobs/t1/source", json={"text": "name: t1\nstart_urls: 5"})
        assert bad.status_code == 422
        assert c.get("/api/jobs").json()[0]["name"] == "t1"
        # run a job from the dashboard and wait for it to finish
        c.put("/api/jobs/t1/source", json={"text": f"name: t1\nstart_urls: [{site['base']}/]\nrender: http\n"
                                                    "crawl: {max_depth: 0}\npoliteness: {delay: 0}\n"})
        assert c.post("/api/jobs/t1/run").status_code == 200
        import time
        for _ in range(50):
            runs = c.get("/api/runs?job=t1").json()
            if runs and runs[0]["status"] != "running":
                break
            time.sleep(0.1)
        assert runs[0]["status"] == "done" and runs[0]["items"] == 1
        assert c.get("/api/jobs/t1/export?fmt=xlsx").status_code == 200
        assert c.get(f"/api/runs/{runs[0]['id']}/html.zip").status_code == 404   # saving was off


async def test_save_html_with_inline_and_external_assets(site, tmp_path):
    store = Store(tmp_path / "h.db")
    out = tmp_path / "html"
    job = Job.model_validate({"name": "pages", "start_urls": [site["base"] + "/assets.html", site["base"] + "/assets2.html"],
                              "render": "http", "crawl": {"max_depth": 0},
                              "output": {"save_html": True, "dir": str(out)},
                              "politeness": {"delay": 0}})
    run = await run_job(job, store)
    assert run["status"] == "done"
    pages = store.list_pages(run["id"])
    assert all(p["html_path"] for p in pages)
    d = __import__("pathlib").Path(pages[0]["html_path"])
    man = json.loads((d / "manifest.json").read_text())
    ext = {e["url"].rsplit("/", 1)[-1]: e for e in man["external"]}
    assert ext["site.css"]["local"].startswith("../../_assets/")
    assert ext["missing.css"]["error"] == "HTTP 404"
    assert "local" in ext["app.js"] and "local" in ext["mod.mjs?v=3"]
    # @import resolved and rewritten to the local copy
    site_css = (d / ext["site.css"]["local"]).resolve().read_text()
    assert "@import url(\"" in site_css and "base.css" in site_css and "/static/" not in site_css
    # index.html points to local files, original.html is untouched
    idx, orig = (d / "index.html").read_text(), (d / "original.html").read_text()
    assert "/static/site.css" in orig and "/static/site.css" not in idx and ext["app.js"]["local"] in idx
    # inline blocks
    files = sorted(e["file"] for e in man["inline"])
    assert files == ["inline/data-1.json", "inline/script-1.js", "inline/script-2.js",
                     "inline/style-1.css", "inline/style-attributes.css", "inline/template-1.txt"]
    assert "inline two" in (d / "inline/script-2.js").read_text()
    # shared assets were downloaded once
    shared = [f for f in (out / "pages" / "_assets").rglob("*") if f.is_file()]
    assert len(shared) == 4   # site.css, base.css, app.js, mod.js
