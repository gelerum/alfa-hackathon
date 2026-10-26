import hashlib
import hmac
import os

from fastapi import HTTPException

from agent_common.core import db, digest, settings


DEMO_SUBJECT = "demo:auto:v1"
# Synthetic CSV clients (revenue > 15 bn RUB, different industries) with fictional display names.
DEFAULT_DEMO_COMPANIES = (
    ("U_102920", "Нордаква Импорт"),
    ("U_35210", "Химресурс Дистрибуция"),
    ("U_26030", "Топливный альянс Урал"),
    ("U_2222", "Автокомпонент Опт"),
    ("U_177581", "Монолит-Столица"),
)


# Editable form prefill for the showcase legends; consistent with each company's CSV row.
DEFAULT_ANSWERS = {
    "role": "Финансовый директор",
    "responsibilities": [
        "Ликвидность и платёжный календарь",
        "Финансирование и кредитный портфель",
        "Расчёты с ключевыми контрагентами",
    ],
    "goals": ["Снизить стоимость финансирования", "Снизить риски по ключевым контрагентам"],
    "kpis": [
        "Стоимость заёмного финансирования, % годовых",
        "Оборачиваемость дебиторской задолженности, дней",
        "Валовая маржа, %",
    ],
    "note": "",
}
DEMO_ANSWERS = {
    "U_102920": {
        "role": "Финансовый директор",
        "responsibilities": [
            "Финансирование закупок и кредитная линия",
            "Валютные платежи импортным поставщикам",
            "Ликвидность и оборотный капитал",
            "Лизинг рефрижераторного транспорта",
        ],
        "goals": [
            "Снизить стоимость финансирования сезонных закупок",
            "Ограничить потери от колебаний курса при оплате импорта",
            "Сохранить отсрочки для торговых сетей без кассовых разрывов",
        ],
        "kpis": [
            "Стоимость заёмного финансирования, % годовых",
            "Оборачиваемость запасов, дней",
            "Курсовые разницы по импортным контрактам",
        ],
        "note": "Готовим закупочную кампанию у поставщиков из Чили и Перу на следующий квартал: решаем, фиксировать ли курс и увеличивать ли кредитный лимит.",
    },
    "U_35210": {
        "role": "Генеральный директор",
        "responsibilities": [
            "Закупки сырья у поставщиков из Китая, Индии и ОАЭ",
            "Отношения с покупателями — производителями стройматериалов",
            "Банковские гарантии по контрактам",
        ],
        "goals": [
            "Удержать маржу при росте цен на сырьё и логистику",
            "Диверсифицировать поставщиков импортного сырья",
            "Снизить зависимость продаж от строительного сектора",
        ],
        "kpis": [
            "Валовая маржа, %",
            "Доля просроченной дебиторской задолженности, %",
            "Срок поставки импортного сырья, дней",
        ],
        "note": "Пересматриваем контракты с поставщиками и условия отсрочки для покупателей из строительной отрасли.",
    },
    "U_26030": {
        "role": "Руководитель казначейства",
        "responsibilities": [
            "Ликвидность и платёжный календарь",
            "Кредитный портфель и лизинг",
            "Расчёты с крупными покупателями топлива",
        ],
        "goals": [
            "Сократить кассовые разрывы между закупкой топлива и оплатой покупателей",
            "Снизить процентные расходы по кредитам",
        ],
        "kpis": [
            "Покрытие платежей ликвидностью, дней",
            "Процентные расходы к выручке, %",
            "Оборачиваемость дебиторской задолженности, дней",
        ],
        "note": "Крупнейший покупатель — компания из инфраструктурного строительства; следим за ценами на нефтепродукты и ключевой ставкой.",
    },
    "U_2222": {
        "role": "Финансовый директор",
        "responsibilities": [
            "Импортные закупки и валютные расчёты",
            "Складские запасы",
            "Финансирование оборотного капитала",
        ],
        "goals": [
            "Снизить валютные риски по закупкам у поставщиков из Китая и ОАЭ",
            "Оптимизировать запасы под изменение спроса на автомобильном рынке",
        ],
        "kpis": [
            "Оборачиваемость запасов, дней",
            "Валовая маржа, %",
            "Стоимость финансирования запасов, % годовых",
        ],
        "note": "Планируем ассортимент и закупки на полугодие с учётом рынка автомобилей и условий импорта.",
    },
    "U_177581": {
        "role": "Генеральный директор",
        "responsibilities": [
            "Портфель строительных проектов",
            "Закупки стройматериалов",
            "Подрядчики и субподрядчики",
        ],
        "goals": [
            "Удержать сроки сдачи объектов",
            "Контролировать рост себестоимости строительства",
            "Подготовить финансирование новых объектов",
        ],
        "kpis": [
            "Себестоимость квадратного метра, ₽",
            "Соблюдение графика строительства, %",
            "Долговая нагрузка, долг/EBITDA",
        ],
        "note": "Решаем, запускать ли новый объект в ближайшие полгода с учётом ставки и спроса на жильё.",
    },
}


