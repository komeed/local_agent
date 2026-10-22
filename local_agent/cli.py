"""`local-agent` command-line entry point."""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.table import Table

from local_agent.config import Settings

app = typer.Typer(add_completion=False, help="macOS desktop agent: Qwen plans, Jev picks UI elements.")
console = Console()


def _prepare(app_name: str | None, delay: float) -> None:
    """Bring the target window to the front: launch/activate an app, or give the user time to switch."""
    if app_name:
        from local_agent.observe.ax_tree import activate_app

        activate_app(app_name)
        time.sleep(0.5)  # let the window finish appearing
    elif delay > 0:
        console.print(f"[dim]Switch to the target window — observing in {delay:.0f}s…[/dim]")
        time.sleep(delay)


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


@app.command()
def doctor(
    as_json: bool = typer.Option(False, "--json", help="Print one JSON object (for the app) instead of text."),
):
    """Check Accessibility permission, Ollama + planner model, and the Jev backend."""
    from local_agent.observe.ax_tree import ensure_trusted
    from local_agent.selector.jev import make_selector

    s = Settings.from_env()
    report: dict = {"accessibility": ensure_trusted(prompt=True)}
    ollama: dict = {"url": s.ollama_url, "ok": False, "models": [], "planner_model": s.planner_model,
                    "planner_model_present": False, "error": None}  # fmt: skip
    try:
        tags = httpx.get(f"{s.ollama_url}/api/tags", timeout=5).json().get("models", [])
        names = {m["name"] for m in tags}
        ollama.update(ok=True, models=[{"name": m["name"], "size_gb": round(m.get("size", 0) / 1e9, 2)} for m in tags],
                      planner_model_present=s.planner_model in names or f"{s.planner_model}:latest" in names)
    except httpx.HTTPError as e:
        ollama["error"] = str(e)
    jev_url = s.jevk5_url if s.jev_backend == "jevk5" else s.jev_url
    report["ollama"] = ollama
    report["jev"] = {"backend": s.jev_backend, "url": jev_url, "ok": make_selector(s).health()}
    ok = report["accessibility"] and ollama["planner_model_present"] and report["jev"]["ok"]
    report["ok"] = ok
    if as_json:
        print(json.dumps(report))
        raise typer.Exit(0 if ok else 1)

    trusted = report["accessibility"]
    console.print(f"Accessibility permission: {'[green]ok' if trusted else '[red]missing (see System Settings)'}")
    if ollama["ok"]:
        console.print(
            f"Ollama at {s.ollama_url}: [green]ok[/green]; model {s.planner_model}: "
            + ("[green]present" if ollama["planner_model_present"] else f"[red]missing — run `ollama pull {s.planner_model}`")
        )
    else:
        console.print(f"Ollama at {s.ollama_url}: [red]unreachable ({ollama['error']}) — start it with `ollama serve`")
    console.print(f"Jev ({s.jev_backend}) at {jev_url}: " + ("[green]ok" if report["jev"]["ok"] else "[red]unreachable — see README"))
    raise typer.Exit(0 if ok else 1)


