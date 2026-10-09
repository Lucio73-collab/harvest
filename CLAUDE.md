# Harvest

Polite web scraper: CLI (Typer) plus a local FastAPI dashboard, SQLite storage with change tracking.

## Commands
- Install: `uv venv && uv pip install -e ".[dev]"`, then `uv run playwright install chromium`
- Test: `uv run pytest` (spins up a local test shop from `tests/conftest.py`)
- Run a job: `uv run harvest run books -o books.xlsx`
- Dashboard: `uv run harvest serve` (127.0.0.1:8787)

## Layout
- `harvest/cli.py` entry, `crawler.py`, `fetch.py` (http/browser/auto), `extract.py`, `llm.py`, `storage.py`, `assets.py` (save HTML + JS/CSS)
- `harvest/web/` dashboard (FastAPI app + static UI)
- `jobs/*.yaml` example jobs, `tests/` pytest suite

## Conventions
- Politeness on by default: robots.txt, per-host delay, backoff on 429/503
- Python 3.11+, type hints, no em dashes in docs, Conventional Commits

## Never
- Commit `harvest.db*`, `html/`, `screenshots/`, exports, or personal job files
- Push or create releases without explicit confirmation
