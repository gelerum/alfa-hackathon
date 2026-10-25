"""An explicitly simulated bank publication API, independently persisted in SQLite.

Seed PDFs are bootstrap data only. Consumers poll the cursor API, never the seed folder.
"""

import os
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from agent_common.core import admin, digest, now, settings
from agent_common.http_limits import BodyLimit

ROOT = Path(os.getenv("BANK_SOURCE_STORAGE", "/tmp/alpha-bank-source"))


@contextmanager
def catalog():
    ROOT.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(ROOT / "catalog.sqlite") as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE IF NOT EXISTS materials(seq INTEGER PRIMARY KEY AUTOINCREMENT,"
            "external_id TEXT NOT NULL,version TEXT NOT NULL,title TEXT NOT NULL,"
            "published_at TEXT,available_at TEXT NOT NULL,sha256 TEXT NOT NULL,"
            "filename TEXT NOT NULL,UNIQUE(external_id,version))"
        )
        yield conn


def publish(data, filename, title, external_id, published_at):
    import re

    if not title.strip() or len(title) > 300 or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", external_id):
        raise ValueError(
            "Title must be 1..300 characters; external_id must be an ASCII slug up to 128 characters"
        )
    if not data.startswith(b"%PDF-") or len(data) > settings.max_upload_bytes:
        raise ValueError("A bounded PDF file is required")
    content_hash = digest(data)
    version = digest(
        {
            "sha256": content_hash,
            "title": title,
            "published_at": published_at.isoformat() if published_at else None,
        }
    )
    ROOT.mkdir(parents=True, exist_ok=True)
    target = ROOT / content_hash
    temporary = ROOT / f".{uuid4()}.tmp"
    temporary.write_bytes(data)
    temporary.replace(target)
    with catalog() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO materials(external_id,version,title,published_at,available_at,sha256,filename) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                external_id,
                version,
                title,
                published_at.isoformat() if published_at else None,
                now().isoformat(),
                content_hash,
                Path(filename).name,
            ),
        )
        return dict(
            conn.execute(
                "SELECT * FROM materials WHERE external_id=? AND version=?", (external_id, version)
            ).fetchone()
        )


@asynccontextmanager
async def lifespan(app):
    with catalog():
        pass
    seed = Path(os.getenv("BANK_SOURCE_SEED_DIR", "/seed"))
    if seed.exists():
        for path in sorted(seed.glob("*.pdf")):
            if path.stat().st_size <= settings.max_upload_bytes:
                publish(path.read_bytes(), path.name, path.stem, "seed-" + digest(path.name)[:24], None)
    yield


app = FastAPI(title="Simulated Alfa analytics source", lifespan=lifespan)
app.add_middleware(BodyLimit, limit=settings.max_upload_bytes + 1024 * 1024)


@app.get("/health")
def health():
    return {"status": "ok", "simulated": True}


@app.get("/materials")
def materials(cursor: int = 0):
    with catalog() as conn:
        rows = [
            dict(r)
            for r in conn.execute("SELECT * FROM materials WHERE seq>? ORDER BY seq LIMIT 50", (cursor,))
        ]
    return {"items": rows, "next_cursor": str(rows[-1]["seq"] if rows else cursor), "simulated": True}


@app.get("/materials/{external_id}/versions/{version}")
def metadata(external_id: str, version: str):
    with catalog() as conn:
        row = conn.execute(
            "SELECT * FROM materials WHERE external_id=? AND version=?", (external_id, version)
        ).fetchone()
    if not row:
        raise HTTPException(404)
    return dict(row)


@app.get("/materials/{external_id}/versions/{version}/content")
def content(external_id: str, version: str):
    row = metadata(external_id, version)
    return FileResponse(ROOT / row["sha256"], media_type="application/pdf", filename=row["filename"])


@app.post("/materials", dependencies=[Depends(admin)])
async def add_material(
    file: UploadFile = File(),
    title: str = Form(),
    external_id: str | None = Form(None),
    published_at: datetime | None = Form(None),
):
    if published_at and published_at.tzinfo is None:
        raise HTTPException(422, "Publication timestamp must contain timezone")
    data = await file.read(settings.max_upload_bytes + 1)
    try:
        return publish(
            data, file.filename or "material.pdf", title, external_id or str(uuid4()), published_at
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
