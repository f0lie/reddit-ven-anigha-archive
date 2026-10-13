"""Real Pandoc regressions for Markdown crossing saved-record boundaries."""

import contextlib
import datetime
import json
import pathlib
import shutil
import sqlite3
from typing import Any
import zipfile

import pytest

from src import archive_manifest
from src import generate_markdown
from src import render_archive
from tests import test_book_formatting
from tests import test_generate_markdown

PANDOC = pytest.mark.skipif(
    not shutil.which("pandoc"),
    reason="Pandoc is needed for record coverage integration",
)
VOLUME = "ven_anigha_reddit_archive_2026"


def save_database(
    path: pathlib.Path,
    submissions: list[dict[str, Any]],
    comments: list[dict[str, Any]],
) -> None:
    """Copy fixture records into a disk database and close both connections.

    Args:
        path: Destination SQLite path; an existing database is overwritten by
            the backup operation.
        submissions: Submission dictionaries accepted by setup_in_memory_db.
        comments: Comment dictionaries accepted by setup_in_memory_db.

    Raises:
        sqlite3.Error: Fixture insertion, opening the target, or backup fails.
    """
    with (
        contextlib.closing(
            test_generate_markdown.setup_in_memory_db(submissions, comments)
        ) as source,
        contextlib.closing(sqlite3.connect(path)) as target,
    ):
        source.backup(target)


def test_inventory_uses_discussion_utc_year_and_keeps_all_comments() -> None:
    """Keep later replies and missing parents in the discussion year."""
    submission, comment = test_book_formatting.sample_records()
    comment["created_utc"] = datetime.datetime(
        2027, 1, 1, tzinfo=datetime.UTC
    ).timestamp()
    comment["parent_id"] = "missing"
    with contextlib.closing(
        test_generate_markdown.setup_in_memory_db([submission], [comment])
    ) as connection:
        assert archive_manifest.record_manifest(connection) == {
            VOLUME: ("discussion-abc123", "comment-def456")
        }


def test_inventory_rejects_comment_without_saved_discussion() -> None:
    """Reject comments with no saved discussion to determine their volume."""
    _, comment = test_book_formatting.sample_records()
    with (
        contextlib.closing(
            test_generate_markdown.setup_in_memory_db([], [comment])
        ) as connection,
        pytest.raises(ValueError, match="has no saved discussion"),
    ):
        archive_manifest.record_manifest(connection)


@pytest.mark.parametrize("record_id", ["", "abc} evil", "abc\n## forged", None])
def test_inventory_rejects_ids_that_cannot_be_generated_anchors(
    record_id: str | None,
) -> None:
    """Reject empty, non-string, and markup-bearing IDs before serialization."""
    submission, _ = test_book_formatting.sample_records()
    submission["id"] = record_id
    with (
        contextlib.closing(
            test_generate_markdown.setup_in_memory_db([submission], [])
        ) as connection,
        pytest.raises(ValueError, match="Invalid discussion record ID"),
    ):
        archive_manifest.record_manifest(connection)


@pytest.mark.parametrize(
    ("expected", "actual", "message"),
    [
        (["comment-a"], [], "missing anchors: comment-a"),
        (
            ["comment-a"],
            ["comment-a", "comment-a"],
            "duplicate anchors: comment-a",
        ),
        (
            ["comment-a", "comment-a"],
            ["comment-a"],
            "duplicate expected anchors: comment-a",
        ),
        (
            ["comment-a"],
            ["comment-a", "discussion-b"],
            "unexpected anchors: discussion-b",
        ),
    ],
)
def test_record_coverage_rejects_missing_duplicate_or_invented_anchors(
    expected: list[str], actual: list[str], message: str
) -> None:
    """Check diagnostics for each kind of record-inventory mismatch."""
    with pytest.raises(ValueError, match=message):
        archive_manifest.validate_record_anchors(
            expected, actual, "test volume"
        )


