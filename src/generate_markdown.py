"""Generates yearly Markdown and safely publishes EPUB/PDF archives.

The database is read-only during a build. Markdown and rendered files stay in
an isolated staging directory until the complete archive is ready to publish.
"""

from collections.abc import Collection
from collections.abc import Mapping
from collections.abc import Sequence
import contextlib
import datetime
import enum
import os
import pathlib
import re
import sqlite3
import tempfile
from typing import Any
import unicodedata

# Both module imports and the documented direct-script entry point are
# supported.
if __package__:
    from src import archive_manifest
    from src import render_archive
else:
    import archive_manifest  # pylint: disable=import-error
    import render_archive  # pylint: disable=import-error

METABLOCK_TEMPLATE = """\
---
title: \"Ven Anīgha Reddit Archive {year}\"
author: \"Ven Anīgha\"
date: \"{year}\"
description: \"Reddit discussions by Ven Anīgha in {year}.\"
lang: en
mainfont: "Noto Serif"
sansfont: "Noto Sans"
monofont: "Noto Sans Mono"
fontsize: 12pt
geometry: margin=1in
documentclass: book
classoption: openany
colorlinks: true
linkcolor: NavyBlue
urlcolor: NavyBlue
pdf-engine: xelatex
toc: true
toc-depth: 2
---

**Archive notes**

"""

# Preserve the existing exported template name for callers of earlier versions.
metablock_template = METABLOCK_TEMPLATE  # pylint: disable=invalid-name


def archive_coverage_note(
    cursor: sqlite3.Cursor,
    submissions: list[dict[str, Any]],
    generated_at: datetime.datetime,
) -> str:
    """Describe saved records without claiming complete Reddit coverage.

    Args:
        cursor: Open SQLite cursor for the archive snapshot. The query reads
            comment counts and UTC Unix-second timestamps without writing.
        submissions: Saved submission mappings in this volume; each must have
            an ``id`` key. Counts include their comments, including later
            replies.
        generated_at: Generation time. Pass a timezone-aware datetime for an
            unambiguous UTC timestamp; naive values use the local timezone.

    Returns:
        Markdown with generation time, record counts, the available comment
        date range, and archive-coverage limitations.

    Raises:
        sqlite3.Error: If the archive query fails.
    """
    submission_ids = [submission["id"] for submission in submissions]
    placeholders = ",".join("?" for _ in submission_ids)
    count, earliest, latest = cursor.execute(
        "SELECT COUNT(*), MIN(created_utc), MAX(created_utc) "
        f"FROM comments WHERE submission_id IN ({placeholders})",
        submission_ids,
    ).fetchone()
    generated = generated_at.astimezone(datetime.UTC).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )
    note = (
        f"**Generated:** {generated}\n\n"
        f"**Saved records in this volume:** {len(submissions)} discussions "
        f"and {count} comments.\n\n"
    )
    if count:
        dates = [
            datetime.datetime.fromtimestamp(value, datetime.UTC).strftime(
                "%Y-%m-%d"
            )
            for value in (earliest, latest)
        ]
        note += f"**Saved comment dates:** {dates[0]} to {dates[1]} (UTC).\n\n"
    note += (
        "Volumes are grouped by the year each discussion began; replies "
        "may be from later years. "
        "This edition contains the records saved in the local database at "
        "generation time. "
        "It is not a claim of complete Reddit coverage. Deleted, "
        "unavailable, or unfetched material "
        "may be absent. Comment timestamps are shown in UTC.\n\n"
    )
    return note


