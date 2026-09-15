"""Jinja2 template engine for Tier 1b (v2 spec §D1 N101 fix).

Tier 1a: raw emit (regex → tool → emit raw data → frontend renders cards)
Tier 1b: template 合成 (regex → tool → Jinja2 模板拼文字回答 → emit text)

触发条件:query 含「说明」「解释」关键词,或 fast_route 在 Tier 1 上
显式标记 ``template=true``。

注册模板后,``TemplateEngine.render(name, **ctx)`` 返字符串。
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from jinja2 import Environment, StrictUndefined, select_autoescape

LOGGER = logging.getLogger(__name__)

#: 触发 Tier 1b 模板合成的关键词(query 中命中任一即触发)
TEMPLATE_TRIGGER_KEYWORDS: tuple[str, ...] = (
    "说明", "解释", "讲讲", "介绍", "详细说明", "用文字",
)


#: 内置模板 — 覆盖常见 Tier 1 数据查询的文字合成场景。
DEFAULT_TEMPLATES: dict[str, str] = {
    "quote_simple": (
        "{{ symbol }} 现价 {{ price | round(2) }} 元,"
        "{% if change_pct is not none %}"
        "涨跌 {{ change | round(2) }} 元 ({{ change_pct | round(2) }}%)"
        "{% else %}"
        "涨跌 {{ change | round(2) }} 元"
        "{% endif %}。"
        "{% if volume %}成交量 {{ volume }} 股。{% endif %}"
    ),
    "fundamentals_simple": (
        "{% if pe_ratio is defined %}PE {{ pe_ratio | round(2) }}。"
        "{% endif %}"
        "{% if pb_ratio is defined %}PB {{ pb_ratio | round(2) }}。"
        "{% endif %}"
        "{% if market_cap is defined %}总市值 {{ (market_cap / 1e8) | round(2) }} 亿元。"
        "{% endif %}"
    ),
    "history_simple": (
        "最近 {{ bars | length }} 个交易日收盘价:{{ closes | join(', ') }}。"
        "{% if avg_close %}均价 {{ avg_close | round(2) }}。{% endif %}"
    ),
    "news_simple": (
        "{% if items %}"
        "共 {{ items | length }} 条新闻:"
        "{% for item in items %}- {{ item.title }}{% endfor %}"
        "{% else %}暂无新闻。{% endif %}"
    ),
}


class TemplateEngine:
    """Jinja2-based template engine for Tier 1b.

    Thread-safe (Environment is thread-safe per Jinja2 docs).
    """

    def __init__(self, templates: dict[str, str] | None = None) -> None:
        self._templates: dict[str, str] = dict(templates or DEFAULT_TEMPLATES)
        self._lock = threading.Lock()
        self.env = Environment(
            autoescape=select_autoescape(default_for_string=False, default=False),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    # ------------------------------------------------------------------
    # Template registry
    # ------------------------------------------------------------------
    def register(self, name: str, template_str: str, *, overwrite: bool = False) -> None:
        """注册 / 覆盖一个模板。

        默认拒绝覆盖已有 name(``overwrite=False``)防止插件误踩核心模板。
        """
        with self._lock:
            if name in self._templates and not overwrite:
                raise ValueError(f"template {name!r} already registered; "
                                 "pass overwrite=True to replace")
            self._templates[name] = template_str

    def unregister(self, name: str) -> bool:
        with self._lock:
            return self._templates.pop(name, None) is not None

    def has(self, name: str) -> bool:
        with self._lock:
            return name in self._templates

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._templates.keys())

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    def render(self, name: str, **ctx: Any) -> str:
        """渲染 ``name`` 模板 with ``ctx`` 上下文。

        Missing template raises KeyError.  Jinja2 errors propagate as
        ``jinja2.TemplateError`` so the caller can fall back to raw emit.
        """
        with self._lock:
            tmpl_str = self._templates.get(name)
        if tmpl_str is None:
            raise KeyError(f"unknown template: {name!r}")
        tmpl = self.env.from_string(tmpl_str)
        return tmpl.render(**ctx).strip()


def should_use_template(message: str) -> bool:
    """判断 query 是否应该走 Tier 1b 模板路径(N101 fix)。"""
    if not message:
        return False
    lower = message.lower()
    return any(kw in lower for kw in TEMPLATE_TRIGGER_KEYWORDS)