@PANDOC
@pytest.mark.parametrize("boundary", ["discussion", "comment"])
def test_fences_in_two_records_fail_closed_and_preserve_published_archive(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    """Preserve stored records and published files when fences hide a record."""
    submission, comment = test_book_formatting.sample_records()
    if boundary == "discussion":
        second = dict(submission, id="second", body="```\nSecond body")
        submission["created_at"] += 1
        submission["body"] = "```text\nFirst body with an unmatched fence"
        submissions, comments = [submission, second], [comment]
        missing = "discussion-second"
    else:
        comment["comment_body"] = (
            "```text\nFirst comment with an unmatched fence"
        )
        second = dict(
            comment,
            id="second",
            created_utc=comment["created_utc"] + 1,
            comment_body="```\nSecond body",
        )
        submissions, comments = [submission], [comment, second]
        missing = "comment-second"
    database = tmp_path / "archive.db"
    save_database(database, submissions, comments)
    original_database = database.read_bytes()
    old_outputs = [
        tmp_path / directory / f"{VOLUME}.{extension}"
        for directory, extension in (
            ("markdown_files", "md"),
            ("epub_files", "epub"),
            ("pdf_files", "pdf"),
        )
    ]
    for target in old_outputs:
        target.parent.mkdir()
        target.write_bytes(b"last known good output")
    real_run = render_archive.run_pandoc

    def parse_only(args: list[str], *rest: object, **kwargs: object) -> str:
        """Forward parsing calls while forbidding premature output rendering.

        Args:
            args: Pandoc flags, which must not contain an output-file argument.
            *rest: Remaining positional arguments forwarded to the real runner.
            **kwargs: Keyword arguments forwarded to the real runner.

        Returns:
            The real runner's decoded stdout. Runner exceptions propagate.

        Raises:
            AssertionError: A call attempts to render an output file.
        """
        assert "-o" not in args, (
            "No output should render after missing record headings"
        )
        return real_run(args, *rest, **kwargs)

    monkeypatch.setattr(render_archive, "run_pandoc", parse_only)
    with pytest.raises(
        ValueError, match=f"Record coverage failed.*missing anchors:.*{missing}"
    ):
        generate_markdown.build_archive(database, tmp_path)
    assert database.read_bytes() == original_database
    assert all(
        path.read_bytes() == b"last known good output" for path in old_outputs
    )
    assert not list(tmp_path.glob(".archive-build-*"))


@PANDOC
def test_ordinary_records_keep_exact_coverage_in_restricted_ast_and_epub(
    tmp_path: pathlib.Path,
) -> None:
    """Preserve every discussion and reply anchor through Pandoc conversion."""
    submission, comment = test_book_formatting.sample_records()
    submission["body"] += "\n\n```text\nA complete code block\n```"
    reply = dict(
        comment,
        id="reply",
        parent_id=comment["id"],
        comment_body="A nested response",
    )
    with contextlib.closing(
        test_generate_markdown.setup_in_memory_db(
            [submission], [comment, reply]
        )
    ) as connection:
        manifest = archive_manifest.record_manifest(connection)
        generate_markdown.save_comments_to_markdown(
            connection, tmp_path / "markdown", tmp_path / "book"
        )
    source = tmp_path / "book" / f"{VOLUME}.md"
    document = render_archive.prepare_document(
        source, generate_markdown.metablock_template, tmp_path, manifest[VOLUME]
    )
    render_archive.validate_document_records(
        json.loads(document), manifest[VOLUME], "ordinary fixture"
    )
    output = tmp_path / "book.epub"
    render_archive.run_pandoc(
        [
            "--sandbox",
            "-f",
            "json",
            "--toc",
            "--toc-depth=2",
            "--split-level=2",
            "-o",
            str(output),
        ],
        tmp_path,
        input_text=document,
    )
    render_archive.validate_output(output)
    render_archive.validate_epub_records(output, manifest[VOLUME])


@PANDOC
def test_duplicate_header_id_in_reddit_body_is_rejected(
    tmp_path: pathlib.Path,
) -> None:
    """Reject forged Setext heading IDs that collide with generated anchors."""
    submission, comment = test_book_formatting.sample_records()
    # Setext headings bypass the old ATX-only content escaping.
    submission["body"] = (
        "Forged heading {#comment-def456}\n"
        "----------------------------------------"
    )
    with contextlib.closing(
        test_generate_markdown.setup_in_memory_db([submission], [comment])
    ) as connection:
        manifest = archive_manifest.record_manifest(connection)
        generate_markdown.save_comments_to_markdown(
            connection, tmp_path / "markdown", tmp_path / "book"
        )
    with pytest.raises(ValueError, match="duplicate anchors: comment-def456"):
        render_archive.prepare_document(
            tmp_path / "book" / f"{VOLUME}.md",
            generate_markdown.metablock_template,
            tmp_path,
            manifest[VOLUME],
        )


def test_record_heading_absorbed_into_a_quote_is_rejected() -> None:
    """Require record headings at the top level even if nested IDs survive."""
    header = {"t": "Header", "c": [3, ["comment-a", [], []], []]}
    document = {"blocks": [{"t": "BlockQuote", "c": [header]}]}
    with pytest.raises(
        ValueError, match="top-level headings.*missing anchors: comment-a"
    ):
        render_archive.validate_document_records(
            document, ["comment-a"], "fixture"
        )


@pytest.mark.parametrize("ids", [[], ["comment-a", "comment-a"]])
def test_epub_actual_destinations_reject_missing_or_duplicate_ids(
    tmp_path: pathlib.Path, ids: list[str]
) -> None:
    """Check XHTML destinations independently of EPUB navigation entries."""
    output = tmp_path / "book.epub"
    with zipfile.ZipFile(output, "w") as archive:
        for index, identifier in enumerate(ids):
            archive.writestr(
                f"EPUB/text/ch{index}.xhtml",
                f'<html><body><h3 id="{identifier}">Text</h3></body></html>',
            )
    with pytest.raises(ValueError, match="Record coverage failed"):
        render_archive.validate_epub_records(output, ["comment-a"])


@pytest.mark.parametrize(
    "manifest", [{}, {"ven_anigha_reddit_archive_2025": ("discussion-a",)}]
)
def test_missing_or_unexpected_volume_blocks_conversion(
    tmp_path: pathlib.Path, manifest: dict[str, tuple[str, ...]]
) -> None:
    """Reject source-volume sets that disagree with the database manifest."""
    source = tmp_path / f"{VOLUME}.md"
    source.write_text("---\ntitle: Archive\n---\nBody")
    with pytest.raises(ValueError, match="Archive volume coverage failed"):
        render_archive.convert_archive(
            tmp_path,
            tmp_path / "epub",
            tmp_path / "pdf",
            "",
            expected_records=manifest,
        )


def test_conversion_requires_independent_database_inventory(
    tmp_path: pathlib.Path,
) -> None:
    """Prevent conversion without an explicit independent record inventory."""
    with pytest.raises(TypeError, match="expected_records"):
        # Omission deliberately verifies the required inventory safety boundary.
        # pylint: disable-next=missing-kwoa
        render_archive.convert_archive(
            tmp_path, tmp_path / "epub", tmp_path / "pdf", ""
        )


def test_epub_coverage_failure_blocks_publication_and_pdf_rendering(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop PDF conversion and publication when the EPUB loses a record."""
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / f"{VOLUME}.md").write_text("generated source")
    targets = []
    for suffix in ("epub", "pdf"):
        path = tmp_path / suffix / f"{VOLUME}.{suffix}"
        path.parent.mkdir()
        path.write_bytes(b"previous output")
        targets.append(path)
    monkeypatch.setattr(render_archive, "prepare_document", lambda *args: "{}")

    def incomplete_epub(
        args: list[str], *rest: object, **kwargs: object
    ) -> None:
        """Write a valid EPUB shell lacking the required discussion anchor.

        Args:
            args: Pandoc flags containing an EPUB output-file argument.
            *rest: Unused positional arguments accepted by the runner interface.
            **kwargs: Unused keywords accepted by the runner interface.

        Raises:
            AssertionError: Rendering reaches PDF before coverage validation.
            OSError: The output archive cannot be written.
        """
        del rest, kwargs  # Unused runner arguments.
        output = pathlib.Path(args[args.index("-o") + 1])
        assert output.suffix == ".epub", (
            "PDF rendering must wait for EPUB coverage validation"
        )
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("mimetype", "application/epub+zip")
            archive.writestr("META-INF/container.xml", "<container/>")
            archive.writestr(
                "EPUB/text/ch01.xhtml", "<html><body>Lost record</body></html>"
            )

    monkeypatch.setattr(render_archive, "run_pandoc", incomplete_epub)
    with pytest.raises(ValueError, match="missing anchors: discussion-a"):
        render_archive.convert_archive(
            inputs,
            targets[0].parent,
            targets[1].parent,
            "",
            expected_records={VOLUME: ("discussion-a",)},
        )
    assert all(path.read_bytes() == b"previous output" for path in targets)
