from local_agent.observe.models import Snapshot


def test_round_trip(textedit):
    assert Snapshot.from_dict(textedit.to_dict()) == textedit


def test_scope_is_compact(textedit):
    scope = textedit.scope()
    assert scope["app"] == "TextEdit"
    assert scope["element_counts_by_role"]["AXButton"] == 3
    assert "AXTextArea" in scope["focused_element"]
    assert "elements" not in scope


def test_describe_is_bounded(textedit):
    for e in textedit.elements:
        assert 0 < len(e.describe()) <= 160
    assert "disabled" in textedit.elements[7].describe()
