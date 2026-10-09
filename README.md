<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/banner-dark.svg">
    <img src="docs/assets/banner.svg" alt="harvest: Polite web scraper with a CLI and a local dashboard" width="100%">
  </picture>
</p>

<p align="center">
  <a href="https://github.com/Lucio73-collab/harvest/issues/new?template=bug_report.yml">Report bug</a>
  ·
  <a href="https://github.com/Lucio73-collab/harvest/issues/new?template=feature_request.yml">Request feature</a>
  ·
  <a href="https://github.com/Lucio73-collab/harvest/releases">Releases</a>
</p>

<p align="center">
  <a href="https://github.com/Lucio73-collab/harvest/actions/workflows/ci.yml"><img src="https://github.com/Lucio73-collab/harvest/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/version-1.0.0-a6e3a1?style=flat-square&labelColor=111111" alt="Version 1.0.0">
  <img src="https://img.shields.io/badge/Python-3.11%2B-111111?style=flat-square&logo=python" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/browser-Playwright-333333?style=flat-square" alt="Playwright">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-333333?style=flat-square" alt="MIT license"></a>
</p>

<p align="center">
  <img src="docs/assets/screenshot.png" alt="Harvest dashboard showing a job with new, changed and removed items" width="800">
</p>

A polite, general-purpose web scraper with a CLI and a local dashboard.

- **Fetching:** plain HTTP (fast) or a headless Chromium browser for JavaScript sites. `auto` mode detects empty JS app shells and switches per site.
- **Crawling:** link following with depth limits, allow and block regexes, "next page" pagination, sitemap seeding, and URL normalisation (strips tracking params).
- **Extraction:** CSS/XPath rules with types, regex and lists; an automatic mode (title, meta, main text, headings, tables, JSON-LD, OpenGraph); and LLM mode via local Ollama or any OpenAI-compatible API. `hybrid` runs rules first and lets the LLM fill the gaps.
- **Browser tricks:** wait for a selector, infinite scroll, click "load more" buttons, block images and fonts, full-page screenshots.
- **Storage:** SQLite with dedupe and change tracking (new, changed with a field diff, removed) on every run.
- **Save pages:** the HTML plus all its JavaScript and CSS, inline and external, rewritten so the copy opens offline.
- **Export:** JSON, JSONL, CSV (Excel-safe UTF-8) and XLSX.
- **Scheduling:** `every 30m`, `every 6h`, `daily` or cron. Discord/Slack webhook alerts on changes.
- **Politeness by default:** respects robots.txt and `Crawl-delay`, per-host rate limits, backs off on 429/503 and `Retry-After`, honest User-Agent.

## Install (Windows, Linux or macOS)

```bash
git clone https://github.com/Lucio73-collab/harvest.git
cd harvest
uv venv
uv pip install -e .
uv run playwright install chromium     # one time, only needed for browser/auto mode
```

## Quick start

```bash
uv run harvest init books -t list       # starter job in ./jobs (templates: auto, list, spa, llm)
uv run harvest run books -o books.xlsx  # crawl once, export
uv run harvest serve                    # dashboard at http://127.0.0.1:8787 (runs schedules too)
```

The `jobs/` folder has four working examples: `books` (rules + pagination + change tracking), `quotes` (browser + infinite scroll), `example` (auto mode) and `product-llm` (Ollama).

## Commands

| Command | What it does |
|---|---|
| `harvest init NAME -t list` | Create a job file from a template |
| `harvest validate NAME` | Check a job file without running it |
| `harvest run NAME [-o out.csv] [--max-pages N] [--render browser] [--save-html]` | Run a job once |
| `harvest quick URL [-s "title=h1" ...] [--item ".card"] [--save-html DIR]` | Scrape one page now, prints JSON |
| `harvest runs [NAME]` | Run history |
| `harvest changes NAME [--run ID]` | What was new, changed or removed |
| `harvest export NAME out.xlsx [--all] [--search text]` | Export stored items |
| `harvest schedule` | Run all scheduled jobs forever (headless) |
| `harvest serve [--port 8787] [--no-schedule]` | Dashboard + scheduler |

## Job file reference

