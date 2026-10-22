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
def doctor():
    """Check Accessibility permission, Ollama + planner model, and the Jev backend."""
    from local_agent.observe.ax_tree import ensure_trusted
    from local_agent.selector.jev import make_selector

    s = Settings.from_env()
    ok = True
    trusted = ensure_trusted(prompt=True)
    console.print(f"Accessibility permission: {'[green]ok' if trusted else '[red]missing (see System Settings)'}")
    ok &= trusted
    try:
        tags = httpx.get(f"{s.ollama_url}/api/tags", timeout=5).json().get("models", [])
        names = {m["name"] for m in tags}
        has = s.planner_model in names or f"{s.planner_model}:latest" in names
        console.print(
            f"Ollama at {s.ollama_url}: [green]ok[/green]; model {s.planner_model}: "
            + ("[green]present" if has else f"[red]missing — run `ollama pull {s.planner_model}`")
        )
        ok &= has
    except httpx.HTTPError as e:
        console.print(f"Ollama at {s.ollama_url}: [red]unreachable ({e}) — start it with `ollama serve`")
        ok = False
    jev_url = s.jevk5_url if s.jev_backend == "jevk5" else s.jev_url
    jev_ok = make_selector(s).health()
    console.print(f"Jev ({s.jev_backend}) at {jev_url}: " + ("[green]ok" if jev_ok else "[red]unreachable — see README"))
    ok &= jev_ok
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
    model: str | None = typer.Option(None, help="Ollama planner model (default qwen2.5-coder:7b)."),
    log: Path | None = typer.Option(None, "--log", help="Write the step history as JSON here."),
):
    """Work toward GOAL on the frontmost window until done or out of steps."""
    from local_agent.act import executor
    from local_agent.loop import Agent, Hooks
    from local_agent.observe.ax_tree import snapshot as take_snapshot
    from local_agent.planner.qwen import QwenPlanner
    from local_agent.selector.jev import make_selector

    s = Settings.from_env(threshold=threshold, max_steps=max_steps, planner_model=model)

    def confirm(cmd: str) -> bool:
        if yes:
            return True
        try:
            return typer.confirm(f"Run terminal command: {cmd}", default=False)
        except typer.Abort:  # no TTY / EOF: treat as declined
            return False

    def on_plan(n, snap, plan):
        console.rule(f"step {n} · {snap.app} — {snap.window_title or '(no window)'} · {len(snap.elements)} elements")
        console.print(f"[cyan]plan[/cyan] {plan.summary()}  [dim]{plan.reasoning}[/dim]")

    def on_select(sel):
        console.print(
            f"[magenta]jev[/magenta] {sel.element.describe()}  p={sel.probability:.2f} conf={sel.confidence:.2f} "
            f"[dim]({sel.candidates} candidates, {sel.rounds} round{'s' if sel.rounds > 1 else ''})[/dim]"
        )

    def status(rec) -> str:
        if rec.note.startswith("dry-run"):
            return "dry-run"
        return "ok" if rec.succeeded else "failed"

    def on_record(rec):
        color = {"ok": "green", "failed": "red"}.get(status(rec), "yellow")
        v = "" if rec.verify_probability is None else f" verify={rec.verify_probability:.2f}"
        console.print(f"[{color}]{status(rec)}[/{color}]{v} {rec.note}")

    hooks = Hooks(
        observe=lambda: take_snapshot(s),
        execute=executor.execute,
        run_terminal=executor.run_terminal,
        confirm_terminal=confirm,
        on_plan=on_plan,
        on_select=on_select,
        on_record=on_record,
    )
    agent = Agent(s, QwenPlanner(s), make_selector(s), hooks, dry_run=dry_run)
    _prepare(app_name, delay)
    result = agent.run(goal)

    table = Table("step", "plan", "element", "p", "result", title=f"{'✓' if result.success else '✗'} {result.reason}")
    for r in result.history:
        table.add_row(
            str(r.step),
            r.plan,
            r.element or "",
            "" if r.probability is None else f"{r.probability:.2f}",
            status(r),
        )
    console.print(table)
    if log:
        log.write_text(json.dumps([r.for_planner() for r in result.history], indent=2))
    raise typer.Exit(0 if result.success else 1)


if __name__ == "__main__":
    app()
