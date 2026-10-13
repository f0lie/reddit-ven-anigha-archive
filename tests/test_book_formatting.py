"""Check navigation, provenance, and lossless Unicode rendering."""

import contextlib
import datetime
import pathlib
import shutil
import subprocess
from typing import Any
import unicodedata
import zipfile

import pytest

from src import generate_markdown
from tests import test_generate_markdown


def sample_records() -> tuple[dict[str, Any], dict[str, Any]]:
    """Build fresh related records with Unicode text and fixed UTC dates.

    Returns:
        A tuple of independently mutable (submission, comment) dictionaries
        accepted by setup_in_memory_db. The submission is dated January 1, 2026,
        and the comment January 2. Timestamp fields contain Unix seconds.
    """
    timestamp = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC).timestamp()
    submission = {
        "id": "abc123",
        "title": "A discussion",
        "body": "Question 非梵行",
        "author": "Reader",
        "created_at": timestamp,
        "link": "https://www.reddit.com/comments/abc123/",
        "subreddit": "HillsideHermitage",
        "updated_at": timestamp,
    }
    comment = {
        "id": "def456",
        "submission_id": "abc123",
        "author": "Bhikkhu_Anigha",
        "created_utc": timestamp + 86400,
        "parent_id": None,
        "permalink": "https://www.reddit.com/comments/abc123/comment/def456/",
        "comment_body": "Saved response: m\u0307 🙏 非梵行 →",
        "updated_at": timestamp,
    }
    return submission, comment


def test_generated_at_and_saved_record_coverage_are_in_both_sources(
    tmp_path: pathlib.Path,
) -> None:
    """Include UTC provenance in both outputs and normalize only book text."""
    submission, comment = sample_records()
    generated = datetime.datetime(2026, 10, 12, 6, 30, tzinfo=datetime.UTC)
    with contextlib.closing(
        test_generate_markdown.setup_in_memory_db([submission], [comment])
    ) as connection:
        generate_markdown.save_comments_to_markdown(
            connection,
            tmp_path / "markdown",
            tmp_path / "book",
            generated_at=generated,
        )
    filename = "ven_anigha_reddit_archive_2026.md"
    raw = (tmp_path / "markdown" / filename).read_text(encoding="utf-8")
    rendered = (tmp_path / "book" / filename).read_text(encoding="utf-8")
    for content in (raw, rendered):
        assert "**Generated:** 2026-10-12 06:30:00 UTC" in content
        assert "1 discussions and 1 comments" in content
        assert "2026-01-02 to 2026-01-02 (UTC)" in content
        assert "not a claim of complete Reddit coverage" in content
        assert "replies may be from later years" in content
        assert "🙏 非梵行 →" in content
    # Source Markdown retains the saved text; books avoid combining-glyph gaps.
    assert "m\u0307" in raw
    assert "ṁ" in rendered


def test_empty_comment_coverage_does_not_invent_dates() -> None:
    """Omit a comment-date range when no saved comments exist."""
    submission, _ = sample_records()
    generated = datetime.datetime(2026, 10, 12, tzinfo=datetime.UTC)
    with contextlib.closing(
        test_generate_markdown.setup_in_memory_db([submission], [])
    ) as connection:
        note = generate_markdown.archive_coverage_note(
            connection.cursor(), [submission], generated
        )
    assert "0 comments" in note
    assert "Saved comment dates" not in note


def test_unicode_content_is_never_stripped() -> None:
    """Normalize Unicode to NFC without dropping emoji or non-Latin content."""
    original = "Pāli m\u0307 🙏 🤔 非梵行 😅 🏿 😂 👌 👍 →"
    assert generate_markdown.sanitize_markdown_content(
        original
    ) == unicodedata.normalize("NFC", original)


def test_comment_anchor_is_stable_and_not_a_toc_entry() -> None:
    """Keep stable, unlisted comment IDs even beyond supported heading depth."""
    _, comment = sample_records()
    comment.update(children=[], type=generate_markdown.CommentType.PARENT)
    for depth in (0, 1, 3, 8, 50):
        rendered = generate_markdown.create_non_indented_md_from_thread(
            comment, level=depth
        )
        assert "{#comment-def456 .unnumbered .unlisted}" in rendered
        assert rendered.startswith("#" * min(depth + 3, 6) + " ")
        assert not rendered.startswith("#######")
        assert "🙏 非梵行" in rendered


