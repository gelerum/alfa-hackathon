-- Loaded once by the PostgreSQL Docker entrypoint, after schema.sql.
-- Keep CSV strings unchanged in raw_attributes; normalize only typed columns.
-- Unknown currency, revenue year and aggregation period remain NULL.
BEGIN;

INSERT INTO bank_import_batches (dataset, source_filename, sha256)
SELECT dataset, filename,
       encode(sha256(pg_read_binary_file('/csv/' || filename)), 'hex')
FROM (VALUES
  ('company', 'df_Uid_data_hack.csv'),
  ('payments', 'df_Uid_payment_hack.csv'),
  ('people', 'df_Fid_Uid_link_hack.csv')
) AS files(dataset, filename);

-- Column order matches the supplied CSV headers. Row numbers include the header.
CREATE TEMP TABLE company_csv (
  source_row_number bigint GENERATED ALWAYS AS IDENTITY (START WITH 2),
  "SYNTHETIC_U" text, "ORG_TYPE" text, "OKFS_NAME" text,
  "COMPANY_AGE_YEARS_GRADATION" text, "ECO_MARK_LABEL" text,
  "MAINOKVED_CODE" text, "MAINOKVED_NAME" text, "NEGATIVEMARK" text,
  "AMOUNT_WORKERS_FNS_GRADATION" text, "AMOUNT_WORKERS_SPARK_GRADATION" text,
  "CL_TYPE" text, "CL_PRODUCT_RKO_MARK" text, "CL_PRODUCT_GAR_MARK" text,
  "CL_PRODUCT_LOA_MARK" text, "CL_PRODUCT_LEA_MARK" text,
  "CL_PRODUCT_DEP_MARK" text, "CL_PRODUCT_ZPP_MARK" text,
  "FTS_FLAG" text, "FTS_COUNTRY_NAME" text, "FACTORING_MARK" text,
  "REVENUE_COMPANY" text, "GROUP_ID" text, "CITY" text, "REGION" text
) ON COMMIT DROP;
COPY company_csv (
  "SYNTHETIC_U", "ORG_TYPE", "OKFS_NAME", "COMPANY_AGE_YEARS_GRADATION",
  "ECO_MARK_LABEL", "MAINOKVED_CODE", "MAINOKVED_NAME", "NEGATIVEMARK",
  "AMOUNT_WORKERS_FNS_GRADATION", "AMOUNT_WORKERS_SPARK_GRADATION", "CL_TYPE",
  "CL_PRODUCT_RKO_MARK", "CL_PRODUCT_GAR_MARK", "CL_PRODUCT_LOA_MARK",
  "CL_PRODUCT_LEA_MARK", "CL_PRODUCT_DEP_MARK", "CL_PRODUCT_ZPP_MARK",
  "FTS_FLAG", "FTS_COUNTRY_NAME", "FACTORING_MARK", "REVENUE_COMPANY",
  "GROUP_ID", "CITY", "REGION"
) FROM '/csv/df_Uid_data_hack.csv'
WITH (FORMAT csv, HEADER MATCH, DELIMITER ';', NULL '\N', ENCODING 'UTF8');

INSERT INTO companies (company_id)
SELECT DISTINCT btrim("SYNTHETIC_U") FROM company_csv;

