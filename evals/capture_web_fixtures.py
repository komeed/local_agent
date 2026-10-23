"""Record the page outline of each evals/fixtures/web/*.html as <case>.json, in Arc (or any browser in KNOWN_BROWSERS).

    uv run python -m evals.capture_web_fixtures [--browser Arc] [--only web_composer]

Each file is opened as a new tab of the front window (AppleScript `make new tab`; `open -a Arc <file>` did nothing), the script waits until the tab's
title is the file's <title> and the page is loaded, then runs web_outline.js once. Nothing is clicked or typed and no
other tab is read. The tabs stay open. The outline depends on the window size, so the JSON records innerWidth/Height.
"""

import json
import re
import subprocess
import time
from pathlib import Path

import typer

from local_agent.act import browser
from local_agent.observe import web

DIR = Path(__file__).parent / "fixtures" / "web"


def title_of(path: Path) -> str:
    m = re.search(r"<title>(.*?)</title>", path.read_text())
    return m.group(1) if m else ""


def capture(bundle: str, name: str, path: Path, timeout_s: float = 15.0) -> dict:
    # `open -a Arc <file>` opened nothing; a new tab in the front window works (chromium dialect: Arc, Chrome, ...).
    url = browser.escape_applescript(path.resolve().as_uri())
    subprocess.run(["osascript", "-e", f'tell application id "{bundle}" to tell front window to make new tab '
                    f'with properties {{URL:"{url}"}}'], check=True, capture_output=True)
    want, deadline = title_of(path), time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        time.sleep(0.4)
        try:
            # A yes/no only, so a tab that isn't ours is never read.
            ready = browser.run_js(bundle, "return document.readyState === 'complete' && document.title === args.t && "
                                   "location.href.endsWith(args.f);", {"t": want, "f": path.name})
        except browser.BrowserError:
            continue
        if ready is True:
            outline = web.read_outline(bundle)
            outline["url"] = f"file:///evals/fixtures/web/{path.name}"  # no local paths in the checked-in JSON
            return outline
    raise TimeoutError(f"{path.name}: the tab did not show {want!r}")


def main(
    browser_name: str = typer.Option("Arc", "--browser"),
    only: str = typer.Option("", help="Capture just this case."),
):
    bundle = next((b for b, n in browser.KNOWN_BROWSERS.items() if n.lower() == browser_name.lower()), None)
    if bundle is None or not browser.is_running(bundle):
        raise typer.BadParameter(f"{browser_name} must be running (this script never launches or quits a browser)")
    if (st := browser.status(bundle)) != "ok":
        raise typer.BadParameter(f"{browser_name}: {st}")
    for path in sorted(DIR.glob("*.html")):
        if path.stem == "web_heavy":  # the stress page (evals/web_stress.py), not a recorded case
            continue
        if only and path.stem != only:
            continue
        outline = capture(bundle, browser_name, path)
        (DIR / f"{path.stem}.json").write_text(json.dumps(outline, ensure_ascii=False, indent=1) + "\n")
        print(f"{path.stem}: {len(outline['elements'])} listed, {outline['candidates']} candidates, "
              f"{outline['more']} more; viewport {outline['inner_width']}x{outline['inner_height']}")


if __name__ == "__main__":
    typer.run(main)
