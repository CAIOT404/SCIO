"""The research brief (W-09) asks whether this repository can have a feature before it asks how
the feature behaves (I-227: the merge queue was planned for a user-owned repository, and
GitHub offers it only to organizations)."""

from __future__ import annotations

from scio.settings import REPO_ROOT

TEMPLATE = REPO_ROOT / ".claude" / "templates" / "research-brief.md"


def test_the_research_brief_asks_whether_this_repository_can_have_the_feature() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    availability = text.index("## 1. Can we have it at all?")
    assert availability < text.index("## 2. How it behaves")  # asked first
    section = text[availability : text.index("## 2.")]
    assert "availability line" in section
    for fact in ("owner type", "plan", "visibility"):
        assert f"- {fact}:" in section, fact
    assert "not documented" in text  # no guessing
