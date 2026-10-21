# local_agent

A local macOS desktop agent. You give it a goal; it loops until done:

1. **Observe** – reads every interactive element of the frontmost window through the macOS Accessibility API
   (the same approach as [MacOS-MCP](https://github.com/CursorTouch/MacOS-MCP)).
2. **Plan** – Qwen 2.5 Coder (via Ollama) sees only the goal, a compact scope (app, window, element counts by role,
   focused element) and recent step history, and returns one next step as JSON: `click`, `double_click`,
   `right_click`, `type`, `shortcut`, `scroll`, or `terminal` (which must include a `command`).
3. **Select** – a System-1 decision model picks the element matching Qwen's target. The default is
   [JevK5](https://github.com/allebee/jevk5) 4B (Q8_0 GGUF on llama.cpp, ~5 GB). Candidates are first narrowed by
   role, then by a lexical pre-filter to the 32 best text matches (all of them if nothing's text matches), so JevK5 —
   which reads 16 options per pass — usually needs 1–3 passes. Choice questions are capped at 255 options, so any
   larger set runs as a tournament. [OpenJev](https://github.com/razorback16/openjev) (DiffusionGemma 26B, ~16–24 GB)
   is still supported with `LOCAL_AGENT_JEV_BACKEND=openjev`.
4. **Act** – AXPress / Quartz mouse & keyboard events, or a shell command (asks for confirmation unless `--yes`).
5. **Verify** – re-observe and ask Jev whether the UI changed as intended. Steps whose pick falls under the
   probability threshold are skipped. The outcome is logged back to Qwen for the next step.

## Setup (Apple silicon; ~10 GB for both models)

```bash
uv sync
```

Planner (Ollama):

```bash
brew install ollama && ollama serve   # or run the Ollama app
ollama pull qwen2.5-coder:7b
```

Selector (JevK5 4B on llama.cpp; `uv sync` already installed jevk5's stdlib client without torch):

```bash
brew install llama.cpp
uvx --from huggingface_hub hf download alibiserikbay/JevK5-GGUF --include jevk5-4b-v0.3-Q8_0.gguf --local-dir ~/models/jevk5
llama-server -m ~/models/jevk5/jevk5-4b-v0.3-Q8_0.gguf -c 8192 -ngl 99 --parallel 1 --cache-ram 0 --port 8091
```

Keep `--parallel 1 --cache-ram 0`: by default llama-server runs 4 slots and keeps up to 8 GB of prompt cache, which
pushed it to ~9 GB here; JevK5 never reuses cached prompts. The calibration temperatures in `config.py` (1.22 / 0.93) belong to the v0.3 4B files; other JevK5 files list
their own on the model card.

Optional, much heavier: OpenJev on MLX (~16–24 GB):

```bash
git clone https://github.com/razorback16/openjev ~/openjev
cd ~/openjev && python3.12 -m venv .venv && .venv/bin/pip install -e '.[mlx]'
OPENJEV_BACKEND=mlx OPENJEV_MLX_CACHE_LIMIT_GB=2 .venv/bin/python -m openjev   # serves 127.0.0.1:8080
LOCAL_AGENT_JEV_BACKEND=openjev uv run local-agent doctor
```

Permissions: add your terminal app under **System Settings → Privacy & Security → Accessibility**.

Check everything:

```bash
uv run local-agent doctor
```

## Usage

```bash
uv run local-agent snapshot --app TextEdit -o snapshot.json   # dump the window's element JSON
uv run local-agent run "make a new document and type hello world" --app TextEdit --dry-run
uv run local-agent run "make a new document and type hello world" --app TextEdit
```

Without `--app`, the agent waits `--delay` seconds (default 3) so you can switch to the target window.
Useful flags: `--threshold 0.5`, `--max-steps 20`, `--model qwen2.5-coder:14b`, `--yes`, `--log run.json`.
All settings can also be set as `LOCAL_AGENT_<NAME>` env vars (see `local_agent/config.py`).
