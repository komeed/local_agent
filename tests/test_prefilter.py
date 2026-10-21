from local_agent.observe.models import UIElement
from local_agent.selector.prefilter import prefilter, score, words


def btn(i, **kw):
    return UIElement(id=i, role=kw.pop("role", "AXButton"), **kw)


def test_words_splits_roles_and_drops_stopwords():
    assert words("AXCloseButton") == ["close", "button"]
    assert words("the Send button in the toolbar") == ["send", "button", "toolbar"]


def test_text_beats_role(textedit):
    close, bold = textedit.elements[0], textedit.elements[3]
    assert score("close button", close)[0] > 0
    assert score("close button", bold)[0] == 0


def test_keeps_best_matches_in_original_order():
    els = [btn(i, title=f"Item {i}") for i in range(100)]
    els[70].title = "Send"
    els[10].label = "Send later"
    kept = prefilter("Send button", els, top_k=5)
    assert len(kept) == 5
    assert els[70] in kept and els[10] in kept
    assert [e.id for e in kept] == sorted(e.id for e in kept)


def test_fuzzy_and_prefix_match():
    els = [btn(i, title="Other") for i in range(50)] + [btn(50, label="Preferences…")]
    assert els[50] in prefilter("preference settings", els, top_k=3)


def test_no_text_match_returns_everything():
    els = [btn(i, label="") for i in range(50)]
    assert prefilter("red close button", els, top_k=10) == els


def test_small_or_disabled_lists_untouched():
    els = [btn(i, title="x") for i in range(5)]
    assert prefilter("y", els, top_k=32) == els
    assert prefilter("x", [btn(i, title="x") for i in range(50)], top_k=0) == [btn(i, title="x") for i in range(50)]
