from decimal import Decimal, InvalidOperation


BANK_INTERPRETATION_VERSION = "hackathon-qa-2026-10-26-v1"
BANK_COMPANY_FILENAME = "df_Uid_data_hack.csv"


def normalize_company_record(record):
    """Apply the organizers' interpretation only to the provided company CSV."""
    row = dict(record)
    if (row.get("source_filename") or "").rsplit("/", 1)[-1] != BANK_COMPANY_FILENAME:
        return row
    row["currency"] = "RUB"
    row["revenue_year"] = 2025
    row["interpretation_version"] = BANK_INTERPRETATION_VERSION
    value = row.get("revenue_raw", row.get("revenue_value"))
    if value is None:
        value = row.get("revenue_value")
    try:
        is_placeholder = value is not None and Decimal(str(value)) == Decimal(1000)
    except InvalidOperation:
        is_placeholder = False
    if is_placeholder:
        row["revenue_value"] = None
        row["revenue_missing_reason"] = "Значение 1000 — заглушка в исходных данных, выручка неизвестна."
    return row
