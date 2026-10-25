BEGIN;
-- Read-model/UI state stays separate from immutable analytical content.
ALTER TABLE api_tokens ADD COLUMN expires_at timestamptz;
ALTER TABLE discussions ADD COLUMN insight_id uuid REFERENCES insights(insight_id);
ALTER TABLE discussion_messages ADD COLUMN answer_status text
 CHECK(answer_status IS NULL OR answer_status IN ('answered','insufficient_evidence'));
CREATE TABLE insight_feedback (
 user_id uuid NOT NULL, company_id text NOT NULL, insight_id uuid NOT NULL REFERENCES insights(insight_id),
 watch_status text NOT NULL DEFAULT 'none' CHECK(watch_status IN ('none','watching','checked')),
 helpful boolean, not_relevant boolean NOT NULL DEFAULT false, reason text NOT NULL DEFAULT '',
 updated_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(user_id,insight_id),
 FOREIGN KEY(user_id,company_id) REFERENCES company_memberships(user_id,company_id)
);
CREATE TABLE insight_feedback_events (
 user_id uuid NOT NULL REFERENCES app_users(user_id), request_id uuid NOT NULL,
 insight_id uuid NOT NULL REFERENCES insights(insight_id), payload jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(user_id,request_id)
);
CREATE TRIGGER feedback_events_immutable BEFORE UPDATE OR DELETE ON insight_feedback_events
 FOR EACH ROW EXECUTE FUNCTION prevent_row_mutation();
CREATE INDEX discussion_target ON discussions(report_version_id,user_id,insight_id);
CREATE INDEX messages_thread_order ON discussion_messages(discussion_id,created_at,message_id);
CREATE INDEX feedback_watching ON insight_feedback(user_id,company_id) WHERE watch_status='watching';
COMMIT;
