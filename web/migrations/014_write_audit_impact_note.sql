-- 014_write_audit_impact_note.sql
-- Adds impact_note column to write_audit_log so the friendly Chinese line
-- shown to the user at approval time is persisted alongside the audit row.
-- Lets the audit viewer / debugging tools show "why this row was approved"
-- without re-running describe_impact (which depends on i18n strings).

ALTER TABLE write_audit_log ADD COLUMN impact_note TEXT;

CREATE INDEX IF NOT EXISTS idx_write_audit_impact_note
    ON write_audit_log(impact_note) WHERE impact_note IS NOT NULL;