def test_metadata_and_running_header_use_reproducible_fonts() -> None:
    """Pin font families and avoid source-controlled text in running headers."""
    metadata = generate_markdown.metablock_template.format(year=2026)
    assert 'mainfont: "Noto Serif"' in metadata
    assert 'sansfont: "Noto Sans"' in metadata
    assert 'monofont: "Noto Sans Mono"' in metadata
    assert "colorlinks: true" in metadata
    header = pathlib.Path("src/book_header.tex").read_text(encoding="utf-8")
    assert "Noto Serif CJK SC" in header
    assert "Symbola" in header
    assert r"\thepage" in header
    assert r"\leftmark" not in header and r"\rightmark" not in header
    assert "Ven Anīgha Reddit Archive" in header


@pytest.mark.skipif(
    not shutil.which("pandoc"),
    reason="Pandoc is needed for EPUB navigation integration",
)
def test_epub_toc_lists_discussions_only_and_keeps_comment_anchors(
    tmp_path: pathlib.Path,
) -> None:
    """Keep comments linkable and limit EPUB navigation to discussions."""
    submission, comment = sample_records()
    with contextlib.closing(
        test_generate_markdown.setup_in_memory_db([submission], [comment])
    ) as connection:
        generate_markdown.save_comments_to_markdown(
            connection, tmp_path / "markdown", tmp_path / "book"
        )
    output = tmp_path / "book.epub"
    source = tmp_path / "book" / "ven_anigha_reddit_archive_2026.md"
    subprocess.run(
        [
            "pandoc",
            str(source),
            "--toc",
            "--toc-depth=2",
            "--split-level=2",
            "-o",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    with zipfile.ZipFile(output) as archive:
        navigation = archive.read("EPUB/nav.xhtml").decode()
        chapters = [
            archive.read(name).decode()
            for name in archive.namelist()
            if name.startswith("EPUB/text/") and name.endswith(".xhtml")
        ]
    assert "A discussion" in navigation
    assert "Comment by" not in navigation
    assert "Archive notes" not in navigation
    assert any('id="comment-def456"' in chapter for chapter in chapters)
    assert any('id="discussion-abc123"' in chapter for chapter in chapters)
    assert any("🙏" in chapter and "非梵行" in chapter for chapter in chapters)


def test_code_blocks_wrap_without_discarding_text() -> None:
    """Enable verbatim line wrapping without modifying archived code."""
    header = pathlib.Path("src/book_header.tex").read_text(encoding="utf-8")
    assert r"\usepackage{fvextra}" in header
    assert r"\RecustomVerbatimEnvironment{verbatim}{Verbatim}" in header
    assert "breaklines=true,breakanywhere=true" in header


def test_multiline_title_keeps_its_literal_link_and_stable_anchor() -> None:
    """Collapse whitespace and escape links without mutating saved titles."""
    submission, comment = sample_records()
    submission["title"] = (
        "  A question\nabout [moderation] and secular living.\n"
    )
    with contextlib.closing(
        test_generate_markdown.setup_in_memory_db([submission], [comment])
    ) as connection:
        for renderer in (
            generate_markdown.create_intended_md_from_submission,
            generate_markdown.create_non_indented_md_from_submission,
        ):
            rendered = renderer(connection.cursor(), submission)
            assert (
                "[A question about \\[moderation\\] and secular living.]"
                in rendered
            )
            assert "A question\n" not in rendered
        book = generate_markdown.create_non_indented_md_from_submission(
            connection.cursor(), submission
        )
        assert "{#discussion-abc123}\n" in book
        assert book.splitlines()[0].endswith("{#discussion-abc123}")
    # No mutation of stored source data.
    assert submission["title"].endswith("\n")


def test_title_backslashes_are_literal() -> None:
    """Escape backslashes and brackets before placing titles in links."""
    assert (
        generate_markdown.format_submission_title(r"See \ this [example]")
        == r"See \\ this \[example\]"
    )