# Keep the per-year staging steps together for straightforward review.
# pylint: disable-next=too-many-locals
def save_comments_to_markdown(
    conn: sqlite3.Connection,
    markdown_files_dir: str | os.PathLike[str],
    temp_files_dir: str | os.PathLike[str],
    *,
    generated_at: datetime.datetime | None = None,
) -> None:
    """Saves Reddit submissions and comments in yearly Markdown files.

    Organizes submissions by year and generates separate markdown files for each
    year.
    Creates both indented and non-indented (for EPUB) markdown formats.

    Args:
        conn: An SQLite database connection.
        markdown_files_dir: The directory to save the indented markdown files.
        temp_files_dir: Destination for non-indented EPUB/PDF source Markdown.
        generated_at: Time used for the coverage note, defaulting to current
            UTC.

    Raises:
        sqlite3.Error: The submissions or comments cannot be read.
        OSError: The destination directories or files cannot be written.
    """
    markdown_dir = pathlib.Path(markdown_files_dir)
    temp_dir = pathlib.Path(temp_files_dir)
    markdown_dir.mkdir(parents=True, exist_ok=True)
    temp_dir.mkdir(parents=True, exist_ok=True)
    generated_at = generated_at or datetime.datetime.now(datetime.UTC)

    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT
            id, title, body, author, created_at, link, subreddit, updated_at
        FROM submissions
        ORDER BY created_at DESC
        """
    )
    submissions = cursor.fetchall()
    column_names = [description[0] for description in cursor.description]

    # Group submissions by year
    submissions_by_year: dict[int, list[dict[str, Any]]] = {}
    for submission in submissions:
        submission_dict = dict(zip(column_names, submission))
        # Get the year from created_at timestamp
        submission_time = datetime.datetime.fromtimestamp(
            submission_dict["created_at"], datetime.UTC
        )
        year = submission_time.year
        submissions_by_year.setdefault(year, []).append(submission_dict)

    # Process submissions year by year
    for year, submissions_in_year in submissions_by_year.items():
        coverage = archive_coverage_note(
            cursor, submissions_in_year, generated_at
        )
        markdown_text = (
            f"# Ven Anīgha Reddit Archive {year}\n\n{coverage}---\n\n"
        )
        epub_text = METABLOCK_TEMPLATE.format(year=year) + coverage
        for submission_dict in submissions_in_year:
            markdown_text += create_intended_md_from_submission(
                cursor, submission_dict
            )
            epub_text += create_non_indented_md_from_submission(
                cursor, submission_dict
            )

        markdown_file = markdown_dir / f"ven_anigha_reddit_archive_{year}.md"
        markdown_file.write_text(markdown_text, encoding="utf-8")
        epub_file = temp_dir / f"ven_anigha_reddit_archive_{year}.md"
        epub_file.write_text(
            unicodedata.normalize("NFC", epub_text), encoding="utf-8"
        )
        print(
            f"Generated markdown file with indention for {year} at: "
            f"{markdown_file}"
        )
        print(
            f"Generated markdown file for EPUB creation for {year} at: "
            f"{epub_file}"
        )


class CommentType(enum.Enum):
    """Identifies root comments, replies, and replies missing their parent."""

    # Comments directly responding to the submission.
    PARENT = 1
    # Comments replying to another comment.
    REPLY = 2
    # Comments replying to another comment whose parent is absent from the
    # database.
    ORPHAN = 3


def create_thread_dicts(
    comments: Sequence[Sequence[Any]],
) -> list[dict[str, Any]]:
    """Builds nested comment dictionaries from database rows.

    Args:
        comments: Rows ordered as id, submission_id, author, created_utc,
            parent_id, permalink, comment_body, and updated_at.

    Returns:
        A list of dictionaries, where each dictionary represents a comment
        thread,
        potentially nested with child comments under the 'children' key.
    """
    comments_by_id: dict[str, dict[str, Any]] = {}
    for comment in comments:
        comments_by_id[comment[0]] = {
            "id": comment[0],
            "submission_id": comment[1],
            "author": comment[2],
            "created_utc": comment[3],
            "parent_id": comment[4],
            "permalink": comment[5],
            "comment_body": comment[6],
            "updated_at": comment[7],
            "children": [],
            "type": None,
        }

    # Build the nested structure
    top_level_comments: list[dict[str, Any]] = []
    orphan_comments: list[dict[str, Any]] = []

    for comment in comments_by_id.values():
        parent_id = comment["parent_id"]
        # Populate children comments
        if parent_id and parent_id in comments_by_id:
            comments_by_id[parent_id]["children"].append(comment)

        # Root comments have no parents.
        if not parent_id:
            comment["type"] = CommentType.PARENT
            top_level_comments.append(comment)
        # Orphan comments have parent_ids not in the database.
        elif parent_id not in comments_by_id:
            comment["type"] = CommentType.ORPHAN
            orphan_comments.append(comment)
        else:
            # Reply comment are not added because they will be traversed by the
            # parent comment
            comment["type"] = CommentType.REPLY

    top_level_comments.sort(key=lambda comment: comment["created_utc"])
    orphan_comments.sort(key=lambda comment: comment["created_utc"])

    # Append all orphan_comments to the end so that comments with parents are
    # seen first.
    return top_level_comments + orphan_comments


def format_submission_title(title: str) -> str:
    """Escape a saved title for a single Markdown link-label heading.

    Args:
        title: Original submission title, possibly containing line breaks.

    Returns:
        A copy with whitespace collapsed and backslashes/brackets escaped. The
        saved database value is not modified.
    """
    title = " ".join(title.split())
    return title.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def create_intended_md_from_submission(
    cursor: sqlite3.Cursor, submission_dict: dict[str, Any]
) -> str:
    """Generates indented Markdown for a submission and its comments.

    The historical function name is retained for existing callers.

    Args:
        cursor: An SQLite database cursor.
        submission_dict: A dictionary containing the submission data.

    Returns:
        A string containing the markdown representation of the submission and
        its comments,
        with comments indented according to their nesting level.

    Raises:
        sqlite3.Error: The submission's comments cannot be read.
    """
    submission_time_str = datetime.datetime.fromtimestamp(
        submission_dict["created_at"], datetime.UTC
    ).strftime("%Y-%m-%d %H:%M:%S")

    submission_md = (
        f"**{submission_dict['subreddit']}** | "
        f"Posted by {submission_dict['author']} _{submission_time_str}_\n"
    )
    submission_md += (
        f"### [{format_submission_title(submission_dict['title'])}]"
        f"({submission_dict['link']})\n\n"
    )
    submission_md += f"{submission_dict['body']}\n\n"

    # Fetch and process comments for the submission
    cursor.execute(
        """
        SELECT
            id, submission_id, author, created_utc, parent_id, permalink,
            comment_body, updated_at
        FROM comments
        WHERE submission_id = ?
        ORDER BY created_utc
        """,
        (submission_dict["id"],),
    )
    comments = cursor.fetchall()

    comment_threads = create_thread_dicts(comments)
    for comment in comment_threads:
        submission_md += create_intended_md_from_thread(comment)
    return submission_md + "\n---\n\n"


def create_intended_md_from_thread(
    comment: dict[str, Any], level: int = 0
) -> str:
    """Recursively generates indented Markdown for a comment thread.

    The historical function name is retained for existing callers.

    Args:
        comment: A dictionary representing a comment thread.
        level: The nesting level of the comment (default: 0).

    Returns:
        A string containing the markdown representation of the comment and its
        replies,
        with each comment indented according to its nesting level.
    """
    indent = "    " * level
    content = "\n".join(
        f"{indent}    {line}" if line else ""
        for line in comment["comment_body"].splitlines()
    )

    parent_info = ""
    if comment["type"] == CommentType.ORPHAN:
        parent_info = " *(in reply to a comment not included)*"

    comment_time = datetime.datetime.fromtimestamp(
        comment["created_utc"], datetime.UTC
    ).strftime("%Y-%m-%d %H:%M:%S")
    comment_title = (
        f"**[{comment['author']}]({comment['permalink']})** "
        f"_{comment_time}_{parent_info}"
    )

    markdown = f"{indent}- {comment_title}:\n\n{content}\n"

    sorted_children = sorted(
        comment["children"], key=lambda comment: comment["created_utc"]
    )
    for child in sorted_children:
        markdown += create_intended_md_from_thread(child, level + 1)
    return markdown


def sanitize_markdown_content(content: str) -> str:
    """Escapes headings so Reddit content cannot alter the EPUB structure.

    Currently, it escapes markdown headers by prefixing them with a backslash.

    Args:
        content: The string content to sanitize.

    Returns:
        The sanitized string content.
    """
    # Escape markdown headers by prefixing with a backslash
    return re.sub(
        r"^(#+)",
        r"\\\1",
        unicodedata.normalize("NFC", content),
        flags=re.MULTILINE,
    )


def create_non_indented_md_from_submission(
    cursor: sqlite3.Cursor, submission_dict: dict[str, Any]
) -> str:
    """Generates heading-based Markdown for a submission and its comments.

    Uses markdown headings to represent the structure instead of indentation.

    Args:
        cursor: An SQLite database cursor.
        submission_dict: A dictionary containing the submission data.

    Returns:
        A string containing the markdown representation of the submission and
        its comments,
        without indentation and using headings for structure.

    Raises:
        sqlite3.Error: The submission's comments cannot be read.
    """
    submission_time_str = datetime.datetime.fromtimestamp(
        submission_dict["created_at"], datetime.UTC
    ).strftime("%Y-%m-%d %H:%M:%S")

    submission_md = (
        f"## [{format_submission_title(submission_dict['title'])}]"
        f"({submission_dict['link']}) {{#discussion-{submission_dict['id']}}}\n"
    )
    submission_md += (
        f"**Subreddit**: {submission_dict['subreddit']} | "
        f"**Posted by**: {submission_dict['author']} "
        f"_{submission_time_str}_\n\n"
    )
    submission_md += f"{sanitize_markdown_content(submission_dict['body'])}\n\n"

    # Fetch and process comments for the submission
    cursor.execute(
        """
        SELECT
            id, submission_id, author, created_utc, parent_id, permalink,
            comment_body, updated_at
        FROM comments
        WHERE submission_id = ?
        ORDER BY created_utc
        """,
        (submission_dict["id"],),
    )
    comments = cursor.fetchall()

    comment_threads = create_thread_dicts(comments)
    for comment in comment_threads:
        submission_md += create_non_indented_md_from_thread(comment)
    return submission_md


def create_non_indented_md_from_thread(
    comment: dict[str, Any], level: int = 0
) -> str:
    """Recursively generates heading-based Markdown for a comment thread.

    Uses markdown headings to represent the comment hierarchy.

    Args:
        comment: A dictionary representing a comment thread.
        level: The nesting level of the comment (default: 0).

    Returns:
        A string containing the markdown representation of the comment and its
        replies, without indentation and using headings for structure.
    """
    # Use level to determine heading level (e.g., ###, ####)
    heading_prefix = "#" * min(
        level + 3, 6
    )  # At most six Markdown heading levels.
    comment_time = datetime.datetime.fromtimestamp(
        comment["created_utc"], datetime.UTC
    ).strftime("%Y-%m-%d %H:%M:%S")

    parent_info = ""
    if comment["type"] == CommentType.ORPHAN:
        parent_info = " *(in reply to a comment not included)*"
    comment_title = (
        f"{heading_prefix} Comment by "
        f"[{comment['author']}]({comment['permalink']}) "
        f"on {comment_time}{parent_info} "
        f"{{#comment-{comment['id']} .unnumbered .unlisted}}"
    )

    markdown = (
        f"{comment_title}\n\n"
        f"{sanitize_markdown_content(comment['comment_body'])}\n\n"
    )

    # Sort replies from oldest to newest.
    sorted_children = sorted(
        comment["children"], key=lambda comment: comment["created_utc"]
    )
    for child in sorted_children:
        markdown += create_non_indented_md_from_thread(child, level + 1)
    return markdown


def convert_to_epub_and_pdf(
    input_dir: str | os.PathLike[str],
    epub_dir: str | os.PathLike[str],
    pdf_dir: str | os.PathLike[str],
    *,
    expected_records: Mapping[str, Collection[str]],
) -> None:
    """Builds the complete EPUB/PDF set and publishes validated outputs.

    Args:
        input_dir: Directory containing generated yearly Markdown sources.
        epub_dir: Destination directory for EPUB files.
        pdf_dir: Destination directory for PDF files.
        expected_records: Volume stems mapped to database-derived record
            anchors. Use the same snapshot as the generated Markdown.

    Raises:
        ValueError: A record inventory, font, glyph, or archive file is invalid,
            or no Markdown exists.
        OSError: An input, staging directory, or published file is inaccessible.
        RuntimeError: A renderer is missing or publication recovery fails.
        subprocess.TimeoutExpired: A renderer exceeds its time limit.
        subprocess.CalledProcessError: A renderer exits unsuccessfully.
        zipfile.BadZipFile: An EPUB is not a readable ZIP archive.
        KeyError: An EPUB lacks its required mimetype entry.
        xml.etree.ElementTree.ParseError: Rendered EPUB XHTML is malformed.
    """
    render_archive.convert_archive(
        input_dir,
        epub_dir,
        pdf_dir,
        METABLOCK_TEMPLATE,
        expected_records=expected_records,
    )


def build_archive(
    database: str | os.PathLike[str] = "reddit_comments.db",
    output_dir: str | os.PathLike[str] = ".",
) -> None:
    """Reads the database and stages every archive format before publication.

    Args:
        database: Existing SQLite archive database, opened read-only.
        output_dir: Root containing markdown_files, epub_files, and pdf_files.

    Raises:
        sqlite3.Error: The database cannot be opened read-only or queried.
        ValueError: A record inventory, font, glyph, or archive file is invalid,
            or no Markdown exists.
        OSError: A staging directory or published file is inaccessible.
        RuntimeError: A renderer is missing or publication recovery fails.
        subprocess.TimeoutExpired: A renderer exceeds its time limit.
        subprocess.CalledProcessError: A renderer exits unsuccessfully.
        zipfile.BadZipFile: An EPUB is not a readable ZIP archive.
        KeyError: An EPUB lacks its required mimetype entry.
        xml.etree.ElementTree.ParseError: Rendered EPUB XHTML is malformed.
    """
    root = pathlib.Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".archive-build-", dir=root
    ) as temporary_directory:
        stage = pathlib.Path(temporary_directory)
        markdown_dir = stage / "markdown_files"
        markdown_dir.mkdir()
        source_dir = stage / "source"
        epub_dir = stage / "epub_files"
        pdf_dir = stage / "pdf_files"
        uri = pathlib.Path(database).resolve().as_uri() + "?mode=ro"
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as connection:
            # Inventory and rendered text must use the same SQLite snapshot.
            connection.execute("BEGIN")
            expected_records = archive_manifest.record_manifest(connection)
            save_comments_to_markdown(connection, markdown_dir, source_dir)
        convert_to_epub_and_pdf(
            source_dir, epub_dir, pdf_dir, expected_records=expected_records
        )
        pairs = []
        for directory in (markdown_dir, epub_dir, pdf_dir):
            for file in sorted(directory.iterdir()):
                pairs.append((file, root / directory.name / file.name))
        render_archive.publish_files(pairs)


if __name__ == "__main__":
    build_archive()
