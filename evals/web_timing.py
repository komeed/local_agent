"""Latency and element counts of the page outline on the recorded fixture pages, live in a browser.

    uv run python -m evals.web_timing [--browser Arc] [--n 20]

Opens each evals/fixtures/web page as a new tab (like capture_web_fixtures), then runs web_outline.js `n` times and
prints p50/p90 round-trip time plus: candidate nodes on the page, elements listed (after the viewport filter) and the
controls reported as beyond the viewport. Read-only; no other tab is touched.
"""

import statistics
import time

import typer

from evals.capture_web_fixtures import DIR, capture
from local_agent.act import browser
from local_agent.observe import web


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def main(browser_name: str = typer.Option("Arc", "--browser"), n: int = typer.Option(20)):
    bundle = next(b for b, nm in browser.KNOWN_BROWSERS.items() if nm.lower() == browser_name.lower())
    if not browser.is_running(bundle) or browser.status(bundle) != "ok":
        raise typer.BadParameter(f"{browser_name} is not ready: {browser.status(bundle)}")
    allms = []
    print(f"{'page':22} {'p50 ms':>7} {'p90 ms':>7} {'nodes':>6} {'listed':>6} {'more':>5}")
    for path in sorted(DIR.glob("*.html")):
        if path.stem == "web_heavy":  # the stress page (evals/web_stress.py), not a recorded case
            continue
        first = capture(bundle, browser_name, path)
        ms = []
        for _ in range(n):
            t = time.perf_counter()
            web.read_outline(bundle)
            ms.append((time.perf_counter() - t) * 1000)
        allms += ms
        print(f"{path.stem:22} {statistics.median(ms):7.1f} {pct(ms, 0.9):7.1f} {first['candidates']:6} "
              f"{first['listed']:6} {first['more']:5}")
    print(f"{'all pages':22} {statistics.median(allms):7.1f} {pct(allms, 0.9):7.1f}")


if __name__ == "__main__":
    typer.run(main)
