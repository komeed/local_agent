import pytest

from local_agent.config import Settings
from local_agent.goal_facts import Fact, check_facts, parse_facts
from local_agent.observe.models import Snapshot, UIElement
from local_agent.planner.schema import PlannerOutput
from local_agent.selector.cosine import CosineClient
from local_agent.selector.jev import planner_pick

APPS = ["TextEdit", "Finder", "Mail", "Photos", "Music", "Maps", "Google Chrome"]


def test_parse_text_and_app_facts():
    facts = parse_facts('open a new document in textedit and type "hello world"', APPS)
    assert facts == [Fact("app", "TextEdit"), Fact("text", "hello world")]  # goal order
    assert parse_facts("open chrome", APPS) == [Fact("app", "Google Chrome")]
    assert parse_facts("Make a new TextEdit document", APPS) == [Fact("app", "TextEdit")]


def test_quoted_text_is_not_an_app():
    assert parse_facts('type "Mail" in the field', APPS) == [Fact("text", "Mail")]


@pytest.mark.parametrize("goal", ["send mail to bob", "copy photos to the desktop", "play some music", "find maps of x"])
def test_common_words_are_not_apps_unless_capitalised_or_after_open(goal):
    assert parse_facts(goal, APPS) == []


def test_common_word_apps_with_a_cue():
    assert parse_facts("open mail", APPS) == [Fact("app", "Mail")]
    assert parse_facts("Check Photos for a picture", APPS) == [Fact("app", "Photos")]


def snap(app="TextEdit", value="Hello world", role="AXTextArea"):
    return Snapshot(app=app, bundle_id="b", pid=1, window_title="Untitled", elements=[UIElement(id=1, role=role, value=value)])


def test_check_facts():
    facts = [Fact("text", "hello world"), Fact("app", "TextEdit")]
    assert check_facts(facts, snap())
    assert not check_facts(facts, snap(app="Finder"))
    assert not check_facts(facts, snap(value="nope"))
    assert not check_facts(facts, snap(role="AXStaticText"))  # only text fields / areas count
    assert not check_facts([], snap())


def _pick(snap, **target):
    return planner_pick(snap, PlannerOutput(done=False, reasoning="r", action="click", target=target))


def test_number_must_match_description():
    s = Snapshot(app="A", bundle_id="b", pid=1, window_title="w", elements=[
        UIElement(id=1, role="AXButton", subrole="AXIncrementPage"),
        UIElement(id=10, role="AXMenuBarItem", title="Edit"),
        UIElement(id=11, role="AXMenuBarItem", title="File"),
        UIElement(id=2, role="AXTextArea"),
    ])  # fmt: skip
    new = _pick(s, id=1, role="AXButton", description="New")
    assert new.selection is None and new.overruled == 1 and "does not match" in new.note
    file = _pick(s, id=10, role="AXMenuBarItem", description="File")
    assert file.overruled == 10 and file.selection.source == "exact" and file.selection.element.id == 11
    assert _pick(s, id=11, role="AXMenuBarItem", description="File").selection is not None
    area = _pick(s, id=2, role="AXTextArea", description="text area")  # role words match an unnamed field
    assert area.selection is not None and area.overruled is None


class FakeJudge:
    def goal_done(self, goal, snap):
        return 0.99

    def command_safety(self, *a):
        return 0.97

    def verify(self, *a):
        return 0.12


def test_cosine_judge_wiring():
    c = CosineClient(Settings())
    assert c.checks() == {"goal": False, "safety": False, "verify": "heuristic"}
    assert c.goal_done("g", snap()) is None and c.command_safety("g", None, None, []) is None
    c.judge = FakeJudge()
    assert c.checks() == {"goal": True, "safety": True, "verify": "jev"}
    assert c.goal_done("g", snap()) == 0.99 and c.command_safety("g", None, None, []) == 0.97
    assert c.verify("g", None, None, snap(), snap()) == 0.12


