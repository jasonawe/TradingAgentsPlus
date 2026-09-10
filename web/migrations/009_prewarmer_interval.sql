-- 009_prewarmer_interval.sql
-- Bump prewarmer interval default 30s → 5s for snappier cache freshness.
-- Only update rows still at the old default; users who explicitly chose another
-- value are not overridden.
UPDATE settings
   SET value = '5',
       updated_at = CURRENT_TIMESTAMP
 WHERE key = 'prewarmer.interval_seconds'
   AND value = '30';

-- Backfill an explicit quote_ttl_seconds default if the row is missing entirely
-- (existing rows are read with fallback, so this is purely for visibility in the
-- settings UI). INSERT OR IGNORE is safe because the runtime code path already
-- falls back to 60s when the key is absent.
INSERT OR IGNORE INTO settings(key, value, source, updated_at)
VALUES ('quote_ttl_seconds', '60', 'default', CURRENT_TIMESTAMP);
