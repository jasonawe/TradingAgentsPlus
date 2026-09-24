"""Harness 主类 — 组装所有组件(v3 spec §6, N4/N15/N36 fix).

Init order (must follow §6.0 docstring):
1.  config (HarnessConfig)
2.  tool_registry (ToolRegistry) + builtin tools (P3)
3.  agent_registry (AgentRegistry) + 6 builtin agents (P5)
4.  llm_factory (LLMFactory)
5.  data_registry (PROVIDERS dict — P2)
6.  memory (MemoryManager)
7.  audit (AuditLogger)
8.  health (HealthChecker — P7)
9.  context_priority (ContextPriority — P4)
10. retry_policy + circuit_breaker (P4)
11. routing (fast_route — P4)
12. plugin_registry (PluginRegistry + entry_points — P6)
13. orchestrator (Orchestrator, depends on 2-11)
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import AsyncIterator

from tradingagents.agent_harness.config.schema import HarnessConfig
from tradingagents.agent_harness.core import (
    AgentScope,
    CircuitBreaker,
    ContextPriority,
    EventBus,
    Orchestrator,
    RetryPolicy,
)
from tradingagents.agent_harness.tools import (
    ToolRegistry,
    install_builtin_tools,
)

LOGGER = logging.getLogger(__name__)


class Harness:
    """Harness 主类 — 组装 tool/agent/registry + orchestrator, 提供统一入口."""

    @classmethod
    def from_data_dir(cls, data_dir, **kwargs):
        """Build a Harness with a minimal config pointing at ``data_dir``.

        Convenience constructor for tests + CLI bootstrap. Production
        code should still pass a fully-built HarnessConfig.
        """
        from .config.schema import HarnessConfig
        from pathlib import Path as _P
        cfg = HarnessConfig(data_dir=_P(str(data_dir)))
        return cls(config=cfg, **kwargs)

    def __init__(
        self,
        config: HarnessConfig | None = None,
        *,
        settings_lookup: "Callable[[str], str | None] | None" = None,
    ) -> None:
        """Build a Harness.

        ``settings_lookup`` is the optional runtime-config hook (see
        ``LLMFactory.settings_lookup``). When provided, the main and
        judge LLM factories consult it on each TTL miss so the user
        can switch providers/models via the settings page without
        restarting the service. Tests + CLI bootstrap pass ``None``
        to keep the legacy env-var-only behaviour.
        """
        self.config = config or HarnessConfig.from_env()
        self.settings_lookup = settings_lookup

        # 1b. EventBus (W3-D3 E6) — single unified event bus for plugin
        # cross-cutting hooks. 3rd-party plugins can subscribe to
        # "tool.invoked" / "llm.complete" / "tool.execute" etc.
        # through harness.events.subscribe(...). Built before tool /
        # agent / plugin wiring so plugins can register handlers.
        self.events = EventBus()

        # 2. ToolRegistry + builtin tools
        self.tool_registry = ToolRegistry()
        install_builtin_tools(self.tool_registry)

        # W3-D6 R8: AppIdentity User-Agent 强制
        # 全进程共享一个 identity(harness 自己持有),所有通过
        # llm_factory.make() 创建的 provider 都会自动注入 identity
        # headers 到 ChatOpenAI.default_headers。
        from tradingagents.agent_harness.llm import default_app_identity
        self.app_identity = default_app_identity()

        # 4. llm_factory — wraps tradingagents/llm_clients (v3 spec §3 llm/).
        # Built BEFORE agents so we can inject it into them.
        # §P3-4 — when a settings_lookup is provided, the factory
        # delegates (provider, model) resolution to it on each TTL miss
        # so users can switch at runtime via the settings page.
        from tradingagents.agent_harness.llm import LLMFactory
        self.llm_factory = LLMFactory(
            default_provider=self.config.llm_provider,
            default_model=self.config.llm_model,
            identity=self.app_identity,
            settings_lookup=settings_lookup,
            role="main",
        )

        # 4a. per-agent override factories (Phase 3). When the user
        # sets both ``llm.agents.<name>.provider`` and
        # ``llm.agents.<name>.model`` in settings_repo, that agent
        # gets a dedicated factory with role="agent_<name>" so the
        # mode-routing short-circuit in ``make(mode=...)`` is
        # ignored — the override is "this exact model, period".
        # Agents without overrides keep using ``self.llm_factory``
        # (Phase 2 quick/deep behaviour).
        from tradingagents.agent_harness.llm.factory import LLMFactory as _LLMF
        self._agent_overrides: dict[str, _LLMF] = {}
        if settings_lookup is not None:
            for agent_name in ("planner", "data", "news", "alpha", "synth"):
                try:
                    p_key, m_keys = _LLMF._agent_role_keys(agent_name)
                    p_setting = settings_lookup(p_key)
                    m_setting = settings_lookup(m_keys["unspecified"])
                except Exception:
                    p_setting = m_setting = None
                if p_setting and m_setting:
                    role = f"agent_{agent_name}"
                    _LLMF._ROLE_KEYS[role] = _LLMF._agent_role_keys(agent_name)
                    self._agent_overrides[agent_name] = _LLMF(
                        default_provider=p_setting,
                        default_model=m_setting,
                        identity=self.app_identity,
                        settings_lookup=settings_lookup,
                        role=role,
                    )
                    LOGGER.info(
                        "Per-agent LLM override active for %s: %s/%s",
                        agent_name, p_setting, m_setting,
                    )

        # 4b. judge_factory — L3 LLM-judge 模型 (spec §D6 N89 fix: judge
        # 不复用主 LLM)。
        #
        # §P3-4 — always create a separate factory with role="judge".
        # Previously the harness aliased ``self.judge_factory =
        # self.llm_factory`` when the static HarnessConfig had empty
        # judge_* fields, which silently ignored any
        # ``llm.judge_provider`` / ``llm.judge_model`` the user set on
        # the settings page (since the shared factory's role was
        # always "main"). A standalone role="judge" factory reads
        # ``llm.judge_*`` keys first; if both settings and defaults
        # are empty, ``is_configured()`` returns False and the
        # verifier falls back to its non-LLM path — preserving the
        # legacy "no judge configured" behaviour.
        self.judge_factory = LLMFactory(
            default_provider=self.config.judge_provider,
            default_model=self.config.judge_model,
            identity=self.app_identity,
            settings_lookup=settings_lookup,
            role="judge",
        )

        # 3. AgentRegistry + 6 builtin agents (N64 fix, P8 LLM wiring).
        # W3-D1 E5: sub-agents are now wired through SubagentProvider (name
        # → factory) instead of a hard-coded class tuple. 3rd-party agents
        # can register themselves via the ``agent_harness.subagents`` entry
        # point group and join the same registry at startup.
        from tradingagents.agent_harness.agents import (
            AgentRegistry,
            AlphaAgent,
            DataAgent,
            NewsAgent,
            PlannerAgent,
            SubagentProvider,
            SynthesizerAgent,
            VerifierAgent,
        )
        # W3-D4 E7: per-agent scope. The harness owns the default scope
        # (no restrictions); individual agents can shadow it to restrict
        # tools or override the LLM. Plugins can mutate
        # ``harness.default_agent_scope`` before agents are built.
        self.default_agent_scope = AgentScope(name="harness")
        self.subagent_provider = SubagentProvider()
        # 3rd-party first so builtin names win on collision (we want a
        # bad plugin to surface as ``ValueError`` rather than silently
        # shadowing the builtin).
        self.subagent_provider.discover_entry_points()
        for name, cls in (
            ("planner", PlannerAgent),
            ("verifier", VerifierAgent),
            ("data_agent", DataAgent),
            ("alpha_agent", AlphaAgent),
            ("news_agent", NewsAgent),
            ("synthesizer", SynthesizerAgent),
        ):
            self.subagent_provider.register(name, cls)

        # All build kwargs (full superset). ``SubagentProvider.build``
        # filters by factory signature, so ``verifier`` picks up the
        # extra judge / L3 args and the others ignore them. No per-name
        # map required → 3rd-party entry points just work.
        # E7: ``scope`` is passed too; agents that declare it (e.g. when
        # subclasses opt in) get the harness default scope.
        self.agent_registry = AgentRegistry()
        # §P3-4 Phase 3 — map builtin agent names → override slot
        # names. The override slots use shorter, intent-revealing
        # names ("data" / "news" / "alpha" / "synth") while the
        # agent registry uses the long names ("data_agent" /
        # "news_agent" etc.).
        _agent_to_slot = {
            "planner": "planner",
            "data_agent": "data",
            "news_agent": "news",
            "alpha_agent": "alpha",
            "synthesizer": "synth",
        }

        def _llm_factory_for(agent_name: str):
            """Return the dedicated factory for an agent when the
            user has set both ``llm.agents.<slot>.provider`` and
            ``llm.agents.<slot>.model``; otherwise return the main
            factory (Phase 2 quick/deep behaviour).

            Built-in agents that have no override slot configured
            silently fall back to ``self.llm_factory``; 3rd-party
            agents registered via entry points likewise get the main
            factory unless the operator has set their override
            keys.
            """
            slot = _agent_to_slot.get(agent_name)
            if slot is not None and slot in self._agent_overrides:
                return self._agent_overrides[slot]
            return self.llm_factory

        build_kwargs = {
            "llm_factory": self.llm_factory,  # default; overridden per agent below
            "tool_registry": self.tool_registry,
            "judge_factory": self.judge_factory,
            "enable_l3": self.config.enable_l3,
            "scope": self.default_agent_scope,
        }
        for name in self.subagent_provider.list_names():
            per_agent_kwargs = dict(build_kwargs)
            per_agent_kwargs["llm_factory"] = _llm_factory_for(name)
            agent = self.subagent_provider.build(name, **per_agent_kwargs)
            self.agent_registry.register(agent)
        # Expose the per-agent factory lookup so orchestrator /
        # runtime components can ask "which factory does agent X
        # use right now?" without having to know the slot map.
        self.llm_factory_for = _llm_factory_for

        # 5. data_registry (PROVIDERS dict)
        from tradingagents.data.providers.registry import PROVIDERS
        self.data_registry = PROVIDERS

        # 6. memory — L1/L2/L3 facade (v3 spec §3 memory/)
        from tradingagents.agent_harness.memory import MemoryManager, EventLog
        from tradingagents.agent_harness.memory.l1_session import SqliteSessionMemory
        # W3-D7 A6: L1 默认走 LangGraph SqliteSaver,和
        # ``agents/general/orchestrator.py`` 共用
        # ``{data_dir}/agent_general/sessions/agent_<safe_id>.db`` 文件,
        # session 级 DELETE 时一个 unlink 就够了。
        l1 = SqliteSessionMemory(
            use_langgraph_checkpointer=True,
            data_dir=str(self.config.data_dir),
        )
        self.memory = MemoryManager(data_dir=str(self.config.data_dir), l1=l1)
        # W3-D5 R1: append-only event log (separate from L1 chat history).
        # Plugins and orchestrator can append to ``harness.event_log``;
        # the LLM chat history is derived from ``type='message'`` events.
        self.event_log = EventLog(
            db_path=str(Path(self.config.data_dir) / "event_log.sqlite"),
        )

        # W3-D6 R9: 把 L1 history 滚动归档接到 EventLog。
        # 长会话超出 archive_threshold 的消息推到
        # ``type=archive/legacy_history``(surface=audit-only,不进 LLM context),
        # 不再静默丢。callback 由 L1 内部异常隔离,主流程不阻塞。
        from tradingagents.agent_harness.memory.l1_session import SqliteSessionMemory
        if isinstance(self.memory.l1, SqliteSessionMemory):
            self.memory.l1._archive_callback = (
                lambda session_id, msgs: self.event_log.append(
                    session_id,
                    "archive/legacy_history",
                    {"archived_count": len(msgs), "messages": msgs},
                )
            )

        # 7. audit (P7) — single-source: route lifecycle events
        # through the same EventLog as SurfaceOp conversation events.
        # The legacy ``audit.log`` JSONL stays as a fallback for
        # standalone / test usage where no EventLog is wired in.
        from tradingagents.agent_harness.observability import AuditLogger
        self.audit = AuditLogger(
            self.config.data_dir,
            event_log=self.event_log,
        )

        # 8. health (P7)
        from tradingagents.agent_harness.observability import (
            HealthChecker, Metrics, Tracer,
        )
        self.health = HealthChecker()
        self.metrics = Metrics()
        self.tracer = Tracer()

        # 9. context priority — auto-injects memory L1/L2 (P8 Task 2)
        self.context_priority = ContextPriority(memory=self.memory)
        # §D4 entry_points — discover the 8 builtin layer providers and
        # wire live dependencies (memory + tool_registry). Third-party
        # plugins shipping through the ``tradingagents.context.providers``
        # entry-point group are appended automatically.
        self.context_priority.discover_providers(
            memory=self.memory,
            tool_registry=self.tool_registry,
        )
        # Q5 / P2-8: cooperative system-prompt waterfall. Plugins can
        # ``harness.system_prompt_waterfall.add(WaterfallSection(...))``
        # in ``install()`` to contribute sections at runtime. The
        # waterfall composes sections by priority and is the canonical
        # way to merge plugin-injected prompts without monkey-patching
        # ``ContextPriority.assemble()``.
        from tradingagents.agent_harness.core.system_prompt_waterfall import (
            SystemPromptWaterfall,
        )
        self.system_prompt_waterfall = SystemPromptWaterfall(
            final_prefix="TradingAgentsPlus harness",
        )

        # 10. retry + circuit breaker
        self.retry_policy = RetryPolicy(max_retries=2, backoff_seconds=0.5, exponential=True)
        self.circuit_breaker = CircuitBreaker(failure_threshold=5, reset_seconds=30.0)

        # 12. plugin registry + builtin plugins (P6)
        from tradingagents.agent_harness.plugins import PluginRegistry
        from tradingagents.agent_harness.plugins.builtin import (
            AlertPlugin, NewsPlugin, QuantPlugin,
        )
        # §7.3 #7: lazy_plugin_load 决定 PluginRegistry 是否在启动时 import
        # 第三方 plugin。启用时仅在 ``registry.get(name)`` 首次访问才 load。
        self.plugin_registry = PluginRegistry(self, lazy=self.config.lazy_plugin_load)
        for cls in (QuantPlugin, NewsPlugin, AlertPlugin):
            self.plugin_registry.register(cls())
        self.plugin_registry.discover_entry_points()

        # 13. orchestrator (depends on 2, 3, 9, 10)
        self.orchestrator = Orchestrator(
            tool_registry=self.tool_registry,
            agent_registry=self.agent_registry,
            llm_factory=self.llm_factory,
            context_priority=self.context_priority,
            retry_policy=self.retry_policy,
            circuit_breaker=self.circuit_breaker,
            audit=self.audit,
            enable_l3=self.config.enable_l3,
            judge_factory=self.judge_factory,
            memory=self.memory,
            # §P3-4 Phase 3 — orchestrator's own _llm_plan /
            # _llm_synthesize paths use the per-agent override factory
            # when one is configured ("planner" / "synthesizer"),
            # otherwise the main factory (Phase 2 quick/deep).
            llm_factory_for=self.llm_factory_for,
        )

        # 14. supervised AgentRuntime components (Task 19)
        self._init_runtime_components()

        # §1.3 A3 — single-session-lifecycle facade. Built with
        # l1 + event_log; session_store + checkpoint_store are filled
        # in later by set_session_store / set_checkpoint_store. Missing
        # collaborators are silently skipped on delete, so the manager
        # is safe to use partially.
        from tradingagents.agent_harness.core.session_manager import (
            SessionManager,
        )
        self.session_manager = SessionManager(
            l1=self.memory.l1 if self.memory is not None else None,
            event_log=self.event_log,
        )
        self.orchestrator._session_manager = self.session_manager

        LOGGER.info(
            "Harness ready (tools=%d, providers=%d, stage=P3+P4+P5+P6+P7)",
            len(self.tool_registry.list_all()),
            len(self.data_registry),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def stream_chat(
        self,
        session_id: str,
        user_message: str,
        *,
        history: list | None = None,
    ) -> AsyncIterator[tuple[str, dict]]:
        """Tier-aware unified entry — delegates to orchestrator."""
        async for event in self.orchestrator.stream_chat(
            session_id=session_id,
            user_message=user_message,
            history=history,
        ):
            yield event

    def set_checkpoint_store(self, store) -> None:
        """Wire a ``HarnessCheckpointStore`` into the underlying orchestrator.

        After this call, every ``stream_chat`` invocation will persist
        per-session checkpoints so a crashed session can be resumed via
        ``resume(session_id)``.
        """
        self.orchestrator._checkpoint_store = store
        if getattr(self, "session_manager", None) is not None:
            self.session_manager.checkpoint_store = store

    def set_plan_cache_db_path(
        self,
        db_path: str | None,
        *,
        ttl_seconds: float = 300.0,
        max_entries: int = 256,
    ) -> None:
        """Step 28-E — swap the orchestrator's plan cache to a persistent SQLite-backed one.

        The default cache (in-memory only) loses all entries on
        restart. By passing db_path here, every subsequent
        :class:`PersistentPlanCache` set/get goes through the same
        LRU + TTL semantics but survives process restarts. Useful for
        prod where the web server gets restarted (gunicorn workers,
        uvicorn reload, deploys) — without this, every fresh process
        has to re-plan every symbol combination on first request.

        Safe to call multiple times — re-instantiates the cache.
        """
        if not db_path:
            return
        from tradingagents.agent_harness.core.persistent_plan_cache import (
            PersistentPlanCache,
        )
        self.orchestrator.plan_cache = PersistentPlanCache(
            db_path=db_path,
            ttl_seconds=ttl_seconds,
            max_entries=max_entries,
        )

    def set_session_store(self, store) -> None:
        """Wire a ``SessionStore`` into the underlying orchestrator (A2).

        After this call, every ``stream_chat`` invocation will
        auto-create / touch the session row so the front-end can list
        active sessions with real ``last_active`` / ``message_count``.
        DELETE /api/agent/sessions/{id} also relies on this wiring.
        """
        self.orchestrator._session_store = store
        # §1.3 A3 — keep the SessionManager facade in sync so DELETE
        # cleans every session-scoped store in one go.
        self.session_manager.session_store = store
        if hasattr(self.orchestrator, "_checkpoint_store") and \
                self.orchestrator._checkpoint_store is not None:
            self.session_manager.checkpoint_store = (
                self.orchestrator._checkpoint_store
            )

    async def resume(
        self, session_id: str,
    ) -> AsyncIterator[tuple[str, dict]]:
        """Resume a crashed/interrupted session from its last checkpoint.

        See ``Orchestrator.resume`` for details.
        """
        async for event in self.orchestrator.resume(session_id):
            yield event

    def get_tool(self, name: str):
        return self.tool_registry.get(name)

    def list_tools(self):
        return self.tool_registry.list_all()


# ---------------------------------------------------------------------------
# FastAPI integration helper
# ---------------------------------------------------------------------------

    # ─── Task 19 — supervised Runtime assembly ────────────────────

    def _init_runtime_components(self) -> None:
        """Wire up RuntimeStore / scheduler / dispatcher / runtime / policy.

        Coexists with the existing V1 execution path — Harness.stream_chat
        still goes through the legacy Orchestrator (TurnCoordinator) until
        Task 24 production cutover.
        """
        try:
            from .runtime.store import AgentRuntimeStore
            from .runtime.scheduler import TaskScheduler
            from .runtime.dispatcher import AgentDispatcher
            from .runtime.policy import PolicyGuard
            from .runtime.projector import TraceProjector
            from .agents.registry import AgentRegistry
            from .agents.base import AgentDescriptor

            runtime_db = self.config.data_dir / "agent_runtime.sqlite"
            self.runtime_store = AgentRuntimeStore(runtime_db)

            # Don't replace self.agent_registry — it's already populated
            # by SubagentProvider with the real V1 agents.  Augment it
            # with V2 descriptors for the builtins so runtime layer can
            # do capability lookup + handoff metadata.
            from .agents.base import BaseAgent, AgentResult
            reg = self.agent_registry
            builtin_specs = [
                ("planner", ("planning",), "global", 10),
                ("data_agent", ("domain_lookup",), "user", 5),
                ("news_agent", ("news_lookup",), "user", 5),
                ("alpha_agent", ("alpha_compute",), "user", 5),
                ("verifier", ("verify_evidence", "verify_answer"), "global", 8),
                ("synthesizer", ("synthesis",), "global", 8),
            ]
            class _BuiltinProxy(BaseAgent):
                def __init__(self, n):
                    self.name = n
                    self.description = n
                    self.tools = []
                async def run(self, input, *, context):
                    return AgentResult(success=True, content=n)
            for name, caps, scope, prio in builtin_specs:
                instance = reg.get(name) if reg.has(name) else _BuiltinProxy(name)
                if not reg.has(name):
                    reg.register(instance)
                reg.register_v2(
                    AgentDescriptor(
                        name=name, version=2,
                        capabilities=caps, scope=scope, priority=prio,
                    ),
                    instance=instance,
                )
            self.policy = PolicyGuard(
                registry=None,
                max_tasks=10, max_handoff_depth=2,
                max_messages=20, max_repairs=1,
                deadline_seconds=600.0, max_tokens=4000,
            )
            self.scheduler = TaskScheduler(self.runtime_store, lease_seconds=60)

            # §0.4.32 — Task 24 production cutover: wire real V2 collaborators.
            # ContextAssembler builds the 6-layer agent context for every
            # task. MessageIngestor sanitizes + canonicalizes + enforces
            # idempotency on every message. CommandResolver is the single
            # source of truth for write-tool CRUD dispatch (replaces the
            # legacy Orchestrator._CRUD_DISPATCH dict).
            from .runtime.ingest import MessageIngestor
            from .core.context_assembler import ContextAssembler
            from .core.command_resolver import CommandResolver

            self.message_ingestor = MessageIngestor()
            self.context_provider = ContextAssembler(
                store=self.runtime_store,
                memory=getattr(self, "memory", None),
            )
            self.command_resolver = CommandResolver()

            self.dispatcher = AgentDispatcher(
                agent_registry=reg,
                command_resolver=self.command_resolver,
                message_ingestor=self.message_ingestor,
                context_provider=self.context_provider,
            )
            self.trace_projector = TraceProjector(store=self.runtime_store)

            # Real AgentRuntime facade (Task 12 + Task 19). The runtime
            # composes store / agent_registry / command_resolver /
            # message_ingestor / context_provider into a single API the
            # web adapter can call.
            from .runtime.runtime import AgentRuntime
            self.runtime = AgentRuntime(
                store=self.runtime_store,
                agent_registry=reg,
                command_resolver=self.command_resolver,
                message_ingestor=self.message_ingestor,
                context_provider=self.context_provider,
                scheduler_factory=lambda s: self.scheduler,
                policy_guard=self.policy,
            )
            # Track whether V2 cutover is wired (read by web/app.py
            # chat route to choose between V1 orchestrator and V2
            # runtime).
            self.runtime_active = True
        except Exception as exc:  # pragma: no cover
            LOGGER.warning("Harness runtime assembly partial: %s", exc)
            self.runtime_store = None
            self.scheduler = None
            self.dispatcher = None
            self.runtime = None
            self.policy = None
            self.trace_projector = None
            self.message_ingestor = None
            self.context_provider = None
            self.command_resolver = None
            self.runtime_active = False


def mount_health_endpoint(app, harness: "Harness", path: str = "/api/harness/health") -> None:
    """Attach the `` /api/harness/health `` endpoint to ``app``.

    Caller decides when to call this (e.g. inside ``web/app.create_app()``);
    we keep the harness package free-free of FastAPI dependencies.
    """
    try:
        from fastapi import HTTPException  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "FastAPI is required to mount the health endpoint; "
            "pip install fastapi"
        ) from e

    @app.get(path)
    async def _health():  # type: ignore[misc]
        try:
            return await harness.health.check_all(harness)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    LOGGER.info("mounted harness health endpoint at %s", path)
