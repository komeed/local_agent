"""Round-trip time of one trivial, read-only JS call per running browser: NSAppleScript vs osascript.

    uv run python -m evals.browser_timing [--n 20]
"""

import statistics
import subprocess
import time

import typer

from local_agent.act import browser

JS = "return document.title.length;"


def pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def main(n: int = typer.Option(20, help="Round trips per method.")):
    for bundle, name in browser.installed_browsers():
        if not browser.is_running(bundle):
            print(f"{name}: not running")
            continue
        if (st := browser.status(bundle)) != "ok":
            print(f"{name}: {st}")
            continue
        src = browser.script_for(browser.dialect(bundle), bundle, browser.wrap(JS, {}))
        runs = {
            "NSAppleScript": lambda: browser.run_js(bundle, JS),
            "osascript": lambda: subprocess.run(["osascript", "-e", src], capture_output=True, check=True),
        }
        for label, fn in runs.items():
            fn()  # warm-up (compile / process cache)
            ms = []
            for _ in range(n):
                t = time.perf_counter()
                fn()
                ms.append((time.perf_counter() - t) * 1000)
            print(f"{name:8} {label:14} p50 {statistics.median(ms):6.1f} ms  p90 {pct(ms, 0.9):6.1f} ms  (n={n})")


if __name__ == "__main__":
    typer.run(main)
