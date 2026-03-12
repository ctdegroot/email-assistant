"""
Tests for note_generator.py — pure logic only.
No Claude API calls, no filesystem access.
"""

import pytest

from email_to_motion.note_generator import (
    _extract_tags_from_markdown,
    _safe_filename,
)


# ── _safe_filename ────────────────────────────────────────────────────────────

class TestSafeFilename:
    DATE = "2026-03-12"

    def test_basic_subject(self):
        assert _safe_filename("Hello World", self.DATE) == "2026-03-12 - Hello World.md"

    def test_always_ends_with_md(self):
        result = _safe_filename("Test", self.DATE)
        assert result.endswith(".md")

    def test_date_prefix_present(self):
        result = _safe_filename("Subject", self.DATE)
        assert result.startswith(self.DATE)

    def test_forbidden_chars_removed(self):
        forbidden = r'\/:*?"<>|#%{}'
        result = _safe_filename(f"File{forbidden}Name", self.DATE)
        for ch in forbidden:
            assert ch not in result

    def test_multiple_spaces_collapsed(self):
        result = _safe_filename("Hello   World", self.DATE)
        assert "  " not in result

    def test_long_subject_truncated(self):
        subject = "A" * 100
        result  = _safe_filename(subject, self.DATE)
        # Subject portion is capped at 60 chars
        # Full name = date + " - " + subject[:60] + ".md"
        subject_part = result[len(self.DATE) + len(" - "):-len(".md")]
        assert len(subject_part) <= 60

    def test_short_subject_not_truncated(self):
        subject = "Short"
        result  = _safe_filename(subject, self.DATE)
        assert subject in result

    def test_strips_leading_trailing_whitespace(self):
        result = _safe_filename("  My Subject  ", self.DATE)
        # Should not have leading/trailing spaces in the subject portion
        subject_part = result[len(self.DATE) + len(" - "):-len(".md")]
        assert subject_part == subject_part.strip()

    def test_empty_subject_produces_valid_filename(self):
        # Empty subject → just the date prefix
        result = _safe_filename("", self.DATE)
        assert result.endswith(".md")
        assert self.DATE in result


# ── _extract_tags_from_markdown ───────────────────────────────────────────────

def _note(frontmatter: str, body: str = "## Summary\nTest content.") -> str:
    """Wrap frontmatter and body in a valid Obsidian-style markdown note."""
    return f"---\n{frontmatter}\n---\n\n{body}"


class TestExtractTagsFromMarkdown:

    def test_inline_tags(self):
        md = _note("date: 2026-03-12\nfrom: Alice\nsubject: Test\ntags: [lab-safety, budget, policy]")
        assert _extract_tags_from_markdown(md) == ["lab-safety", "budget", "policy"]

    def test_inline_tags_with_extra_spaces(self):
        md = _note("tags: [ lab-safety ,  budget ]")
        tags = _extract_tags_from_markdown(md)
        assert tags == ["lab-safety", "budget"]

    def test_block_tags(self):
        md = _note("date: 2026-03-12\ntags:\n  - lab-safety\n  - budget")
        tags = _extract_tags_from_markdown(md)
        assert "lab-safety" in tags
        assert "budget" in tags

    def test_block_tags_all_returned(self):
        md = _note("tags:\n  - alpha\n  - beta\n  - gamma")
        tags = _extract_tags_from_markdown(md)
        assert set(tags) == {"alpha", "beta", "gamma"}

    def test_no_frontmatter_returns_empty(self):
        assert _extract_tags_from_markdown("No frontmatter here.") == []

    def test_frontmatter_without_tags_returns_empty(self):
        md = _note("date: 2026-03-12\nfrom: Alice\nsubject: Test")
        assert _extract_tags_from_markdown(md) == []

    def test_empty_inline_tags(self):
        md = _note("tags: []")
        assert _extract_tags_from_markdown(md) == []

    def test_single_tag(self):
        md = _note("tags: [policy]")
        assert _extract_tags_from_markdown(md) == ["policy"]

    def test_inline_quoted_tags_stripped(self):
        md = _note("tags: ['lab-safety', 'budget']")
        tags = _extract_tags_from_markdown(md)
        assert "lab-safety" in tags
        assert "budget" in tags
        # Quotes should be stripped
        for t in tags:
            assert "'" not in t
