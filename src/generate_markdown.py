"""Generates yearly Markdown and converts it with the legacy renderer.

The legacy conversion path logs per-file failures and continues. The isolated,
fail-closed build pipeline is introduced in the next stacked change.
"""

from collections.abc import Sequence
import datetime
import enum
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
from typing import Any

METABLOCK_TEMPLATE = """\
---
title: \"Ven Anīgha Reddit Archive {year}\"
author: \"Ven Anīgha\"
date: \"{year}\"
description: \"Reddit discussions by Ven Anīgha in {year}.\"
lang: en
mainfont: "Source Serif 4"
fontsize: 12pt
geometry: margin=1in
documentclass: book
pdf-engine: xelatex
toc: true
toc-depth: 2
---

# Ven Anīgha Reddit Archive {year}

"""

# Preserve the existing exported template name for callers of earlier versions.
metablock_template = METABLOCK_TEMPLATE  # pylint: disable=invalid-name


# Keep the per-year staging steps together for straightforward review.
# pylint: disable-next=too-many-locals
def save_comments_to_markdown(
    conn: sqlite3.Connection,
    markdown_files_dir: str | os.PathLike[str],
    temp_files_dir: str | os.PathLike[str],
) -> None:
    """Saves Reddit submissions and comments in yearly Markdown files.

    Organizes submissions by year and generates separate markdown files for each
    year.
    Creates both indented and non-indented (for EPUB) markdown formats.

    Args:
        conn: An SQLite database connection.
        markdown_files_dir: The directory to save the indented markdown files.
        temp_files_dir: Destination for non-indented EPUB/PDF source Markdown.

    Raises:
        sqlite3.Error: The submissions or comments cannot be read.
        OSError: The destination directories or files cannot be written.
    """
    markdown_dir = pathlib.Path(markdown_files_dir)
    temp_dir = pathlib.Path(temp_files_dir)
    temp_dir.mkdir(exist_ok=True)

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
        markdown_text = ""
        epub_text = METABLOCK_TEMPLATE.format(year=year)
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
        epub_file.write_text(epub_text, encoding="utf-8")
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
        f"### [{submission_dict['title']}]({submission_dict['link']})\n\n"
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
    return re.sub(r"^(#+)", r"\\\1", content, flags=re.MULTILINE)


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
        f"## [{submission_dict['title']}]({submission_dict['link']})\n"
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
        replies,
        without indentation and using headings for structure.
    """
    # Use level to determine heading level (e.g., ###, ####)
    heading_prefix = "#" * (level + 3)  # Start from ### for comments
    comment_time = datetime.datetime.fromtimestamp(
        comment["created_utc"], datetime.UTC
    ).strftime("%Y-%m-%d %H:%M:%S")

    parent_info = ""
    if comment["type"] == CommentType.ORPHAN:
        parent_info = " *(in reply to a comment not included)*"
    comment_title = (
        f"{heading_prefix} Comment by "
        f"[{comment['author']}]({comment['permalink']}) "
        f"on {comment_time}{parent_info}"
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
    input_dir: pathlib.Path,
    epub_dir: str | os.PathLike[str],
    pdf_dir: str | os.PathLike[str],
) -> None:
    """Runs the legacy per-file Pandoc conversion and logs failures.

    Outputs are written directly rather than staged. A failed conversion is
    printed and processing continues; callers cannot infer that every book
    was successfully replaced from this function returning normally.

    Args:
        input_dir: Directory containing Markdown files matched by ``*.md``.
        epub_dir: Existing destination directory for EPUB files.
        pdf_dir: Existing destination directory for PDF files.
    """
    epub_dir = pathlib.Path(epub_dir)
    pdf_dir = pathlib.Path(pdf_dir)
    for source_file in input_dir.glob("*.md"):
        base_name = source_file.stem
        epub_path = epub_dir / f"{base_name}.epub"
        pdf_path = pdf_dir / f"{base_name}.pdf"
        try:
            subprocess.run(
                ["pandoc", str(source_file), "-o", str(epub_path)], check=True
            )
            subprocess.run(
                [
                    "pandoc",
                    str(source_file),
                    "-o",
                    str(pdf_path),
                    "--pdf-engine",
                    "xelatex",
                ],
                check=True,
            )
            print(f"Converted {source_file} into {epub_path} and {pdf_path}.")
        except subprocess.CalledProcessError as error:
            print(f"Pandoc failed to convert {source_file}: {error}")
        # Preserve the legacy continue-on-error contract in this stack layer.
        except Exception as error:  # pylint: disable=broad-exception-caught
            print(
                f"An unexpected error occurred while converting "
                f"{source_file}: {error}"
            )


def main() -> None:
    """Generates the legacy archive from the working directory's database.

    Reads ``reddit_comments.db`` and writes Markdown, EPUB, and PDF files in
    their conventional working-directory folders. The temporary source folder
    is removed even when generation fails. Per-file conversion failures are
    logged by the legacy converter rather than propagated.

    Raises:
        sqlite3.Error: The database cannot be opened or queried.
        OSError: A source/output directory or file operation fails.
    """
    temp_files_dir = pathlib.Path("temp_files")
    temp_files_dir.mkdir(exist_ok=True)
    try:
        with sqlite3.connect("reddit_comments.db") as connection:
            save_comments_to_markdown(
                connection, "markdown_files", temp_files_dir
            )
        convert_to_epub_and_pdf(temp_files_dir, "epub_files", "pdf_files")
    finally:
        shutil.rmtree(temp_files_dir)


if __name__ == "__main__":
    main()
