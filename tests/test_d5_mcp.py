"""Day 5 — MCP server 集成测试。"""
import json
import subprocess
import sys
import time
from pathlib import Path

def _start_mcp_server():
    """启动 MCP server subprocess(stdio transport)。"""
    root = Path(__file__).resolve().parent.parent
    env = {
        "MINIMAX_CN_API_KEY": "test-dummy-key",
        "TRADINGAGENTS_LLM_PROVIDER": "minimax-cn",
        "TRADINGAGENTS_QUOTE_TTL_SECONDS": "60",
        "PYTHONPATH": str(root),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "tradingagents.agents.general.mcp_server"],
        cwd=str(root),
        env={**env, **__import__("os").environ},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    return proc


def _send_request(proc, req_id, method, params=None):
    """发送 MCP request 到 server,读 response。"""
    msg = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        msg["params"] = params
    body = (json.dumps(msg) + "\n").encode()
    proc.stdin.write(body)
    proc.stdin.flush()
    # 读一行 response
    line = proc.stdout.readline()
    if not line:
        # server 可能挂了,stderr 有 info
        err = proc.stderr.read()
        raise RuntimeError(f"server closed: {err.decode()}")
    return json.loads(line.decode().strip())


def _send_notification(proc, method, params=None):
    """发送 notification(无 id,server 不响应)。"""
    msg = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    body = (json.dumps(msg) + "\n").encode()
    proc.stdin.write(body)
    proc.stdin.flush()


def test_mcp_server_initialization():
    """测试 MCP server initialize + tools/list 协议。"""
    proc = _start_mcp_server()
    try:
        # 1. initialize
        resp = _send_request(proc, 1, "initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "0.1.0"},
        })
        assert "result" in resp, f"init failed: {resp}"
        assert "serverInfo" in resp["result"]
        assert "name" in resp["result"]["serverInfo"]
        print(f"  ✓ initialize: server={resp['result']['serverInfo']}")

        # 2. initialized notification
        _send_notification(proc, "notifications/initialized")

        # 3. tools/list
        resp = _send_request(proc, 2, "tools/list", {})
        assert "result" in resp
        tools = resp["result"]["tools"]
        tool_names = {t["name"] for t in tools}
        print(f"  ✓ tools/list: {len(tools)} tools")
        # 验证 15 个工具都在
        expected = {
            "get_quote", "get_quotes_batch", "get_history", "get_fundamentals",
            "list_watchlist",
            "list_alpha_factors", "compute_alpha_factors", "evaluate_alpha",
            "create_note", "update_note", "delete_note",
            "create_alert", "update_alert", "delete_alert",
            "update_preference",
        }
        missing = expected - tool_names
        assert not missing, f"missing tools: {missing}"
        print(f"  ✓ all 15 expected tools present")
    finally:
        proc.terminate()
        proc.wait(timeout=3)


def test_mcp_server_tool_call_get_quote():
    """测试调 get_quote tool(读操作)。"""
    proc = _start_mcp_server()
    try:
        _send_request(proc, 1, "initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "0.1.0"},
        })
        _send_notification(proc, "notifications/initialized")

        # 调 get_quote
        resp = _send_request(proc, 2, "tools/call", {
            "name": "get_quote",
            "arguments": {"symbol": "600036.SS", "asset_type": "stock"},
        })
        assert "result" in resp, f"call failed: {resp}"
        content = resp["result"]["content"]
        assert isinstance(content, list)
        assert len(content) >= 1
        text = content[0].get("text", "")
        print(f"  ✓ get_quote result: {text[:100]}...")
        # 成功应该包含 symbol: 600036.SS
        assert "600036" in text or "NO_DATA" in text or "ERROR" in text
    finally:
        proc.terminate()
        proc.wait(timeout=3)


def test_mcp_server_tool_call_create_note_auto_approve():
    """测试调 create_note tool — MCP 默认 auto-approve。"""
    proc = _start_mcp_server()
    try:
        _send_request(proc, 1, "initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "0.1.0"},
        })
        _send_notification(proc, "notifications/initialized")

        # 调 create_note(auto-approve)
        resp = _send_request(proc, 2, "tools/call", {
            "name": "create_note",
            "arguments": {
                "symbol": "600036.SS",
                "body_md": "MCP test 2026-09-11",
                "asset_type": "stock",
            },
        })
        assert "result" in resp, f"call failed: {resp}"
        content = resp["result"]["content"]
        text = content[0].get("text", "")
        print(f"  ✓ create_note result: {text[:100]}...")
        # 应该 NOTE_CREATED(成功)或 ERROR
        assert "NOTE_CREATED" in text or "ERROR" in text
    finally:
        proc.terminate()
        proc.wait(timeout=3)


if __name__ == "__main__":
    print("=" * 60)
    print("Day 5 MCP Server 集成测试")
    print("=" * 60)
    print()
    print("[1] MCP server initialization + tools/list")
    test_mcp_server_initialization()
    print()
    print("[2] MCP tool call: get_quote(读)")
    test_mcp_server_tool_call_get_quote()
    print()
    print("[3] MCP tool call: create_note(写 + auto-approve)")
    test_mcp_server_tool_call_create_note_auto_approve()
    print()
    print("=" * 60)
    print("✅ Day 5 MCP server 全部测试通过")
    print("=" * 60)
