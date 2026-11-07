"""Stage 4 gate: a developer completes an investigation and audits its citations through the UI,
in a real browser (Google Chrome, driven by Playwright). Uses the live stack and the real gateway
(real provider, runner in OpenShell). Saves a screenshot of each step.

Steps: sign in -> start an investigation for a recorded window (no fault label, only a symptom)
-> watch progress -> open the report -> follow every citation to its evidence (mouse and keyboard)
-> download the JSON and HTML exports -> check the HTML export has no scripts -> phone-width layout.

Usage: PYTHONPATH=src:. uv run python evaluation/browser_demo.py START END "symptom"
       (START and END in UTC, for example 2026-11-06T11:28:33)
Writes evaluation/results/stage4/browser-demo-<timestamp>/.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8088"


def main() -> int:
    start, end, symptom = sys.argv[1], sys.argv[2], sys.argv[3]
    out = ROOT / "evaluation/results/stage4" / ("browser-demo-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    out.mkdir(parents=True, exist_ok=True)
    secret = (ROOT / "deploy/local/access-secret.txt").read_text().strip()
    checks: dict[str, object] = {}
    dialogs: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        # Screenshots document each step (video recording would need an extra ffmpeg download).
        context = browser.new_context(viewport={"width": 1280, "height": 900}, accept_downloads=True)
        page = context.new_page()
        page.on("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.dismiss()))
        console_errors: list[str] = []
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)

        page.goto(BASE + "/")
        expect(page).to_have_url(BASE + "/login")
        page.fill("#secret", secret)
        page.click("#login-button")
        expect(page.locator("h1")).to_have_text("Start an investigation")
        expect(page.locator("#availability li").first).to_be_visible()
        checks["availability"] = page.locator("#availability").inner_text()
        page.screenshot(path=str(out / "1-start.png"), full_page=True)

        page.fill("#symptom", symptom)
        page.check("input[name=window-mode][value=absolute]")
        page.fill("#start", start)
        page.fill("#end", end)
        page.click("#investigate")
        page.wait_for_url(re.compile(r".*/investigations/[0-9a-f-]+$"))
        job_url = page.url
        checks["job"] = job_url.rsplit("/", 1)[-1]
        if page.locator("#progress").count():
            expect(page.locator("#timeline li").first).to_be_visible(timeout=30_000)
            page.screenshot(path=str(out / "2-progress.png"), full_page=True)
            checks["progress_seen"] = True
        page.wait_for_selector("#report", timeout=240_000)
        page.screenshot(path=str(out / "3-report.png"), full_page=True)
        checks["banner"] = page.locator(".banner").first.inner_text()
        checks["summary"] = page.locator(".summary").inner_text() if page.locator(".summary").count() else None

        # Audit every citation: each must open its saved observation, which must name the same handle.
        audited = []
        citations = page.locator(".findings a.cite")
        for index in range(citations.count()):
            link = citations.nth(index)
            handle = link.get_attribute("data-handle")
            link.click()
            item = page.locator(f"li#{handle}")
            expect(item.locator("details")).to_have_attribute("open", "")
            text = item.locator("pre").inner_text()
            assert f"[{handle}]" in text and f"saved as {handle}" in text, handle
            audited.append({"handle": handle, "coverage": item.locator(".coverage").first.inner_text(),
                            "first_line": text.splitlines()[0]})
            page.keyboard.press("Escape")
        checks["citations_audited"] = audited
        assert audited, "the report has no findings with citations"

        # Keyboard: tab to the first citation and follow it with Enter.
        page.locator("body").focus()
        for _ in range(60):
            page.keyboard.press("Tab")
            if page.evaluate("document.activeElement && document.activeElement.classList.contains('cite')"):
                break
        handle = page.evaluate("document.activeElement.dataset.handle")
        page.keyboard.press("Enter")
        expect(page.locator(f"li#{handle} details")).to_have_attribute("open", "")
        checks["keyboard_citation"] = handle
        page.screenshot(path=str(out / "4-evidence-open.png"), full_page=False)

        for fmt in ("json", "html"):
            with page.expect_download() as download:
                page.click(f"a[href$='format={fmt}']")
            path = out / f"export.{fmt}"
            download.value.save_as(str(path))
        exported = (out / "export.html").read_text()
        checks["html_export_has_script_tag"] = "<script" in exported.lower()
        checks["html_export_external_references"] = re.findall(r'(?:src|href)="(https?:[^"]+)"', exported)
        checks["json_export_report_outcome"] = json.loads((out / "export.json").read_text())["report"]["outcome"]

        page.set_viewport_size({"width": 390, "height": 844})
        page.reload()
        page.screenshot(path=str(out / "5-report-phone.png"), full_page=True)
        checks["phone_horizontal_overflow"] = page.evaluate(
            "document.documentElement.scrollWidth > document.documentElement.clientWidth")
        checks["dialogs"] = dialogs
        checks["console_errors"] = console_errors
        context.close()
        browser.close()

    (out / "checks.json").write_text(json.dumps(checks, indent=2) + "\n")
    print(json.dumps(checks, indent=2))
    ok = (not checks["html_export_has_script_tag"] and not checks["html_export_external_references"]
          and not dialogs and not checks["phone_horizontal_overflow"])
    print(f"{'PASSED' if ok else 'FAILED'}; wrote {out.relative_to(ROOT)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
