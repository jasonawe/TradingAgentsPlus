-- Roadmap §Harness-redesign — Session 元数据扩展。
-- schema 列的添加在 storage.py 的 _run_python_migration_hook(version == 18)
-- 中以 idempotent 方式执行(检查 PRAGMA table_info 后才 ALTER),
-- 本文件保留为 schema version bump 锚点,SQL 段留空。
SELECT 1;
