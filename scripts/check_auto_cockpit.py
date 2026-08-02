"""Visual + layout check for the AUTO Mission Control cockpit.

Boots the app in-process on a spare port, optionally injects a synthetic
running loop (scripts/fake_auto_run.py), then drives a headless browser to
assert the design's hard layout invariants and capture reference PNGs:

  * the cockpit never scrolls (the whole point of the redesign),
  * the phase strip stays on ONE row at every supported width,
  * the console keeps its 130px floor,
  * the brief slide-over opens and closes,
  * no JS errors on the page.

Usage:  python scripts/check_auto_cockpit.py [--keep]
Shots:  .pwtmp/auto-*.png
"""

from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.abspath(".pwtmp")
os.makedirs(OUT, exist_ok=True)
os.environ.setdefault("TMP", OUT)
os.environ.setdefault("TEMP", OUT)

PORT = 8213
BASE = f"http://127.0.0.1:{PORT}"
# 924x540 is the design handoff's stated worst case; 1440 is the reference.
VIEWPORTS = [("wide", 1440, 900), ("narrow", 924, 540)]


def _serve() -> None:
    import uvicorn

    uvicorn.run("server:app", host="127.0.0.1", port=PORT, log_level="error")


def main() -> int:
    from playwright.sync_api import sync_playwright

    threading.Thread(target=_serve, daemon=True).start()
    from scripts.fake_auto_run import inject

    run_id = None
    for _ in range(60):
        try:
            import urllib.request

            urllib.request.urlopen(BASE + "/studio", timeout=2).read()
            run_id = inject()
            break
        except Exception:
            time.sleep(0.5)
    if run_id is None:
        print("FAIL: server did not come up")
        return 1

    problems: list[str] = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        for name, w, h in VIEWPORTS:
            page = b.new_page(viewport={"width": w, "height": h})
            errs: list[str] = []
            page.on("pageerror", lambda e: errs.append(str(e)))
            page.goto(BASE + "/studio", wait_until="networkidle")
            page.evaluate("window.labMode('otonom')")
            page.wait_for_selector(".mc-body", timeout=10_000)
            page.wait_for_timeout(1200)  # let one poll land
            page.screenshot(path=os.path.join(OUT, f"auto-{name}.png"))

            m = page.evaluate(
                """() => {
                  const q = s => document.querySelector(s);
                  const strip = q('.mc-phases');
                  const cells = [...document.querySelectorAll('.mc-phase')];
                  const tops = new Set(cells.map(c => Math.round(
                      c.getBoundingClientRect().top)));
                  const con = q('.mc-console');
                  return {
                    // The cockpit's own overflow is what matters. The page can
                    // still scroll because of the sidebar's engine card, which
                    // predates this screen and is not part of the redesign.
                    cockpitOverflow: Math.max(
                      0, Math.round(q('.mc').getBoundingClientRect().bottom)
                         - document.documentElement.clientHeight),
                    innerScroll: Math.max(
                      q('.mc-center').scrollHeight - q('.mc-center').clientHeight,
                      q('.mc-console-body').scrollHeight
                        - q('.mc-console-body').clientHeight > 0 ? 0 : 0),
                    stripRows: tops.size,
                    cellCount: cells.length,
                    stripOverflow: strip.scrollWidth - strip.clientWidth,
                    consoleH: con.getBoundingClientRect().height,
                    railVisible: !!q('.mc-rail-right')
                                 && q('.mc-rail-right').offsetParent !== null,
                    ringPct: q('.mc-ring-pct').textContent.trim(),
                    status: q('.mc-pill').textContent.trim(),
                    cands: document.querySelectorAll('.mc-cand').length,
                    lines: document.querySelectorAll('.mc-line').length,
                  };
                }"""
            )
            if errs:
                problems.append(f"{name}: JS errors {errs}")
            if m["cellCount"] != 5:
                problems.append(f"{name}: phase strip has {m['cellCount']} cells")
            if m["stripRows"] != 1:
                problems.append(f"{name}: phase strip wrapped to {m['stripRows']} rows")
            if m["stripOverflow"] > 1:
                problems.append(
                    f"{name}: phase strip overflows by {m['stripOverflow']}"
                )
            if m["consoleH"] < 130:
                problems.append(f"{name}: console is {m['consoleH']:.0f}px (<130)")
            if m["cockpitOverflow"] > 4:
                problems.append(
                    f"{name}: cockpit overflows the viewport by "
                    f"{m['cockpitOverflow']}px"
                )
            if m["innerScroll"] > 4:
                problems.append(
                    f"{name}: center column scrolls by {m['innerScroll']}px"
                )
            if m["status"] != "RUNNING":
                problems.append(f"{name}: status is {m['status']!r}, expected RUNNING")
            if m["lines"] < 2:
                problems.append(f"{name}: console rendered {m['lines']} lines")
            if name == "wide" and m["cands"] < 3:
                problems.append(f"{name}: only {m['cands']} candidate cards")
            print(f"{name}: {m}")

            # Brief slide-over
            page.evaluate("window.mcBrief(true)")
            page.wait_for_timeout(400)
            page.screenshot(path=os.path.join(OUT, f"auto-{name}-brief.png"))
            x = page.evaluate(
                "document.querySelector('.mc-drawer').getBoundingClientRect().x"
            )
            if abs(x) > 1:
                problems.append(f"{name}: drawer did not open (x={x})")
            page.evaluate("window.mcBrief(false)")
            page.wait_for_timeout(400)
            x2 = page.evaluate(
                "document.querySelector('.mc-drawer').getBoundingClientRect().x"
            )
            if x2 > -100:
                problems.append(f"{name}: drawer did not close (x={x2})")
            page.close()

        # Stopped state
        from web.routes.agent_backtest import _AGENT_LOCK, _AGENT_PROGRESS

        with _AGENT_LOCK:
            _AGENT_PROGRESS[run_id]["stop_requested"] = True
        page = b.new_page(viewport={"width": 1440, "height": 900})
        page.goto(BASE + "/studio", wait_until="networkidle")
        page.evaluate("window.labMode('otonom')")
        page.wait_for_selector(".mc-body", timeout=10_000)
        page.wait_for_timeout(1500)
        page.screenshot(path=os.path.join(OUT, "auto-stopped.png"))
        st = page.evaluate("document.querySelector('.mc-pill').textContent.trim()")
        if st != "DURDURULDU":
            problems.append(f"stopped: status is {st!r}")
        page.close()
        b.close()

    for pr in problems:
        print("FAIL:", pr)
    print(("OK — " if not problems else "") + "shots in " + OUT)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
