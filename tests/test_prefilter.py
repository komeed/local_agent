from local_agent.observe.models import UIElement
from local_agent.selector.prefilter import prefilter, rank, score, words


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


def test_keeps_whole_tie_at_cutoff():
    els = [btn(i, title="Delete") for i in range(50)]
    kept = prefilter("Delete button", els, top_k=32, max_k=128)
    assert kept == els  # all 50 score the same; none dropped by document order


def test_tie_capped_and_reported():
    els = [btn(i, title="Delete") for i in range(200)]
    r = rank("Delete button", els, top_k=32, max_k=128)
    assert len(r.kept) == 128 and r.tie_size == 200 and r.truncated
    assert r.kept == els[:128]


def test_better_matches_survive_truncation():
    els = [btn(i, title="Delete") for i in range(200)]
    els[190].context = 'row "invoice-0425"'
    r = rank("Delete invoice-0425", els, top_k=32, max_k=64)
    assert els[190] in r.kept and r.truncated


def test_filler_cutoff_not_widened():
    els = [btn(i, title="Other") for i in range(50)] + [btn(50, title="Send")]
    r = rank("Send button", els, top_k=5, max_k=128)
    assert len(r.kept) == 5 and els[50] in r.kept and not r.truncated


def test_context_breaks_the_tie():
    els = [btn(i, title="Delete", context=f'row "invoice-{401 + i:04d}"') for i in range(50)]
    target = els[37]
    assert score("Delete button for invoice-0438", target)[1] > score("Delete button for invoice-0438", els[36])[1]
    r = rank("Delete button for invoice-0438", els, top_k=1, max_k=128)
    assert r.kept == [target]


def test_icon_button_found_by_its_row():
    els = [btn(i, context=f'row "photo {i}"') for i in range(60)]
    kept = prefilter("trash button in the photo 47 row", els, top_k=1)
    assert kept == [els[47]]  # no own text at all: before, every icon button came back


def test_window_title_is_not_context():
    els = [btn(i, path="AXWindow:Invoices > AXGroup") for i in range(50)]
    assert prefilter("Invoices button", els, top_k=5) == els  # nothing but the window matches: no signal


def test_missing_name_raises_in_select_but_role_only_descriptions_do_not():
    import pytest

    from local_agent.config import Settings
    from local_agent.observe.models import Snapshot, UIElement
    from local_agent.planner.schema import PlannerOutput
    from local_agent.selector.jev import JevClient, JevError
    from local_agent.selector.prefilter import missing_name

    els = [UIElement(id=1, role="AXButton", subrole="AXFullScreenButton"), UIElement(id=2, role="AXButton", title="Cancel"),
           UIElement(id=3, role="AXTextArea")]  # fmt: skip
    snap = Snapshot(app="A", bundle_id="b", pid=1, window_title="w", elements=els)

    def step(desc, role="any", action="click"):
        return PlannerOutput(done=False, reasoning="r", action=action, text="x" if action == "type" else None,
                             target={"role": role, "description": desc})  # fmt: skip

    class Probe(JevClient):
        def __init__(self):
            self.settings = Settings()

        def choose(self, goal, snap, step, candidates, describe=None):
            return candidates

    with pytest.raises(JevError, match='no element named "New Document" on screen'):
        Probe().select("g", snap, step("New Document"))
    assert Probe().select("g", snap, step("text area", "AXTextArea", "type")) == [els[2]]
    assert Probe().select("g", snap, step("the trash icon in the invoice-0441 row"))  # no specific name: fallback
    assert missing_name('the "Cancel" button', els) == ""  # exists
    assert missing_name("Text Area", els) == ""  # role words only


def test_exact_name_wins_without_a_model():
    import pytest

    from local_agent.config import Settings
    from local_agent.observe.models import Snapshot, UIElement
    from local_agent.planner.schema import PlannerOutput
    from local_agent.selector.jev import JevClient, planner_pick

    def snap(els):
        return Snapshot(app="A", bundle_id="b", pid=1, window_title="w", elements=els)

    def step(desc, **t):
        return PlannerOutput(done=False, reasoning="r", action="click", target={"description": desc, **t})

    class NoModel(JevClient):
        def __init__(self):
            self.settings = Settings()

        def choose(self, *a, **k):
            raise AssertionError("asked a model")

    menu = snap([UIElement(id=1, role="AXMenuBarItem", title="File", path="AXMenuBar"),
                 UIElement(id=2, role="AXMenuBarItem", title="Edit", path="AXMenuBar"),
                 UIElement(id=3, role="AXMenuItem", title="Share", path="AXMenuBar > AXMenuBarItem:File > AXMenu"),
                 UIElement(id=4, role="AXMenuItem", title="Close", path="AXMenuBar > AXMenuBarItem:File > AXMenu")])  # fmt: skip
    sel = NoModel().select("g", menu, step("File"))
    assert sel.element.id == 1 and sel.source == "exact" and sel.probability == 1.0
    assert NoModel().select("g", menu, step('the "File" menu')).element.id == 1
    # A wrong number for "File" goes straight to the File menu bar item.
    pick = planner_pick(menu, step("File", id=2))
    assert pick.overruled == 2 and pick.selection.element.id == 1 and pick.selection.source == "exact"

    rows = snap([UIElement(id=i, role="AXButton", title="Delete", context=f'row "f{i}"') for i in range(50)])
    with pytest.raises(AssertionError, match="asked a model"):
        NoModel().select("g", rows, step("Delete"))
