import json
from pathlib import Path

import pytest

from web.history import ReportHistory, ReportNotFound
from web.repositories import ReportIndexRepository
from web.storage import SQLiteStore


def write_report(root: Path, relative: str, *, sidecar: dict | None = None, complete: str | None = None, sections: dict[str, str] | None = None) -> Path:
    report_dir = root / relative
    report_dir.mkdir(parents=True)
    (report_dir / "complete_report.md").write_text(
        complete or "# Trading Analysis Report: AAPL\n\nGenerated: 2026-08-26 10:00:00\n\n## V. Portfolio Manager Decision\n\nBUY because momentum is strong.",
        encoding="utf-8",
    )
    for name, value in (sections or {}).items():
        target = report_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value, encoding="utf-8")
    if sidecar is not None:
        metadata = {"status": "completed", **sidecar}
        (report_dir / "run.json").write_text(json.dumps(metadata), encoding="utf-8")
        if metadata["status"] == "completed":
            (report_dir / "COMMITTED").write_text("ok\n", encoding="utf-8")
    return report_dir


def test_indexes_web_and_legacy_roots_newest_first(tmp_path):
    web_root = tmp_path / "results" / "web_reports"
    legacy_root = tmp_path / "results" / "reports"
    cwd_root = tmp_path / "cwd" / "reports"
    write_report(web_root, "AAPL/2026-08-26/run-1", sidecar={
        "run_id": "run-1", "report_id": "run-1", "ticker": "AAPL",
        "generated_at": "2026-08-26T12:00:00+00:00", "signal": "BUY",
    })
    write_report(legacy_root, "MSFT_2026-08-25", complete="# Trading Analysis Report: MSFT\n\nGenerated: 2026-08-25 09:00:00\n\n## V. Portfolio Manager Decision\n\nHOLD")
    write_report(
        cwd_root,
        "TSLA/2026-08-24/run",
        sidecar={"report_id": "ignored"},
        complete="# Trading Analysis Report: TSLA\n\nGenerated: 2026-08-24 08:00:00\n\n## V. Portfolio Manager Decision\n\nSELL",
    )

    records = ReportHistory(results_dir=tmp_path / "results", cwd=tmp_path / "cwd").list_reports()
    assert [record["ticker"] for record in records] == ["AAPL", "MSFT", "TSLA"]
    assert records[0]["report_id"] == "run-1"
    assert records[0]["source"] == "web"
    assert records[0]["decision_preview"].startswith("BUY")
    assert set(records[0]) >= {"report_id", "source", "ticker", "generated_at", "decision_preview"}


def test_detail_has_complete_report_and_all_explicit_sections(tmp_path):
    root = tmp_path / "results" / "web_reports"
    write_report(root, "AAPL/2026-08-26/r1", sidecar={"report_id": "r1", "ticker": "AAPL"}, sections={
        "1_analysts/market.md": "market",
        "2_research/bull.md": "bull",
        "3_trading/trader.md": "trader",
        "5_portfolio/decision.md": "decision",
    })
    detail = ReportHistory(results_dir=tmp_path / "results", cwd=tmp_path).get_report("r1")
    assert detail["complete_report"].startswith("# Trading Analysis Report")
    assert detail["analysts"]["market"] == "market"
    assert detail["research"]["bull"] == "bull"
    assert detail["trading"]["trader"] == "trader"
    assert detail["portfolio"]["decision"] == "decision"
    assert detail["analysts"]["sentiment"] == ""
    assert detail["risk"]["neutral"] == ""


def test_detail_reads_optional_executive_summary(tmp_path):
    root = tmp_path / "results" / "web_reports"
    report_dir = write_report(root, "AAPL/2026-08-26/r2", sidecar={"report_id": "r2", "ticker": "AAPL"})
    (report_dir / "executive_summary.md").write_text("## Executive summary\n\nHold", encoding="utf-8")
    detail = ReportHistory(results_dir=tmp_path / "results", cwd=tmp_path).get_report("r2")
    assert detail["executive_summary"] == "## Executive summary\n\nHold"


def test_legacy_id_is_root_qualified_and_collisions_are_stable(tmp_path, monkeypatch):
    results = tmp_path / "results"
    cwd = tmp_path / "cwd"
    write_report(results / "reports", "AAPL_2026-08-26")
    write_report(cwd / "reports", "AAPL_2026-08-26")
    first = ReportHistory(results_dir=results, cwd=cwd).list_reports()
    second = ReportHistory(results_dir=results, cwd=cwd).list_reports()
    assert first[0]["report_id"] != first[1]["report_id"]
    assert [item["report_id"] for item in first] == [item["report_id"] for item in second]

    monkeypatch.setattr("web.history._legacy_digest", lambda _identity: "0" * 16)
    collided = ReportHistory(results_dir=results, cwd=cwd).list_reports()
    assert {item["report_id"] for item in collided} == {"legacy-" + "0" * 16, "legacy-" + "0" * 16 + "-2"}


def test_rejects_traversal_and_outside_paths(tmp_path):
    history = ReportHistory(results_dir=tmp_path / "results", cwd=tmp_path)
    with pytest.raises(ReportNotFound):
        history.get_report("../../etc/passwd")
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    assert history.resolve_path(outside) is None


