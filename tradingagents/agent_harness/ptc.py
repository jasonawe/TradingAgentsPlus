"""PTC (Program That Calls) executor skeleton (P0-2).

dsh's killer feature — instead of LLM returning a JSON list of tool_calls,
LLM returns a *program* (JSON DSL here) that the harness executes with
free concurrency. Solves the L3 multi-tool concurrent call failure
("unknown tool ''" when LLM wants quote+fundamentals+news in parallel).

JSON DSL format (intentionally simple — we don't ship a Python sandbox):

    {
      "mode": "ptc",
      "groups": [
        {
          "id": "g1",
          "calls": [
            {"name": "get_quote",        "args": {"symbol": "600036.SS"}},
            {"name": "get_fundamentals", "args": {"symbol": "600036.SS"}},
            {"name": "get_news",         "args": {"symbol": "600036.SS"}}
          ]
        },
        {
          "id": "g2",
          "calls": [{"name": "list_alpha_factors", "args": {"symbol": "600036.SS"}}],
          "depends_on": ["g1"]
        }
      ]
    }

Execution model:
1. Parse → PTCProgram (Pydantic)
2. Topological sort groups by `depends_on`
4. For each wave (independent groups), run with asyncio.gather
5. Each call goes through the existing tool.invoke(args, context) pipeline
6. Collect results per group, surface to caller as a flat list

This is the *skeleton* — full integration with Orchestrator (replacing
or augmenting plan-then-execute) comes in a follow-up commit.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from pydantic import BaseModel, Field, field_validator

from .tools.base import BaseTool

LOGGER = logging.getLogger(__name__)


class PTCCall(BaseModel):
    """One tool call inside a PTC group."""

    name: str = Field(..., description="Registered tool name")
    args: dict[str, Any] = Field(default_factory=dict)


class PTCGroup(BaseModel):
    """One concurrency group — calls inside run with asyncio.gather."""

    id: str = Field(..., description="Stable group id for depends_on resolution")
    calls: list[PTCCall] = Field(default_factory=list)
    depends_on: list[str] = Field(
        default_factory=list,
        description="Group ids that must complete before this group runs",
    )


class PTCProgram(BaseModel):
    """Top-level PTC program from LLM."""

    mode: str = Field("ptc", description="Sentinel; must be 'ptc'")
    groups: list[PTCGroup] = Field(default_factory=list)

    @field_validator("mode")
    @classmethod
    def _mode_must_be_ptc(cls, v: str) -> str:
        if v != "ptc":
            raise ValueError(f"mode must be 'ptc', got {v!r}")
        return v

    def is_empty(self) -> bool:
        return not self.groups or all(not g.calls for g in self.groups)


def parse_program(obj: dict[str, Any]) -> PTCProgram:
    """Parse a dict into a PTCProgram. Raises pydantic.ValidationError on bad input.

    Convenience for the orchestrator: ``parse_program(llm_response)``.
    """
    return PTCProgram.model_validate(obj)


class PTCExecutor:
    """Execute a PTCProgram against a tool registry with asyncio.gather.

    Independent groups within the same wave run concurrently. Each call
    funnels through ``tool.invoke(args, context)`` exactly like the
    existing orchestrator path — no special-casing of pipeline stages.
    """

    def __init__(self, tool_registry, pipeline=None) -> None:
        self.registry = tool_registry
        # Optional ToolPipeline — when set, every call goes through 5-stage
        # pipeline (pre/guard/exec/post/result) for HITL / metrics / etc.
        # When None, calls go directly to tool.invoke (legacy behaviour).
        self.pipeline = pipeline

    async def execute(self, program: PTCProgram, context: Any) -> list[dict[str, Any]]:
        """Execute the program; return a flat list of per-call results.

        Each result dict has shape::

            {"group_id": str, "name": str, "ok": bool,
             "result": Any | None, "error": str | None}

        """
        if program.is_empty():
            return []

        waves = self._topo_waves(program)
        all_results: list[dict[str, Any]] = []

        for wave_idx, wave in enumerate(waves):
            wave_tasks = [
                self._run_group(group, context) for group in wave
            ]
            # asyncio.gather keeps order; one group's failure does NOT
            # cancel siblings (return_exceptions=True collects them).
            wave_results = await asyncio.gather(
                *wave_tasks, return_exceptions=False
            )
            for grp_results in wave_results:
                all_results.extend(grp_results)

            LOGGER.info(
                "PTC wave %d: %d groups, %d total calls",
                wave_idx, len(wave), sum(len(g.calls) for g in wave),
            )

        return all_results

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    async def _run_group(self, group: PTCGroup, context: Any) -> list[dict[str, Any]]:
        """Run one group's calls concurrently; return per-call results."""
        if not group.calls:
            return []

        async def _one(call: PTCCall) -> dict[str, Any]:
            try:
                tool = self.registry.get(call.name)
            except KeyError as e:
                return {
                    "group_id": group.id, "name": call.name, "ok": False,
                    "result": None, "error": str(e),
                }
            try:
                args = self._coerce_args(tool, call.args)
                if isinstance(args, dict) and "error" in args:
                    return {
                        "group_id": group.id, "name": call.name, "ok": False,
                        "result": None, "error": args["error"],
                    }
                if self.pipeline is not None:
                    # Route through 5-stage pipeline (HITL / metrics / audit)
                    pipe_res = await self.pipeline.run(
                        tool_name=call.name, args=args, tool_context=context,
                        executor=tool.invoke,
                    )
                    if not pipe_res.ok:
                        return {
                            "group_id": group.id, "name": call.name,
                            "ok": False, "result": None, "error": pipe_res.error,
                            "needs_approval": pipe_res.needs_approval,
                            "approval_payload": pipe_res.approval_payload,
                            "replaced": pipe_res.replaced,
                        }
                    return {
                        "group_id": group.id, "name": call.name, "ok": True,
                        "result": pipe_res.result, "error": None,
                        "replaced": pipe_res.replaced,
                    }
                # No pipeline — direct invoke (legacy behaviour)
                value = await tool.invoke(args, context)
                return {
                    "group_id": group.id, "name": call.name, "ok": True,
                    "result": value, "error": None,
                }
            except Exception as e:  # surface, don't crash the wave
                LOGGER.warning("PTC call %s failed: %s", call.name, e)
                return {
                    "group_id": group.id, "name": call.name, "ok": False,
                    "result": None, "error": str(e),
                }

        return await asyncio.gather(*(_one(c) for c in group.calls))

    def _coerce_args(self, tool: BaseTool, raw: dict[str, Any]) -> Any:
        """Coerce dict → Pydantic args_schema instance (mirrors orchestrator)."""
        try:
            schema_cls = tool.schema.args_schema
            if hasattr(schema_cls, "model_validate"):
                return schema_cls.model_validate(raw)
        except Exception as e:
            return {"error": f"args coerce failed: {e}"}
        return raw

    def _topo_waves(self, program: PTCProgram) -> list[list[PTCGroup]]:
        """Topologically sort groups into execution waves.

        Returns a list of waves; each wave is a list of independent groups
        that can run in parallel. Detects cycles and unknown deps.
        """
        ids = [g.id for g in program.groups]
        if len(ids) != len(set(ids)):
            from collections import Counter
            dupes = [i for i, c in Counter(ids).items() if c > 1]
            raise ValueError(f"duplicate group id in PTC program: {sorted(dupes)}")
        by_id = {g.id: g for g in program.groups}

        unknown_deps: list[str] = []
        for g in program.groups:
            for dep in g.depends_on:
                if dep not in by_id:
                    unknown_deps.append(f"{g.id} -> {dep}")
        if unknown_deps:
            raise ValueError(f"PTC program has unknown depends_on: {unknown_deps}")

        # Detect cycles via Kahn's algorithm
        indeg: dict[str, int] = {gid: 0 for gid in by_id}
        edges: dict[str, list[str]] = {gid: [] for gid in by_id}
        for g in program.groups:
            for dep in g.depends_on:
                indeg[g.id] += 1
                edges[dep].append(g.id)

        waves: list[list[PTCGroup]] = []
        remaining = dict(indeg)
        visited = set()

        while remaining:
            ready = [gid for gid, d in remaining.items() if d == 0]
            if not ready:
                cycle = sorted(remaining)
                raise ValueError(f"PTC program has cycle: {cycle}")
            wave = [by_id[gid] for gid in sorted(ready)]
            waves.append(wave)
            for gid in ready:
                visited.add(gid)
                del remaining[gid]
                for nxt in edges.get(gid, []):
                    if nxt in remaining:
                        remaining[nxt] -= 1

        return waves
