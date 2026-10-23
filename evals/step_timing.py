"""Time jev-mode steps with each speed-up switched on and off (fake actions on the synthetic planner-case screens).

    llama-server -m ~/models/jevk5/jevk5-4b-v0.3-Q4_K_M.gguf -c 8192 -ngl 99 --parallel 1 --cache-ram 0 --port 8092
    uv run python -m evals.step_timing --url http://127.0.0.1:8092 [--repeats 1] [--limit 12] [--json out.json]

One agent step per expressible planner case (the real `Agent`, `JevPlanner` and Jev server; clicks and keys are fake and
change the window title, as a working step would). Per configuration it reports the median / p90 of the whole step and of
its stages: planning, verify, goal check and settle, plus the Jev prompt tokens evaluated vs reused. The UI is modelled as
busy for `--ui-ms` (200) after each action, so the settle numbers show what polling saves against a fixed sleep; they are
not measured on a live desktop. The script refuses port 8091 (the user's own server).
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from dataclasses import replace
from urllib.parse import urlparse

from evals.planner_cases import CASES
from local_agent.config import Settings
from local_agent.loop import Agent, Hooks
from local_agent.planner.qwen import make_planner
from local_agent.selector.jev import make_selector

OFF = dict(code_verify=False, facts_only_goal_check=False, event_settle=False, jev_cache_prompt=False, jev_max_options=100)
CONFIGS = {
    "before": {},
    "+code_verify": {"code_verify": True},
    "+facts_goal": {"facts_only_goal_check": True},
    "+cap15": {"jev_max_options": 15},
    "+cache_prompt": {"jev_cache_prompt": True},
    "+event_settle": {"event_settle": True},
    "all": {"code_verify": True, "facts_only_goal_check": True, "event_settle": True, "jev_cache_prompt": True, "jev_max_options": 15},
}  # fmt: skip


class Outcome:
    def __init__(self, ok=True, note="ok"):
        self.ok, self.note = ok, note


def run_case(case, settings: Settings, selector, ui_s: float) -> dict:
    snap = case.snap()
    state = {"snap": snap, "acted": None}

    def execute(step, element):
        state["acted"] = time.monotonic()
        state["snap"] = replace(snap, window_title=snap.window_title + " (changed)")
        return Outcome()

    def fingerprint():
        t = state["acted"]
        busy = t is not None and time.monotonic() - t < ui_s
        return (state["snap"].window_title, int((time.monotonic() - (t or 0)) * 20) if busy else -1)

    hooks = Hooks(observe=lambda: state["snap"], execute=execute, run_terminal=lambda c: Outcome(), sleep=time.sleep,
                  fingerprint=fingerprint)
    agent = Agent(replace(settings, max_steps=1), make_planner(settings, selector), selector, hooks)
    take = getattr(selector, "take_stats", None)
    if take:
        take()
    t0 = time.monotonic()
    agent.run(case.goal)
    total = time.monotonic() - t0
    stats = take() if take else {}
    t = agent.timing
    return {"case": case.name, "total_s": total, "planner_s": t.get("planner_s", 0.0), "verify_s": t.get("verify_s", 0.0),
            "goal_s": t.get("goal_s", 0.0), "settle_s": t.get("settle_s", settings.settle_s if state["acted"] else 0.0),
            "prompt_n": stats.get("prompt_n", 0), "cache_n": stats.get("cache_n", 0), "jev_calls": stats.get("calls", 0),
            "verify_source": agent.history[0].verify_source if agent.history else ""}  # fmt: skip


def pct(values: list[float], q: float) -> float:
    values = sorted(values)
    return values[int(q * (len(values) - 1))] if values else 0.0


def summarize(rows: list[dict]) -> dict:
    out = {}
    for key in ("total_s", "planner_s", "verify_s", "goal_s", "settle_s"):
        vals = [r[key] for r in rows]
        out[key] = (round(statistics.median(vals), 2), round(pct(vals, 0.9), 2))
    out["prompt_n"] = round(statistics.mean(r["prompt_n"] for r in rows))
    out["cache_n"] = round(statistics.mean(r["cache_n"] for r in rows))
    out["jev_calls"] = round(statistics.mean(r["jev_calls"] for r in rows), 1)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8092")
    ap.add_argument("--configs", default=",".join(CONFIGS))
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0, help="Only the first N cases.")
    ap.add_argument("--ui-ms", type=float, default=200, help="Modelled time the UI is busy after an action.")
    ap.add_argument("--json", help="Write every row here.")
    args = ap.parse_args()
    if urlparse(args.url).port == 8091:
        raise SystemExit("port 8091 is the user's own llama-server; start your own on 8092")
    cases = [c for c in CASES if not c.jev_na()]
    cases = cases[: args.limit] if args.limit else cases
    results, rows_all = {}, {}
    for name in args.configs.split(","):
        settings = replace(Settings.from_env(planner_mode="jev", planner_view="ids", jevk5_url=args.url, outline=False),
                           **{**OFF, **CONFIGS[name]})  # fmt: skip
        selector = make_selector(settings)
        selector.system_one("warm up", {"q": {"type": "noul", "instructions": "x", "criteria": {"true": "a", "false": "b"}}})
        rows = [run_case(c, settings, selector, args.ui_ms / 1000) for _ in range(args.repeats) for c in cases]
        results[name], rows_all[name] = summarize(rows), rows
        print(f"{name:14s} " + "  ".join(f"{k}={v}" for k, v in results[name].items()), flush=True)
    print(f"\n{'config':14s}" + "".join(f"{k:>16s}" for k in ("total", "planning", "verify", "goal", "settle")) + "   prompt/cache tokens")
    for name, r in results.items():
        cells = "".join(f"{r[k][0]:>8.2f}/{r[k][1]:<7.2f}" for k in ("total_s", "planner_s", "verify_s", "goal_s", "settle_s"))
        print(f"{name:14s}{cells}   {r['prompt_n']}/{r['cache_n']}")
    if args.json:
        with open(args.json, "w") as f:
            json.dump({"summary": results, "rows": rows_all}, f, indent=1)


if __name__ == "__main__":
    main()
