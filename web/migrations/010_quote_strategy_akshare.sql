-- 010_quote_strategy_akshare.sql
-- Switch default quote strategy to default-akshare. The previous default
-- (default-eastmoney) hits eastmoney first which is geo-blocked from this
-- network and burns a 10s timeout per symbol before falling through to
-- yfinance, making the 5s prewarmer interval impractical.
UPDATE settings
   SET value = 'default-akshare',
       updated_at = CURRENT_TIMESTAMP
 WHERE key = 'quote_strategy_id'
   AND value = 'default-eastmoney';

INSERT OR IGNORE INTO settings(key, value, source, updated_at)
VALUES ('quote_strategy_id', 'default-akshare', 'default', CURRENT_TIMESTAMP);
