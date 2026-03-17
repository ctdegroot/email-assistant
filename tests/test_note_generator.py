"""
Tests for note_generator.py — pure logic only.
No Claude API calls, no filesystem access.
"""

import pytest

from email_to_motion.note_generator import (
    _extract_tags_from_markdown,
    _safe_filename,
    _strip_note_fence,
    _clean_note_output,
    _quote_frontmatter_scalars,
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


# ── _strip_note_fence ──────────────────────────────────────────────────────────

_BARE_NOTE = '---\ndate: "2026-03-12"\n---\n\n## Summary\nHello.'


class TestStripNoteFence:

    def test_no_fence_unchanged(self):
        assert _strip_note_fence(_BARE_NOTE) == _BARE_NOTE

    def test_markdown_fence_stripped(self):
        fenced = f"```markdown\n{_BARE_NOTE}\n```"
        assert _strip_note_fence(fenced) == _BARE_NOTE

    def test_yaml_fence_stripped(self):
        fenced = f"```yaml\n{_BARE_NOTE}\n```"
        assert _strip_note_fence(fenced) == _BARE_NOTE

    def test_plain_fence_stripped(self):
        fenced = f"```\n{_BARE_NOTE}\n```"
        assert _strip_note_fence(fenced) == _BARE_NOTE

    def test_md_fence_stripped(self):
        fenced = f"```md\n{_BARE_NOTE}\n```"
        assert _strip_note_fence(fenced) == _BARE_NOTE

    def test_leading_trailing_whitespace_ignored(self):
        fenced = f"  ```markdown\n{_BARE_NOTE}\n```  "
        # strip_note_fence calls text.strip() first, so surrounding whitespace is fine
        assert _strip_note_fence(fenced) == _BARE_NOTE

    def test_partial_fence_not_stripped(self):
        # Only an opening fence — should be left alone rather than mangled
        partial = f"```markdown\n{_BARE_NOTE}"
        result = _strip_note_fence(partial)
        assert "---" in result  # content preserved, no crash


# ── _quote_frontmatter_scalars ────────────────────────────────────────────────

class TestQuoteFrontmatterScalars:

    def test_quotes_email_in_from(self):
        fm = 'from: Sarah Brooks <sarah.brooks@uwo.ca>'
        result = _quote_frontmatter_scalars(fm)
        assert result == 'from: "Sarah Brooks <sarah.brooks@uwo.ca>"'

    def test_quotes_date_with_time(self):
        fm = 'date: 2026-03-16 22:49'
        result = _quote_frontmatter_scalars(fm)
        assert result == 'date: "2026-03-16 22:49"'

    def test_quotes_subject_with_colon(self):
        fm = 'subject: Re: Important Update'
        result = _quote_frontmatter_scalars(fm)
        assert result == 'subject: "Re: Important Update"'

    def test_does_not_double_quote_already_quoted(self):
        fm = 'from: "Already Quoted <x@y.com>"'
        assert _quote_frontmatter_scalars(fm) == fm

    def test_does_not_touch_tags_list(self):
        fm = 'tags:\n  - research\n  - policy'
        assert _quote_frontmatter_scalars(fm) == fm

    def test_does_not_touch_other_keys(self):
        fm = 'source_hash: abc123'
        assert _quote_frontmatter_scalars(fm) == fm

    def test_preserves_empty_list_value(self):
        fm = 'attachments: []'
        assert _quote_frontmatter_scalars(fm) == fm


# ── _clean_note_output ────────────────────────────────────────────────────────

_FM = (
    'date: "2026-03-16 22:49"\n'
    'from: "Sarah Brooks <sarah.brooks@uwo.ca>"\n'
    'subject: "Re: Test"\n'
    'tags:\n  - research\n'
    'attachments: []\n'
    'watch_dates: []'
)
_BODY = "## Summary\nHello world."
_FULL_NOTE = f"---\n{_FM}\n---\n\n{_BODY}"


class TestCleanNoteOutput:

    def test_valid_note_unchanged(self):
        assert _clean_note_output(_FULL_NOTE) == _FULL_NOTE

    def test_strips_full_markdown_fence(self):
        fenced = f"```markdown\n{_FULL_NOTE}\n```"
        assert _clean_note_output(fenced) == _FULL_NOTE

    def test_strips_full_yaml_fence(self):
        fenced = f"```yaml\n{_FULL_NOTE}\n```"
        assert _clean_note_output(fenced) == _FULL_NOTE

    def test_strips_frontmatter_only_fence_with_body(self):
        """Claude wraps only the frontmatter in ```yaml```, body follows after."""
        raw_fm = (
            'date: 2026-03-13 10:25\n'
            'from: Carolann Anderson <canders3@uwo.ca>\n'
            'subject: Reviewers of Final Exams\n'
            'tags:\n  - curriculum\n'
            'attachments:\n  - file.docx\n'
            'watch_dates: []'
        )
        claude_output = f"```yaml\n{raw_fm}\n```\n\n{_BODY}"
        result = _clean_note_output(claude_output)
        assert result.startswith("---\n")
        assert "---" in result[4:]         # closing ---
        assert _BODY in result             # body preserved
        assert "```" not in result         # no backticks remain

    def test_frontmatter_only_fence_adds_dashes_when_absent(self):
        raw_fm = 'date: 2026-03-13\nfrom: Alice <a@b.com>\nsubject: Test'
        claude_output = f"```yaml\n{raw_fm}\n```\n\n{_BODY}"
        result = _clean_note_output(claude_output)
        assert result.startswith("---\n")

    def test_quotes_unquoted_from_in_frontmatter(self):
        unquoted = "---\ndate: 2026-03-16\nfrom: Alice <alice@example.com>\nsubject: Hi\n---\n\n## Summary\nHello."
        result = _clean_note_output(unquoted)
        assert 'from: "Alice <alice@example.com>"' in result

    def test_quotes_unquoted_date_in_frontmatter(self):
        unquoted = "---\ndate: 2026-03-16 22:49\nfrom: Alice\nsubject: Hi\n---\n\n## Summary\nHello."
        result = _clean_note_output(unquoted)
        assert 'date: "2026-03-16 22:49"' in result

    def test_body_preserved_after_quoting(self):
        unquoted = "---\nfrom: Alice <a@b.com>\nsubject: Test\ndate: 2026-01-01\n---\n\n## Summary\nImportant content here."
        result = _clean_note_output(unquoted)
        assert "Important content here." in result
