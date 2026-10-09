"""A tiny local test website served in a background thread."""
from __future__ import annotations

import http.server
import json
import socketserver
import threading
from pathlib import Path

import pytest

PRODUCTS = [
    {"id": i, "name": f"Widget {i}", "price": 10 + i * 1.5, "stock": "In stock" if i % 3 else "Sold out"}
    for i in range(1, 13)
]


def build_site(root: Path, products=PRODUCTS) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "robots.txt").write_text(
        "User-agent: *\nDisallow: /private/\nSitemap: http://{host}/sitemap.xml\n")
    per = 5
    pages = [products[i:i + per] for i in range(0, len(products), per)]
    for n, chunk in enumerate(pages, 1):
        nxt = f'<li class="next"><a href="/shop/page-{n + 1}.html">next</a></li>' if n < len(pages) else ""
        cards = "".join(
            f'<article class="product"><h2><a href="/shop/item-{p["id"]}.html">{p["name"]}</a></h2>'
            f'<span class="price">EUR {p["price"]:.2f}</span><p class="stock">{p["stock"]}</p></article>'
            for p in chunk)
        (root / "shop").mkdir(exist_ok=True)
        (root / "shop" / f"page-{n}.html").write_text(
            f"<html><head><title>Shop page {n}</title></head><body><nav><a href='/'>home</a></nav>"
            f"<main><h1>Shop</h1>{cards}<ul>{nxt}</ul></main><a href='/private/secret.html'>secret</a></body></html>")
    for p in products:
        (root / "shop" / f"item-{p['id']}.html").write_text(
            f"<html><head><title>{p['name']}</title><meta name='description' content='A fine widget'>"
            f"<script type='application/ld+json'>{json.dumps({'@type': 'Product', 'name': p['name']})}</script></head>"
            f"<body><main><h1>{p['name']}</h1><p>This is a long product description for {p['name']}. "
            + "It has plenty of words so the readability heuristic picks it up as the main content. " * 5
            + f"</p><table><tr><th>Spec</th><th>Value</th></tr><tr><td>Weight</td><td>{p['id']}00 g</td></tr></table>"
            f"</main></body></html>")
    (root / "private").mkdir(exist_ok=True)
    (root / "private" / "secret.html").write_text("<html><body>secret</body></html>")
    (root / "index.html").write_text(
        "<html><head><title>Home</title></head><body><main><h1>Test shop</h1>"
        "<p>" + "Welcome to the test shop. " * 20 + "</p>"
        "<a href='/shop/page-1.html'>Shop</a> <a href='/private/secret.html'>Private</a>"
        "<a href='/app.html'>App</a></main></body></html>")
    # page with external + inline CSS/JS (assets shared with a second page)
    (root / "static").mkdir(exist_ok=True)
    (root / "static" / "base.css").write_text("body{color:#111}")
    (root / "static" / "site.css").write_text('@import url("base.css");\n.hero{color:green}')
    (root / "static" / "app.js").write_text("window.APP = 1;")
    (root / "static" / "mod.mjs").write_text("export const x = 2;")
    assets_head = ("<link rel='stylesheet' href='/static/site.css'>"
                   "<link rel=\"stylesheet\" href=\"/static/missing.css\">"
                   "<script src='/static/app.js'></script>"
                   "<script type='module' src='/static/mod.mjs?v=3'></script>")
    for name in ("assets", "assets2"):
        (root / f"{name}.html").write_text(
            f"<html><head><title>{name}</title>{assets_head}"
            "<style>.inline{margin:0}</style>"
            "<script>console.log('inline one')</script>"
            "<script type='application/ld+json'>{\"@type\": \"WebPage\"}</script>"
            "<script type='text/x-template'><div>tpl</div></script>"
            "</head><body><main><h1 style='color:red' id='t'>Assets</h1><p>" + "words " * 80 + "</p>"
            "<script>console.log('inline two')</script></main></body></html>")
    # JS-rendered page: empty shell, content injected by script
    (root / "app.html").write_text(
        "<html><head><title>App</title></head><body><div id='root'></div><script>"
        "document.getElementById('root').innerHTML = "
        "Array.from({length: 4}, (_, i) => `<div class='card'><h3>JS item ${i+1}</h3><b class='v'>${(i+1)*7}</b></div>`).join('');"
        "</script></body></html>")


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


@pytest.fixture()
def site(tmp_path):
    root = tmp_path / "site"
    build_site(root)
    handler = lambda *a, **kw: _Quiet(*a, directory=str(root), **kw)  # noqa: E731
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    host = f"127.0.0.1:{srv.server_address[1]}"
    robots = root / "robots.txt"
    robots.write_text(robots.read_text().replace("{host}", host))
    sm = "".join(f"<url><loc>http://{host}/shop/item-{p['id']}.html</loc></url>" for p in PRODUCTS)
    (root / "sitemap.xml").write_text(
        f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{sm}</urlset>')
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield {"base": f"http://{host}", "root": root, "host": host}
    srv.shutdown()
