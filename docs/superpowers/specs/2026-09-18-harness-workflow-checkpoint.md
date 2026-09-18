# Workflow ↔ Checkpoint Mapping — 2026-09-18 (Step 42)

## Background

v3 review backlog item E. `HarnessCheckpointStore` 记录 turn milestones；
`Workflow.run()` 记录节点执行顺序。这两套记录目前不互通：

- checkpoint resume 时不知道上一个 workflow 走到了哪个 node
- workflow introspection 不知道 turn 跨重启后恢复时从哪继续

## 设计

新增 `WorkflowCheckpointAdapter`：

- 每个 Node 注册时可指定一个 `milestone_id`（默认 "checkpoint"）
- Workflow.run() 调 `_maybe_checkpoint(node.id, milestone_id)` after each node
- Resume 时根据 last milestone 找对应 entry node，跳过已完成节点

`HarnessCheckpointStore` 新增 API：

- `checkpoint_for_node(session_id, workflow_name, node_id)` — 在指定 node 写入
  composite (session_id, workflow_name, node_id) checkpoint
- `last_node_completed(session_id, workflow_name)` — 读最后完成的 node

## 文件

1. `tradingagents/agent_harness/core/workflow_checkpoint.py` (新) — adapter
2. `tradingagents/agent_harness/core/harness_checkpoint.py` — 新增
   composite (session_id, workflow_name, node_id) PK 支持
3. `web/migrations/017_workflow_checkpoints.sql` (新) — 新表
4. `tradingagents/agent_harness/core/workflow.py` — 集成 adapter hook
5. `tests/test_step42_workflow_checkpoint.py` — 7 测试
