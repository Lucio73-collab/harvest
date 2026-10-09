# Contributing to harvest

Thanks for helping out. Keep changes small and focused.

## Setup
```
uv venv
uv pip install -e ".[dev]"
uv run playwright install chromium
```

## Run and test
```
uv run harvest serve
uv run pytest
```
Tests must pass before you open a pull request.

## Commits
Use [Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`, `docs:`, `refactor:`, `test:`.

## Pull requests
- One topic per PR, with a short description of what and why
- Update the README when behavior or commands change
- Never commit secrets, personal data or local config
