"""Safe discovery and reading of persisted TradingAgents reports."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .markdown import render_markdown


class ReportNotFound(LookupError):
    """Raised when an opaque report ID is unknown or no longer readable."""


_GENERATED_RE = re.compile(r"^Generated:\s*(.+?)\s*$", re.MULTILINE)
_TITLE_RE = re.compile(r"^#\s+Trading Analysis Report:\s*(.+?)\s*$", re.MULTILINE)
_DATE_RE = re.compile(r"(?:^|[_/.-])(\d{4}-\d{2}-\d{2})(?:$|[_/.-])")
_SECTION_PATHS = {
    "analysts": {
        "market": "1_analysts/market.md",
        "sentiment": "1_analysts/sentiment.md",
        "news": "1_analysts/news.md",
        "fundamentals": "1_analysts/fundamentals.md",
    },
    "research": {
        "bull": "2_research/bull.md",
        "bear": "2_research/bear.md",
        "manager": "2_research/manager.md",
    },
    "trading": {"trader": "3_trading/trader.md"},
    "risk": {
        "aggressive": "4_risk/aggressive.md",
        "conservative": "4_risk/conservative.md",
        "neutral": "4_risk/neutral.md",
    },
    "portfolio": {"decision": "5_portfolio/decision.md"},
}


def _legacy_digest(identity: str) -> str:
    return hashlib.sha256(identity.encode("utf-8", "surrogatepass")).hexdigest()[:16]


@dataclass(frozen=True)
class _ReportEntry:
    report_id: str
    path: Path
    root: Path
    source: str
    relative: str
    sidecar: dict[str, Any]
    ticker: str | None
    analysis_date: str | None
    generated_at: str | None
    signal: str | None
    status: str | None
    asset_type: str | None
    analysts: list[str]
    research_depth: int | None
    provider: str | None
    quick_model: str | None
    deep_model: str | None
    output_language: str | None
    summary_status: str | None
    data_snapshot_id: str | None
    data_status: str | None
    reproducibility: str | None
    quote_strategy_id: str | None
    effective_quote_provider_chain: list[str]


class ReportHistory:
    """Index reports from the three report roots permitted by the console."""

    def __init__(self, results_dir: str | Path | None = None, cwd: str | Path | None = None) -> None:
        self.results_dir = Path(results_dir) if results_dir is not None else Path.cwd()
        self.cwd = Path(cwd) if cwd is not None else Path.cwd()
        self._index: dict[str, _ReportEntry] = {}

    @property
    def roots(self) -> tuple[tuple[str, Path, str], ...]:
        return (
            ("web", self.results_dir / "web_reports", "web_reports"),
            ("legacy", self.results_dir / "reports", "results_reports"),
            ("legacy", self.cwd / "reports", "cwd_reports"),
        )

    def refresh(self) -> list[dict[str, Any]]:
        candidates: list[tuple[str, Path, Path, str, dict[str, Any]]] = []
        seen: set[Path] = set()
        for source, root, root_name in self.roots:
            root_resolved = self._resolved_root(root)
            if root_resolved is None or not root_resolved.is_dir():
                continue
            for complete in sorted(root_resolved.rglob("complete_report.md")):
                report_dir = complete.parent
                if report_dir in seen or self._safe_descendant(complete, root_resolved) is False:
                    continue
                seen.add(report_dir)
                sidecar = self._read_sidecar(report_dir, root_resolved)
                if source == "web" and not self._is_publishable_web_report(report_dir, sidecar):
                    continue
                candidates.append((source, root_resolved, report_dir, root_name, sidecar))

        candidates.sort(key=lambda item: (item[0], item[1].as_posix(), item[2].relative_to(item[1]).as_posix()))
        entries: list[_ReportEntry] = []
        used: dict[str, int] = {}
        for source, root, report_dir, root_name, sidecar in candidates:
            complete = report_dir / "complete_report.md"
            metadata = self._metadata(complete, report_dir, root, sidecar)
            base_id = str(sidecar.get("report_id") or sidecar.get("run_id") or "") if source == "web" else ""
            if not base_id:
                identity = f"{root_name}/{report_dir.relative_to(root).as_posix()}"
                base_id = f"legacy-{_legacy_digest(identity)}"
            count = used.get(base_id, 0) + 1
            used[base_id] = count
            report_id = base_id if count == 1 else f"{base_id}-{count}"
            entries.append(_ReportEntry(
                report_id=report_id,
                path=report_dir,
                root=root,
                source=source,
                relative=report_dir.relative_to(root).as_posix(),
                sidecar=sidecar,
                ticker=metadata["ticker"],
                analysis_date=metadata["analysis_date"],
                generated_at=metadata["generated_at"],
                signal=metadata["signal"],
                status=metadata["status"],
                asset_type=metadata["asset_type"],
                analysts=metadata["analysts"],
                research_depth=metadata["research_depth"],
                provider=metadata["provider"],
                quick_model=metadata["quick_model"],
                deep_model=metadata["deep_model"],
                output_language=metadata["output_language"],
                summary_status=metadata["summary_status"],
                data_snapshot_id=metadata["data_snapshot_id"],
                data_status=metadata["data_status"],
                reproducibility=metadata["reproducibility"],
                quote_strategy_id=metadata["quote_strategy_id"],
                effective_quote_provider_chain=metadata["effective_quote_provider_chain"],
            ))
        self._index = {entry.report_id: entry for entry in entries}
        return self._list_records(entries)

    def list_reports(self) -> list[dict[str, Any]]:
        records = self.refresh()
        return records

    def get_entry(self, report_id: str) -> _ReportEntry:
        self.refresh()
        entry = self._index.get(report_id)
        if entry is None or not self._safe_descendant(entry.path, entry.root):
            raise ReportNotFound(report_id)
        return entry

    def get_report(self, report_id: str) -> dict[str, Any]:
        entry = self.get_entry(report_id)
        complete_path = entry.path / "complete_report.md"
        complete = self._read_text(complete_path, entry.root)
        if complete is None:
            raise ReportNotFound(report_id)
        detail: dict[str, Any] = {
            "report_id": entry.report_id,
            "source": entry.source,
            "ticker": entry.ticker,
            "analysis_date": entry.analysis_date,
            "generated_at": entry.generated_at,
            "signal": entry.signal,
            "rating": entry.signal,
            "status": entry.status,
            "asset_type": entry.asset_type,
            "analysts": entry.analysts,
            "research_depth": entry.research_depth,
            "provider": entry.provider,
            "quick_model": entry.quick_model,
            "deep_model": entry.deep_model,
            "output_language": entry.output_language,
            "summary_status": entry.summary_status,
            "data_snapshot_id": entry.data_snapshot_id,
            "data_status": entry.data_status or "unknown",
            "reproducibility": entry.reproducibility,
            "quote_strategy_id": entry.quote_strategy_id,
            "effective_quote_provider_chain": entry.effective_quote_provider_chain,
            "complete_report": complete,
            "complete_report_html": render_markdown(complete),
        }
        for group, fields in _SECTION_PATHS.items():
            section = {
                key: self._read_text(entry.path / relative, entry.root) or ""
                for key, relative in fields.items()
            }
            detail[group] = section
            detail[f"{group}_html"] = {
                key: render_markdown(value) for key, value in section.items()
            }
        detail["executive_summary"] = self._read_text(entry.path / "executive_summary.md", entry.root) or ""
        detail["executive_summary_html"] = render_markdown(detail["executive_summary"])
        return detail

    def resolve_path(self, path: str | Path, *, root: str | Path | None = None) -> Path | None:
        """Resolve a path only when it remains below an allowlisted root."""
        candidate = Path(path)
        roots = [Path(root)] if root is not None else [item[1] for item in self.roots]
        for allowed in roots:
            resolved_root = self._resolved_root(allowed)
            if resolved_root is not None and self._safe_descendant(candidate, resolved_root):
                return candidate.resolve()
        return None

    def _list_records(self, entries: list[_ReportEntry]) -> list[dict[str, Any]]:
        ordered = sorted(entries, key=lambda entry: self._sort_datetime(entry.generated_at), reverse=True)
        return [
            {
                "report_id": entry.report_id,
                "source": entry.source,
                "ticker": entry.ticker,
                "analysis_date": entry.analysis_date,
                "generated_at": entry.generated_at,
                "signal": entry.signal,
                "rating": entry.signal,
                "status": entry.status,
                "asset_type": entry.asset_type,
                "analysts": entry.analysts,
                "research_depth": entry.research_depth,
                "provider": entry.provider,
                "quick_model": entry.quick_model,
                "deep_model": entry.deep_model,
                "output_language": entry.output_language,
                "summary_status": entry.summary_status,
                "data_snapshot_id": entry.data_snapshot_id,
                "data_status": entry.data_status or "unknown",
                "reproducibility": entry.reproducibility,
                "quote_strategy_id": entry.quote_strategy_id,
                "effective_quote_provider_chain": entry.effective_quote_provider_chain,
                "decision_preview": self._decision_preview(entry.path, entry.root),
            }
            for entry in ordered
        ]

    @staticmethod
    def _sort_datetime(value: str | None) -> datetime:
        if not value:
            return datetime.min
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return datetime.min

    @staticmethod
    def _resolved_root(root: Path) -> Path | None:
        try:
            return root.expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            return None

    @staticmethod
    def _safe_descendant(path: Path, root: Path) -> bool:
        try:
            path.expanduser().resolve(strict=False).relative_to(root.resolve(strict=False))
            return True
        except (ValueError, OSError, RuntimeError):
            return False

    @classmethod
    def _read_sidecar(cls, report_dir: Path, root: Path) -> dict[str, Any]:
        path = report_dir / "run.json"
        if not cls._safe_descendant(path, root):
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}

    @classmethod
    def _is_publishable_web_report(cls, report_dir: Path, sidecar: dict[str, Any]) -> bool:
        """Keep failed and temporary reports out while reading older completed reports."""
        if ".tmp" in report_dir.parts:
            return False
        # New runs always have COMMITTED after the temp directory is renamed.
        # Older runs predate that marker, but their completed run.json remains a
        # valid publication record and must stay visible to users.
        return sidecar.get("status") == "completed"

    @classmethod
    def _read_text(cls, path: Path, root: Path) -> str | None:
        if not cls._safe_descendant(path, root):
            return None
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None

    @classmethod
    def _metadata(cls, complete: Path, report_dir: Path, root: Path, sidecar: dict[str, Any]) -> dict[str, Any]:
        text = cls._read_text(complete, root) or ""
        title = _TITLE_RE.search(text)
        generated = sidecar.get("generated_at")
        if generated is None:
            generated_match = _GENERATED_RE.search(text)
            generated = generated_match.group(1).strip() if generated_match else None
            if generated:
                try:
                    generated = datetime.fromisoformat(generated).isoformat()
                except ValueError:
                    generated = str(generated)
        analysis_date = sidecar.get("analysis_date")
        if analysis_date is None:
            match = _DATE_RE.search(report_dir.name) or _DATE_RE.search(report_dir.as_posix())
            analysis_date = match.group(1) if match else None
        return {
            "ticker": str(sidecar.get("ticker") or (title.group(1).strip() if title else "")) or None,
            "analysis_date": str(analysis_date) if analysis_date else None,
            "generated_at": str(generated) if generated else None,
            "signal": str(sidecar.get("signal") or sidecar.get("rating")) if (sidecar.get("signal") is not None or sidecar.get("rating") is not None) else None,
            "status": str(sidecar.get("status")) if sidecar.get("status") is not None else None,
            "asset_type": str(sidecar.get("asset_type")) if sidecar.get("asset_type") is not None else None,
            "analysts": [str(value) for value in sidecar.get("analysts", [])] if isinstance(sidecar.get("analysts", []), list) else [],
            "research_depth": sidecar.get("research_depth") if isinstance(sidecar.get("research_depth"), int) else None,
            "provider": str(sidecar.get("provider")) if sidecar.get("provider") is not None else None,
            "quick_model": str(sidecar.get("quick_model")) if sidecar.get("quick_model") is not None else None,
            "deep_model": str(sidecar.get("deep_model")) if sidecar.get("deep_model") is not None else None,
            "output_language": str(sidecar.get("output_language")) if sidecar.get("output_language") is not None else None,
            "summary_status": str(sidecar.get("summary_status")) if sidecar.get("summary_status") is not None else None,
            "data_snapshot_id": str(sidecar.get("data_snapshot_id")) if sidecar.get("data_snapshot_id") is not None else None,
            "data_status": str(sidecar.get("data_status")) if sidecar.get("data_status") is not None else None,
            "reproducibility": str(sidecar.get("reproducibility")) if sidecar.get("reproducibility") is not None else None,
            "quote_strategy_id": str(sidecar.get("quote_strategy_id") or sidecar.get("effective_quote_strategy_id")) if (sidecar.get("quote_strategy_id") or sidecar.get("effective_quote_strategy_id")) is not None else None,
            "effective_quote_provider_chain": [str(value) for value in sidecar.get("effective_quote_provider_chain", [])] if isinstance(sidecar.get("effective_quote_provider_chain", []), list) else [],
        }

    @classmethod
    def _decision_preview(cls, report_dir: Path, root: Path, limit: int = 240) -> str:
        text = cls._read_text(report_dir / "5_portfolio" / "decision.md", root)
        if not text:
            complete = cls._read_text(report_dir / "complete_report.md", root) or ""
            marker = re.search(r"##\s+V\.\s+Portfolio Manager Decision\s*\n+(.*?)(?=\n##\s|\Z)", complete, re.S | re.I)
            text = marker.group(1) if marker else ""
        return " ".join(text.split())[:limit]
