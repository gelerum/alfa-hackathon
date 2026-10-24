BEGIN;

-- Alfa Hack: unified schema v1, PostgreSQL 17 + pgvector.
-- All identifiers and external data are treated as untrusted. Never infer the real identity
-- of SYNTHETIC_U / SYNTHETIC_F.
CREATE EXTENSION IF NOT EXISTS vector;

-- 01. Identity and authorization. company_id = synthetic U_... in demo.
CREATE TABLE companies (
  company_id text PRIMARY KEY,
  legal_name text, public_identifier text,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (length(company_id) BETWEEN 1 AND 128)
);
CREATE TABLE app_users (
  user_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  auth_subject text NOT NULL UNIQUE, display_name text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE company_memberships (
  membership_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id text NOT NULL REFERENCES companies(company_id),
  user_id uuid NOT NULL REFERENCES app_users(user_id),
  business_role text, access_role text NOT NULL DEFAULT 'viewer'
    CHECK (access_role IN ('viewer','analyst','admin')),
  is_active boolean NOT NULL DEFAULT true,
  joined_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (company_id,user_id),
  UNIQUE (membership_id,company_id),
  UNIQUE (user_id,company_id)
);
CREATE INDEX company_memberships_user_idx ON company_memberships(user_id,company_id);

-- 02. Three bank-provided CSVs, with exact per-row preservation and versioned imports.
CREATE TABLE bank_import_batches (
  source_snapshot_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  dataset text NOT NULL CHECK (dataset IN ('company','payments','people')),
  source_filename text NOT NULL,
  sha256 text, source_as_of date,
  currency text, revenue_year integer,
  imported_at timestamptz NOT NULL DEFAULT now(),
  status text NOT NULL DEFAULT 'loading' CHECK (status IN ('loading','ready','failed')),
  row_count bigint, metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE TABLE bank_company_data (
  bank_record_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_snapshot_id uuid NOT NULL REFERENCES bank_import_batches(source_snapshot_id),
  source_row_number bigint NOT NULL CHECK(source_row_number>0),
  company_id text NOT NULL REFERENCES companies(company_id),
  org_type text, okfs_name text, company_age_band text,
  eco_mark_label text, main_okved_code text, main_okved_name text,
  negativemark text, employees_fns_band text, employees_spark_band text,
  cl_type text, group_id text, registration_city text, registration_region text,
  revenue_raw text, revenue_value numeric,
  fts_flag boolean, fts_country_name text,
  loan_mark boolean, leasing_mark boolean, factoring_mark boolean,
  raw_attributes jsonb NOT NULL,
  imported_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(source_snapshot_id,source_row_number)
);
CREATE INDEX bank_company_data_company_idx ON bank_company_data(company_id,source_snapshot_id);
CREATE INDEX bank_company_data_group_idx ON bank_company_data(group_id) WHERE group_id IS NOT NULL;
CREATE INDEX bank_company_data_okved_idx ON bank_company_data(main_okved_code);
-- Payment endpoints may not be present in the company CSV: no FK on payer/beneficiary.
CREATE TABLE bank_payment_edges (
  payment_edge_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_snapshot_id uuid NOT NULL REFERENCES bank_import_batches(source_snapshot_id),
  source_row_number bigint NOT NULL CHECK(source_row_number>0),
  payer text NOT NULL, beneficiary text NOT NULL,
  pay_amt numeric, pay_cnt bigint,
  currency text, aggregation_from date, aggregation_to date,
  raw_attributes jsonb NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE(source_snapshot_id,source_row_number),
  CHECK(pay_cnt IS NULL OR pay_cnt>=0),
  CHECK(aggregation_to IS NULL OR aggregation_from IS NULL OR aggregation_to>=aggregation_from)
);
CREATE INDEX bank_payment_payer_idx ON bank_payment_edges(payer);
CREATE INDEX bank_payment_beneficiary_idx ON bank_payment_edges(beneficiary);
CREATE TABLE bank_person_links (
  person_link_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_snapshot_id uuid NOT NULL REFERENCES bank_import_batches(source_snapshot_id),
  source_row_number bigint NOT NULL CHECK(source_row_number>0),
  synthetic_f text NOT NULL, synthetic_u text NOT NULL,
  city_name text, rc_name text,
  raw_attributes jsonb NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE(source_snapshot_id,source_row_number)
);
CREATE INDEX bank_person_links_u_idx ON bank_person_links(synthetic_u);
CREATE INDEX bank_person_links_f_idx ON bank_person_links(synthetic_f);
CREATE TABLE company_payment_features (
  feature_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id text NOT NULL REFERENCES companies(company_id),
  source_snapshot_id uuid NOT NULL REFERENCES bank_import_batches(source_snapshot_id),
  method_version text NOT NULL,
  flow_direction text NOT NULL CHECK(flow_direction IN ('incoming','outgoing','both')),
  payload jsonb NOT NULL, -- totals, coverage, group/self-transfer exclusions, denominators
  currency text, aggregation_from date, aggregation_to date,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(company_id,source_snapshot_id,method_version,flow_direction)
);

-- 03. Inputs from interviews / web / documents. These may be absent.
CREATE TABLE profile_interviews (
  interview_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id text NOT NULL REFERENCES companies(company_id),
  user_id uuid NOT NULL REFERENCES app_users(user_id),
  status text NOT NULL DEFAULT 'active' CHECK(status IN ('active','completed','cancelled')),
  started_at timestamptz NOT NULL DEFAULT now(), ended_at timestamptz,
  UNIQUE(interview_id,company_id),
  FOREIGN KEY(user_id,company_id) REFERENCES company_memberships(user_id,company_id)
);
CREATE TABLE profile_interview_messages (
  message_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  interview_id uuid NOT NULL REFERENCES profile_interviews(interview_id),
  sequence_no integer NOT NULL CHECK(sequence_no>=1),
  speaker text NOT NULL CHECK(speaker IN ('user','assistant','system')),
  content text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(interview_id,sequence_no)
);
CREATE TABLE documents (
  document_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id text REFERENCES companies(company_id),
  source_type text NOT NULL CHECK(source_type IN ('user_upload','public_web','bank_analytics')),
  title text NOT NULL, source_url text, storage_key text,
  content_sha256 text, published_at timestamptz,
  fetched_at timestamptz NOT NULL DEFAULT now(),
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX documents_company_idx ON documents(company_id);

-- 04. Embedding index, shared by news / search facets / RAG chunks.
-- v1 deliberately locks the vector dimension to 1024, agreed baseline candidate.
CREATE TABLE embedding_configs (
  embedding_config_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  config_key text NOT NULL UNIQUE, model_id text NOT NULL, model_revision text NOT NULL,
  dimensions integer NOT NULL DEFAULT 1024 CHECK(dimensions=1024),
  distance_metric text NOT NULL DEFAULT 'cosine' CHECK(distance_metric='cosine'),
  query_template text, document_template text,
  normalize boolean NOT NULL DEFAULT true,
  extra jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE search_embeddings (
  embedding_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  embedding_config_id uuid NOT NULL REFERENCES embedding_configs(embedding_config_id),
  encoding_role text NOT NULL CHECK(encoding_role IN ('query','document')),
  text_sha256 text NOT NULL, embedding vector(1024) NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(embedding_config_id,encoding_role,text_sha256),
  UNIQUE(embedding_id,embedding_config_id,encoding_role)
);
CREATE INDEX search_embeddings_hnsw_idx ON search_embeddings
  USING hnsw (embedding vector_cosine_ops);
CREATE TABLE document_chunks (
  chunk_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id uuid NOT NULL REFERENCES documents(document_id),
  ordinal integer NOT NULL CHECK(ordinal>=0),
  content text NOT NULL, embedding_id uuid REFERENCES search_embeddings(embedding_id),
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE(document_id,ordinal)
);

-- 05. Parser and archived news (not physically deleted after feed time window).
CREATE TABLE news_events (
  event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  title text, first_observed_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE news (
  article_id text PRIMARY KEY,
  canonical_url text NOT NULL UNIQUE,
  source_name text NOT NULL, source_domain text,
  title text NOT NULL, body text,
  description text, language text,
  published_at timestamptz, fetched_at timestamptz NOT NULL DEFAULT now(),
  content_version text NOT NULL,
  duplicate_of_id text REFERENCES news(article_id),
  event_id uuid REFERENCES news_events(event_id),
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  CHECK(duplicate_of_id IS DISTINCT FROM article_id)
);
CREATE INDEX news_published_idx ON news(published_at DESC);
CREATE INDEX news_event_idx ON news(event_id) WHERE event_id IS NOT NULL;
CREATE INDEX news_fts_idx ON news USING gin (
  to_tsvector('russian', coalesce(title,'') || ' ' || coalesce(body,'') || ' ' || coalesce(description,''))
);
CREATE TABLE news_revisions (
  article_id text NOT NULL REFERENCES news(article_id),
  content_version text NOT NULL,
  title text NOT NULL, body text, description text,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(article_id,content_version)
);
CREATE TABLE news_embeddings (
  article_id text NOT NULL, content_version text NOT NULL,
  embedding_config_id uuid NOT NULL REFERENCES embedding_configs(embedding_config_id),
  embedding vector(1024) NOT NULL, -- dedicated ANN index for news, not mixed with RAG docs
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(article_id,content_version,embedding_config_id),
  FOREIGN KEY(article_id,content_version) REFERENCES news_revisions(article_id,content_version)
);
CREATE INDEX news_embeddings_hnsw_idx ON news_embeddings
  USING hnsw (embedding vector_cosine_ops);

-- 06. Evidence registry and normalized company knowledge. No duplicated Search_info store.
CREATE TABLE evidence_sources (
  source_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id text REFERENCES companies(company_id),
  source_type text NOT NULL CHECK(source_type IN ('bank_import','user_answer','public_web','document','bank_analytics','news','derived')),
  source_ref jsonb NOT NULL,
  source_url text, excerpt text,
  source_date timestamptz, observed_at timestamptz NOT NULL DEFAULT now(),
  document_id uuid REFERENCES documents(document_id),
  article_id text REFERENCES news(article_id)
);
CREATE TABLE company_facts (
  fact_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id text NOT NULL REFERENCES companies(company_id),
  field text NOT NULL, value jsonb NOT NULL, unit text,
  source_type text NOT NULL CHECK(source_type IN ('bank_import','user_answer','public_web','document','derived')),
  source_ref jsonb NOT NULL DEFAULT '{}'::jsonb, source_url text, evidence_text text,
  source_date timestamptz, observed_at timestamptz NOT NULL DEFAULT now(),
  assertion_type text NOT NULL CHECK(assertion_type IN ('reported','calculated','hypothesis')),
  status text NOT NULL DEFAULT 'candidate' CHECK(status IN ('candidate','usable','rejected')),
  validation_reason text, supersedes_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(fact_id,company_id),
  FOREIGN KEY(supersedes_id,company_id) REFERENCES company_facts(fact_id,company_id)
);
CREATE INDEX company_facts_company_field_idx ON company_facts(company_id,field,status);
CREATE INDEX company_facts_source_idx ON company_facts(source_type,company_id);
CREATE TABLE fact_sources (
  fact_id uuid NOT NULL REFERENCES company_facts(fact_id),
  source_id uuid NOT NULL REFERENCES evidence_sources(source_id),
  relation text NOT NULL DEFAULT 'supports' CHECK(relation IN ('supports','contradicts','context')),
  PRIMARY KEY(fact_id,source_id)
);
CREATE TABLE company_goals (
  goal_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id text NOT NULL REFERENCES companies(company_id),
  text text NOT NULL, lifecycle_status text NOT NULL DEFAULT 'proposed'
    CHECK(lifecycle_status IN ('proposed','active','paused','completed','cancelled')),
  source_type text NOT NULL CHECK(source_type IN ('bank_import','user_answer','public_web','document','derived')),
  source_ref jsonb NOT NULL DEFAULT '{}'::jsonb,
  author_user_id uuid REFERENCES app_users(user_id),
  expires_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(goal_id,company_id),
  FOREIGN KEY(author_user_id,company_id) REFERENCES company_memberships(user_id,company_id)
);
CREATE TABLE company_goal_facts (
  goal_id uuid NOT NULL,
  fact_id uuid NOT NULL,
  company_id text NOT NULL,
  PRIMARY KEY(goal_id,fact_id),
  FOREIGN KEY(goal_id,company_id) REFERENCES company_goals(goal_id,company_id),
  FOREIGN KEY(fact_id,company_id) REFERENCES company_facts(fact_id,company_id)
);

-- 07. Immutable published company and user context versions.
CREATE TABLE company_profiles (
  profile_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id text NOT NULL REFERENCES companies(company_id),
  profile_version integer NOT NULL CHECK(profile_version>=1),
  status text NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','ready','failed')),
  payload jsonb NOT NULL DEFAULT '{}'::jsonb, -- full VALUES and reasoning limitations snapshot
  missing_data jsonb NOT NULL DEFAULT '[]'::jsonb,
  conflicts jsonb NOT NULL DEFAULT '[]'::jsonb,
  source_snapshot_id uuid REFERENCES bank_import_batches(source_snapshot_id),
  generator_version text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(), published_at timestamptz,
  UNIQUE(company_id,profile_version), UNIQUE(profile_id,company_id),
  CHECK(status<>'ready' OR published_at IS NOT NULL)
);
CREATE INDEX company_profiles_latest_idx ON company_profiles(company_id,profile_version DESC) WHERE status='ready';
-- Multiple bank input snapshots can contribute: company, payment, people.
CREATE TABLE company_profile_bank_sources (
  profile_id uuid NOT NULL REFERENCES company_profiles(profile_id),
  source_snapshot_id uuid NOT NULL REFERENCES bank_import_batches(source_snapshot_id),
  PRIMARY KEY(profile_id,source_snapshot_id)
);
CREATE TABLE company_profile_facts (
  profile_id uuid NOT NULL, fact_id uuid NOT NULL, company_id text NOT NULL,
  PRIMARY KEY(profile_id,fact_id),
  FOREIGN KEY(profile_id,company_id) REFERENCES company_profiles(profile_id,company_id),
  FOREIGN KEY(fact_id,company_id) REFERENCES company_facts(fact_id,company_id)
);
CREATE TABLE company_profile_goals (
  profile_id uuid NOT NULL, goal_id uuid NOT NULL, company_id text NOT NULL,
  PRIMARY KEY(profile_id,goal_id),
  FOREIGN KEY(profile_id,company_id) REFERENCES company_profiles(profile_id,company_id),
  FOREIGN KEY(goal_id,company_id) REFERENCES company_goals(goal_id,company_id)
);
CREATE TABLE user_preferences (
  user_context_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id text NOT NULL REFERENCES companies(company_id),
  user_id uuid NOT NULL REFERENCES app_users(user_id),
  context_version integer NOT NULL CHECK(context_version>=1),
  status text NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','ready','failed')),
  business_role text, responsibility text,
  focus_topics text[] NOT NULL DEFAULT '{}', interest_markets text[] NOT NULL DEFAULT '{}',
  watchlist text[] NOT NULL DEFAULT '{}', answer_depth text,
  raw_answers jsonb NOT NULL DEFAULT '[]'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(), published_at timestamptz,
  UNIQUE(user_id,company_id,context_version),
  UNIQUE(user_context_id,company_id),
  UNIQUE(user_context_id,company_id,user_id),
  FOREIGN KEY(user_id,company_id) REFERENCES company_memberships(user_id,company_id),
  CHECK(status<>'ready' OR published_at IS NOT NULL)
);
CREATE INDEX user_preferences_latest_idx ON user_preferences(company_id,user_id,context_version DESC) WHERE status='ready';
CREATE TABLE user_preference_goals (
  goal_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_context_id uuid NOT NULL, company_id text NOT NULL,
  text text NOT NULL, source_answer_id uuid REFERENCES profile_interview_messages(message_id),
  created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz,
  UNIQUE(user_context_id,goal_id),
  FOREIGN KEY(user_context_id,company_id) REFERENCES user_preferences(user_context_id,company_id)
);

-- 08. ONE table of corporate and personalized searchable aspects.
-- A personalized facet can combine company profile information and user interests in one query.
CREATE TABLE company_context_facets (
  facet_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id text NOT NULL REFERENCES companies(company_id),
  profile_id uuid NOT NULL,
  scope text NOT NULL CHECK(scope IN ('company','user_company')),
  user_context_id uuid,
  facet_type text NOT NULL CHECK(facet_type IN (
    'activity','counterparty_network','corporate_group','financial_exposure',
    'foreign_trade','strategic_goals','user_goals','user_focus','watchlist','other')),
  text text NOT NULL, keywords text[] NOT NULL DEFAULT '{}',
  entities jsonb NOT NULL DEFAULT '[]'::jsonb,
  embedding_config_id uuid NOT NULL REFERENCES embedding_configs(embedding_config_id),
  embedding_id uuid,
  encoding_role text NOT NULL DEFAULT 'query' CHECK(encoding_role='query'),
  text_hash text NOT NULL, generation_version text NOT NULL,
  status text NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','ready','failed')),
  created_at timestamptz NOT NULL DEFAULT now(), published_at timestamptz,
  UNIQUE(facet_id,profile_id), UNIQUE(facet_id,user_context_id),
  FOREIGN KEY(profile_id,company_id) REFERENCES company_profiles(profile_id,company_id),
  FOREIGN KEY(user_context_id,company_id) REFERENCES user_preferences(user_context_id,company_id),
  FOREIGN KEY(embedding_id,embedding_config_id,encoding_role)
    REFERENCES search_embeddings(embedding_id,embedding_config_id,encoding_role),
  CHECK(
    (scope='company' AND user_context_id IS NULL)
    OR (scope='user_company' AND user_context_id IS NOT NULL)
  ),
  CHECK(status<>'ready' OR (embedding_id IS NOT NULL AND published_at IS NOT NULL))
);
CREATE INDEX facets_profile_idx ON company_context_facets(profile_id,scope,status);
CREATE INDEX facets_user_idx ON company_context_facets(user_context_id,status) WHERE user_context_id IS NOT NULL;
CREATE TABLE facet_facts (
  facet_id uuid NOT NULL, profile_id uuid NOT NULL, fact_id uuid NOT NULL,
  PRIMARY KEY(facet_id,fact_id),
  FOREIGN KEY(facet_id,profile_id) REFERENCES company_context_facets(facet_id,profile_id),
  FOREIGN KEY(profile_id,fact_id) REFERENCES company_profile_facts(profile_id,fact_id)
);
CREATE TABLE facet_company_goals (
  facet_id uuid NOT NULL, profile_id uuid NOT NULL, goal_id uuid NOT NULL,
  PRIMARY KEY(facet_id,goal_id),
  FOREIGN KEY(facet_id,profile_id) REFERENCES company_context_facets(facet_id,profile_id),
  FOREIGN KEY(profile_id,goal_id) REFERENCES company_profile_goals(profile_id,goal_id)
);
CREATE TABLE facet_preference_goals (
  facet_id uuid NOT NULL, user_context_id uuid NOT NULL, goal_id uuid NOT NULL,
  PRIMARY KEY(facet_id,goal_id),
  FOREIGN KEY(facet_id,user_context_id) REFERENCES company_context_facets(facet_id,user_context_id),
  FOREIGN KEY(user_context_id,goal_id) REFERENCES user_preference_goals(user_context_id,goal_id)
);

-- 09. Retrieval / Ranking, with optional second behavioral reranker.
CREATE TABLE ranking_runs (
  ranking_run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id text NOT NULL REFERENCES companies(company_id),
  user_id uuid,
  profile_id uuid NOT NULL,
  user_context_id uuid,
  as_of timestamptz NOT NULL DEFAULT now(), published_after timestamptz,
  top_k integer NOT NULL CHECK(top_k BETWEEN 1 AND 1000),
  retrieval_config_version text NOT NULL, ranking_config_version text NOT NULL,
  ranker1_version text NOT NULL, ranker2_version text,
  ranking_mode text NOT NULL DEFAULT 'context' CHECK(ranking_mode IN ('context','behavior')),
  embedding_config_id uuid NOT NULL REFERENCES embedding_configs(embedding_config_id),
  feature_pipeline_version text,
  result_unit text NOT NULL DEFAULT 'article' CHECK(result_unit='article'),
  context_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
  policy_id text, status text NOT NULL DEFAULT 'running' CHECK(status IN ('running','completed','failed')),
  created_at timestamptz NOT NULL DEFAULT now(), completed_at timestamptz,
  UNIQUE(ranking_run_id,company_id),
  FOREIGN KEY(profile_id,company_id) REFERENCES company_profiles(profile_id,company_id),
  FOREIGN KEY(user_context_id,company_id,user_id) REFERENCES user_preferences(user_context_id,company_id,user_id),
  CHECK ((user_context_id IS NULL AND user_id IS NULL) OR (user_context_id IS NOT NULL AND user_id IS NOT NULL))
);
CREATE INDEX ranking_runs_company_created_idx ON ranking_runs(company_id,created_at DESC);
-- ALL scored candidates go here; rank=NULL means not in Top-K.
CREATE TABLE ranking_items (
  ranking_item_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  ranking_run_id uuid NOT NULL REFERENCES ranking_runs(ranking_run_id),
  article_id text NOT NULL, content_version text NOT NULL,
  event_id uuid REFERENCES news_events(event_id),
  retrieval_hits jsonb NOT NULL DEFAULT '[]'::jsonb,
  features_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
  context_score double precision, behavior_score double precision,
  score double precision, score_type text,
  rank integer CHECK(rank IS NULL OR rank>=1),
  reason_codes text[] NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(ranking_run_id,article_id),
  FOREIGN KEY(article_id,content_version) REFERENCES news_revisions(article_id,content_version)
);
CREATE UNIQUE INDEX ranking_items_rank_unique_idx ON ranking_items(ranking_run_id,rank) WHERE rank IS NOT NULL;
CREATE INDEX ranking_items_run_idx ON ranking_items(ranking_run_id,rank);
CREATE TABLE ranking_item_facts (
  ranking_item_id uuid NOT NULL REFERENCES ranking_items(ranking_item_id),
  profile_id uuid NOT NULL, fact_id uuid NOT NULL,
  PRIMARY KEY(ranking_item_id,fact_id),
  FOREIGN KEY(profile_id,fact_id) REFERENCES company_profile_facts(profile_id,fact_id)
);
CREATE TABLE ranking_item_company_goals (
  ranking_item_id uuid NOT NULL REFERENCES ranking_items(ranking_item_id),
  profile_id uuid NOT NULL, goal_id uuid NOT NULL,
  PRIMARY KEY(ranking_item_id,goal_id),
  FOREIGN KEY(profile_id,goal_id) REFERENCES company_profile_goals(profile_id,goal_id)
);
CREATE TABLE ranking_item_preference_goals (
  ranking_item_id uuid NOT NULL REFERENCES ranking_items(ranking_item_id),
  user_context_id uuid NOT NULL, goal_id uuid NOT NULL,
  PRIMARY KEY(ranking_item_id,goal_id),
  FOREIGN KEY(user_context_id,goal_id) REFERENCES user_preference_goals(user_context_id,goal_id)
);

-- 10. Frontend presentation and behavioral feedback (optional Ranker 2 training data).
CREATE TABLE impressions (
  impression_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  ranking_run_id uuid NOT NULL,
  company_id text NOT NULL, user_id uuid NOT NULL REFERENCES app_users(user_id),
  item_type text NOT NULL CHECK(item_type IN ('article','event','insight')),
  item_id text NOT NULL, -- article_id / event_id / insight_id, depending on item_type
  position integer NOT NULL CHECK(position>=1), surface text NOT NULL,
  visible_at timestamptz NOT NULL, visibility_rule_version text NOT NULL,
  candidate_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
  policy_id text, propensity double precision CHECK(propensity>0 AND propensity<=1),
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY(ranking_run_id,company_id) REFERENCES ranking_runs(ranking_run_id,company_id),
  FOREIGN KEY(user_id,company_id) REFERENCES company_memberships(user_id,company_id)
);
CREATE INDEX impressions_user_history_idx ON impressions(company_id,user_id,visible_at DESC);
CREATE INDEX impressions_ranking_run_idx ON impressions(ranking_run_id);
CREATE TABLE interaction_events (
  interaction_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  impression_id uuid NOT NULL REFERENCES impressions(impression_id),
  event_type text NOT NULL CHECK(event_type IN ('open','save','useful','not_useful','hide','read')),
  event_at timestamptz NOT NULL, received_at timestamptz NOT NULL DEFAULT now(),
  properties jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX interactions_impression_idx ON interaction_events(impression_id,event_at);

-- 11. Agent loop evidence and explainable business insights.
CREATE TABLE agent_runs (
  agent_run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_name text NOT NULL CHECK(agent_name IN ('company_profiler','insight_agent')),
  company_id text NOT NULL REFERENCES companies(company_id),
  user_id uuid, ranking_run_id uuid,
  llm_model text, input_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
  status text NOT NULL DEFAULT 'running' CHECK(status IN ('running','completed','failed','cancelled')),
  started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
  FOREIGN KEY(user_id,company_id) REFERENCES company_memberships(user_id,company_id),
  FOREIGN KEY(ranking_run_id,company_id) REFERENCES ranking_runs(ranking_run_id,company_id)
);
CREATE TABLE agent_steps (
  agent_step_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_run_id uuid NOT NULL REFERENCES agent_runs(agent_run_id),
  step_no integer NOT NULL CHECK(step_no>=1),
  step_kind text NOT NULL CHECK(step_kind IN ('plan','tool_call','tool_result','validation','synthesis')),
  tool_name text, arguments jsonb, result_summary jsonb,
  status text NOT NULL CHECK(status IN ('ok','error','skipped')),
  started_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
  UNIQUE(agent_run_id,step_no)
);
CREATE TABLE insights (
  insight_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id text NOT NULL REFERENCES companies(company_id),
  profile_id uuid NOT NULL, user_context_id uuid,
  ranking_run_id uuid, agent_run_id uuid REFERENCES agent_runs(agent_run_id),
  title text NOT NULL, event_summary text, business_impact text,
  risks jsonb NOT NULL DEFAULT '[]'::jsonb,
  opportunities jsonb NOT NULL DEFAULT '[]'::jsonb,
  recommendations jsonb NOT NULL DEFAULT '[]'::jsonb,
  status text NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','published','rejected')),
  created_at timestamptz NOT NULL DEFAULT now(), published_at timestamptz,
  FOREIGN KEY(profile_id,company_id) REFERENCES company_profiles(profile_id,company_id),
  FOREIGN KEY(user_context_id,company_id) REFERENCES user_preferences(user_context_id,company_id),
  FOREIGN KEY(ranking_run_id,company_id) REFERENCES ranking_runs(ranking_run_id,company_id)
);
CREATE INDEX insights_company_idx ON insights(company_id,created_at DESC);
CREATE TABLE insight_articles (
  insight_id uuid NOT NULL REFERENCES insights(insight_id),
  article_id text NOT NULL, content_version text NOT NULL,
  PRIMARY KEY(insight_id,article_id),
  FOREIGN KEY(article_id,content_version) REFERENCES news_revisions(article_id,content_version)
);
CREATE TABLE insight_claims (
  claim_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  insight_id uuid NOT NULL REFERENCES insights(insight_id),
  kind text NOT NULL CHECK(kind IN (
    'verified_fact','bank_position','ai_inference','risk','opportunity','recommendation')),
  statement text NOT NULL,
  certainty text NOT NULL DEFAULT 'qualified' CHECK(certainty IN ('confirmed','qualified','unverified')),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE insight_claim_sources (
  claim_id uuid NOT NULL REFERENCES insight_claims(claim_id),
  source_id uuid NOT NULL REFERENCES evidence_sources(source_id),
  relation text NOT NULL CHECK(relation IN ('supports','contradicts','context')),
  PRIMARY KEY(claim_id,source_id)
);
CREATE TABLE insight_claim_facts (
  claim_id uuid NOT NULL REFERENCES insight_claims(claim_id),
  fact_id uuid NOT NULL REFERENCES company_facts(fact_id),
  PRIMARY KEY(claim_id,fact_id)
);

-- Integrity rules that require looking across rows. Safe if application clients use SQL tools.
CREATE FUNCTION deny_changed_published_record() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF OLD.status = 'ready' THEN RAISE EXCEPTION 'immutable published record: %', TG_TABLE_NAME; END IF;
    RETURN OLD;
  END IF;
  IF OLD.status = 'ready' THEN
    RAISE EXCEPTION 'immutable published record: %', TG_TABLE_NAME;
  END IF;
  RETURN NEW;
END; $$;
CREATE TRIGGER company_profiles_immutable BEFORE UPDATE OR DELETE ON company_profiles
  FOR EACH ROW EXECUTE FUNCTION deny_changed_published_record();
CREATE TRIGGER user_preferences_immutable BEFORE UPDATE OR DELETE ON user_preferences
  FOR EACH ROW EXECUTE FUNCTION deny_changed_published_record();
CREATE TRIGGER company_context_facets_immutable BEFORE UPDATE OR DELETE ON company_context_facets
  FOR EACH ROW EXECUTE FUNCTION deny_changed_published_record();

CREATE FUNCTION protect_profile_edges() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE profile_state text; target_id uuid;
BEGIN
  IF TG_OP = 'DELETE' THEN target_id := OLD.profile_id; ELSE target_id := NEW.profile_id; END IF;
  SELECT status INTO profile_state FROM company_profiles
  WHERE profile_id = target_id;
  IF profile_state = 'ready' THEN RAISE EXCEPTION 'cannot change published profile membership'; END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END; $$;
CREATE TRIGGER protect_profile_fact_edges BEFORE INSERT OR UPDATE OR DELETE ON company_profile_facts
  FOR EACH ROW EXECUTE FUNCTION protect_profile_edges();
CREATE TRIGGER protect_profile_goal_edges BEFORE INSERT OR UPDATE OR DELETE ON company_profile_goals
  FOR EACH ROW EXECUTE FUNCTION protect_profile_edges();
CREATE TRIGGER protect_profile_bank_sources BEFORE INSERT OR UPDATE OR DELETE ON company_profile_bank_sources
  FOR EACH ROW EXECUTE FUNCTION protect_profile_edges();

CREATE FUNCTION protect_user_goal_edges() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE state text; target_id uuid;
BEGIN
  IF TG_OP = 'DELETE' THEN target_id := OLD.user_context_id; ELSE target_id := NEW.user_context_id; END IF;
  SELECT status INTO state FROM user_preferences
    WHERE user_context_id = target_id;
  IF state='ready' THEN RAISE EXCEPTION 'cannot modify goals of published user preferences'; END IF;
  IF TG_OP='DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END; $$;
CREATE TRIGGER protect_user_goals BEFORE INSERT OR UPDATE OR DELETE ON user_preference_goals
 FOR EACH ROW EXECUTE FUNCTION protect_user_goal_edges();

CREATE FUNCTION protect_facet_edges() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE state text; target_id uuid;
BEGIN
  IF TG_OP = 'DELETE' THEN target_id := OLD.facet_id; ELSE target_id := NEW.facet_id; END IF;
  SELECT status INTO state FROM company_context_facets
    WHERE facet_id = target_id;
  IF state='ready' THEN RAISE EXCEPTION 'cannot change evidence of ready search facet'; END IF;
  IF TG_OP='DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END; $$;
CREATE TRIGGER facet_fact_guard BEFORE INSERT OR UPDATE OR DELETE ON facet_facts
 FOR EACH ROW EXECUTE FUNCTION protect_facet_edges();
CREATE TRIGGER facet_company_goal_guard BEFORE INSERT OR UPDATE OR DELETE ON facet_company_goals
 FOR EACH ROW EXECUTE FUNCTION protect_facet_edges();
CREATE TRIGGER facet_user_goal_guard BEFORE INSERT OR UPDATE OR DELETE ON facet_preference_goals
 FOR EACH ROW EXECUTE FUNCTION protect_facet_edges();

-- Profiles are ready only after all company facets are ready and backed by vectors.
CREATE FUNCTION validate_profile_publication() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE all_facets integer; ready_facets integer; config_count integer;
BEGIN
  IF NEW.status = 'ready' AND OLD.status IS DISTINCT FROM 'ready' THEN
    IF NEW.published_at IS NULL THEN NEW.published_at := now(); END IF;
    SELECT count(*),count(*) FILTER(WHERE status='ready'),
           count(DISTINCT embedding_config_id)
      INTO all_facets,ready_facets,config_count
      FROM company_context_facets WHERE profile_id=NEW.profile_id AND scope='company';
    IF all_facets=0 OR all_facets<>ready_facets OR config_count<>1 THEN
      RAISE EXCEPTION 'company profile % not publishable: % company facets, % ready, % embedding configurations',
        NEW.profile_id,all_facets,ready_facets,config_count;
    END IF;
    IF EXISTS(
      SELECT 1 FROM company_profile_facts pf JOIN company_facts f ON f.fact_id=pf.fact_id
      WHERE pf.profile_id=NEW.profile_id AND f.status<>'usable'
    ) THEN RAISE EXCEPTION 'profile contains non-usable fact'; END IF;
  END IF;
  RETURN NEW;
END; $$;
CREATE TRIGGER profile_publish_guard BEFORE UPDATE ON company_profiles
 FOR EACH ROW EXECUTE FUNCTION validate_profile_publication();

-- Block additional corporate facets after profile was published. Personal facets may be built later.
CREATE FUNCTION validate_facet_changes() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE state text; pref_state text;
BEGIN
  SELECT status INTO state FROM company_profiles WHERE profile_id=NEW.profile_id;
  IF NEW.scope='company' AND state='ready' AND TG_OP='INSERT' THEN
    RAISE EXCEPTION 'new corporate facet requires a new company profile';
  END IF;
  IF NEW.user_context_id IS NOT NULL THEN
    SELECT status INTO pref_state FROM user_preferences WHERE user_context_id=NEW.user_context_id;
    IF pref_state<>'ready' THEN RAISE EXCEPTION 'personal facet requires a ready user context'; END IF;
    IF state<>'ready' THEN RAISE EXCEPTION 'personal facet requires a ready company profile'; END IF;
  END IF;
  IF NEW.status='ready' AND NEW.published_at IS NULL THEN NEW.published_at := now(); END IF;
  IF NEW.status='ready' AND NEW.embedding_id IS NULL THEN
    RAISE EXCEPTION 'ready facet requires embedding';
  END IF;
  RETURN NEW;
END; $$;
CREATE TRIGGER facet_validate BEFORE INSERT OR UPDATE ON company_context_facets
 FOR EACH ROW EXECUTE FUNCTION validate_facet_changes();

CREATE FUNCTION validate_ranker_run() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE cp_status text; up_status text;
BEGIN
  SELECT status INTO cp_status FROM company_profiles WHERE profile_id=NEW.profile_id;
  IF cp_status <> 'ready' THEN RAISE EXCEPTION 'ranking requires ready profile'; END IF;
  IF NEW.user_context_id IS NOT NULL THEN
    SELECT status INTO up_status FROM user_preferences WHERE user_context_id=NEW.user_context_id;
    IF up_status <> 'ready' THEN RAISE EXCEPTION 'ranking requires ready user context'; END IF;
  END IF;
  IF NEW.ranking_mode='behavior' AND NEW.ranker2_version IS NULL THEN
    RAISE EXCEPTION 'behavior ranking needs ranker2_version';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM company_context_facets
    WHERE profile_id=NEW.profile_id AND scope='company' AND status='ready'
      AND embedding_config_id=NEW.embedding_config_id
  ) THEN RAISE EXCEPTION 'no compatible ready search aspects for ranking'; END IF;
  RETURN NEW;
END; $$;
CREATE TRIGGER ranker_run_validate BEFORE INSERT ON ranking_runs
 FOR EACH ROW EXECUTE FUNCTION validate_ranker_run();

-- Supporting source references must match the precise context versions in the run.
CREATE FUNCTION check_ranking_item_fact() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE actual_profile uuid;
BEGIN
 SELECT rr.profile_id INTO actual_profile FROM ranking_items ri JOIN ranking_runs rr
 ON rr.ranking_run_id=ri.ranking_run_id WHERE ri.ranking_item_id=NEW.ranking_item_id;
 IF actual_profile IS DISTINCT FROM NEW.profile_id THEN RAISE EXCEPTION 'fact used with wrong profile'; END IF;
 RETURN NEW;
END; $$;
CREATE TRIGGER ranking_item_facts_validate BEFORE INSERT OR UPDATE ON ranking_item_facts
 FOR EACH ROW EXECUTE FUNCTION check_ranking_item_fact();
CREATE TRIGGER ranking_item_goals_validate BEFORE INSERT OR UPDATE ON ranking_item_company_goals
 FOR EACH ROW EXECUTE FUNCTION check_ranking_item_fact();
CREATE FUNCTION check_ranking_item_personal_goal() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE actual_context uuid;
BEGIN
 SELECT rr.user_context_id INTO actual_context FROM ranking_items ri JOIN ranking_runs rr
 ON rr.ranking_run_id=ri.ranking_run_id WHERE ri.ranking_item_id=NEW.ranking_item_id;
 IF actual_context IS DISTINCT FROM NEW.user_context_id THEN RAISE EXCEPTION 'personal goal used with wrong user context'; END IF;
 RETURN NEW;
END; $$;
CREATE TRIGGER ranking_item_personal_goals_validate BEFORE INSERT OR UPDATE ON ranking_item_preference_goals
 FOR EACH ROW EXECUTE FUNCTION check_ranking_item_personal_goal();

CREATE FUNCTION validate_impression() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE run_user uuid;
BEGIN
  SELECT user_id INTO run_user FROM ranking_runs WHERE ranking_run_id=NEW.ranking_run_id;
  IF run_user IS NOT NULL AND run_user <> NEW.user_id THEN
    RAISE EXCEPTION 'impression owner mismatch';
  END IF;
  IF NEW.item_type='article' AND NOT EXISTS(
    SELECT 1 FROM ranking_items WHERE ranking_run_id=NEW.ranking_run_id
      AND article_id=NEW.item_id AND rank IS NOT NULL
  ) THEN RAISE EXCEPTION 'article impression not present in visible ranking'; END IF;
  RETURN NEW;
END; $$;
CREATE TRIGGER impression_guard BEFORE INSERT ON impressions
 FOR EACH ROW EXECUTE FUNCTION validate_impression();

-- Historical content and published snapshots must remain reproducible.
CREATE FUNCTION prevent_row_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'append-only table: %', TG_TABLE_NAME; END; $$;
CREATE TRIGGER embedding_append_only BEFORE UPDATE OR DELETE ON search_embeddings
 FOR EACH ROW EXECUTE FUNCTION prevent_row_mutation();
CREATE TRIGGER news_revision_append_only BEFORE UPDATE OR DELETE ON news_revisions
 FOR EACH ROW EXECUTE FUNCTION prevent_row_mutation();
CREATE TRIGGER bank_company_append_only BEFORE UPDATE OR DELETE ON bank_company_data
 FOR EACH ROW EXECUTE FUNCTION prevent_row_mutation();
CREATE TRIGGER bank_payment_append_only BEFORE UPDATE OR DELETE ON bank_payment_edges
 FOR EACH ROW EXECUTE FUNCTION prevent_row_mutation();
CREATE TRIGGER bank_person_append_only BEFORE UPDATE OR DELETE ON bank_person_links
 FOR EACH ROW EXECUTE FUNCTION prevent_row_mutation();

-- Fact values and source evidence are append-only: revisions get a fresh fact_id/source_id.
CREATE TRIGGER facts_append_only BEFORE UPDATE OR DELETE ON company_facts
 FOR EACH ROW EXECUTE FUNCTION prevent_row_mutation();
CREATE TRIGGER evidence_append_only BEFORE UPDATE OR DELETE ON evidence_sources
 FOR EACH ROW EXECUTE FUNCTION prevent_row_mutation();
CREATE TRIGGER news_embeddings_append_only BEFORE UPDATE OR DELETE ON news_embeddings
 FOR EACH ROW EXECUTE FUNCTION prevent_row_mutation();

CREATE FUNCTION protect_published_company_goal() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF EXISTS(SELECT 1 FROM company_profile_goals cg JOIN company_profiles cp
  ON cp.profile_id=cg.profile_id WHERE cg.goal_id=OLD.goal_id AND cp.status='ready') THEN
  RAISE EXCEPTION 'company goal already included in published profile';
 END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END; $$;
CREATE TRIGGER company_goals_used_immutable BEFORE UPDATE OR DELETE ON company_goals
 FOR EACH ROW EXECUTE FUNCTION protect_published_company_goal();

-- Ranking run is a factual trace: no edits once it has been completed.
CREATE FUNCTION protect_finalized_ranking_run() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF OLD.status='completed' THEN RAISE EXCEPTION 'completed ranking run is immutable'; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END; $$;
CREATE TRIGGER ranking_run_finalized_guard BEFORE UPDATE OR DELETE ON ranking_runs
 FOR EACH ROW EXECUTE FUNCTION protect_finalized_ranking_run();

CREATE FUNCTION protect_finalized_ranking_item() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE parent_run uuid; parent_state text;
BEGIN
 IF TG_OP='DELETE' THEN parent_run:=OLD.ranking_run_id;
 ELSE parent_run:=NEW.ranking_run_id; END IF;
 SELECT status INTO parent_state FROM ranking_runs WHERE ranking_run_id=parent_run;
 IF parent_state='completed' THEN RAISE EXCEPTION 'cannot modify candidates of completed ranking run'; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END; $$;
CREATE TRIGGER ranking_item_finalized_guard BEFORE INSERT OR UPDATE OR DELETE ON ranking_items
 FOR EACH ROW EXECUTE FUNCTION protect_finalized_ranking_item();

CREATE FUNCTION protect_finalized_ranking_evidence() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target_item uuid; parent_state text;
BEGIN
 IF TG_OP='DELETE' THEN target_item:=OLD.ranking_item_id;
 ELSE target_item:=NEW.ranking_item_id; END IF;
 SELECT rr.status INTO parent_state
 FROM ranking_items ri JOIN ranking_runs rr ON rr.ranking_run_id=ri.ranking_run_id
 WHERE ri.ranking_item_id=target_item;
 IF parent_state='completed' THEN RAISE EXCEPTION 'cannot change evidence of completed ranking run'; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END; $$;
CREATE TRIGGER ranking_item_fact_final_guard BEFORE INSERT OR UPDATE OR DELETE ON ranking_item_facts
 FOR EACH ROW EXECUTE FUNCTION protect_finalized_ranking_evidence();
CREATE TRIGGER ranking_item_company_goal_final_guard BEFORE INSERT OR UPDATE OR DELETE ON ranking_item_company_goals
 FOR EACH ROW EXECUTE FUNCTION protect_finalized_ranking_evidence();
CREATE TRIGGER ranking_item_user_goal_final_guard BEFORE INSERT OR UPDATE OR DELETE ON ranking_item_preference_goals
 FOR EACH ROW EXECUTE FUNCTION protect_finalized_ranking_evidence();

CREATE FUNCTION protect_published_insight() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF OLD.status='published' THEN RAISE EXCEPTION 'published insight is immutable'; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END; $$;
CREATE TRIGGER insight_published_guard BEFORE UPDATE OR DELETE ON insights
 FOR EACH ROW EXECUTE FUNCTION protect_published_insight();

-- Evidence cannot leak between companies. NULL evidence company means public/global source.
CREATE FUNCTION validate_fact_evidence_company() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE fact_company text; evidence_company text;
BEGIN
 SELECT company_id INTO fact_company FROM company_facts WHERE fact_id=NEW.fact_id;
 SELECT company_id INTO evidence_company FROM evidence_sources WHERE source_id=NEW.source_id;
 IF evidence_company IS NOT NULL AND evidence_company<>fact_company THEN
   RAISE EXCEPTION 'evidence source belongs to another company';
 END IF;
 RETURN NEW;
END; $$;
CREATE TRIGGER fact_evidence_company_guard BEFORE INSERT OR UPDATE ON fact_sources
 FOR EACH ROW EXECUTE FUNCTION validate_fact_evidence_company();

CREATE FUNCTION validate_insight_claim_evidence() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE insight_company text; evidence_company text;
BEGIN
 SELECT i.company_id INTO insight_company FROM insight_claims c
 JOIN insights i ON i.insight_id=c.insight_id WHERE c.claim_id=NEW.claim_id;
 IF TG_TABLE_NAME='insight_claim_facts' THEN
   SELECT company_id INTO evidence_company FROM company_facts WHERE fact_id=NEW.fact_id;
 ELSE
   SELECT company_id INTO evidence_company FROM evidence_sources WHERE source_id=NEW.source_id;
 END IF;
 IF evidence_company IS NOT NULL AND evidence_company<>insight_company THEN
   RAISE EXCEPTION 'insight claim cites another company data';
 END IF;
 RETURN NEW;
END; $$;
CREATE TRIGGER insight_claim_fact_company_guard BEFORE INSERT OR UPDATE ON insight_claim_facts
 FOR EACH ROW EXECUTE FUNCTION validate_insight_claim_evidence();
CREATE TRIGGER insight_claim_source_company_guard BEFORE INSERT OR UPDATE ON insight_claim_sources
 FOR EACH ROW EXECUTE FUNCTION validate_insight_claim_evidence();

CREATE VIEW current_company_profiles AS
  SELECT DISTINCT ON (company_id) * FROM company_profiles
  WHERE status='ready' ORDER BY company_id,profile_version DESC;
CREATE VIEW current_user_preferences AS
  SELECT DISTINCT ON (company_id,user_id) * FROM user_preferences
  WHERE status='ready' ORDER BY company_id,user_id,context_version DESC;
-- Search_info in teammate's diagram: a VIEW, never duplicated physical storage.
CREATE VIEW search_info AS
  SELECT * FROM company_facts WHERE source_type='public_web';
-- Only facets from the latest ready profile, plus applicable latest ready personal version.
-- Query requires company_id and optionally user_context_id to avoid mixing users.
CREATE VIEW company_facets_ready AS
  SELECT f.* FROM company_context_facets f
  JOIN current_company_profiles cp ON cp.profile_id=f.profile_id
  WHERE f.status='ready' AND f.scope='company';
CREATE VIEW ready_user_company_facets AS
  SELECT f.* FROM company_context_facets f
  JOIN current_user_preferences up ON up.user_context_id=f.user_context_id
  JOIN current_company_profiles cp ON cp.profile_id=f.profile_id
  WHERE f.status='ready' AND f.scope='user_company';
-- Attributed feedback. A non-impression is NOT a negative training sample.
CREATE VIEW feedback_history AS
  SELECT i.impression_id,i.company_id,i.user_id,i.ranking_run_id,
    i.item_type,i.item_id,i.position,i.visible_at,i.surface,
    i.policy_id,i.propensity,e.interaction_id,e.event_type,e.event_at
  FROM impressions i LEFT JOIN interaction_events e ON e.impression_id=i.impression_id;

COMMIT;
