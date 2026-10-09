"""SQLite storage: runs, page log, items with dedupe + change history."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL DEFAULT 'running',     -- running | done | failed | stopped
  pages INTEGER DEFAULT 0,
  errors INTEGER DEFAULT 0,
  blocked INTEGER DEFAULT 0,                  -- skipped by robots.txt
  items INTEGER DEFAULT 0,
  new_items INTEGER DEFAULT 0,
  changed_items INTEGER DEFAULT 0,
  removed_items INTEGER DEFAULT 0,
  message TEXT
);
CREATE TABLE IF NOT EXISTS pages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL,
  url TEXT NOT NULL,
  status INTEGER,
  method TEXT,
  elapsed_ms INTEGER,
  items INTEGER DEFAULT 0,
  error TEXT,
  content_hash TEXT,
  fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS pages_run ON pages(run_id);
CREATE TABLE IF NOT EXISTS items (
  job TEXT NOT NULL,
  key TEXT NOT NULL,
  url TEXT,
  data TEXT NOT NULL,
  data_hash TEXT NOT NULL,
  first_seen_run INTEGER NOT NULL,
  last_seen_run INTEGER NOT NULL,
  changed_run INTEGER,
  first_seen_at TEXT,
  updated_at TEXT,
  PRIMARY KEY (job, key)
);
CREATE INDEX IF NOT EXISTS items_job_seen ON items(job, last_seen_run);
CREATE TABLE IF NOT EXISTS item_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job TEXT NOT NULL,
  key TEXT NOT NULL,
  run_id INTEGER NOT NULL,
  change TEXT NOT NULL,                       -- new | changed | removed
  old_data TEXT,
  new_data TEXT,
  at TEXT
);
CREATE INDEX IF NOT EXISTS hist_job ON item_history(job, run_id);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stable_hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _strip_volatile(item: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in item.items() if not k.startswith("_")}


class Store:
    def __init__(self, path: str | Path = "harvest.db"):
        self.path = str(path)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(pages)")}
        if "html_path" not in cols:   # migrate databases from before html saving existed
            self.conn.execute("ALTER TABLE pages ADD COLUMN html_path TEXT")

    # ------------------------------------------------------------ runs
    def start_run(self, job: str) -> int:
        with self._lock:
            cur = self.conn.execute("INSERT INTO runs(job, started_at) VALUES (?, ?)", (job, now()))
            return int(cur.lastrowid)

    def update_run(self, run_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self.conn.execute(f"UPDATE runs SET {cols} WHERE id=?", (*fields.values(), run_id))

    def finish_run(self, run_id: int, status: str = "done", message: str | None = None) -> None:
        self.update_run(run_id, status=status, finished_at=now(), message=message)

    def get_run(self, run_id: int) -> dict | None:
        r = self.conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(r) if r else None

    def list_runs(self, job: str | None = None, limit: int = 50) -> list[dict]:
        q, args = "SELECT * FROM runs", []
        if job:
            q += " WHERE job=?"
            args.append(job)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.conn.execute(q, args)]

    def last_run(self, job: str, status: str | None = "done") -> dict | None:
        q = "SELECT * FROM runs WHERE job=?" + (" AND status=?" if status else "") + " ORDER BY id DESC LIMIT 1"
        r = self.conn.execute(q, (job, status) if status else (job,)).fetchone()
        return dict(r) if r else None

    def mark_stale_runs(self) -> None:
        """Runs left 'running' by a crashed process."""
        with self._lock:
            self.conn.execute("UPDATE runs SET status='failed', message='interrupted' WHERE status='running'")

    # ------------------------------------------------------------ pages
    def log_page(self, run_id: int, url: str, status: int, method: str, elapsed_ms: int,
                 items: int, error: str | None, content_hash: str | None, html_path: str | None = None) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO pages(run_id,url,status,method,elapsed_ms,items,error,content_hash,fetched_at,html_path)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (run_id, url, status, method, elapsed_ms, items, error, content_hash, now(), html_path),
            )

    def get_page(self, page_id: int) -> dict | None:
        r = self.conn.execute("SELECT * FROM pages WHERE id=?", (page_id,)).fetchone()
        return dict(r) if r else None

    def list_pages(self, run_id: int, limit: int = 500) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM pages WHERE run_id=? ORDER BY id LIMIT ?", (run_id, limit))]

    # ------------------------------------------------------------ items
    def upsert_item(self, job: str, run_id: int, item: dict[str, Any], key_field: str | None, url: str) -> str:
        """Returns 'new', 'changed' or 'same'."""
        core = _strip_volatile(item)
        key_val = core.get(key_field) if key_field else None
        key = str(key_val) if key_val not in (None, "") else stable_hash(core)
        h = stable_hash(core)
        data = json.dumps(item, ensure_ascii=False, default=str)
        ts = now()
        with self._lock:
            row = self.conn.execute("SELECT data, data_hash FROM items WHERE job=? AND key=?", (job, key)).fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO items(job,key,url,data,data_hash,first_seen_run,last_seen_run,first_seen_at,updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?)", (job, key, url, data, h, run_id, run_id, ts, ts))
                self.conn.execute(
                    "INSERT INTO item_history(job,key,run_id,change,new_data,at) VALUES (?,?,?,?,?,?)",
                    (job, key, run_id, "new", data, ts))
                return "new"
            if row["data_hash"] != h:
                self.conn.execute(
                    "UPDATE items SET data=?, data_hash=?, url=?, last_seen_run=?, changed_run=?, updated_at=?"
                    " WHERE job=? AND key=?", (data, h, url, run_id, run_id, ts, job, key))
                self.conn.execute(
                    "INSERT INTO item_history(job,key,run_id,change,old_data,new_data,at) VALUES (?,?,?,?,?,?,?)",
                    (job, key, run_id, "changed", row["data"], data, ts))
                return "changed"
            self.conn.execute("UPDATE items SET last_seen_run=?, data=? WHERE job=? AND key=?",
                              (run_id, data, job, key))
            return "same"

    def mark_removed(self, job: str, run_id: int) -> int:
        """Items seen in the previous completed run but not in this one."""
        prev = self.conn.execute(
            "SELECT id FROM runs WHERE job=? AND id<? AND status='done' ORDER BY id DESC LIMIT 1",
            (job, run_id)).fetchone()
        if not prev:
            return 0
        with self._lock:
            rows = self.conn.execute(
                "SELECT key, data FROM items WHERE job=? AND last_seen_run=?", (job, prev["id"])).fetchall()
            for r in rows:
                self.conn.execute(
                    "INSERT INTO item_history(job,key,run_id,change,old_data,at) VALUES (?,?,?,?,?,?)",
                    (job, r["key"], run_id, "removed", r["data"], now()))
            return len(rows)

    def items(self, job: str, run_id: int | None = None, only_current: bool = True,
              search: str | None = None, limit: int | None = None, offset: int = 0) -> list[dict]:
        q, args = "SELECT * FROM items WHERE job=?", [job]
        if run_id is not None:
            q += " AND last_seen_run=?"
            args.append(run_id)
        elif only_current:
            last = self.conn.execute("SELECT MAX(last_seen_run) m FROM items WHERE job=?", (job,)).fetchone()["m"]
            if last is not None:
                q += " AND last_seen_run=?"
                args.append(last)
        if search:
            q += " AND data LIKE ?"
            args.append(f"%{search}%")
        q += " ORDER BY first_seen_run, rowid"
        if limit:
            q += " LIMIT ? OFFSET ?"
            args += [limit, offset]
        out = []
        for r in self.conn.execute(q, args):
            d = json.loads(r["data"])
            d["_key"] = r["key"]
            d["_first_seen"] = r["first_seen_at"]
            d["_updated"] = r["updated_at"]
            out.append(d)
        return out

    def count_items(self, job: str) -> int:
        last = self.conn.execute("SELECT MAX(last_seen_run) m FROM items WHERE job=?", (job,)).fetchone()["m"]
        if last is None:
            return 0
        return self.conn.execute("SELECT COUNT(*) c FROM items WHERE job=? AND last_seen_run=?",
                                 (job, last)).fetchone()["c"]

    def changes(self, job: str, run_id: int | None = None, limit: int = 500) -> list[dict]:
        if run_id is None:
            r = self.last_run(job)
            run_id = r["id"] if r else -1
        rows = self.conn.execute(
            "SELECT * FROM item_history WHERE job=? AND run_id=? ORDER BY id LIMIT ?", (job, run_id, limit))
        out = []
        for r in rows:
            d = dict(r)
            d["old_data"] = json.loads(d["old_data"]) if d["old_data"] else None
            d["new_data"] = json.loads(d["new_data"]) if d["new_data"] else None
            if d["old_data"] and d["new_data"]:
                d["diff"] = {k: [d["old_data"].get(k), d["new_data"].get(k)]
                             for k in set(d["old_data"]) | set(d["new_data"])
                             if not k.startswith("_") and d["old_data"].get(k) != d["new_data"].get(k)}
            out.append(d)
        return out

    def jobs(self) -> list[str]:
        return [r["job"] for r in self.conn.execute("SELECT DISTINCT job FROM runs ORDER BY job")]

    def delete_job_data(self, job: str) -> None:
        with self._lock:
            for t in ("items", "item_history", "runs"):
                self.conn.execute(f"DELETE FROM {t} WHERE job=?", (job,))
            self.conn.execute("DELETE FROM pages WHERE run_id NOT IN (SELECT id FROM runs)")
