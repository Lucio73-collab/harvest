"""robots.txt handling and per-host rate limiting."""
from __future__ import annotations

import asyncio
import time
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx


def origin(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


class RobotsCache:
    """Fetches and caches robots.txt per origin. Unreachable robots.txt means 'allowed'."""

    def __init__(self, client: httpx.AsyncClient, user_agent: str):
        self.client = client
        self.ua = user_agent
        self._parsers: dict[str, RobotFileParser | None] = {}
        self._lock = asyncio.Lock()

    async def _get(self, url: str) -> RobotFileParser | None:
        o = origin(url)
        async with self._lock:
            if o in self._parsers:
                return self._parsers[o]
            rp: RobotFileParser | None = None
            try:
                r = await self.client.get(f"{o}/robots.txt", timeout=10)
                if r.status_code < 400:
                    rp = RobotFileParser()
                    rp.parse(r.text.splitlines())
                elif r.status_code in (401, 403):
                    # robots.txt itself is forbidden: be conservative, treat site as disallowed
                    rp = RobotFileParser()
                    rp.parse(["User-agent: *", "Disallow: /"])
            except httpx.HTTPError:
                rp = None
            self._parsers[o] = rp
            return rp

    async def allowed(self, url: str) -> bool:
        rp = await self._get(url)
        return True if rp is None else rp.can_fetch(self.ua, url)

    async def crawl_delay(self, url: str) -> float | None:
        rp = await self._get(url)
        if rp is None:
            return None
        d = rp.crawl_delay(self.ua)
        return float(d) if d is not None else None

    async def sitemaps(self, url: str) -> list[str]:
        rp = await self._get(url)
        return list(rp.site_maps() or []) if rp else []


class HostLimiter:
    """Ensures at most `per_host` in-flight requests and `delay` seconds between starts, per host."""

    def __init__(self, delay: float, per_host: int):
        self.delay = delay
        self.per_host = max(1, per_host)
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._extra: dict[str, float] = {}

    def set_host_delay(self, host: str, delay: float) -> None:
        self._extra[host] = max(self._extra.get(host, 0), delay)

    def slow_down(self, host: str, seconds: float) -> None:
        """Called on 429 / Retry-After: push the next allowed start into the future."""
        self._last[host] = max(self._last.get(host, 0), time.monotonic() + seconds)

    def _host(self, url: str) -> str:
        return urlsplit(url).netloc

    def acquire(self, url: str) -> "_Slot":
        return _Slot(self, self._host(url))


class _Slot:
    def __init__(self, lim: HostLimiter, host: str):
        self.lim, self.host = lim, host

    async def __aenter__(self):
        lim = self.lim
        sem = lim._sems.setdefault(self.host, asyncio.Semaphore(lim.per_host))
        lock = lim._locks.setdefault(self.host, asyncio.Lock())
        await sem.acquire()
        async with lock:
            gap = max(lim.delay, lim._extra.get(self.host, 0))
            wait = lim._last.get(self.host, 0) + gap - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            lim._last[self.host] = time.monotonic()
        return self

    async def __aexit__(self, *exc):
        self.lim._sems[self.host].release()
