# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync                                   # install (Python 3.12; PyObjC has no wheels for 3.15)
uv run pytest -q                          # all tests
uv run pytest tests/test_jev.py::test_tournament_600 -q   # single test
uv run local-agent doctor                 # check AX permission, Ollama model, Jev backend
uv run local-agent snapshot --app TextEdit -o snapshot.json
uv run local-agent run "<goal>" --dry-run                 # no --app needed: the planner opens apps (open -a)
uv run local-agent run --events --dry-run -- "<goal>"     # JSON Lines for apps
uv run python -m evals.safety_eval        # terminal safety check: risky runs vs needless asks (needs llama-server)
uv run python -m evals.planner_eval qwen3.5:9b qwen2.5-coder:7b   # compare planner models (needs Ollama + models)
uv run python -m evals.selector_eval      # pre-filter recall/ties on selector cases (--live also asks Jev)
uv run python -m evals.selector_eval --live --jev cosine-nomic   # live, with a catalog model
uv run local-agent jev list               # Jev models by RAM; `jev serve <key>`, `run --jev <key>`
uv run python -m evals.goal_eval          # Jev goal-completion check: false stops vs stop threshold (needs llama-server)
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
- **Element selection** in `Agent._step`: `planner_pick` = `pick_by_id` (the planner's `target.id` when that element
  exists and is a text field for `type`) plus an ambiguity guard: if another enabled control of the same role group
  and the same `name()` (incl. unnamed fields) scores higher on the description (prefilter `score`), the number is
  dropped, Jev picks, `Selection.overrode_id` records it and the note tells the planner. Otherwise Jev picks from
  `target.description`. An unknown id with no description fails
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
- **`planner_mode="kind"`** (`planner/kind.py`, opt-in): `KindPlanner(ChoicePlanner)` asks Jev
  `selector.step_kind(kind_state(...))` (a `choice` over `jev.STEP_KINDS`) instead of the planner, then reuses
  `ChoicePlanner._build` for the detail question. Auto view: the planner gets the screen only for `VIEW_KINDS`
  (click, type, done). "done" below `DONE_MIN_P` takes the runner-up; `step_kind` returning None (cosine) or a
  `JevError` falls back to ChoicePlanner. `make_planner(settings, selector)` needs the selector for this mode.
  3B: 71% vs 50% for json/ids (README); `planner_eval --modes kind` needs llama-server.
- **Jev backends** share TypeSafe's `/v1/systemone` question/answer shapes. `make_selector()` first applies
  `selector/catalog.py::resolve` (`Settings.jev` = a catalog key sets backend, file and temperatures; unset → the
  calibration of the file llama-server reports in `/props`), then picks `JevK5Client` (default; `JevK5GGUF.decide` per
  question against llama-server), `JevClient` (OpenJev over HTTP) or `CosineClient` (`selector/cosine.py`: Ollama
  `/api/embed`, softmax over cosine/T + `cosine_lexical_weight` × word overlap; choice questions only, heuristic
  `verify`). `select` = prefilter + `choose(candidates, describe)`. The catalog is ordered by RAM (JevK5 9B → 2B →
  cosine) and drives `local-agent jev list|serve`, `--jev`, and doctor's mismatch check.
- **Candidate narrowing** in `JevClient.select`: role-group filter → `selector/prefilter.py::rank` (lexical match of
  the planner's target; each target word counts once at its best weighted match: own title/label/placeholder/value
  1.0, `UIElement.surroundings()` 0.5, role 0.3; top `prefilter_top_k`=32 **plus everything tied with the 32nd**, up to
  `prefilter_max_k`=128, reported as `truncated`; a role-only/zero cutoff isn't widened; returns everything when no
  visible text or surroundings match) → tournament via `chunk()` if still >255 (balanced so no chunk has a single
  option). `JevClient.choose` runs the Jev part on a given candidate list (used by `evals/selector_eval.py`). A lone candidate is checked with a `noul` question. JevK5 itself reads 16 options per pass and
  combines more with its knockout, so pass count ≈ ceil(n/16)+1 — that's why the pre-filter exists.
