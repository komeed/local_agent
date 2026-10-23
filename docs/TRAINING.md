# Planner training data

`evals/export_training.py` turns the events files that `run --events` writes (the app saves them as
`~/Documents/*.jsonl`) into one JSONL example per planner step:

```bash
uv run python -m evals.export_training ~/Documents/*.jsonl --only-success --dedupe --out train.jsonl
```

Each line: `{"messages": [system, user, ...], "target": <the plan JSON>, "outcome": "success"|"failed", "run", "step",
"app", "goal"}`. `messages` is the chat the planner saw, without its own reply; `target` is what it answered.
`outcome` comes from the step's `record` (executed and not verified-false), or the run's `result` for a `done` step.

- `--only-success` drops failed steps; `--dedupe` keeps one example per identical user message.
- To teach a better answer for a failed step, keep the line and edit `target` to the plan it should have given
  (same fields as the `plan` event), then set `"outcome": "success"`.
- Held-out eval cases (`evals/planner_cases.py` and its fixtures) must not be in the training set: filter out their
  goals before training, or the eval numbers mean nothing.
