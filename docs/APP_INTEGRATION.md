# Driving `local-agent` from an app

This is the contract a front end (e.g. the LocalAgent macOS app) relies on. The app runs the CLI as a child process,
reads JSON from stdout, shows stderr as a log, answers terminal-command confirmations on stdin, and stops a run with
SIGINT. Everything about planning, element picking, acting and verifying stays in the CLI.

## 1. Commands

Executable: `<repo>/.venv/bin/local-agent` (created by `uv sync`). Run it with the repo as the working directory.

| Command | Purpose |
|---|---|
| `local-agent doctor --json` | Health check. Prints one JSON object (section 4). |
| `local-agent run --events [options] -- <goal>` | Run the agent and stream JSON Lines events (section 3). |

`run` options (pass all of them explicitly; `--` before the goal so a goal starting with `-` isn't read as a flag):

| Flag | Values | Default |
|---|---|---|
| `--app NAME` | Optional, no longer needed: the agent opens the app the goal needs itself (`open -a` terminal step). Only to force a starting app | none |
| `--jev KEY` | Jev model from `jev list --json` | what llama-server serves |
| `--turbo` (alias `--yes`) | Run every terminal command with no safety check and no `confirm_terminal` | off |
| `--model NAME` | Ollama planner model | `qwen3.5:9b` |
| `--view ids\|names\|off` | Planner screen view: numbered controls, names only, or none | `ids` |
| `--planner json\|choice\|kind` | Planner mode (choice and kind are experimental; kind: Jev picks the kind of step, best for 3B planners) | `json` |
| `--max-steps N` | Step limit | 20 |
| `--threshold P` | Min Jev probability to act on a picked element (`.` decimal separator) | 0.5 |
| `--dry-run` | Plan and pick one step, don't act | off |
| `--events` | JSON Lines on stdout; human text on stderr | off |

`--delay` (default 0) waits before the first observation; don't pass it from the app. Without `--app` the agent starts
on whatever is frontmost; while that is the app that launched it (LocalAgent, a terminal; found from the process
tree), the planner is told to open the goal's app first. So the app needs no app picker: send the goal only.

## 2. Exit codes

| Code | Meaning | Last stdout event |
|---|---|---|
| 0 | Goal done (`result.success == true`) | `result` |
| 1 | Run ended without success (max steps, consecutive failures, dry run that couldn't act) | `result` |
| 2 | The agent crashed, or the CLI rejected its arguments | `error` if it crashed; nothing if arguments were rejected (usage text on stderr) |
| 130 | Cancelled with SIGINT | `result` with `reason: "cancelled"` |

Prefer the `result`/`error` event over the exit code. A process that exits with no `result`/`error` crashed before the
event loop started (see stderr).

`doctor --json` exits 0 when everything is ready, 1 otherwise; stdout has the JSON either way.

## 3. Events (`run --events`)

One JSON object per line on stdout, flushed as it happens. Every event has `type` and `t` (seconds since the CLI
started). Ignore unknown types and fields; new ones may be added without bumping `version`.

Order: `start`, then per step `plan` → (`select`) → (`confirm_terminal`) → `record`, then a final `plan` with
`plan.done == true` if the planner finished, then `result` (or `error`).
A step whose planner call failed has a `record` (`plan: "planner error"`) and no `plan` event.
`plan.plan` is an **object**; `record.plan` is a **string** (the step summary).

### `start`
```json
{"type": "start", "t": 0.0, "version": 1, "goal": "Make a new TextEdit document", "app": "TextEdit", "model": "qwen3.5:9b", "view": "ids", "planner": "json", "jev": "jevk5-4b-q8", "turbo": false, "dry_run": false, "max_steps": 20, "threshold": 0.5, "checks": {"goal": true, "safety": true, "verify": "jev"}}
```
`app` is null without `--app`. `version` is 1 for this document. `checks` says which judgments run: `goal` (Jev goal
check), `safety` (Jev rates terminal commands; false: every non-turbo command asks), `verify` (`"jev"` or `"heuristic"`).
They are false/heuristic only with `--jev cosine-*` while llama-server is down (the CLI also warns on stderr). `web` is `false` when page reading is off, else an object of running browser name → `"ok"` / `"js_disabled"` / `"no_permission"` / `"no_window"` (only `ok` browsers are read). The `plan` event has `web`: `{ms, candidates, listed, more}` for a step that read a web page, else null.

### `stage`
```json
{"type": "stage", "t": 5.9, "step": 1, "stage": "reading_page", "elapsed_s": 0.021}
```
Emitted when a phase of step `step` starts; `elapsed_s` is how long the previous phase took (null for the first). `stage` is
one of `observing`, `reading_page` (the browser page is read through its JavaScript), `planning`, `selecting`, `acting`,
`verifying`, and for the agent's own browser navigation `listing_tabs`, `opening_tab`, `selecting_tab`, `searching`, `focusing_address_bar`, `typing_query`, `opening_result`, `waiting_for_page`. A phase that never
ends shows as the last `stage` event of a stuck run. Apps that don't know the type ignore it. `--verbose` prints them as text.

### `outline` and `replan`
```json
{"type": "outline", "t": 3.1, "items": ["Open Keynote", "Select the third slide", "Export as PDF"], "seconds": 1.2, "load_s": 0.5}
{"type": "replan", "t": 40.2, "reason": "two failed steps on one item", "items": ["Choose File > Export To > PDF"]}
```
`outline` follows `start` when the goal's facts could not cover it (and no form is on screen): the planner model's numbered
steps, how long the call took and how much of that was loading the model. `replan` carries the steps still to do after the run got
stuck (`reason`: two failed steps on one item, loop warning, or jev found no listed action); at most 3 per run, then the `result`
reason is `stuck: <item>`. Apps that don't know these types ignore them; to show them, list `items` as the plan.

### `plan`
```json
{"type": "plan", "t": 5.31, "step": 1, "plan": {"done": false, "reasoning": "Make a new document first.", "action": "shortcut", "target": null, "text": null, "keys": "cmd+n", "direction": null, "command": null, "summary": "shortcut keys=cmd+n"}, "screen": "App: TextEdit — window \"Untitled\"\n…", "messages": [{"role": "system", "content": "…"}, {"role": "user", "content": "…"}, {"role": "assistant", "content": "{\"done\": false, …}"}], "prompt_tokens": 1767, "planner_s": 4.71}
```
- `plan.action`: `click`, `double_click`, `right_click`, `type`, `shortcut`, `scroll`, `terminal`, `menu` (`plan.command` is the menu path, e.g. `File > Save As…`), or null when `done`.
- `plan.target`: null or `{"id": int|null, "role": str, "description": str}` (`id` is the screen-view number).
- `screen`: the screen view the planner got; null when `view` is `off`.
- `messages`: the chat as sent to Ollama (including any schema note and repair turns); the last message is the
  model's raw reply. May be null.
- `prompt_tokens`, `planner_s`: may be null.

The done plan:
```json
{"type": "plan", "t": 21.0, "step": 3, "plan": {"done": true, "reasoning": "There are 42 files in Downloads.", "action": null, "target": null, "text": null, "keys": null, "direction": null, "command": null, "summary": "done"}, "screen": "…", "messages": […], "prompt_tokens": 1890, "planner_s": 3.2}
```
For question-style goals, `reasoning` of the done plan is the answer.

### `select`
Sent when the step targets an element.
```json
{"type": "select", "t": 7.2, "step": 2, "source": "jev", "element": {"id": 5, "role": "AXMenuBarItem", "name": "File", "description": "AXMenuBarItem title=\"File\" in AXMenuBar at (80,0)", "frame": {"x": 80, "y": 0, "w": 36, "h": 24}}, "probability": 0.99, "confidence": 0.93, "candidates": 14, "rounds": 1, "margin": 0.98, "overrode_id": null, "select_s": 1.8}
```
`source` is `"planner"` when the planner picked the element by number (then `probability` is 1.0 and no Jev call
was made), `"exact"` (the one element whose own name equals the description; probability 1.0, no model call) or
`"jev"`. `margin` is Jev's final probability minus the runner-up's. `overrode_id` is the planner's
number when the ambiguity guard overruled it (another control with the same name matched the planner's
description better) and Jev picked instead; otherwise null. `element.frame` and `select_s` may be null.

### `confirm_terminal`
```json
{"type": "confirm_terminal", "t": 12.4, "step": 3, "command": "rm ~/Downloads/*.dmg", "reason": "Jev rated it safe with p=0.40 only (needs 0.80)", "safety_probability": 0.4}
```
Terminal commands normally run without asking. This is sent only for a command the safety check didn't clear:
- it matches a dangerous pattern (`local_agent/safety.py::ALWAYS_ASK`: sudo, recursive rm, disk erase, `curl | sh`,
  killall, keychain, ssh keys, force-push, …); `reason` names it, e.g. `"deletes folders recursively"`, and
  `safety_probability` is null;
- Jev rated it below `safety_threshold` (0.8) given the goal, the planner's reason and the screen;
- the Jev backend can't judge (cosine models) or the check errored.

When a save hits an existing file (the goal's file name was typed by the agent), the agent asks first with `command`
`replace existing "<name>"` and `reason` `would overwrite an existing file`, then presses Replace (yes) or Cancel (no).
On no the run ends with `result` `success: false`, `reason: "overwrite_declined"` (the LocalAgent app shows any unknown
reason as `Stopped: <reason>`).

The same event also asks before a click on a Replace/Overwrite button (or `return`) while an overwrite warning is on
screen, even with `--turbo`: `command` is then the step summary and `reason` is `would overwrite an existing file`.

The CLI then blocks reading **one line from stdin**: `y` or `yes` (any case) runs the command; anything else, or EOF,
declines it. Show `reason` to the user. Never sent with `--turbo` or `--dry-run`.

### `record`
```json
{"type": "record", "t": 6.1, "step": 1, "plan": "shortcut keys=cmd+n", "element": null, "probability": null, "confidence": null, "executed": true, "verified": true, "verify_probability": 0.97, "goal_probability": 0.12, "note": "", "verify_s": 0.6, "goal_s": 0.5}
```
- `safety_probability`: Jev's probability that the step's terminal command was safe to run unasked (null when not a
  terminal step, with `--turbo`, or when a dangerous pattern decided). Notes for approved commands start with
  `approved by the user (<reason>); `, declined ones are `user declined the terminal command (<reason>)`.
- `goal_probability`: Jev's probability that the screen after this step shows the whole goal done (null when the step
  failed, was a terminal command, or the backend can't judge, e.g. cosine). ≥ 0.9 ends the run as a success; 0.5–0.9
  adds a hint to `note` for the planner. `goal_s` may be null.
- Status: `note` starting with `dry-run` → dry run; `note` starting with `skipped:` → element below threshold;
  `plan == "planner error"` → the planner failed; otherwise OK when `executed && verified != false`, else failed.
- Failed verification: `"executed": true, "verified": false, "verify_probability": 0.12`.
- Declined command: `"executed": false, "note": "user declined the terminal command"`.
- Terminal output: `"note": "exit=0 output: 42"`.
- Nullable: `element`, `probability`, `confidence`, `verified`, `verify_probability`, `verify_s`.

### `result`
```json
{"type": "result", "t": 21.0, "success": true, "reason": "planner reported the goal is done", "steps": 2, "elapsed_s": 21.0, "history": [{"step": 1, "plan": "shortcut keys=cmd+n", "element": null, "probability": null, "confidence": null, "executed": true, "verified": true, "verify_probability": 0.97, "note": ""}]}
```
`history` holds the records without `type`, `t`, `verify_s` and `goal_s`. For a "fill out the form" goal with a form on
screen the checklist is the form's fields; a required field with no value (from the goal's labelled quotes or `run --data
<file>`, a JSON object or `label: value` lines) ends the run before any step with `reason: "needs_input: Email, Phone"` (the
labels), and password, card-number, SSN and ID fields are listed as `<label> (the user must enter it)`. Submitting always
asks first (`confirm_terminal` with command `submit the form "<title>"`, reason `sends your data`, even with `--turbo`);
declining ends the run with `reason: "declined: submit the form"`. A browser dialog saying leaving loses unsaved changes ("Changes you made may not be saved"), or Don't Save in a close sheet, also asks first (`confirm_terminal` with command `leave the page and lose unsaved changes`, reason `would discard unsaved changes`, even with `--turbo`); when the agent's own navigation raised it, declining presses Cancel and ends the run with `reason: "leave_declined"`. Reasons include `planner reported the goal
is done`, `Jev: the screen shows the goal done (p=0.97)`, `stopped: <plan or "N steps"> succeeded 3 times in a row`
(completed work being redone; `success` is true when Jev rated the goal likely done along the way, with `; Jev rated
the goal likely done` appended), `reached max steps (N)`, `N consecutive failed steps`, `dry run: stopped after one
step`, `unsure: <goal item>` (jev mode: two steps in a row where Jev's best option was below `jev_min_pick_p` (default 0.3, `--jev-min-p`), wasn't the checklist's next line and wasn't `done`, so nothing was done; the first such step is a record with `executed: false` and the note `unsure (p=0.xx): no option clearly fits; <top 3 options>`, and replans an outline), and `cancelled`.

### `error`
```json
{"type": "error", "t": 0.8, "message": "AccessibilityError: Could not bring TextEdit to the front", "kind": "AccessibilityError"}
```
`message` is `"<kind>: <text>"`. Known ones: `AccessibilityError: Accessibility permission missing…` (grant it to the
process that launched the CLI); `CalledProcessError: …open…` (`open -a` failed, usually an app name that doesn't
exist); `AccessibilityError: Could not bring … to the front` (another window kept focus). The traceback goes to
stderr.

## 4. `doctor --json`

```json
{"ok": false,
 "accessibility": {"ok": true},
 "ollama": {"ok": true, "url": "http://127.0.0.1:11434", "error": null, "models": [{"name": "qwen3.5:9b", "size_gb": 6.6}], "planner_model": "qwen3.5:9b", "has_model": true},
 "jev": {"ok": false, "backend": "jevk5", "model": "jevk5-4b-q8", "ram_gb": 5.1, "url": "http://127.0.0.1:8091", "served_file": null, "error": null, "command": "llama-server -m /Users/you/models/jevk5/jevk5-4b-v0.3-Q8_0.gguf -c 8192 -ngl 99 --parallel 1 --cache-ram 0 --port 8091"}}
```
- `ollama.error` is set when Ollama is unreachable (then `models` is empty).
- `has_model` checks `LOCAL_AGENT_PLANNER_MODEL`, so set it to the model the app will use.
- `jev.command` starts the chosen model (llama-server for JevK5, `ollama pull` for cosine); null for `openjev`.
- `jev.model` is the catalog key (`local-agent jev list --json` lists them with `ram_gb`); `doctor --jev <key>` checks
  that one. `jev.error` is set when llama-server serves a different file (`served_file`) than the chosen one; `run`
  then exits 2 before `start`.
- The first run may show the system Accessibility prompt. Accessibility belongs to the process that runs the CLI: when
  an app launches it, the grant must be on that app.

## 5. Cancelling

Send SIGINT. The CLI stops at the next Python instruction, emits `result` with `reason: "cancelled"` and the history so
far, and exits 130. A blocking model request may take a moment to interrupt; if the process is still alive after a few
seconds, SIGTERM, then SIGKILL.

## 6. Environment

Every `Settings` field (`local_agent/config.py`) can be set with `LOCAL_AGENT_<FIELD>`. Most useful for apps:

| Variable | Default |
|---|---|
| `LOCAL_AGENT_OLLAMA_URL` | `http://127.0.0.1:11434` |
| `LOCAL_AGENT_JEVK5_URL` | `http://127.0.0.1:8091` |
| `LOCAL_AGENT_JEV` | unset (catalog key, e.g. `jevk5-2b-q8`; `run` also takes `--jev`) |
| `LOCAL_AGENT_PLANNER_MODEL` | `qwen3.5:9b` (used by `doctor`; `run` takes `--model`) |
| `LOCAL_AGENT_PLANNER_NUM_CTX` | 8192 |
| `LOCAL_AGENT_VERIFY_THRESHOLD` | 0.5 |
| `LOCAL_AGENT_SETTLE_S` | 0.8 |

Use `.` as the decimal separator. Set `PYTHONUNBUFFERED=1`. GUI apps get a minimal `PATH`; terminal commands the
agent runs inherit it, so prepend `/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin`.

## 7. Focus rules

The agent observes and acts on the **frontmost** window.
- Don't pass `--app`: the agent opens and focuses the goal's app itself. While the LocalAgent app is frontmost (it
  launched the CLI), the planner is told to open the needed app first, so starting with the app in front is fine.
- Once the first step has run, the app must not activate itself, open windows or show alerts. Hiding it during the
  run is the safest option.
- The only exception is a `confirm_terminal` (now only for risky commands). Write the answer, then step out of the
  way: after a terminal command the CLI re-observes whatever window is frontmost (and `open -a` commands bring their
  app to the front themselves).
- Don't let the app's window cover the target app.
