import json

from evals.export_training import export


def events(tmp_path):
    msgs = lambda n: [{"role": "system", "content": "s"}, {"role": "user", "content": f"u{n}"},
                      {"role": "assistant", "content": "{}"}]  # fmt: skip
    plan = lambda n, **kw: {"type": "plan", "step": n, "plan": {"done": False, "action": "click", "summary": "x", **kw},
                            "screen": 'App: TextEdit — window "Untitled"', "messages": msgs(n)}  # fmt: skip
    lines = [
        {"type": "start", "goal": "g", "app": None},
        plan(1), {"type": "record", "step": 1, "executed": True, "verified": True},
        plan(2), {"type": "record", "step": 2, "executed": True, "verified": False},
        {"type": "plan", "step": 3, "plan": {"done": True, "summary": "done"}, "screen": None, "messages": msgs(1)},
        {"type": "result", "success": True},
    ]  # fmt: skip
    p = tmp_path / "run.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n{truncated")
    return p


def test_export_outcomes_and_filters(tmp_path):
    rows = export([events(tmp_path)])
    assert [(r["step"], r["outcome"]) for r in rows] == [(1, "success"), (2, "failed"), (3, "success")]
    first = rows[0]
    assert first["messages"][-1]["content"] == "u1" and len(first["messages"]) == 2  # the model's reply is dropped
    assert first["target"] == {"done": False, "action": "click"} and first["app"] == "TextEdit" and first["goal"] == "g"
    assert [r["step"] for r in export([events(tmp_path)], only_success=True)] == [1, 3]
    assert [r["step"] for r in export([events(tmp_path)], dedupe=True)] == [1, 2]  # step 3 repeats step 1's user message
