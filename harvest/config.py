"""Job configuration models. A job is one YAML/JSON file describing what to scrape and how."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

DEFAULT_UA = "HarvestBot/1.0 (+https://github.com/LucioTD/harvest)"


class FieldRule(BaseModel):
    """How to pull one field out of a page (or out of one item container)."""

    css: str | None = None
    xpath: str | None = None
    regex: str | None = None          # applied after selection; first group wins if present
    multiple: bool = False            # return a list instead of the first match
    type: Literal["str", "int", "float", "bool", "url", "html"] = "str"
    default: Any = None
    join: str | None = None           # join a list into one string with this separator
    strip: bool = True

    @model_validator(mode="after")
    def _one_selector(self) -> "FieldRule":
        if not self.css and not self.xpath:
            raise ValueError("field needs 'css' or 'xpath'")
        return self


class LLMConfig(BaseModel):
    provider: Literal["ollama", "openai"] = "ollama"
    model: str = "llama3.1"
    host: str = "http://localhost:11434"          # Ollama host, or OpenAI-compatible base URL
    api_key_env: str = "OPENAI_API_KEY"           # only used by provider=openai
    instructions: str = "Extract the main structured data from this page."
    schema_: dict[str, Any] = Field(default_factory=dict, alias="schema")
    max_chars: int = 12000
    many: bool = False                            # expect a list of items instead of one object
    timeout: float = 120

    model_config = {"populate_by_name": True}


class ExtractConfig(BaseModel):
    mode: Literal["rules", "auto", "llm", "hybrid"] = "auto"
    only_urls: list[str] = Field(default_factory=list)   # regexes; extract only on matching pages
    item: str | None = None                              # CSS selector for repeating item containers
    item_xpath: str | None = None
    fields: dict[str, FieldRule] = Field(default_factory=dict)
    key: str | None = None                               # field used as stable identity for change tracking
    include_meta: bool = False                           # add url/title/fetched_at to every item
    llm: LLMConfig = Field(default_factory=LLMConfig)

    @field_validator("fields", mode="before")
    @classmethod
    def _shorthand(cls, v: Any) -> Any:
        # allow `title: "h1::text"` as shorthand for {css: "h1::text"}
        if isinstance(v, dict):
            return {k: ({"css": r} if isinstance(r, str) else r) for k, r in v.items()}
        return v


class CrawlConfig(BaseModel):
    max_pages: int = 50
    max_depth: int = 1
    same_domain: bool = True
    follow: list[str] = Field(default_factory=list)       # regex allowlist for links to follow
    exclude: list[str] = Field(default_factory=list)      # regex blocklist
    pagination: str | None = None                         # CSS selector of "next page" link, always followed
    sitemap: bool = False                                 # seed from robots.txt sitemaps / sitemap.xml
    sitemap_filter: list[str] = Field(default_factory=list)


class BrowserConfig(BaseModel):
    wait_until: Literal["load", "domcontentloaded", "networkidle", "commit"] = "networkidle"
    wait_for: str | None = None          # CSS selector to wait for before reading the page
    scroll: int = 0                      # number of scroll-to-bottom passes (infinite scroll)
    scroll_pause: float = 1.0
    click: list[str] = Field(default_factory=list)   # selectors to click before extraction (e.g. "load more")
    block_resources: list[str] = Field(default_factory=lambda: ["image", "media", "font"])
    headless: bool = True
    screenshot: bool = False             # save a full-page PNG per page into ./screenshots


class PolitenessConfig(BaseModel):
    respect_robots: bool = True
    delay: float = 1.0                   # min seconds between requests to the same host
    concurrency: int = 4                 # total parallel requests
    per_host: int = 2                    # parallel requests per host
    timeout: float = 20
    retries: int = 3
    user_agent: str = DEFAULT_UA


class OutputConfig(BaseModel):
    save_html: bool = False      # write every fetched page to disk
    dir: str = "html"            # root folder: <dir>/<job>/<host>/<page>/
    assets: bool = True          # download external CSS + JS (and CSS @imports)
    inline: bool = True          # also write inline <script>/<style> blocks to separate files
    rewrite: bool = True         # index.html points at the local copies (original.html is untouched)
    max_asset_mb: float = 10


class NotifyConfig(BaseModel):
    webhook: str | None = None           # Discord / Slack / generic webhook
    on: Literal["changes", "always", "errors"] = "changes"


class Job(BaseModel):
    name: str
    start_urls: list[str]
    render: Literal["http", "browser", "auto"] = "auto"
    headers: dict[str, str] = Field(default_factory=dict)
    cookies: dict[str, str] = Field(default_factory=dict)
    proxy: str | None = None
    schedule: str | None = None          # "every 30m" | "every 6h" | cron "0 8 * * *"
    crawl: CrawlConfig = Field(default_factory=CrawlConfig)
    extract: ExtractConfig = Field(default_factory=ExtractConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    politeness: PolitenessConfig = Field(default_factory=PolitenessConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)

    @field_validator("start_urls", mode="before")
    @classmethod
    def _listify(cls, v: Any) -> Any:
        return [v] if isinstance(v, str) else v

    @field_validator("name")
    @classmethod
    def _clean_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name is required")
        return v


def load_job(path: str | Path) -> Job:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    data = json.loads(text) if p.suffix == ".json" else yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"{p}: job file must be a mapping")
    data.setdefault("name", p.stem)
    return Job.model_validate(data)


def parse_job_text(text: str, fallback_name: str = "job") -> Job:
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("job must be a YAML mapping")
    data.setdefault("name", fallback_name)
    return Job.model_validate(data)
