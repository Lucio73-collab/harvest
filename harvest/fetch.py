"""Fetching: plain HTTP (httpx), headless browser (Playwright), and 'auto' which picks per site."""
from __future__ import annotations

import asyncio
import email.utils
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .config import Job

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}
JS_SHELL_HINTS = re.compile(
    r'<div[^>]+id=["\'](root|app|__next|__nuxt|svelte)["\'][^>]*>\s*</div>'
    r"|<noscript>[^<]*(enable|requires?) javascript",
    re.I,
)


@dataclass
class Page:
    url: str                 # final URL after redirects
    requested_url: str
    status: int
    html: str
    content_type: str = ""
    method: str = "http"     # http | browser
    elapsed_ms: int = 0
    error: str | None = None
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 400

    @property
    def is_html(self) -> bool:
        ct = self.content_type.lower()
        return not ct or "html" in ct or "xml" in ct


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            dt = email.utils.parsedate_to_datetime(value)
            return max(0.0, dt.timestamp() - time.time())
        except (TypeError, ValueError):
            return None


def visible_text_len(html: str) -> int:
    body = re.sub(r"(?is)<(script|style|noscript|template|svg)[^>]*>.*?</\1>", " ", html)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    return len(re.sub(r"\s+", " ", body).strip())


def looks_js_rendered(html: str, wait_for: str | None = None) -> bool:
    """Heuristic: is this an empty JS app shell that needs a browser?"""
    if visible_text_len(html) < 250:
        return True
    if JS_SHELL_HINTS.search(html):
        return True
    if wait_for:
        try:
            from parsel import Selector
            if not Selector(text=html).css(wait_for):
                return True
        except Exception:
            pass
    return False


def build_client(job: Job) -> httpx.AsyncClient:
    headers = {
        "User-Agent": job.politeness.user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.8,nl;q=0.6",
        **job.headers,
    }
    return httpx.AsyncClient(
        headers=headers,
        cookies=job.cookies,
        follow_redirects=True,
        timeout=job.politeness.timeout,
        http2=True,
        proxy=job.proxy,
        limits=httpx.Limits(max_connections=max(4, job.politeness.concurrency * 2)),
    )


class HttpFetcher:
    def __init__(self, client: httpx.AsyncClient, retries: int, on_backoff=None):
        self.client = client
        self.retries = retries
        self.on_backoff = on_backoff   # callback(host, seconds) for the rate limiter

    async def fetch(self, url: str) -> Page:
        last_err = None
        for attempt in range(self.retries + 1):
            t0 = time.monotonic()
            try:
                r = await self.client.get(url)
                ms = int((time.monotonic() - t0) * 1000)
                if r.status_code in RETRY_STATUS and attempt < self.retries:
                    wait = _retry_after(r.headers.get("retry-after")) or min(60, 2 ** (attempt + 1))
                    if self.on_backoff:
                        self.on_backoff(urlsplit(url).netloc, wait)
                    await asyncio.sleep(wait)
                    continue
                return Page(
                    url=str(r.url), requested_url=url, status=r.status_code,
                    html=r.text if _is_text(r) else "",
                    content_type=r.headers.get("content-type", ""),
                    elapsed_ms=ms, headers=dict(r.headers),
                )
            except httpx.HTTPError as e:
                last_err = f"{type(e).__name__}: {e}"
                if attempt < self.retries:
                    await asyncio.sleep(min(30, 2 ** (attempt + 1)))
        return Page(url=url, requested_url=url, status=0, html="", error=last_err or "failed")


def _is_text(r: httpx.Response) -> bool:
    ct = r.headers.get("content-type", "").lower()
    return not ct or any(t in ct for t in ("text", "html", "xml", "json", "javascript"))


