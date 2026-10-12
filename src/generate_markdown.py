import datetime
import re
import sqlite3
import unicodedata
from enum import Enum
from pathlib import Path
from typing import Any

metablock_template = """\
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


def archive_coverage_note(
    cursor: sqlite3.Cursor,
    submissions: list[dict[str, Any]],
    generated_at: datetime.datetime,
) -> str:
    """Describe the saved records included, without implying a complete Reddit history."""
    submission_ids = [submission["id"] for submission in submissions]
    placeholders = ",".join("?" for _ in submission_ids)
    count, earliest, latest = cursor.execute(
        f"SELECT COUNT(*), MIN(created_utc), MAX(created_utc) FROM comments WHERE submission_id IN ({placeholders})",
        submission_ids,
    ).fetchone()
    generated = generated_at.astimezone(datetime.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    note = (
        f"**Generated:** {generated}\n\n"
        f"**Saved records in this volume:** {len(submissions)} discussions and {count} comments.\n\n"
    )
    if count:
        dates = [
            datetime.datetime.fromtimestamp(value, datetime.UTC).strftime("%Y-%m-%d") for value in (earliest, latest)
        ]
        note += f"**Saved comment dates:** {dates[0]} to {dates[1]} (UTC).\n\n"
    note += (
        "Volumes are grouped by the year each discussion began; replies may be from later years. "
        "This edition contains the records saved in the local database at generation time. "
        "It is not a claim of complete Reddit coverage. Deleted, unavailable, or unfetched material "
        "may be absent. Comment timestamps are shown in UTC.\n\n"
    )
    return note


def save_comments_to_markdown(
    conn: sqlite3.Connection,
    markdown_files_dir,
    temp_files_dir,
    *,
    generated_at: datetime.datetime | None = None,
) -> None:
    """
    Saves Reddit submissions and comments from a SQLite database to markdown files.

    Organizes submissions by year and generates separate markdown files for each year.
    Creates both indented and non-indented (for EPUB) markdown formats.

    Args:
        conn: An SQLite database connection.
        markdown_files_dir: The directory to save the indented markdown files.
        temp_files_dir: The directory to save the non-indented markdown files used for EPUB generation.
    """
    markdown_dir = Path(markdown_files_dir)
    temp_dir = Path(temp_files_dir)
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
    submissions_by_year = {}
    for submission in submissions:
        submission_dict = dict(zip(column_names, submission))
        # Get the year from created_at timestamp
        submission_time = datetime.datetime.fromtimestamp(submission_dict["created_at"], datetime.UTC)
        year = submission_time.year
        submissions_by_year.setdefault(year, []).append(submission_dict)

    # Process submissions year by year
    for year, submissions_in_year in submissions_by_year.items():
        coverage = archive_coverage_note(cursor, submissions_in_year, generated_at)
        markdown_text = f"# Ven Anīgha Reddit Archive {year}\n\n{coverage}---\n\n"
        epub_text = metablock_template.format(year=year) + coverage
        for submission_dict in submissions_in_year:
            markdown_text += create_intended_md_from_submission(cursor, submission_dict)
            epub_text += create_non_indented_md_from_submission(cursor, submission_dict)

        markdown_file = markdown_dir / f"ven_anigha_reddit_archive_{year}.md"
        markdown_file.write_text(markdown_text, encoding="utf-8")
        epub_file = temp_dir / f"ven_anigha_reddit_archive_{year}.md"
        epub_file.write_text(unicodedata.normalize("NFC", epub_text), encoding="utf-8")
        print(f"Generated markdown file with indention for {year} at: {markdown_file}")
        print(f"Generated markdown file for EPUB creation for {year} at: {epub_file}")


class CommentType(Enum):
    """
    Enum representing the type of a comment within a thread.
    """

    # Comments directly reponding to the submission.
    PARENT = 1
    # Comments replying to another comment.
    REPLY = 2
    # Comments replying to another comment but does not appear in the database.
    ORPHAN = 3


def create_thread_dicts(comments: list[list[Any]]) -> list[dict[str, Any]]:
    """
    Converts a list of comment tuples into a list of nested dictionaries representing the thread structure.

    Args:
        comments: A list of tuples, each representing a comment with its attributes.

    Returns:
        A list of dictionaries, where each dictionary represents a comment thread,
        potentially nested with child comments under the 'children' key.
    """
    comments_dict = {}
    for comment in comments:
        comments_dict[comment[0]] = {
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
    top_level_comments = []
    orphan_comments = []

    for comment in comments_dict.values():
        parent_id = comment["parent_id"]
        # Populate children comments
        if parent_id and parent_id in comments_dict:
            comments_dict[parent_id]["children"].append(comment)

        # Root comments have no parents.
        if not parent_id:
            comment["type"] = CommentType.PARENT
            top_level_comments.append(comment)
        # Orphan comments have parent_ids not in the database.
        elif parent_id not in comments_dict:
            comment["type"] = CommentType.ORPHAN
            orphan_comments.append(comment)
        else:
            # Reply comment are not added because they will be traversed by the parent comment
            comment["type"] = CommentType.REPLY

    top_level_comments.sort(key=lambda x: x["created_utc"])
    orphan_comments.sort(key=lambda x: x["created_utc"])

    # Append all orphan_comments to the end so that comments with parents are seen first.
    return top_level_comments + orphan_comments


def format_submission_title(title: str) -> str:
    """Keep a saved title on one heading line and escape link-label delimiters."""
    title = " ".join(title.split())
    return title.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def create_intended_md_from_submission(cursor: sqlite3.Cursor, submission_dict: dict[str, str | float]) -> str:
    """
    Generates indented markdown text for a given Reddit submission and its comments.

    Args:
        cursor: An SQLite database cursor.
        submission_dict: A dictionary containing the submission data.

    Returns:
        A string containing the markdown representation of the submission and its comments,
        with comments indented according to their nesting level.
    """
    submission_time_str = datetime.datetime.fromtimestamp(submission_dict["created_at"], datetime.UTC).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    submission_md = (
        f"**{submission_dict['subreddit']}** | Posted by {submission_dict['author']} _{submission_time_str}_\n"
    )
    submission_md += f"### [{format_submission_title(submission_dict['title'])}]({submission_dict['link']})\n\n"
    submission_md += f"{submission_dict['body']}\n\n"

    # Fetch and process comments for the submission
    cursor.execute(
        """
        SELECT
            id, submission_id, author, created_utc, parent_id, permalink, comment_body, updated_at
        FROM comments
        WHERE submission_id = ?
        ORDER BY created_utc
        """,
        (submission_dict["id"],),
    )
    comments = cursor.fetchall()

    comments_dict = create_thread_dicts(comments)
    for comment in comments_dict:
        submission_md += create_intended_md_from_thread(comment)
    return submission_md + "\n---\n\n"


def create_intended_md_from_thread(comment: dict[str, Any], level=0) -> str:
    """
    Recursively generates indented markdown text for a comment thread.

    Args:
        comment: A dictionary representing a comment thread.
        level: The nesting level of the comment (default: 0).

    Returns:
        A string containing the markdown representation of the comment and its replies,
        with each comment indented according to its nesting level.
    """
    indent = "    " * level
    content = "\n".join(f"{indent + '    ' + line if line else ''}" for line in comment["comment_body"].splitlines())

    parent_info = ""
    if comment["type"] == CommentType.ORPHAN:
        parent_info = " *(in reply to a comment not included)*"

    comment_time = datetime.datetime.fromtimestamp(comment["created_utc"], datetime.UTC).strftime("%Y-%m-%d %H:%M:%S")
    comment_title = f"**[{comment['author']}]({comment['permalink']})** _{comment_time}_{parent_info}"

    markdown = f"{indent}- {comment_title}:\n\n{content}\n"

    sorted_children = sorted(comment["children"], key=lambda x: x["created_utc"])
    for child in sorted_children:
        markdown += create_intended_md_from_thread(child, level + 1)
    return markdown


def sanitize_markdown_content(content: str) -> str:
    """
    Sanitizes user-generated content to prevent it from interfering with EPUB markdown structure.

    Currently, it escapes markdown headers by prefixing them with a backslash.

    Args:
        content: The string content to sanitize.

    Returns:
        The sanitized string content.
    """
    # Escape markdown headers by prefixing with a backslash
    sanitized_content = re.sub(r"^(#+)", r"\\\1", unicodedata.normalize("NFC", content), flags=re.MULTILINE)
    return sanitized_content


def create_non_indented_md_from_submission(cursor: sqlite3.Cursor, submission_dict: dict[str, str | float]) -> str:
    """
    Generates non-indented markdown text for a given Reddit submission and its comments, suitable for EPUB generation.

    Uses markdown headings to represent the structure instead of indentation.

    Args:
        cursor: An SQLite database cursor.
        submission_dict: A dictionary containing the submission data.

    Returns:
        A string containing the markdown representation of the submission and its comments,
        without indentation and using headings for structure.
    """
    submission_time_str = datetime.datetime.fromtimestamp(submission_dict["created_at"], datetime.UTC).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    submission_md = f"## [{format_submission_title(submission_dict['title'])}]({submission_dict['link']}) {{#discussion-{submission_dict['id']}}}\n"
    submission_md += f"**Subreddit**: {submission_dict['subreddit']} | **Posted by**: {submission_dict['author']} _{submission_time_str}_\n\n"
    submission_md += f"{sanitize_markdown_content(submission_dict['body'])}\n\n"

    # Fetch and process comments for the submission
    cursor.execute(
        """
        SELECT
            id, submission_id, author, created_utc, parent_id, permalink, comment_body, updated_at
        FROM comments
        WHERE submission_id = ?
        ORDER BY created_utc
        """,
        (submission_dict["id"],),
    )
    comments = cursor.fetchall()

    comments_dict = create_thread_dicts(comments)
    for comment in comments_dict:
        submission_md += create_non_indented_md_from_thread(comment)
    return submission_md


def create_non_indented_md_from_thread(comment: dict[str, Any], level=0) -> str:
    """
    Recursively generates non-indented markdown text for a comment thread, suitable for EPUB generation.

    Uses markdown headings to represent the comment hierarchy.

    Args:
        comment: A dictionary representing a comment thread.
        level: The nesting level of the comment (default: 0).

    Returns:
        A string containing the markdown representation of the comment and its replies,
        without indentation and using headings for structure.
    """
    # Use level to determine heading level (e.g., ###, ####)
    heading_prefix = "#" * min(level + 3, 6)  # Markdown supports at most six levels.
    comment_time = datetime.datetime.fromtimestamp(comment["created_utc"], datetime.UTC).strftime("%Y-%m-%d %H:%M:%S")

    parent_info = ""
    if comment["type"] == CommentType.ORPHAN:
        parent_info = " *(in reply to a comment not included)*"
    comment_title = (
        f"{heading_prefix} Comment by [{comment['author']}]({comment['permalink']}) on {comment_time}{parent_info} "
        f"{{#comment-{comment['id']} .unnumbered .unlisted}}"
    )

    markdown = f"{comment_title}\n\n{sanitize_markdown_content(comment['comment_body'])}\n\n"

    # Sort children by 'created_at' ascending
    sorted_children = sorted(comment["children"], key=lambda x: x["created_utc"])
    for child in sorted_children:
        markdown += create_non_indented_md_from_thread(child, level + 1)
    return markdown


def convert_to_epub_and_pdf(input_dir: Path, epub_dir: str, pdf_dir, *, expected_records) -> None:
    """Build the full set in isolation and publish only validated outputs."""
    try:
        from .render_archive import convert_archive
    except ImportError:  # Direct script execution.
        from render_archive import convert_archive
    convert_archive(input_dir, epub_dir, pdf_dir, metablock_template, expected_records=expected_records)


def build_archive(database="reddit_comments.db", output_dir="."):
    """Read the database without modifying it; stage all formats before publishing."""
    import tempfile
    from contextlib import closing

    try:
        from .archive_manifest import record_manifest
        from .render_archive import publish_files
    except ImportError:
        from archive_manifest import record_manifest
        from render_archive import publish_files
    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".archive-build-", dir=root) as temp:
        stage = Path(temp)
        markdown = stage / "markdown_files"
        markdown.mkdir()
        source = stage / "source"
        epub = stage / "epub_files"
        pdf = stage / "pdf_files"
        uri = Path(database).resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            # Keep the inventory and generated text on the same SQLite snapshot.
            conn.execute("BEGIN")
            expected_records = record_manifest(conn)
            save_comments_to_markdown(conn, markdown, source)
        convert_to_epub_and_pdf(source, epub, pdf, expected_records=expected_records)
        pairs = [
            (file, root / directory.name / file.name)
            for directory in (markdown, epub, pdf)
            for file in sorted(directory.iterdir())
        ]
        publish_files(pairs)


if __name__ == "__main__":
    build_archive()
