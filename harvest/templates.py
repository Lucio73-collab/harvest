"""Starter job files for `harvest init`."""

TEMPLATES: dict[str, str] = {
    "auto": """\
# Auto mode: grabs title, description, main text, headings, tables, JSON-LD from every page.
name: {name}
start_urls:
  - https://example.com/
render: auto            # http | browser | auto (auto switches to a browser for JS-heavy sites)
crawl:
  max_pages: 25
  max_depth: 1          # 0 = only start_urls, 1 = plus pages they link to, ...
  same_domain: true
  follow: []            # regexes; only follow links matching one of these (empty = all)
  exclude: ["/login", "/cart", "\\\\?sort="]
extract:
  mode: auto
politeness:
  delay: 1.0            # seconds between requests to the same host
  concurrency: 4
  respect_robots: true
output:
  save_html: true       # save each page's HTML + inline and external JS/CSS to ./html/<job>/
""",
    "list": """\
# Rules mode: repeating items on listing pages, with pagination and change tracking.
# Try it as-is: books.toscrape.com is a public sandbox made for practising scraping.
name: {name}
start_urls:
  - https://books.toscrape.com/
render: http
schedule: every 6h       # used by `harvest schedule` and `harvest serve --schedule`
crawl:
  max_pages: 10
  max_depth: 0
  pagination: "li.next a"          # follow the "next" link on every page
extract:
  mode: rules
  item: "article.product_pod"      # one item per matching element
  key: url                         # stable identity, so price changes show up as 'changed'
  fields:
    title: {{css: "h3 a::attr(title)"}}
    url:   {{css: "h3 a::attr(href)", type: url}}
    price: {{css: ".price_color::text", type: float}}
    stock: ".availability"         # shorthand: element text
    rating: {{css: "p.star-rating::attr(class)", regex: "star-rating (\\\\w+)"}}
notify:
  webhook: null                    # Discord/Slack webhook URL to get pinged on changes
  on: changes
output:
  save_html: true       # save each page's HTML + inline and external JS/CSS to ./html/<job>/
""",
    "spa": """\
# Browser mode: for JavaScript apps, infinite scroll and "load more" buttons.
name: {name}
start_urls:
  - https://quotes.toscrape.com/scroll
render: browser
crawl:
  max_pages: 1
  max_depth: 0
browser:
  wait_until: networkidle
  wait_for: ".quote"
  scroll: 5                 # scroll to the bottom 5 times
  scroll_pause: 1.0
  click: []                 # e.g. ["button.load-more"]
  screenshot: false
extract:
  mode: rules
  item: ".quote"
  key: text
  fields:
    text: ".text"
    author: ".author"
    tags: {{css: ".tag::text", multiple: true}}
output:
  save_html: true       # save each page's HTML + inline and external JS/CSS to ./html/<job>/
""",
    "llm": """\
# LLM mode: describe what you want, a local Ollama model fills in the JSON.
# Needs Ollama running (ollama serve) and the model pulled (ollama pull llama3.1).
name: {name}
start_urls:
  - https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html
render: http
crawl:
  max_pages: 5
  max_depth: 0
extract:
  mode: llm               # or 'hybrid': CSS rules first, LLM fills the gaps
  include_meta: true
  llm:
    provider: ollama      # or 'openai' for any OpenAI-compatible API (host = base URL)
    model: llama3.1
    host: http://localhost:11434
    instructions: Extract the product details from this page.
    many: false           # true = expect a list of items per page
    schema:
      name: string
      price: number
      in_stock: boolean
      description: string
output:
  save_html: true       # save each page's HTML + inline and external JS/CSS to ./html/<job>/
""",
}
