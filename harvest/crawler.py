"""The crawl engine: queue, workers, robots, rate limits, link following, extraction, storage."""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import re
import time
from collections import deque
from typing import Any, Callable
from urllib.parse import parse_qsl, urldefrag, urlencode, urljoin, urlsplit, urlunsplit

from parsel import Selector

from . import notify
from .assets import AssetSaver
from .config import Job
from .extract import extract, extract_links, main_text
from .fetch import Fetcher, build_client, visible_text_len
from .llm import LLMExtractor
from .politeness import HostLimiter, RobotsCache, origin
from .storage import Store

SKIP_EXT = re.compile(
    r"\.(jpe?g|png|gif|webp|svg|ico|bmp|tiff?|mp[34]|m4a|wav|ogg|webm|avi|mov|mkv|pdf|zip|rar|7z|gz|tar|"
    r"exe|msi|dmg|apk|iso|bin|docx?|xlsx?|pptx?|css|js|woff2?|ttf|eot)(\?|$)", re.I)
TRACKING = re.compile(r"^(utm_.*|fbclid|gclid|mc_cid|mc_eid|ref_src|_ga)$", re.I)


def normalize(url: str) -> str:
    url, _ = urldefrag(url.strip())
    p = urlsplit(url)
    query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not TRACKING.match(k)])
    path = p.path or "/"
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), path, query, ""))


