"""Validate record coverage independently of untrusted Markdown parsing."""

import collections
from collections.abc import Iterable
import datetime
import re
import sqlite3


def _record_anchor(kind: str, record_id: object) -> str:
    """Build a record anchor only from an ASCII-alphanumeric database ID.

    Args:
        kind: Trusted prefix identifying the record as a discussion or comment.
        record_id: Saved record ID to validate before inclusion in Markdown.

    Returns:
        The prefixed ID, such as ``comment-abc123``.

    Raises:
        ValueError: The ID is not a nonempty ASCII-alphanumeric string.
    """
    if not isinstance(record_id, str) or not re.fullmatch(
        r"[A-Za-z0-9]+", record_id
    ):
        raise ValueError(f"Invalid {kind} record ID: {record_id!r}")
    return f"{kind}-{record_id}"


def record_manifest(
    connection: sqlite3.Connection,
) -> dict[str, tuple[str, ...]]:
    """Inventory every saved record without parsing untrusted Markdown.

    Comments belong to the discussion's UTC year, including later-year replies.
    The caller owns the connection and must supply a consistent database
    snapshot. This function does not commit, close, or change the database.

    Args:
        connection: Open archive database containing submissions and comments.
            Submission ``created_at`` values are Unix timestamps in seconds.

    Returns:
        Volume stems mapped to tuples of discussion and comment anchors. Each
        saved record occurs exactly once. Anchor order follows database
        iteration, not necessarily chronology. An empty database yields {}.

    Raises:
        ValueError: A record ID is invalid, a comment has no saved discussion,
            anchors are duplicated, or a timestamp cannot be converted to UTC.
        OverflowError: A timestamp exceeds the platform's supported range.
        OSError: The platform cannot convert a timestamp.
        sqlite3.Error: The database cannot be queried.
    """
    volumes: dict[str, list[str]] = {}
    submission_volumes: dict[str, str] = {}

    for submission_id, created_at in connection.execute(
        "SELECT id, created_at FROM submissions"
    ):
        year = datetime.datetime.fromtimestamp(created_at, datetime.UTC).year
        volume = f"ven_anigha_reddit_archive_{year}"
        submission_volumes[submission_id] = volume
        volumes.setdefault(volume, []).append(
            _record_anchor("discussion", submission_id)
        )
    for comment_id, submission_id in connection.execute(
        "SELECT id, submission_id FROM comments"
    ):
        if submission_id not in submission_volumes:
            raise ValueError(
                f"Comment {comment_id!r} has no saved discussion "
                f"{submission_id!r}"
            )
        volumes[submission_volumes[submission_id]].append(
            _record_anchor("comment", comment_id)
        )
    for volume, anchors in volumes.items():
        validate_record_anchors(
            anchors, anchors, f"database manifest for {volume}"
        )
    return {volume: tuple(anchors) for volume, anchors in volumes.items()}


def is_record_anchor(identifier: str) -> bool:
    """Identify a record-anchor prefix without validating its ID suffix.

    Args:
        identifier: Heading or destination ID to classify.

    Returns:
        Whether the ID begins with ``discussion-`` or ``comment-``.
    """
    return identifier.startswith(("discussion-", "comment-"))


def validate_record_anchors(
    expected: Iterable[str], actual: Iterable[str], context: str
) -> None:
    """Require one occurrence per database record and no invented anchors.

    Args:
        expected: Authoritative record anchors, consumed once for counting.
        actual: Observed record anchors, consumed once for counting.
        context: Volume or processing stage to include in failure diagnostics.

    Raises:
        ValueError: Either inventory has duplicates, or actual anchors are
            missing from or additional to the expected inventory.
    """
    expected_counts = collections.Counter(expected)
    actual_counts = collections.Counter(actual)
    problems = []
    for label, anchors in (
        (
            "duplicate expected anchors",
            [key for key, count in expected_counts.items() if count != 1],
        ),
        ("missing anchors", expected_counts.keys() - actual_counts.keys()),
        (
            "duplicate anchors",
            [key for key, count in actual_counts.items() if count != 1],
        ),
        ("unexpected anchors", actual_counts.keys() - expected_counts.keys()),
    ):
        if anchors:
            ordered = sorted(anchors)
            excerpt = ", ".join(ordered[:10])
            if len(ordered) > 10:
                excerpt += f", ... ({len(ordered)} total)"
            problems.append(f"{label}: {excerpt}")
    if problems:
        raise ValueError(
            f"Record coverage failed for {context}: " + "; ".join(problems)
        )
