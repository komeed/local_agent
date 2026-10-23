"""Turn the app's events .jsonl files into planner training examples (docs/TRAINING.md).

    uv run python -m evals.export_training ~/Documents/*.jsonl --only-success --dedupe --out train.jsonl
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def read_events(path: Path) -> list[dict]:
    events = []
    for line in path.read_text().splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue  # a truncated last line of a cancelled run
    return events


def examples(path: Path) -> list[dict]:
    """One example per `plan` event that carries its chat: messages without the model's reply, the plan as target, and
    whether the step worked (its `record`'s executed/verified; for a `done` plan, the run's `result`)."""
    events = read_events(path)
    start = next((e for e in events if e.get("type") == "start"), {})
    result = next((e for e in events if e.get("type") == "result"), {})
    records = {e["step"]: e for e in events if e.get("type") == "record" and "step" in e}
    out = []
    for e in events:
        if e.get("type") != "plan" or not e.get("messages"):
            continue
        plan = {k: v for k, v in e["plan"].items() if k != "summary"}
        if plan.get("done"):
            ok = bool(result.get("success"))
        else:
            rec = records.get(e["step"])
            ok = bool(rec and rec.get("executed") and rec.get("verified") is not False)
        header = re.match(r"App: (.+?) — window", e.get("screen") or "")
        out.append({
            "messages": e["messages"][:-1],  # the last message is the model's own reply
            "target": plan,
            "outcome": "success" if ok else "failed",
            "run": path.name,
            "step": e["step"],
            "app": header.group(1) if header else start.get("app"),
            "goal": start.get("goal"),
        })  # fmt: skip
    return out


def export(paths: list[Path], only_success: bool = False, dedupe: bool = False) -> list[dict]:
    rows, seen = [], set()
    for p in paths:
        for ex in examples(p):
            key = json.dumps(ex["messages"][-1:], sort_keys=True)  # the user message the model answered
            if only_success and ex["outcome"] != "success" or dedupe and key in seen:
                continue
            seen.add(key)
            rows.append(ex)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("files", nargs="+", type=Path, help="events .jsonl files from `run --events`")
    ap.add_argument("--only-success", action="store_true")
    ap.add_argument("--dedupe", action="store_true", help="keep one example per identical user message")
    ap.add_argument("--out", type=Path, default=Path("training.jsonl"))
    args = ap.parse_args()
    rows = export(args.files, args.only_success, args.dedupe)
    args.out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    print(f"wrote {len(rows)} examples to {args.out}")


if __name__ == "__main__":
    main()
