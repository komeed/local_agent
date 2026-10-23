import json

import pytest

from local_agent.act import browser as b

NASTY = ['a"b', "back\\slash", "line\nbreak\r\ttab", "</script>", "  ", "é🙂", '"; do shell script "x" --', "\\", '\\"', ""]


def unescape_applescript(lit: str) -> str:
    out, i = [], 0
    while i < len(lit):
        if lit[i] == "\\":
            i += 1
        out.append(lit[i])
        i += 1
    return "".join(out)


@pytest.mark.parametrize("s", NASTY)
def test_escaping_round_trips_through_an_applescript_literal(s):
    js = b.wrap("return args.s;", {"s": s})
    lit = b.escape_applescript(js)
    assert '"' not in lit.replace('\\"', "")  # every quote is escaped
    assert unescape_applescript(lit) == js
    payload = js[js.index("})(") + 3 : -3]  # the JSON args between "})(" and "})"
    assert json.loads(payload) == {"s": s}
    assert "\n" in js and " " not in js and "</" not in payload


def test_wrap_calls_the_body_with_args_and_returns_json():
    js = b.wrap("return args.n+1;", {"n": 1})
    assert js.startswith("JSON.stringify({r: (function(args){return args.n+1;") and js.endswith('})({"n": 1})})')


def test_script_dialects():
    assert b.script_for("safari", "com.apple.Safari", 'x"').endswith('do JavaScript "x\\"" in current tab of front window')
    c = b.script_for("chromium", "company.thebrowser.Browser", "1")
    assert c == 'tell application id "company.thebrowser.Browser" to execute active tab of front window javascript "1"'


def test_dialect_from_sdef():
    assert b.dialect_from_sdef('<command name="do JavaScript" code="x">') == "safari"
    assert b.dialect_from_sdef('<command name="execute" code="y"><parameter name="javascript"/>') == "chromium"
    assert b.dialect_from_sdef('<command name="quit"/>') is None


def test_dialect_is_probed_once_per_bundle(monkeypatch):
    calls = []
    monkeypatch.setattr(b, "app_path", lambda bid: calls.append(bid) or "/x.app")

    class R:
        stdout = '<command name="do JavaScript">'

    monkeypatch.setattr(b.subprocess, "run", lambda *a, **k: R)
    b.dialect.cache_clear()
    assert b.dialect("t.safari") == b.dialect("t.safari") == "safari"
    assert calls == ["t.safari"]
    b.dialect.cache_clear()


@pytest.mark.parametrize(
    "info,kind",
    [
        ({"NSAppleScriptErrorNumber": -1743, "NSAppleScriptErrorMessage": "Not authorized to send Apple events to Arc."}, "no_permission"),
        ({"NSAppleScriptErrorNumber": 12, "NSAppleScriptErrorMessage": "Executing JavaScript through AppleScript is turned off. To turn it on, from the menu bar, go to View > Developer > Allow JavaScript from Apple Events."}, "js_disabled"),
        ({"NSAppleScriptErrorNumber": 8, "NSAppleScriptErrorMessage": "You must enable 'Allow JavaScript from Apple Events' in the Develop menu."}, "js_disabled"),
        ({"NSAppleScriptErrorNumber": -1728, "NSAppleScriptErrorMessage": "Safari got an error: Can’t get current tab of window 1."}, "no_window"),
        ({"NSAppleScriptErrorNumber": -1719, "NSAppleScriptErrorMessage": "Can’t get window 1. Invalid index."}, "no_window"),
        ({"NSAppleScriptErrorNumber": -600, "NSAppleScriptErrorMessage": "Application isn’t running."}, "not_running"),
        ({"NSAppleScriptErrorNumber": -2740, "NSAppleScriptErrorMessage": "syntax error"}, "other"),
        (None, "other"),
    ],
)
def test_error_kinds(info, kind):
    assert b.classify_error(info).kind == kind


def test_run_js_decodes_plain_and_double_encoded_results(monkeypatch):
    monkeypatch.setattr(b, "is_running", lambda _: True)
    monkeypatch.setattr(b, "dialect", lambda _: "chromium")
    for raw in ['{"r":"2"}', json.dumps('{"r":"2"}')]:
        monkeypatch.setattr(b, "_execute", lambda _s, kind="read", raw=raw: raw)
        assert b.run_js("x", "return '2';") == "2"


def test_run_js_not_running(monkeypatch):
    monkeypatch.setattr(b, "is_running", lambda _: False)
    with pytest.raises(b.BrowserError) as e:
        b.run_js("x", "return 1;")
    assert e.value.kind == "not_running"


def test_doctor_lines_with_a_fake_status():
    states = {"s": "ok", "c": "js_disabled", "a": "no_permission", "w": "no_window"}
    lines = b.doctor_lines(
        [("s", "Safari"), ("c", "Chrome"), ("a", "Arc"), ("w", "Brave"), ("n", "Edge")],
        probe=states.get, dialect_of=lambda bid: "chromium" if bid == "c" else "safari",
        running=lambda bid: bid != "n",
    )
    text = [l.text() for l in lines]
    assert text[0] == "Safari: ok"
    assert text[1].startswith("Chrome: JS off — View › Developer › Allow JavaScript from Apple Events")
    assert text[2].startswith("Arc: no permission — System Settings › Privacy & Security › Automation")
    assert text[3].startswith("Brave: no window")
    assert text[4].startswith("Edge: not running")
