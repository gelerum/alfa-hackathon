import hashlib
import hmac
import os

from fastapi import HTTPException

from agent_common.core import db, digest, settings


DEMO_SUBJECT = "demo:auto:v1"


def create_demo_session():
    if os.environ.get("DEMO_MODE", "false").lower() not in ("true", "1", "yes"):
        raise HTTPException(404, "Demo mode is disabled")
    if not settings.internal_token:
        raise HTTPException(503, "Demo mode requires INTERNAL_TOKEN")
    # Reuse a single local account/token instead of adding rows on every page load.
    token = hmac.new(settings.internal_token.encode(), DEMO_SUBJECT.encode(), hashlib.sha256).hexdigest()
    with db() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('demo:auto:v1',0))")
        user = conn.execute(
            "INSERT INTO app_users(auth_subject,display_name) VALUES(%s,'Демо-пользователь') "
            "ON CONFLICT(auth_subject) DO UPDATE SET auth_subject=EXCLUDED.auth_subject RETURNING user_id",
            (DEMO_SUBJECT,),
        ).fetchone()
        # Keep the original choice across reloads, even when new companies are imported.
        company = conn.execute(
            "SELECT company_id FROM company_memberships WHERE user_id=%s AND is_active "
            "ORDER BY joined_at,company_id LIMIT 1",
            (user["user_id"],),
        ).fetchone()
        if not company:
            company = conn.execute("SELECT company_id FROM companies ORDER BY company_id LIMIT 1").fetchone()
            if not company:
                company = conn.execute(
                    "INSERT INTO companies(company_id,legal_name,is_fictional) "
                    "VALUES('DEMO_LOCAL','Демо-компания',true) "
                    "ON CONFLICT(company_id) DO UPDATE SET company_id=EXCLUDED.company_id RETURNING company_id"
                ).fetchone()
            conn.execute(
                "INSERT INTO company_memberships(company_id,user_id,access_role,business_role) "
                "VALUES(%s,%s,'admin','Руководитель') ON CONFLICT(company_id,user_id) "
                "DO UPDATE SET is_active=true,access_role='admin'",
                (company["company_id"], user["user_id"]),
            )
        conn.execute(
            "UPDATE companies SET legal_name=coalesce(legal_name,'Демо-компания'),is_fictional=true "
            "WHERE company_id=%s",
            (company["company_id"],),
        )
        conn.execute(
            "INSERT INTO api_tokens(token_hash,user_id) VALUES(%s,%s) ON CONFLICT(token_hash) DO NOTHING",
            (digest(token), user["user_id"]),
        )
        active = conn.execute(
            "SELECT 1 FROM api_tokens WHERE token_hash=%s AND revoked_at IS NULL", (digest(token),)
        ).fetchone()
        if not active:
            raise HTTPException(403, "Demo token was revoked; an administrator must reset it")
    return {
        "token": token,
        "user_id": user["user_id"],
        "company_id": company["company_id"],
        "display_name": "Демо-пользователь",
        "demo": True,
        "warning": "Локальный демо-режим: общая тестовая учётная запись, не банковская авторизация.",
    }
