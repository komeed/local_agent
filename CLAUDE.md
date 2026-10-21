# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync                                   # install (Python 3.12; PyObjC has no wheels for 3.15)
uv run pytest -q                          # all tests
uv run pytest tests/test_jev.py::test_tournament_600 -q   # single test
uv run local-agent doctor                 # check AX permission, Ollama model, Jev backend
uv run local-agent snapshot --app TextEdit -o snapshot.json
uv run local-agent run "<goal>" --app TextEdit --dry-run
```

Runtime dependencies are external services: Ollama at `127.0.0.1:11434` (`qwen2.5-coder:7b`) and, for the default
`jevk5` backend, `llama-server -m ~/models/jevk5/jevk5-4b-v0.3-Q8_0.gguf -c 8192 -ngl 99 --parallel 1 --cache-ram 0 --port 8091`. The optional
`openjev` backend is an OpenJev server at `127.0.0.1:8080`. Tests fake all of them and need none.

Memory budget matters (24 GB Mac): Qwen 7B + JevK5 4B Q8_0 is ~10 GB and keeps memory pressure green, but only with
`--parallel 1 --cache-ram 0` (llama-server's default slots + 8 GB prompt cache reached ~9 GB on their own). OpenJev's DiffusionGemma 26B (~16–24 GB) caused
memory pressure, which is why JevK5 is the default.

`jevk5` is installed from git with its torch/transformers deps removed via `[tool.uv] override-dependencies`;
only its stdlib llama.cpp client (`jevk5.gguf.JevK5GGUF`) is used. Don't import `jevk5.runtime` (needs torch/CUDA).

## Architecture

macOS-only agent loop in `local_agent/loop.py` (`Agent.run`): observe → plan → select → act → verify, repeated until
the planner returns `done`, `max_steps`, or `max_consecutive_failures`.

- **Token split is the core design.** The planner (`planner/qwen.py`) gets only `Snapshot.scope()` (app, window,
  role counts, focused element) plus recent `StepRecord.for_planner()` history — never the element list. The full
  element list goes only to Jev (`selector/jev.py`). Keep it that way when changing prompts.
- **Planner contract** is `planner/schema.py::PlannerOutput`, passed to Ollama as a JSON schema (`format=`). Its
  validator enforces per-action required fields (e.g. `terminal` needs `command`); `needs_jev()` decides whether a
  step goes through element selection. Invalid output gets one repair round-trip.
- **Jev backends** share TypeSafe's `/v1/systemone` question/answer shapes. `make_selector()` picks `JevK5Client`
  (default; calls `JevK5GGUF.decide` per question against llama-server) or `JevClient` (OpenJev over HTTP). Both
  inherit `select`/`verify`, which only go through `system_one()`.
- **Candidate narrowing** in `JevClient.select`: role-group filter → `selector/prefilter.py` (lexical match of the
  planner's target against title/label/placeholder/value, top `prefilter_top_k`=32; returns everything when no
  visible text matches, e.g. icon-only buttons) → tournament via `chunk()` if still >255 (balanced so no chunk has a
  single option). A lone candidate is checked with a `noul` question. JevK5 itself reads 16 options per pass and
  combines more with its knockout, so pass count ≈ ceil(n/16)+1 — that's why the pre-filter exists.
- **Thresholds**: `threshold` gates acting on Jev's *probability* for the pick (not `confidence`, which is
  entropy-normalized and stays low for 2–3-way finals). `verify_threshold` gates the post-action `noul` check.
- **Side effects are injected** through `loop.Hooks` (observe, execute, run_terminal, confirm, sleep), so the loop is
  tested with fakes in `tests/test_loop.py`. `observe/models.py` is pure Python; `observe/ax_tree.py` and `act/` import
  PyObjC.
- `UIElement.ref` holds the live `AXUIElementRef` used by `act/executor.py` (AXPress for native button-like roles,
  otherwise CGEvent clicks at frame center); it is never serialized. `UIElement.describe()` is the text Jev sees.
