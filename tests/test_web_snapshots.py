import json

import pytest
from langchain_core.messages import HumanMessage

from web.snapshots import (
    DataSnapshotRecorder,
    SnapshotAwareDataProvider,
    SnapshotCorruptError,
    SnapshotStore,
    canonical_bytes,
)


def test_canonical_bytes_are_stable_for_json_csv_and_text():
    assert canonical_bytes({"b": 2, "a": [1, None]}) == b'{"a":[1,null],"b":2}'
    assert canonical_bytes("a,b\n2,x\n1,y\n", kind="csv") == b'[{"a":"2","b":"x"},{"a":"1","b":"y"}]'
    assert canonical_bytes("one\r\ntwo\rthree", kind="text") == b"one\ntwo\nthree"


def test_canonical_bytes_normalizes_langchain_messages_in_final_state():
    payload = {"messages": [HumanMessage(content="600999.SS")], "signal": "HOLD"}

    encoded = canonical_bytes(payload)

    assert b"600999.SS" in encoded
    assert json.loads(encoded)["messages"][0]["data"]["content"] == "600999.SS"


def test_recorder_writes_manifest_and_verifies_payload(tmp_path):
    store = SnapshotStore(tmp_path)
    recorder = DataSnapshotRecorder(store, "run-1", provider_chain=["yfinance"])
    entry = recorder.record(
        "ohlcv",
        {"close": [2, 1]},
        provider="yfinance",
        symbol="AAPL",
        request_fingerprint="fp-1",
    )
    manifest = recorder.finalize()

    assert manifest["run_id"] == "run-1"
    assert manifest["datasets"][0]["sha256"] == entry["sha256"]
    assert store.read_dataset("run-1", entry) == {"close": [2, 1]}


def test_store_rejects_traversal_symlink_and_corrupt_hash(tmp_path):
    store = SnapshotStore(tmp_path)
    recorder = DataSnapshotRecorder(store, "run-1")
    entry = recorder.record("news", {"items": []}, request_fingerprint="fp")
    recorder.finalize()

    with pytest.raises(SnapshotCorruptError):
        store.resolve_payload("../outside.json")
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    link = store.run_root("run-1") / "datasets" / "link.json"
    link.symlink_to(outside)
    with pytest.raises(SnapshotCorruptError):
        store.resolve_payload(str(link.relative_to(store.root)))

    payload = store.resolve_payload(entry["payload_ref"])
    payload.write_text('{"changed":true}', encoding="utf-8")
    with pytest.raises(SnapshotCorruptError):
        store.read_dataset("run-1", entry)


def test_snapshot_provider_reads_before_upstream_and_records_miss(tmp_path):
    store = SnapshotStore(tmp_path)
    recorder = DataSnapshotRecorder(store, "run-1")
    calls = []

    def upstream():
        calls.append("network")
        return {"value": 7}

    provider = SnapshotAwareDataProvider(store, upstream, recorder, "run-1")
    assert provider.get_dataset("fundamentals", "fp", provider="yfinance") == {"value": 7}
    assert provider.get_dataset("fundamentals", "fp", provider="yfinance") == {"value": 7}
    assert calls == ["network"]


def test_manifest_json_is_immutable_after_finalize(tmp_path):
    store = SnapshotStore(tmp_path)
    recorder = DataSnapshotRecorder(store, "run-1")
    recorder.record("macro", "ok", request_fingerprint="fp")
    manifest = recorder.finalize()
    path = store.manifest_path("run-1")
    before = path.read_bytes()
    assert json.loads(before)["manifest_hash"] == manifest["manifest_hash"]
    with pytest.raises(RuntimeError):
        recorder.record("news", "late", request_fingerprint="late")
    assert path.read_bytes() == before
