"""SubagentProvider — 注册表把 sub-agent 从写死 class 解耦(roadmap §2.4 E5)。

dsh spec::
    SubagentProvider 注册表 + 6 backend
    (spawn / fork-in-process / ACP / Codex / Claude Code / DSH SDK)

我们的实现:name → factory callable,与 LLM_REGISTRY / ToolRegistry
的注册模式对齐。Factory 在 ``build(name, **kwargs)`` 时才实例化,
所以可以懒加载。

3 种注册方式:
1. Decorator: ``@subagent_provider.register(\"my_agent\")``
2. Manual:   ``subagent_provider.register(\"my_agent\", MyAgentCls)``
3. entry_points: pyproject.toml 配 ``agent_harness.subagents`` 组,
   Harness 启动时 ``discover_entry_points()`` 自动发现。

Why this exists
---------------
之前 6 个 builtin agents 在 ``harness.py`` 写死成 tuple,加新 agent 要改
framework。E5 spec 要求 \"sub-agent 注册化\",这里把 factory + name 解耦。
"""
from __future__ import annotations

import importlib.metadata as md
import inspect
import logging
from typing import Any, Callable

LOGGER = logging.getLogger(__name__)

AgentFactory = Callable[..., Any]  # returns BaseAgent instance


class SubagentProvider:
    """Name → factory 注册表。

    Factory 签名约定: ``factory(**kwargs) -> BaseAgent``。
    Harness 把 ``llm_factory + tool_registry + (可选) judge_factory`` 传进去。
    """

    def __init__(self) -> None:
        self._factories: dict[str, AgentFactory] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------
    def register(self, name: str, factory: AgentFactory | None = None) -> AgentFactory:
        """Register ``factory`` under ``name`` (decorator + manual form).

        用法 1(decorator)::
            @subagent_provider.register("my_agent")
            class MyAgent(BaseAgent):
                ...

        用法 2(manual)::
            subagent_provider.register("my_agent", MyAgentCls)
        """
        if factory is None:
            # Decorator form: ``register("name")(cls)``
            def _decorator(cls: AgentFactory) -> AgentFactory:
                self._register(name, cls)
                return cls
            return _decorator
        self._register(name, factory)
        return factory

    def _register(self, name: str, factory: AgentFactory) -> None:
        if name in self._factories:
            raise ValueError(
                f"subagent {name!r} already registered; "
                "use a different name or unregister first"
            )
        if not callable(factory):
            raise TypeError(f"factory for {name!r} must be callable")
        self._factories[name] = factory

    def unregister(self, name: str) -> bool:
        return self._factories.pop(name, None) is not None

    # ------------------------------------------------------------------
    # Build / lookup
    # ------------------------------------------------------------------
    def build(self, name: str, **kwargs: Any) -> Any:
        """Instantiate the agent by name with ``kwargs``.

        Factory signature: ``factory(**kwargs) -> BaseAgent``。Factory
        接受任意 kwargs,比如 ``llm_factory / tool_registry /
        judge_factory / enable_l3``。

        通过 ``inspect.signature`` 过滤出 factory 实际声明的参数,
        这样 harness 可以给所有 factory 传一份"完整" kwargs,各
        factory 只接收自己需要的那部分,不需要每个 name 维护一份
        kwargs map (VerifierAgent 比其他多 ``judge_factory +
        enable_l3``)。
        """
        if name not in self._factories:
            raise KeyError(
                f"subagent {name!r} not registered; "
                f"known: {sorted(self._factories)}"
            )
        factory = self._factories[name]
        try:
            sig = inspect.signature(factory)
        except (TypeError, ValueError):
            # Builtin / C callables — pass through, let it raise.
            return factory(**kwargs)
        accepted = {
            key: value
            for key, value in kwargs.items()
            if key in sig.parameters
        }
        return factory(**accepted)

    def list_names(self) -> list[str]:
        return sorted(self._factories)

    def has(self, name: str) -> bool:
        return name in self._factories

    def __contains__(self, name: str) -> bool:
        return self.has(name)

    def __len__(self) -> int:
        return len(self._factories)

    # ------------------------------------------------------------------
    # Entry-points discovery (3rd-party subagent plugins)
    # ------------------------------------------------------------------
    ENTRY_POINT_GROUP = "agent_harness.subagents"

    def discover_entry_points(self) -> int:
        """Discover 3rd-party subagent factories from installed packages.

        Each entry point's ``load()`` returns either:
        - a callable (registered as ``entry_point.name``)
        - a module containing ``register(provider)`` (we call it with self)

        Returns the number of new factories registered.
        """
        discovered = 0
        for ep in md.entry_points(group=self.ENTRY_POINT_GROUP):
            try:
                obj = ep.load()
                if callable(obj):
                    self._register(ep.name, obj)
                    discovered += 1
                    LOGGER.info(
                        "subagent entry_point registered: %s -> %s",
                        ep.name, getattr(obj, "__qualname__", repr(obj)),
                    )
                elif hasattr(obj, "register"):
                    obj.register(self)
                    discovered += 1
                    LOGGER.info(
                        "subagent entry_point module registered: %s",
                        ep.name,
                    )
                else:
                    LOGGER.warning(
                        "subagent entry_point %s: object has no "
                        "'register' method and isn't callable; skipping",
                        ep.name,
                    )
            except Exception:
                LOGGER.exception(
                    "subagent entry_point %s failed to load", ep.name,
                )
        return discovered


# --------------------------------------------------------------------------
# Module-level singleton — default registry, like LLM_REGISTRY
# --------------------------------------------------------------------------
SUBAGENT_PROVIDER = SubagentProvider()


def register(name: str) -> Callable[[AgentFactory], AgentFactory]:
    """Shorthand: ``@register(\"foo\")``."""
    return SUBAGENT_PROVIDER.register(name)
