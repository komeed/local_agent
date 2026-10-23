# Plan: let the UI describe itself instead of hardcoding app knowledge

## Why
Recent fixes put app knowledge into prompts and code:
- the shortcut lists in `planner/qwen.py` and `planner/choice.py::SHORTCUTS`;
- the "goal names an app" and "close the Open file panel" rules (`Agent._close_open_panel`).

Each live failure adds another rule. The TextEdit Open panel showed the root causes:
- The planner was shown `[72] button` with no name, because the walker never read the label "New Document".
- 1,339 file rows filled the view budget around it.

No model size fixes that, because the information never reached the model.

**Principle: code lists what's possible, a model picks from the list, code checks the result.**
- Every model decision is a choice among options built from the live UI. Free text is only text to type, a shell
  command, or a target description.
- Prompts carry no knowledge about specific apps.
- More, smaller questions are fine. A step may take 1–3 s longer if the choices become reliable.

**Keep** (general, not app-specific): AX typing, the number/description check, rejecting a false `done`, goal facts,
the Jev goal/safety checks, and the loop guard.

## Part 1: Menu commands as the action list (general for every app)
Every Mac app lists its commands in the menu bar, with their shortcuts.

**Read them** (`observe/menus.py`, new):
- When the front app changes, walk its `AXMenuBar` → `AXMenuBarItem` → `AXMenu` → `AXMenuItem` (submenus to depth 2).
- For each item read `AXTitle`, `AXEnabled`, `AXMenuItemCmdChar`, `AXMenuItemCmdModifiers` and `AXMenuItemCmdVirtualKey`.
- Closed menus still expose their items. They report 0×0 frames, which is why the snapshot drops them today. The
  frames don't matter here.
- Cache the result per (pid, window title); re-read whenever an item's enabled state could have changed, which is every
  step (cheap: no frames).
- Output: a list of `MenuCommand(path="File > Save As…", keys="cmd+shift+s" | None, enabled, ref)`.

**Use them** as a new planner action and Jev kind, `menu`:
- The planner (json) or Jev (kind mode) picks a command by its path from the *enabled* commands, as an enum. With
  ~150 items per app this goes through the prefilter plus Jev's choice, exactly like element selection.
- Code runs it by pressing the shortcut when there is one, otherwise by `AXPress` on the item (works on closed menus),
  otherwise by opening the menu and clicking.
- Verify as usual.

**This replaces:**
- the shortcut lists in both planner prompts (deleted);
- `ChoicePlanner`'s "Which keys?" question, which keeps only keys that are not menu commands (escape, return, tab, arrows);
- the "menus" paragraph that teaches opening AXMenuBarItem → item → submenu (the menu path does it).

The `shortcut` action stays for keys only.

## Part 2: Name every element (general)
In `observe/ax_tree.py::_to_element`, an element with no title, label or value gets its name from the first of these
that exists:
1. `AXDescription`
2. `AXTitleUIElement`'s value (already read)
3. the text of a child `AXStaticText`/`AXImage` description (`_child_text`, extended to buttons)
4. `AXHelp`
5. `AXIdentifier`, if it reads like words (e.g. `newDocumentButton` → "new document button")
6. the nearest label (`observe/context.py`, already done for fields; extend to buttons inside dialogs)

Record which source the name came from (`UIElement.name_source`) so evals can show it.

**Step 1 result (done).** The panel's buttons *are* named (`AXTitle "New Document"`, identifier
`NewDocumentButton`). The real cause was the walk: depth-first with a 5 s limit, so 1,339 file rows used up the time
before the buttons were reached, and the lone unnamed `[72] button` was a scroll-bar part. Fixed generally:
- list rows are walked last;
- scroll-bar parts are dropped;
- identifiers name icon-only controls.

The 3B now answers `click New Document` on the recorded panel in both views.

**Original plan for this step: a live snapshot of TextEdit's Open panel** (read-only; it launches TextEdit fresh on the user's screen).
Find which attribute holds "New Document". If the panel's view-service process exposes nothing at all, the fallback
is Part 1: File > New is in the menu list either way.

## Part 3: Outline-and-drill instead of one flat, truncated view
Today `screen_view.render` flattens everything and cuts it to the budget, so whatever sorts last is lost.

**Outline** (`observe/outline.py`, new):
- Group elements into regions using the existing `area()` grouping: dialog/sheet, toolbar, sidebar, each list, the
  window's buttons, menu bar, focused element.