class BrowserFetcher:
    """Lazy Playwright Chromium. One browser, one context, a page per fetch."""

    def __init__(self, job: Job):
        self.job = job
        self._pw = None
        self._browser = None
        self._context = None
        self._lock = asyncio.Lock()

    async def _ensure(self):
        async with self._lock:
            if self._context:
                return
            from playwright.async_api import async_playwright
            self._pw = await async_playwright().start()
            launch = {"headless": self.job.browser.headless}
            exe = os.environ.get("HARVEST_CHROMIUM")
            if exe:
                launch["executable_path"] = exe
            if self.job.proxy:
                launch["proxy"] = {"server": self.job.proxy}
            self._browser = await self._pw.chromium.launch(**launch)
            self._context = await self._browser.new_context(
                user_agent=self.job.politeness.user_agent,
                extra_http_headers=self.job.headers or None,
                ignore_https_errors=False,
            )
            if self.job.cookies:
                cookies = []
                for u in self.job.start_urls:
                    host = urlsplit(u).hostname
                    cookies += [{"name": k, "value": v, "domain": host, "path": "/"} for k, v in self.job.cookies.items()]
                await self._context.add_cookies(cookies)
            blocked = set(self.job.browser.block_resources)
            if blocked:
                async def _route(route):
                    if route.request.resource_type in blocked:
                        await route.abort()
                    else:
                        await route.continue_()
                await self._context.route("**/*", _route)

    async def fetch(self, url: str) -> Page:
        b = self.job.browser
        t0 = time.monotonic()
        try:
            await self._ensure()
            page = await self._context.new_page()
        except Exception as e:
            return Page(url=url, requested_url=url, status=0, html="", method="browser",
                        error=f"browser unavailable: {e}")
        try:
            resp = await page.goto(url, wait_until=b.wait_until, timeout=self.job.politeness.timeout * 1000)
            if b.wait_for:
                try:
                    await page.wait_for_selector(b.wait_for, timeout=self.job.politeness.timeout * 1000)
                except Exception:
                    pass
            for sel in b.click:
                try:
                    loc = page.locator(sel).first
                    if await loc.count():
                        await loc.click(timeout=5000)
                        await page.wait_for_timeout(int(b.scroll_pause * 1000))
                except Exception:
                    pass
            for _ in range(b.scroll):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(int(b.scroll_pause * 1000))
            if b.screenshot:
                d = Path("screenshots")
                d.mkdir(exist_ok=True)
                name = re.sub(r"[^a-zA-Z0-9]+", "_", url)[:150] + ".png"
                await page.screenshot(path=str(d / name), full_page=True)
            html = await page.content()
            status = resp.status if resp else 200
            ct = (await resp.header_value("content-type")) if resp else "text/html"
            return Page(url=page.url, requested_url=url, status=status, html=html,
                        content_type=ct or "text/html", method="browser",
                        elapsed_ms=int((time.monotonic() - t0) * 1000))
        except Exception as e:
            return Page(url=url, requested_url=url, status=0, html="", method="browser",
                        error=f"{type(e).__name__}: {str(e).splitlines()[0]}",
                        elapsed_ms=int((time.monotonic() - t0) * 1000))
        finally:
            await page.close()

    async def close(self):
        try:
            if self._context:
                await self._context.close()
            if self._browser:
                await self._browser.close()
            if self._pw:
                await self._pw.stop()
        finally:
            self._context = self._browser = self._pw = None


class Fetcher:
    """Front door used by the crawler. Handles render=http|browser|auto."""

    def __init__(self, job: Job, client: httpx.AsyncClient, on_backoff=None):
        self.job = job
        self.http = HttpFetcher(client, job.politeness.retries, on_backoff)
        self.browser = BrowserFetcher(job) if job.render != "http" else None
        self._host_needs_js: dict[str, bool] = {}
        self.browser_error: str | None = None   # surfaced once by the crawler as a warning

    async def fetch(self, url: str) -> Page:
        mode = self.job.render
        if mode == "browser":
            return await self.browser.fetch(url)
        page = await self.http.fetch(url)
        if mode == "http" or not page.ok or not page.is_html or "html" not in page.content_type.lower():
            return page
        host = urlsplit(url).netloc
        needs = self._host_needs_js.get(host)
        if needs is None:
            needs = looks_js_rendered(page.html, self.job.browser.wait_for)
            self._host_needs_js[host] = needs
        if needs:
            bp = await self.browser.fetch(url)
            if bp.ok:
                return bp
            # browser failed: keep the HTTP result rather than nothing, and report why
            self.browser_error = bp.error
        return page

    async def close(self):
        if self.browser:
            await self.browser.close()
