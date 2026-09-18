# D4 Memory Tier Merging — Step 45

## Background

当前 L1 session cache / L2 short-term memory / L3 references 三层
独立（`memory/l1_session.py`, `memory/l2_short_term.py`,
`memory/l3_references.py`）。问题：

- 三层各自维护 TTL / 序列化格式 / 接口
- 查询走 3 次 lookup，hit rate 低
- 数据同步：user 在 L2 改了 note，L3 reference 不会更新

## 设计

统一 `MemoryStore` protocol：

```python
class MemoryStore(Protocol):
    def get(self, key: str, tier: str) -> dict | None: ...
    def put(self, key: str, value: dict, tier: str, ttl: int) -> None: ...
    def delete(self, key: str, tier: str) -> None: ...
    def query(self, prefix: str, tier: str) -> list[dict]: ...
```

L1/L2/L3 变成 config-controlled tiers 而不是 hardcoded classes：

- `L1Tier(ttl=60s)` — session cache (volatile)
- `L2Tier(ttl=86400s, persistent=True)` — short-term (SQLite)
- `L3Tier(persistent=True, cross_session=True)` — references

每个 tier 可以 pluggable backend (in-memory / SQLite / Redis) —
抽象后只需配 backend 不改业务代码。

## 文件

1. `tradingagents/agent_harness/memory/store.py` (新) — MemoryStore protocol
2. `tradingagents/agent_harness/memory/tier.py` (新) — Tier config
3. `tradingagents/agent_harness/memory/sqlite_backend.py` — extracted
4. `tradingagents/agent_harness/memory/memory_facade.py` — unified facade
5. `tests/test_step45_memory_merging.py` — 7 测试