@app.command()
def run(
    goal: str = typer.Argument(..., help="What you want done, in plain language."),
    app_name: str | None = typer.Option(None, "--app", help="Activate this app before starting."),
    delay: float = typer.Option(3.0, help="Seconds to wait before the first observation."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Plan and select one step without acting."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Run terminal commands without asking."),
    threshold: float | None = typer.Option(None, help="Min Jev probability to act on a chosen element."),
    max_steps: int | None = typer.Option(None, help="Stop after this many steps."),
    model: str | None = typer.Option(None, help="Ollama planner model (default qwen3.5:9b)."),
    view: str | None = typer.Option(None, help="Planner screen view: ids (numbered, default), names, or off."),
    planner: str | None = typer.Option(
        None, help="Planner mode: json (one free-form step, default) or choice (multiple choice; for small models)."
    ),
    log: Path | None = typer.Option(None, "--log", help="Write the step history as JSON here."),
    events: bool = typer.Option(
        False, "--events", help="Write JSON Lines events to stdout for the app (docs/APP_INTEGRATION.md); "
        "human-readable output moves to stderr. Terminal commands are confirmed by a 'y' line on stdin."
    ),
):
    """Work toward GOAL on the frontmost window until done or out of steps."""
    import sys

    from local_agent.act import executor
    from local_agent.events import EventStream, TimedPlanner, TimedSelector, step_status
    from local_agent.loop import Agent, Hooks
    from local_agent.observe.ax_tree import snapshot as take_snapshot
    from local_agent.planner.qwen import make_planner
    from local_agent.selector.jev import make_selector

    s = Settings.from_env(threshold=threshold, max_steps=max_steps, planner_model=model, planner_view=view,
                          planner_mode=planner)
    con = Console(stderr=True) if events else console
    stream = EventStream() if events else None
    timed_planner = TimedPlanner(make_planner(s))
    timed_selector = TimedSelector(make_selector(s))
    current = {"step": 0}

    def confirm(cmd: str) -> bool:
        if yes:
            return True
        if stream:  # the app answers with one line on stdin
            stream.confirm(current["step"], cmd)
            return sys.stdin.readline().strip().lower() in ("y", "yes")
        try:
            return typer.confirm(f"Run terminal command: {cmd}", default=False)
        except typer.Abort:  # no TTY / EOF: treat as declined
            return False

    def on_plan(n, snap, plan):
        current["step"] = n
        timed_selector.verify_seconds = None
        if stream:
            stream.plan(n, snap, plan, timed_planner)
        con.rule(f"step {n} · {snap.app} — {snap.window_title or '(no window)'} · {len(snap.elements)} elements")
        con.print(f"[cyan]plan[/cyan] {plan.summary()}  [dim]{plan.reasoning}[/dim]")

    def on_select(sel):
        if stream:
            stream.select(current["step"], sel, timed_selector)
        if sel.source == "planner":
            con.print(f"[magenta]pick[/magenta] [{sel.element.id}] {sel.element.describe()}  [dim](by number)[/dim]")
            return
        con.print(
            f"[magenta]jev[/magenta] {sel.element.describe()}  p={sel.probability:.2f} conf={sel.confidence:.2f} "
            f"[dim]({sel.candidates} candidates, {sel.rounds} round{'s' if sel.rounds > 1 else ''})[/dim]"
        )

    def on_record(rec):
        if stream:
            stream.record(rec, timed_selector)
        color = {"ok": "green", "failed": "red"}.get(step_status(rec), "yellow")
        v = "" if rec.verify_probability is None else f" verify={rec.verify_probability:.2f}"
        con.print(f"[{color}]{step_status(rec)}[/{color}]{v} {rec.note}")

    hooks = Hooks(
        observe=lambda: take_snapshot(s),
        execute=executor.execute,
        run_terminal=executor.run_terminal,
        confirm_terminal=confirm,
        on_plan=on_plan,
        on_select=on_select,
        on_record=on_record,
    )
    agent = Agent(s, timed_planner, timed_selector, hooks, dry_run=dry_run)
    if stream:
        stream.start(goal, s, dry_run, app_name)
    try:
        _prepare(app_name, delay)
        result = agent.run(goal)
    except KeyboardInterrupt:  # the app stops a run with SIGINT
        if stream:
            stream.result(False, "cancelled", [])
        raise typer.Exit(130)
    except Exception as e:  # e.g. Accessibility permission missing, app not running
        if stream:
            stream.error(f"{type(e).__name__}: {e}")
            raise typer.Exit(2)
        raise
    if stream:
        stream.result(result.success, result.reason, result.history)

    table = Table("step", "plan", "element", "p", "result", title=f"{'✓' if result.success else '✗'} {result.reason}")
    for r in result.history:
        table.add_row(
            str(r.step),
            r.plan,
            r.element or "",
            "" if r.probability is None else f"{r.probability:.2f}",
            step_status(r),
        )
    con.print(table)
    if log:
        log.write_text(json.dumps([r.for_planner() for r in result.history], indent=2))
    raise typer.Exit(0 if result.success else 1)


if __name__ == "__main__":
    app()
