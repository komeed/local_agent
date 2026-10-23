"""Compare planner models and screen-view modes on evals/planner_cases.py: accuracy, latency, prompt size, memory.

    uv run python -m evals.planner_eval qwen2.5-coder:3b qwen2.5-coder:7b qwen3.5:9b --views off,names,ids
    uv run python -m evals.planner_eval qwen2.5-coder:3b --modes json,choice --views ids   # multiple-choice planner
    uv run python -m evals.planner_eval qwen2.5-coder:3b --modes kind --views ids   # Jev picks the kind (llama-server)
    uv run python -m evals.planner_eval qwen2.5-coder:3b --modes jev --views ids --ram   # no planner model; peak memory

`--modes jev` needs llama-server and never loads the Ollama model. Cases whose expected answer the jev options can't express
(a shell command, a free-text answer, a shortcut other than return / escape / tab) are reported as n/a and left out of its
accuracy; `expressible` is every mode's accuracy on the cases that are not n/a, so the modes compare on equal terms.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import threading
import time

import httpx

from evals.planner_cases import CASES, case_scope
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


class MemorySampler:
    """Peak memory of the model processes while an eval runs: llama-server and the Ollama runner, sampled every
    `interval` s. Two numbers per process group: RSS (`ps -o rss`) and the physical footprint (`footprint`), which on
    Apple silicon includes the GPU buffers Metal maps for the weights and RSS undercounts."""

    # Ollama's runner is itself a binary named llama-server (under .../ollama/), so the two groups must not overlap.
    GROUPS = {"llama-server": r"^(?!.*ollama).*llama-server", "ollama-runner": r"ollama/.*llama-server|ollama.*runner"}

    def __init__(self, interval: float = 0.5, llama_port: int | None = None):
        self.interval, self.stop = interval, threading.Event()
        if llama_port:  # only the server this run uses (another may be running on another port)
            self.GROUPS = {**self.GROUPS, "llama-server": rf"^(?!.*ollama).*llama-server.*--port {llama_port}\b"}
        self.peak = {g: {"rss_mb": 0.0, "footprint_mb": 0.0} for g in self.GROUPS}
        self.thread = threading.Thread(target=self._loop, daemon=True)

    @staticmethod
    def processes() -> list[tuple[int, int, str]]:
        out = subprocess.run(["ps", "-axo", "pid=,rss=,command="], capture_output=True, text=True).stdout
        rows = []
        for line in out.splitlines():
            parts = line.split(None, 2)
            if len(parts) == 3 and parts[0].isdigit():
                rows.append((int(parts[0]), int(parts[1]), parts[2]))
        return rows

    @staticmethod
    def footprint_mb(pid: int) -> float:
        out = subprocess.run(["footprint", "-p", str(pid)], capture_output=True, text=True).stdout
        m = re.search(r"Footprint:\s*([\d.]+)\s*(KB|MB|GB)", out)
        if not m:
            return 0.0
        return float(m.group(1)) * {"KB": 1 / 1024, "MB": 1, "GB": 1024}[m.group(2)]

    def sample(self) -> None:
        procs = self.processes()
        for group, pattern in self.GROUPS.items():
            mine = [(pid, rss) for pid, rss, cmd in procs if re.search(pattern, cmd) and "grep" not in cmd
                    and "planner_eval" not in cmd]  # fmt: skip
            rss = sum(r for _, r in mine) / 1024
            foot = sum(self.footprint_mb(pid) for pid, _ in mine)
            self.peak[group]["rss_mb"] = max(self.peak[group]["rss_mb"], rss)
            self.peak[group]["footprint_mb"] = max(self.peak[group]["footprint_mb"], foot)

    def _loop(self) -> None:
        while not self.stop.is_set():
            self.sample()
            self.stop.wait(self.interval)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join()

    def total_gb(self, key: str = "footprint_mb") -> float:
        return sum(p[key] for p in self.peak.values()) / 1024


def run(model: str, view: str, repeats: int, mode: str = "json", jev: str | None = None,
        menu_actions: bool | None = None) -> dict:  # fmt: skip
    s = Settings.from_env(planner_model=model, planner_view=view, planner_mode=mode, jev=jev, menu_actions=menu_actions)
    selector = None
    if mode in ("kind", "jev"):
        from local_agent.selector.jev import make_selector

        selector = make_selector(s)
    planner = make_planner(s, selector)
    budget = view_budget(s.planner_num_ctx, s.planner_view_tokens)
    rows, times, prompt_tokens, kinds = [], [], [], []
    for case in CASES:
        snap = case.snap()
        screen = render(snap, budget, numbered=view == "ids", collapse_page=case.collapse) if view != "off" else None
        passes, reasons = 0, []
        for _ in range(repeats):
            t = time.monotonic()
            try:
                extra = {"snap": snap} if getattr(planner, "needs_snapshot", False) else {}
                out = planner.next_step(case.goal, case_scope(snap, s.menus_on(), case.progress, case.collapse), case.history,
                                        screen, **extra)
                reason = case.verdict(out, snap)
            except PlannerError as e:
                reason = f"invalid output: {e}"[:200]
            times.append(time.monotonic() - t)
            prompt_tokens.append(planner.last_prompt_tokens)
            kinds.append(getattr(planner, "last_kind", None))
            passes += reason == ""
            if reason:
                reasons.append(reason)
        na = case.jev_na() if mode == "jev" else ""
        rows.append({"case": case.name, "category": case.category, "needs_screen": case.needs_screen,
                     "passes": passes, "runs": repeats, "why": reasons[0] if reasons else "",
                     "kind": kinds[-1], "na": na, "expressible": not case.jev_na()})  # fmt: skip
    graded = [r for r in rows if not r["na"]]  # n/a rows are reported, not counted as failures
    total = len(graded) * repeats
    expressible = [r for r in rows if r["expressible"]]
    screen_rows = [r for r in graded if r["category"] == "screen"]
    return {
        "model": model,
        "mode": mode,
        "view": view,
        "rows": rows,
        "accuracy": sum(r["passes"] for r in graded) / total,
        "screen_accuracy": sum(r["passes"] for r in screen_rows) / (len(screen_rows) * repeats),
        "other_accuracy": sum(r["passes"] for r in graded if r["category"] != "screen")
        / ((len(graded) - len(screen_rows)) * repeats),
        "n_a": len(rows) - len(graded),
        "expressible": sum(r["passes"] for r in expressible) / (len(expressible) * repeats),
        "median_s": statistics.median(times),
        "p90_s": sorted(times)[int(0.9 * (len(times) - 1))],
        "prompt_tokens": statistics.median(prompt_tokens),
        "max_prompt_tokens": max(prompt_tokens),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("models", nargs="+")
    ap.add_argument("--views", default="off,names,ids", help="Comma-separated: off, names, ids.")
    ap.add_argument("--modes", default="json", help="Comma-separated planner modes: json, choice, kind, jev.")
    ap.add_argument("--ram", action="store_true", help="Sample the model processes' peak memory during each run.")
    ap.add_argument("--llama-port", type=int, help="With --ram: count only the llama-server on this port.")
    ap.add_argument("--jev", help="Catalog Jev model for --modes kind (default: what llama-server serves).")
    ap.add_argument("--menu-actions", action=argparse.BooleanOptionalAction, default=None,
                    help="Menu commands as a planner action (default off).")
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
        if modes != ["jev"]:  # jev mode has no planner model to load
            try:  # load weights before timing; the reply doesn't matter
                QwenPlanner(Settings.from_env(planner_model=model)).next_step("warm up", CASES[0].snap().scope(), [])
            except PlannerError:
                pass
        loaded = loaded_size_gb(s.ollama_url, model)
        level, free = pressure()
        for mode, view in [(m, v) for m in modes for v in views]:
            print(f"== {model}  mode={mode}  view={view}", flush=True)
            sampler = MemorySampler(llama_port=args.llama_port) if args.ram else None
            if sampler:
                sampler.__enter__()
            try:
                r = run(model, view, args.repeats, mode, args.jev, args.menu_actions)
            finally:
                if sampler:
                    sampler.__exit__()
            r.update(loaded_gb=loaded, pressure_level=level, free_pct=free)
            if sampler:
                r["peak"] = sampler.peak
                r["peak_footprint_gb"], r["peak_rss_gb"] = sampler.total_gb("footprint_mb"), sampler.total_gb("rss_mb")
            for row in r["rows"]:
                mark = "–" if row["na"] else "✓" if row["passes"] == row["runs"] else "✗"
                if row["na"]:
                    row["why"] = f"n/a ({row['na']} not offered as an option)"
                kind = f"{row['kind'][0]} {row['kind'][1]:.2f}" if row["kind"] else ""
                print(f"  {mark} {row['category']:10s} {row['case']:24s} {kind:14s} {row['why'][:100]}", flush=True)
            print(f"  accuracy {r['accuracy']:.0%} (screen {r['screen_accuracy']:.0%}, other {r['other_accuracy']:.0%})"
                  f"  median {r['median_s']:.2f}s  p90 {r['p90_s']:.2f}s  prompt ~{r['prompt_tokens']:.0f} tok"
                  f"  [n/a {r['n_a']}, expressible-only {r['expressible']:.0%}]", flush=True)  # fmt: skip
            if sampler:
                p = r["peak"]
                print("  peak memory (footprint / rss MB): " + ", ".join(
                    f"{g} {v['footprint_mb']:.0f}/{v['rss_mb']:.0f}" for g, v in p.items())
                    + f"  total {r['peak_footprint_gb']:.2f} GB footprint, {r['peak_rss_gb']:.2f} GB rss", flush=True)
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
