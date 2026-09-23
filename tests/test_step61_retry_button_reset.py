"""§0.4.24.1 — retry button is reset in sendMessage's ``finally`` block.

When a tool result emits an error card with a "🔄 重试" button, that
button is disabled while the SSE request is in flight, then re-enabled
when the response completes (success OR error). Without the ``finally``
reset, a failed request left the button stuck disabled and the user
couldn't retry.

This pins the source contract: the reset code path lives in
``finally`` (not just ``catch``), uses ``state.messagesEl``, and
disables ``disabled=false`` + restores text "🔄 重试" on every
in-flight retry button.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


HARNESS_JS = Path("web/static/harness.js").read_text()


# ─────────────────────────────────────────────────────────────────
# Pin the source structure
# ─────────────────────────────────────────────────────────────────


def test_sendMessage_finally_block_resets_retry_buttons():
    """The reset code must live inside ``finally { ... }`` AFTER setBusy(false).

    Pins §0.4.24.1 contract:
        finally {
            setBusy(false);
            // §0.4.24.1 — reset every in-flight retry button ...
            if (state.messagesEl) {
                state.messagesEl.querySelectorAll('[data-action="retry-tool"]') ...
            }
        }
    """
    # 1. Find the sendMessage finally block.
    m = re.search(
        r"async\s+function\s+sendMessage[^{]*\{[\s\S]*?finally\s*\{([\s\S]*?)\}\s*\n\s*\}",
        HARNESS_JS,
    )
    assert m, "sendMessage(...) does not have a finally block — regex did not match"

    finally_body = m.group(1)

    # 2. setBusy(false) must be called first.
    assert "setBusy(false)" in finally_body, (
        "finally block should call setBusy(false) first so the UI unblocks before retry reset"
    )

    # 3. The reset path must use state.messagesEl.
    assert "state.messagesEl" in finally_body, (
        "finally block must guard against missing messagesEl via `if (state.messagesEl)`"
    )

    # 4. The selector must target the retry button.
    assert '[data-action="retry-tool"]' in finally_body, (
        "finally block must select every [data-action=\"retry-tool\"] button"
    )

    # 5. Both ``btn.disabled = false`` AND ``btn.textContent = "🔄 重试"``
    # are required (otherwise click target reverts but visual label sticks).
    assert re.search(r"btn\.disabled\s*=\s*false", finally_body), (
        "finally block must set btn.disabled = false"
    )
    assert "🔄 重试" in finally_body, (
        "finally block must restore textContent to '🔄 重试'"
    )


def test_retry_button_listener_clicks_call_resend():
    """When the user clicks '🔄 重试', it must trigger another resend
    (not just visually reset). Pin the listener wiring.

    Real wiring pattern (verified from harness.js):
        state.messagesEl.addEventListener("click", (ev) => {
            const btn = ev.target.closest("[data-action=\"retry-tool\"]");
            ...
            retryLastTool();
        });
    """
    # The retry handler attaches to state.messagesEl (not document).
    m = re.search(
        r"state\.messagesEl\.addEventListener\(['\"]click['\"]\s*,\s*\(ev\)\s*=>\s*\{[^}]*data-action=['\"]retry-tool['\"][\s\S]{0,500}",
        HARNESS_JS,
    )
    assert m, "no retry-tool click handler on state.messagesEl found"

    listener_block = m.group(0)

    # The listener must call retryLastTool() or sendMessage() to
    # actually re-trigger the failed tool call (not just visual reset).
    assert "retryLastTool()" in listener_block or "sendMessage()" in listener_block, (
        "retry click handler doesn\'t actually re-trigger the failed tool"
    )

    # Also confirm ``btn.disabled = true`` to mark in-flight state.
    assert "btn.disabled = true" in listener_block, (
        "retry click handler should disable the button while retry is in flight"
    )

    # And confirm a "in-flight" text indicator.
    assert "重试中" in listener_block, (
        "retry click handler should swap label to '重试中...' while in flight"
    )


def test_retry_button_disabled_while_in_flight():
    """While the request is in flight (just before fetch), the retry
    buttons must be disabled. This is what makes the §0.4.24.1
    ``finally`` reset actually necessary.

    Verify the click handler does ``btn.disabled = true`` and shows
    some "in flight" label before invoking ``retryLastTool`` (or
    ``sendMessage``).
    """
    # Find the retry-tool click handler.
    m = re.search(
        r"data-action=['\"]retry-tool['\"][\s\S]{0,400}?}",
        HARNESS_JS,
    )
    # If the close brace doesn't fit in 400 chars, widen.
    if not m:
        m = re.search(
            r"data-action=['\"]retry-tool['\"][\s\S]{0,800}",
            HARNESS_JS,
        )
    assert m, "retry-tool selector not found"
    handler = m.group(0)
    assert re.search(r"btn\.disabled\s*=\s*true", handler), (
        "retry click handler doesn't disable the button while in flight"
    )
    # Must also show some "in flight" indicator (next-state label).
    assert "重试中" in handler or "…" in handler, (
        "retry click handler doesn't change the label while in flight"
    )


# ─────────────────────────────────────────────────────────────────
# JS syntax sanity — caught early so we don't ship broken JS
# ─────────────────────────────────────────────────────────────────


def test_harness_js_no_syntax_errors_node_node():
    """Run ``node -c`` on the JS file. If this fails the harness.js
    on disk doesn't even parse, so the retry reset code couldn't
    possibly run.
    """
    import subprocess
    proc = subprocess.run(
        ["node", "-c", "web/static/harness.js"],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert proc.returncode == 0, (
        f"web/static/harness.js failed node -c:\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