def test_make_selector_uses_judge_only_when_llama_server_is_healthy(monkeypatch):
    from local_agent.selector import jev

    class Fake:
        up = True

        def __init__(self, settings):
            pass

        def health(self):
            return Fake.up

    monkeypatch.setattr(jev, "JevK5Client", Fake)
    s = Settings(jev="cosine-qwen3")
    assert isinstance(jev.make_selector(s).judge, Fake)
    Fake.up = False
    assert jev.make_selector(s).judge is None


def test_warning_text_only_when_checks_missing():
    from local_agent.cli import checks_warning

    assert checks_warning({"goal": True, "safety": True, "verify": "jev"}) is None
    w = checks_warning({"goal": False, "safety": False, "verify": "heuristic"})
    assert "no goal check" in w and "every terminal command asks" in w and "heuristic" in w


def test_app_fact_alone_ends_only_a_bare_open_goal():
    from local_agent.goal_facts import decisive_facts

    apps = [*APPS, "Safari"]
    # A named site is a fact too (its URL must be on screen), so app + site is decisive.
    assert [f.kind for f in decisive_facts("Open ChatGPT in Safari", apps)] == ["app", "site"]
    assert decisive_facts("Open Safari", apps) == [Fact("app", "Safari")]
    assert decisive_facts("launch the Safari app", apps) == [Fact("app", "Safari")]
    assert decisive_facts('open a new document in textedit and type "hello world"', APPS) != []


SAVE_GOAL = 'open a new document in textedit and type "hello world" and save it as "helloworld.txt"'


def test_save_fact_parsed():
    facts = parse_facts(SAVE_GOAL, APPS)
    assert Fact("saved", "helloworld.txt") in facts and Fact("text", "hello world") in facts
    assert parse_facts("save as notes.md", APPS) == [Fact("saved", "notes.md")]


def test_full_goal_done_only_once_saved():
    from local_agent.goal_facts import decisive_facts

    facts = decisive_facts(SAVE_GOAL, APPS)
    assert len(facts) == 3
    typed = Snapshot(app="TextEdit", bundle_id="b", pid=1, window_title="Untitled",
                     elements=[UIElement(id=1, role="AXTextArea", value="Hello world")])  # fmt: skip
    assert not check_facts(facts, typed)
    for title in ("helloworld.txt", "helloworld", "HelloWorld.TXT"):
        saved = Snapshot(app="TextEdit", bundle_id="b", pid=1, window_title=title, elements=typed.elements)
        assert check_facts(facts, saved)


def test_coverage_rule():
    from local_agent.goal_facts import decisive_facts

    assert decisive_facts('type "hi" and send it', APPS) == []
    assert decisive_facts('type "hi" in TextEdit and print it', APPS) == []
    assert decisive_facts("Open Safari" , [*APPS, "Safari"]) != []
    assert [f.kind for f in decisive_facts("Open ChatGPT in Safari", [*APPS, "Safari"])] == ["app", "site"]
    assert decisive_facts("Open ChatGPT in Safari and sign in", [*APPS, "Safari"]) == []
    assert decisive_facts('open a new document in textedit and type "hello world"', APPS) != []
    assert decisive_facts('type "hello world"', APPS) != []


def _s(app, title, value=None):
    els = [] if value is None else [UIElement(id=1, role="AXTextArea", value=value)]
    return Snapshot(app=app, bundle_id="b", pid=1, window_title=title, elements=els)


def test_progress_checklist_at_each_stage():
    from local_agent.goal_facts import progress_scope

    facts = parse_facts(SAVE_GOAL, APPS, progress=True)
    assert [f.kind for f in facts] == ["app", "new", "text", "saved"]  # app first, then goal order
    opened = progress_scope(facts, _s("TextEdit", "old.txt", "old text"))
    assert opened["done"] == ["open TextEdit"] and opened["next"] == "make a new empty document"
    fresh = progress_scope(facts, _s("TextEdit", "Untitled", ""))
    assert fresh["done"] == ["open TextEdit", "make a new empty document"]
    assert fresh["next"] == 'type "hello world" into the document' and fresh["remaining"] == ['save the document as "helloworld.txt"']
    typed = progress_scope(facts, _s("TextEdit", "Untitled", "Hello world"))
    assert typed["next"] == 'save the document as "helloworld.txt"' and typed["remaining"] == []


