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
uv run python -m evals.planner_eval qwen3.5:9b qwen2.5-coder:7b   # compare planner models (needs Ollama + models)
```

Runtime dependencies are external services: Ollama at `127.0.0.1:11434` (`qwen3.5:9b`) and, for the default
`jevk5` backend, `llama-server -m ~/models/jevk5/jevk5-4b-v0.3-Q8_0.gguf -c 8192 -ngl 99 --parallel 1 --cache-ram 0 --port 8091`. The optional
`openjev` backend is an OpenJev server at `127.0.0.1:8080`. Tests fake all of them and need none.

Memory budget matters (24 GB Mac): Qwen3.5 9B + JevK5 4B Q8_0 keeps memory pressure green (~40% free), but only with
`--parallel 1 --cache-ram 0` (llama-server's default slots + 8 GB prompt cache reached ~9 GB on their own). OpenJev's DiffusionGemma 26B (~16–24 GB) caused
memory pressure, which is why JevK5 is the default.

`jevk5` is installed from git with its torch/transformers deps removed via `[tool.uv] override-dependencies`;
only its stdlib llama.cpp client (`jevk5.gguf.JevK5GGUF`) is used. Don't import `jevk5.runtime` (needs torch/CUDA).

## Architecture

macOS-only agent loop in `local_agent/loop.py` (`Agent.run`): observe → plan → select → act → verify, repeated until
the planner returns `done`, `max_steps`, or `max_consecutive_failures`.

- **Token split is the core design.** The planner (`planner/qwen.py`) never gets the element JSON. It gets
  `Snapshot.scope()` (app, window, role counts, focused element, open menu items), recent `StepRecord.for_planner()`
  history, and — unless `planner_view="off"` — a **screen view** (`observe/screen_view.py`): plain text lines per
  control/visible text, grouped by area (focused, dialog, open menus, window, toolbar, lists, menu bar), long lists
  collapsed to 12 + a count, cut to `view_budget(num_ctx, planner_view_tokens)` (~3.5k tokens reserved for the rest
  of the prompt). `planner_view="ids"` numbers controls so the planner can answer `target.id`; `"names"` omits numbers
  and Jev picks every element. The full element list still goes only to Jev.
- **Element selection** in `Agent._step`: `pick_by_id` uses the planner's `target.id` when that element exists (and
  is a text field for `type`); otherwise Jev picks from `target.description`. An unknown id with no description fails
  the step with a note. Non-interactive text elements (`UIElement.interactive=False`, AXStaticText/AXHeading) appear in
  the view but are never Jev candidates.
- **Planner contract** is `planner/schema.py::PlannerOutput`, passed to Ollama as a JSON schema (`format=`). Ollama's
  MLX engine for Qwen3.5 answers 501 "structured output is unavailable", so `QwenPlanner` then puts the schema in a
  system message and pulls the JSON out of the reply (`extract_json`). Its validator enforces per-action required
  fields (e.g. `terminal` needs `command`); `needs_jev()` decides whether a step goes through element selection.
  Invalid output gets one repair round-trip. Thinking is off and `num_ctx` is 8192, or Qwen3.5 reserves KV for 262k.
- **Planner evals**: `evals/planner_cases.py` holds cases on real TextEdit snapshots (`evals/fixtures/`, recorded by
  `uv run python -m evals.capture_fixtures`, which drives TextEdit) plus synthetic ones (error sheet, find bar,
  300-file Finder). Checks take `(plan, snapshot)` and resolve `target.id`. `tests/test_planner_cases.py` pins each
  check against ideal answers, number picks and live mistakes. Change the planner prompt, view or model → rerun
  `uv run python -m evals.planner_eval <models> --views off,names,ids`.
- **`planner_mode="choice"`** (`planner/choice.py`, opt-in): `ChoicePlanner` asks the kind of step first (ordered
  checklist + worked examples, enum-constrained), then one detail question appended to the same conversation so
  Ollama reuses the cached prefix; click targets are chosen by name-first labels parsed from the screen view, not
  numbers. Same `PlannerOutput` out. It scored below the default JSON planner for 3B/7B/9B (see README).
- **Jev backends** share TypeSafe's `/v1/systemone` question/answer shapes. `make_selector()` picks `JevK5Client`
  (default; calls `JevK5GGUF.decide` per question against llama-server) or `JevClient` (OpenJev over HTTP). Both
  inherit `select`/`verify`, which only go through `system_one()`.
- **Candidate narrowing** in `JevClient.select`: role-group filter → `selector/prefilter.py` (lexical match of the
  planner's target against title/label/placeholder/value, top `prefilter_top_k`=32; returns everything when no
  visible text matches, e.g. icon-only buttons) → tournament via `chunk()` if still >255 (balanced so no chunk has a
  single option). A lone candidate is checked with a `noul` question. JevK5 itself reads 16 options per pass and
  combines more with its knockout, so pass count ≈ ceil(n/16)+1 — that's why the pre-filter exists.
- **Verification** (`JevClient.verify`) asks a `noul` over the before/after element diff and deliberately omits the
  overall goal: with the goal in the state, JevK5 answers "is the goal done?" and scores working steps ~0.4. Re-tune
  against live cases (menu opened, cmd+a selection, typing, no-change) if you change the wording.
- **Observation quirks** (`observe/ax_tree.py`): the frontmost app comes from the AX system-wide element because
  `NSWorkspace.frontmostApplication()` is stale without a run loop; `activate_app` uses
  `NSRunningApplication.activateWithOptions_` since `open -a` can't take focus on recent macOS; Electron apps need
  `AXManualAccessibility` set before their tree appears; open menus are walked via the selected `AXMenuBarItem`
  (closed menu items report 0×0 frames and are dropped). `Snapshot.scope()` lists open menu item names so the
  planner can navigate submenus, and `selected` text is part of `describe()` so selections show up in diffs.
- **Thresholds**: `threshold` gates acting on Jev's *probability* for the pick (not `confidence`, which is
  entropy-normalized and stays low for 2–3-way finals). `verify_threshold` gates the post-action `noul` check.
- **Side effects are injected** through `loop.Hooks` (observe, execute, run_terminal, confirm, sleep), so the loop is
  tested with fakes in `tests/test_loop.py`. `observe/models.py` is pure Python; `observe/ax_tree.py` and `act/` import
  PyObjC.
- `UIElement.ref` holds the live `AXUIElementRef` used by `act/executor.py` (AXPress for native button-like roles,
  otherwise CGEvent clicks at frame center); it is never serialized. `UIElement.describe()` is the text Jev sees.
