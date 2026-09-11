"""D2-1~D2-4 后端 endpoints 验证。"""
import os
os.environ["STAGE_C_MOCK_LLM"] = "1"  # 强制 mock

import sys, json
sys.path.insert(0, '/Users/shenkang/workroom/languages/agent/TradingAgents')

from fastapi.testclient import TestClient
from web.app import create_app

print("=" * 60)
print("D2-1~D2-4 endpoints 验证")
print("=" * 60)

app = create_app()
client = TestClient(app)

# D2-1: 创建 session
print("\n[D2-1] POST /api/agent/sessions")
resp = client.post("/api/agent/sessions")
print(f"  status: {resp.status_code}")
data = resp.json()
print(f"  body: {data}")
assert resp.status_code == 201
assert "session_id" in data
assert data["session_id"].startswith("s_")
session_id = data["session_id"]
print(f"  ✓ created session: {session_id}")

# D2-2: 列 sessions
print("\n[D2-2] GET /api/agent/sessions")
resp = client.get("/api/agent/sessions")
print(f"  status: {resp.status_code}")
sessions = resp.json()
print(f"  count: {len(sessions)}")
print(f"  first: {sessions[0] if sessions else '(empty)'}")
assert resp.status_code == 200
assert isinstance(sessions, list)
print(f"  ✓ list returned {len(sessions)} session(s)")

# D2-4: SSE chat stream(主测试)
print("\n[D2-4] POST /api/agent/chat/stream (SSE)")
body = {"session_id": session_id, "user_message": "招商银行 600036 多少钱?"}
with client.stream("POST", "/api/agent/chat/stream", json=body) as resp:
    print(f"  status: {resp.status_code}")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    print(f"  content-type: {resp.headers['content-type']}")
    
    events = []
    for line in resp.iter_lines():
        if not line:
            continue
        events.append(line)
        print(f"  raw: {line[:100]}")
        if line.startswith("event: done"):
            break
    
print(f"\n  total events: {len(events)}")
print(f"  ✓ SSE 流式工作")

# D2-3: 读历史
print("\n[D2-3] GET /api/agent/sessions/{sid}")
resp = client.get(f"/api/agent/sessions/{session_id}")
print(f"  status: {resp.status_code}")
data = resp.json()
print(f"  history length: {len(data['history'])}")
for h in data['history'][:5]:
    role = h['role']
    content = str(h.get('content', h.get('name', '')))[:60]
    print(f"    [{role}] {content}")
assert resp.status_code == 200
assert data['session_id'] == session_id
print(f"  ✓ history retrieved")

# 错误处理测试
print("\n[error handling] 缺参数")
resp = client.post("/api/agent/chat/stream", json={"session_id": session_id})
print(f"  缺 user_message: {resp.status_code} {resp.json().get('detail')}")
assert resp.status_code == 400
print(f"  ✓ 400 on missing user_message")

# 清理
import shutil
from pathlib import Path
sessions_dir = Path.home() / '.tradingagents' / 'agent_general' / 'sessions'
if sessions_dir.exists():
    for f in sessions_dir.glob(f'AGENT_{session_id.upper()}.db'):
        f.unlink()
        print(f"  ✓ cleanup: {f.name}")

print()
print("=" * 60)
print("✅ D2-1~D2-4 全部 4 个 endpoints 验证通过")
print("=" * 60)
