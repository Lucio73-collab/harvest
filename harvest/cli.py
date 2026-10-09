"""Command line interface."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import export as exporter
from .config import ExtractConfig, FieldRule, Job, load_job
from .crawler import run_job
from .storage import Store
from .templates import TEMPLATES

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="Harvest: polite web scraper with crawling, JS rendering, smart extraction and a dashboard.")
con = Console()
GREEN = "#a6e3a1"

DbOpt = typer.Option("harvest.db", "--db", help="SQLite database file")


def _resolve_job(job: str, jobs_dir: str = "jobs") -> Path:
    p = Path(job)
    if p.exists():
        return p
    for ext in (".yaml", ".yml", ".json"):
        c = Path(jobs_dir) / f"{job}{ext}"
        if c.exists():
            return c
    raise typer.BadParameter(f"job file not found: {job}")


def _print_run(run: dict) -> None:
    color = {"done": GREEN, "failed": "red", "stopped": "yellow"}.get(run["status"], "white")
    con.print(f"[bold {color}]run #{run['id']} {run['status']}[/]  "
              f"pages {run['pages']}  items {run['items']}  new {run['new_items']}  "
              f"changed {run['changed_items']}  removed {run['removed_items']}  "
              f"errors {run['errors']}  robots-blocked {run['blocked']}"
              + (f"\n[dim]{run['message']}[/]" if run.get("message") else ""))


@app.command()
def init(name: str = typer.Argument("my-job"),
         template: str = typer.Option("list", "--template", "-t", help=f"one of: {', '.join(TEMPLATES)}"),
         jobs_dir: str = typer.Option("jobs", "--dir")):
    """Create a starter job file."""
    if template not in TEMPLATES:
        raise typer.BadParameter(f"template must be one of {list(TEMPLATES)}")
    p = Path(jobs_dir) / f"{name}.yaml"
    if p.exists():
        con.print(f"[red]{p} already exists[/]")
        raise typer.Exit(1)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(TEMPLATES[template].format(name=name), encoding="utf-8")
    con.print(f"[{GREEN}]created[/] {p}\nnext: [bold]harvest run {name}[/]")


@app.command()
def validate(job: str):
    """Check a job file for mistakes without running it."""
    try:
        j = load_job(_resolve_job(job))
    except Exception as e:
        con.print(f"[red]invalid:[/] {e}")
        raise typer.Exit(1)
    con.print(f"[{GREEN}]ok[/] {j.name}: {len(j.start_urls)} start url(s), render={j.render}, "
              f"extract={j.extract.mode}, fields={list(j.extract.fields) or '-'}, schedule={j.schedule or '-'}")


@app.command()
def run(job: str = typer.Argument(..., help="job file or name inside ./jobs"),
        db: str = DbOpt,
        max_pages: Optional[int] = typer.Option(None, help="override crawl.max_pages"),
        render: Optional[str] = typer.Option(None, help="override render: http|browser|auto"),
        out: Optional[str] = typer.Option(None, "--out", "-o", help="also export to this file (.json/.jsonl/.csv/.xlsx)"),
        save_html: bool = typer.Option(False, "--save-html", help="save each page's HTML + inline and external JS/CSS"),
        html_dir: Optional[str] = typer.Option(None, "--html-dir", help="where saved pages go (default: ./html)"),
        quiet: bool = typer.Option(False, "-q")):
    """Run a job once."""
    j = load_job(_resolve_job(job))
    if render:
        j.render = render  # type: ignore[assignment]
    if save_html:
        j.output.save_html = True
    if html_dir:
        j.output.dir = html_dir
    store = Store(db)

    def on_event(ev: dict):
        if quiet:
            return
        k = ev["kind"]
        if k == "page":
            con.print(f"[{GREEN}]{ev['status']}[/] [dim]{ev['method']:7} {ev['ms']:>5}ms[/] "
                      f"{ev['url']}  [bold]{ev['items'] or ''}[/]")
        elif k == "blocked":
            con.print(f"[yellow]robots[/] {ev['url']}")
        elif k in ("error", "warn"):
            con.print(f"[red]{k}[/] {ev.get('url', '')} {ev.get('msg', '')}")
        elif k == "info":
            con.print(f"[dim]{ev['msg']}[/]")

    result = asyncio.run(run_job(j, store, on_event, max_pages))
    _print_run(result)
    if out:
        items = store.items(j.name, run_id=result["id"])
        p = exporter.write(items, out)
        con.print(f"exported {len(items)} items to [bold]{p}[/]")
    if j.output.save_html:
        con.print(f"pages saved to [bold]{Path(j.output.dir) / j.name}[/]")
    if result["status"] == "failed":
        raise typer.Exit(1)


@app.command()
def quick(url: str,
          select: list[str] = typer.Option([], "--select", "-s", help="name=css selector, repeatable"),
          item: Optional[str] = typer.Option(None, help="CSS selector for repeating items"),
          render: str = typer.Option("auto", help="http|browser|auto"),
          out: Optional[str] = typer.Option(None, "--out", "-o"),
          save_html: Optional[str] = typer.Option(None, "--save-html", help="folder to save the HTML + JS/CSS into"),
          ignore_robots: bool = typer.Option(False, help="skip robots.txt check (only for sites you own)")):
    """Scrape one URL right now, without a job file or database."""
    fields = {}
    for s in select:
        if "=" not in s:
            raise typer.BadParameter("--select must look like name=css")
        k, v = s.split("=", 1)
        fields[k.strip()] = FieldRule(css=v.strip())
    j = Job(name="quick", start_urls=[url], render=render,  # type: ignore[arg-type]
            extract=ExtractConfig(mode="rules" if fields else "auto", fields=fields, item=item))
    j.crawl.max_pages, j.crawl.max_depth = 1, 0
    j.politeness.respect_robots = not ignore_robots
    if save_html:
        j.output.save_html, j.output.dir = True, save_html
    store = Store(":memory:")
    result = asyncio.run(run_job(j, store))
    pages = store.list_pages(result["id"])
    if pages and pages[0]["error"]:
        con.print(f"[red]{pages[0]['error']}[/]")
        raise typer.Exit(1)
    items = store.items("quick", run_id=result["id"])
    for i in items:
        for k in ("_key", "_first_seen", "_updated"):
            i.pop(k, None)
    if save_html and pages and pages[0]["html_path"]:
        con.print(f"page saved to [bold]{pages[0]['html_path']}[/]", highlight=False)
    if out:
        exporter.write(items, out)
        con.print(f"saved {len(items)} item(s) to {out}")
    else:
        sys.stdout.write(json.dumps(items if len(items) != 1 else items[0], ensure_ascii=False, indent=2) + "\n")


@app.command()
def runs(job: Optional[str] = typer.Argument(None), db: str = DbOpt, limit: int = 20):
    """List recent runs."""
    t = Table(header_style=f"bold {GREEN}", box=None)
    for c in ("id", "job", "status", "started", "pages", "items", "new", "changed", "removed", "errors"):
        t.add_column(c)
    for r in Store(db).list_runs(job, limit):
        t.add_row(str(r["id"]), r["job"], r["status"], r["started_at"][:19].replace("T", " "),
                  str(r["pages"]), str(r["items"]), str(r["new_items"]), str(r["changed_items"]),
                  str(r["removed_items"]), str(r["errors"]))
    con.print(t)


@app.command()
def changes(job: str, run_id: Optional[int] = typer.Option(None, "--run"), db: str = DbOpt):
    """Show what was new, changed or removed in a run (default: latest)."""
    rows = Store(db).changes(job, run_id)
    if not rows:
        con.print("[dim]no changes[/]")
    for c in rows:
        col = {"new": GREEN, "changed": "yellow", "removed": "red"}[c["change"]]
        line = f"[{col}]{c['change']:8}[/] {c['key'][:70]}"
        if c.get("diff"):
            line += "  " + ", ".join(f"[bold]{k}[/] {o!r} -> {n!r}" for k, (o, n) in c["diff"].items())
        con.print(line)


@app.command("export")
def export_cmd(job: str, out: str = typer.Argument(..., help="output file; format from extension"),
               fmt: Optional[str] = typer.Option(None, "--format", "-f", help="json|jsonl|csv|xlsx"),
               run_id: Optional[int] = typer.Option(None, "--run"),
               all_items: bool = typer.Option(False, "--all", help="every item ever seen, not just the latest run"),
               search: Optional[str] = typer.Option(None, help="only items containing this text"),
               db: str = DbOpt):
    """Export stored items."""
    items = Store(db).items(job, run_id=run_id, only_current=not all_items, search=search)
    p = exporter.write(items, out, fmt)
    con.print(f"[{GREEN}]exported[/] {len(items)} items to {p}")


@app.command()
def schedule(jobs_dir: str = typer.Argument("jobs"), db: str = DbOpt):
    """Run every job that has a `schedule:` field, forever (Ctrl+C to stop)."""
    from .schedule import Scheduler, load_jobs
    store = Store(db)
    store.mark_stale_runs()
    running: set[str] = set()

    async def main():
        def launch(job: Job):
            async def go():
                running.add(job.name)
                con.print(f"[{GREEN}]starting[/] {job.name}")
                try:
                    _print_run(await run_job(job, store))
                finally:
                    running.discard(job.name)
            asyncio.create_task(go())
        sch = Scheduler(jobs_dir, launch, lambda n: n in running)
        found = [n for n, (_, j) in load_jobs(jobs_dir).items() if not isinstance(j, Exception) and j.schedule]
        con.print(f"scheduler watching [bold]{jobs_dir}[/]: {', '.join(found) or 'no scheduled jobs yet'}")
        await sch.loop()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        con.print("stopped")


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8787, jobs_dir: str = typer.Option("jobs", "--jobs"),
          db: str = DbOpt, with_schedule: bool = typer.Option(True, "--schedule/--no-schedule")):
    """Open the local dashboard."""
    import uvicorn
    from .web.app import create_app
    con.print(f"dashboard on [bold {GREEN}]http://{host}:{port}[/]")
    uvicorn.run(create_app(jobs_dir, db, with_schedule), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    app()