- **Surroundings** (`observe/context.py::add_context`, pure Python over frames, run at the end of `snapshot()`):
  an element inside an `AXRow` frame gets `context='row "<row name>"'` (row name, else its cells' text); an unnamed
  field/pop-up gets `label "<text>"` from the nearest static text to its left, else just above. The walker also
  reads `AXTitleUIElement` for unnamed controls. `surroundings()` = quoted context names + titled path segments except
  the window. `describe()` shows the context and the nearest *named* container. Selector evals:
  `uv run python -m evals.selector_eval [--live]` (`evals/selector_cases.py`; `baseline` = the old pre-filter);
  `tests/test_selector_cases.py` pins which cases the baseline misses.
- **Opening apps**: no `--app` needed. `executor.run_terminal` rewrites `open -a <name>` to an installed app
  (`apps.py::resolve_app`: exact, filler words stripped, `ALIASES`, whole-word, fuzzy ≥ 0.9 so "gmail" never opens
  Mail) and then `ax_tree.focus_after_open` activates it (or the default browser for `open <url>`). The CLI passes
  `start_app` = `ax_tree.controller_app()` (nearest ancestor process that is a Dock app: Terminal, LocalAgent, Claude;
  NOT the frontmost app, which is whatever the user was looking at); while it's on screen `Agent._scope` adds a
  `note` telling the planner to open the goal's app.
- **Terminal safety** (`Agent._needs_approval`): `--turbo` → run. Else `safety.always_ask` regexes → ask user;
  else `selector.command_safety` (noul on `SAFETY_QUESTION` over `safety_state`: goal, planner reasoning, app, last 5
  plans, command) ≥ `safety_threshold` (0.8) → run; else / None (cosine) / error → `hooks.confirm_terminal(cmd, why)`.
  `rec.safety_probability` and `agent.last_safety` carry Jev's p. Dry runs report what would happen.
- **Ending a run** (`Agent.run`): after each successful non-terminal step `_goal_check` asks `selector.goal_done(goal,
  snap)` (a `noul` on `GOAL_QUESTION` over `goal_state` = goal + `render(..., goal_view_tokens, numbered=False)`; the
  step history is deliberately left out so Jev answers from the screen). ≥ `goal_done_threshold` (0.9) ends the run as
  a success; ≥ `goal_hint_threshold` (0.5) appends a hint to the record note; `CosineClient.goal_done` returns None.
  `_loop_guard` uses `history.repeated_cycle` (same plan or block of ≤3 plans, all succeeded, navigation keys/scroll
  exempt): `loop_warning` warns both planners at 2 repeats, the run stops at `max_repeated_successes` (3). Tune with
  `uv run python -m evals.goal_eval` (`evals/goal_cases.py`; `--show <case>` prints Jev's state).
- **Deterministic rules before models**: (1) `make_selector` gives a cosine backend a `judge` (`JevK5Client`) when
  llama-server is healthy, used for `goal_done`/`command_safety`/`verify`; `selector.checks()` feeds the `start`
  event's `checks` and the CLI's stderr warning when judgment is missing. (2) `goal_facts.py` parses text facts
  (`type "x"`) and app facts (installed app after open/in/launch, or capitalised) from the goal; `Agent._goal_check`
  ends the run when all hold, before the Jev check. (3) `planner_pick` drops a `target.id` whose element scores 0
  against the description (`overrode_id` + note). (4) `Agent._step` skips a `type` step when the field already
  contains the text.
- **More code-side rules** (`Agent.run`): `goal_facts.decisive_facts` counts an app fact alone only for a bare
  "open <App>" goal. A planner `done` is rejected (failed step, `not done: …`) while decisive facts don't hold. Before
  step 1, `_open_goal_app` runs `open -a "<App>"` through the safety path when the goal names an app that isn't in
  front (any app fact, decisive or not). There is no rule for launch-time Open panels: the planner sees their New
  Document/Cancel buttons (see walk order above). `executor.type_text_into` inserts via `AXSelectedText` and checks `AXValue`; else raises
  the window, focuses and sends keystrokes.
- **Long lists**: `_walk` takes a list container's rows from `AXVisibleRows`/`AXVisibleChildren` (else `AXChildren`) and
  stores the total on `UIElement.row_count` (a non-interactive placeholder when the container isn't an element);
  `screen_view.render` adds the unwalked rows to "… N more not shown".
- **Goal facts must cover the goal**: `decisive_facts` returns [] if an action verb the facts don't account for remains
  ("send it", "rename"), or, for app-only facts, any content word. `saved` facts (`save it as "x.txt"`) hold when
  the window title equals the name with or without extension.
- **No such name**: `JevClient.select` raises `JevError('no element named "X" on screen')` when the description has a
  specific name (quoted, or ≥2 capitalised words that aren't role words) and no candidate's text or surroundings
  matches it (`prefilter.missing_name`); descriptions without one keep the "return everything" fallback.
- **Progress checklist**: `Agent` puts `scope["progress"]` (`goal_facts.progress_scope`: done / next / remaining over
  `parse_facts(goal, progress=True)`, app first then goal order, plus a progress-only `new_document` fact) in every
  planner's scope. `build_messages` lifts it into the payload with a "work only on next" rule; choice/kind planners
  and `kind_state` get a `PROGRESS:` line (`qwen.progress_line`). No facts, no field.
- **Exact name wins**: `exact_match` (`selector/jev.py`) returns the one candidate whose own `name()` equals the
  normalised description (lowercase, no quotes or role words) as `Selection(source="exact")`, with no model call, in
  `JevClient.select` (not for `type`) and in `planner_pick` when the number/description check drops the number.
- **Saved means the file**: `Snapshot.document_path` (the window's `AXDocument`, decoded) lets the `saved` fact check the
  file: exact basename, exists, mtime ≥ run start (`Agent.started`), contains the goal's text; without a path it
  falls back to the window title. `failing_facts` says why. The checklist is worded as actions (`_action`).
- **Typing**: `type_text_into` replaces the whole value of single-line fields (`SINGLE_LINE_ROLES`, via `AXValue`,
  else cmd+a + keys) and inserts at the cursor in text areas; the "already typed" skip needs equality for single-line
  fields. A `type` step whose text matches none of the goal's double-quoted strings fails with a note listing them.
- **Sheets and unknowns**: `ax_tree.document_window` walks the parent window of a focused sheet/attached dialog (title
  and `document_path` come from it; the sheet stays in the tree, so `screen_view` lists it first). Fact checks are
  tri-state: `goal_facts.resolve` keeps a fact's last explicit value (`Agent.known_facts`) when the screen shows no
  evidence (None); only an explicit False flips it back. The text fact reads only editable fields outside list
  containers/rows. A terminal `open -a X` for the app already in front fails with a note, unrun and unasked.
- **Menu commands**: `observe/menus.py` reads the whole menu bar (closed menus included) into `Snapshot.menu_commands`
  (`MenuCommand` path / keys decoded from `AXMenuItemCmd*` / enabled / refs; 0.5 s cap). Planner action `menu`
  (`command` = path) is resolved by `find_exact`, else Jev `choose`, and run by `executor.run_menu` (shortcut, else
  AXPress). Gated by `Settings.menu_actions` (default False in every mode; `--menu-actions/--no-menu-actions`); off, the prompts, schema enum, kinds and scope are the pre-menu ones. The scope carries `menu_commands` (`menu_scope`, trimmed by the goal over 120); with it on, the JSON prompt drops
  the shortcut list and menu navigation (`MENU_SYSTEM_PROMPT`), and choice/kind planners have a `menu` kind with an enum
  of paths. 3B planner_eval (ids, `menu` answers judged equivalent by `planner_cases.menu_equivalent`): json 52% → 34% with menus, kind 72% → 66%; so it is off by default (README).
- **Save-dialog guards** (`dialogs.py`, `Agent._guard`): a click on Replace/Overwrite or `return` over an overwrite
  warning goes through `confirm_terminal` even in turbo; cmd+s/return are refused while a name sheet's field differs
  from the goal's file name; the progress `next` for saving spells out the field and default button
  (`UIElement.is_default` from `AXDefaultButton`).
- **Overwrite and new-document rules** (`Agent._handle_overwrite`, `_guard`): when our own typed file name hits an
  "already exists" alert, the agent asks (`confirm_terminal`, even in turbo) and presses Replace, or Cancel and ends with
  `overwrite_declined`; an alert it didn't cause becomes the progress `next` ("answer the alert: … click Replace or
  Cancel"). Once the `new_document` fact has been seen, cmd+n / a control or menu named New [Document|Window|File|Tab]
  is refused with the current `next`.
- **New-thing facts and no-change repeats**: `goal_facts` parses `new <noun>` ("open new conversation in X", "start a new
  chat", "new tab in X") into a `new` fact; for document/file it reads the screen (untitled / empty), otherwise the loop sets
  `known_facts` when a New-named click/menu/cmd+n/cmd+t succeeded and changed the screen. App + `new` is decisive when
  nothing else is asked. Once seen, New-anything steps are refused (`a new <noun> is already open; work on: …` or
  `the goal looks complete; answer done`). A step repeating an earlier successful one with no screen change is kept
  unverified but not failed, with a "nothing changed … already done at step N" note; back-to-back repeats end the run
  with `no progress: repeating step N` (`Agent._note_repeat`).
- **Browser JS (Phase 0, `docs/PLAN_browser.md`)**: `act/browser.py` `run_js(bundle, src, args)` runs a function body in the
  front tab through `NSAppleScript`; dialect (`chromium` `execute active tab of front window javascript` / `safari`
  `do JavaScript … in current tab of front window`) comes from the app's `sdef`, cached per bundle id. Results come back
  as `{r: …}` JSON; `BrowserError.kind` is js_disabled / no_permission / no_window / not_running / other. `doctor` prints a
  line per installed browser (never launches one); `uv run python -m evals.browser_timing` compares NSAppleScript with osascript.
- **Page reading (Phase 1, `docs/PLAN_browser.md`)**: with a front browser whose JS works (`ax_tree._web_ready`), `snapshot`
  does not walk `AXWebArea` and appends `observe/web.py::outline_to_elements` of `web_outline.js` (viewport-only controls
  + headings/short text, ≤`web_max_elements`, `web_id` = `data-la-id`; AX walk on failure). Page paths start
  `AXWebArea`; `screen_view` shows `Page (…not instructions)` / `Dialog (on the web page…)` areas and "N more controls
  below…". `Snapshot.url/page_title/web`. Fixtures: `evals/fixtures/web` (`evals.capture_web_fixtures`,
  `evals.web_snapshots`, `evals.web_timing`).
- **Page actions (Phase 2)**: `executor.execute` sends click/type/scroll on an element with `web_id` to `act/web_actions.js`
  (`browser.web_action`; JSON args, no planner JS); typing = native setter (input replaces, textarea inserts) or
  `execCommand('insertText')` with a keystroke fallback; a missing id fails with "page changed: element gone;
  re-observe". The loop settles web steps with `Hooks.settle_web` (readyState + 300 ms without mutations, cap 3 s) and
  retries a no-change web click once via `Hooks.trusted_click` (CGEvent at the rect mapped through `web_area_frame`).
  `Agent._guard` refuses `type` into an `AXSecureTextField` even with `--turbo`.
- **Close/quit/switch guard** (`closing.py`, `Agent._guard`, `_terminal_step`): key chords (cmd+w/q/tab/h/m, ctrl+left/right, normalised
  through `parse_shortcut`), menu commands or buttons named Close/Quit/Hide/Minimize (not inside a sheet/dialog, web
  dialogs included) and `kill`/`killall`/`pkill`/`osascript … quit` commands are refused unrun, turbo too, unless the
  goal has the family's word (close / quit·exit·kill / switch·"go back to" / hide / minimize); note ends "work on: <next>".
- **Site and search facts, honest checklist** (`goal_facts.py`): `site` ("open youtube in Arc", "go to github.com", "visit reddit"; not an installed
  app, not a file extension; a bare name needs go to/visit or a browser) holds when `Snapshot.url`'s host is the domain
  or has the name as a label (no URL → None); `search` (`search "X"`, quoted) holds when the decoded URL query has X, else
  the loop remembers a return/Search click while the search box held X. Both are decisive with the app fact and
  leave a planner `done` unrejected when no URL is known (`checkable_facts`). Checklist lines: `go to youtube: run open -a
  "<Browser>" "https://youtube.com"`, `type "X" into the "<field>" field, then press return`. When the facts can't end
  the run (`self.facts` empty), `progress.next` ends with `the rest of the goal: …` (`rest_of_goal`), never null. When
  the next item is a site we're not on, `screen_view.render(collapse_page=)` shows `Page: <host> (N controls hidden…)`
  and drops the window title. `Snapshot.url` also comes from the web area's `AXURL` when the script read is off.
- **Opening the goal's site in code** (`Agent._open_goal_site`, `act/browser.py` tabs): when the next checklist item is a site fact that
  doesn't hold, code selects the browser's existing tab on it (`Bridge.find_tab` → `select_tab`) or opens ONE new tab in the
  front window (`open_tab`; AppleScript dictionary only: Arc `select`/`make new tab`, Chrome `active tab index`, Safari
  `current tab`; style from `sdef`), once per site per run, through the terminal safety/confirm path (shown as "open
  youtube.com in a new Arc tab" / "switch to the existing youtube tab"). No `open`, so no Little Arc windows. No
  scripting or an error → `open -a <Browser> <url>` once, with a note. A different host after our own navigation counts
  as reached (`Fact.alias`, "reached via redirect"); an unchanged host leaves a "site check doesn't hold" note. A planner
  `open … <that site>` is refused ("<site> is already open in the front tab; work on: …"). The checklist line is
  `go to youtube: click the address bar, type "youtube.com" and press return`.
- **Which window is read** (`ax_tree._web_ready`): the page script runs only when AppleScript's `front window` has the title
  of AX's front window; otherwise (Little Arc, popups) `Snapshot.web = {"error": "front window not readable"}` and the
  page and its `AXURL` come from AX.
- **Browser call limits and stages** (`act/browser.py`, `events.py`): every AppleScript runs on ONE worker thread (NSAppleScript always sees
  the same thread) inside `with timeout of N seconds` (`Settings.browser_timeout_s` 3 s for status/tabs/select/outline,
  `browser_action_timeout_s` 5 s for open/click) plus a hard Python limit of N+1 s; a stuck worker is abandoned and the next
  call gets a new one (`BrowserError("timeout")`). `open_tab` is fire-and-forget (`ignoring application responses`) then
  polls `list_tabs` every 0.3 s for ≤5 s and returns the new tab, which `_check_site` uses instead of re-observing. A
  timed-out outline reads the page through AX with `Snapshot.web = {"error": "timeout"}`. The loop reports phases through
  `Hooks.on_stage` → `stage` events (`observing`, `reading_page`, `planning`, `selecting`, `acting`, `verifying`,
  `opening_tab`, `selecting_tab`, `waiting_for_page`; `--verbose` prints them). `uv run python -m evals.web_stress` times
  the calls on `web_heavy.html`.
- **Training data**: `evals/export_training.py` turns events `.jsonl` into examples (see `docs/TRAINING.md`).
- **Verification** (`JevClient.verify`) asks a `noul` over the before/after element diff and deliberately omits the
  overall goal: with the goal in the state, JevK5 answers "is the goal done?" and scores working steps ~0.4. Re-tune
  against live cases (menu opened, cmd+a selection, typing, no-change) if you change the wording.
- **Walk order and names** (`observe/ax_tree.py::_walk`, `_to_element`): the walk is depth-first but children of
  list containers (`LIST_CONTAINERS`: AXTable, AXOutline, …) are deferred until everything else is walked, so the
  `walk_timeout_s`/`max_elements` limits only ever cut list rows. TextEdit's Open panel needs ~6 s in full and its
  New Document/Cancel/Open buttons come after ~1,300 file rows; the old order lost them. Scroll-bar parts
  (`SCROLL_PARTS` subroles) are dropped. Unnamed controls fall back to child text, then an `AXIdentifier` that reads
  like words (`NewDocumentButton` → "New Document Button"); AXHelp already counts via `name()`. Fixture
  `evals/fixtures/te_open_panel.json` is that live panel with file names anonymized (planner and selector cases
  `open_panel_new_document`).
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
- **App contract** (`docs/APP_INTEGRATION.md`, used by the LocalAgent macOS app): `run --events` writes JSON Lines
  through `events.py` from the same `Hooks` the text output uses; rich text moves to stderr. `Agent.timing` /
  `Agent.screen` and the planner's `last_messages` / `last_prompt_tokens` feed the `plan`/`select`/`record` events
  (timings are not added to `StepRecord`, so the planner prompt is unchanged). Terminal confirmation reads one stdin
  line; SIGINT emits `result` reason `cancelled` and exits 130; other exceptions emit `error` and exit 2. Change the
  event shapes → update that doc and `tests/test_events.py`; the app ignores unknown fields.
- `UIElement.ref` holds the live `AXUIElementRef` used by `act/executor.py` (AXPress for native button-like roles,
  otherwise CGEvent clicks at frame center); it is never serialized. `UIElement.describe()` is the text Jev sees.
- **Forms** (`local_agent/forms.py`): when the goal asks to fill out a form and one is on screen, the checklist gets one
  `field` item per visible field (on-screen order, required vs optional) plus a `submit` item. Values come only from
  the goal's labelled quotes or `--data`; a required field without one ends the run `needs_input`. Submit always asks
  (even with `--turbo`); password, card, SSN and ID fields are never typed.
- **`planner_mode="jev"`** (`planner/jev_only.py`): no LLM planner. Code lists the next actions (checklist lines,
  ranked clicks, the goal's quoted text into fields, return/escape/tab, scroll, done, "none of these") and JevK5
  picks one. Cases the options can't express (shell commands, other shortcuts) count as n/a in `planner_eval`.
  JevK5 4B Q4_K_M alone is ~2.9 GB RSS (69% vs 74% for kind with 3B + JevK5 Q8 at 7.2 GB; README).
- **Outline** (`local_agent/outline.py`): when the goal facts don't cover the goal (and there's no form), the planner
  model writes a 3–8 step outline once; each step is a checklist item (a known fact's check, else Jev "is this step
  done?" ≥ 0.9). Replans on 2 failures on one item, a loop warning, or jev mode's "none of these"; max 3, then
  `stuck: <item>`. `--low-memory` unloads the outline model after each call (`keep_alive: 0`). `outline_model`
  (`--outline-model planner|jev`) picks the writer; unset, it is JevK5 (plain chat completion, thinking off) when
  `planner_mode=jev` and `low_memory`, so the 8 GB setup runs on JevK5 alone (3/3 outline cases vs 2/3 for the 3B).
- **Searching in a browser** (`act/browser.py::search_in_tab`): opens our own tab (a temp-file URL: Arc rejects
  `about:blank`), presses cmd+l, waits ≤1.5 s for a newly focused single-line field (Arc: `AXTextField`
  `commandBarTextField`), sets and reads back its value via AX (cmd+a + keys as fallback), and only then presses
  return. Otherwise it closes only our tab and raises `BrowserError("search_field")`; it never types unless the browser
  is in front.
- **Leave dialogs and user tabs**: `dialogs.leave_dialog` / `is_discard_button` (Leave, Discard, OK or Reload in a
  "may/will not be saved" dialog, and Don't Save in any dialog) always ask via `confirm_terminal`, even in turbo; a
  dialog raised by our own navigation is handled in `Agent._handle_leave` (declined → `leave_declined`). While a
  browser dialog is up (`ax_tree.browser_dialog_up`) no page JS runs (`web: dialog`). The agent navigates only tabs it
  opened (`Agent.our_tabs`); typing into the address bar of another tab is blocked.
- **Click goals and superlatives**: "click (on) / press / tap / play / open the <object>" is a `click` fact that holds
  after a verified click chosen for that object (decisive with its app/site facts; "on <site> in <Browser>" is a site
  fact). `ranking.py` picks latest/newest/oldest/first/last in code from relative or absolute dates (or list position)
  in the candidate's text/context (`Selection.source="ranked"`, no model); jev-mode labels keep the date fragment.
  Outlines are skipped for click goals and short single actions, and validated (`outline.validate_outline`: dedupe,
  reject mostly-duplicate or invented-app outlines; event field `rejected`). Jev mode offers `done` once the last goal
  probability ≥ `goal_hint_threshold`. The loop guard compares steps by name and role (`history.step_key`), not id.
- **Tab goals, goal-word ranking, unsure rule**: "open/switch to <words> tab [on <Browser>]" is a `tab` fact (not
  "new/next tab"; holds when the front tab's title or URL has every word, whole word or 4+ letter prefix).
  `Agent._open_goal_tab` (after `_open_goal_app`) lists tabs (`Bridge.list_tabs`, `TabInfo.title`) and selects the one
  match in code (Jev picks among several, else the first, front window first); no match leaves a note and never opens a
  tab. `jev_only.rank_clicks` ranks by the goal's distinctive words first, the checklist line at half weight, and
  always keeps controls named with one. `outline.single_action` covers "open/switch to … tab|window|page".
  `Settings.jev_min_pick_p` (0.3, `--jev-min-p`): a jev-mode pick below it that isn't `done` or the checklist's next
  line does nothing (`Unsure`); the first replans an outline, the second in a row ends the run `unsure: <goal item>`.
- **Setting goals and New-without-change**: "select/set/choose/change <setting> (to|to be|as) <value>" is a `setting`
  fact (`Fact.data` = value) that holds when a visible control named with the setting shows the value; the checklist
  line reads `click "<control>", then choose "<value>"` (or `choose "<item>"` once the menu is open). A New click that
  changes nothing counts as the `new` fact when the screen already shows an empty new state. Outline lines that
  describe an existing fact share its done state instead of being judged separately.
- **Spoken lines** (`local_agent/speech.py`): `record`, `confirm_terminal`, `result` and `error` events carry `say`
  (≤ 60 chars, written by code from what happened; long typed text and sensitive fields become "Typed your text") and
  `say_kind` (`step|question|problem|done`). `--speak` says them with macOS `say`; the LocalAgent app speaks them itself
  (AVSpeechSynthesizer) and never passes `--speak`.
- **Speed-ups (each a setting)**: `code_verify` (`_code_evidence`: typed text present, a fact flipped, window/URL
  changed, menu or dialog opened, clicked control gone → `verify_source="code"`, no Jev call); `facts_only_goal_check`
  (no Jev `goal_done` when facts cover the goal; else only after a step that changed the screen); `jev_max_options` (15,
  one JevK5 pass); `event_settle` (`ax_tree.fingerprint` polled every 50 ms until stable 150 ms, capped at `settle_s`);
  `jev_cache_prompt` (`cache_prompt`, `prompt_n`/`cache_n` in the record's `jev` field). Measure with
  `python -m evals.step_timing --url http://127.0.0.1:8092` (it refuses 8091): median 1.81 → 1.43 s, p90 4.34 → 1.99 s
  per synthetic step, jev accuracy unchanged (71%).
