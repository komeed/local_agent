import json

import httpx
import pytest

from local_agent.config import JEV_MAX_CHOICES, Settings
from local_agent.planner.schema import PlannerOutput
from local_agent.selector.jev import JevClient, chunk, filter_candidates, parse_answers


def click(role, desc):
    return PlannerOutput(done=False, reasoning="r", action="click", target={"role": role, "description": desc})


def fake_jev(pick):
    """OpenJev stand-in: every choice question picks the option named by pick(options); records requests."""
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        answers = {}
        for qid, q in body["questions"].items():
            if q["type"] == "noul":
                answers[qid] = {"noul": 0.8}
                continue
            names = list(q["criteria"])
            choice = pick(names)
            probs = {n: (0.9 if n == choice else 0.1 / (len(names) - 1)) for n in names}
            answers[qid] = {"choice": choice, "probabilities": probs, "confidence": 0.7}
        return httpx.Response(200, json={"model": body["model"], "answers": answers, "usage": {}})

    client = httpx.Client(base_url="http://jev", transport=httpx.MockTransport(handler))
    return JevClient(Settings(), client=client), requests


@pytest.mark.parametrize("n", [1, 2, 255, 256, 511, 600, 1000])
def test_chunk_balanced(n):
    groups = chunk(list(range(n)), JEV_MAX_CHOICES)
    assert sum(len(g) for g in groups) == n
    assert all(len(g) <= JEV_MAX_CHOICES for g in groups)
    if n > JEV_MAX_CHOICES:
        assert all(len(g) >= 2 for g in groups)


def test_tournament_600(big_snapshot):
    target = "e437"
    jev, requests = fake_jev(lambda names: target if target in names else names[0])
    jev.settings.prefilter_top_k = 0
    sel = jev.select("press 437", big_snapshot, click("AXButton", "Button 437"))
    assert sel.element.id == 437
    assert sel.candidates == 600 and sel.rounds == 2
    first, final = requests
    assert len(first["questions"]) == 3
    for q in first["questions"].values():
        assert len(q["criteria"]) <= JEV_MAX_CHOICES
    assert len(final["questions"]) == 1 and len(final["questions"]["pick_0"]["criteria"]) == 3
    assert sel.probability == pytest.approx(0.9)


def test_single_round_when_small(textedit):
    jev, requests = fake_jev(lambda names: "e5" if "e5" in names else names[0])
    sel = jev.select("open file menu", textedit, click("AXMenuBarItem", "File menu"))
    assert sel.element.title == "File" and sel.rounds == 1
    assert set(requests[0]["questions"]["pick_0"]["criteria"]) == {"e5", "e6"}


def test_single_candidate_uses_noul(textedit):
    jev, requests = fake_jev(lambda names: names[0])
    step = PlannerOutput(
        done=False, reasoning="r", action="type", text="hi", target={"role": "AXTextArea", "description": "document body"}
    )
    sel = jev.select("type", textedit, step)
    assert sel.element.role == "AXTextArea" and sel.probability == pytest.approx(0.8)
    assert requests[0]["questions"]["is_target"]["type"] == "noul"


def test_filter_excludes_disabled_and_groups_roles(textedit):
    ids = {e.id for e in filter_candidates(textedit.elements, click("AXButton", "x"))}
    assert ids == {0, 1, 2}  # buttons + popup button, not the disabled Save
    assert {e.id for e in filter_candidates(textedit.elements, click("AXNope", "x"))} == {0, 1, 2, 3, 4, 5, 6}


def test_parse_answers_shapes():
    assert parse_answers({"answers": {"a": {"noul": 0.3}}}) == {"a": {"noul": 0.3}}
    assert parse_answers({"answers": [{"id": "a", "noul": 0.3}]})["a"]["noul"] == 0.3
    assert parse_answers({"answers": {"nouls": {"a": {"noul": 0.3}}}})["a"]["noul"] == 0.3


class FakeJevK5:
    """Stands in for jevk5.JevK5GGUF: picks options whose description mentions 'File'."""

    def __init__(self):
        self.calls = []

    def decide(self, state, q):
        self.calls.append(q)
        if q["type"] == "noul":
            return {"type": "noul", "noul": 0.9, "confidence": 0.9, "input_tokens": 10}
        names = list(q["criteria"])
        choice = next((n for n in names if "File" in q["criteria"][n]), names[0])
        probs = {n: (0.95 if n == choice else 0.05 / (len(names) - 1)) for n in names}
        return {"type": "choice", "choice": choice, "probabilities": probs, "confidence": 0.95, "input_tokens": 10}


def test_jevk5_backend_selects(textedit):
    from local_agent.selector.jev import JevK5Client

    model = FakeJevK5()
    jev = JevK5Client(Settings(), model=model)
    sel = jev.select("open file menu", textedit, click("AXMenuBarItem", "File menu"))
    assert sel.element.title == "File" and sel.probability == pytest.approx(0.95)
    assert jev.verify("g", click("AXMenuBarItem", "File"), sel.element, textedit, textedit) == pytest.approx(0.9)


def test_prefilter_shrinks_tournament(big_snapshot):
    jev, requests = fake_jev(lambda names: "e437" if "e437" in names else names[0])
    sel = jev.select("press 437", big_snapshot, click("AXButton", "Button 437"))
    assert sel.element.id == 437
    # "437" matches one title, "button" all of them (role + text): top 32 survive, one round.
    assert sel.candidates == 32 and sel.rounds == 1


def test_make_selector():
    from local_agent.selector.jev import JevK5Client, make_selector

    assert isinstance(make_selector(Settings(jev_backend="jevk5")), JevK5Client)
    assert type(make_selector(Settings(jev_backend="openjev"))) is JevClient
    with pytest.raises(ValueError):
        make_selector(Settings(jev_backend="nope"))
