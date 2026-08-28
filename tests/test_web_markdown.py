from web.markdown import render_markdown


def test_report_markdown_supports_gfm_blocks_and_sanitizes_raw_html():
    source = """# 标题

---

## 指标

| 指标 | 数值 |
| :--- | ---: |
| RSI | **81.88** |

- [x] 已完成
- [ ] 待确认

~~旧结论~~

```python
print(1)
```

<script>alert('xss')</script>
"""

    rendered = render_markdown(source)

    assert "<h1>标题</h1>" in rendered
    assert "<hr>" in rendered
    assert "<table>" in rendered
    assert "<th style=\"text-align:left;\">指标</th>" in rendered
    assert 'style="text-align:right;"' in rendered
    assert 'type="checkbox"' in rendered
    assert "<s>旧结论</s>" in rendered
    assert '<pre><code class="language-python">print(1)' in rendered
    assert "<script" not in rendered
    assert "&lt;script&gt;alert('xss')&lt;/script&gt;" in rendered


def test_empty_report_markdown_returns_empty_html():
    assert render_markdown("") == ""
