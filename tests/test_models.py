from local_agent.observe.models import Snapshot


def test_round_trip(textedit):
    assert Snapshot.from_dict(textedit.to_dict()) == textedit


def test_to_dict_skips_uncopyable_ref(textedit):
    class Uncopyable:
        def __reduce_ex__(self, protocol):
            raise TypeError("Cannot pickle Objective-C objects")

    el = textedit.elements[0]
    el.ref = Uncopyable()
    d = el.to_dict()
    assert "ref" not in d and d["frame"]["w"] == 14
    assert textedit.to_dict()["elements"][0]["label"] == "close button"


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


def test_scope_lists_open_menu_items(textedit):
    from local_agent.observe.models import UIElement

    assert "open_menu_items" not in textedit.scope()
    textedit.elements += [UIElement(id=90, role="AXMenuItem", title="Font"), UIElement(id=91, role="AXMenuItem", title="Text")]
    assert textedit.scope()["open_menu_items"] == ["Font", "Text"]


def test_selection_is_visible_to_jev(textedit):
    area = textedit.elements[4]
    before = area.describe()
    area.selected = "Hello world"
    assert area.describe() != before and 'selected="Hello world"' in area.describe()
