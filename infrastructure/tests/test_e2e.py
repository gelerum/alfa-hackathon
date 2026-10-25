import json
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_BROWSER_TESTS") != "1", reason="Set RUN_BROWSER_TESTS=1 and install Chromium dependencies"
)
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def frontend_url():
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(ROOT / "frontend"), **kwargs)

        def do_GET(self):
            if self.path.startswith("/frontend/"):
                self.path = self.path[len("/frontend") :]
            super().do_GET()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/"
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.fixture
def browser_page(frontend_url):
    scenario = {"calls": [], "question_error": False, "hold_a": False, "held": [], "empty": False}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1360, "height": 960})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def api_route(route):
            request = route.request
            path = urlsplit(request.url).path.removeprefix("/api")
            scenario["calls"].append((path, request))
            status = 200
            if path == "/demo/session":
                data = {"token": "demo-token"}
            elif path == "/companies":
                data = [
                    {"company_id": "A", "legal_name": "Компания А"},
                    {"company_id": "B", "legal_name": "Компания Б"},
                ]
            elif path.endswith("/context"):
                data = {
                    "status": "ready",
                    "profile": {"profile_version": 2},
                    "user_context": {"business_role": "Казначей", "raw_answers": {"role": "Казначей"}},
                }
            elif path.endswith("/documents"):
                data = [] if request.method == "GET" else {"document_id": "document", "job_id": "extract"}
                if request.method == "POST":
                    status = 202
            elif path.endswith("/onboarding"):
                status = 202
                data = {"job_id": "profile"}
            elif path.startswith("/companies/") and path.endswith("/reports"):
                if scenario["hold_a"] and "/A/" in path:
                    scenario["held"].append(route)
                    return
                company = path.split("/")[2]
                data = (
                    []
                    if scenario["empty"]
                    else [
                        {
                            "report_id": "report-1",
                            "version": 2,
                            "summary": f"Важное {company}",
                            "published_at": "2026-10-19T05:00:00Z",
                            "window_start": "2026-10-12T05:00:00Z",
                            "window_end": "2026-10-19T05:00:00Z",
                        }
                    ]
                )
            elif path.endswith("/versions"):
                data = [
                    {"version": 2, "published_at": "2026-10-19T05:00:00Z"},
                    {"version": 1, "published_at": "2026-10-19T04:59:00Z"},
                ]
            elif path.endswith("/questions"):
                status = 409 if scenario["question_error"] else 202
                data = (
                    {"detail": "Источники временно недоступны"} if status == 409 else {"job_id": "answer-job"}
                )
            elif path.startswith("/jobs/"):
                data = {
                    "status": "completed",
                    "result": {"answer": "Проверьте условия кредитных договоров.", "source_details": []},
                }
            elif path.startswith("/reports/"):
                bulletin = "version=1" in request.url
                source = {
                    "source_type": "bank_analytics",
                    "source_url": "https://example.org/report",
                    "source_date": None,
                    "excerpt": "Банк ожидает сохранение жёстких условий.",
                    "source_ref": {"locator": {"page": 2}},
                }
                data = {
                    "report": {
                        "report_id": "report-1",
                        "window_start": "2026-10-12T05:00:00Z",
                        "window_end": "2026-10-19T05:00:00Z",
                    },
                    "version": {
                        "report_version_id": "version-1" if bulletin else "version-2",
                        "version": 1 if bulletin else 2,
                        "summary": "Анализ не завершён" if bulletin else "Проверьте чувствительность бюджета",
                        "published_at": "2026-10-19T05:00:00Z",
                        "limitations": ["Это уведомление о неполноте, не аналитический вывод"]
                        if bulletin
                        else ["Условия долга неизвестны"],
                    },
                    "status": "availability_notice" if bulletin else "ready",
                    "company_context_metadata": {
                        "profile_version": 2,
                        "published_at": "2026-10-18T00:00:00Z",
                    },
                    "cards": []
                    if bulletin
                    else [
                        {
                            "insight_id": "card-1",
                            "title": "Стоимость финансирования",
                            "event_summary": "Ставка сохранена.",
                            "business_impact": "Условное влияние на бюджет",
                            "details": {
                                "mechanism": "Влияние зависит от договоров",
                                "role_action": "Проверить сценарии",
                                "assumptions": ["Тип ставки неизвестен"],
                            },
                            "claims": [
                                {
                                    "kind": "bank_position",
                                    "statement": source["excerpt"],
                                    "certainty": "qualified",
                                    "sources": [source],
                                },
                                {
                                    "kind": "ai_inference",
                                    "statement": "Проверьте чувствительность бюджета",
                                    "certainty": "qualified",
                                    "company_facts": [{"field": "activity", "value": "Производство"}],
                                },
                            ],
                        }
                    ],
                }
            else:
                raise AssertionError("Unexpected API call: " + path)
            route.fulfill(
                status=status, content_type="application/json", body=json.dumps(data, ensure_ascii=False)
            )

        page.route("**/api/**", api_route)
        yield page, scenario, frontend_url
        assert not errors, errors
        browser.close()