def test_missing_generated_metadata_is_null(tmp_path):
    write_report(tmp_path / "results" / "reports", "AAPL_2026-08-26", complete="# Trading Analysis Report: AAPL\n\nNo metadata")
    item = ReportHistory(results_dir=tmp_path / "results", cwd=tmp_path).list_reports()[0]
    assert item["generated_at"] is None


def test_unpublished_web_report_in_tmp_is_not_indexed(tmp_path):
    web_root = tmp_path / "results" / "web_reports"
    write_report(web_root, "AAPL/2026-08-26/.tmp/run-failed", sidecar={
        "run_id": "run-failed", "report_id": "run-failed", "ticker": "AAPL",
        "status": "failed",
    })

    records = ReportHistory(results_dir=tmp_path / "results", cwd=tmp_path).list_reports()

    assert records == []


def test_web_report_in_tmp_is_never_indexed_even_with_publish_markers(tmp_path):
    web_root = tmp_path / "results" / "web_reports"
    report_dir = write_report(web_root, "AAPL/2026-08-26/.tmp/run-marked", sidecar={
        "run_id": "run-marked", "report_id": "run-marked", "ticker": "AAPL",
    })

    (report_dir / "COMMITTED").write_text("ok\n", encoding="utf-8")

    assert ReportHistory(results_dir=tmp_path / "results", cwd=tmp_path).list_reports() == []


def test_completed_legacy_web_report_without_marker_remains_visible(tmp_path):
    web_root = tmp_path / "results" / "web_reports"
    report_dir = write_report(web_root, "AAPL/2026-08-26/run-old", sidecar={
        "run_id": "run-old", "report_id": "run-old", "ticker": "AAPL",
    })
    (report_dir / "COMMITTED").unlink()

    records = ReportHistory(results_dir=tmp_path / "results", cwd=tmp_path).list_reports()

    assert [record["report_id"] for record in records] == ["run-old"]


def test_indexed_history_rebuilds_once_and_marks_deleted_files_missing(tmp_path):
    results = tmp_path / "results"
    report_dir = write_report(
        results / "web_reports",
        "AAPL/2026-08-27/run-indexed",
        sidecar={"report_id": "run-indexed", "ticker": "AAPL"},
    )
    repo = ReportIndexRepository(SQLiteStore(tmp_path / "history.sqlite3"))
    history = ReportHistory(results_dir=results, cwd=tmp_path, repository=repo)

    assert history.rebuild_index() == 1
    assert [item["report_id"] for item in history.list_reports()] == ["run-indexed"]
    (report_dir / "complete_report.md").unlink()
    assert [item["report_id"] for item in history.list_reports()] == ["run-indexed"]
    history.rebuild_index()
    assert history.list_reports() == []


def test_outbox_backed_report_is_immediately_readable(tmp_path, monkeypatch):
    results = tmp_path / "results"
    report_dir = write_report(
        results / "web_reports",
        "AAPL/2026-08-27/run-outbox",
        sidecar={"report_id": "run-outbox", "ticker": "AAPL"},
    )
    repo = ReportIndexRepository(SQLiteStore(tmp_path / "history.sqlite3"))
    history = ReportHistory(results_dir=results, cwd=tmp_path, repository=repo)
    monkeypatch.setattr(repo, "upsert", lambda _metadata: (_ for _ in ()).throw(RuntimeError("busy")))

    history.index_report(report_dir)

    assert history.list_reports()[0]["report_id"] == "run-outbox"
    assert history.get_report("run-outbox")["ticker"] == "AAPL"


def test_new_web_publication_requires_full_gate_before_indexing(tmp_path):
    results = tmp_path / "results"
    report_dir = write_report(
        results / "web_reports",
        "AAPL/2026-08-27/run-incomplete",
        sidecar={"report_id": "run-incomplete", "ticker": "AAPL"},
    )
    (report_dir / "COMMITTED").unlink()
    history = ReportHistory(
        results_dir=results,
        cwd=tmp_path,
        repository=ReportIndexRepository(SQLiteStore(tmp_path / "history.sqlite3")),
    )

    with pytest.raises(ValueError, match="incomplete"):
        history.index_report(report_dir)


def test_exact_detail_falls_back_to_cached_canonical_path_when_index_fails(
    tmp_path, monkeypatch
):
    results = tmp_path / "results"
    report_dir = write_report(
        results / "web_reports",
        "AAPL/2026-08-27/run-fallback",
        sidecar={"report_id": "run-fallback", "ticker": "AAPL"},
    )
    repo = ReportIndexRepository(SQLiteStore(tmp_path / "history.sqlite3"))
    history = ReportHistory(results_dir=results, cwd=tmp_path, repository=repo)
    monkeypatch.setattr(repo, "upsert", lambda _metadata: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(repo, "enqueue", lambda *_args: (_ for _ in ()).throw(RuntimeError("down")))
    history.index_report(report_dir)
    monkeypatch.setattr(repo, "get", lambda _report_id: (_ for _ in ()).throw(RuntimeError("down")))

    assert history.get_report("run-fallback")["ticker"] == "AAPL"
