"""E2E tests for the Alpha Business frontend via Playwright.

Start a real backend + test PostgreSQL, then drive the browser.
Run with:
    TEST_DATABASE_URL="postgresql://postgres:@/postgres?host=/tmp/alpha-test-postgres"
    ADMIN_TOKEN=test-admin-token
    INTERNAL_TOKEN=test-internal-token
    uv run pytest tests/test_e2e.py -v --tb=short -s
"""

from pathlib import Path

import psycopg
import pytest
from playwright.sync_api import Page, expect
from starlette.testclient import TestClient

from backend_service.api import app as backend_app


@pytest.fixture(scope="module")
def api_client():
    return TestClient(backend_app)


@pytest.fixture(scope="module")
def test_db():
    """Ensure test PostgreSQL schema exists."""
    dsn = Path("/tmp/alpha-test-dsn").read_text().strip()
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute((Path(__file__).resolve().parents[2] / "database/schema.sql").read_text())
        conn.execute((Path(__file__).resolve().parents[2] / "database/application.sql").read_text())
    return dsn


@pytest.fixture(scope="module")
def demo_token(api_client, test_db):
    """Create a demo user + token via admin API."""
    r = api_client.post(
        "/api/admin/demo",
        json={
            "synthetic_company_id": "U_demo",
            "fictional_name": "Демо-компания",
            "display_name": "Тестовый пользователь",
        },
        headers={"Authorization": "Bearer test-admin-token"},
    )
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.fixture(scope="module")
def page():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1280, "height": 800})
        pg = ctx.new_page()
        yield pg
        pg.close()
        ctx.close()
        browser.close()


@pytest.mark.skip(
    reason="E2E requires a live HTTP server at :8092; run with `docker compose up backend` first"
)
def test_frontend_loads(page: Page):
    """Frontend renders and login screen is visible."""
    page.goto("http://127.0.0.1:8092/")
    page.wait_for_selector("#loginScreen", timeout=5000)
    expect(page).to_have_title("Альфа-Бизнес — Аналитический обзор")
    expect(page.locator("#tokenInput")).to_be_visible()


@pytest.mark.skip(reason="E2E requires a live HTTP server at :8092")
def test_login_success(page: Page, demo_token: str):
    """Login with valid token → dashboard loads."""
    page.goto("http://127.0.0.1:8092/")
    page.fill("#tokenInput", demo_token)
    page.click("#loginForm button[type=submit]")
    page.wait_for_selector("#dashboardScreen", timeout=8000)
    expect(page.locator("#dashCompany")).to_be_visible()
    expect(page.locator("#companySelect")).to_be_visible()


@pytest.mark.skip(reason="E2E requires a live HTTP server at :8092")
def test_onboarding_flow(page: Page, demo_token: str, api_client):
    """Fill onboarding → verify company context becomes ready."""
    page.goto("http://127.0.0.1:8092/")
    page.fill("#tokenInput", demo_token)
    page.click("#loginForm button[type=submit]")
    page.wait_for_selector("#dashboardScreen", timeout=8000)

    # Click onboarding button if visible
    if page.is_visible("#onboardingPrompt"):
        page.click("#startOnboardingBtn")
        page.wait_for_selector("#onboardingScreen", timeout=3000)

        page.fill("#obRole", "Руководитель отдела аналитики")
        page.fill("#tagInput", "Планирование")
        page.keyboard.press("Enter")
        page.fill("#tagInput", "Бюджет")
        page.keyboard.press("Enter")
        page.fill("#goalInput", "Сокращение издержек")
        page.keyboard.press("Enter")
        page.fill("#obNote", "Тестовый контекст для E2E")

        submit_btn = page.locator("#obSubmit")
        submit_btn.click()
        page.wait_for_selector(".submit-status.success", timeout=15000)
        expect(page.locator(".submit-status.success")).to_be_visible()

        # Navigate back to dashboard
        page.click("#backFromOnboarding")
        page.wait_for_selector("#dashboardScreen", timeout=3000)

        # Verify context via API
        headers = {"Authorization": f"Bearer {demo_token}"}
        ctx = api_client.get("/api/companies/U_demo/context", headers=headers).json()
        assert ctx["status"] == "ready", f"Context not ready: {ctx}"
    else:
        # Already onboarded or context ready — skip
        pass


@pytest.mark.skip(reason="E2E requires a live HTTP server at :8092")
def test_static_assets_served(page: Page):
    """CSS and JS assets are served correctly."""
    page.goto("http://127.0.0.1:8092/frontend/style.css")
    expect(page).to_have_title("")  # raw CSS
    css_content = page.content()
    assert "--red: #EF352D" in css_content
    assert ".btn-primary" in css_content

    page.goto("http://127.0.0.1:8092/frontend/app.js")
    js_content = page.content()
    assert "API_BASE" in js_content or "API = " in js_content
    assert "loginForm" in js_content
