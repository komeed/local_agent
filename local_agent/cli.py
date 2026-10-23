"""`local-agent` command-line entry point."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.table import Table

from local_agent.config import Settings

app = typer.Typer(add_completion=False, help="macOS desktop agent: Qwen plans, Jev picks UI elements.")
jev_app = typer.Typer(add_completion=False, help="Jev models: list them with their RAM, start one.")
app.add_typer(jev_app, name="jev")
console = Console()
JEV_HELP = "Jev model from `local-agent jev list` (e.g. jevk5-9b-q8, jevk5-2b-q8, cosine-nomic)."


def _prepare(app_name: str | None, delay: float, console: Console = console) -> None:
    """Bring the target window to the front: launch/activate an app, or give the user time to switch."""
    if app_name:
        from local_agent.observe.ax_tree import activate_app

        activate_app(app_name)
        time.sleep(0.5)  # let the window finish appearing
    elif delay > 0:
        console.print(f"[dim]Switch to the target window — observing in {delay:.0f}s…[/dim]")
        time.sleep(delay)


def browser_bridge():
    from local_agent.act import browser

    return browser.Bridge()


def web_checks(s: Settings):
    """The `start` event's `web`: false when page reading is off, else each running browser's status ("ok", "js_disabled"...)."""
    if not s.web_reading:
        return False
    try:
        from local_agent.act import browser

        return {l.name: l.status for l in browser.doctor_lines(
            [(b, n) for b, n in browser.installed_browsers() if browser.is_running(b)])}
    except Exception:  # PyObjC missing or sdef failing must not stop a run
        return {}


def checks_warning(checks: dict) -> str | None:
    """Plain-text warning for a selector that can't judge (cosine without llama-server), else None."""
    if checks.get("goal") and checks.get("safety") and checks.get("verify") == "jev":
        return None
    return ("warning: no Jev judge available (llama-server is not running), so there is no goal check, every "
            "terminal command asks for approval, and step verification is a heuristic")


def _start_app(app_name: str | None, delay: float) -> str | None:
    """The app the agent was launched from (the terminal or the LocalAgent app the goal was typed into), unless the
    user chose the window (--app, or --delay to switch). While it's on screen, the planner opens the goal's app
    instead of working in it."""
    if app_name or delay > 0:
        return None
    try:
        from local_agent.observe.ax_tree import controller_app

        return controller_app()
    except Exception:  # the planner just doesn't get the note
        return None


@app.command()
def snapshot(
    out: Path | None = typer.Option(None, "--out", "-o", help="Write full JSON here instead of stdout."),
    app_name: str | None = typer.Option(None, "--app", help="Activate this app first."),
    delay: float = typer.Option(3.0, help="Seconds to wait before observing."),
    no_menubar: bool = typer.Option(False, "--no-menubar", help="Skip menu bar items."),
):
    """Dump every interactive element of the frontmost window as JSON."""
    from local_agent.observe.ax_tree import snapshot as take_snapshot

    _prepare(app_name, delay)
    t0 = time.monotonic()
    snap = take_snapshot(Settings.from_env(), include_menubar=not no_menubar)
    elapsed = time.monotonic() - t0
    data = json.dumps(snap.to_dict(), indent=2, ensure_ascii=False)
    if out:
        out.write_text(data)
        console.print(
            f"[green]{len(snap.elements)} elements[/green] from {snap.app} — {snap.window_title!r} "
            f"in {elapsed:.2f}s → {out}"
        )
        console.print(snap.scope())
    else:
        print(data)


