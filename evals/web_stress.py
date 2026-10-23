"""Time the browser calls on the heavy local page (~8k DOM nodes, mutating every 50 ms, an iframe), live in Arc.

    uv run python -m evals.web_stress

Opens evals/fixtures/web/web_heavy.html in two new tabs (one sync `make new tab`, one fire-and-forget) and closes
them again; measures list_tabs, the page outline, a full ax_tree.snapshot() with the browser in front, and
settle. Only the tabs it creates are touched.
"""

import statistics
import time
from pathlib import Path

from local_agent.act import browser
from local_agent.config import Settings
from local_agent.observe import web

B = "company.thebrowser.Browser"
URL = (Path(__file__).parent / "fixtures" / "web" / "web_heavy.html").resolve().as_uri()


def timed(label, fn, n=5):
    ms = []
    for _ in range(n):
        t = time.perf_counter()
        try:
            r = fn()
        except browser.BrowserError as e:
            r = f"ERR {e.kind}"
        ms.append((time.perf_counter() - t) * 1000)
    print(f"{label:34} p50 {statistics.median(ms):8.1f} ms  max {max(ms):8.1f} ms  -> {str(r)[:60]}")


def mine():
    return [t for t in browser.list_tabs(B) if t.url.endswith("web_heavy.html")]


def close(tabs):
    for t in tabs:
        browser._execute(f'tell application id "{B}" to tell window {t.window} to tell tab id "{t.id}" to close', "action")


def main():
    from local_agent.observe import ax_tree

    assert browser.status(B) == "ok"
    before = {browser.tab_key(t) for t in browser.list_tabs(B)}
    t = time.perf_counter()
    browser._execute(f'tell application id "{B}" to tell front window to make new tab with properties {{URL:"{URL}"}}', "action")
    print(f"sync make new tab returned after {(time.perf_counter() - t) * 1000:.0f} ms")
    time.sleep(2)
    t = time.perf_counter()
    tab = browser.open_tab(B, URL)
    print(f"fire-and-forget open_tab (send + poll until seen) {(time.perf_counter() - t) * 1000:.0f} ms; seen={tab is not None}")
    time.sleep(2)
    timed("list_tabs (heavy tab active)", lambda: len(browser.list_tabs(B)))
    timed("page outline run_js", lambda: len(web.read_outline(B)["elements"]))
    ax_tree.activate_app("Arc")  # a background tab's timers are throttled; the page only mutates at full rate in front
    time.sleep(1.5)
    timed("settle (mutating page, cap 3 s)", lambda: round(browser.settle(B), 2), 2)
    timed("full ax_tree.snapshot()",
          lambda: (lambda s: (s.app, len(s.elements), s.web, s.url[-20:]))(ax_tree.snapshot(Settings())), 3)
    close([x for x in mine() if browser.tab_key(x) not in before])
    print("tabs left from this run:", len([x for x in mine() if browser.tab_key(x) not in before]))


if __name__ == "__main__":
    main()
