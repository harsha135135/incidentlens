"""Browser smoke check and escaping (spec §15, §17), in real headless Google Chrome via Playwright,
against the real gateway and supervisor processes with the test-only fake provider. The symptom and
the model's report carry HTML and script payloads: nothing may execute, everything must appear as
text, and the HTML export must contain no scripts and load nothing."""

from __future__ import annotations

import re

from playwright.sync_api import expect, sync_playwright

from incidentlens.config import PROJECT_ROOT
from tests.process.conftest import PUBLIC, create_job, job_row, wait_until

PAYLOAD = '<script>alert("x")</script><img src=x onerror=alert(1)>'


def hostile_report() -> dict:
    return {"reply": "tool", "name": "submit_report", "input": {
        "outcome": "TENTATIVE", "summary": f"Summary with {PAYLOAD}",
        "findings": [{"claim": f"Claim {PAYLOAD}", "support": "observed", "evidence_ids": ["E1"],
                      "reason": "<b>bold?</b> javascript:alert(2)"}],
        "alternatives": [], "limitations": [f"Limitation {PAYLOAD}"], "next_checks": []}}


def test_the_ui_runs_an_investigation_and_escapes_everything(owner, client, supervisor, fake, tmp_path):
    fake.set_script([hostile_report()])
    job_id = create_job(client, symptom=f"Symptom {PAYLOAD}")
    wait_until(lambda: job_row(owner, job_id)[0] == "COMPLETED", "the job to complete", 60)
    secret = (PROJECT_ROOT / "deploy/local/access-secret.txt").read_text().strip()
    dialogs: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_context(accept_downloads=True).new_page()
        page.on("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.dismiss()))
        page.goto(PUBLIC + "/")
        expect(page).to_have_url(PUBLIC + "/login")
        page.fill("#secret", secret)
        page.click("#login-button")
        expect(page.locator("h1")).to_have_text("Start an investigation")

        page.goto(f"{PUBLIC}/investigations/{job_id}")
        expect(page.locator("#report")).to_be_visible()
        expect(page.locator(".symptom")).to_contain_text(PAYLOAD)  # shown as text
        expect(page.locator(".claim")).to_contain_text(PAYLOAD)
        assert page.locator("img[src=x]").count() == 0
        assert page.locator("script").evaluate_all("nodes => nodes.map(n => n.getAttribute('src'))") == ["/static/app.js"]
        page.locator("a.cite").first.click()
        expect(page.locator("li#E1 details")).to_have_attribute("open", "")
        page.screenshot(path=str(tmp_path / "report.png"), full_page=True)

        with page.expect_download() as download:
            page.click("a[href$='format=html']")
        exported = open(download.value.path(), encoding="utf-8").read()
        browser.close()

    assert dialogs == [], dialogs
    assert "<script" not in exported.lower() and "<img" not in exported.lower()
    assert "&lt;script&gt;" in exported
    assert re.findall(r'(?:src|href)="(https?:[^"]+)"', exported) == []


def test_pages_require_a_session_and_send_security_headers(gateway):
    import httpx
    response = httpx.get(PUBLIC + "/", headers={"Host": "127.0.0.1:8088"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login"
    login = httpx.get(PUBLIC + "/login", headers={"Host": "127.0.0.1:8088"})
    assert "script-src 'self'" in login.headers["content-security-policy"]
    assert login.headers["x-frame-options"] == "DENY" and login.headers["x-content-type-options"] == "nosniff"