- One line per region with a count and its first few names, e.g.
  `Dialog "Open": buttons (3): New Document, Cancel, Open · sidebar (60 rows) · file list (1339 rows)`.
- Buttons and fields in a region are always listed before its list rows, so a long list can't push them out.

**Drill for selection** (`JevClient.select`, behind a setting `select_mode="drill"`, flat stays the default until
measured):
- Jev answers a `choice` over the regions ("which region holds: New Document button?").
- Then it answers within that region. A region over 32 items still goes through the prefilter and tournament inside it.
- That's 2 questions instead of 1 over everything, about +0.5–1 s.
- Cross-region ambiguity (a "Save" in the toolbar and in a dialog) is handled by the region step, using the
  description's location words.

**Expand for reading** (a planner action `look <region>`):
- The planner gets the outline by default; the full view is gone.
- When it needs the contents of one region (which files are listed, what a message says), it answers `look`, and the
  next prompt carries that region rendered in full (up to the budget).
- In kind mode Jev gains a `look` kind ("the step needs text that is not on the outline").
- The loop guard limits looks to 2 in a row.

## Part 4: Remove the special cases
Once Parts 1–3 pass their evals, remove:
- ~~`Agent._close_open_panel`: the planner sees `Dialog "Open": New Document, Cancel` and picks it, or uses the menu.~~ Removed after step 1: the walk fix made New Document visible.
- the shortcut lists in the prompts;
- the menu-navigation paragraph.

Keep "open the goal's app in code" for now. It's general (any app named in the goal), not about one app.

## Evals (measure before switching any default)
New cases in `evals/selector_cases.py` / `evals/planner_cases.py`. Snapshots are synthetic where possible; the live
snapshot from Part 2 is recorded as a fixture.

| Case | What it checks | Pass |
|---|---|---|
| `textedit_open_panel` | Open panel with 1,339 file rows; goal "new document" | picks New Document (button or File > New) |
| `save_dialog` | "save it as helloworld.txt" after typing | Save ⌘S from menus, then types the name into the sheet's field, then Save |
| `long_list_button` | 500-row list plus 3 dialog buttons; target is a button | button survives; drill picks it |
| `menu_only_command` | goal needs a command with no visible button (Format > Make Plain Text) | `menu` action with that path |
| `unnamed_icons` | toolbar icons named only by AXDescription/AXHelp | named in the view and picked |
| `read_list` | "which files are in Downloads?" with a long Finder list | planner uses `look` on the list, then answers |

Run all of these, plus the existing planner and selector cases, with `--planner json` and `--planner kind` on the 3B:
- **flat:** current view, no menus;
- **menus + names:** Parts 1–2;
- **outline + drill:** Parts 1–3.

Report accuracy, median time per step and prompt tokens.

**A part is accepted when:**
- its new cases pass;
- existing accuracy doesn't drop by more than 2 cases;
- time per step grows by ≤1.5 s.

## Order of work
1. Live snapshot of TextEdit's Open panel → Part 2 (name recovery) + `textedit_open_panel` and `unnamed_icons` cases.
   Small.
2. Part 1 (menu commands) + `menu_only_command` and `save_dialog`. Remove the prompt shortcut lists in the same change
   and compare.
3. Part 3 (outline, drill, look) behind settings, with `long_list_button` and `read_list`. Compare flat vs drill.
4. Part 4: delete the special cases that the evals show are no longer needed.
5. Live check on the user's goals: TextEdit new doc + type + save, Open ChatGPT in Safari, Gmail in Arc. Run with a
   JevK5 model, since cosine has no goal or safety check.

Each step is its own change with its own eval numbers in the README.

## Risks and open questions
- **Unreadable panels.** Some panels (view-service processes such as the Open panel, web content) may expose little
  through AX. The menu list is the fallback for commands. For content, a browser backend later (see the Playwright
  discussion).
- **Menu size.** Xcode and browsers have hundreds of items. The prefilter must rank paths by the description; only
  enabled items are offered.
- **Choosing between menu and click.** In json mode the planner now chooses between `menu` and `click`. The 3B may
  still click a toolbar button where a menu command exists; both are fine as long as verify passes.
- **Jev on region choices.** JevK5 hasn't been tested on region choices. If it's poor at them, keep drill for long
  views only (more than ~150 elements).
- **App contract.** New event fields (`menu` in plans, `outline` instead of `screen` in plan events) need
  `docs/APP_INTEGRATION.md` updates; the app ignores unknown fields.
