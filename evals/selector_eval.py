"""Measure element selection on evals/selector_cases.py: the current pre-filter vs the previous one (top_k cut in
document order, own text only, last path segment in Jev's option text).

    uv run python -m evals.selector_eval            # pre-filter only: recall, candidates kept, ties, JevK5 passes
    uv run python -m evals.selector_eval --live     # also ask Jev (LOCAL_AGENT_JEV_BACKEND, default jevk5)
    uv run python -m evals.selector_eval --live --jev cosine-nomic   # a model from `local-agent jev list`

Offline columns: `recall` = the right element reached Jev; `unique` = its option text (minus coordinates) differs from
every other option Jev sees, i.e. Jev can tell it apart without guessing from positions.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import time
from dataclasses import dataclass
from typing import Callable

from evals.selector_cases import CASES, PICK_CASES, PickCase, SelectorCase
from local_agent.config import Settings
from local_agent.observe.models import UIElement
from local_agent.selector.jev import JevError, filter_candidates, make_selector, planner_pick
from local_agent.selector.prefilter import ROLE_WEIGHT, _best, rank, words

CONFIGS = ("baseline", "new")


# --- the selector before ties and surroundings, kept here so the comparison stays reproducible ------------------


def legacy_prefilter(target: str, candidates: list[UIElement], top_k: int) -> list[UIElement]:
    if top_k <= 0 or len(candidates) <= top_k:
        return candidates
    query = set(words(target))

    def score(e: UIElement) -> tuple[float, float]:
        text_words = words(" ".join([e.title, e.label, e.placeholder, e.value[:60], e.help]))
        role_words = words(f"{e.role} {e.subrole}")
        return sum(_best(q, text_words) for q in query), sum(_best(q, role_words) for q in query) * ROLE_WEIGHT

    scored = [(score(e), i) for i, e in enumerate(candidates)]
    if max(s[0] for s, _ in scored) == 0:
        return candidates
    ranked = sorted(scored, key=lambda t: (-(t[0][0] + t[0][1]), t[1]))[:top_k]
    return [candidates[i] for _, i in sorted(ranked, key=lambda t: t[1])]


def legacy_describe(e: UIElement) -> str:
    stripped = UIElement.from_dict({**e.to_dict(), "context": "", "path": e.path.split(" > ")[-1]})
    text = stripped.describe(160)
    # The old text named the last path segment raw ("in AXGroup:Billing address").
    return re.sub(r'in (\w+) "([^"]*)"', lambda m: f"in AX{m[1]}:{m[2]}", text)


# --- runs ----------------------------------------------------------------------------------------------------------


@dataclass
class Run:
    kept: list[UIElement]
    expected: set[int]
    describe: Callable[[UIElement], str]
    tie_size: int = 0
    truncated: bool = False

    @property
    def recall(self) -> bool:
        return any(e.id in self.expected for e in self.kept)

    @property
    def unique(self) -> bool:
        strip = lambda e: re.sub(r" at \(-?\d+,-?\d+\)", "", self.describe(e))  # noqa: E731
        others = {strip(e) for e in self.kept if e.id not in self.expected}
        return any(strip(e) not in others for e in self.kept if e.id in self.expected)

    @property
    def passes(self) -> int:
        """JevK5 forward passes for one choice question: ceil(n/16)+1 (none for a lone candidate's noul)."""
        n = len(self.kept)
        return 1 if n <= 1 else math.ceil(n / 16) + 1


def prefilter_run(case: SelectorCase, config: str, s: Settings) -> Run:
    snap = case.snap()
    pool = filter_candidates(snap.elements, case.step)
    target = case.step.target.description
    expected = {e.id for e in snap.elements if case.expect(e)}
    if config == "baseline":
        return Run(legacy_prefilter(target, pool, s.prefilter_top_k), expected, legacy_describe)
    r = rank(target, pool, s.prefilter_top_k, s.prefilter_max_k)
    return Run(r.kept, expected, UIElement.describe, r.tie_size, r.truncated)


def passed(case: SelectorCase, run: Run) -> bool:
    return run.truncated if case.expect_truncated else run.recall


def pick_run(case: PickCase) -> dict:
    """What the ambiguity guard does with the planner's number: keep it, or hand the step to Jev."""
    snap = case.snap()
    picked = next(e for e in snap.elements if case.picked(e))
    step = case.step(picked.id)
    pick = planner_pick(snap, step)
    to_jev = pick.selection is None
    row = {"case": case.name, "to_jev": to_jev, "guard_ok": to_jev == case.expect_jev}
    if not to_jev:
        row["correct"] = case.expect(pick.selection.element)
    return row


def run_picks(jev) -> list[dict]:
    rows = []
    print(f"\n{'number pick':24s} {'guard':6s} {'action':12s}" + ("  final" if jev else ""))
    for case in PICK_CASES:
        row = pick_run(case)
        line = f"{case.name:24s} {'✓' if row['guard_ok'] else '✗':6s} {'→ jev' if row['to_jev'] else 'keep number':12s}"
        if row["to_jev"] and jev is not None:
            snap = case.snap()
            step = case.step(next(e for e in snap.elements if case.picked(e)).id)
            try:
                sel = jev.select(case.goal, snap, step)
                row["correct"] = case.expect(sel.element)
                line += f"  {'✓' if row['correct'] else '✗'} p={sel.probability:.2f}"
            except JevError as e:
                row["correct"] = False
                line += f"  error {str(e)[:40]}"
        elif "correct" in row:
            line += f"  {'✓' if row['correct'] else '✗'}" if jev else ""
        print(line, flush=True)
        rows.append(row)
    ok = sum(r["guard_ok"] for r in rows)
    summary = f"guard {ok}/{len(rows)}"
    final = [r for r in rows if "correct" in r]
    if jev is not None:
        summary += f"  final pick right {sum(r['correct'] for r in final)}/{len(final)}"
    print(summary)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="Also ask Jev to pick (needs llama-server / OpenJev).")
    ap.add_argument("--jev", help="Jev model for --live, from `local-agent jev list` (default: what's configured).")
    ap.add_argument("--json", help="Write full results here.")
    args = ap.parse_args()
    s = Settings.from_env(jev=args.jev)
    jev = make_selector(s) if args.live else None
    if jev is not None:
        print(f"Jev: {jev.settings.jev or jev.settings.jev_backend}\n")

    results = []
    print(f"{'case':24s} {'config':8s} {'recall':6s} {'unique':6s} {'kept':>4s} {'tie':>4s} {'trunc':5s} {'passes':>6s}"
          + ("  jev   p     margin  time" if jev else ""))  # fmt: skip
    for case in CASES:
        for config in CONFIGS:
            run = prefilter_run(case, config, s)
            row = {"case": case.name, "config": config, "passed": passed(case, run), "recall": run.recall,
                   "unique": run.unique, "kept": len(run.kept), "tie": run.tie_size, "truncated": run.truncated,
                   "passes": run.passes}  # fmt: skip
            line = (f"{case.name:24s} {config:8s} {'✓' if row['passed'] else '✗':6s} {'✓' if run.unique else '·':6s} "
                    f"{len(run.kept):4d} {run.tie_size:4d} {'yes' if run.truncated else '':5s} {run.passes:6d}")  # fmt: skip
            if jev is not None and not case.expect_truncated:
                snap = case.snap()  # fresh copy: Jev answers by option name, ids line up with `run`
                kept = [next(e for e in snap.elements if e.id == k.id) for k in run.kept]
                t = time.monotonic()
                try:
                    sel = jev.choose(case.goal, snap, case.step, kept, run.describe)
                    ok = sel.element.id in run.expected
                    row.update(jev_ok=ok, probability=sel.probability, margin=sel.margin)
                    line += f"  {'✓' if ok else '✗'}  {sel.probability:5.2f}  {sel.margin:5.2f}"
                except JevError as e:
                    row.update(jev_ok=False, error=str(e)[:200])
                    line += f"  error {str(e)[:40]}"
                row["seconds"] = time.monotonic() - t
                line += f"  {row['seconds']:5.1f}s"
            print(line, flush=True)
            results.append(row)

    print()
    for config in CONFIGS:
        rows = [r for r in results if r["config"] == config]
        summary = f"{config:8s} passed {sum(r['passed'] for r in rows)}/{len(rows)}"
        summary += f"  unique {sum(r['unique'] for r in rows)}/{len(rows)}"
        summary += f"  median kept {sorted(r['kept'] for r in rows)[len(rows) // 2]}"
        live = [r for r in rows if "jev_ok" in r]
        if live:
            summary += f"  jev {sum(r['jev_ok'] for r in live)}/{len(live)}"
        print(summary)
    picks = run_picks(jev)
    if args.json:
        with open(args.json, "w") as f:
            json.dump({"select": results, "picks": picks}, f, indent=2)


if __name__ == "__main__":
    main()
