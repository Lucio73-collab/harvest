"""Scheduling: 'every 30m' style intervals or cron expressions, run from a jobs folder."""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from croniter import croniter

from .config import Job, load_job

UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def next_run(spec: str, after: float | None = None) -> float:
    """Return the next run time (epoch seconds) for a schedule spec."""
    after = after or time.time()
    s = spec.strip().lower()
    m = re.fullmatch(r"(?:every\s+)?(\d+)\s*([smhd])", s)
    if m:
        return after + int(m.group(1)) * UNITS[m.group(2)]
    if s in ("hourly", "daily", "weekly"):
        s = {"hourly": "0 * * * *", "daily": "0 8 * * *", "weekly": "0 8 * * 1"}[s]
    if croniter.is_valid(s):
        return croniter(s, datetime.fromtimestamp(after)).get_next(float)
    raise ValueError(f"bad schedule {spec!r}: use 'every 30m', 'every 6h', 'daily' or a cron expression")


def load_jobs(folder: str | Path) -> dict[str, tuple[Path, Job | Exception]]:
    out: dict[str, tuple[Path, Job | Exception]] = {}
    for p in sorted(Path(folder).glob("*")):
        if p.suffix not in (".yaml", ".yml", ".json"):
            continue
        try:
            j = load_job(p)
            out[j.name] = (p, j)
        except Exception as e:
            out[p.stem] = (p, e)
    return out


class Scheduler:
    """Polls the jobs folder (so edits are picked up live) and launches due jobs."""

    def __init__(self, folder: str | Path, launch: Callable[[Job], "asyncio.Future | None"],
                 is_running: Callable[[str], bool], tick: float = 15):
        self.folder = Path(folder)
        self.launch = launch
        self.is_running = is_running
        self.tick = tick
        self.next: dict[str, float] = {}
        self.specs: dict[str, str] = {}
        self._task: asyncio.Task | None = None

    def snapshot(self) -> dict[str, dict]:
        return {k: {"schedule": self.specs.get(k), "next": v} for k, v in self.next.items()}

    async def loop(self) -> None:
        while True:
            now = time.time()
            for name, (_, job) in load_jobs(self.folder).items():
                if isinstance(job, Exception) or not job.schedule:
                    self.next.pop(name, None)
                    continue
                if self.specs.get(name) != job.schedule:      # new or edited schedule
                    self.specs[name] = job.schedule
                    try:
                        self.next[name] = next_run(job.schedule, now)
                    except ValueError:
                        self.next.pop(name, None)
                    continue
                due = self.next.get(name)
                if due and now >= due and not self.is_running(name):
                    self.launch(job)
                    self.next[name] = next_run(job.schedule, now)
            await asyncio.sleep(self.tick)

    def start(self) -> asyncio.Task:
        self._task = asyncio.create_task(self.loop())
        return self._task
