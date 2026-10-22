# Driving `local-agent` from the LocalAgent macOS app

This is the contract between the Swift app (repo root `LocalAgent/`) and the Python agent in the `local_llm/`
submodule. The app never imports Python: it launches the `local-agent` CLI as a child process, reads **JSON Lines
events** from its stdout, and answers terminal-command confirmations on its stdin.

Everything below was checked against the code in `local_agent/cli.py` and `local_agent/events.py`
(event schema version **1**). When this file and the code disagree, the code wins. Also update this file.

---

## 1. One-time setup on the Mac

| What | How | Check |
|---|---|---|
| Python env | `cd local_llm && uv sync` creates `local_llm/.venv` (Python 3.12) | `local_llm/.venv/bin/local-agent --help` |
| Ollama (planner) | Ollama app or `ollama serve`, at `http://127.0.0.1:11434` | `doctor --json` → `ollama.ok` |
| Planner models | `ollama pull qwen3.5:9b` (default); others to compare, e.g. `qwen2.5-coder:7b`, `qwen2.5-coder:3b` | `doctor --json` → `ollama.models` |
| Jev (element picker) | `llama-server -m ~/models/jevk5/jevk5-4b-v0.3-Q8_0.gguf -c 8192 -ngl 99 --parallel 1 --cache-ram 0 --port 8091` | `doctor --json` → `jev.ok` |

Keep `--parallel 1 --cache-ram 0` on llama-server. Without them it takes ~9 GB and memory pressure on the 24 GB Mac
goes yellow.

### Xcode target settings (required)

- **App Sandbox: off.** A sandboxed app can't launch a binary outside its container, and it can't drive other apps
  through Accessibility. This is a local dev tool, so leave it unsandboxed.
- **Accessibility permission belongs to the app.** macOS attributes a child process's Accessibility use to the app
  that launched it, so the **LocalAgent app** needs it in System Settings → Privacy & Security → Accessibility. A
  rebuild with a different signature can silently invalidate the grant. Sign with a stable team or certificate, and
  if `doctor --json` reports `"accessibility": false`, remove the app from the list and add it again. (`doctor` also
  triggers the system prompt.)
- Run the agent's own Python, not whatever is on `PATH`. GUI apps get a minimal `PATH`
  (`/usr/bin:/bin:/usr/sbin:/sbin`), so `uv` and Homebrew aren't on it. Use the absolute path
  `<local_llm>/.venv/bin/local-agent`, and let the user pick or store the `local_llm` folder location (a setting, not
  a hard-coded path).

### The focus rule (most important runtime gotcha)

The agent observes and acts on the **frontmost window**. When the user clicks "Run" in your UI, *your app* is
frontmost.

- **Always pass `--app <TargetApp>`** (e.g. `--app TextEdit`). The CLI activates that app, waits 0.5 s, then starts
  observing. Without `--app`, it waits `--delay` seconds (default 3) for the user to switch manually.
- **Don't take focus back during a run.** No `NSApp.activate`, no key windows, no modal alerts. If your app becomes
  frontmost mid-run, the agent will plan against *your* UI. Show progress passively, e.g. a floating, non-activating
  panel or a window on another display.
- **Don't cover the target app.** Non-button clicks are synthesized mouse events at screen coordinates. If your window
  overlaps the target control, the click lands on your window.
- The terminal confirmation (section 4) is the one moment the user must interact. Make it a non-activating panel
  (`NSPanel` with `.nonactivatingPanel`), or accept that the next observation may see your app and re-activate the
  target before answering.

---

## 2. Commands the app uses

All commands are `<local_llm>/.venv/bin/local-agent <command> …`. Set the working directory to `<local_llm>`.

### 2.1 `doctor --json`: health check and installed models

```
local-agent doctor --json
```

Prints **one** JSON object and exits 0 when everything needed is ready, 1 otherwise:

```json
{
  "accessibility": true,
  "ollama": {
    "url": "http://127.0.0.1:11434",
    "ok": true,
    "models": [{"name": "qwen3.5:9b", "size_gb": 7.64}, {"name": "qwen2.5-coder:3b", "size_gb": 1.93}],
    "planner_model": "qwen3.5:9b",
    "planner_model_present": true,
    "error": null
  },
  "jev": {"backend": "jevk5", "url": "http://127.0.0.1:8091", "ok": true},
  "ok": true
}
```

