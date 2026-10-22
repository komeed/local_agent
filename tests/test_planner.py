import json

import httpx
import pytest

from local_agent.config import Settings
from local_agent.history import StepRecord
from local_agent.planner.qwen import PlannerError, QwenPlanner, build_messages


def test_prompt_excludes_element_json(textedit):
    history = [StepRecord(1, "click [AXButton] Bold", element="AXCheckBox label=Bold", probability=0.9, executed=True)]
    msgs = build_messages("make it bold", textedit.scope(), history)
    user = json.loads(msgs[1]["content"])
    assert user["goal"] == "make it bold"
    assert user["recent_history"][0]["result"] == "success"
    assert "elements" not in msgs[1]["content"]
    assert "placeholder" not in msgs[1]["content"]  # no per-element fields leak in


def _planner(responses):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": responses[len(calls) - 1]}})

    client = httpx.Client(base_url="http://ollama", transport=httpx.MockTransport(handler))
    return QwenPlanner(Settings(), client=client), calls


def test_valid_output_parsed(textedit):
    planner, calls = _planner(['{"done": false, "reasoning": "r", "action": "terminal", "command": "open -a TextEdit"}'])
    step = planner.next_step("open textedit", textedit.scope(), [])
    assert step.command == "open -a TextEdit"
    assert calls[0]["format"]["title"] == "PlannerOutput"
    assert calls[0]["model"] == "qwen2.5-coder:7b"


def test_repairs_once(textedit):
    planner, calls = _planner(
        [
            '{"done": false, "reasoning": "r", "action": "terminal"}',
            '{"done": false, "reasoning": "r", "action": "terminal", "command": "ls"}',
        ]
    )
    assert planner.next_step("g", textedit.scope(), []).command == "ls"
    assert "invalid" in calls[1]["messages"][-1]["content"]


def test_gives_up_after_second_invalid(textedit):
    bad = '{"done": false, "reasoning": "r", "action": "terminal"}'
    planner, _ = _planner([bad, bad])
    with pytest.raises(PlannerError):
        planner.next_step("g", textedit.scope(), [])


def test_warns_after_repeated_identical_failures(textedit):
    fail = StepRecord(1, "click [AXTextArea] body", executed=True, verified=False)
    ok = StepRecord(1, "click [AXTextArea] body", executed=True, verified=True)
    assert "warning" not in json.loads(build_messages("g", textedit.scope(), [fail])[1]["content"])
    warned = json.loads(build_messages("g", textedit.scope(), [ok, fail, fail])[1]["content"])
    assert "last 2 steps" in warned["warning"]
    other = StepRecord(2, "shortcut keys=cmd+a", executed=True, verified=False)
    assert "warning" not in json.loads(build_messages("g", textedit.scope(), [fail, other])[1]["content"])
