import json
import math

import httpx
import pytest

from evals.selector_cases import in_row, invoice_list, invoice_name
from local_agent.config import Settings
from local_agent.observe.models import Snapshot, UIElement
from local_agent.planner.schema import PlannerOutput
from local_agent.selector.cosine import CosineClient, overlap, softmax
from local_agent.selector.prefilter import words


VOCAB: dict[str, int] = {}


def bag_of_words(text: str) -> list[float]:
    """Stand-in embedding: word counts over a growing vocabulary, so texts sharing words are close."""
    v = [0.0] * 2048
    for w in words(text):
        v[VOCAB.setdefault(w, len(VOCAB))] += 1
    return v


def fake_ollama(models=("nomic-embed-text:latest",)):
    calls = []

    def handler(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": m} for m in models]})
        body = json.loads(request.content)
        calls.append(body["input"])
        return httpx.Response(200, json={"embeddings": [bag_of_words(t) for t in body["input"]]})

    client = httpx.Client(base_url="http://ollama", transport=httpx.MockTransport(handler))
    return CosineClient(Settings(jev_backend="cosine"), client=client), calls


def click(desc):
    return PlannerOutput(done=False, reasoning="r", action="click", target={"role": "AXButton", "description": desc})


def test_softmax_and_overlap():
    p = softmax([0.80, 0.70, 0.10], 0.03)
    assert math.isclose(sum(p), 1) and p[0] > 0.9
    assert overlap("Delete invoice-0425", 'AXButton title="Delete" row "invoice-0425.pdf"') == 1.0
    assert overlap("Delete invoice-0425", 'AXButton title="Delete" row "invoice-0424.pdf"') < 1.0


def test_picks_row_by_similarity_and_caches_embeddings():
    jev, calls = fake_ollama()
    snap = invoice_list()
    sel = jev.select("delete it", snap, click("Delete button for invoice-0425"))
    assert in_row(invoice_name(24), title="Delete")(sel.element)
    assert sel.margin > 0  # how sure is calibration: measured live by evals/selector_eval.py
    n = sum(len(c) for c in calls)
    jev.select("delete it", snap, click("Delete button for invoice-0425"))
    assert sum(len(c) for c in calls) == n  # second pick embedded nothing new


def test_nomic_gets_task_prefixes():
    jev, calls = fake_ollama()
    jev.similarities("Which UI element is: Save?", ["AXButton title=\"Save\""])
    assert calls[0][0].startswith("search_query: ") and calls[0][1].startswith("search_document: ")


def test_lone_candidate_and_health():
    jev, _ = fake_ollama()
    only = UIElement(id=1, role="AXButton", title="Save")
    sel = jev.choose("g", Snapshot("A", "a", 0, "W", [only]), click("Save button"), [only])
    assert sel.element is only and 0 < sel.probability <= 1
    assert jev.health()
    assert not fake_ollama(models=("qwen3.5:9b",))[0].health()


def test_noul_questions_rejected():
    jev, _ = fake_ollama()
    with pytest.raises(Exception, match="choice questions only"):
        jev.system_one("s", {"q": {"type": "noul", "instructions": "?"}})


def test_verify_heuristic(textedit):
    jev, _ = fake_ollama()
    step = click("Bold")
    assert jev.verify("g", step, None, textedit, textedit) == pytest.approx(0.1)
    changed = Snapshot.from_dict(textedit.to_dict())
    changed.elements[0].title = "different"
    assert jev.verify("g", step, None, textedit, changed) == pytest.approx(0.8)
    typed = PlannerOutput(done=False, reasoning="r", action="type", text="hello")
    changed.elements[0].value = "say hello world"
    assert jev.verify("g", typed, None, textedit, changed) == pytest.approx(0.95)