Use `ollama.models` to fill a model picker. `ollama.error` is a string when Ollama is unreachable. `size_gb` is the
download size; loaded memory is larger (see section 6).

### 2.2 `run --events`: run a goal and stream events

```
local-agent run "<goal>" --events --app TextEdit --model qwen2.5-coder:7b [options]
```

| Option | Default | Meaning |
|---|---|---|
| `GOAL` (argument) | required | Plain-language task. Pass it as a single argv element (no shell quoting needed with `Process`). |
| `--events` | off | **Required for the app.** JSON Lines on stdout; human-readable log moves to stderr. |
| `--app NAME` | none | Activate this app first (launches it if needed). Use the app's name, e.g. `TextEdit`, `Finder`. |
| `--delay SECONDS` | 3 | Only without `--app`: wait before the first observation. |
| `--model NAME` | `qwen3.5:9b` | Ollama planner model, any name from `doctor --json`. |
| `--view ids\|names\|off` | `ids` | Screen view sent to the planner: numbered controls, names only, or none. |
| `--planner json\|choice` | `json` | `json`: one free-form step per call. `choice`: multiple-choice questions (experimental, for small models). |
| `--dry-run` | off | Plan (and pick the element for) **one** step, act on nothing, stop. Safe for comparing models. |
| `--yes` / `-y` | off | Run terminal commands without asking (no `confirm_terminal` events). |
| `--threshold P` | 0.5 | Minimum Jev probability to act on a picked element. |
| `--max-steps N` | 20 | Stop after N steps. |
| `--log PATH` | none | Also write the step history as a JSON array to PATH. |

Exit codes: **0** goal reported done (or, with `--dry-run`, the one step was planned), **1** stopped without success
(max steps, 3 consecutive failures, a dry-run step that was skipped or failed), **2** crashed before or during the run (an `error` event explains), **130** cancelled with
SIGINT. Typer usage errors (bad flag) also exit 2, but print usage to stderr with **no** events, so treat "exit 2
and no `error` event" as a usage error.

### 2.3 `snapshot`: what the agent can see (optional debug view)

```
local-agent snapshot --app TextEdit -o /tmp/snap.json
```

Writes the full element list (every interactive control, plus visible text) as JSON. Without `-o` it prints the
JSON to stdout. With `-o` it prints a human summary to stdout instead. Useful for a "what does the agent see?" panel. The
screen view the planner actually gets arrives in every `plan` event (`screen` field), so a separate snapshot is
rarely needed.

### 2.4 Planner eval: batch comparison of models (optional)

```
uv run python -m evals.planner_eval qwen3.5:9b qwen2.5-coder:7b --modes json --views ids --json /tmp/eval.json
```

Scores each model on 28 fixed cases (no UI is touched). It runs with `uv run`, so from the app use the venv's Python:
`<local_llm>/.venv/bin/python -m evals.planner_eval …`. Stdout is human progress text; the **`--json` file** is the
machine-readable result, a list with one object per (model, mode, view):

```json
{"model": "qwen3.5:9b", "mode": "json", "view": "ids",
 "accuracy": 0.93, "screen_accuracy": 0.91, "other_accuracy": 0.94,
 "median_s": 4.75, "p90_s": 6.93, "prompt_tokens": 1767, "max_prompt_tokens": 2661,
 "loaded_gb": 7.8, "pressure_level": 1, "free_pct": 44,
 "rows": [{"case": "select_all", "category": "shortcuts", "needs_screen": false, "passes": 1, "runs": 1, "why": ""}]}
```

`pressure_level`: 1 normal (green), 2 warn (yellow), 4 critical. It takes roughly 25 s (3B) to 2 min (9B) per model and
mode. It unloads each model after its run.

---

## 3. Event stream (`run --events`)

- One JSON object per line on **stdout**, flushed immediately. Nothing else is written to stdout in this mode.
- **stderr** carries the human-readable log (colored text). Always drain it (see the Swift sketch), or the child
  blocks once the pipe buffer fills.
