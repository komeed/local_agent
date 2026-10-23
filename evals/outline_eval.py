"""Quality of the outline the planner model writes for multi-step goals (evals.planner_cases.OUTLINE_CASES).

    uv run python -m evals.outline_eval qwen2.5-coder:3b [qwen3.5:9b ...]

Needs Ollama. For each case: the steps it returned, whether they contain the needed steps in a sensible order, and how
long the call took (with the model's load time, which is what `--low-memory` pays on every call)."""

import argparse

from evals.planner_cases import OUTLINE_CASES, outline_check
from local_agent.config import Settings
from local_agent.outline import Outliner


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("models", nargs="+")
    ap.add_argument("--low-memory", action="store_true", help="Unload after every call, so each call pays the load.")
    ap.add_argument("--backend", default="planner", choices=["planner", "jev"], help="Who writes it (jev: the JevK5 llama-server).")
    ap.add_argument("--repeats", type=int, default=1, help="Runs per case, to see nondeterminism.")
    args = ap.parse_args()
    for model in args.models:
        s = Settings.from_env(planner_model=model, low_memory=args.low_memory or None, outline_model=args.backend)
        outliner, passed = Outliner(s), 0
        print(f"== {model}  low_memory={s.low_memory}")
        for case in OUTLINE_CASES:
            runs = []
            for _ in range(args.repeats):
                items = outliner.outline(case.goal)
                runs.append((items, outline_check(items, case.must), outliner.last_seconds, outliner.last_load_s))
            items, why, secs, load = runs[0]
            passed += not why
            same = len({tuple(r[0]) for r in runs}) == 1
            note = "" if same else f"  [outlines differ between runs: {sum(1 for r in runs if r[1] == '')}/{len(runs)} pass]"
            print(f"  {'✓' if not why else '✗'} {case.name:16s} {secs:5.2f}s (load {load:4.2f}s) {why}{note}\n      "
                  + " → ".join(items))
        print(f"  {passed}/{len(OUTLINE_CASES)} outlines contain the needed steps in order")


if __name__ == "__main__":
    main()
