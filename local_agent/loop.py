"""The agent loop: observe -> plan (Qwen) -> select (Jev) -> act -> verify (Jev) -> feed back to Qwen."""

from __future__ import annotations

import re
import shlex
import time
from dataclasses import dataclass, field, replace
from urllib.parse import urlparse
from typing import Callable, Protocol

import httpx

from local_agent.act.browser import KNOWN_BROWSERS, BrowserError
from local_agent.ranking import pick_ranked, required_words, superlative
from local_agent.search_results import MAX_FOR_JEV, host_of, matches_name, organic_links, pick_by_host
from local_agent.forms import (SENSITIVE_NOTE, FormPlan, find_form, match_values, missing_required, norm,
                               sensitive_reason, wants_form)
from local_agent.outline import MAX_REPLANS
from local_agent.closing import chord_family, command_family, goal_allows, name_family
from local_agent.apps import installed_apps, opened_app, rewrite_open
from local_agent.config import Settings
from local_agent.dialogs import (
    is_discard_button,
    leave_buttons,
    leave_dialog,
    leave_line,
    in_dialog,
    alert_file,
    alert_buttons,
    dialog_elements,
    is_overwrite_button,
    name_sheet,
    overwrite_alert,
)
from local_agent.goal_facts import (
    Fact,
    content_words,
    progress as progress_facts,
    _action as action_for,
    check_facts,
    _holds,
    checkable_facts,
    is_browser,
    open_command,
    site_url,
    decisive_facts,
    pending_fact,
    rest_of_goal,
    search_field,
    describe_facts,
    failing_facts,
    parse_facts,
    progress_scope,
    quoted_strings,
)
from local_agent.history import NAVIGATION_KEYS, SCROLL_STUCK, StepRecord, loop_warning, repeated_cycle
from local_agent.observe.menus import find_exact, menu_scope
from local_agent.observe.models import SINGLE_LINE_ROLES, Snapshot, UIElement
from local_agent.planner.qwen import PlannerError
from local_agent.planner.schema import PlannerOutput
from local_agent.safety import always_ask
from local_agent.observe.screen_view import render, view_budget
from local_agent.selector.jev import TEXT_ROLES, JevError, Selection, filter_candidates, planner_pick


class Planner(Protocol):
    def next_step(
        self, goal: str, scope: dict, history: list[StepRecord], screen: str | None = None
    ) -> PlannerOutput: ...


class Selector(Protocol):
    def select(self, goal: str, snap: Snapshot, step: PlannerOutput) -> Selection: ...

    def verify(
        self, goal: str, step: PlannerOutput, element: UIElement | None, before: Snapshot, after: Snapshot
    ) -> float: ...


class ActOutcome(Protocol):
    """Satisfied by act.executor.ActionResult."""

    ok: bool
    note: str


@dataclass
class _Failed:
    note: str
    ok: bool = False


SECURE_NOTE = "typing into password fields is not allowed; the user must do this"


@dataclass
class Hooks:
    """Side-effecting dependencies, injectable for tests and --dry-run."""

    observe: Callable[[], Snapshot]
    execute: Callable[[PlannerOutput, UIElement | None], ActOutcome]
    run_terminal: Callable[[str], ActOutcome]
    # Ask the user about a terminal command the safety check didn't clear: (command, why) -> run it?
    confirm_terminal: Callable[[str, str], bool] = lambda cmd, why: False
    # Run an app menu command (observe/menus.py MenuCommand) by shortcut or AXPress.
    run_menu: Callable[[object], ActOutcome] = lambda cmd: _Failed("menu commands are not available")
    on_plan: Callable[[int, Snapshot, PlannerOutput], None] = lambda *a: None
    on_select: Callable[[Selection], None] = lambda *a: None
    on_record: Callable[[StepRecord], None] = lambda *a: None
    sleep: Callable[[float], None] = time.sleep
    # Web pages: wait for the page to settle (returns seconds waited), and click for real where a script click did nothing.
    settle_web: Callable[[], float] | None = None
    trusted_click: Callable[[UIElement], ActOutcome] | None = None
    # Navigate the front browser window by tab (act/browser.py Bridge: bundle_for, find_tab, select_tab, open_tab).
    browser: object | None = None
    # Reports the start of a phase of the current step ("observing", "planning", ...) to the event stream.
    on_stage: Callable[[int, str], None] = lambda n, stage: None
    # The outline made at the start (items, seconds, model load seconds) and a revision (reason, new items).
    on_outline: Callable[[list, float, float, str], None] = lambda items, seconds, load_s, rejected: None
    on_replan: Callable[[str, list, str], None] = lambda reason, items, rejected: None


@dataclass
class AgentResult:
    success: bool
    reason: str
    history: list[StepRecord] = field(default_factory=list)


