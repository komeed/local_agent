"""Measure Jev's goal-completion check (JevClient.goal_done) on evals/goal_cases.py.

    uv run python -m evals.goal_eval                    # Jev as configured (llama-server must be running)
    uv run python -m evals.goal_eval --jev jevk5-4b-q8  # a model from `local-agent jev list`
    uv run python -m evals.goal_eval --show gmail_sign_in   # print the state Jev reads for one case

Per case: Jev's probability and what the loop would do with it (stop at goal_done_threshold, hint the planner at
goal_hint_threshold, else continue). A *false stop* (a stop on a screen that isn't done) ends a run early; a *miss*
(continue on a done screen) only costs steps. The sweep shows both counts per stop threshold.
"""

from __future__ import annotations

import argparse
import json
import time

from evals.goal_cases import GOAL_CASES
from local_agent.config import Settings
from local_agent.selector.jev import JevError, goal_state, make_selector


def action(p: float, s: Settings) -> str:
    return "stop" if p >= s.goal_done_threshold else "hint" if p >= s.goal_hint_threshold else "continue"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jev", help="Jev model from `local-agent jev list` (default: what's configured).")
    ap.add_argument("--show", help="Print the state Jev reads for this case and exit.")
    ap.add_argument("--json", help="Write results here.")
    args = ap.parse_args()
    s = Settings.from_env(jev=args.jev)
    if args.show:
        case = next(c for c in GOAL_CASES if c.name == args.show)
        print(goal_state(case.goal, case.snap(), s.goal_view_tokens))
        return
    jev = make_selector(s)
    s = jev.settings  # with the catalog's calibration applied
    print(f"Jev: {s.jev or s.jev_backend}   stop ≥ {s.goal_done_threshold}   hint ≥ {s.goal_hint_threshold}\n")
    rows = []
    for case in GOAL_CASES:
        t = time.monotonic()
        try:
            p = jev.goal_done(case.goal, case.snap())
        except JevError as e:
            print(f"{case.name:24s} error {e}")
            continue
        if p is None:
            print(f"{s.jev or s.jev_backend} doesn't judge goal completion (planner and loop guard decide).")
            return
        secs = time.monotonic() - t
        act = action(p, s)
        verdict = ("FALSE STOP" if act == "stop" and not case.done else "miss" if act == "continue" and case.done
                   else "ok")  # fmt: skip
        print(f"{case.name:24s} {'done' if case.done else 'not done':8s} p={p:5.2f}  {act:8s} {verdict:10s} {secs:4.1f}s",
              flush=True)  # fmt: skip
        rows.append({"case": case.name, "done": case.done, "p": p, "action": act, "verdict": verdict, "seconds": secs})

    done = [r for r in rows if r["done"]]
    not_done = [r for r in rows if not r["done"]]
    print(f"\nfalse stops {sum(r['verdict'] == 'FALSE STOP' for r in rows)}/{len(not_done)} not-done screens   "
          f"stops on done screens {sum(r['action'] == 'stop' for r in done)}/{len(done)}   "
          f"misses {sum(r['verdict'] == 'miss' for r in rows)}/{len(done)}")
    print("\nstop threshold   false stops   done screens stopped")
    for th in (0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.98):
        fs = sum(r["p"] >= th for r in not_done)
        st = sum(r["p"] >= th for r in done)
        print(f"  {th:4.2f}           {fs}/{len(not_done)}           {st}/{len(done)}")
    if args.json:
        with open(args.json, "w") as f:
            json.dump(rows, f, indent=2)


if __name__ == "__main__":
    main()
