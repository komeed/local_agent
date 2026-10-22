"""Compare planner models and screen-view modes on evals/planner_cases.py: accuracy, latency, prompt size, memory.

    uv run python -m evals.planner_eval qwen2.5-coder:3b qwen2.5-coder:7b qwen3.5:9b --views off,names,ids
    uv run python -m evals.planner_eval qwen2.5-coder:3b --modes json,choice --views ids   # multiple-choice planner
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import time

import httpx

from evals.planner_cases import CASES
from local_agent.config import Settings
from local_agent.observe.screen_view import render, view_budget
from local_agent.planner.qwen import PlannerError, QwenPlanner, make_planner


def loaded_size_gb(ollama_url: str, model: str) -> float | None:
    """Memory Ollama reports for the loaded model (weights + KV cache)."""
    for m in httpx.get(f"{ollama_url}/api/ps", timeout=5).json().get("models", []):
        if m["name"] == model or m["model"] == model:
            return m.get("size", 0) / 1e9
    return None


def pressure() -> tuple[int, int]:
    """(macOS memory pressure level: 1 normal, 2 warn, 4 critical; system-wide free %)."""
    level = int(subprocess.run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"], capture_output=True,
                               text=True).stdout.strip())  # fmt: skip
    line = subprocess.run(["memory_pressure"], capture_output=True, text=True).stdout.strip().splitlines()[-1]
    return level, int(line.rsplit(" ", 1)[-1].rstrip("%"))


def unload(ollama_url: str, model: str) -> None:
    httpx.post(f"{ollama_url}/api/generate", json={"model": model, "keep_alive": 0}, timeout=60)


def run(model: str, view: str, repeats: int, mode: str = "json") -> dict:
    s = Settings.from_env(planner_model=model, planner_view=view, planner_mode=mode)
    planner = make_planner(s)
    budget = view_budget(s.planner_num_ctx, s.planner_view_tokens)
    rows, times, prompt_tokens = [], [], []
    for case in CASES:
        snap = case.snap()
        screen = render(snap, budget, numbered=view == "ids") if view != "off" else None
        passes, reasons = 0, []
        for _ in range(repeats):
            t = time.monotonic()
            try:
                out = planner.next_step(case.goal, snap.scope(), case.history, screen)
                reason = case.check(out, snap)
            except PlannerError as e:
                reason = f"invalid output: {e}"[:200]
            times.append(time.monotonic() - t)
            prompt_tokens.append(planner.last_prompt_tokens)
            passes += reason == ""
            if reason:
                reasons.append(reason)
        rows.append({"case": case.name, "category": case.category, "needs_screen": case.needs_screen,
                     "passes": passes, "runs": repeats, "why": reasons[0] if reasons else ""})  # fmt: skip
    total = len(rows) * repeats
    screen_rows = [r for r in rows if r["category"] == "screen"]
    return {
        "model": model,
        "mode": mode,
        "view": view,
        "rows": rows,
        "accuracy": sum(r["passes"] for r in rows) / total,
        "screen_accuracy": sum(r["passes"] for r in screen_rows) / (len(screen_rows) * repeats),
        "other_accuracy": sum(r["passes"] for r in rows if r["category"] != "screen")
        / ((len(rows) - len(screen_rows)) * repeats),
        "median_s": statistics.median(times),
        "p90_s": sorted(times)[int(0.9 * (len(times) - 1))],
        "prompt_tokens": statistics.median(prompt_tokens),
        "max_prompt_tokens": max(prompt_tokens),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("models", nargs="+")
    ap.add_argument("--views", default="off,names,ids", help="Comma-separated: off, names, ids.")
    ap.add_argument("--modes", default="json", help="Comma-separated planner modes: json, choice.")
    ap.add_argument("--repeats", type=int, default=1, help="Runs per case (temperature is 0, so 1 is usually enough).")
    ap.add_argument("--json", help="Write full results here.")
    args = ap.parse_args()
    views = args.views.split(",")
    modes = args.modes.split(",")

    s = Settings.from_env()
    results = []
    for model in args.models:
        for other in args.models:
            unload(s.ollama_url, other)  # measure each model alone next to Jev
        try:  # load weights before timing; the reply doesn't matter
            QwenPlanner(Settings.from_env(planner_model=model)).next_step("warm up", CASES[0].snap().scope(), [])
        except PlannerError:
            pass
        loaded = loaded_size_gb(s.ollama_url, model)
        level, free = pressure()
        for mode, view in [(m, v) for m in modes for v in views]:
            print(f"== {model}  mode={mode}  view={view}", flush=True)
            r = run(model, view, args.repeats, mode)
            r.update(loaded_gb=loaded, pressure_level=level, free_pct=free)
            for row in r["rows"]:
                mark = "✓" if row["passes"] == row["runs"] else "✗"
                print(f"  {mark} {row['category']:10s} {row['case']:24s} {row['why'][:110]}", flush=True)
            print(f"  accuracy {r['accuracy']:.0%} (screen {r['screen_accuracy']:.0%}, other {r['other_accuracy']:.0%})"
                  f"  median {r['median_s']:.2f}s  p90 {r['p90_s']:.2f}s  prompt ~{r['prompt_tokens']:.0f} tok",
                  flush=True)  # fmt: skip
            results.append(r)
        unload(s.ollama_url, model)

    print("\nmodel              mode   view   accuracy  screen  other  median   p90   prompt tok  loaded  pressure  free")
    for r in results:
        print(f"{r['model']:18s} {r['mode']:6s} {r['view']:5s}  {r['accuracy']:6.0%}  {r['screen_accuracy']:6.0%} {r['other_accuracy']:6.0%}"
              f"  {r['median_s']:5.2f}s {r['p90_s']:5.2f}s  {r['prompt_tokens']:6.0f}/{r['max_prompt_tokens']:<5.0f}"
              f" {r['loaded_gb'] or 0:5.1f}GB  {r['pressure_level']:^8d}  {r['free_pct']}%")  # fmt: skip
    if args.json:
        with open(args.json, "w") as f:
            json.dump(results, f, indent=2)


if __name__ == "__main__":
    main()
