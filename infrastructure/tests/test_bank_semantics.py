import json
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from agent_common.bank_semantics import BANK_INTERPRETATION_VERSION, normalize_company_record
from agent_common.contracts import AgentScope
from agent_common.core import db, j, now, one
from backend_service import api
from profiler_service.service import bank_facts, company_inputs
import test_integration
from tool_gateway_service.tools import bank_data

demo = test_integration.demo


@pytest.mark.parametrize("value", [1000, "1000.0", Decimal("1000.000")])
def test_placeholder_is_not_revenue_and_raw_value_stays(value):
    raw = {"source_filename": "df_Uid_data_hack.csv", "revenue_raw": str(value), "revenue_value": value}
    row = normalize_company_record(raw)
    assert row["revenue_value"] is None
    assert row["revenue_raw"] == raw["revenue_raw"]
    assert row["currency"] == "RUB" and row["revenue_year"] == 2025
    assert raw["revenue_value"] == value


def test_revenue_metadata_does_not_spread_to_other_sources():
    raw = {"source_filename": "uploaded-report.csv", "revenue_value": 1000, "currency": "USD", "revenue_year": 2023}
    assert normalize_company_record(raw) == raw
    assert normalize_company_record({"source_filename": None}) == {"source_filename": None}
    row = normalize_company_record({"source_filename": "df_Uid_data_hack.csv", "revenue_value": Decimal("16083708340")})
    assert row["revenue_value"] == Decimal("16083708340")
    assert row["currency"] == "RUB" and row["revenue_year"] == 2025


def test_existing_bank_rows_get_new_interpretation_without_rewriting_history(demo):
    with db() as conn:
        snapshot = conn.execute(
            "INSERT INTO bank_import_batches(dataset,source_filename,status) "
            "VALUES('company','df_Uid_data_hack.csv','ready') RETURNING source_snapshot_id"
        ).fetchone()["source_snapshot_id"]
        row = conn.execute(
            "INSERT INTO bank_company_data(source_snapshot_id,source_row_number,company_id,revenue_raw,"
            "revenue_value,fts_country_name,raw_attributes) VALUES(%s,2,'U_demo','1000.0',1000,'КИТАЙ','{}') "
            "RETURNING bank_record_id", (snapshot,)
        ).fetchone()
        ref = {"bank_record_id": row["bank_record_id"], "snapshot_id": str(snapshot)}
        legacy = conn.execute(
            "INSERT INTO evidence_sources(company_id,source_type,source_ref,excerpt) "
            "VALUES('U_demo','bank_import',%s,%s) RETURNING source_id",
            (j(ref), '{"revenue_value": "1000", "currency": null, "revenue_year": null}'),
        ).fetchone()["source_id"]
    scope = AgentScope(**demo, agent_run_id=uuid4(), as_of=now())
    result = bank_data(scope)
    source = next(s for s in result["sources"] if s["source_ref"].get("interpretation_version"))
    data = json.loads(source["excerpt"])
    assert data["revenue_value"] is None and data["revenue_raw"] == "1000.0"
    assert data["currency"] == "RUB" and data["revenue_year"] == 2025
    assert data["fts_country_name"] == "КИТАЙ"
    assert source["source_id"] != legacy
    assert json.loads(one("SELECT excerpt FROM evidence_sources WHERE source_id=%s", (legacy,))["excerpt"])["revenue_value"] == "1000"
    facts = bank_facts({"sources": [source]})
    fields = {fact.field: fact.value for fact in facts}
    assert "revenue_value" not in fields
    assert fields["currency"] == "RUB" and fields["revenue_year"] == "2025"
    with db() as conn:
        assert company_inputs(conn, "U_demo")["bank_interpretation_version"] == BANK_INTERPRETATION_VERSION
    company = api.companies(UUID(demo["user_id"]))[0]
    assert company["revenue_value"] is None and company["currency"] == "RUB" and company["revenue_year"] == 2025


def test_supplied_payment_amounts_are_rubles_but_dates_remain_unknown(demo):
    with db() as conn:
        snapshot = conn.execute(
            "INSERT INTO bank_import_batches(dataset,source_filename,status) "
            "VALUES('payments','df_Uid_payment_hack.csv','ready') RETURNING source_snapshot_id"
        ).fetchone()["source_snapshot_id"]
        conn.execute(
            "INSERT INTO bank_payment_edges(source_snapshot_id,source_row_number,payer,beneficiary,pay_amt,pay_cnt) "
            "VALUES(%s,2,'U_demo','unknown-uid',1200,2)", (snapshot,)
        )
    result = bank_data(AgentScope(**demo, agent_run_id=uuid4(), as_of=now()))
    aggregate = result["payment_aggregates"][0]
    assert aggregate["currency"] == "RUB"
    assert aggregate["aggregation_from"] is None and aggregate["aggregation_to"] is None
    assert result["top_counterparties"][0]["counterparty"] == "unknown-uid"
    assert result["top_counterparties"][0]["currency"] == "RUB"
    assert one("SELECT company_id FROM companies WHERE company_id='unknown-uid'") is None