def _bare_host(url: str) -> str:
    h = (urlsplit(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


class Crawler:
    def __init__(self, job: Job, store: Store, on_event: Callable[[dict], None] | None = None,
                 max_pages: int | None = None):
        self.job = job
        self.store = store
        self.on_event = on_event
        self.max_pages = max_pages or job.crawl.max_pages
        self.stop_event = asyncio.Event()
        self.events: deque[dict] = deque(maxlen=400)
        self.run_id: int | None = None
        self.stats = dict(pages=0, errors=0, blocked=0, items=0, new_items=0, changed_items=0, removed_items=0)
        self._seen: set[str] = set()
        self._hosts = {_bare_host(u) for u in job.start_urls}
        self._follow = [re.compile(p) for p in job.crawl.follow]
        self._exclude = [re.compile(p) for p in job.crawl.exclude]
        self._only = [re.compile(p) for p in job.extract.only_urls]
        self._item_keys_this_run: set[str] = set()

    # ------------------------------------------------------------ helpers
    def _emit(self, kind: str, **data: Any) -> None:
        ev = {"t": time.time(), "kind": kind, **data}
        self.events.append(ev)
        if self.on_event:
            try:
                self.on_event(ev)
            except Exception:
                pass

    def stop(self) -> None:
        self.stop_event.set()

    def _allowed_link(self, url: str) -> bool:
        p = urlsplit(url)
        if p.scheme not in ("http", "https") or SKIP_EXT.search(p.path):
            return False
        if self.job.crawl.same_domain and _bare_host(url) not in self._hosts:
            return False
        if self._follow and not any(r.search(url) for r in self._follow):
            return False
        if any(r.search(url) for r in self._exclude):
            return False
        return True

    def _enqueue(self, q: asyncio.Queue, url: str, depth: int) -> bool:
        n = normalize(url)
        if n in self._seen or len(self._seen) >= self.max_pages:
            return False
        self._seen.add(n)
        q.put_nowait((n, depth))
        return True

    def _should_extract(self, url: str) -> bool:
        return not self._only or any(r.search(url) for r in self._only)

    # ------------------------------------------------------------ sitemaps
    async def _sitemap_urls(self, client, robots: RobotsCache) -> list[str]:
        found: list[str] = []
        roots: list[str] = []
        for u in self.job.start_urls:
            roots += await robots.sitemaps(u) or [f"{origin(u)}/sitemap.xml"]
        filters = [re.compile(p) for p in self.job.crawl.sitemap_filter]
        todo, done = list(dict.fromkeys(roots)), set()
        while todo and len(done) < 50 and len(found) < self.max_pages * 5:
            sm = todo.pop(0)
            if sm in done:
                continue
            done.add(sm)
            try:
                r = await client.get(sm)
                if r.status_code >= 400:
                    continue
                body = r.content
                if sm.endswith(".gz") or body[:2] == b"\x1f\x8b":
                    body = gzip.decompress(body)
                sel = Selector(text=body.decode("utf-8", "replace"), type="xml")
                sel.remove_namespaces()
                todo += sel.xpath("//sitemap/loc/text()").getall()
                for loc in sel.xpath("//url/loc/text()").getall():
                    loc = loc.strip()
                    if not filters or any(f.search(loc) for f in filters):
                        found.append(loc)
            except Exception as e:
                self._emit("warn", msg=f"sitemap {sm}: {e}")
        self._emit("info", msg=f"sitemaps: {len(found)} urls from {len(done)} files")
        return found

    # ------------------------------------------------------------ main
    async def run(self) -> dict:
        job = self.job
        self.run_id = run_id = self.store.start_run(job.name)
        self._emit("start", run_id=run_id, job=job.name)
        client = build_client(job)
        robots = RobotsCache(client, job.politeness.user_agent)
        limiter = HostLimiter(job.politeness.delay, job.politeness.per_host)
        fetcher = Fetcher(job, client, on_backoff=limiter.slow_down)
        llm = LLMExtractor(job.extract.llm) if job.extract.mode in ("llm", "hybrid") else None
        gate = asyncio.Semaphore(max(1, job.politeness.concurrency))
        self.saver = (AssetSaver(job.name, job.output, client, limiter,
                                 robots if job.politeness.respect_robots else None)
                      if job.output.save_html else None)
        q: asyncio.Queue = asyncio.Queue()
        status, message = "done", None

        try:
            for u in job.start_urls:
                self._enqueue(q, u, 0)
            if job.crawl.sitemap:
                for u in await self._sitemap_urls(client, robots):
                    if self._allowed_link(u):
                        self._enqueue(q, u, 0)

            async def worker():
                while True:
                    url, depth = await q.get()
                    try:
                        if not self.stop_event.is_set():
                            await self._process(url, depth, q, fetcher, robots, limiter, gate, llm)
                    except Exception as e:
                        self.stats["errors"] += 1
                        self._emit("error", url=url, msg=f"{type(e).__name__}: {e}")
                    finally:
                        q.task_done()

            workers = [asyncio.create_task(worker()) for _ in range(max(1, job.politeness.concurrency))]
            await q.join()
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

            if self.stop_event.is_set():
                status, message = "stopped", "stopped by user"
            elif self.stats["pages"] == 0 or self.stats["pages"] == self.stats["errors"]:
                status, message = "failed", "no pages fetched successfully"
            elif self.stats["errors"] == 0 and job.extract.mode != "auto":
                self.stats["removed_items"] = self.store.mark_removed(job.name, run_id)
        except Exception as e:
            status, message = "failed", f"{type(e).__name__}: {e}"
            self._emit("error", msg=message)
        finally:
            await fetcher.close()
            await client.aclose()
            if llm:
                await llm.close()
            self.store.update_run(run_id, **self.stats)
            self.store.finish_run(run_id, status, message)

        run = self.store.get_run(run_id)
        self._emit("done", run=run)
        await self._notify(run)
        return run

    async def _process(self, url, depth, q, fetcher: Fetcher, robots: RobotsCache,
                       limiter: HostLimiter, gate, llm) -> None:
        job = self.job
        run_id = self.run_id
        if job.politeness.respect_robots:
            if not await robots.allowed(url):
                self.stats["blocked"] += 1
                self.store.log_page(run_id, url, -1, "-", 0, 0, "disallowed by robots.txt", None)
                self._emit("blocked", url=url)
                self.store.update_run(run_id, **self.stats)
                return
            cd = await robots.crawl_delay(url)
            if cd:
                limiter.set_host_delay(urlsplit(url).netloc, cd)

        async with gate:
            async with limiter.acquire(url):
                page = await fetcher.fetch(url)

        self.stats["pages"] += 1
        if fetcher.browser_error:
            msg = fetcher.browser_error
            fetcher.browser_error = None
            if "doesn't exist" in msg or "playwright install" in msg:
                msg = "browser not installed, run: playwright install chromium (using plain HTTP instead)"
            self._emit("warn", url=url, msg=msg)
        if not page.ok:
            self.stats["errors"] += 1
            err = page.error or f"HTTP {page.status}"
            self.store.log_page(run_id, url, page.status, page.method, page.elapsed_ms, 0, err, None)
            self._emit("error", url=url, msg=err)
            self.store.update_run(run_id, **self.stats)
            return

        final = normalize(page.url)
        if final != url:
            if final in self._seen:   # redirected to something we already have
                self.store.log_page(run_id, url, page.status, page.method, page.elapsed_ms, 0,
                                    f"redirect to already-seen {final}", None)
                self.store.update_run(run_id, **self.stats)
                return
            self._seen.add(final)

        n_items = 0
        if page.is_html and page.html:
            if self._should_extract(final):
                items = await extract(page.html, final, job.extract, llm)
                if llm and llm.last_error:
                    self._emit("warn", url=final, msg=llm.last_error)
                for it in items:
                    it.setdefault("_url", final)
                    res = self.store.upsert_item(job.name, run_id, it, job.extract.key, final)
                    n_items += 1
                    if res == "new":
                        self.stats["new_items"] += 1
                    elif res == "changed":
                        self.stats["changed_items"] += 1
                self.stats["items"] += n_items

            # follow links
            if depth < job.crawl.max_depth:
                for link in extract_links(page.html, final):
                    if self._allowed_link(link):
                        self._enqueue(q, link, depth + 1)
            if job.crawl.pagination:
                sel = Selector(text=page.html, base_url=final)
                css = job.crawl.pagination
                hrefs = sel.css(css if "::" in css else f"{css}::attr(href)").getall()
                for h in hrefs:
                    nxt = urljoin(final, h)
                    if urlsplit(nxt).scheme in ("http", "https"):
                        self._enqueue(q, nxt, depth)   # pagination does not consume depth

        saved = None
        if self.saver and page.is_html and page.html:
            try:
                saved = str(await self.saver.save(page.html, final, page.method))
            except Exception as e:
                self._emit("warn", url=final, msg=f"could not save html: {e}")
        h = hashlib.sha1(main_text(page.html).encode()).hexdigest()[:16] if page.html else None
        self.store.log_page(run_id, final, page.status, page.method, page.elapsed_ms, n_items, None, h, saved)
        self._emit("page", url=final, status=page.status, method=page.method,
                   ms=page.elapsed_ms, items=n_items, chars=visible_text_len(page.html) if page.html else 0)
        self.store.update_run(run_id, **self.stats)

    async def _notify(self, run: dict) -> None:
        n = self.job.notify
        if not n.webhook:
            return
        changed = run["new_items"] + run["changed_items"] + run["removed_items"]
        if n.on == "changes" and not changed:
            return
        if n.on == "errors" and run["status"] == "done" and not run["errors"]:
            return
        try:
            await notify.send(n.webhook, self.job.name, run, self.store.changes(self.job.name, run["id"], 50))
            self._emit("info", msg="webhook sent")
        except Exception as e:
            self._emit("warn", msg=f"webhook failed: {e}")


async def run_job(job: Job, store: Store, on_event=None, max_pages: int | None = None) -> dict:
    return await Crawler(job, store, on_event, max_pages).run()
