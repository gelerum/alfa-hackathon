from pathlib import Path
import importlib

from fastapi.testclient import TestClient

from backend_service.api import app


ROOT = Path(__file__).resolve().parents[2]


def test_implementations_live_in_their_own_projects():
    for module, project in (
        ("agent_common.core", "service-common"),
        ("backend_service.api", "backend"),
        ("profiler_service.service", "company-user-profiler"),
        ("insight_service.service", "insight-agent"),
        ("tool_gateway_service.tools", "tool-gateway"),
        ("bank_source_service.api", "bank-materials-source"),
    ):
        location = Path(importlib.import_module(module).__file__).resolve()
        assert location.is_relative_to(ROOT / project)
        assert (ROOT / project / "pyproject.toml").is_file()
        assert (ROOT / project / "uv.lock").is_file()


def test_no_accidental_python_project_in_parent_directory():
    for name in ("alpha", "tests", "pyproject.toml", "uv.lock", "Dockerfile", "compose.yaml"):
        assert not (ROOT / name).exists(), name


def test_frontend_entry_and_referenced_assets_are_served():
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    for asset in ("style.css", "app.js"):
        url = f"/frontend/{asset}"
        assert url in response.text
        assert client.get(url).status_code == 200