def suggested_answers(company_id):
    return DEMO_ANSWERS.get(company_id, DEFAULT_ANSWERS)


def demo_companies():
    """DEMO_COMPANIES="U_1=Name;U_2=Name" overrides the default showcase set."""
    raw = os.environ.get("DEMO_COMPANIES", "").strip()
    if not raw:
        return list(DEFAULT_DEMO_COMPANIES)
    pairs = [item.split("=", 1) for item in raw.split(";") if item.strip()]
    return [(cid.strip(), (name[0] if name else cid).strip()) for cid, *name in pairs if cid.strip()]


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
        configured = demo_companies()
        names = dict(configured)
        found = {
            row["company_id"]
            for row in conn.execute(
                "SELECT company_id FROM companies WHERE company_id=ANY(%s)", (list(names),)
            ).fetchall()
        }
        showcase = [cid for cid, _ in configured if cid in found]
        if showcase:
            for cid in showcase:
                conn.execute(
                    "UPDATE companies SET legal_name=%s,is_fictional=true WHERE company_id=%s",
                    (names[cid], cid),
                )
                _join(conn, cid, user["user_id"])
            conn.execute(
                "UPDATE company_memberships SET is_active=false WHERE user_id=%s AND NOT company_id=ANY(%s)",
                (user["user_id"], showcase),
            )
            company = {"company_id": showcase[0]}
        else:
            company = _fallback_company(conn, user["user_id"])
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
        "company_ids": showcase or [company["company_id"]],
        "display_name": "Демо-пользователь",
        "demo": True,
        "warning": "Локальный демо-режим: общая тестовая учётная запись, не банковская авторизация.",
    }


def _join(conn, company_id, user_id):
    conn.execute(
        "INSERT INTO company_memberships(company_id,user_id,access_role,business_role) "
        "VALUES(%s,%s,'admin','Руководитель') ON CONFLICT(company_id,user_id) "
        "DO UPDATE SET is_active=true,access_role='admin'",
        (company_id, user_id),
    )


def _fallback_company(conn, user_id):
    # No showcase rows imported (e.g. empty test DB): keep one stable company across reloads.
    company = conn.execute(
        "SELECT company_id FROM company_memberships WHERE user_id=%s AND is_active "
        "ORDER BY joined_at,company_id LIMIT 1",
        (user_id,),
    ).fetchone()
    if not company:
        company = conn.execute("SELECT company_id FROM companies ORDER BY company_id LIMIT 1").fetchone()
        if not company:
            company = conn.execute(
                "INSERT INTO companies(company_id,legal_name,is_fictional) "
                "VALUES('DEMO_LOCAL','Демо-компания',true) "
                "ON CONFLICT(company_id) DO UPDATE SET company_id=EXCLUDED.company_id RETURNING company_id"
            ).fetchone()
        _join(conn, company["company_id"], user_id)
    conn.execute(
        "UPDATE companies SET legal_name=coalesce(legal_name,'Демо-компания'),is_fictional=true "
        "WHERE company_id=%s",
        (company["company_id"],),
    )
    return company
