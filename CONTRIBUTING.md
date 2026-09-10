# Contributing to mynah

Issues and focused pull requests are welcome. For bugs, include the operating
system, Python version, selected device (`mps`, `cuda`, or `cpu`), the steps that
trigger the problem, and the relevant Activity log. Do not attach private voice
recordings unless you deliberately want to publish them.

## Development setup

```bash
git clone https://github.com/seena18/mynah
cd mynah
uv sync --group dev
uv run run.py
```

The Python server serves the committed production frontend. To work on React,
leave it running on port 8765 and start Vite in another terminal:

```bash
cd frontend
npm ci
npm run dev
```

Vite proxies `/api` to the Python server. Run `npm run build` before committing
frontend changes; the generated files under `mynah/web/` belong in the commit.

## Checks

```bash
cd frontend && npm run build && cd ..
MYNAH_BROWSER_TESTS=1 uv run --group dev python -m unittest discover -v tests
```

The browser suite uses synthetic inference and temporary data, so it does not
download model weights or assess voice quality. Changes to model execution or
platform dependencies should also be tested with real inference on the affected
hardware.

Keep changes small enough to review. Explain the user-visible problem, the new
behavior, and how you verified it. Files under `data/` are intentionally ignored
and must never be added to a contribution.