```yaml
name: books
start_urls: [https://books.toscrape.com/]
render: auto              # http | browser | auto
schedule: every 6h        # optional: every 30m | every 6h | daily | cron "0 8 * * *"
headers: {}               # extra request headers
cookies: {}               # e.g. a session cookie for a site you have an account on
proxy: null               # http://user:pass@host:port

crawl:
  max_pages: 50
  max_depth: 1            # 0 = only start_urls
  same_domain: true
  follow: ["/catalogue/"] # only follow links matching one of these regexes
  exclude: ["/login"]
  pagination: "li.next a" # always followed, does not use up depth
  sitemap: false          # seed from robots.txt sitemaps or /sitemap.xml
  sitemap_filter: []

extract:
  mode: rules             # rules | auto | llm | hybrid
  only_urls: []           # regexes: only extract on matching pages (crawl the rest)
  item: "article.product_pod"   # repeating container; omit for one item per page
  key: url                # stable id so edits show as "changed" (default: hash of all fields)
  include_meta: false     # add _url and _fetched_at to items
  fields:
    title: "h3 a"                             # shorthand = element text
    url:   {css: "h3 a::attr(href)", type: url}
    price: {css: ".price_color::text", type: float}   # handles 1.234,56 and 1,234.56
    tags:  {css: ".tag::text", multiple: true}        # list (add join: ", " for a string)
    sku:   {xpath: "//td[@class='sku']/text()", regex: "SKU-(\\d+)", type: int}
  llm:                    # used by mode llm / hybrid
    provider: ollama      # or openai (host = base URL, key from api_key_env)
    model: llama3.1
    host: http://localhost:11434
    instructions: Extract the product details.
    many: false
    schema: {name: string, price: number}

browser:
  wait_until: networkidle
  wait_for: ".results"
  scroll: 0               # infinite-scroll passes
  click: []               # e.g. ["button.load-more"]
  block_resources: [image, media, font]
  screenshot: false       # saves PNGs to ./screenshots

politeness:
  respect_robots: true
  delay: 1.0              # seconds between requests per host
  concurrency: 4
  per_host: 2
  timeout: 20
  retries: 3
  user_agent: "HarvestBot/1.0 (+https://github.com/Lucio73-collab/harvest)"

output:
  save_html: false        # true = save every page (same as --save-html)
  dir: html
  assets: true            # download external CSS + JS (follows CSS @import)
  inline: true            # write inline <script>/<style> blocks to their own files
  rewrite: true           # index.html links to the local copies
  max_asset_mb: 10

notify:
  webhook: null           # Discord or Slack webhook URL
  on: changes             # changes | always | errors
```

## Saved pages (HTML + JS/CSS)

Turn it on with `--save-html` or `output.save_html: true`. In the dashboard, open **Runs**, click a run, and use **Download HTML + JS/CSS** (or the per-page zip link). Quick scrape has a checkbox for it.

```
html/<job>/
  <host>/<page>/
    index.html          links rewritten to the local CSS/JS, opens offline
    original.html       exactly as fetched (rendered DOM in browser mode)
    inline/             script-1.js, script-2.js, style-1.css, data-1.json (JSON-LD),
                        template-1.txt, style-attributes.css (all style="" attributes)
    manifest.json       every asset: original URL, local path, HTTP status, size, sha256
  _assets/<host>/       external .css/.js, downloaded once and shared by every page
```

- External files come from `<link rel=stylesheet>`, `<script src>`, `preload` and `modulepreload` tags, including CDNs (other domains).
- Asset downloads follow the same robots.txt and rate-limit rules as pages. Failures (404, blocked, too large) are listed in `manifest.json` instead of stopping the run.
- Images and fonts are not downloaded. In browser mode, `original.html` is the page after JavaScript ran.
- Module scripts (`type="module"`) are blocked by browsers on `file://`. To run those, serve the folder: `python -m http.server -d html`.

## Notes

- **"Removed" items** are only computed after a clean run (no errors). If `max_pages` cuts a crawl short, pages it never reached would otherwise look removed, so raise `max_pages` for jobs where removals matter.
- **Auto render** decides once per host. Force `render: browser` if a site serves some content server-side and the rest by JavaScript.
- **Data** lives in `harvest.db` (change with `--db`). The dashboard only listens on 127.0.0.1.
- **Tests:** `uv pip install -e ".[dev]"` then `uv run pytest`. The suite spins up a local test shop (pagination, robots.txt, sitemap, JS page) and checks crawling, change tracking, browser rendering, exports and the dashboard API.

## Staying on the right side of the rules

Harvest is built to be a good citizen, but the tool can't judge your use case. Check the site's terms, prefer an official API when one exists, and avoid scraping personal data (names, profiles, contact details). Under the GDPR that is where most legal risk sits. Don't use cookies or proxies to get around logins, paywalls or blocks you aren't authorised to pass.

## Contributing

See [CONTRIBUTING.md](.github/CONTRIBUTING.md). Security issues: [SECURITY.md](SECURITY.md).

## License

Code released under the [MIT License](LICENSE).
