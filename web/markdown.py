"""Safe, GitHub-flavoured Markdown rendering for report content."""

from __future__ import annotations

from bleach import clean
from bleach.css_sanitizer import CSSSanitizer
from markdown_it import MarkdownIt
from mdit_py_plugins.tasklists import tasklists_plugin


_MARKDOWN = MarkdownIt(
    "gfm-like",
    {
        "breaks": True,
        "html": False,
        "linkify": True,
        "xhtmlOut": True,
    },
).use(tasklists_plugin)

_ALLOWED_TAGS = {
    "a",
    "blockquote",
    "br",
    "code",
    "del",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "input",
    "li",
    "ol",
    "p",
    "pre",
    "s",
    "strong",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "ul",
}
_ALLOWED_ATTRIBUTES = {
    "a": {"href", "title", "target", "rel"},
    "code": {"class"},
    "input": {"type", "checked", "disabled", "class"},
    "li": {"class"},
    "th": {"style"},
    "td": {"style"},
    "ul": {"class"},
}
_CSS_SANITIZER = CSSSanitizer(allowed_css_properties={"text-align"})


def render_markdown(source: str | None) -> str:
    """Render report Markdown as a constrained HTML fragment."""

    if not source:
        return ""
    rendered = _MARKDOWN.render(str(source))
    return clean(
        rendered,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        protocols={"http", "https", "mailto"},
        css_sanitizer=_CSS_SANITIZER,
        strip=True,
    )
