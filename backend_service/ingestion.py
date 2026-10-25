from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import httpx

from agent_common.core import db, digest, enqueue, j, one, settings


def poll_bank_source():
    saved = one("SELECT cursor FROM source_cursors WHERE source_name=%s", ("alfa_mock",))
    cursor = saved["cursor"] if saved else "0"
    response = httpx.get(settings.bank_source_url + "/materials", params={"cursor": cursor}, timeout=20)
    response.raise_for_status()
    page = response.json()
    for material in page["items"]:
        external_id, version = material["external_id"], material["version"]
        existing = one(
            "SELECT document_id FROM documents WHERE external_id=%s AND external_version=%s",
            (external_id, version),
        )
        if not existing:
            url = (
                settings.bank_source_url
                + "/materials/"
                + quote(external_id, safe="")
                + "/versions/"
                + quote(version, safe="")
                + "/content"
            )
            data = bytearray()
            with httpx.stream("GET", url, timeout=60) as download:
                download.raise_for_status()
                for chunk in download.iter_bytes():
                    data.extend(chunk)
                    if len(data) > settings.max_upload_bytes:
                        raise ValueError("Bank source document exceeds upload limit")
            if digest(bytes(data)) != material["sha256"] or not data.startswith(b"%PDF-"):
                raise ValueError("Bank source content checksum/format mismatch")
            document_id = uuid4()
            key = str(document_id)
            settings.storage_dir.mkdir(parents=True, exist_ok=True)
            path = settings.storage_dir / key
            path.write_bytes(data)
            with db() as conn:
                row = conn.execute(
                    "INSERT INTO documents(document_id,source_type,title,source_url,storage_key,content_sha256,"
                    "published_at,original_filename,media_type,external_id,external_version,metadata) "
                    "VALUES(%s,'bank_analytics',%s,%s,%s,%s,%s,%s,'application/pdf',%s,%s,%s) "
                    "ON CONFLICT(external_id,external_version) WHERE external_id IS NOT NULL DO NOTHING RETURNING document_id",
                    (
                        document_id,
                        material["title"],
                        url,
                        key,
                        material["sha256"],
                        material["published_at"],
                        Path(material["filename"]).name,
                        external_id,
                        version,
                        j(
                            {
                                "simulated_source": True,
                                "available_at": material["available_at"],
                                "date_warning": "unknown" if not material["published_at"] else None,
                            }
                        ),
                    ),
                ).fetchone()
                if row:
                    conn.execute(
                        "INSERT INTO jobs(kind,idempotency_key,payload) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
                        ("extract_document", "extract:" + key, j({"document_id": document_id})),
                    )
                else:
                    path.unlink(missing_ok=True)
        # Advance only after document and durable extraction job are committed.
        with db() as conn:
            conn.execute(
                "INSERT INTO source_cursors(source_name,cursor) VALUES(%s,%s) "
                "ON CONFLICT(source_name) DO UPDATE SET cursor=EXCLUDED.cursor,updated_at=now()",
                ("alfa_mock", str(material["seq"])),
            )
    return {"next_cursor": page["next_cursor"], "count": len(page["items"])}


def refresh_profiles_for_document(payload):
    document = one("SELECT * FROM documents WHERE document_id=%s", (payload["document_id"],))
    if not document["company_id"]:
        return
    with db() as conn:
        users = conn.execute(
            "SELECT user_id FROM company_memberships WHERE company_id=%s AND is_active "
            "AND access_role IN ('admin','analyst')",
            (document["company_id"],),
        ).fetchall()
    for user in users:
        enqueue(
            "profile",
            f"document:{document['document_id']}:{user['user_id']}",
            {"company_id": document["company_id"], "user_id": str(user["user_id"])},
        )
