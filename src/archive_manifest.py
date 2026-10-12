"""Database-derived record coverage, independent of untrusted Markdown parsing."""

import datetime
import re
from collections import Counter


def record_manifest(connection):
    """Inventory every saved record using the caller's read-only DB snapshot.

    Comments follow the discussion's UTC year, including replies from later years.
    Do not infer this inventory from Markdown: a body can swallow later headings.
    """
    volumes = {}
    submission_volumes = {}

    def anchor(kind, record_id):
        if not isinstance(record_id, str) or not re.fullmatch(r"[A-Za-z0-9]+", record_id):
            raise ValueError(f"Invalid {kind} record ID: {record_id!r}")
        return f"{kind}-{record_id}"

    for submission_id, created_at in connection.execute("SELECT id, created_at FROM submissions"):
        year = datetime.datetime.fromtimestamp(created_at, datetime.UTC).year
        volume = f"ven_anigha_reddit_archive_{year}"
        submission_volumes[submission_id] = volume
        volumes.setdefault(volume, []).append(anchor("discussion", submission_id))
    for comment_id, submission_id in connection.execute("SELECT id, submission_id FROM comments"):
        if submission_id not in submission_volumes:
            raise ValueError(f"Comment {comment_id!r} has no saved discussion {submission_id!r}")
        volumes[submission_volumes[submission_id]].append(anchor("comment", comment_id))
    for volume, anchors in volumes.items():
        validate_record_anchors(anchors, anchors, f"database manifest for {volume}")
    return {volume: tuple(anchors) for volume, anchors in volumes.items()}


def is_record_anchor(identifier):
    return identifier.startswith(("discussion-", "comment-"))


def validate_record_anchors(expected, actual, context):
    """Require exactly one occurrence per database record, with no invented IDs."""
    expected, actual = Counter(expected), Counter(actual)
    problems = []
    for label, anchors in (
        ("duplicate expected anchors", [key for key, count in expected.items() if count != 1]),
        ("missing anchors", expected.keys() - actual.keys()),
        ("duplicate anchors", [key for key, count in actual.items() if count != 1]),
        ("unexpected anchors", actual.keys() - expected.keys()),
    ):
        if anchors:
            ordered = sorted(anchors)
            excerpt = ", ".join(ordered[:10])
            if len(ordered) > 10:
                excerpt += f", ... ({len(ordered)} total)"
            problems.append(f"{label}: {excerpt}")
    if problems:
        raise ValueError(f"Record coverage failed for {context}: " + "; ".join(problems))