def _doctor_report(s: Settings, trusted: bool) -> dict:
    """What `doctor --json` prints; see docs/APP_INTEGRATION.md."""
    from local_agent.selector import catalog
    from local_agent.selector.jev import make_selector

    ollama: dict = {"ok": False, "url": s.ollama_url, "error": None, "models": [], "planner_model": s.planner_model,
                    "has_model": False}
    try:
        tags = httpx.get(f"{s.ollama_url}/api/tags", timeout=5).json().get("models", [])
        ollama["models"] = [{"name": m["name"], "size_gb": round(m.get("size", 0) / 1e9, 1)} for m in tags]
        names = {m["name"] for m in tags}
        ollama["has_model"] = s.planner_model in names or f"{s.planner_model}:latest" in names
        ollama["ok"] = True
    except (httpx.HTTPError, ValueError) as e:
        ollama["error"] = str(e)
    s = catalog.resolve(s)
    jevk5, cosine = s.jev_backend == "jevk5", s.jev_backend == "cosine"
    model = catalog.BY_KEY.get(s.jev) or catalog.BY_FILE.get(s.jevk5_file if jevk5 else s.embed_model)
    served = catalog.served_file(s.jevk5_url) if jevk5 else None
    problem = catalog.mismatch(s)
    jev = {
        "ok": make_selector(s).health(),
        "backend": s.jev_backend,
        "model": model.key if model else None,
        "ram_gb": model.ram_gb if model else None,
        "url": s.jevk5_url if jevk5 else s.ollama_url if cosine else s.jev_url,
        "served_file": served,
        "error": None,
        "command": " ".join(catalog.serve_command(model, s.jevk5_url)) if model else None,
    }
    if problem:
        jev["ok"], jev["error"] = False, problem
    return {
        "ok": trusted and ollama["ok"] and ollama["has_model"] and jev["ok"],
        "accessibility": {"ok": trusted},
        "ollama": ollama,
        "jev": jev,
    }


@app.command()
def doctor(
    as_json: bool = typer.Option(False, "--json", help="Print one JSON object instead of text."),
    jev: str | None = typer.Option(None, "--jev", help=JEV_HELP),
):
    """Check Accessibility permission, Ollama + planner model, and the Jev backend."""
    from local_agent.observe.ax_tree import ensure_trusted

    s = Settings.from_env(jev=jev)
    report = _doctor_report(s, ensure_trusted(prompt=True))
    if as_json:
        print(json.dumps(report, ensure_ascii=False))
        raise typer.Exit(0 if report["ok"] else 1)
    trusted, ollama, jev = report["accessibility"]["ok"], report["ollama"], report["jev"]
    console.print(f"Accessibility permission: {'[green]ok' if trusted else '[red]missing (see System Settings)'}")
    if ollama["ok"]:
        console.print(
            f"Ollama at {s.ollama_url}: [green]ok[/green]; model {s.planner_model}: "
            + ("[green]present" if ollama["has_model"] else f"[red]missing — run `ollama pull {s.planner_model}`")
        )
    else:
        console.print(f"Ollama at {s.ollama_url}: [red]unreachable ({ollama['error']}) — start it with `ollama serve`")
    name = jev["model"] or jev["backend"]
    if jev["ok"]:
        console.print(f"Jev {name} at {jev['url']}: [green]ok")
    else:
        console.print(f"Jev {name} at {jev['url']}: [red]{jev['error'] or 'not ready'}")
        if jev["command"]:
            console.print(f"  start it with: [bold]{jev['command']}[/bold]")
    from local_agent.act import browser

    for line in browser.doctor_lines():
        console.print(f"Browser {line.text()}", markup=False)
    raise typer.Exit(0 if report["ok"] else 1)


def _ollama_names(url: str) -> set[str]:
    try:
        return {m["name"] for m in httpx.get(f"{url}/api/tags", timeout=5).json().get("models", [])}
    except (httpx.HTTPError, ValueError):
        return set()