def test_new_document_is_progress_only():
    assert all(f.kind != "new" for f in parse_facts(SAVE_GOAL, APPS))


def test_progress_reaches_every_planner():
    from local_agent.planner.choice import ChoicePlanner
    from local_agent.planner.kind import kind_state
    from local_agent.planner.qwen import build_messages

    scope = {"app": "TextEdit", "window": "Untitled",
             "progress": {"done": ["open TextEdit"], "next": '"hi" in text area', "remaining": ["x"]}}  # fmt: skip
    msgs = build_messages("g", scope, [])
    assert '"progress"' in msgs[1]["content"] and "never redo" in msgs[0]["content"]
    assert "progress" not in msgs[1]["content"].split('"progress"')[0]  # lifted out of current_scope
    plain = build_messages("g", {"app": "A"}, [])
    assert "progress" not in plain[1]["content"] and "never redo" not in plain[0]["content"]
    assert 'PROGRESS: done: open TextEdit; next: "hi" in text area; remaining: x' in kind_state("g", scope, [], None)
    assert "PROGRESS: done: open TextEdit" in ChoicePlanner._messages(
        ChoicePlanner.__new__(ChoicePlanner), "g", scope, [], None, "sys")[1]["content"]


def test_starting_state_never_comes_back_as_next():
    from local_agent.goal_facts import progress_scope

    facts = parse_facts(SAVE_GOAL, APPS, progress=True)
    seen: set = set()
    stages = [_s("Finder", "Desktop"), _s("TextEdit", "Untitled", ""), _s("TextEdit", "Untitled", "Hello world"),
              _s("TextEdit", "helloworld.txt", "Hello world")]  # fmt: skip
    nexts = [progress_scope(facts, s, seen) for s in stages]
    assert nexts[0]["next"] == "open TextEdit"
    assert all(n["next"] != "make a new empty document" for n in nexts[1:])
    assert "make a new empty document" in nexts[3]["done"]
    assert nexts[3]["next"] is None and nexts[3]["remaining"] == []
    # Without memory, the later facts alone also settle it.
    assert progress_scope(facts, stages[3])["next"] is None


def _doc(tmp_path, name, content, title=None, mtime=None):
    import os

    f = tmp_path / name
    f.write_text(content)
    if mtime is not None:
        os.utime(f, (mtime, mtime))
    return Snapshot(app="TextEdit", bundle_id="b", pid=1, window_title=title or name, elements=[],
                    document_path=str(f))  # fmt: skip


def test_saved_fact_checks_the_file(tmp_path):
    import time

    from local_agent.goal_facts import decisive_facts, failing_facts

    facts = decisive_facts(SAVE_GOAL, APPS)
    start = time.time() - 5
    good = _doc(tmp_path, "helloworld.txt", "Hello world")
    assert check_facts([f for f in facts if f.kind == "saved"], good, start) is True
    # Run A's end state: TextEdit appended .rtf and the empty second document was saved.
    rtf = _doc(tmp_path, "helloworld.txt.rtf", "")
    assert not check_facts(facts, rtf, start)
    assert failing_facts(facts, rtf, start).endswith('saved as "helloworld.txt.rtf"; the goal asks for "helloworld.txt"')
    empty = _doc(tmp_path, "helloworld.txt", "")
    assert 'doesn\'t contain "hello world"' in failing_facts(facts, empty, start)
    old = _doc(tmp_path, "helloworld.txt", "hello world", mtime=start - 1000)
    assert "was not written during this run" in failing_facts(facts, old, start)
    ok = _doc(tmp_path, "helloworld.txt", "{\\rtf1 Hello world}")
    assert "helloworld" not in failing_facts(facts, ok, start)  # content check is case-insensitive
    # No document path: an exact window title is all there is to compare.
    titled = Snapshot(app="TextEdit", bundle_id="b", pid=1, window_title="helloworld.txt", elements=[])
    assert not failing_facts([f for f in facts if f.kind == "saved"], titled, start)


