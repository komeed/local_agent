import pytest

from local_agent.observe.menus import MenuCommand, decode_keys, find_exact, menu_scope, read_commands
from local_agent.planner.schema import PlannerOutput
from tests.test_loop import DONE, FakeSelector, Outcome, P, make


class Node:
    def __init__(self, role, title="", children=(), **attrs):
        self.attrs = {"AXRole": role, "AXTitle": title, "AXChildren": list(children), **attrs}


def item(title, char=None, mods=None, vkey=None, enabled=True, sub=None):
    kids = [Node("AXMenu", children=sub)] if sub else []
    return Node("AXMenuItem", title, kids, AXMenuItemCmdChar=char, AXMenuItemCmdModifiers=mods,
                AXMenuItemCmdVirtualKey=vkey, AXEnabled=enabled)  # fmt: skip


def bar():
    file_menu = [item("New", "N", 0), item("Save As…", "S", 1), item(""), item("Revert", enabled=False),
                 item("Export", sub=[item("PDF", "P", 3), item("Plain", "T", 8)])]  # fmt: skip
    edit = [item("Undo", "Z", 0), item("Select Tool", None, 8, 36)]
    return Node("AXMenuBar", children=[Node("AXMenuBarItem", "File", [Node("AXMenu", children=file_menu)]),
                                       Node("AXMenuBarItem", "Edit", [Node("AXMenu", children=edit)])])  # fmt: skip


def attr(el, name):
    return el.attrs.get(name)


def test_decode_modifier_bitmask():
    assert decode_keys("S", 0, None) == "cmd+s"
    assert decode_keys("s", 1, None) == "cmd+shift+s"
    assert decode_keys("p", 3, None) == "cmd+option+shift+p"
    assert decode_keys("t", 8, None) == "t"  # bit 3: no cmd
    assert decode_keys("a", 4 | 2, None) == "cmd+ctrl+option+a"
    assert decode_keys(None, 8, 36) == "return"
    assert decode_keys("", None, None) is None


def test_menus_are_read_with_paths_keys_and_enabled_state():
    cmds = {c.path: c for c in read_commands(bar(), attr)}
    assert cmds["File > Save As…"].keys == "cmd+shift+s" and cmds["File > New"].keys == "cmd+n"
    assert cmds["File > Export > PDF"].keys == "cmd+option+shift+p"
    assert cmds["File > Export > Plain"].keys == "t" and cmds["Edit > Select Tool"].keys == "return"
    assert not cmds["File > Revert"].enabled and cmds["File > Revert"].keys is None
    assert "File > Export" not in cmds and all(c.path for c in cmds.values())  # submenu parents and separators skipped
    assert cmds["File > New"].bar_ref is not None and cmds["File > New"].ref is not None


def test_path_resolution_and_disabled_items_are_not_offered():
    cmds = read_commands(bar(), attr)
    assert find_exact("File > Save As…", cmds).path == "File > Save As…"
    assert find_exact("file > save as...", cmds).path == "File > Save As…"
    assert find_exact("Undo", cmds).path == "Edit > Undo"  # unique item name alone
    assert find_exact("File > Revert", cmds) is None
    assert "File > Revert" not in "\n".join(menu_scope(cmds))
    assert "File > Save As… [cmd+shift+s]" in menu_scope(cmds)


def test_large_menus_are_trimmed_by_the_goal():
    cmds = [MenuCommand(f"Menu{i % 5} > Item {i}", None) for i in range(400)] + [MenuCommand("File > Save As…", "cmd+s")]
    lines = menu_scope(cmds, "save the file as", limit=50)
    assert len(lines) <= 50 and "File > Save As… [cmd+s]" in lines and "File [menu]" in lines


def test_menu_action_validation():
    PlannerOutput(done=False, reasoning="r", action="menu", command="File > Save As…")
    with pytest.raises(ValueError):
        PlannerOutput(done=False, reasoning="r", action="menu")
    assert PlannerOutput(done=False, reasoning="r", action="menu", command="File > New").summary() == "menu menu='File > New'"


