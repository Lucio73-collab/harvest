"""Local dashboard: FastAPI backend + a single-page UI in static/index.html."""
from __future__ import annotations

import asyncio
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response

from .. import export as exporter
from ..config import ExtractConfig, FieldRule, Job, load_job, parse_job_text
from ..crawler import Crawler
from ..schedule import Scheduler, load_jobs
from ..storage import Store
from ..templates import TEMPLATES

STATIC = Path(__file__).parent / "static"
NAME_RE = re.compile(r"^[\w.-]{1,80}$")


def create_app(jobs_dir: str = "jobs", db: str = "harvest.db", with_schedule: bool = True) -> FastAPI:
    jobs_path = Path(jobs_dir)
    jobs_path.mkdir(parents=True, exist_ok=True)
    store = Store(db)
    store.mark_stale_runs()
    active: dict[str, Crawler] = {}
    finished_events: dict[int, list] = {}
    state: dict = {"scheduler": None}

    def launch(job: Job) -> Crawler:
        c = Crawler(job, store)
        active[job.name] = c

        async def go():
            try:
                await c.run()
            finally:
                if c.run_id is not None:
                    finished_events[c.run_id] = list(c.events)
                    for k in list(finished_events)[:-20]:
                        finished_events.pop(k, None)
                active.pop(job.name, None)
        asyncio.create_task(go())
        return c

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if with_schedule:
            sch = Scheduler(jobs_path, launch, lambda n: n in active)
            state["scheduler"] = sch
            sch.start()
        yield
        for c in active.values():
            c.stop()

    app = FastAPI(title="Harvest", lifespan=lifespan)

    def job_file(name: str) -> Path:
        if not NAME_RE.match(name):
            raise HTTPException(400, "invalid job name")
        for ext in (".yaml", ".yml", ".json"):
            p = jobs_path / f"{name}{ext}"
            if p.exists():
                return p
        # job name may differ from file stem
        for n, (p, _) in load_jobs(jobs_path).items():
            if n == name:
                return p
        raise HTTPException(404, f"job {name} not found")

    def get_job(name: str) -> Job:
        try:
            return load_job(job_file(name))
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(422, f"job file invalid: {e}")

    # ------------------------------------------------------------ pages
    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    # ------------------------------------------------------------ jobs
    @app.get("/api/jobs")
    def list_jobs():
        sch = state["scheduler"].snapshot() if state["scheduler"] else {}
        out = []
        for name, (path, job) in load_jobs(jobs_path).items():
            ok = not isinstance(job, Exception)
            last = store.last_run(name, status=None)
            out.append({
                "name": name, "file": path.name, "valid": ok,
                "error": None if ok else str(job).splitlines()[0][:300],
                "schedule": job.schedule if ok else None,
                "next_run": sch.get(name, {}).get("next"),
                "render": job.render if ok else None,
                "mode": job.extract.mode if ok else None,
                "start_urls": job.start_urls if ok else [],
                "running": name in active,
                "live_run": active[name].run_id if name in active else None,
                "last_run": last,
                "items": store.count_items(name),
            })
        return out

    @app.post("/api/jobs")
    def create_job(payload: dict = Body(...)):
        name = str(payload.get("name", "")).strip()
        tpl = payload.get("template", "auto")
        if not NAME_RE.match(name):
            raise HTTPException(400, "name may only contain letters, numbers, dot, dash, underscore")
        if tpl not in TEMPLATES:
            raise HTTPException(400, "unknown template")
        p = jobs_path / f"{name}.yaml"
        if p.exists():
            raise HTTPException(409, "a job with that name exists")
        text = TEMPLATES[tpl].format(name=name)
        url = str(payload.get("url") or "").strip()
        if url:
            if not re.match(r"^https?://\S+$", url):
                raise HTTPException(400, "URL must start with http:// or https://")
            text = re.sub(r"start_urls:\n(  - .*\n)+", lambda _: f"start_urls:\n  - {url}\n", text, count=1)
        p.write_text(text, encoding="utf-8")
        return {"name": name, "file": p.name}

    @app.get("/api/jobs/{name}/source")
    def job_source(name: str):
        return {"text": job_file(name).read_text(encoding="utf-8")}

    @app.put("/api/jobs/{name}/source")
    def save_source(name: str, payload: dict = Body(...)):
        text = payload.get("text", "")
        try:
            job = parse_job_text(text, name)
        except Exception as e:
            raise HTTPException(422, str(e))
        if job.name != name:
            raise HTTPException(422, f"'name' in the file ({job.name}) must match the job ({name}); create a new job to rename")
        job_file(name).write_text(text, encoding="utf-8")
        return {"ok": True}

    @app.post("/api/validate")
    def validate(payload: dict = Body(...)):
        try:
            j = parse_job_text(payload.get("text", ""))
            return {"ok": True, "summary": f"{len(j.start_urls)} start url(s), render {j.render}, "
                                           f"extract {j.extract.mode}, {len(j.extract.fields)} field(s)"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @app.delete("/api/jobs/{name}")
    def delete_job(name: str, data: bool = False):
        if name in active:
            raise HTTPException(409, "job is running")
        job_file(name).unlink()
        if data:
            store.delete_job_data(name)
        return {"ok": True}

    @app.post("/api/jobs/{name}/run")
    async def start(name: str, max_pages: int | None = None):
        if name in active:
            raise HTTPException(409, "already running")
        job = get_job(name)
        if max_pages:
            job.crawl.max_pages = max_pages
        launch(job)
        return {"started": True}

    @app.post("/api/jobs/{name}/stop")
    def stop(name: str):
        c = active.get(name)
        if not c:
            raise HTTPException(404, "not running")
        c.stop()
        return {"stopping": True}

    # ------------------------------------------------------------ data
    @app.get("/api/runs")
    def runs(job: str | None = None, limit: int = 50):
        return store.list_runs(job, limit)

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: int, since: float = 0):
        r = store.get_run(run_id)
        if not r:
            raise HTTPException(404)
        live = next((c for c in active.values() if c.run_id == run_id), None)
        events = list(live.events) if live else finished_events.get(run_id, [])
        return {"run": r, "live": live is not None,
                "events": [e for e in events if e["t"] > since and e["kind"] != "done"],
                "pages": store.list_pages(run_id, 1000) if not live else []}

    @app.get("/api/jobs/{name}/items")
    def items(name: str, search: str | None = None, run: int | None = None,
              limit: int = Query(200, le=5000), offset: int = 0):
        rows = store.items(name, run_id=run, search=search, limit=limit, offset=offset)
        total = len(store.items(name, run_id=run, search=search))
        return {"total": total, "items": rows}

    @app.get("/api/jobs/{name}/changes")
    def changes(name: str, run: int | None = None):
        return store.changes(name, run)

    @app.get("/api/jobs/{name}/export")
    def export(name: str, fmt: str = "csv", run: int | None = None, search: str | None = None,
               all_items: bool = False):
        if fmt not in exporter.FORMATS:
            raise HTTPException(400, "bad format")
        rows = store.items(name, run_id=run, only_current=not all_items, search=search)
        media = {"json": "application/json", "jsonl": "application/x-ndjson", "csv": "text/csv",
                 "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}[fmt]
        stamp = time.strftime("%Y%m%d-%H%M")
        return Response(exporter.to_bytes(rows, fmt), media_type=media,
                        headers={"Content-Disposition": f'attachment; filename="{name}-{stamp}.{fmt}"'})

    # ------------------------------------------------------------ saved html
    def _zip_dirs(dirs: list[Path], base: Path) -> bytes:
        import io, zipfile
        buf = io.BytesIO()
        added: set[Path] = set()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for d in dirs:
                files = [f for f in d.rglob("*") if f.is_file()]
                # pull in the shared _assets the page references
                man = d / "manifest.json"
                if man.exists():
                    import json as _json
                    m = _json.loads(man.read_text(encoding="utf-8"))
                    for e in m.get("external", []):
                        if e.get("local"):
                            files.append((d / e["local"]).resolve())
                        for imp in e.get("imports") or []:
                            if imp.get("path"):
                                files.append(Path(imp["path"]).resolve())
                for f in files:
                    f = Path(f).resolve()
                    if f in added or not f.exists():
                        continue
                    added.add(f)
                    try:
                        arc = f.relative_to(base.resolve())
                    except ValueError:
                        continue
                    z.write(f, str(arc))
        return buf.getvalue()

    @app.get("/api/runs/{run_id}/html.zip")
    def run_html_zip(run_id: int):
        r = store.get_run(run_id)
        if not r:
            raise HTTPException(404)
        dirs = [Path(p["html_path"]) for p in store.list_pages(run_id, 100000) if p.get("html_path")]
        dirs = [d for d in dirs if d.exists()]
        if not dirs:
            raise HTTPException(404, "no saved pages in this run; set output.save_html: true")
        base = dirs[0].parents[2]   # <dir>/<job>/<host>/<page> -> <dir>
        return Response(_zip_dirs(dirs, base), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{r["job"]}-run{run_id}-html.zip"'})

    @app.get("/api/pages/{page_id}/files")
    def page_files(page_id: int):
        p = store.get_page(page_id)
        if not p or not p.get("html_path") or not Path(p["html_path"]).exists():
            raise HTTPException(404, "page not saved")
        d = Path(p["html_path"])
        man = d / "manifest.json"
        return {"dir": str(d.resolve()), "manifest": __import__("json").loads(man.read_text(encoding="utf-8")) if man.exists() else {}}

    @app.get("/api/pages/{page_id}/zip")
    def page_zip(page_id: int):
        p = store.get_page(page_id)
        if not p or not p.get("html_path") or not Path(p["html_path"]).exists():
            raise HTTPException(404, "page not saved")
        d = Path(p["html_path"])
        return Response(_zip_dirs([d], d.parents[2]), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{d.name}.zip"'})

    # ------------------------------------------------------------ quick scrape
    @app.post("/api/quick")
    async def quick(payload: dict = Body(...)):
        url = str(payload.get("url", "")).strip()
        if not re.match(r"^https?://", url):
            raise HTTPException(400, "URL must start with http:// or https://")
        fields = {}
        for line in str(payload.get("selectors", "")).splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                if k.strip() and v.strip():
                    fields[k.strip()] = FieldRule(css=v.strip())
        try:
            job = Job(name="__quick__", start_urls=[url], render=payload.get("render", "auto"),
                      extract=ExtractConfig(mode="rules" if fields else "auto", fields=fields,
                                            item=(payload.get("item") or None)))
        except Exception as e:
            raise HTTPException(422, str(e))
        job.crawl.max_pages, job.crawl.max_depth = 1, 0
        if payload.get("save_html"):
            job.output.save_html, job.output.dir = True, "html/_quick"
        mem = Store(":memory:")
        result = await Crawler(job, mem).run()
        pages = mem.list_pages(result["id"])
        rows = mem.items("__quick__", run_id=result["id"])
        for r in rows:
            for k in ("_key", "_first_seen", "_updated"):
                r.pop(k, None)
        page = pages[0] if pages else {}
        return {"page": page, "items": rows}

    @app.get("/api/templates")
    def templates():
        return list(TEMPLATES)

    return app
