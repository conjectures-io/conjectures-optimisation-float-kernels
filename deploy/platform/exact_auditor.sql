-- Create this login role and set its password separately before applying.
-- Audit findings cannot modify submissions, promotions, eligibility or rewards.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM exact_auditor;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM exact_auditor;
GRANT USAGE ON SCHEMA public TO exact_auditor;
DO $$ BEGIN
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO exact_auditor', current_database());
END $$;
GRANT SELECT ON promotions, evaluations, submissions, sources TO exact_auditor;
GRANT SELECT, INSERT, UPDATE ON audits, audit_cursors TO exact_auditor;
GRANT SELECT, INSERT, UPDATE ON audit_attempts TO exact_auditor;
GRANT USAGE, SELECT ON SEQUENCE audits_id_seq TO exact_auditor;