def test_auto_login_published_report_sources_question_and_bulletin(browser_page):
    page, scenario, url = browser_page
    page.goto(url)
    expect(page.locator("#dashboardScreen")).to_be_visible()
    expect(page.locator(".report-tile")).to_contain_text("Опубликован · версия 2")
    page.locator(".report-tile").click()
    page.locator(".insight-heading").click()
    expect(page.locator(".bank_position")).to_contain_text("Позиция Альфа-Банка")
    expect(page.locator(".source").first).to_contain_text("Дата не указана")
    expect(page.locator(".warning").last).to_contain_text("Тип ставки неизвестен")
    scenario["question_error"] = True
    page.locator(".question-form input").fill("Что проверить?")
    page.locator(".question-form button").click()
    expect(page.locator(".answer")).to_contain_text("Источники временно недоступны")
    expect(page.locator(".question-form button")).to_be_enabled()
    scenario["question_error"] = False
    page.locator(".question-form button").click()
    expect(page.locator(".answer")).to_contain_text("Проверьте условия кредитных договоров")
    submitted = [r.post_data_json for path, r in scenario["calls"] if path.endswith("/questions")][-1]
    assert submitted["insight_id"] == "card-1" and submitted["report_version_id"] == "version-2"
    page.locator("#versionSelect").select_option("1")
    expect(page.locator("#reportBody")).to_contain_text("Анализ не завершён")
    expect(page.locator("#reportBody")).to_contain_text("Уведомление о неполноте")
    assert page.locator(".insight-card").count() == 0
    page.screenshot(path="/tmp/alpha-business-report.png", full_page=True)


def test_onboarding_sends_multiple_goals_and_multipart_file(browser_page):
    page, scenario, url = browser_page
    page.goto(url)
    expect(page.locator("#dashboardScreen")).to_be_visible()
    page.locator("#settingsBtn").click()
    page.locator("#obResponsibilities").fill("Финансы\nЛиквидность")
    page.locator("#obGoals").fill("Снизить затраты\nПроверить риски\nУлучшить прогноз")
    page.locator("#fileInput").set_input_files(
        {"name": "company.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-test"}
    )
    page.locator("#obSubmit").click()
    expect(page.locator("#notice")).to_contain_text("Фокус сохранён")
    answers = [r.post_data_json for path, r in scenario["calls"] if path.endswith("/onboarding")][-1]
    assert len(answers["responsibilities"]) == 2 and len(answers["goals"]) == 3
    upload = [r for path, r in scenario["calls"] if path.endswith("/documents") and r.method == "POST"][0]
    assert "multipart/form-data; boundary=" in upload.headers["content-type"]


def test_stale_company_response_does_not_overwrite_new_company(browser_page):
    page, scenario, url = browser_page
    scenario["hold_a"] = True
    page.goto(url)
    expect(page.locator("#companySelect option")).to_have_count(2)
    page.wait_for_timeout(100)
    page.locator("#companySelect").select_option("B")
    expect(page.locator("#reportsList")).to_contain_text("Важное B")
    for route in scenario["held"]:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps([{"report_id": "old", "summary": "STALE COMPANY A"}]),
        )
    page.wait_for_timeout(100)
    expect(page.locator("#reportsList")).not_to_contain_text("STALE COMPANY A")
    expect(page.locator("#dashCompany")).to_have_text("Компания Б")


def test_mobile_shell_does_not_overflow(browser_page):
    page, scenario, url = browser_page
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(url)
    expect(page.locator(".report-tile")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path="/tmp/alpha-business-mobile.png", full_page=True)