- Every event has `type` (string) and `t` (seconds since the run started, float). **Ignore unknown `type`s and
  unknown fields.** New ones may be added without a version bump.
- Order: `start`, then for each step `plan` → (`select`) → (`confirm_terminal`) → `record`, and finally `result`
  (or `error`). The last `plan` of a successful run has `plan.done == true` and no `record` follows it.

### `start`

```json
{"type": "start", "t": 0.03, "version": 1, "goal": "Make the text bold", "dry_run": false, "target_app": "TextEdit",
 "settings": {"planner_model": "qwen3.5:9b", "planner_mode": "json", "planner_view": "ids", "planner_num_ctx": 8192,
              "jev_backend": "jevk5", "threshold": 0.5, "verify_threshold": 0.5, "max_steps": 20,
              "max_consecutive_failures": 3}}
```

Check `version == 1`. `settings` are the values actually in effect (flags plus env vars plus defaults).

### `plan`: what the planner decided (the main thing to show when comparing models)

```json
{"type": "plan", "t": 4.9, "step": 1, "app": "TextEdit", "window": "Untitled", "elements": 112,
 "plan": {"done": false, "reasoning": "Select the text first.", "action": "shortcut",
          "target": null, "text": null, "keys": "cmd+a", "direction": null, "command": null},
 "summary": "shortcut keys=cmd+a",
 "planner_s": 4.71, "prompt_tokens": 1767,
 "screen": "App: TextEdit — window \"Untitled\"\nFocused: [0] text area = \"Hello world\" (focused)\n…",
 "messages": [{"role": "system", "content": "…"}, {"role": "user", "content": "…"},
              {"role": "assistant", "content": "{\"done\": false, …}"}]}
```

| Field | Meaning |
|---|---|
| `step` | 1-based step number. |
| `app`, `window`, `elements` | What was observed before planning: frontmost app, window title, element count. |
| `plan` | The planner's step. `action` ∈ `click`, `double_click`, `right_click`, `type`, `shortcut`, `scroll`, `terminal`, or null when `done`. `target` is `{"id": int\|null, "role": str, "description": str}`. `id` is a `[number]` from `screen`. |
| `summary` | One-line human form of `plan`. |
| `planner_s` | Wall time of the planner call(s), in seconds. The first step includes model load time (~10–15 s cold). |
| `prompt_tokens` | Ollama's `prompt_eval_count` for the planner call (largest call in `choice` mode). May be lower than the real prompt when Ollama reused its cache. |
| `screen` | The exact screen view text the planner got (null with `--view off`). |
| `messages` | The exact conversation sent to Ollama plus the model's raw reply as the last `assistant` message. In `choice` mode this is the whole multi-question conversation. ~2–8 KB; show it in an expandable "prompt" panel. |

When the planner fails (Ollama down, invalid JSON twice), no `plan` event is sent for that step. A `record` follows
with `plan: "planner error"` and the error in `note`.

### `select`: which element will be acted on

Only for steps that target an element (clicks, and `type`/`scroll` with a target).

```json
{"type": "select", "t": 5.1, "step": 2, "source": "jev",
 "element": {"id": 31, "role": "AXMenuBarItem", "name": "Format",
             "description": "AXMenuBarItem title=\"Format\" in AXMenuBar at (203,0)", "path": "AXMenuBar",
             "frame": {"x": 203.0, "y": 0.0, "w": 52.0, "h": 24.0}},
 "probability": 0.99, "confidence": 0.93, "candidates": 14, "rounds": 1, "select_s": 1.8}
```

`source` is `"planner"` when the planner named the element by number (no Jev call; `select_s` is null), else `"jev"`.
`probability` is what gates acting (`< threshold` → the step is skipped and the `record` note says so). `frame` is in
screen points (top-left origin), handy for drawing a highlight.

### `confirm_terminal`: the app must answer

```json
{"type": "confirm_terminal", "t": 9.2, "step": 3, "command": "ls ~/Downloads | wc -l"}
```

See section 4. Not sent with `--yes` or `--dry-run`.

### `record`: outcome of a step

```json
{"type": "record", "t": 6.9, "step": 1, "plan": "shortcut keys=cmd+a", "element": null,
 "probability": null, "confidence": null, "executed": true, "verified": true, "verify_probability": 0.97,
 "note": "pressed cmd+a", "status": "ok", "verify_s": 0.6}
```