class Agent:
    def __init__(self, settings: Settings, planner: Planner, selector: Selector, hooks: Hooks, dry_run: bool = False,
                 start_app: str | None = None, form_data: dict | None = None, outliner=None):  # fmt: skip
        self.form_data = form_data
        self.outliner = outliner  # local_agent/outline.py::Outliner; None: no outline, no replanning
        self.our_tabs: list = []  # tabs this run opened (the only ones it may navigate)
        self._own_steps: set = set()  # records of the agent's own navigation steps
        self._leave_asked = False
        self.outline_active, self.replans = False, 0
        self._item_failures: dict = {}
        self.s = settings
        self.planner = planner
        self.selector = selector
        self.hooks = hooks
        self.dry_run = dry_run
        # The app the user started the agent from (terminal, LocalAgent): while it's frontmost the planner is told to
        # open the app the goal needs instead of working in it.
        self.start_app = start_app
        self.last_safety: float | None = None  # Jev's safety probability for the current terminal command
        self.history: list[StepRecord] = []  # of the current run, so an interrupted run can still report it
        # Seconds spent in each model call of the current step ("planner_s", "select_s", "verify_s"); set before the
        # matching on_plan / on_select / on_record hook fires.
        self.timing: dict[str, float] = {}
        self.web: dict = {}
        self._goal = ""
        self.stage_step = 0
        self._ranked_why = ""
        self.rest: str | None = None
        self.screen: str | None = None  # screen view the planner got in the current step (None when view is off)
        self.apps: list[str] | None = None  # installed app names for goal facts (None: look them up)
        self.facts: list = []  # decisive facts of the current goal
        self.started = time.time()  # when the current run began, for files the goal says to save
        self._asked_overwrite: set = set()
        self._repeat_of: int | None = None  # set when no-change repeats of one step reach the limit
        self._last_repeat: tuple[int, int, int] | None = None
        self.known_facts: dict = {}  # last explicit True/False of each fact in this run
        self.seen_facts: set = set()  # starting-state facts that held earlier in this run
        self.goal = ""
        self.form_plan: FormPlan | None = None
        self._declined: str | None = None  # set when the user declines the submit: the run ends
        self._submitting = False
        self.checklist: list = []  # every fact of the goal, in goal order, for the planner's progress

    def run(self, goal: str) -> AgentResult:
        history: list[StepRecord] = []
        self.history = history
        failures = 0
        snap: Snapshot | None = None
        self.goal = goal
        self.facts = decisive_facts(goal, self.apps)
        self.checklist = parse_facts(goal, self.apps, progress=True)
        # Facts that can't end the run alone leave a rest the checklist must still name.
        self.rest = rest_of_goal(goal, self.checklist) if self.checklist and not self.facts else None
        self._goal = goal
        self.seen_facts, self.known_facts = set(), {}
        self._asked_overwrite: set = set()
        self._last_repeat, self._repeat_of = None, None
        self.started = time.time()
        n = 0  # records so far (planner steps and the agent's own: opening the goal's app)
        self._stage(1, "observing")
        snap = self.hooks.observe()
        self._tried_sites: set = set()
        self.navigated: set = set()  # sites code already went to (opened or selected)
        if not self.dry_run:
            snap, n = self._open_goal_app(goal, snap, history, n)
        if (stop := self._plan_form(goal, snap)) is not None:
            return stop
        self.outline_active, self.replans, self._item_failures = False, 0, {}
        if self.form_plan is None and not self.facts and self.outliner is not None and not self.dry_run:
            self._make_outline(goal, snap)
        while n < self.s.max_steps:
            if snap is None:
                self._stage(n + 1, "observing")
                snap = self.hooks.observe()
            snap, n = self._open_goal_site(goal, snap, history, n)
            snap, n, stop = self._handle_overwrite(goal, snap, history, n)
            if stop is not None:
                return stop
            snap, n, stop = self._handle_leave(goal, snap, history, n)
            if stop is not None:
                return stop
            n += 1
            self._repeat_of = None
            rec, snap = self._step(n, goal, snap, history)
            if rec is None:
                gate = checkable_facts(self.facts, snap)
                if gate and not check_facts(gate, snap, self.started, self.known_facts):
                    rec = StepRecord(n, "done", note=f"not done: {failing_facts(gate, snap, self.started, self.known_facts)}")
                    history.append(rec)
                    self.hooks.on_record(rec)
                    failures += 1
                    if failures >= self.s.max_consecutive_failures:
                        return AgentResult(False, f"{failures} consecutive failed steps", history)
                    continue
                return AgentResult(True, "planner reported the goal is done", history)
            history.append(rec)
            if self.outline_active and not self.dry_run and (stuck := self._track_outline(rec, snap, history)) is not None:
                self.hooks.on_record(rec)
                return stuck
            if self._declined:
                self.hooks.on_record(rec)
                return AgentResult(False, self._declined, history)
            stop = None if self.dry_run else (
                self._goal_check(goal, rec, snap) or self._no_progress(history) or self._loop_guard(history))
            self.hooks.on_record(rec)
            if stop is not None:
                return stop
            if self.dry_run:
                return AgentResult(rec.note.startswith("dry-run"), "dry run: stopped after one step", history)
            failures = 0 if rec.succeeded else failures + 1
            if failures >= self.s.max_consecutive_failures:
                return AgentResult(False, f"{failures} consecutive failed steps", history)
        return AgentResult(False, f"reached max steps ({self.s.max_steps})", history)

    def _handle_overwrite(self, goal: str, snap: Snapshot, history: list[StepRecord], n: int):
        """Our own save hit "already exists": ask the user once, then press Replace or Cancel ourselves. Only when the
        goal has a file name, an earlier step typed it, and the alert names it; any other alert is left to the planner
        (the progress line tells it what the alert asks)."""
        fact = self._saved_fact()
        if fact is None or not overwrite_alert(snap) or fact.value in self._asked_overwrite:
            return snap, n, None
        typed = any(h.succeeded and h.plan.startswith("type") and f"text={fact.value!r}" in h.plan for h in history)
        if not typed or fact.value.lower() not in " ".join(e.value.lower() for e in dialog_elements(snap)):
            return snap, n, None
        self._asked_overwrite.add(fact.value)
        buttons = [e for e in dialog_elements(snap) if e.role == "AXButton" and e.interactive]
        replace = next(b for b in buttons if is_overwrite_button(b))
        cancel = next((b for b in buttons if b.name().strip().lower() == "cancel"), None)
        question = f'replace existing "{fact.value}"'
        if self.dry_run:
            rec = StepRecord(n + 1, "overwrite", note=f"dry-run: would ask the user to {question}")
            history.append(rec)
            self.hooks.on_record(rec)
            return snap, n + 1, AgentResult(True, "dry run: stopped after one step", history)
        yes = self.hooks.confirm_terminal(question, "would overwrite an existing file")  # even in turbo
        button = replace if yes else cancel
        if button is not None:
            plan = PlannerOutput(done=False, reasoning="the save hit an existing file", action="click",
                                 target={"id": button.id, "role": "AXButton", "description": button.name()})  # fmt: skip
        else:  # no Cancel button on screen: escape dismisses the alert
            plan = PlannerOutput(done=False, reasoning="cancel the alert", action="shortcut", keys="escape")
        self.timing, self.screen = {}, None
        self.hooks.on_plan(n + 1, snap, plan)
        rec = StepRecord(n + 1, plan.summary(), element=button.describe() if button else None)
        out = self.hooks.execute(plan, button)
        rec.executed, rec.verified = out.ok, out.ok
        rec.note = (f"overwrite confirmed by user; {out.note}" if yes
                    else f'overwrite declined: "{fact.value}" already exists; {out.note}')  # fmt: skip
        self.hooks.sleep(self.s.settle_s)
        history.append(rec)
        after = self.hooks.observe()
        stop = None
        if yes:
            stop = self._goal_check(goal, rec, after)
        else:
            stop = AgentResult(False, "overwrite_declined", history)
        self.hooks.on_record(rec)
        return after, n + 1, stop

    def _handle_leave(self, goal: str, snap: Snapshot, history: list[StepRecord], n: int):
        """Our own navigation raised "Changes you made may not be saved": ask the user once, then press Leave or Cancel
        ourselves. A dialog the planner caused is left to the planner (the guard asks before it discards anything)."""
        if not leave_dialog(snap):
            self._leave_asked = False
            return snap, n, None
        if self._leave_asked or not history or history[-1].step not in self._own_steps:
            return snap, n, None
        self._leave_asked = True
        leave, stay = leave_buttons(snap)
        if leave is None:
            return snap, n, None
        if self.dry_run:
            rec = StepRecord(n + 1, "leave", note="dry-run: would ask the user to leave the page and lose unsaved changes")
            history.append(rec)
            self.hooks.on_record(rec)
            return snap, n + 1, AgentResult(True, "dry run: stopped after one step", history)
        yes = self.hooks.confirm_terminal("leave the page and lose unsaved changes", "would discard unsaved changes")
        button = leave if yes else stay
        if button is not None:
            plan = PlannerOutput(done=False, reasoning="our navigation hit a leave dialog", action="click",
                                 target={"id": button.id, "role": "AXButton", "description": button.name()})  # fmt: skip
        else:
            plan = PlannerOutput(done=False, reasoning="stay on the page", action="shortcut", keys="escape")
        self.timing, self.screen = {}, None
        self.hooks.on_plan(n + 1, snap, plan)
        rec = StepRecord(n + 1, plan.summary(), element=button.describe() if button else None)
        out = self.hooks.execute(plan, button)
        rec.executed, rec.verified = out.ok, out.ok
        rec.note = ("leave confirmed by user; " if yes else "leave declined; stayed on the page; ") + out.note
        self.hooks.sleep(self.s.settle_s)
        history.append(rec)
        after = self.hooks.observe()
        self.hooks.on_record(rec)
        return after, n + 1, (None if yes else AgentResult(False, "leave_declined", history))

    def _open_goal_app(self, goal: str, snap: Snapshot, history: list[StepRecord], n: int):
        """Step 1 in code: when the goal names an app that isn't in front, `open -a` it (through the safety path)."""
        wanted = next((f.value for f in parse_facts(goal, self.apps) if f.kind == "app"), None)
        if wanted is None or snap.app.lower() == wanted.lower() or self.s.max_steps < 1:
            return snap, n
        plan = PlannerOutput(done=False, reasoning=f"the goal names {wanted}", action="terminal",
                             command=f'open -a "{wanted}"')  # fmt: skip
        self.timing, self.screen = {}, None
        self.hooks.on_plan(n + 1, snap, plan)
        rec, new = self._terminal_step(goal, plan, snap, history, StepRecord(n + 1, plan.summary()))
        rec.note = f"opened by the agent (goal names {wanted}); {rec.note}"
        history.append(rec)
        self.hooks.on_record(rec)
        return new or self.hooks.observe(), n + 1

    def _open_goal_site(self, goal: str, snap: Snapshot, history: list[StepRecord], n: int):
        """When the next checklist item is a site we aren't on, go there in code (once per site per run): select the
        browser's existing tab on it, else open ONE new tab in the front window (no `open`, no new windows). Without
        a scriptable browser, or if that errors, fall back to `open -a <Browser> <url>` once. The fact is then checked
        on the page that loads; a host we were redirected to (our own navigation, no planner step in between) counts
        as reaching the site."""
        fact = pending_fact(self.checklist, snap, self.seen_facts, self.started, self.known_facts) if self.checklist else None
        if fact is None or fact.kind != "site" or fact in self._tried_sites or n >= self.s.max_steps:
            return snap, n
        self._tried_sites.add(fact)
        self.navigated.add(fact)
        bridge = self.hooks.browser
        app = next((f.value for f in self.checklist if f.kind == "app" and is_browser(f.value)), snap.app)
        bundle = bridge.bundle_for(app) if bridge is not None else None
        why = ""
        if bridge is not None and bundle:
            done = self._navigate_tab(goal, fact, bridge, bundle, snap, history, n)
            if isinstance(done, tuple):
                return done
            why = done  # the reason navigation failed
        if "." not in fact.value:  # no `<name>.com` guessing: without scripting the planner follows the checklist
            rec = StepRecord(n + 1, f"search for {fact.value}", note="opened by the agent (goal names site "
                             f"{fact.value}); not done: {why or 'no scriptable browser'}; search for it in the address bar")
            history.append(rec)
            self.hooks.on_record(rec)
            return snap, n + 1
        return self._open_site_by_command(goal, fact, snap, history, n, why)

    def _search_site(self, goal, fact, bridge, bundle, snap, history, n):
        """Search for the name in a new tab (the browser's own engine), read the results page and go to the official
        result by pointing the same tab at it; ask the user when its host doesn't mention the name."""
        name = KNOWN_BROWSERS.get(bundle, "the browser")
        desc = f'search for "{fact.value}" in a new {name} tab'
        plan = PlannerOutput(done=False, reasoning=f"the goal names the site {fact.value}", action="terminal",
                             command=f"({desc})")  # fmt: skip
        self.timing, self.screen = {}, None
        self.hooks.on_plan(n + 1, snap, plan)
        rec = StepRecord(n + 1, plan.summary())
        note = f"opened by the agent (goal names site {fact.value}); "
        ask = None if self.dry_run else self._needs_approval(goal, plan, snap, history, rec)
        if self.dry_run:
            rec.note = note + f"dry-run: would {desc}"
        elif ask is not None and not self.hooks.confirm_terminal(desc, ask):
            rec.note = note + f"user declined ({ask})"
        else:
            return self._search_and_pick(goal, fact, bridge, bundle, snap, history, n, rec, note + desc)
        history.append(rec)
        self.hooks.on_record(rec)
        return snap, n + 1

    def _search_and_pick(self, goal, fact, bridge, bundle, snap, history, n, rec, note):
        self._stage(n + 1, "searching")
        try:
            outcome = bridge.search_in_tab(bundle, fact.value, stage=lambda name: self._stage(n + 1, name))
        except BrowserError as e:
            return f"search failed ({e.kind}: {e})"
        search_tab = getattr(outcome, "tab", None)
        if search_tab is not None:
            self.our_tabs.append(search_tab)
        self._own_steps.add(rec.step)
        rec.executed, rec.verified = True, True
        after = self._wait_for_results(snap)
        links = organic_links(after)
        chosen = pick_by_host(links, fact.value)
        if chosen is None and links:
            chosen = self._ask_jev_for_site(goal, fact.value, after, links[:MAX_FOR_JEV])
        host = host_of(chosen.href) if chosen is not None else ""
        if chosen is None:
            rec.note = note + "; the results page has no usable result link"
        elif not matches_name(chosen, fact.value) and not self.hooks.confirm_terminal(
                f'open {host} for "{fact.value}"', "search result does not match the name"):  # asks even in turbo
            rec.note = note + f"; user declined {host} for \"{fact.value}\""
        else:
            self._stage(n + 1, "opening_result")
            try:
                if self._search_tab_is_active(bridge, bundle, search_tab):
                    bridge.navigate_tab(bundle, chosen.href)  # our own tab, still in front
                else:  # never point a tab we didn't open somewhere else: open the result in a new one
                    opened = bridge.open_tab(bundle, chosen.href)
                    if opened is not None:
                        self.our_tabs.append(opened)
            except BrowserError as e:
                rec.note = note + f"; could not open the result ({e.kind}: {e})"
            else:
                self._alias_site(fact, host)
                self.known_facts[fact] = True
                rec.note = note + f"; opened the result {host}"
                self._stage(n + 1, "observing")
                after = self.hooks.observe()
        history.append(rec)
        self.hooks.on_record(rec)
        return after, n + 1

    def _search_tab_is_active(self, bridge, bundle, search_tab) -> bool:
        if search_tab is None or not self._is_ours(search_tab):
            return False
        active = getattr(bridge, "active_tab", None)
        if active is None:
            return True
        try:
            now = active(bundle)
        except BrowserError:
            return False
        return now is None or (self._is_ours(now) and (not now.id or now.id == search_tab.id))

    def _wait_for_results(self, snap: Snapshot) -> Snapshot:
        """Observe until a page other than the one we started on has links to read (the results load asynchronously)."""
        self._stage(self.stage_step, "waiting_for_page")
        after = snap
        for _ in range(6):
            self.hooks.sleep(self.s.settle_s)
            after = self.hooks.observe()
            if after.url and after.url != snap.url and organic_links(after):
                break
        return after

    def _ask_jev_for_site(self, goal: str, name: str, snap: Snapshot, links: list):
        choose = getattr(self.selector, "choose", None)
        if choose is None:
            return links[0]
        step = PlannerOutput(done=False, reasoning="r", action="click",
                             target={"role": "AXLink", "description": f"the official site for {name}"})  # fmt: skip
        try:
            return choose(goal, snap, step, links).element
        except (JevError, httpx.HTTPError):
            return None

    def _navigate_tab(self, goal, fact, bridge, bundle, snap, history, n):
        """(snapshot, n) when the tab route ran (or was declined / dry-run); else a reason string to fall back."""
        name = KNOWN_BROWSERS.get(bundle, "the browser")
        url, before = site_url(fact.value), urlparse(snap.url).hostname or ""
        try:
            tab = bridge.find_tab(bundle, fact.value)
        except BrowserError as e:
            return f"tab navigation unavailable ({e.kind}: {e})"
        if tab is None and "." not in fact.value:  # a bare name: search for it, then pick the official result
            return self._search_site(goal, fact, bridge, bundle, snap, history, n)
        desc = (f"switch to the existing {fact.value} tab" if tab is not None
                else f"open {urlparse(url).hostname} in a new {name} tab")  # fmt: skip
        plan = PlannerOutput(done=False, reasoning=f"the goal names the site {fact.value}", action="terminal",
                             command=f"({desc})")  # fmt: skip
        self.timing, self.screen = {}, None
        self.hooks.on_plan(n + 1, snap, plan)
        rec = StepRecord(n + 1, plan.summary())
        note = f"opened by the agent (goal names site {fact.value}); "
        if self.dry_run:
            rec.note = note + f"dry-run: would {desc}"
        elif (ask := self._needs_approval(goal, plan, snap, history, rec)) is not None and not self.hooks.confirm_terminal(
                desc, ask):
            rec.note = note + f"user declined ({ask})"
        else:
            seen = None
            self._stage(n + 1, "selecting_tab" if tab is not None else "opening_tab")
            try:
                if tab is not None:
                    bridge.select_tab(bundle, tab)
                    seen = tab
                else:
                    seen = bridge.open_tab(bundle, url, stage=lambda name: self._stage(n + 1, name))
            except BrowserError as e:
                return f"tab navigation failed ({e.kind}: {e})"
            if tab is None and seen is not None:
                self.our_tabs.append(seen)  # a tab we opened: ours to navigate later
            self._own_steps.add(rec.step)
            rec.executed, rec.verified = True, True
            rec.note = note + desc
            self._stage(n + 1, "observing")
            after = self.hooks.observe()
            after = self._check_site(fact, rec, after, before, url, seen.url if seen is not None else None)
            history.append(rec)
            self.hooks.on_record(rec)
            return after, n + 1
        history.append(rec)
        self.hooks.on_record(rec)
        return snap, n + 1

    def _open_site_by_command(self, goal, fact, snap, history, n, why: str = ""):
        url, before = site_url(fact.value), urlparse(snap.url).hostname or ""
        plan = PlannerOutput(done=False, reasoning=f"the goal names the site {fact.value}", action="terminal",
                             command=open_command(fact, self.checklist))  # fmt: skip
        self.timing, self.screen = {}, None
        self.hooks.on_plan(n + 1, snap, plan)
        rec, new = self._terminal_step(goal, plan, snap, history, StepRecord(n + 1, plan.summary()), by_agent=True)
        rec.note = f"opened by the agent (goal names site {fact.value}); " + (why + "; " if why else "") + rec.note
        if not rec.executed:  # dry run, declined or refused: the planner carries on
            history.append(rec)
            self.hooks.on_record(rec)
            return new or snap, n + 1
        after = new or self.hooks.observe()
        after = self._check_site(fact, rec, after, before, url)
        self._own_steps.add(rec.step)
        history.append(rec)
        self.hooks.on_record(rec)
        return after, n + 1

    def _check_site(self, fact, rec: StepRecord, after: Snapshot, before: str, url: str,
                    seen_url: str | None = None) -> Snapshot:
        """Did the navigation land on the site? `seen_url` is the tab's address as the browser reported it (no need to
        wait for the page to be read again); without it the page is observed again, up to three times."""
        if seen_url:
            probe = Snapshot(after.app, after.bundle_id, after.pid, after.window_title, [], url=seen_url)
            holds, host = _holds(fact, probe) is True, urlparse(seen_url).hostname or ""
        else:
            for _ in range(3):  # the page may still be loading: wait for the address to change or the site to hold
                if _holds(fact, after) or (urlparse(after.url).hostname or "") not in ("", before):
                    break
                self.hooks.sleep(self.s.settle_s)
                after = self.hooks.observe()
            host = urlparse(after.url).hostname or ""
            holds = _holds(fact, after) is True
        if holds:
            self.known_facts[fact] = True
        elif host and host != before:  # redirected by the site itself, e.g. gmail.com -> mail.google.com
            self._alias_site(fact, host)
            self.known_facts[fact] = True
            rec.note += f"; reached via redirect: {host}"
        elif host:
            rec.note += f"; opened {url}, but the tab is on {host}; the site check doesn't hold"
        else:
            rec.note += f"; opened {url}; the page's address is unknown, so the site check can't run"
        return after

    def _ranked_pick(self, plan: PlannerOutput, snap: Snapshot):
        """A click toward "the latest / oldest / first ... <thing>" goal item, picked by dates or position in code."""
        if plan.action not in ("click", "double_click") or not self.checklist:
            return None
        fact = pending_fact(self.checklist, snap, self.seen_facts, self.started, self.known_facts)
        if fact is None or fact.kind != "click" or superlative(fact.value) is None:
            return None
        said = f"{plan.target.description if plan.target else ''}"
        if said.strip() and not (required_words(fact.value) & content_words(said)) and not superlative(said):
            return None  # the planner is clicking something else (Home, a menu...)
        element, why = pick_ranked(fact.value, filter_candidates(snap.elements, plan))
        if element is None:
            return None
        self._ranked_why = why
        return Selection(element, 1.0, 1.0, len(snap.elements), 0, source="ranked")

    @staticmethod
    def _clicked_for(fact: Fact, plan: PlannerOutput, element: UIElement) -> bool:
        """Was this click chosen for the fact's object? The planner's description or the chosen element's text shares a
        content word with it (latest / first / button... don't count)."""
        wanted = content_words(fact.value)
        said = f"{plan.target.description if plan.target else ''} {element.name()} {element.context}"
        return bool(wanted & content_words(said))

    def _is_ours(self, tab) -> bool:
        """Did this run open `tab`? By id where the browser has ids, else by window and position."""
        if tab is None:
            return False
        return any((t.id and t.id == tab.id) or (not t.id and (t.window, t.index) == (tab.window, tab.index))
                   for t in self.our_tabs)  # fmt: skip

    def _user_tab_address_bar(self, element, snap: Snapshot) -> bool:
        """Typing into the browser's address field while the active tab is not one we opened would navigate the user's own
        tab (it can raise "Changes you made may not be saved"). Needs the browser bridge to know the active tab."""
        from local_agent.act.browser import KNOWN_BROWSERS

        bridge = self.hooks.browser
        target = element or next((e for e in snap.elements if e.focused), None)
        if bridge is None or target is None or snap.bundle_id not in KNOWN_BROWSERS or "AXWebArea" in target.path:
            return False
        if target.role not in ("AXTextField", "AXComboBox") or not re.search(
                r"address|url|location|command bar|omnibox|search or enter", f"{target.name()} {target.placeholder}", re.I):  # fmt: skip
            return False
        try:
            return not self._is_ours(bridge.active_tab(snap.bundle_id))
        except BrowserError:
            return True  # can't tell whose tab it is: don't type into it

    def _reopens_site(self, cmd: str):
        """The navigated site fact that an `open …` command would open again."""
        if not self.navigated or not re.match(r"\s*open\b", cmd):
            return None
        for m in re.finditer(r"(?:https?://)?([\w-]+(?:\.[\w-]+)+)", cmd):
            for fact in self.navigated:
                if _holds(fact, Snapshot("", "", 0, "", [], url=f"https://{m.group(1)}/")) is True:
                    return fact
        return None

    def _alias_site(self, fact, host: str) -> None:
        """Remember the host a site fact was redirected to, so later checks accept it (Fact equality ignores it)."""
        swap = lambda facts: [replace(f, alias=host) if f == fact else f for f in facts]  # noqa: E731
        self.checklist, self.facts = swap(self.checklist), swap(self.facts)
        self.known_facts = {(replace(f, alias=host) if f == fact else f): v for f, v in self.known_facts.items()}

    def _scope(self, snap: Snapshot) -> dict:
        scope = snap.scope()
        if snap.menu_commands and self.s.menus_on():
            scope["menu_commands"] = menu_scope(snap.menu_commands, self.goal)
        if self.checklist:
            scope["progress"] = self._progress(snap)
            self._name_the_file(scope["progress"], snap)
            if self._collapse_for(snap):
                scope["window"] = ""  # a browser window is titled after the page we are leaving
        if leave_dialog(snap):
            progress = scope.setdefault("progress", {"done": [], "next": None, "remaining": []})
            if progress.get("next"):
                progress["remaining"] = [progress["next"], *progress.get("remaining", [])]
            progress["next"] = leave_line(snap)
        if self.start_app and snap.app == self.start_app:
            scope["note"] = (
                f"{snap.app} is where the user started the agent, not the app to work in. First open the app the goal "
                'needs with a terminal step: open -a "App Name" (or open https://… for a website).'
            )
        return scope

    def _progress(self, snap: Snapshot) -> dict:
        return progress_scope(self.checklist, snap, self.seen_facts, self.started, self.known_facts, self.rest)

    def _collapse_for(self, snap: Snapshot) -> str | None:
        """The site to reach when that is the next item and we aren't there: the current page is then not shown."""
        pending = pending_fact(self.checklist, snap, self.seen_facts, self.started, self.known_facts) if self.checklist else None
        return pending.value if pending is not None and pending.kind == "site" else None

    def _needs_approval(
        self, goal: str, plan: PlannerOutput, snap: Snapshot, history: list[StepRecord], rec: StepRecord
    ) -> str | None:
        """Why the user must OK this terminal command, or None to run it now (turbo, or cleared by the safety
        check: not on safety.ALWAYS_ASK and Jev rates it safe)."""
        self.last_safety = None
        if self.s.turbo:
            return None
        cmd = plan.command or ""
        if why := always_ask(cmd):
            return why
        check = getattr(self.selector, "command_safety", None)
        if check is None:
            return "no safety check available"
        t0 = time.monotonic()
        try:
            p = check(goal, plan, snap, history)
        except (JevError, httpx.HTTPError) as e:
            return f"safety check failed: {e}"[:200]
        finally:
            self.timing["safety_s"] = time.monotonic() - t0
        if p is None:
            return "this Jev model can't judge command safety"
        rec.safety_probability = self.last_safety = p
        if p >= self.s.safety_threshold:
            return None
        return f"Jev rated it safe with p={p:.2f} only (needs {self.s.safety_threshold:.2f})"

    def _goal_check(self, goal: str, rec: StepRecord, snap: Snapshot | None) -> AgentResult | None:
        """After a successful step, first check the facts the goal states (goal_facts); all hold: success. Else ask Jev whether the screen now shows the goal done. High: the run ends as a
        success. Middling: the step's note tells the planner it may be done. Backends that can't judge return None."""
        if rec.succeeded and snap is not None and check_facts(self.facts, snap, self.started, self.known_facts):
            return AgentResult(True, f"goal facts hold: {describe_facts(self.facts)}", self.history)
        check = getattr(self.selector, "goal_done", None)
        if not (self.s.goal_check and check and rec.succeeded and snap is not None):
            return None  # terminal steps re-observe next step; their goals usually end in an answer from the planner
        t0 = time.monotonic()
        try:
            p = check(goal, snap)
        except (JevError, httpx.HTTPError) as e:
            rec.note += f"; goal check error: {e}"[:200]
            return None
        finally:
            self.timing["goal_s"] = time.monotonic() - t0
        if p is None:
            return None
        rec.goal_probability = p
        if p >= self.s.goal_done_threshold:
            return AgentResult(True, f"Jev: the screen shows the goal done (p={p:.2f})", self.history)
        if p >= self.s.goal_hint_threshold:
            rec.note += (f"; Jev thinks the screen may already show the goal done (p={p:.2f}): "
                         "if SCREEN confirms it, answer done")  # fmt: skip
        return None

    def _no_progress(self, history: list[StepRecord]) -> AgentResult | None:
        if self._repeat_of is None:
            return None
        recent = history[-self.s.max_repeated_successes:]
        likely = any((h.goal_probability or 0) >= self.s.goal_hint_threshold for h in recent)
        reason = f"no progress: repeating step {self._repeat_of}"
        return AgentResult(likely, reason + ("; Jev rated the goal likely done" if likely else ""), history)

    def _loop_guard(self, history: list[StepRecord]) -> AgentResult | None:
        """Stop when completed work keeps being redone (the planner was warned one repeat earlier)."""
        block, times = repeated_cycle(history)
        if times < self.s.max_repeated_successes:
            return None
        recent = history[-times * len(block):]
        likely = any((h.goal_probability or 0) >= self.s.goal_hint_threshold for h in recent)
        what = repr(block[0]) if len(block) == 1 else f"{len(block)} steps"
        reason = f"stopped: {what} succeeded {times} times in a row"
        return AgentResult(likely, reason + ("; Jev rated the goal likely done" if likely else ""), history)

    def _step(
        self, n: int, goal: str, snap: Snapshot, history: list[StepRecord]
    ) -> tuple[StepRecord | None, Snapshot | None]:
        """Run one iteration. Returns (record, snapshot to reuse next time) or (None, _) when done."""
        self.timing = {}
        self.web = snap.web  # {ms, candidates, listed, more} of the page read, {"error": …} or {}
        if snap.web.get("ms") is not None:
            self.timing["web_s"] = snap.web["ms"] / 1000
        screen = None
        if self.s.planner_view in ("ids", "names"):
            budget = view_budget(self.s.planner_num_ctx, self.s.planner_view_tokens)
            screen = render(snap, budget, numbered=self.s.planner_view == "ids", collapse_page=self._collapse_for(snap))
        self.screen = screen
        self._stage(n, "planning")
        t0 = time.monotonic()
        try:
            extra = {"snap": snap} if getattr(self.planner, "needs_snapshot", False) else {}
            plan = self.planner.next_step(goal, self._scope(snap), history, screen, **extra)
        except (PlannerError, httpx.HTTPError) as e:
            return StepRecord(n, "planner error", note=str(e)[:300]), snap
        finally:
            self.timing["planner_s"] = time.monotonic() - t0
        self.hooks.on_plan(n, snap, plan)
        if plan.done:
            return None, snap
        rec = StepRecord(n, plan.summary())
        quoted = quoted_strings(goal)
        if plan.action == "type" and quoted and (plan.text or "").strip().lower() not in {q.lower() for q in quoted}:
            rec.note = ("the goal's text is one of: " + ", ".join(f'"{q}"' for q in quoted)
                        + "; type one of them exactly")  # fmt: skip
            return rec, snap

        if plan.action == "terminal":
            return self._terminal_step(goal, plan, snap, history, rec)

        menu_cmd = None
        if plan.action == "menu":
            menu_cmd = self._resolve_menu(goal, plan, snap)
            if menu_cmd is None:
                rec.note = f'no enabled menu command "{plan.command}"; use a path from menu_commands'
                return rec, snap
            rec.element = menu_cmd.path

        element, pick, ranked_note = None, None, ""
        if plan.needs_jev():
            pick = planner_pick(snap, plan)
            sel = pick.selection
            ranked = self._ranked_pick(plan, snap)
            if ranked is not None:
                sel = ranked  # decided in code from the dates / positions on screen: no model
            if sel is None and pick.overruled is None and plan.target is not None and not plan.target.description.strip():
                rec.note = f"element [{plan.target.id}] is not on the current screen; use a number from SCREEN"
                return rec, snap
            self._stage(n, "selecting")
            t0 = time.monotonic()
            try:
                sel = sel or self.selector.select(goal, snap, plan)
            except (JevError, httpx.HTTPError) as e:
                rec.note = f"selector error: {e}"[:300]
                return rec, snap
            finally:
                self.timing["select_s"] = time.monotonic() - t0
            sel.overrode_id = pick.overruled
            self.hooks.on_select(sel)
            element = sel.element
            rec.element, rec.probability, rec.confidence = element.describe(), sel.probability, sel.confidence
            if sel.source == "ranked":
                ranked_note = f"ranked in code: {self._ranked_why}"
                rec.note = ranked_note
            elif sel.source == "planner":
                rec.note = "picked by number from the screen view"
            elif pick.note:
                rec.note = pick.note
            if sel.probability < self.s.threshold:
                rec.note = (
                    f"skipped: best match probability {sel.probability:.2f} below threshold {self.s.threshold:.2f} "
                    f"({sel.candidates} candidates); describe the target differently or use another action"
                )
                return rec, snap

        kind, text = self._guard(plan, element, menu_cmd, snap)
        if kind == "block":
            rec.note = text
            return rec, snap
        if kind == "leave":  # discards the user's unsaved work: always asked, turbo or not
            if not self.dry_run and not self.hooks.confirm_terminal("leave the page and lose unsaved changes", text):
                rec.note = "user declined: leave the page and lose unsaved changes"
                return rec, snap
        if kind == "submit":  # sends the user's data: always asked, turbo or not
            if not self.dry_run and not self.hooks.confirm_terminal(text, "sends your data"):
                rec.note = f"user declined: {text}"
                self._declined = "declined: submit the form"
                return rec, snap
            self._submitting = True
        if kind == "ask" and not self.dry_run and not self.hooks.confirm_terminal(plan.summary(), text):
            rec.note = f"user declined: {text}"  # turbo does not skip this: it isn't a terminal command
            return rec, snap

        if self.dry_run:
            rec.note = "dry-run: action not executed"
            return rec, snap

        if plan.action == "type" and plan.text:
            field = element or next((e for e in snap.elements if e.focused and e.role in TEXT_ROLES), None)
            same = field is not None and (
                field.value == plan.text if field.role in SINGLE_LINE_ROLES else plan.text.lower() in field.value.lower()
            )
            if same:
                rec.executed, rec.verified = True, True
                rec.note = f'already typed: the field contains "{plan.text}"; not typed again'
                return rec, snap

        self._stage(n, "acting")
        out = self.hooks.run_menu(menu_cmd) if menu_cmd else self.hooks.execute(plan, element)
        rec.executed, rec.note = out.ok, out.note
        if ranked_note:
            rec.note = f"{ranked_note}; {out.note}"
        elif pick is not None and pick.note:
            rec.note = f"{pick.note}; {out.note}"  # tell the planner its number was overruled
        if not out.ok:
            return rec, None
        web = element is not None and bool(element.web_id) and self.hooks.settle_web is not None
        self._settle(web)
        self._stage(n, "observing")
        after = self.hooks.observe()
        self._stage(n, "verifying")
        self._verify(goal, plan, element, snap, after, rec)
        changed = self._changed(snap, after)
        if web and plan.action == "click" and not changed and self.hooks.trusted_click is not None:
            # A script click that changed nothing may have been ignored by the page: click for real, once.
            again = self.hooks.trusted_click(element)
            rec.note += f"; the script click changed nothing; trusted click: {again.note}"
            if again.ok:
                self._settle(web)
                after = self.hooks.observe()
                self._verify(goal, plan, element, snap, after, rec)
                changed = self._changed(snap, after)
        if changed and rec.executed and rec.verified is not False and self._is_new_thing_step(
                element, menu_cmd, self._keys(plan, menu_cmd)):
            for f in self.checklist:  # a New click that changed the screen: the new thing is open
                if f.kind == "new":
                    self.known_facts[f] = True
                    self.seen_facts.add(f)
        if changed and rec.executed and rec.verified and plan.action in ("click", "double_click") and element is not None:
            pending = pending_fact(self.checklist, snap, self.seen_facts, self.started, self.known_facts)
            if pending is not None and pending.kind == "click" and self._clicked_for(pending, plan, element):
                self.known_facts[pending] = True  # a verified click chosen for the goal's object
        if self._submitting and changed and after.url and after.url != snap.url:
            for f in self.checklist:
                if f.kind == "submit":
                    self.known_facts[f] = True  # the page moved on after the submit click
        self._submitting = False
        if changed and rec.executed and rec.verified is not False:
            for f in self.checklist:
                if f.kind == "search" and self._submitted(plan, element, snap, f.value):
                    self.known_facts[f] = True  # pages that keep the query out of the URL
        self._note_repeat(rec, plan, history, changed)
        return rec, after

    def _settle(self, web: bool) -> None:
        if web:
            t0 = time.monotonic()
            self.hooks.settle_web()
            self.timing["settle_s"] = time.monotonic() - t0
        else:
            self.hooks.sleep(self.s.settle_s)

    def _make_outline(self, goal: str, snap: Snapshot) -> None:
        """Ask the planner model once for 3-8 steps; each becomes a checklist item (outline.items_from_steps)."""
        from local_agent.outline import items_from_steps, single_action, validate_outline

        if single_action(goal, self.checklist):
            return  # one short action on an app or site: no outline needed
        context = ", ".join(f"{f.kind}: {f.value}" for f in self.checklist if f.kind in ("app", "site"))
        try:
            steps = self.outliner.outline(goal, context)
        except (httpx.HTTPError, KeyError, ValueError):
            return
        if not steps:
            return
        done = {f.value.lower() for f, ok in progress_facts(self.checklist, snap, self.started, self.known_facts) if ok}
        cleaned, rejected = validate_outline(steps, goal, self.apps, done)
        if rejected:  # no outline: the planner follows the goal's own facts
            self.hooks.on_outline([], self.outliner.last_seconds, self.outliner.last_load_s, rejected)
            return
        items = items_from_steps(cleaned, self.apps)
        apps = [f for f in self.checklist if f.kind == "app" and f not in items]
        self.checklist = apps + items
        self.rest, self.outline_active = None, True
        self.hooks.on_outline(cleaned, self.outliner.last_seconds, self.outliner.last_load_s, "")

    def _track_outline(self, rec: StepRecord, snap: Snapshot, history: list[StepRecord]):
        """After a step: judge the pending outline item, count failures per item, and replan when stuck (2 failures on
        one item, a loop warning, or jev mode finding no listed action). None, or the run's result when it is stuck."""
        from local_agent.outline import plan_matches
        from local_agent.planner.jev_only import NO_SHELL

        item = pending_fact(self.checklist, snap, self.seen_facts, self.started, self.known_facts)
        if item is None:
            return None
        if rec.succeeded:
            self._item_failures[item] = 0
            if item.kind == "outline":
                judge = getattr(self.selector, "goal_done", None)
                try:
                    p = judge(item.value, snap) if judge and self.s.goal_check else None
                except (JevError, httpx.HTTPError):
                    p = None
                if (p is not None and p >= self.s.goal_done_threshold) or (p is None and rec.verified and plan_matches(item.value, rec)):
                    self.known_facts[item] = True  # without a judge: a verified step that names the item (simple, and coarse)
        else:
            self._item_failures[item] = self._item_failures.get(item, 0) + 1
        reason = None
        if NO_SHELL in rec.note:
            reason = "jev found no listed action"
        elif self._item_failures.get(item, 0) >= 2:
            reason = "two failed steps on one item"
        elif loop_warning(history):
            reason = "loop warning"
        if reason is None:
            return None
        return self._replan(reason, item, history, snap)

    def _replan(self, reason: str, item: Fact, history: list[StepRecord], snap: Snapshot):
        from local_agent.outline import items_from_steps, validate_outline
        from local_agent.planner.choice import history_lines

        if self.replans >= MAX_REPLANS:
            return AgentResult(False, f"stuck: {item.value}", history)
        self.replans += 1
        done = [f for f, ok in progress_facts(self.checklist, snap, self.started, self.known_facts) if ok]
        try:
            steps = self.outliner.replan(self.goal, [action_for(f, self.checklist, snap) for f in done], action_for(item, self.checklist, snap),
                                         history_lines(history[-3:]))
        except (httpx.HTTPError, KeyError, ValueError):
            steps = []
        self._item_failures = {}
        cleaned, rejected = validate_outline(steps, self.goal, self.apps, {f.value.lower() for f in done}, min_items=1)
        if steps and rejected:  # keep the remaining items as they were
            self.hooks.on_replan(reason, [], rejected)
        elif cleaned:
            new = [f for f in items_from_steps(cleaned, self.apps) if f not in done]
            self.checklist = [f for f in self.checklist if f in done] + new
            self.hooks.on_replan(reason, cleaned, "")
        return None

    def _plan_form(self, goal: str, snap: Snapshot) -> AgentResult | None:
        """A fill-in goal with a form on screen: the fields become the checklist (values from the goal's labelled quotes
        and --data); a required field without a value ends the run as needs_input."""
        self.form_plan = None
        if not wants_form(goal) or (form := find_form(snap)) is None:
            return None
        values = match_values(form.fields, goal, self.form_data, pick=self._pick_field)
        missing = missing_required(form, values)
        if missing:
            return AgentResult(False, "needs_input: " + ", ".join(missing), self.history)
        facts = [Fact("field", f.label, i, data=values[f.label], meta=f.kind)
                 for i, f in enumerate(form.fields) if f.label in values]  # fmt: skip
        if form.submit is not None:
            facts.append(Fact("submit", form.submit.name(), 10_000, data=form.submit.name()))
        self.form_plan = FormPlan(form, values)
        apps = [f for f in self.checklist if f.kind == "app"]
        self.checklist = apps + facts
        self.facts = apps + facts
        self.rest = None
        return None

    def _pick_field(self, described: str, labels: list[str]) -> str | None:
        """Jev's choice of the field a value belongs to, when no label matches by name."""
        pick = getattr(self.selector, "pick_option", None)
        if pick is None or not labels:
            return None
        options = {f"o{i}": label for i, label in enumerate(labels)}
        try:
            answer = pick(f"A value for a form: {described}", "Which form field does this value belong to?", options)
        except (JevError, httpx.HTTPError):
            return None
        return options.get(answer.choice) if answer is not None and answer.probability >= 0.5 else None

    def _stage(self, n: int, name: str) -> None:
        self.stage_step = n
        self.hooks.on_stage(n, name)

    def _verify(self, goal, plan, element, before, after, rec) -> None:
        t0 = time.monotonic()
        try:
            p = self.selector.verify(goal, plan, element, before, after)
            rec.verify_probability, rec.verified = p, p >= self.s.verify_threshold
        except (JevError, httpx.HTTPError) as e:
            rec.note += f"; verify error: {e}"[:200]
        finally:
            self.timing["verify_s"] = time.monotonic() - t0

    @staticmethod
    def _submitted(plan: PlannerOutput, element: UIElement | None, before: Snapshot, query: str) -> bool:
        """return (or a click on a Search/Go button) while the page's search box held the query."""
        field = search_field(before)
        if field is None or field.value.strip().lower() != query.lower():
            return False
        if plan.action == "shortcut":
            return (plan.keys or "").lower() in ("return", "enter")
        return plan.action == "click" and element is not None and bool(re.search(r"search|go|find", element.name(), re.I))

    def _note_repeat(self, rec: StepRecord, plan: PlannerOutput, history: list[StepRecord], changed: bool) -> None:
        """A step that repeats one that already worked and changed nothing is not a failure: tell the planner."""
        self._repeat_of = None
        exempt = plan.action == "scroll" or (plan.action == "shortcut" and (plan.keys or "").lower() in NAVIGATION_KEYS)
        if plan.action == "scroll" and not changed and rec.executed:
            rec.note += f"; {SCROLL_STUCK}"  # four of these in a row count as a repeat (history.stuck_scroll)
        if changed or exempt or not rec.executed:
            return
        first = next((h for h in history if h.succeeded and (h.plan == rec.plan or (
            rec.element is not None and h.element == rec.element and h.plan.split()[0] == rec.plan.split()[0]))), None)
        if first is None:
            return
        rec.verified = None  # unverified, but not counted as a failed step
        rec.note += (f"; nothing changed: this was already done at step {first.step}. If the goal is met, answer "
                     "done; otherwise do the next item.")  # fmt: skip
        last = self._last_repeat  # (step, original step, count): only back-to-back repeats of one step add up
        count = last[2] + 1 if last and last[0] == rec.step - 1 and last[1] == first.step else 1
        self._last_repeat = (rec.step, first.step, count)
        if count >= self.s.max_repeated_successes - 1:
            self._repeat_of = first.step

    def _saved_fact(self):
        return next((f for f in self.checklist if f.kind == "saved"), None)

    def _name_the_file(self, progress: dict, snap: Snapshot) -> None:
        """When the next item is saving and a sheet asks for the file name, spell out both moves: the field and the
        sheet's default button (read from the screen, so the planner doesn't have to know how saving works)."""
        fact = self._saved_fact()
        if overwrite_alert(snap) and progress.get("next"):
            file = alert_file(snap) or (fact.value if fact else "the file")
            replace, cancel = alert_buttons(snap)
            progress["next"] = (f'answer the alert: "{file} already exists": click "{replace}" to overwrite'
                                + (f' or "{cancel}"' if cancel else ""))  # fmt: skip
            return
        if fact is None or progress.get("next") != action_for(fact) or (sheet := name_sheet(snap)) is None:
            return
        progress["next"] = f'type "{fact.value}" into the "{sheet.label}" field' + (
            f', then click "{sheet.button}"' if sheet.button else "")  # fmt: skip

    def _guard(self, plan: PlannerOutput, element: UIElement | None, menu_cmd, snap: Snapshot) -> tuple[str, str]:
        """(kind, text) when this step must not run as planned: ("ask", reason) needs the user's OK first (it would
        overwrite a file); ("block", note) is refused because the name field isn't filled in yet. ("", "") otherwise."""
        keys = self._keys(plan, menu_cmd)
        if plan.action == "type":  # never, whatever the mode: the user types passwords and card numbers themselves
            target = element or next((e for e in snap.elements if e.focused), None)
            if target is not None and target.role == "AXSecureTextField":
                return "block", SECURE_NOTE
            if target is not None and sensitive_reason(target):
                return "block", SENSITIVE_NOTE
        if self.form_plan is not None and self.form_plan.form.submit is not None:
            submit = self.form_plan.form.submit
            current = find_form(snap)
            clicks = (plan.action in ("click", "double_click") and element is not None and element.role == "AXButton"
                      and norm(element.name()) == norm(submit.name()))  # fmt: skip
            enter = (plan.action == "shortcut" and keys in ("return", "enter") and current is not None
                     and any(f.element.focused for f in current.fields))  # fmt: skip
            if clicks or enter:
                return "submit", f'submit the form "{self.form_plan.form.title}"'
        if is_discard_button(snap, element) or (keys in ("return", "enter") and leave_dialog(snap)):
            return "leave", "would discard unsaved changes"  # Cancel / Stay is never gated
        if plan.action == "type" and self._user_tab_address_bar(element, snap):
            return "block", "that tab is yours; the agent only navigates tabs it opened"
        if overwrite_alert(snap) and (is_overwrite_button(element) or keys in ("return", "enter")):
            return "ask", "would overwrite an existing file"
        if (family := self._closing_family(element, menu_cmd, keys)) and not goal_allows(self._goal, family):
            return "block", self._closing_note(family, keys or (menu_cmd.path if menu_cmd else element.name()), snap)
        opened = next((f for f in self.checklist if f.kind == "new" and f in self.seen_facts), None)
        if opened is not None and self._is_new_thing_step(element, menu_cmd, keys):
            todo = self._progress(snap)["next"]
            return "block", f"a new {opened.value} is already open; " + (
                f"work on: {todo}" if todo else "the goal looks complete; answer done")  # fmt: skip
        fact = self._saved_fact()
        sheet = name_sheet(snap) if fact is not None else None
        if sheet is not None and keys in ("cmd+s", "return", "enter") and sheet.field.value.lower() != fact.value.lower():
            return "block", f'the name field says "{sheet.field.value}"; type "{fact.value}" into it first'
        return "", ""

    @staticmethod
    def _closing_family(element: UIElement | None, menu_cmd, keys: str) -> str | None:
        """close / quit / switch / hide / minimize, for a key chord, a menu command or a button named like one. Buttons
        inside a sheet, alert or dialog (an error alert's Close) are exempt."""
        if keys and (fam := chord_family(keys)):
            return fam
        if menu_cmd is not None:
            return name_family(menu_cmd.path.split(">")[-1])
        if element is not None and not in_dialog(element):
            return name_family(element.name())
        return None

    def _closing_note(self, family: str, what: str, snap: Snapshot) -> str:
        todo = self._progress(snap)["next"] or self._goal
        return f'"{what}" would close/quit/switch apps, which the goal doesn\'t ask for; work on: {todo}'

    @staticmethod
    def _is_new_thing_step(element: UIElement | None, menu_cmd, keys: str) -> bool:
        """cmd+n / cmd+t, or a click or menu command named "New" or "New <something>" (by name only)."""
        if keys in ("cmd+n", "cmd+t"):
            return True
        name = (menu_cmd.path.split(">")[-1] if menu_cmd else element.name() if element else "").strip().lower()
        return bool(re.fullmatch(r"new(\s+[^\s…]+){0,2}\s*(…|\.\.\.)?", name))

    @staticmethod
    def _keys(plan: PlannerOutput, menu_cmd) -> str:
        return (plan.keys or "").lower() if plan.action == "shortcut" else (menu_cmd.keys or "") if menu_cmd else ""

    @staticmethod
    def _changed(before: Snapshot, after: Snapshot) -> bool:
        if (before.app, before.window_title) != (after.app, after.window_title):
            return True
        return {e.describe(80) for e in before.elements} != {e.describe(80) for e in after.elements}

    def _resolve_menu(self, goal: str, plan: PlannerOutput, snap: Snapshot):
        """The enabled menu command the planner named: an exact path first (no model), else Jev picks among the
        commands the path resembles."""
        commands = snap.menu_commands
        if hit := find_exact(plan.command or "", commands):
            return hit
        choose = getattr(self.selector, "choose", None)
        pool = [c for c in commands if c.enabled]
        if choose is None or not pool:
            return None
        from local_agent.selector.prefilter import prefilter

        pseudo = [UIElement(id=i, role="AXMenuItem", title=c.path) for i, c in enumerate(pool)]
        step = PlannerOutput(done=False, reasoning=plan.reasoning, action="click",
                             target={"role": "AXMenuItem", "description": plan.command or ""})  # fmt: skip
        near = prefilter(plan.command or "", pseudo, self.s.prefilter_top_k, self.s.prefilter_max_k)
        try:
            sel = choose(goal, snap, step, near)
        except (JevError, httpx.HTTPError):
            return None
        return pool[sel.element.id] if sel.probability >= self.s.threshold else None

    def _noop_open(self, cmd: str, snap: Snapshot) -> str | None:
        """The app's name when `cmd` is just `open -a <App>` for the app already in front, else None."""
        command, _ = rewrite_open(cmd, installed_apps() if self.apps is None else self.apps)
        name = opened_app(command)
        if name is None or len(shlex.split(command)) != 3 or name.lower() != snap.app.lower():
            return None
        return name

    def _terminal_step(self, goal: str, plan: PlannerOutput, snap: Snapshot, history: list[StepRecord],
                       rec: StepRecord, by_agent: bool = False) -> tuple[StepRecord, Snapshot | None]:  # fmt: skip
        cmd = plan.command or ""
        if not by_agent and (site := self._reopens_site(cmd)) is not None:
            rec.note = (f"{site.value} is already open in the front tab; work on: "
                        f"{self._progress(snap)['next'] or self._goal}")  # fmt: skip
            return rec, snap
        if (fam := command_family(cmd)) and not goal_allows(goal, fam):
            rec.note = self._closing_note(fam, cmd, snap)
            return rec, snap
        if app := self._noop_open(cmd, snap):
            rec.note = f"{app} is already in front; opening it again does nothing. Work on the next checklist item."
            return rec, snap
        why = self._needs_approval(goal, plan, snap, history, rec)
        if self.dry_run:
            rec.note = f"dry-run: would run {cmd!r} " + (f"after asking the user ({why})" if why else "without asking")
            return rec, snap
        if why is not None and not self.hooks.confirm_terminal(cmd, why):
            rec.note = f"user declined the terminal command ({why})"
            return rec, snap
        self._stage(rec.step, "acting")
        out = self.hooks.run_terminal(cmd)
        rec.executed, rec.verified, rec.note = True, out.ok, out.note
        if why is not None:
            rec.note = f"approved by the user ({why}); {out.note}"
        self.hooks.sleep(self.s.settle_s)
        return rec, None  # terminal commands may change the frontmost app; re-observe