def test_checklist_reads_as_actions():
    from local_agent.goal_facts import progress_scope

    p = progress_scope(parse_facts(SAVE_GOAL, APPS, progress=True), _s("Finder", "Desktop"))
    assert p["next"] == "open TextEdit"
    assert p["remaining"] == ["make a new empty document", 'type "hello world" into the document',
                              'save the document as "helloworld.txt"']  # fmt: skip


def test_text_fact_ignores_list_rows_and_has_no_evidence_without_editable_text():
    from local_agent.goal_facts import _holds

    row_field = UIElement(id=1, role="AXTextField", value="Untitledhello world",
                          path="AXWindow:Open > AXOutline > AXRow")  # fmt: skip
    snap_ = Snapshot(app="TextEdit", bundle_id="b", pid=1, window_title="Open", elements=[row_field])
    assert _holds(Fact("text", "hello world"), snap_) is None  # a file name is not the document
    area = UIElement(id=2, role="AXTextArea", value="nothing")
    snap_.elements.append(area)
    assert _holds(Fact("text", "hello world"), snap_) is False


def test_sheet_over_the_document_keeps_progress():
    from local_agent.goal_facts import progress_scope

    facts = parse_facts(SAVE_GOAL, APPS, progress=True)
    known: dict = {}
    typed = _s("TextEdit", "Untitled", "Hello world")
    progress_scope(facts, typed, set(), None, known)
    sheet = Snapshot(app="TextEdit", bundle_id="b", pid=1, window_title="", elements=[
        UIElement(id=1, role="AXButton", title="Save", path="AXSheet"),
    ])  # fmt: skip
    p = progress_scope(facts, sheet, set(), None, known)
    assert p["next"] == 'save the document as "helloworld.txt"'  # no evidence on screen: typing stays done
    assert 'type "hello world" into the document' in p["done"]
    # Never known: no evidence counts as not done.
    assert 'type "hello world" into the document' not in progress_scope(facts, sheet, set(), None, {})["done"]


NEW_APPS = [*APPS, "Claude", "ChatGPT", "Safari", "Notes"]


@pytest.mark.parametrize("goal,noun,app", [
    ("open new conversation in claude", "conversation", "Claude"),
    ("start a new chat in ChatGPT", "chat", "ChatGPT"),
    ("new tab in Safari", "tab", "Safari"),
    ("create a new note", "note", None),
])  # fmt: skip
def test_new_thing_facts(goal, noun, app):
    facts = parse_facts(goal, NEW_APPS, progress=True)
    assert Fact("new", noun) in facts and (app is None or Fact("app", app) in facts)
    assert all(f.kind != "new" for f in parse_facts(goal, NEW_APPS))  # progress-only unless decisive


@pytest.mark.parametrize("goal", ["read the news in Safari", "renew my license", "this is newer", "what's new"])
def test_new_is_not_matched_inside_other_words(goal):
    assert all(f.kind != "new" for f in parse_facts(goal, NEW_APPS, progress=True))


def test_app_plus_new_thing_is_decisive():
    from local_agent.goal_facts import decisive_facts, progress_scope

    facts = decisive_facts("open new conversation in claude", NEW_APPS)
    assert [f.kind for f in facts] == ["app", "new"]
    assert decisive_facts("open new conversation in claude and send hi", NEW_APPS) == []
    assert decisive_facts("create a new note", NEW_APPS) == []  # no app named: nothing to check it against
    snap_ = Snapshot(app="Claude", bundle_id="b", pid=1, window_title="Claude", elements=[])
    assert not check_facts(facts, snap_)  # nothing on screen says it is open
    assert check_facts(facts, snap_, None, {Fact("new", "conversation"): True})  # the loop saw its New click work
    assert progress_scope(facts, snap_)["next"] == "make a new conversation"