def test_loop_runs_menu_by_exact_path(textedit):
    ran = []
    textedit.menu_commands = read_commands(bar(), attr)
    agent, planner, executed, _ = make(textedit, [P(action="menu", command="File > Save As…"), DONE], menu_actions=True)
    agent.hooks.run_menu = lambda cmd: ran.append(cmd.path) or Outcome(True, f'menu "{cmd.path}" via {cmd.keys}')
    result = agent.run("g")
    assert ran == ["File > Save As…"] and executed == [] and result.history[0].note.startswith('menu "File > Save As…" via')
    assert "File > Save As… [cmd+shift+s]" in planner.scopes[0]["menu_commands"]


def test_loop_rejects_unknown_menu_path(textedit):
    textedit.menu_commands = read_commands(bar(), attr)
    agent, _, _, _ = make(textedit, [P(action="menu", command="File > Nonexistent"), DONE], max_steps=1)
    agent.selector = FakeSelector()  # no `choose`: no Jev fallback
    assert 'no enabled menu command "File > Nonexistent"' in agent.run("g").history[0].note


def test_executor_prefers_shortcut_then_axpress(monkeypatch):
    pytest.importorskip("ApplicationServices")
    from local_agent.act import executor

    calls = []
    monkeypatch.setattr(executor, "press_shortcut", lambda k: calls.append(("keys", k)))
    monkeypatch.setattr(executor, "AXUIElementPerformAction", lambda ref, a: calls.append(("press", ref)) or 0)
    r = executor.run_menu(MenuCommand("File > New", "cmd+n", ref="item"))
    assert r.ok and calls == [("keys", "cmd+n")] and "via cmd+n" in r.note
    calls.clear()
    r = executor.run_menu(MenuCommand("Help > Search", None, ref="item"))
    assert r.ok and calls == [("press", "item")] and r.note == 'menu "Help > Search" via AXPress'


def test_choice_planner_menu_kind_picks_from_enabled_paths(textedit):
    from local_agent.planner.choice import menu_paths

    lines = ["File > New [cmd+n]", "File > Save As… [cmd+shift+s]", "File [menu]", "Edit > Undo [cmd+z]"]
    assert menu_paths(lines, "g") == ["File > New", "File > Save As…", "Edit > Undo"]
    many = [f"M > Item {i}" for i in range(200)] + ["File > Save As… [cmd+s]"]
    paths = menu_paths(many, "save as", limit=60)
    assert len(paths) <= 60 and "File > Save As…" in paths


def test_json_planner_with_menu_actions_off_is_the_old_prompt():
    import json

    from local_agent.config import Settings
    from local_agent.planner.qwen import MENU_SYSTEM_PROMPT, SYSTEM_PROMPT, QwenPlanner, build_messages

    off = build_messages("g", {"app": "A", "menu_commands": ["File > New [cmd+n]"]}, [])
    assert off[0]["content"] == SYSTEM_PROMPT and "menu_commands" not in off[1]["content"]
    assert 'Common shortcuts: select all "cmd+a"' in SYSTEM_PROMPT and "Menus: menu items (AXMenuItem)" in SYSTEM_PROMPT
    assert "- menu:" not in SYSTEM_PROMPT
    on = build_messages("g", {"app": "A", "menu_commands": ["File > New [cmd+n]"]}, [], menus=True)
    assert on[0]["content"] == MENU_SYSTEM_PROMPT and "Common shortcuts" not in MENU_SYSTEM_PROMPT
    assert "- menu: needs command" in MENU_SYSTEM_PROMPT and json.loads(on[1]["content"])["menu_commands"]
    assert "menu" not in json.dumps(QwenPlanner(Settings()).schema["properties"]["action"])
    assert "menu" in json.dumps(QwenPlanner(Settings(menu_actions=True)).schema["properties"]["action"])