| Field | Meaning |
|---|---|
| `status` | `"ok"`, `"failed"` or `"dry-run"`. Use this for the colored badge. |
| `executed` | The action ran (or the terminal command exited 0). |
| `verified` | Jev judged the UI changed as intended (`verify_probability ≥ verify_threshold`), or null when no check ran (e.g. terminal steps). |
| `note` | Executor message, skip reason, or terminal output tail (`exit=0 output: …`). |
| `element` | Text description of the acted-on element, or null. |
| `verify_s` | Seconds spent verifying, or null. |

### `result`: always the last event of a finished run

```json
{"type": "result", "t": 21.4, "success": true, "reason": "planner reported the goal is done", "steps": 3,
 "history": [ {"step": 1, "plan": "…", "status": "ok", "…": "…"} ]}
```

`history` holds the same objects as the `record` events. `reason` is one of: `planner reported the goal is done`,
`reached max steps (N)`, `N consecutive failed steps`, `dry run: stopped after one step`, `cancelled`.

### `error`: the run crashed

```json
{"type": "error", "t": 0.4, "message": "RuntimeError: …"}
```

Followed by exit code 2 and no `result`. Typical causes: `AccessibilityError: Accessibility permission missing…`,
`CalledProcessError` (`--app` names an app that doesn't exist), `AccessibilityError: Could not bring X to the front`
(something kept focus; see the focus rule). Ollama/Jev being down does **not** produce `error`. It shows up as failed `record`s, then a `result`.

---

## 4. Terminal-command confirmation

When the planner wants to run a shell command and `--yes` wasn't passed:

1. The CLI emits `confirm_terminal` with the command and **blocks** reading one line from stdin.
2. The app shows the command and writes `y\n` (run it) or `n\n` (decline) to the child's stdin.
3. Anything other than `y`/`yes` (case-insensitive), or closing stdin, counts as declined. The step's `record` then
   has `note: "user declined the terminal command"`, and the planner sees that and tries something else.

Keep the child's stdin pipe open for the whole run. If you close it, every later command is declined automatically.

---

## 5. Cancelling a run

- `process.interrupt()` (SIGINT): the CLI stops right away, emits
  `{"type": "result", "success": false, "reason": "cancelled", "steps": 0, "history": []}` and exits 130. An action
  already sent to the target app isn't undone. Prefer this. (The step history so far is in the `record` events you
  already have.)
- `process.terminate()` (SIGTERM): immediate kill, **no** final event. Use it only if SIGINT hasn't ended the
  process after a few seconds.

Only one run at a time: there is one keyboard and mouse. Disable "Run" while a run is in progress.

---

## 6. Comparing models: memory, speed and what to expect

- **Memory:** Ollama keeps a model loaded for 5 minutes after its last use. Switching models in the UI can leave two
  loaded and push memory pressure to yellow. When the user switches models, unload the old one:
  `POST http://127.0.0.1:11434/api/generate` with body `{"model": "<old>", "keep_alive": 0}`.
  Loaded sizes next to JevK5 (~5 GB): qwen3.5:9b 7.8 GB, qwen2.5-coder:7b 5.1 GB, qwen2.5-coder:3b 2.4 GB.
- **Cold start:** the first `plan` after a switch includes loading the model (~10–15 s for the 9B).
- **Safe comparisons:** `--dry-run` plans and picks one step without touching anything. Run the same goal on the
  same screen state with each model and compare `plan`, `planner_s` and `messages` side by side.
- **Planner eval results** (28 cases; README "Choosing the planner model" has the full table):

  | Model | Best setup | Accuracy | Planner time / step |
  |---|---|---|---|
  | qwen3.5:9b | `--view ids` (default) | 93% | ~4.8 s |
  | qwen2.5-coder:7b | `--view names` | 61% | ~3.7 s |
  | qwen2.5-coder:3b | `--view off` | 32% | ~1.7 s |

  Every step also costs a few seconds beyond the planner: re-observing, the 0.8 s settle wait, and Jev's
  select/verify (`select_s`, `verify_s`).

---

## 7. Environment variables

