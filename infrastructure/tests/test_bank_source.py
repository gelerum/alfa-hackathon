from datetime import datetime, timezone

import fitz
from fastapi.testclient import TestClient

from bank_source_service import api as bank_source
from agent_common.core import settings


def pdf_bytes(text):
    with fitz.open() as pdf:
        page = pdf.new_page()
        page.insert_text((30, 30), text)
        return pdf.tobytes()


def test_source_cursor_versioning_and_live_publication(tmp_path, monkeypatch):
    monkeypatch.setattr(bank_source, "ROOT", tmp_path / "source")
    monkeypatch.setattr(settings, "admin_token", "source-admin")
    monkeypatch.setenv("BANK_SOURCE_SEED_DIR", str(tmp_path / "empty"))
    with TestClient(bank_source.app) as client:
        assert client.get("/materials").json()["items"] == []
        assert (
            client.post(
                "/materials", files={"file": ("a.pdf", pdf_bytes("first"))}, data={"title": "Report"}
            ).status_code
            == 401
        )
        headers = {"Authorization": "Bearer source-admin"}
        response = client.post(
            "/materials",
            headers=headers,
            files={"file": ("a.pdf", pdf_bytes("first"))},
            data={"title": "Report", "external_id": "report-a", "published_at": "2026-10-20T08:00:00+03:00"},
        )
        assert response.status_code == 200, response.text
        first = response.json()
        page = client.get("/materials").json()
        assert len(page["items"]) == 1
        cursor = page["next_cursor"]
        assert client.get("/materials", params={"cursor": cursor}).json()["items"] == []
        second = client.post(
            "/materials",
            headers=headers,
            files={"file": ("a.pdf", pdf_bytes("updated"))},
            data={"title": "Updated report", "external_id": "report-a"},
        ).json()
        assert second["version"] != first["version"]
        assert second["published_at"] is None  # never substitute ingestion time for publication date
        updates = client.get("/materials", params={"cursor": cursor}).json()["items"]
        assert len(updates) == 1
        downloaded = client.get(f"/materials/report-a/versions/{first['version']}/content")
        assert downloaded.status_code == 200 and downloaded.content.startswith(b"%PDF-")
        assert len(client.get("/materials").json()["items"]) == 2


def test_republishing_same_version_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(bank_source, "ROOT", tmp_path)
    data = pdf_bytes("Stable bytes")
    at = datetime(2026, 10, 20, tzinfo=timezone.utc)
    first = bank_source.publish(data, "report.pdf", "Report", "id", at)
    second = bank_source.publish(data, "report.pdf", "Report", "id", at)
    assert first == second
