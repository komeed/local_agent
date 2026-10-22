# local_agent

A local macOS desktop agent. You give it a goal; it loops until done:

1. **Observe** – reads every interactive element of the frontmost window through the macOS Accessibility API
   (the same approach as [MacOS-MCP](https://github.com/CursorTouch/MacOS-MCP)).
2. **Plan** – Qwen3.5 9B (via Ollama, thinking off) sees only the goal, a compact scope (app, window, element counts by role,
   focused element), recent step history, and a **screen view**: one text line per visible control and text, grouped
   by area (dialog, open menu, window, toolbar, lists, menu bar), long lists collapsed, cut to fit its context window
   (~1–2k tokens). It returns one next step as JSON: `click`, `double_click`, `right_click`, `type`, `shortcut`,
   `scroll`, or `terminal` (which must include a `command`). Controls in the view are numbered, so Qwen can name the
   element directly (`target.id`).
3. **Select** – when Qwen gave a number, that element is used. Otherwise a System-1 decision model picks the element
   matching Qwen's description. The default is
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
ollama pull qwen3.5:9b   # needs Ollama >= 0.22
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

## Choosing the planner model

`evals/planner_eval.py` scores planner models on 28 cases: 17 taken from live TextEdit runs (shortcuts, menu/submenu
navigation, recovering from failed steps, terminal for file tasks, knowing when it's done) and 11 that need what's on
screen (toolbar controls, save sheet, error dialog, find bar, a 300-file Finder window, reading on-screen text). Each
model runs with the screen view off, as names only (Jev picks every element), and numbered (`ids`, the default):

```bash
uv run python -m evals.planner_eval qwen2.5-coder:3b qwen2.5-coder:7b qwen3.5:9b --views off,names,ids
```

Results on an M-series Mac with 24 GB (JevK5 loaded, 8k planner context). "Screen" is the 11 on-screen cases:

| Model | View | Accuracy | Screen | Other | Median / step | Prompt tokens | Memory | Pressure |
|---|---|---|---|---|---|---|---|---|
| **qwen3.5:9b** | **ids** (default) | **93%** | 91% | 94% | 4.8 s | ~1.8k | 7.8 GB | green, 44% free |
| qwen3.5:9b | names | 89% | 91% | 88% | 4.1 s | ~1.6k | | |
| qwen3.5:9b | off | 64% | 36% | 82% | 3.4 s | ~1.4k | | |
| qwen2.5-coder:7b | names | 61% | 55% | 65% | 3.7 s | ~1.0k | 5.1 GB | green, 60% free |
| qwen2.5-coder:7b | ids | 50% | 36% | 59% | 3.8 s | ~1.1k | | |
| qwen2.5-coder:7b | off | 50% | 9% | 76% | 2.9 s | ~0.7k | | |
| qwen2.5-coder:3b | off | 32% | 18% | 41% | 1.7 s | ~0.7k | 2.4 GB | green, 69% free |
| qwen2.5-coder:3b | names | 29% | 45% | 18% | 2.0 s | ~0.9k | | |
| qwen2.5-coder:3b | ids | 25% | 36% | 18% | 2.3 s | ~1.1k | | |

The view lifts the 9B from 64% to 93% for about 1.4 s more per step. Smaller models gain on on-screen cases but lose
on the rest: they start clicking listed controls instead of using shortcuts, terminal or `done`. For a 7B use
`--view names`; the 3B doesn't plan reliably either way (it almost never chooses `terminal` or `done`).
An earlier 17-case run without the view also tried qwen3.5:4b (71%, below qwen2.5-coder:7b's 82%).

`--planner choice` (`planner_mode="choice"`) splits each step into short multiple-choice questions in one
conversation: first the kind of step from an ordered checklist (done, terminal, click, shortcut, type, open,
scroll), then its details (keys, the element picked by its name from the view, the text or command). It didn't beat
the default, so it stays opt-in:

| Model | `--planner choice` | best `json` (above) |
|---|---|---|
| qwen3.5:9b | 82% (4.1 s) | 93% |
| qwen2.5-coder:7b | 54% (2.4 s) | 61% |
| qwen2.5-coder:3b | 32% (0.9 s) | 32% |

The 3B's weak point is deciding what kind of step comes next (it answers "click" for terminal and shortcut tasks
even with worked examples), not writing the JSON.

## Usage

```bash
uv run local-agent snapshot --app TextEdit -o snapshot.json   # dump the window's element JSON
uv run local-agent run "make a new document and type hello world" --app TextEdit --dry-run
uv run local-agent run "make a new document and type hello world" --app TextEdit
```

Without `--app`, the agent waits `--delay` seconds (default 3) so you can switch to the target window.
Useful flags: `--threshold 0.5`, `--max-steps 20`, `--model qwen2.5-coder:7b`, `--view ids|names|off`,
`--planner json|choice`, `--yes`, `--log run.json`.
All settings can also be set as `LOCAL_AGENT_<NAME>` env vars (see `local_agent/config.py`).
