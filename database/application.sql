BEGIN;
-- Application extension; loaded after schema.sql and CSV import on a fresh volume.
ALTER TABLE companies ADD COLUMN is_fictional boolean NOT NULL DEFAULT false;
ALTER TABLE documents ADD COLUMN processing_status text NOT NULL DEFAULT 'pending'
 CHECK(processing_status IN ('pending','processing','ready','failed'));
ALTER TABLE documents ADD COLUMN processing_error text;
ALTER TABLE documents ADD COLUMN original_filename text;
ALTER TABLE documents ADD COLUMN media_type text;
ALTER TABLE documents ADD COLUMN extraction_version text;
ALTER TABLE documents ADD COLUMN external_id text;
ALTER TABLE documents ADD COLUMN external_version text;
CREATE UNIQUE INDEX document_external_version ON documents(external_id,external_version)
 WHERE external_id IS NOT NULL;
CREATE INDEX document_content_lookup ON documents(company_id,content_sha256);

CREATE TABLE api_tokens (
 token_hash text PRIMARY KEY, user_id uuid NOT NULL REFERENCES app_users(user_id),
 created_at timestamptz NOT NULL DEFAULT now(), revoked_at timestamptz
);
CREATE TABLE onboarding_answers (
 answer_id uuid PRIMARY KEY DEFAULT gen_random_uuid(), company_id text NOT NULL,
 user_id uuid NOT NULL, answers jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY(user_id,company_id) REFERENCES company_memberships(user_id,company_id)
);
CREATE TRIGGER onboarding_answers_immutable BEFORE UPDATE OR DELETE ON onboarding_answers
 FOR EACH ROW EXECUTE FUNCTION prevent_row_mutation();
