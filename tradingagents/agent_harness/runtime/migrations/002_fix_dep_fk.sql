-- Task 11 — fix broken FK on agent_task_dependencies.
-- Original 001_initial.sql declared a composite FK to agent_tasks(run_id,
-- task_id), but agent_tasks' primary key is just (task_id) — the FK never
-- matched and inserts into agent_task_dependencies failed.
-- Recreate the table without the composite FK; FK to agent_tasks(task_id)
-- alone preserves referential integrity.

CREATE TABLE agent_task_dependencies_new (
  run_id              TEXT NOT NULL,
  task_id             TEXT NOT NULL,
  depends_on_task_id  TEXT NOT NULL,
  condition           TEXT NOT NULL,
  PRIMARY KEY (run_id, task_id, depends_on_task_id),
  FOREIGN KEY (task_id) REFERENCES agent_tasks(task_id),
  FOREIGN KEY (depends_on_task_id) REFERENCES agent_tasks(task_id)
);

INSERT INTO agent_task_dependencies_new
  SELECT run_id, task_id, depends_on_task_id, condition
  FROM agent_task_dependencies;

DROP TABLE agent_task_dependencies;
ALTER TABLE agent_task_dependencies_new RENAME TO agent_task_dependencies;

CREATE INDEX idx_task_deps_depends ON agent_task_dependencies(depends_on_task_id);
