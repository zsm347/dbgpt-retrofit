# AGENTS.md

Notes for AI coding agents working in this repository.

## Local environment (macOS / Apple Silicon)

- **Python 3.11 IS installed.** This project is a `uv` workspace and `.python-version` pins `3.11`.
  - The interpreter is Homebrew's `/opt/homebrew/opt/python@3.11/bin/python3.11` (3.11.15).
  - `uv sync` / `uv run` resolve it automatically — do **not** try to install Python 3.11.
- **Do NOT use bare `python3 -V` to decide whether Python 3.11 exists.**
  - `python3` resolves to `/usr/bin/python3` (macOS system Python, 3.9.6), because Homebrew only
    links `python3.11` and not `python3`. This is expected and harmless.
  - Correct checks: `uv python find 3.11`, `uv run python -V`, or `python3.11 -V`.
- `uv` is available on PATH (0.11.x).

## Install and start

```bash
# Install dependencies (extras for the openai profile)
uv sync --all-packages --extra base --extra proxy_openai --extra rag --extra storage_chromadb --extra dbgpts

# Start the server
uv run dbgpt start webserver --profile openai      # wizard/profile mode
uv run dbgpt start webserver --config configs/dbgpt-proxy-openai.toml   # explicit config
```

Web UI: http://localhost:5670 (host `0.0.0.0`, port `5670`).

## Configuration

- Provider profiles: `openai` / `kimi` / `qwen` / `minimax` / `glm` / `custom` / `default`
  (see `packages/dbgpt-core/src/dbgpt/cli/_profiles.py`).
- `uv run dbgpt setup` writes `~/.dbgpt/configs/<profile>.toml`; `uv run dbgpt setup --show`
  prints the active profile and config path.
- API key env vars: `OPENAI_API_KEY`, `MOONSHOT_API_KEY`, `DASHSCOPE_API_KEY`,
  `MINIMAX_API_KEY`, `ZHIPUAI_API_KEY`. Kimi's *embeddings* also need `OPENAI_API_KEY`.
- Ready-made configs live in `configs/*.toml` (proxy / local model variants).
- Default storage needs no setup: SQLite metadata at `pilot/meta_data/dbgpt.db`,
  Chroma vectors at `pilot/data`. MySQL variant: `configs/dbgpt-proxy-siliconflow-mysql.toml`.
- Prebuilt web assets are already in `packages/dbgpt-app/src/dbgpt_app/static/web`
  (and `static/old_web`) — no frontend build required.

## Stop

```bash
uv run dbgpt stop webserver
```