CREATE TABLE jobs (
 job_id uuid PRIMARY KEY DEFAULT gen_random_uuid(), kind text NOT NULL,
 idempotency_key text NOT NULL UNIQUE, payload jsonb NOT NULL,
 status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','running','completed','failed')),
 attempts integer NOT NULL DEFAULT 0, max_attempts integer NOT NULL DEFAULT 5,
 available_at timestamptz NOT NULL DEFAULT now(), lease_until timestamptz, lease_token uuid,
 last_error text, result jsonb, created_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz
);
CREATE INDEX jobs_claim ON jobs(kind,available_at) WHERE status IN ('pending','running');
CREATE TABLE operation_results (
 operation_key text PRIMARY KEY, result jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE source_cursors (
 source_name text PRIMARY KEY, cursor text NOT NULL DEFAULT '', updated_at timestamptz NOT NULL DEFAULT now()
);

-- Chunk index shared by documents and precise news revisions. Evidence is immutable;
-- vectors may be filled later, so lexical retrieval works during embedding outages.
CREATE TABLE rag_chunks (
 chunk_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 company_id text REFERENCES companies(company_id),
 kind text NOT NULL CHECK(kind IN ('company_document','bank_analytics','news')),
 document_id uuid REFERENCES documents(document_id), article_id text, content_version text,
 ordinal integer NOT NULL CHECK(ordinal>=0), title text NOT NULL, content text NOT NULL,
 metadata jsonb NOT NULL DEFAULT '{}'::jsonb, published_at timestamptz,
 observed_at timestamptz NOT NULL DEFAULT now(),
 source_id uuid NOT NULL UNIQUE REFERENCES evidence_sources(source_id),
 embedding_config_id uuid REFERENCES embedding_configs(embedding_config_id), embedding vector(1024),
 search_vector tsvector GENERATED ALWAYS AS
 (to_tsvector('russian',coalesce(title,'') || ' ' || content)) STORED,
 FOREIGN KEY(article_id,content_version) REFERENCES news_revisions(article_id,content_version),
 CHECK ((kind='news' AND article_id IS NOT NULL AND content_version IS NOT NULL AND document_id IS NULL AND company_id IS NULL)
 OR (kind='company_document' AND document_id IS NOT NULL AND article_id IS NULL AND company_id IS NOT NULL)
 OR (kind='bank_analytics' AND document_id IS NOT NULL AND article_id IS NULL AND company_id IS NULL)),
 CHECK((embedding IS NULL)=(embedding_config_id IS NULL))
);
CREATE UNIQUE INDEX rag_document_chunk ON rag_chunks(document_id,ordinal) WHERE document_id IS NOT NULL;
CREATE UNIQUE INDEX rag_news_chunk ON rag_chunks(article_id,content_version,ordinal) WHERE article_id IS NOT NULL;
CREATE INDEX rag_fts ON rag_chunks USING gin(search_vector);
CREATE INDEX rag_vectors ON rag_chunks USING hnsw(embedding vector_cosine_ops);
CREATE INDEX rag_scope ON rag_chunks(kind,company_id,published_at);

CREATE TABLE reports (
 report_id uuid PRIMARY KEY DEFAULT gen_random_uuid(), company_id text NOT NULL,
 user_id uuid NOT NULL, trigger_type text NOT NULL CHECK(trigger_type IN ('scheduled','on_demand')),
 window_start timestamptz NOT NULL, window_end timestamptz NOT NULL,
 deadline timestamptz NOT NULL, idempotency_key text NOT NULL UNIQUE,
 created_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY(user_id,company_id) REFERENCES company_memberships(user_id,company_id),
 CHECK(window_end-window_start=interval '7 days'), CHECK(deadline>=window_end)
);
CREATE TRIGGER report_identity_immutable BEFORE UPDATE OR DELETE ON reports
 FOR EACH ROW EXECUTE FUNCTION prevent_row_mutation();
CREATE TRIGGER operation_result_immutable BEFORE UPDATE OR DELETE ON operation_results
 FOR EACH ROW EXECUTE FUNCTION prevent_row_mutation();
CREATE TABLE report_versions (
 report_version_id uuid PRIMARY KEY DEFAULT gen_random_uuid(), report_id uuid NOT NULL REFERENCES reports(report_id),
 version integer NOT NULL CHECK(version>0), profile_id uuid, user_context_id uuid,
 ranking_run_id uuid REFERENCES ranking_runs(ranking_run_id), agent_run_id uuid REFERENCES agent_runs(agent_run_id),
 status text NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','published','failed')),
 summary text NOT NULL DEFAULT '', limitations jsonb NOT NULL DEFAULT '[]', changes jsonb NOT NULL DEFAULT '[]',
 input_snapshot jsonb NOT NULL DEFAULT '{}', published_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(report_id,version),
 CHECK(status<>'published' OR published_at IS NOT NULL)
);
ALTER TABLE insights ADD COLUMN details jsonb NOT NULL DEFAULT '{}';
ALTER TABLE agent_runs ADD COLUMN llm_tokens bigint NOT NULL DEFAULT 0;
ALTER TABLE agent_runs ADD COLUMN tool_calls integer NOT NULL DEFAULT 0 CHECK(tool_calls>=0);
CREATE TABLE report_insights (
 report_version_id uuid NOT NULL REFERENCES report_versions(report_version_id),
 insight_id uuid NOT NULL REFERENCES insights(insight_id), position integer NOT NULL CHECK(position>0),
 PRIMARY KEY(report_version_id,insight_id), UNIQUE(report_version_id,position)
);
CREATE TABLE discussions (
 discussion_id uuid PRIMARY KEY DEFAULT gen_random_uuid(), report_version_id uuid NOT NULL REFERENCES report_versions(report_version_id),
 user_id uuid NOT NULL REFERENCES app_users(user_id), created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE discussion_messages (
 message_id uuid PRIMARY KEY DEFAULT gen_random_uuid(), discussion_id uuid NOT NULL REFERENCES discussions(discussion_id),
 speaker text NOT NULL CHECK(speaker IN ('user','assistant')), content text NOT NULL,
 sources jsonb NOT NULL DEFAULT '[]', agent_run_id uuid REFERENCES agent_runs(agent_run_id),
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE ranking_requests (
 request_id uuid PRIMARY KEY DEFAULT gen_random_uuid(), report_id uuid NOT NULL REFERENCES reports(report_id),
 reason text NOT NULL, input_fingerprint text NOT NULL, profile_id uuid, user_context_id uuid,
 context_snapshot jsonb NOT NULL DEFAULT '{}',
 status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','sent','completed','failed')),
 last_error text, created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(report_id,input_fingerprint)
);
CREATE TABLE tool_audit (
 audit_id uuid PRIMARY KEY DEFAULT gen_random_uuid(), agent_run_id uuid REFERENCES agent_runs(agent_run_id),
 company_id text NOT NULL, user_id uuid NOT NULL, tool_name text NOT NULL,
 arguments jsonb NOT NULL, status text NOT NULL, result_count integer,
 created_at timestamptz NOT NULL DEFAULT now()
);

CREATE FUNCTION protect_report_version() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF OLD.status='published' THEN RAISE EXCEPTION 'published report version is immutable'; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER report_version_guard BEFORE UPDATE OR DELETE ON report_versions
 FOR EACH ROW EXECUTE FUNCTION protect_report_version();
CREATE FUNCTION protect_report_insight_edge() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target uuid;
BEGIN
 IF TG_OP='DELETE' THEN target:=OLD.report_version_id; ELSE target:=NEW.report_version_id; END IF;
 IF EXISTS(SELECT 1 FROM report_versions WHERE report_version_id=target AND status='published') THEN
 RAISE EXCEPTION 'published report membership is immutable'; END IF;
 IF TG_OP='UPDATE' AND EXISTS(SELECT 1 FROM report_versions WHERE report_version_id=OLD.report_version_id AND status='published') THEN
 RAISE EXCEPTION 'published report membership is immutable'; END IF;
 IF TG_OP<>'DELETE' AND NOT EXISTS(
 SELECT 1 FROM report_versions v JOIN reports r USING(report_id) JOIN insights i ON i.insight_id=NEW.insight_id
 WHERE v.report_version_id=NEW.report_version_id AND r.company_id=i.company_id AND v.profile_id=i.profile_id
 AND v.user_context_id IS NOT DISTINCT FROM i.user_context_id AND i.status='published') THEN
 RAISE EXCEPTION 'report insight context mismatch'; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF; RETURN NEW;
END $$;
CREATE TRIGGER report_insight_guard BEFORE INSERT OR UPDATE OR DELETE ON report_insights
 FOR EACH ROW EXECUTE FUNCTION protect_report_insight_edge();
CREATE FUNCTION protect_insight_children() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target uuid; old_target uuid;
BEGIN
 IF TG_TABLE_NAME IN ('insight_claim_sources','insight_claim_facts') THEN
 IF TG_OP='DELETE' THEN SELECT insight_id INTO target FROM insight_claims WHERE claim_id=OLD.claim_id;
 ELSE SELECT insight_id INTO target FROM insight_claims WHERE claim_id=NEW.claim_id; END IF;
 IF TG_OP='UPDATE' THEN SELECT insight_id INTO old_target FROM insight_claims WHERE claim_id=OLD.claim_id; END IF;
 ELSE
 IF TG_OP='DELETE' THEN target:=OLD.insight_id; ELSE target:=NEW.insight_id; END IF;
 IF TG_OP='UPDATE' THEN old_target:=OLD.insight_id; END IF;
 END IF;
 IF EXISTS(SELECT 1 FROM insights WHERE insight_id IN (target,old_target) AND status='published') THEN
 RAISE EXCEPTION 'published insight evidence is immutable'; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF; RETURN NEW;
END $$;
CREATE TRIGGER claim_guard BEFORE INSERT OR UPDATE OR DELETE ON insight_claims FOR EACH ROW EXECUTE FUNCTION protect_insight_children();
CREATE TRIGGER claim_source_guard BEFORE INSERT OR UPDATE OR DELETE ON insight_claim_sources FOR EACH ROW EXECUTE FUNCTION protect_insight_children();
CREATE TRIGGER claim_fact_guard BEFORE INSERT OR UPDATE OR DELETE ON insight_claim_facts FOR EACH ROW EXECUTE FUNCTION protect_insight_children();
CREATE TRIGGER insight_article_guard BEFORE INSERT OR UPDATE OR DELETE ON insight_articles FOR EACH ROW EXECUTE FUNCTION protect_insight_children();
-- Keep chunk text/locators stable while allowing deferred vector completion.
CREATE FUNCTION protect_rag_content() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'RAG chunks are append-only'; END IF;
 IF (to_jsonb(NEW)-'embedding'-'embedding_config_id'-'search_vector') IS DISTINCT FROM
    (to_jsonb(OLD)-'embedding'-'embedding_config_id'-'search_vector') THEN
 RAISE EXCEPTION 'RAG evidence content is immutable'; END IF;
 IF OLD.embedding IS NOT NULL THEN RAISE EXCEPTION 'RAG embedding is immutable'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER rag_content_guard BEFORE UPDATE OR DELETE ON rag_chunks FOR EACH ROW EXECUTE FUNCTION protect_rag_content();
CREATE FUNCTION validate_report_publication() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE r reports; p company_profiles; u user_preferences;
BEGIN
 SELECT * INTO r FROM reports WHERE report_id=NEW.report_id;
 IF NEW.profile_id IS NOT NULL THEN
 SELECT * INTO p FROM company_profiles WHERE profile_id=NEW.profile_id;
 IF p.company_id IS DISTINCT FROM r.company_id OR p.status<>'ready' THEN RAISE EXCEPTION 'report profile mismatch'; END IF;
 END IF;
 IF NEW.user_context_id IS NOT NULL THEN
 SELECT * INTO u FROM user_preferences WHERE user_context_id=NEW.user_context_id;
 IF u.company_id IS DISTINCT FROM r.company_id OR u.user_id IS DISTINCT FROM r.user_id OR u.status<>'ready' THEN
 RAISE EXCEPTION 'report user context mismatch'; END IF;
 END IF;
 IF NEW.status='published' AND NEW.profile_id IS NULL AND NOT (NEW.input_snapshot @> '{"deadline_fallback":true}'::jsonb) THEN
 RAISE EXCEPTION 'analytical report requires pinned context'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER report_publication_guard BEFORE INSERT OR UPDATE ON report_versions
 FOR EACH ROW EXECUTE FUNCTION validate_report_publication();
CREATE FUNCTION validate_insight_publication() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.status='published' THEN
 IF NOT EXISTS(SELECT 1 FROM company_profiles WHERE profile_id=NEW.profile_id AND company_id=NEW.company_id AND status='ready') THEN
 RAISE EXCEPTION 'insight requires ready matching company profile'; END IF;
 IF NEW.agent_run_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM agent_runs WHERE agent_run_id=NEW.agent_run_id AND company_id=NEW.company_id) THEN
 RAISE EXCEPTION 'insight agent company mismatch'; END IF;
 IF NEW.published_at IS NULL OR NOT EXISTS(SELECT 1 FROM insight_claims WHERE insight_id=NEW.insight_id) THEN
 RAISE EXCEPTION 'published insight requires timestamp and claims'; END IF;
 IF EXISTS(SELECT 1 FROM insight_claims c WHERE c.insight_id=NEW.insight_id
 AND NOT EXISTS(SELECT 1 FROM insight_claim_sources s WHERE s.claim_id=c.claim_id)
 AND NOT EXISTS(SELECT 1 FROM insight_claim_facts f WHERE f.claim_id=c.claim_id)) THEN
 RAISE EXCEPTION 'all published claims require grounds'; END IF;
 IF EXISTS(SELECT 1 FROM insight_claims c JOIN insight_claim_sources cs USING(claim_id)
 JOIN evidence_sources s USING(source_id) JOIN insight_articles ia ON ia.insight_id=c.insight_id AND ia.article_id=s.article_id
 WHERE c.insight_id=NEW.insight_id AND s.source_type='news'
 AND (s.source_ref->>'content_version') IS DISTINCT FROM ia.content_version) THEN
 RAISE EXCEPTION 'primary citation revision differs from insight article revision'; END IF;
 IF EXISTS(SELECT 1 FROM insight_claims c JOIN insight_claim_facts cf USING(claim_id) WHERE c.insight_id=NEW.insight_id
 AND NOT EXISTS(SELECT 1 FROM company_profile_facts pf WHERE pf.profile_id=NEW.profile_id AND pf.fact_id=cf.fact_id)) THEN
 RAISE EXCEPTION 'claim fact is outside pinned company profile'; END IF;
 IF EXISTS(SELECT 1 FROM insight_claims c WHERE c.insight_id=NEW.insight_id AND c.kind IN ('verified_fact','bank_position')
 AND NOT EXISTS(SELECT 1 FROM insight_claim_sources s WHERE s.claim_id=c.claim_id)) THEN
 RAISE EXCEPTION 'fact and bank-position claims require sources'; END IF;
 IF EXISTS(SELECT 1 FROM insight_claims c JOIN insight_claim_sources cs USING(claim_id) JOIN evidence_sources s USING(source_id)
 WHERE c.insight_id=NEW.insight_id AND c.kind='bank_position' AND s.source_type<>'bank_analytics') THEN
 RAISE EXCEPTION 'bank position requires Alfa analytics'; END IF;
 IF EXISTS(SELECT 1 FROM insight_claims WHERE insight_id=NEW.insight_id
 AND kind IN ('ai_inference','risk','opportunity','recommendation') AND certainty='confirmed') THEN
 RAISE EXCEPTION 'AI inferences cannot be confirmed facts'; END IF;
 IF NEW.ranking_run_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM ranking_runs r WHERE r.ranking_run_id=NEW.ranking_run_id
 AND r.status='completed' AND r.profile_id=NEW.profile_id AND r.user_context_id IS NOT DISTINCT FROM NEW.user_context_id) THEN
 RAISE EXCEPTION 'insight ranking context mismatch'; END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER insight_publication_guard BEFORE INSERT OR UPDATE ON insights
 FOR EACH ROW EXECUTE FUNCTION validate_insight_publication();
COMMIT;
