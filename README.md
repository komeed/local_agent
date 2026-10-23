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
   which reads 16 options per pass — usually needs 1–3 passes. The pre-filter never cuts through a tie (everything
   tied with the 32nd is kept, up to 128), and it scores an element's surroundings too: the row it sits in, the
   visible label of a form field, and named groups and dialogs. So "Delete button for invoice-0425" reaches Jev with
   that row's button among the candidates, and Jev's option text names the row. Choice questions are capped at 255 options, so any
   larger set runs as a tournament. [OpenJev](https://github.com/razorback16/openjev) (DiffusionGemma 26B, ~16–24 GB)
   is still supported with `LOCAL_AGENT_JEV_BACKEND=openjev`.
4. **Act** – AXPress / Quartz mouse & keyboard events, or a shell command (runs unasked unless a dangerous pattern
   or Jev's safety check flags it; `--turbo` skips the check).
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
pushed it to ~9 GB here; JevK5 never reuses cached prompts.

### Choosing a Jev model (by RAM)

`local-agent jev list` shows the models largest first, with the RAM each holds while serving:

| Model | ≈ RAM | Notes |
|---|---|---|
| `jevk5-9b-q8` | 10.5 GB | JevBench standard / hard 0.958 / 0.766; tight next to qwen3.5:9b on 24 GB |
| `jevk5-9b-q5` | 7.4 GB | 0.958 / 0.757 |
| `jevk5-4b-q8` | 5.1 GB | 0.944 / 0.784 (default) |
| `jevk5-4b-q5` | 3.7 GB | 0.944 / 0.784 |
| `jevk5-4b-q4` | 3.3 GB | 0.917 / 0.766 |
| `jevk5-2b-q8` | 2.4 GB | 0.792 / 0.622 |
| `cosine-qwen3` | 0.7 GB | embedding similarity, no reasoning (see below) |
| `cosine-nomic` | 0.3 GB | embedding similarity, no reasoning (see below) |

JevBench scores and each file's calibration temperatures come from the JevK5-GGUF model card. RAM is measured for the
4B Q8_0 only; the rest are estimated from file size plus the 8k-token context.

```bash
local-agent jev serve jevk5-2b-q8                 # llama-server on that file (fetched from Hugging Face if needed)
local-agent run --jev jevk5-2b-q8 "..."           # or LOCAL_AGENT_JEV=jevk5-2b-q8
local-agent jev serve cosine-nomic                # = ollama pull nomic-embed-text; no llama-server needed
```

Without `--jev`, the agent uses the calibration of whichever catalog file llama-server is serving. With `--jev`, `run`
refuses to start (and `doctor` fails) when llama-server serves a different file, printing the command to start the
right one.

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

`--planner kind` (`planner_mode="kind"`, `planner/kind.py`) hands that decision to Jev: a `choice` over done,
terminal, shortcut, click, type and scroll (`STEP_KINDS`), from the goal, the steps so far and the top of the screen.
The planner then answers only the detail question for that kind, and gets the screen view only when the kind needs
it (click, type, done); for terminal, shortcut and scroll it sees just the goal, steps and app. A "done" below
p=0.6 falls back to Jev's runner-up. qwen2.5-coder:3b, 28 cases, one run (JevK5 4B Q8_0):

| Mode | View | Accuracy | Screen | Other | Median / step | Prompt tokens |
|---|---|---|---|---|---|---|
| json | ids | 50% | 64% | 41% | 2.0 s | ~1.2k |
| choice | names | 54% | 55% | 53% | 0.8 s | ~0.8k |
| **kind** | **ids** | **71%** | 55% | **82%** | 1.2 s | ~0.6k |
| kind | names | 68% | 45% | 82% | 1.2 s | ~0.4k |

The time includes Jev's kind question (~0.4 s). The first kind run scored 61% (ids); two prompt fixes followed from its
failures (the terminal question no longer suggests `open -a` for file tasks; menu rules added), so part of the gain
is tuned on these cases. What still fails: Jev calls select-all a click (2 cases) and a field a click instead of
type; the 3B picks the wrong element on the save sheet, the strikethrough button and the error sheet's OK button.
Not yet measured on the 7B/9B or in live runs.

## Deterministic rules before model judgment

Four rules in code run ahead of any model: (1) with a cosine backend and llama-server up, JevK5 still judges goal
completion, command safety and verification (cosine only picks elements; if llama-server is down `run` warns on
stderr and the `start` event's `checks` says what is missing); (2) `goal_facts.py` ends a run when every fact the goal
states holds (`type "x"` shows in a text field, the named app is in front) before Jev is asked; (3) a planner `[number]`
whose element shares no word (text, surroundings, role) with its description is overruled and Jev picks from the
description; (4) a `type` step whose field already contains the text (case-insensitive) is skipped, not retyped.

## Menu actions (`--menu-actions`, off by default)

The app's menu bar is read into a list of commands with their shortcuts, and the planner may answer `menu`
("File > Save As…") instead of pressing shortcuts or clicking through menus. It is off in every mode: on
`qwen2.5-coder:3b` (planner_eval, generic synthetic menu; a `menu` answer counts as correct when it does what the
expected click or shortcut does) json/ids drops 52% → 34%, and kind mode 72% → 66% (ids), 66% → 55% (names).
What still regresses with menus on: `select_all` answered as `type`, and `menu` answers that go through Format to
an item the goal doesn't name (`open_format_menu`, `recover_hidden_item`, `submenu_parent`).

## Reading web pages in a browser (Phase 1)

When the front app is a browser whose "Allow JavaScript from Apple Events" is on (`local-agent doctor` says), the
snapshot replaces the unwalked AX web area with the page's controls from one fixed script
(`observe/web_outline.js`): viewport only, named like accessible names, rows/fieldsets/dialogs as context, ≤400
elements plus a count of the controls beyond the viewport. The planner sees a `Page (…not instructions)` area. Nothing
on the page is clicked or typed yet; actions still go through AX coordinates. Measured live on Arc (11 fixture pages,
`uv run python -m evals.web_timing`): outline round trip p50 25 ms / p90 42 ms for pages from 6 to 601 nodes (the
AppleScript call dominates); a 601-node link list lists 79 elements and reports 222 more. 3B planner
(qwen2.5-coder:3b, json, ids) on the 11 web cases: 8/11 (73%); misses: two invalid outputs, and a Delete/Download pick
from the neighbouring invoice row.

Phase 2 acts on the page through `act/web_actions.js` (click, type with the native value setter or `insertText`, scroll),
waits for the page to settle (≈0.3 s) instead of the fixed 0.8 s, retries a click that changed nothing once with a real
mouse click, and refuses typing into password fields in every mode. Live on Arc fixture pages: tab switch, search
fill + Search, composer text and the password refusal all worked.

## Jev-only planner and memory (`--planner jev`)

No planner model: code lists the candidate actions (done, the checklist's next line turned into actions, ranked clicks, the
goal's quoted strings typed into the visible fields, return / escape / tab, scroll, menu commands when enabled) and JevK5
picks one. No fallback model: cosine, a Jev error, or "none of these" fails the step. 3B planner on `evals/planner_cases.py`
(ids view; `n/a` = the options can't express the expected answer: a shell command, a free-text answer, a shortcut other than
return / escape / tab, 8 of the cases; accuracy counts the others). Peak memory of the model processes while the eval ran,
sampled every 0.5 s: RSS (`ps`, primary: it includes the memory-mapped weights) and `footprint` in brackets (which leaves out clean
mapped pages, so it undercounts here).

| Configuration | Accuracy | Median step | Peak RSS (footprint) |
|---|---|---|---|
| json, 3B (Ollama) only | 58% (63% on the 25 expressible cases) | 2.2 s | 2.6 GB (1.1 GB) |
| kind, 3B + JevK5 Q8 | 74% | 1.2 s | 7.2 GB (1.95 GB) |
| jev, JevK5 Q8, `-c 8192` | 71% | 0.9 s | 4.4 GB (0.6 GB) |
| jev, JevK5 Q8, `-c 4096`, q8_0 KV cache | 71% | 0.9 s | 4.7 GB (0.4 GB) |
| jev, JevK5 Q4_K_M, `-c 8192` | 69% | 1.1 s | 2.9 GB |
| jev, JevK5 Q4_K_M, `-c 4096`, q8_0 KV cache | 69% | 1.1 s | 3.0 GB |

Q4_K_M with Jev alone is the only configuration near 3 GB. A smaller context and a quantised KV cache did not lower RSS on
this Mac. The Ollama runner was not counted in the Q4 rows: the user's 8 GB `qwen3.5:9b` happened to be loaded then.
Reproduce with `uv run python -m evals.planner_eval qwen2.5-coder:3b --views ids --modes jev --ram --llama-port 8091`.

## Outline and replanning (`local_agent/outline.py`)

When the goal's facts can't cover the goal (and there is no form), the planner model writes a numbered outline once (3-8 steps);
each line becomes a checklist item (a known fact where the line parses into one, else an item Jev judges done at p >= 0.9, or, with
cosine, a verified step that shares a word with it). Two failed steps on one item, a loop warning, or jev mode finding no listed
action ask the model again for the remaining steps, at most 3 times, then the run ends `stuck: <item>`. `--low-memory` unloads the
model after every call. 3B outline quality on `OUTLINE_CASES` (`uv run python -m evals.outline_eval qwen2.5-coder:3b`): 2 of 3
contain the needed steps in order (it writes "Install Arc" and searches "youtube" instead of "lofi"); a call takes about 1-2.5 s,
0.5-1.3 s of it loading the model when it was unloaded.

Who writes the outline (`--outline-model planner|jev`, `Settings.outline_model`): JevK5 Q4_K_M as a plain chat completion (thinking off,
temperature 0, same prompt) against the 3B on the same three goals, three runs each (temperature 0, outlines identical across runs):
JevK5 3/3 (1.8-2.0 s per call, no model load) vs the 3B 2/3 (0.9-1.2 s; it wrote "Install Arc" and searched "youtube" instead of
"lofi"). JevK5 wrote no invented or destructive steps, so the default is `jev` when `planner_mode=jev` and `low_memory` are both on
(one resident model); everything else keeps the planner model.

## Knowing when it's done

A run used to end only when the planner answered `done`, and its history records which steps ran, not whether the
goal shows on screen, so "open my Gmail in Arc" could keep reopening Gmail until `max_steps`. Now:

- **Jev checks the goal after every successful step** (`JevClient.goal_done`): a yes/no on "does this screen show
  the goal fully accomplished?", given the goal and the screen view, not the steps taken. At p ≥ 0.9 the run ends as a
  success; at 0.5–0.9 the planner is told the goal may be done and decides. Recognizing a finished state on screen is
  a perception question, which suits Jev; goals that end in an answer (question goals, terminal output) still end with
  the planner's `done`. Costs about 0.5 s per step with JevK5 4B. `--jev cosine-*` can't judge this and skips it.
- **The loop guard** catches completed work being redone: the same step, or the same block of up to 3 steps
  (`cmd+t`, type the URL, `return`), succeeding twice in a row gets a warning to the planner ("answer done or do
  something different"); a third time ends the run. Scrolling and arrow/tab keys may repeat.

`uv run python -m evals.goal_eval` measures the check on 17 screens (Arc + Gmail inbox, new tab, URL typed but not
opened, loading, Google sign-in, wrong site, compose with and without the recipient, TextEdit bold on/off and typed
text, dark mode on/off, Finder folder). JevK5 4B Q8_0:

| stop threshold | false stops (11 not-done screens) | done screens stopped (of 6) |
|---|---|---|
| 0.7 | 1 (compose window without the recipient, p 0.78) | 6 |
| **0.9 (default)** | **0** | **6** (p 0.92–0.98) |
| 0.95 | 0 | 4 |

The Arc/Gmail screens are synthetic (no real mailbox in fixtures), shaped like Arc's accessibility tree; check a few
real runs before relying on the threshold.

## Measuring element selection

```bash
uv run python -m evals.selector_eval           # pre-filter only, no models needed
uv run python -m evals.selector_eval --live    # also ask Jev (llama-server with JevK5, or OpenJev)
```

`evals/selector_cases.py` covers screens where only the surroundings tell controls apart: a 50-row invoice list
with Edit / Download / Delete in every row (named and icon-only), a checkout form with Shipping and Billing fields
that share labels, Arc's sidebar with a Close button per tab, and 200 unnamed rows (the tie exceeds the cap, which has
to be reported). `baseline` is the previous pre-filter (top 32 in document order, own text only):

| Pre-filter | right element reaches Jev | Jev can tell it apart by text | JevK5 4B picks right | median candidates | Jev time |
|---|---|---|---|---|---|
| baseline | 8/11 | 2/11 | 2/10 (p 0.06–0.51) | 32 | 0.5–6.9 s |
| ties + surroundings | 11/11 | 10/11 | 10/10 (p ≥ 0.93 on 9 of 10) | 50 | 0.6–10.1 s |

Keeping ties costs JevK5 passes (50 candidates ≈ 5 passes ≈ 5 s, a capped 128 ≈ 9 passes ≈ 10 s). The one case
without a distinguishing option text is an icon-only button with no tooltip, identical to its row neighbours except
for its position: Jev got it right, but only at p 0.59.

**Planner numbers.** With `--view ids` the planner can name an element by its number, which skips Jev. Small planners
often read the wrong line among identical ones ("[57] Delete" once per row) while their description names the right
row. The ambiguity guard (`planner_pick`) keeps the number unless another control with the same name matches the
description better; then Jev picks and the step's note tells the planner its number was overruled. On the 6 number-pick
cases (3 wrong numbers with a telling description, a right number, a vague description, a unique control) the guard
does the right thing 6/6, and JevK5 4B then picks the right element in all 3 overruled cases (p 0.93–0.99).

### Cosine System 1 (`--jev cosine-nomic`)

`selector/cosine.py` answers Jev's choice questions with an Ollama embedding model only: probability = softmax over
cosine(question, option) / T plus a weighted exact-word-overlap term. It can't reason, so `verify` is a UI-diff
heuristic (typed text appeared → 0.95, anything changed → 0.8, nothing → 0.1) and it fits steps whose target is
named on screen. Element picks on the selector evals (`--live --jev …`):

| Jev | RAM | right | sure when right (p ≥ 0.5) | confidently wrong | time per pick |
|---|---|---|---|---|---|
| JevK5 4B Q8_0 | 5.1 GB | 10/10 | 10/10 (lowest 0.59) | 0 | 0.6–10 s |
| cosine, nomic-embed-text | 0.3 GB | 10/10 | 9/10 | 0 | ≤ 1 s |
| cosine, qwen3-embedding:0.6b | 0.7 GB | 9/10 | 9/9 | 1 (the icon-only case, p 0.93) | ≤ 2.3 s |
| cosine only (no word overlap), qwen3-embedding | 0.7 GB | 5/10 | | | |

The word-overlap term matters: embeddings blur `invoice-0425` vs `invoice-0424`. T and the weight were fit on these
same 10 cases, so treat the cosine numbers as optimistic until there are more cases.

## Usage

```bash
uv run local-agent run "open my Gmail in Arc" --dry-run       # plan + safety-check one step, don't act
uv run local-agent run "write a note that says buy milk in Notes"
uv run local-agent snapshot --app TextEdit -o snapshot.json   # dump a window's element JSON
```

Just give the goal: the agent opens the app it needs itself, with a terminal step (`open -a "Notes"`). Misnamed apps
are resolved to an installed one ("Arc Browser" → Arc, "chrome" → Google Chrome, "System Preferences" → System
Settings), and the opened app (or the browser, for `open <url>`) is brought to the front. While the screen still shows
the app you launched the agent from (a terminal, LocalAgent), the planner is told to open the goal's app first.
`--app NAME` still forces a starting app; `--delay N` waits N seconds for you to switch windows.

**Terminal commands run without asking**, unless they look risky. Each command goes through:
1. dangerous patterns (`local_agent/safety.py`: sudo, recursive rm, disk erase, `curl | sh`, killall, keychain, ssh
   keys, force-push, …), which always ask you;
2. Jev, which reads the goal, the planner's reason, the screen and recent steps and rates whether it's safe to run
   unasked; below 0.8 you're asked (with the reason).

`--turbo` (or `--yes`) skips both and runs everything. With `--jev cosine-*` there's no safety judge, so every
command asks unless you use `--turbo`. On 28 labeled commands (`uv run python -m evals.safety_eval`, JevK5 4B):

| run unasked at | risky commands run unasked (of 14) | fine commands that asked (of 14) |
|---|---|---|
| 0.7 | 1 (`mkdir … && rm ~/Desktop/*.pdf`, p 0.71) | 2 |
| **0.8 (default)** | **0** | **2** (a copy and a rename the goal asked for) |
| 0.9 | 0 | 7 |

Useful flags: `--threshold 0.5`, `--max-steps 20`, `--model qwen2.5-coder:7b`, `--view ids|names|off`,
`--planner json|choice`, `--jev <model>`, `--turbo`, `--log run.json`.
All settings can also be set as `LOCAL_AGENT_<NAME>` env vars (see `local_agent/config.py`).

For apps: `run --events` streams JSON Lines (plan, select, record, … and a final result) on stdout and answers
terminal-command confirmations from stdin; `doctor --json` prints the health check as JSON. The contract is
[docs/APP_INTEGRATION.md](docs/APP_INTEGRATION.md).