@jev_app.command("list")
def jev_list(as_json: bool = typer.Option(False, "--json", help="Print JSON instead of a table.")):
    """Jev models, largest first, with the RAM each holds while serving."""
    from local_agent.selector import catalog

    s = catalog.resolve(Settings.from_env())
    served = catalog.served_file(s.jevk5_url)
    names = _ollama_names(s.ollama_url)
    current = s.jev or catalog.BY_FILE.get(s.jevk5_file, catalog.get(catalog.DEFAULT_KEY)).key
    rows = [{"key": m.key, "backend": m.backend, "file": m.file, "size_gb": m.size_gb, "ram_gb": m.ram_gb,
             "note": m.note, "downloaded": catalog.downloaded(m, names), "serving": m.file == served,
             "selected": m.key == current} for m in catalog.CATALOG]  # fmt: skip
    if as_json:
        print(json.dumps(rows))
        return
    table = Table("", "model", "≈ RAM", "here", "notes", title="Jev models (largest first)")
    for r in rows:
        mark = "→" if r["selected"] else ""
        state = "serving" if r["serving"] else "yes" if r["downloaded"] else ""
        table.add_row(mark, r["key"], f"{r['ram_gb']:.1f} GB", state, r["note"])
    console.print(table)
    console.print("[dim]→ = selected. Choose with `local-agent run --jev <model>` or LOCAL_AGENT_JEV; start one with "
                  "`local-agent jev serve <model>`. RAM besides the 4B Q8_0 is estimated from file size.[/dim]")


@jev_app.command("serve")
def jev_serve(
    model: str = typer.Argument(..., help="Model key from `local-agent jev list`."),
    print_only: bool = typer.Option(False, "--print", help="Only print the command."),
):
    """Start llama-server with a JevK5 file (downloads it from Hugging Face if it isn't in ~/models/jevk5), or pull a
    cosine embedding model into Ollama. Runs in the foreground; stop any other llama-server on the port first."""
    import os
    import shutil

    from local_agent.selector import catalog

    try:
        m = catalog.get(model)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from None
    cmd = catalog.serve_command(m, Settings.from_env().jevk5_url)
    console.print(" ".join(cmd))
    if print_only:
        return
    if shutil.which(cmd[0]) is None:
        console.print(f"[red]{cmd[0]} not found on PATH")
        raise typer.Exit(1)
    os.execvp(cmd[0], cmd)


