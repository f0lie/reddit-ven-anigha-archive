"""Regression checks for navigation, provenance, and lossless Unicode rendering."""

import datetime
import shutil
import subprocess
import unicodedata
import zipfile
from contextlib import closing
from pathlib import Path

import pytest

from src.generate_markdown import (
    CommentType,
    archive_coverage_note,
    create_intended_md_from_submission,
    create_non_indented_md_from_submission,
    create_non_indented_md_from_thread,
    format_submission_title,
    metablock_template,
    sanitize_markdown_content,
    save_comments_to_markdown,
)
from tests.test_generate_markdown import setup_in_memory_db


def sample_records():
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


def test_generated_at_and_saved_record_coverage_are_in_both_sources(tmp_path):
    submission, comment = sample_records()
    generated = datetime.datetime(2026, 10, 12, 6, 30, tzinfo=datetime.UTC)
    with closing(setup_in_memory_db([submission], [comment])) as connection:
        save_comments_to_markdown(connection, tmp_path / "markdown", tmp_path / "book", generated_at=generated)
    filename = "ven_anigha_reddit_archive_2026.md"
    raw = (tmp_path / "markdown" / filename).read_text()
    rendered = (tmp_path / "book" / filename).read_text()
    for content in (raw, rendered):
        assert "**Generated:** 2026-10-12 06:30:00 UTC" in content
        assert "1 discussions and 1 comments" in content
        assert "2026-01-02 to 2026-01-02 (UTC)" in content
        assert "not a claim of complete Reddit coverage" in content
        assert "replies may be from later years" in content
        assert "🙏 非梵行 →" in content
    assert "m\u0307" in raw  # The source Markdown retains the exact saved source.
    assert "ṁ" in rendered  # NFC avoids needless combining-glyph fallback in PDFs.


def test_empty_comment_coverage_does_not_invent_dates():
    submission, _ = sample_records()
    generated = datetime.datetime(2026, 10, 12, tzinfo=datetime.UTC)
    with closing(setup_in_memory_db([submission], [])) as connection:
        note = archive_coverage_note(connection.cursor(), [submission], generated)
    assert "0 comments" in note
    assert "Saved comment dates" not in note


def test_unicode_content_is_never_stripped():
    original = "Pāli m\u0307 🙏 🤔 非梵行 😅 🏿 😂 👌 👍 →"
    assert sanitize_markdown_content(original) == unicodedata.normalize("NFC", original)


def test_comment_anchor_is_stable_and_not_a_toc_entry():
    _, comment = sample_records()
    comment.update(children=[], type=CommentType.PARENT)
    for depth in (0, 1, 3, 8, 50):
        rendered = create_non_indented_md_from_thread(comment, level=depth)
        assert "{#comment-def456 .unnumbered .unlisted}" in rendered
        assert rendered.startswith("#" * min(depth + 3, 6) + " ")
        assert not rendered.startswith("#######")
        assert "🙏 非梵行" in rendered


def test_metadata_and_running_header_use_reproducible_fonts():
    metadata = metablock_template.format(year=2026)
    assert 'mainfont: "Noto Serif"' in metadata
    assert 'sansfont: "Noto Sans"' in metadata
    assert 'monofont: "Noto Sans Mono"' in metadata
    assert "colorlinks: true" in metadata
    header = Path("src/book_header.tex").read_text()
    assert "Noto Serif CJK SC" in header
    assert "Symbola" in header
    assert r"\thepage" in header
    assert r"\leftmark" not in header and r"\rightmark" not in header
    assert "Ven Anīgha Reddit Archive" in header


@pytest.mark.skipif(not shutil.which("pandoc"), reason="Pandoc is needed for EPUB navigation integration")
def test_epub_toc_lists_discussions_only_and_keeps_comment_anchors(tmp_path):
    submission, comment = sample_records()
    with closing(setup_in_memory_db([submission], [comment])) as connection:
        save_comments_to_markdown(connection, tmp_path / "markdown", tmp_path / "book")
    output = tmp_path / "book.epub"
    source = tmp_path / "book" / "ven_anigha_reddit_archive_2026.md"
    subprocess.run(
        ["pandoc", str(source), "--toc", "--toc-depth=2", "--split-level=2", "-o", str(output)],
        check=True,
        capture_output=True,
        text=True,
    )
    with zipfile.ZipFile(output) as archive:
        nav = archive.read("EPUB/nav.xhtml").decode()
        chapters = [
            archive.read(name).decode()
            for name in archive.namelist()
            if name.startswith("EPUB/text/") and name.endswith(".xhtml")
        ]
    assert "A discussion" in nav
    assert "Comment by" not in nav
    assert "Archive notes" not in nav
    assert any('id="comment-def456"' in chapter for chapter in chapters)
    assert any('id="discussion-abc123"' in chapter for chapter in chapters)
    assert any("🙏" in chapter and "非梵行" in chapter for chapter in chapters)


def test_fallback_characters_are_locally_grouped():
    """Global inter-character group transitions break at quote/emphasis boundaries."""
    header = Path("src/book_header.tex").read_text()
    assert r"\usepackage{newunicodechar}" in header
    assert "ucharclasses" not in header
    assert r"\setTransitionsFor" not in header
    for char in "》非梵行→🏿👌👍😂😅🙏🤔":
        font = "archiveCJK" if char in "》非梵行" else "archiveSymbols"
        declaration = "\\newunicodechar{" + char + "}{{\\" + font + '\\char"' + format(ord(char), "X") + "}}"
        assert declaration in header


def test_code_blocks_wrap_without_discarding_text():
    header = Path("src/book_header.tex").read_text()
    assert r"\usepackage{fvextra}" in header
    assert r"\RecustomVerbatimEnvironment{verbatim}{Verbatim}" in header
    assert "breaklines=true,breakanywhere=true" in header


def test_multiline_title_keeps_its_literal_link_and_stable_anchor():
    submission, comment = sample_records()
    submission["title"] = "  A question\nabout [moderation] and secular living.\n"
    with closing(setup_in_memory_db([submission], [comment])) as connection:
        for renderer in (create_intended_md_from_submission, create_non_indented_md_from_submission):
            rendered = renderer(connection.cursor(), submission)
            assert "[A question about \\[moderation\\] and secular living.]" in rendered
            assert "A question\n" not in rendered
        book = create_non_indented_md_from_submission(connection.cursor(), submission)
        assert "{#discussion-abc123}\n" in book
        assert book.splitlines()[0].endswith("{#discussion-abc123}")
    assert submission["title"].endswith("\n")  # No mutation of stored source data.


def test_title_backslashes_are_literal():
    assert format_submission_title(r"See \ this [example]") == r"See \\ this \[example\]"
