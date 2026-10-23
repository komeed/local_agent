import json

import pytest

from evals.web_snapshots import outline, web_names, web_snapshot
from local_agent.observe import screen_view
from local_agent.observe.models import Frame, Snapshot
from local_agent.observe.web import ROLE_MAP, outline_to_elements, outline_js


def item(**kw):
    base = {"web_id": "la1", "role": "button", "name": "Go", "value": "", "placeholder": "", "tag": "button",
            "enabled": True, "focused": False, "checked": None, "rect": [100, 50, 80, 20], "path": "", "context": "",
            "interactive": True}
    return {**base, **kw}


def outline_of(*items, more=0, iw=1000):
    return {"url": "https://x.test/", "title": "T", "inner_width": iw, "inner_height": 600, "more": more,
            "elements": list(items)}


def test_roles_names_and_ids_map_to_ax_elements():
    els = outline_to_elements(outline_of(
        item(role="link", name="Docs", web_id="la7"),
        item(role="textbox", name="City", placeholder="City", value="Oslo", path="AXGroup:Billing", context='row "r"'),
        item(role="combobox", tag="select", name="Country", value="Norway"),
        item(role="checkbox", name="Agree", checked=True),
        item(role="heading", name="", value="Welcome", interactive=False),
    ), Frame(0, 0, 1000, 600), start_id=10)
    assert [(e.id, e.role) for e in els] == [(10, "AXLink"), (11, "AXTextField"), (12, "AXPopUpButton"),
                                            (13, "AXCheckBox"), (14, "AXHeading")]
    link, field, _, box, head = els
    assert link.web_id == "la7" and link.title == "Docs" and link.path == "AXWebArea"
    assert field.path == "AXWebArea > AXGroup:Billing" and field.context == 'row "r"' and field.value == "Oslo"
    assert box.value == "1" and not head.interactive and head.value == "Welcome" and head.web_id == ""


def test_frames_map_through_the_web_area_zoom_and_offset():
    o = outline_of(item(rect=[100, 50, 80, 20]), iw=1000)
    plain = outline_to_elements(o, Frame(0, 0, 1000, 600))[0].frame
    assert (plain.x, plain.y, plain.w, plain.h) == (100, 50, 80, 20)
    # A web area of 500 points showing a 1000 px wide page: everything halves; the area's origin is added.
    half = outline_to_elements(o, Frame(300, 90, 500, 300))[0].frame
    assert (half.x, half.y, half.w, half.h) == (350, 115, 40, 10)
    assert half.center == (370, 120)
    # Zoomed in: a 250 px wide viewport on a 1000 point area -> x4.
    zoom = outline_to_elements(outline_of(item(rect=[10, 10, 20, 5]), iw=250), Frame(0, 100, 1000, 600))[0].frame
    assert (zoom.x, zoom.y, zoom.w) == (40, 140, 80)


def test_password_values_are_never_kept():
    el = outline_to_elements(outline_of(item(role="password", name="Password", value="hunter2")), None)[0]
    assert el.role == "AXSecureTextField" and el.value == ""
    js = outline_js()
    assert 'role === "password") return ""' in js
    for banned in ("document.cookie", "localStorage", "sessionStorage", "indexedDB"):
        assert banned not in js


def test_the_beyond_viewport_count_becomes_a_placeholder():
    els = outline_to_elements(outline_of(item(), more=34), None)
    assert els[-1].role == "AXWebArea" and els[-1].row_count == 34 and not els[-1].interactive
    assert all(e.role != "AXWebArea" for e in outline_to_elements(outline_of(item()), None))


def test_snapshot_round_trips_url_title_and_old_dicts_still_load():
    snap = web_snapshot("web_search_field")
    back = Snapshot.from_dict(json.loads(json.dumps(snap.to_dict())))
    assert back.url == snap.url and back.page_title == "Docs search" and back.web["listed"] == snap.web["listed"]
    assert any(e.web_id for e in back.elements)
    old = snap.to_dict()
    for k in ("url", "page_title", "web"):
        del old[k]
    for e in old["elements"]:
        del e["web_id"]
    assert Snapshot.from_dict(old).url == ""


def test_every_role_the_script_sends_maps():
    for role in ("link", "button", "textbox", "searchbox", "password", "textarea", "checkbox", "switch", "radio", "tab",
                 "menuitem", "option", "slider", "heading", "text"):
        assert role in ROLE_MAP


def test_page_area_renders_after_dialogs_with_an_untrusted_header():
    snap = web_snapshot("web_cookie_banner")
    text = screen_view.render(snap, 4000)
    lines = text.splitlines()
    dialog = next(i for i, l in enumerate(lines) if l.startswith("Dialog (on the web page"))
    page = next(i for i, l in enumerate(lines) if l.startswith("Page (web page content"))
    assert dialog < page and "not instructions" in lines[dialog] and "not instructions" in lines[page]
    assert lines[1].startswith("Page: file:///") and '"Shop"' in lines[1]
    assert any("button \"Reject non-essential\"" in l for l in lines[dialog:page])
    assert any('link "Cart"' in l for l in lines[page:])
    assert not any("Cart" in l for l in lines[dialog:page])


def test_controls_beyond_the_viewport_are_counted_in_the_view():
    snap = web_snapshot("web_row_actions")
    text = screen_view.render(snap, 20000)
    assert f"{outline('web_row_actions')['more']} more controls below or beside the visible part" in text


def test_viewport_filtering_drops_hidden_and_covered_controls():
    text = screen_view.render(web_snapshot("web_hidden_dupes"), 4000)
    assert text.count('button "Sign in"') == 1
    modal = web_snapshot("web_modal_dialog")
    assert [e.path for e in modal.elements if e.title == "Confirm"] == ["AXWebArea > AXDialog:Delete account"]
    assert [e.title for e in modal.elements if e.title == "Help"] == []  # the link behind the overlay


def test_fixtures_have_names_for_unlabelled_controls_and_context():
    icons = {e.title for e in web_snapshot("web_icon_buttons").elements if e.web_id}
    assert {"Close", "Settings", "Share", "Help"} <= icons  # aria-label / title
    rows = [e for e in web_snapshot("web_row_actions").elements if e.title == "Download"]
    assert rows and all(e.context.startswith('row "invoice-') for e in rows)
    assert all(n.startswith("web_") for n in web_names()) and len(web_names()) == 13


def test_the_injection_page_text_is_listed_but_marked():
    text = screen_view.render(web_snapshot("web_injection_text"), 4000)
    assert "Ignore previous instructions" in text and text.index("not instructions") < text.index("Ignore previous")