@app.command()
def run(
    goal: str = typer.Argument(..., help="What you want done, in plain language."),
    app_name: str | None = typer.Option(
        None, "--app", help="Optional: activate this app first. Normally the agent opens the app the goal needs."
    ),
    delay: float = typer.Option(0.0, help="Seconds to wait before the first observation (to switch windows)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Plan and select one step without acting."),
    turbo: bool = typer.Option(
        False, "--turbo", "--yes", "-y",
        help="Run every terminal command with no safety check and no questions. Default: commands run unless they "
        "match a dangerous pattern or Jev rates them unsafe; only those ask you.",
    ),
    threshold: float | None = typer.Option(None, help="Min Jev probability to act on a chosen element."),
    max_steps: int | None = typer.Option(None, help="Stop after this many steps."),
    model: str | None = typer.Option(None, help="Ollama planner model (default qwen3.5:9b)."),
    view: str | None = typer.Option(None, help="Planner screen view: ids (numbered, default), names, or off."),
    planner: str | None = typer.Option(
        None, help="Planner mode: json (one free-form step, default), choice (multiple choice; for small models) or "
        "kind (Jev picks the kind of step, the planner the details; screen view only when the kind needs it) or "
        "jev (no planner model: code lists the candidate actions, Jev picks one; fits small Macs)."
    ),
    jev: str | None = typer.Option(None, "--jev", help=JEV_HELP),
    menu_actions: bool | None = typer.Option(
        None, "--menu-actions/--no-menu-actions",
        help="Offer the app's menu commands as a planner action (default off: it scored lower on the 3B).",
    ),
    log: Path | None = typer.Option(None, "--log", help="Write the step history as JSON here."),
    events: bool = typer.Option(
        False, "--events", help="Write JSON Lines events to stdout for apps (docs/APP_INTEGRATION.md); text goes to stderr."
    ),
    low_memory: bool | None = typer.Option(None, "--low-memory/--no-low-memory", help="Unload the planner model after each outline call (8 GB Macs)."),
    outline_model: str | None = typer.Option(None, "--outline-model", help="Who writes the outline: planner (Ollama) or jev (JevK5 server)."),
    outline: bool | None = typer.Option(None, "--outline/--no-outline", help="Ask the planner model for a step outline first (default on)."),
    data: Path | None = typer.Option(None, "--data", help="Values for form fields: a JSON object or `label: value` lines."),
    verbose: bool = typer.Option(False, "--verbose", help="Print each phase of a step as it starts, with the last one's time."),
):
    """Work toward GOAL on the frontmost window until done or out of steps."""
    from local_agent.act import executor
    from local_agent.loop import Agent, Hooks
    from local_agent.observe.ax_tree import snapshot as take_snapshot
    from local_agent.planner.qwen import make_planner
    from local_agent.selector.jev import make_selector

    s = Settings.from_env(threshold=threshold, max_steps=max_steps, planner_model=model, planner_view=view,
                          planner_mode=planner, jev=jev, turbo=turbo or None, menu_actions=menu_actions,
                          low_memory=low_memory, outline=outline, outline_model=outline_model)
    from local_agent.selector import catalog

    s = catalog.resolve(s)  # once, so `start` names the Jev model actually used
    if problem := catalog.mismatch(s):
        cmd = " ".join(catalog.serve_command(catalog.get(s.jev), s.jevk5_url))
        Console(stderr=True).print(f"[red]{problem}[/red]; start the chosen model with:\n  {cmd}")
        raise typer.Exit(2)
    out = Console(stderr=True) if events else console
    ev = None
    if events:
        from local_agent import events as ev_mod

        ev = ev_mod.EventWriter()
    step = 0  # current step, for confirm_terminal events

    def confirm(cmd: str, why: str) -> bool:
        """Only reached for commands the safety check didn't clear (never in turbo mode)."""
        if ev:
            ev.emit("confirm_terminal", step=step, command=cmd, reason=why, safety_probability=agent.last_safety)
            return sys.stdin.readline().strip().lower() in ("y", "yes")  # EOF reads "" → declined
        try:
            return typer.confirm(f"Run terminal command ({why}): {cmd}", default=False)
        except typer.Abort:  # no TTY / EOF: treat as declined
            return False

    def on_plan(n, snap, plan):
        nonlocal step
        step = n
        out.rule(f"step {n} · {snap.app} — {snap.window_title or '(no window)'} · {len(snap.elements)} elements")
        out.print(f"[cyan]plan[/cyan] {plan.summary()}  [dim]{plan.reasoning}[/dim]")
        if ev:
            ev.emit("plan", **ev_mod.plan_event(
                n, plan, agent.screen, getattr(agent.planner, "last_messages", None),
                getattr(agent.planner, "last_prompt_tokens", None), agent.timing.get("planner_s"), agent.web,
            ))

    def on_select(sel):
        if ev:
            ev.emit("select", **ev_mod.select_event(step, sel, agent.timing.get("select_s")))
        if sel.source == "planner":
            out.print(f"[magenta]pick[/magenta] [{sel.element.id}] {sel.element.describe()}  [dim](by number)[/dim]")
            return
        out.print(
            f"[magenta]jev[/magenta] {sel.element.describe()}  p={sel.probability:.2f} conf={sel.confidence:.2f} "
            f"[dim]({sel.candidates} candidates, {sel.rounds} round{'s' if sel.rounds > 1 else ''})[/dim]"
        )

    def status(rec) -> str:
        if rec.note.startswith("dry-run"):
            return "dry-run"
        return "ok" if rec.succeeded else "failed"

    stage_clock = {"name": None, "t": time.monotonic()}

    def on_stage(n: int, name: str) -> None:
        now = time.monotonic()
        prev = None if stage_clock["name"] is None else round(now - stage_clock["t"], 3)
        stage_clock.update(name=name, t=now)
        if ev:
            ev.emit("stage", **ev_mod.stage_event(n, name, prev))
        if verbose:
            out.print(f"[dim]  · {name}" + ("" if prev is None else f" (previous phase {prev:.2f}s)") + "[/dim]")

    def on_outline(items, seconds, load_s, rejected=""):
        if ev:
            ev.emit("outline", **ev_mod.outline_event(items, seconds, load_s, rejected))
        out.print("[cyan]outline[/cyan] " + (" → ".join(items) if items else f"(rejected: {rejected})"))

    def on_replan(reason, items, rejected=""):
        if ev:
            ev.emit("replan", **ev_mod.replan_event(reason, items, rejected))
        out.print(f"[yellow]replan[/yellow] ({reason}) " + (" → ".join(items) if items else f"(rejected: {rejected})"))

    def on_record(rec):
        if ev:
            ev.emit("record", **ev_mod.record_event(rec, agent.timing.get("verify_s"), agent.timing.get("goal_s")))
        color = {"ok": "green", "failed": "red"}.get(status(rec), "yellow")
        v = "" if rec.verify_probability is None else f" verify={rec.verify_probability:.2f}"
        v += "" if rec.goal_probability is None else f" goal={rec.goal_probability:.2f}"
        out.print(f"[{color}]{status(rec)}[/{color}]{v} {rec.note}")

    hooks = Hooks(
        observe=lambda: take_snapshot(s),
        execute=executor.execute,
        settle_web=executor.settle_web,
        trusted_click=executor.trusted_click,
        browser=browser_bridge(),
        run_menu=executor.run_menu,
        run_terminal=executor.run_terminal,
        confirm_terminal=confirm,
        on_plan=on_plan,
        on_select=on_select,
        on_record=on_record,
        on_stage=on_stage,
        on_outline=on_outline,
        on_replan=on_replan,
    )
    selector = make_selector(s)
    checks = selector.checks() if hasattr(selector, "checks") else {"goal": True, "safety": True, "verify": "jev"}
    checks = {**checks, "web": web_checks(s)}
    if warning := checks_warning(checks):
        Console(stderr=True).print(f"[yellow]{warning}[/yellow]")
    from local_agent.outline import make_outliner

    form_data = None
    if data is not None:
        from local_agent.forms import parse_data

        form_data = parse_data(data.read_text())
    agent = Agent(s, make_planner(s, selector), selector, hooks, dry_run=dry_run, start_app=_start_app(app_name, delay),
                  form_data=form_data, outliner=make_outliner(s))
    from local_agent.act import browser as browser_mod
    from local_agent.observe import ax_tree

    browser_mod.configure(s.browser_timeout_s, s.browser_action_timeout_s)
    ax_tree.STAGE = lambda name: on_stage(agent.stage_step, name)
    if ev:
        ev.emit("start", version=ev_mod.EVENTS_VERSION, goal=goal, app=app_name, model=s.planner_model,
                view=s.planner_view, planner=s.planner_mode, jev=s.jev or None, turbo=s.turbo, dry_run=dry_run,
                max_steps=s.max_steps, threshold=s.threshold, checks=checks)
        try:
            _prepare(app_name, delay, out)
            result = agent.run(goal)
        except KeyboardInterrupt:
            out.print("[yellow]cancelled[/yellow]")
            ev.emit("result", **ev_mod.result_event(False, "cancelled", agent.history, ev.elapsed()))
            raise typer.Exit(130)
        except Exception as e:  # anything else is a crash the app should show, not a traceback it can't parse
            out.print_exception()
            ev.emit("error", message=f"{type(e).__name__}: {e}", kind=type(e).__name__)
            raise typer.Exit(2)
        ev.emit("result", **ev_mod.result_event(result.success, result.reason, result.history, ev.elapsed()))
    else:
        _prepare(app_name, delay)
        result = agent.run(goal)

    if result.reason == "overwrite_declined":
        out.print("[yellow]The file already exists and you declined to replace it; nothing was overwritten.[/yellow]")
    table = Table("step", "plan", "element", "p", "result", title=f"{'✓' if result.success else '✗'} {result.reason}")
    for r in result.history:
        table.add_row(
            str(r.step),
            r.plan,
            r.element or "",
            "" if r.probability is None else f"{r.probability:.2f}",
            status(r),
        )
    out.print(table)
    if log:
        log.write_text(json.dumps([r.for_planner() for r in result.history], indent=2))
    raise typer.Exit(0 if result.success else 1)


if __name__ == "__main__":
    app()