Every setting in `local_agent/config.py` can be overridden with `LOCAL_AGENT_<FIELD_NAME_UPPERCASE>` in the child's
environment, e.g. `LOCAL_AGENT_OLLAMA_URL`, `LOCAL_AGENT_PLANNER_NUM_CTX`, `LOCAL_AGENT_VERIFY_THRESHOLD`,
`LOCAL_AGENT_JEVK5_URL`, `LOCAL_AGENT_SETTLE_S`. CLI flags win over env vars. Booleans accept
`1/true/yes/on`. Also set `PYTHONUNBUFFERED=1` (harmless; events are flushed anyway).

---

## 8. Swift sketch

```swift
import Foundation

/// Launches `local-agent run --events` and delivers each event on the main queue.
final class AgentRunner {
    let agentRoot: URL                       // the local_llm folder (user setting)
    private var process: Process?
    private var stdinHandle: FileHandle?

    init(agentRoot: URL) { self.agentRoot = agentRoot }

    func start(goal: String, targetApp: String, model: String, extraArgs: [String] = [],
               onEvent: @escaping ([String: Any]) -> Void,
               onLog: @escaping (String) -> Void,
               onExit: @escaping (Int32) -> Void) throws {
        let p = Process()
        p.executableURL = agentRoot.appendingPathComponent(".venv/bin/local-agent")
        p.arguments = ["run", goal, "--events", "--app", targetApp, "--model", model] + extraArgs
        p.currentDirectoryURL = agentRoot
        var env = ProcessInfo.processInfo.environment
        env["PYTHONUNBUFFERED"] = "1"
        p.environment = env

        let out = Pipe(), err = Pipe(), inp = Pipe()
        p.standardOutput = out
        p.standardError = err
        p.standardInput = inp

        var pending = Data()                 // only touched on the handler's serial queue
        out.fileHandleForReading.readabilityHandler = { h in
            let chunk = h.availableData
            if chunk.isEmpty { h.readabilityHandler = nil; return }   // EOF
            pending.append(chunk)
            while let nl = pending.firstIndex(of: 0x0A) {
                let line = pending[pending.startIndex..<nl]
                pending.removeSubrange(pending.startIndex...nl)
                if let obj = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any] {
                    DispatchQueue.main.async { onEvent(obj) }
                }
            }
        }
        // Always drain stderr, or the child blocks when the pipe fills.
        err.fileHandleForReading.readabilityHandler = { h in
            let chunk = h.availableData
            if chunk.isEmpty { h.readabilityHandler = nil; return }
            let text = String(decoding: chunk, as: UTF8.self)
            DispatchQueue.main.async { onLog(text) }
        }
        p.terminationHandler = { proc in
            DispatchQueue.main.async { onExit(proc.terminationStatus) }
        }
        try p.run()
        process = p
        stdinHandle = inp.fileHandleForWriting
    }

    /// Answer a `confirm_terminal` event.
    func confirmTerminal(_ allow: Bool) {
        stdinHandle?.write(Data((allow ? "y\n" : "n\n").utf8))
    }

    /// SIGINT: the CLI emits a final `result` with reason "cancelled" and exits 130.
    func cancel() { process?.interrupt() }
}
```

Dispatch on `event["type"] as? String`: `"start"`, `"plan"`, `"select"`, `"confirm_terminal"`, `"record"`, `"result"`,
`"error"`, and ignore anything else. Use the same `Process` pattern with `["doctor", "--json"]` (read all of stdout,
decode one object) to fill the model picker and a status bar.

---

## 9. Checklist for the app

- [ ] Settings: path to `local_llm`. Validate with `doctor --json` at launch and on a "Refresh" button.
- [ ] Model picker from `ollama.models`. Unload the previous model on change.
- [ ] Target app field (default `TextEdit`), passed as `--app`.
- [ ] Options: view (`ids`/`names`/`off`), planner (`json`/`choice`), dry run, max steps, threshold.
- [ ] Run log: one row per step from `plan` + `select` + `record` (summary, element, probability, status, timings),
      with expandable `screen` and `messages`.
- [ ] Non-activating confirmation panel for `confirm_terminal`.
- [ ] Cancel button → `interrupt()`. Disable Run while running.
- [ ] Never activate the app or cover the target window during a run.
