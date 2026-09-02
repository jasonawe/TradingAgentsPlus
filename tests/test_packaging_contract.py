from pathlib import Path


def test_runtime_dependencies_cover_web_markdown_imports():
    pyproject = (Path(__file__).parents[1] / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    assert '"bleach>=6.0,<7.0"' in pyproject
    assert '"tinycss2>=1.4,<2.0"' in pyproject