INSERT INTO bank_company_data (
  source_snapshot_id, source_row_number, company_id, org_type, okfs_name,
  company_age_band, eco_mark_label, main_okved_code, main_okved_name,
  negativemark, employees_fns_band, employees_spark_band, cl_type, group_id,
  registration_city, registration_region, revenue_raw, revenue_value,
  fts_flag, fts_country_name, loan_mark, leasing_mark, factoring_mark,
  raw_attributes
)
SELECT b.source_snapshot_id, c.source_row_number, btrim(c."SYNTHETIC_U"),
  nullif(btrim(c."ORG_TYPE"), ''), nullif(btrim(c."OKFS_NAME"), ''),
  nullif(btrim(c."COMPANY_AGE_YEARS_GRADATION"), ''),
  nullif(btrim(c."ECO_MARK_LABEL"), ''), nullif(btrim(c."MAINOKVED_CODE"), ''),
  nullif(btrim(c."MAINOKVED_NAME"), ''), nullif(btrim(c."NEGATIVEMARK"), ''),
  nullif(btrim(c."AMOUNT_WORKERS_FNS_GRADATION"), ''),
  nullif(btrim(c."AMOUNT_WORKERS_SPARK_GRADATION"), ''),
  nullif(btrim(c."CL_TYPE"), ''), nullif(btrim(c."GROUP_ID"), ''),
  nullif(btrim(c."CITY"), ''), nullif(btrim(c."REGION"), ''),
  nullif(btrim(c."REVENUE_COMPANY"), ''),
  nullif(btrim(c."REVENUE_COMPANY"), '')::numeric,
  nullif(btrim(c."FTS_FLAG"), '')::numeric::integer::boolean,
  nullif(btrim(c."FTS_COUNTRY_NAME"), ''),
  nullif(btrim(c."CL_PRODUCT_LOA_MARK"), '')::numeric::integer::boolean,
  nullif(btrim(c."CL_PRODUCT_LEA_MARK"), '')::numeric::integer::boolean,
  nullif(btrim(c."FACTORING_MARK"), '')::numeric::integer::boolean,
  to_jsonb(c) - 'source_row_number'
FROM company_csv c CROSS JOIN bank_import_batches b WHERE b.dataset = 'company';

CREATE TEMP TABLE payment_csv (
  source_row_number bigint GENERATED ALWAYS AS IDENTITY (START WITH 2),
  "BENEFICIARY" text, "PAYER" text, "PAY_AMT" text, "PAY_CNT" text
) ON COMMIT DROP;
COPY payment_csv ("BENEFICIARY", "PAYER", "PAY_AMT", "PAY_CNT")
FROM '/csv/df_Uid_payment_hack.csv'
WITH (FORMAT csv, HEADER MATCH, DELIMITER ';', NULL '\N', ENCODING 'UTF8');

INSERT INTO bank_payment_edges (
  source_snapshot_id, source_row_number, payer, beneficiary, pay_amt, pay_cnt,
  raw_attributes
)
SELECT b.source_snapshot_id, c.source_row_number,
  btrim(c."PAYER"), btrim(c."BENEFICIARY"),
  nullif(btrim(c."PAY_AMT"), '')::numeric,
  nullif(btrim(c."PAY_CNT"), '')::bigint,
  to_jsonb(c) - 'source_row_number'
FROM payment_csv c CROSS JOIN bank_import_batches b WHERE b.dataset = 'payments';

CREATE TEMP TABLE people_csv (
  source_row_number bigint GENERATED ALWAYS AS IDENTITY (START WITH 2),
  "SYNTHETIC_F" text, "CITY_NAME" text, "RC_NAME" text, "SYNTHETIC_U" text
) ON COMMIT DROP;
COPY people_csv ("SYNTHETIC_F", "CITY_NAME", "RC_NAME", "SYNTHETIC_U")
FROM '/csv/df_Fid_Uid_link_hack.csv'
WITH (FORMAT csv, HEADER MATCH, DELIMITER ';', NULL '\N', ENCODING 'UTF8');

INSERT INTO bank_person_links (
  source_snapshot_id, source_row_number, synthetic_f, synthetic_u,
  city_name, rc_name, raw_attributes
)
SELECT b.source_snapshot_id, c.source_row_number,
  btrim(c."SYNTHETIC_F"), btrim(c."SYNTHETIC_U"),
  nullif(btrim(c."CITY_NAME"), ''), nullif(btrim(c."RC_NAME"), ''),
  to_jsonb(c) - 'source_row_number'
FROM people_csv c CROSS JOIN bank_import_batches b WHERE b.dataset = 'people';

UPDATE bank_import_batches SET status = 'ready', row_count = CASE dataset
  WHEN 'company' THEN (SELECT count(*) FROM company_csv)
  WHEN 'payments' THEN (SELECT count(*) FROM payment_csv)
  WHEN 'people' THEN (SELECT count(*) FROM people_csv)
END;
COMMIT;

ANALYZE companies;
ANALYZE bank_company_data;
ANALYZE bank_payment_edges;
ANALYZE bank_person_links;
