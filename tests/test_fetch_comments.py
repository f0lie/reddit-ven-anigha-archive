"""Test snapshot preservation, resumable ingestion, and CLI configuration."""

from collections.abc import Iterator
import contextlib
import datetime
import pathlib
import re
import sqlite3
from typing import Any
from unittest import mock

from praw import models
import pytest

from src import fetch_comments

# --- Mock Data and Objects ---

MOCK_DATETIME = datetime.datetime(2023, 10, 27, 10, 0, 0, tzinfo=datetime.UTC)
MOCK_USERNAME = "testuser"
MOCK_COMMENT_ID = "comment1"


def get_trailing_integer(string: str) -> int | None:
    """Return the integer represented by a string's trailing digits.

    Args:
        string: Text whose final decimal digits are to be parsed.

    Returns:
        The parsed integer, or None if the string has no trailing digits.
    """
    if match := re.search(r"\d+$", string):
        return int(match.group())
    return None


def create_mock_submission(submission_id: str, **kwargs: Any) -> mock.MagicMock:
    """Build a mock submission with deterministic, ID-derived metadata.

    Args:
        submission_id: Bare submission ID ending in a decimal day offset when
            no creation time is supplied.
        **kwargs: Optional created_utc datetime, converted to Unix seconds.
            If absent or None, use MOCK_DATETIME minus the ID's day offset.
            Other keyword arguments are ignored.

    Returns:
        A new Submission-spec mock with an author mock and populated metadata.

    Raises:
        TypeError: If the default timestamp is needed but the ID has no suffix.
    """
    mock_submission = mock.MagicMock(spec=models.Submission)
    mock_submission.id = submission_id
    mock_submission.title = "title_" + submission_id
    mock_submission.selftext = "body_" + submission_id
    mock_submission.author = mock.MagicMock(spec=models.Redditor)
    mock_submission.author.name = "author_" + submission_id
    mock_submission.created_utc = (
        kwargs.get("created_utc")
        if kwargs.get("created_utc") is not None
        else (
            MOCK_DATETIME
            - datetime.timedelta(days=get_trailing_integer(submission_id))
        )
    ).timestamp()  # Default time is based on mock id
    mock_submission.url = (
        f"https://reddit.com/r/subreddit_{submission_id}/"
        f"comments/{submission_id}/"
    )
    mock_submission.subreddit_name_prefixed = "r/subreddit_" + submission_id
    return mock_submission


def create_mock_comment(
    submission: mock.MagicMock,
    comment_id: str,
    parent_id: str | None = None,
    is_root: bool = True,
    **kwargs: Any,
) -> mock.MagicMock:
    """Build a mock comment linked to the supplied submission object.

    Args:
        submission: Submission mock to retain by reference, without mutation.
        comment_id: Bare comment ID ending in a decimal day offset when no
            creation timestamp is supplied.
        parent_id: Reddit fullname for the parent. A falsey value defaults to
            the submission's t3_ fullname, independently of is_root.
        is_root: Whether the mock describes a top-level comment.
        **kwargs: Optional created_utc value in Unix seconds, retained as is.
            If absent or None, derive it from MOCK_DATETIME minus the ID's
            day offset. Other keyword arguments are ignored.

    Returns:
        A new Comment-spec mock with author, body, timestamp, and parent data.

    Raises:
        TypeError: If the default timestamp is needed but the ID has no suffix.
    """
    mock_comment = mock.MagicMock(spec=models.Comment)
    mock_comment.id = comment_id
    mock_comment.parent_id = (
        parent_id if parent_id else f"t3_{submission.id}"
    )  # Simulate PRAW's structure
    mock_comment.is_root = is_root
    mock_comment.author = mock.MagicMock(spec=models.Redditor)
    mock_comment.author.name = "author_" + comment_id
    mock_comment.submission = submission  # Use the provided submission object
    mock_comment.permalink = (
        f"/r/{submission.subreddit_name_prefixed}/comments/"
        f"{submission.id}/{comment_id}/"
    )
    mock_comment.created_utc = (
        kwargs.get("created_utc")
        if kwargs.get("created_utc") is not None
        else (
            MOCK_DATETIME
            - datetime.timedelta(days=get_trailing_integer(comment_id))
        ).timestamp()
    )  # Default time is based on mock id
    mock_comment.body = "body_" + comment_id
    return mock_comment


# --- Fixtures ---


@pytest.fixture
def mock_reddit() -> mock.MagicMock:
    """Provide a client mock with two comments under one submission.

    Returns:
        A new mock whose redditor().comments.new() returns the same two-item
        list on each call, without applying limits or cursors.
    """
    mock_reddit = mock.MagicMock()
    mock_submission1 = create_mock_submission("submission1")
    mock_comments = [
        create_mock_comment(mock_submission1, "comment1"),
        create_mock_comment(
            mock_submission1,
            "comment2",
            parent_id="t3_submission1",
            is_root=False,
        ),
    ]
    mock_reddit.redditor.return_value.comments.new.return_value = mock_comments
    return mock_reddit


@pytest.fixture
def in_memory_db() -> Iterator[sqlite3.Connection]:
    """Provide an isolated, initially schema-free SQLite connection.

    Yields:
        An open in-memory database connection. Teardown closes it, discarding
        the database; the fixture does not commit test writes.

    Raises:
        sqlite3.Error: If connection creation or closure fails.
    """
    conn = sqlite3.connect(":memory:")
    yield conn
    conn.close()


# --- Test Cases ---


def test_fetch_user_comments_no_existing(mock_reddit: mock.MagicMock) -> None:
    comments = fetch_comments.fetch_user_comments(
        mock_reddit, MOCK_USERNAME, 100
    )

    assert len(comments) == 1
    assert "submission1" in comments
    assert len(comments["submission1"]["comments"]) == 2


def test_fetch_user_comments_till_existing(mock_reddit: mock.MagicMock) -> None:
    comments = fetch_comments.fetch_user_comments(
        mock_reddit, MOCK_USERNAME, 100, till_comment_id="comment2"
    )

    assert len(comments) == 1
    assert "submission1" in comments
    assert len(comments["submission1"]["comments"]) == 1


def test_fetch_user_comments_empty(mock_reddit: mock.MagicMock) -> None:
    mock_reddit.redditor.return_value.comments.new.return_value = []
    comments = fetch_comments.fetch_user_comments(
        mock_reddit, MOCK_USERNAME, 100
    )
    assert len(comments) == 0


def test_save_comments_to_db(in_memory_db: sqlite3.Connection) -> None:
    mock_submission1 = create_mock_submission("submission1")
    mock_submission2 = create_mock_submission("submission2")
    mock_comments_data = {
        "submission1": {
            "title": "Submission Title 1",
            "body": "Submission Body 1",
            "author": "Author1",
            "created_at": MOCK_DATETIME.timestamp(),
            "link": "https://reddit.com/r/subreddit1/comments/submission1/",
            "subreddit": "r/subreddit1",
            "comments": [
                create_mock_comment(
                    mock_submission1,
                    "comment1",
                    created_utc=MOCK_DATETIME.timestamp(),
                ),
            ],
        },
        "submission2": {
            "title": "Submission Title 2",
            "body": "Submission Body 2",
            "author": "Author2",
            "created_at": MOCK_DATETIME.timestamp() - 86400,
            "link": "https://reddit.com/r/subreddit2/comments/submission2/",
            "subreddit": "r/subreddit2",
            "comments": [
                create_mock_comment(
                    mock_submission2,
                    "comment2",
                    parent_id="t3_submission1",
                    is_root=False,
                    created_utc=MOCK_DATETIME.timestamp() - 86400,
                ),
            ],
        },
    }

    with mock.patch("src.fetch_comments.datetime.datetime") as mock_dt:
        mock_dt.now.return_value = MOCK_DATETIME
        fetch_comments.save_comments_to_db(in_memory_db, mock_comments_data)

    cursor = in_memory_db.cursor()
    cursor.execute("SELECT * FROM submissions")
    submissions = cursor.fetchall()
    assert len(submissions) == 2

    cursor.execute("SELECT * FROM comments")
    comments = cursor.fetchall()
    assert len(comments) == 2

    # Test upsert (update existing comment)
    mock_comments_data["submission1"]["comments"][
        0
    ].body = "Updated Comment Body"
    with mock.patch("src.fetch_comments.datetime.datetime") as mock_dt:
        mock_dt.now.return_value = MOCK_DATETIME
        fetch_comments.save_comments_to_db(in_memory_db, mock_comments_data)

    cursor.execute("SELECT comment_body FROM comments WHERE id = 'comment1'")
    updated_comment_body = cursor.fetchone()[0]
    assert updated_comment_body == "Updated Comment Body"


def test_get_latest_comment_id(in_memory_db: sqlite3.Connection) -> None:
    # Create tables
    cursor = in_memory_db.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS comments (
            id TEXT PRIMARY KEY,
            submission_id TEXT,
            author TEXT,
            created_utc INTEGER,
            parent_id TEXT,
            permalink TEXT,
            comment_body TEXT,
            updated_at REAL,
            FOREIGN KEY (submission_id) REFERENCES submissions (id),
            FOREIGN KEY (parent_id) REFERENCES comments (id)
        )
        """
    )

    # Test with empty database
    latest_comment_id = fetch_comments.get_latest_comment_id(in_memory_db)
    assert latest_comment_id is None

    # Test with populated database
    mock_submission1 = create_mock_submission("submission1")
    mock_comments_data = {
        "submission1": {
            "title": "Submission Title 1",
            "body": "Submission Body 1",
            "author": "Author1",
            "created_at": MOCK_DATETIME.timestamp(),
            "link": "https://reddit.com/r/subreddit1/comments/submission1/",
            "subreddit": "r/subreddit1",
            "comments": [
                create_mock_comment(
                    mock_submission1,
                    "comment1",
                    created_utc=MOCK_DATETIME.timestamp(),
                ),
                create_mock_comment(
                    mock_submission1,
                    "comment2",
                    created_utc=MOCK_DATETIME.timestamp() - 86400,
                ),
            ],
        }
    }
    with mock.patch("src.fetch_comments.datetime.datetime") as mock_dt:
        mock_dt.now.return_value = MOCK_DATETIME
        fetch_comments.save_comments_to_db(in_memory_db, mock_comments_data)

    latest_comment_id = fetch_comments.get_latest_comment_id(in_memory_db)
    assert latest_comment_id == "comment1"


def submission_data(
    submission: mock.MagicMock, comments: list[mock.MagicMock]
) -> dict[str, dict[str, Any]]:
    """Wrap a submission and its comments in the ingestion payload shape.

    Args:
        submission: Mock whose submission metadata is copied into the payload.
        comments: List of comment mocks to retain by reference.

    Returns:
        A new dictionary keyed by the submission ID. Its metadata includes
        created_at in Unix seconds and an author name or None. The supplied
        comment list and mock objects are shared, not copied or mutated.
    """
    return {
        submission.id: {
            "title": submission.title,
            "body": submission.selftext,
            "author": submission.author.name if submission.author else None,
            "created_at": submission.created_utc,
            "link": submission.url,
            "subreddit": submission.subreddit_name_prefixed,
            "comments": comments,
        }
    }


def configure_listing(
    reddit: mock.MagicMock, comments: list[mock.MagicMock]
) -> None:
    """Install a cursor-aware listing side effect on a Reddit client mock.

    Later calls observe mutations to the original comments list, making it
    possible to model newly added or deleted comments between syncs. Only the
    mock's listing side effect is changed; the comments are left untouched.

    Args:
        reddit: Client mock whose redditor().comments.new() is configured.
        comments: Newest-first comment mocks, retained by reference.

    """

    def listing(
        limit: int | None = None, params: dict[str, str] | None = None
    ) -> Iterator[mock.MagicMock]:
        """Select a comment iterator using a Reddit-style limit and cursor.

        Args:
            limit: Maximum items to return, or None for all selected items.
            params: Optional mapping with an after t1_ fullname. An unknown
                cursor returns no comments; a falsey mapping starts at the head.

        Returns:
            An iterator over a slice of the current comments list. Comment
            objects are shared; this call does not mutate the original list.

        Raises:
            KeyError: If a truthy params mapping lacks an after key.
        """
        start = 0
        if params:
            after_id = params["after"].removeprefix("t1_")
            start = next(
                (
                    index + 1
                    for index, comment in enumerate(comments)
                    if comment.id == after_id
                ),
                len(comments),
            )
        selected = comments[start:]
        return iter(selected if limit is None else selected[:limit])

    reddit.redditor.return_value.comments.new.side_effect = listing


def read_state(
    conn: sqlite3.Connection, username: str = MOCK_USERNAME
) -> tuple[str | None, str | None, str | None] | None:
    """Read one user's persisted incremental scan state without mutation.

    Args:
        conn: Open SQLite connection with an existing fetch_state table.
        username: Username to look up using the table's case-insensitive key.

    Returns:
        The checkpoint ID, pending newest ID, and resume ID as a tuple, or
        None if the user has no state row. Individual IDs can also be None.

    Raises:
        sqlite3.Error: If the state query fails, including a missing table.
    """
    return conn.execute(
        "SELECT checkpoint_id, pending_newest_id, after_comment_id "
        "FROM fetch_state WHERE username = ?",
        (username,),
    ).fetchone()


def stored_ids(conn: sqlite3.Connection) -> set[str]:
    """Read the unique comment IDs currently stored in an archive.

    Args:
        conn: Open SQLite connection with an existing comments table.

    Returns:
        A new set of stored IDs, empty if there are no comments. The database
        is not changed.

    Raises:
        sqlite3.Error: If the query fails, including a missing comments table.
    """
    return {row[0] for row in conn.execute("SELECT id FROM comments")}


def test_get_latest_comment_id_initializes_fresh_database(
    in_memory_db: sqlite3.Connection,
) -> None:
    assert fetch_comments.get_latest_comment_id(in_memory_db) is None
    tables = {
        row[0]
        for row in in_memory_db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {
        "submissions",
        "comments",
        "submission_history",
        "comment_history",
        "fetch_state",
    } <= tables


@pytest.mark.parametrize(
    "placeholder",
    [
        "[deleted]",
        "[removed]",
        "[deleted by user]",
        "[removed by reddit]",
        "",
        None,
        " [REMOVED] ",
        " [DELETED BY USER] ",
    ],
)
def test_deleted_content_cannot_replace_archived_snapshots(
    in_memory_db: sqlite3.Connection, placeholder: str | None
) -> None:
    submission = create_mock_submission("submission1")
    comment = create_mock_comment(submission, "comment1")
    fetch_comments.save_comments_to_db(
        in_memory_db, submission_data(submission, [comment])
    )
    original_submission = in_memory_db.execute(
        "SELECT * FROM submissions"
    ).fetchone()
    original_comment = in_memory_db.execute("SELECT * FROM comments").fetchone()

    submission.title = placeholder
    submission.selftext = placeholder
    submission.author = None
    comment.body = placeholder
    comment.author = None
    fetch_comments.save_comments_to_db(
        in_memory_db, submission_data(submission, [comment])
    )

    assert (
        in_memory_db.execute("SELECT * FROM submissions").fetchone()
        == original_submission
    )
    assert (
        in_memory_db.execute("SELECT * FROM comments").fetchone()
        == original_comment
    )
    assert (
        in_memory_db.execute(
            "SELECT COUNT(*) FROM submission_history"
        ).fetchone()[0]
        == 0
    )
    assert (
        in_memory_db.execute("SELECT COUNT(*) FROM comment_history").fetchone()[
            0
        ]
        == 0
    )


def test_deleted_op_is_preserved_when_new_comment_arrives(
    in_memory_db: sqlite3.Connection,
) -> None:
    submission = create_mock_submission("submission1")
    first = create_mock_comment(submission, "comment2")
    fetch_comments.save_comments_to_db(
        in_memory_db, submission_data(submission, [first])
    )
    submission.selftext = "[removed]"
    submission.author = None
    second = create_mock_comment(submission, "comment1")
    fetch_comments.save_comments_to_db(
        in_memory_db, submission_data(submission, [second])
    )

    assert stored_ids(in_memory_db) == {"comment1", "comment2"}
    assert in_memory_db.execute(
        "SELECT body, author FROM submissions"
    ).fetchone() == (
        "body_submission1",
        "author_submission1",
    )


def test_real_edits_keep_previous_versions_and_do_not_duplicate_history(
    in_memory_db: sqlite3.Connection,
) -> None:
    submission = create_mock_submission("submission1")
    comment = create_mock_comment(submission, "comment1")
    fetch_comments.save_comments_to_db(
        in_memory_db, submission_data(submission, [comment])
    )

    submission.title = "Edited title"
    submission.selftext = "Edited question"
    comment.body = "Edited answer"
    fetch_comments.save_comments_to_db(
        in_memory_db, submission_data(submission, [comment])
    )
    fetch_comments.save_comments_to_db(
        in_memory_db, submission_data(submission, [comment])
    )

    assert in_memory_db.execute(
        "SELECT title, body FROM submissions"
    ).fetchone() == ("Edited title", "Edited question")
    assert in_memory_db.execute(
        "SELECT comment_body FROM comments"
    ).fetchone() == ("Edited answer",)
    assert in_memory_db.execute(
        "SELECT title, body FROM submission_history"
    ).fetchall() == [("title_submission1", "body_submission1")]
    assert in_memory_db.execute(
        "SELECT comment_body FROM comment_history"
    ).fetchall() == [("body_comment1",)]

    comment.body = "Second edit"
    fetch_comments.save_comments_to_db(
        in_memory_db, submission_data(submission, [comment])
    )
    assert in_memory_db.execute(
        "SELECT comment_body FROM comment_history ORDER BY history_id"
    ).fetchall() == [
        ("body_comment1",),
        ("Edited answer",),
    ]


def test_useful_content_can_replace_previously_archived_placeholder(
    in_memory_db: sqlite3.Connection,
) -> None:
    submission = create_mock_submission("submission1")
    comment = create_mock_comment(submission, "comment1")
    submission.selftext = "[removed]"
    submission.author = None
    comment.body = "[deleted]"
    comment.author = None
    fetch_comments.save_comments_to_db(
        in_memory_db, submission_data(submission, [comment])
    )

    restored_submission = create_mock_submission("submission1")
    restored_comment = create_mock_comment(restored_submission, "comment1")
    fetch_comments.save_comments_to_db(
        in_memory_db, submission_data(restored_submission, [restored_comment])
    )
    assert in_memory_db.execute(
        "SELECT body, author FROM submissions"
    ).fetchone() == (
        "body_submission1",
        "author_submission1",
    )
    assert in_memory_db.execute(
        "SELECT comment_body, author FROM comments"
    ).fetchone() == (
        "body_comment1",
        "author_comment1",
    )


def test_limited_backfill_resumes_across_database_reopens(
    tmp_path: pathlib.Path, mock_reddit: mock.MagicMock
) -> None:
    submission = create_mock_submission("submission1")
    comments = [
        create_mock_comment(submission, f"comment{i}") for i in range(1, 8)
    ]
    configure_listing(mock_reddit, comments)
    database = tmp_path / "archive.db"

    with contextlib.closing(sqlite3.connect(database)) as conn:
        batch = fetch_comments.sync_user_comments(
            conn, mock_reddit, MOCK_USERNAME, 3
        )
        assert not batch.complete
        assert read_state(conn) == (None, "comment1", "comment3")
        assert stored_ids(conn) == {"comment1", "comment2", "comment3"}
    with contextlib.closing(sqlite3.connect(database)) as conn:
        batch = fetch_comments.sync_user_comments(
            conn, mock_reddit, MOCK_USERNAME, 3
        )
        assert not batch.complete
        assert read_state(conn) == (None, "comment1", "comment6")
    with contextlib.closing(sqlite3.connect(database)) as conn:
        batch = fetch_comments.sync_user_comments(
            conn, mock_reddit, MOCK_USERNAME, 3
        )
        assert batch.complete
        assert stored_ids(conn) == {comment.id for comment in comments}
        assert read_state(conn) == ("comment1", None, None)
    assert mock_reddit.redditor.return_value.comments.new.call_args_list[
        -1
    ].kwargs == {
        "limit": 4,
        "params": {"after": "t1_comment6"},
    }


def test_incremental_limit_does_not_skip_backlog_or_new_arrivals(
    in_memory_db: sqlite3.Connection, mock_reddit: mock.MagicMock
) -> None:
    submission = create_mock_submission("submission1")
    comments = [create_mock_comment(submission, "comment6")]
    configure_listing(mock_reddit, comments)
    fetch_comments.sync_user_comments(
        in_memory_db, mock_reddit, MOCK_USERNAME, 2
    )
    assert read_state(in_memory_db) == ("comment6", None, None)

    comments[:] = [
        create_mock_comment(submission, f"comment{i}") for i in range(1, 7)
    ]
    fetch_comments.sync_user_comments(
        in_memory_db, mock_reddit, MOCK_USERNAME, 2
    )
    assert read_state(in_memory_db) == ("comment6", "comment1", "comment2")
    comments.insert(0, create_mock_comment(submission, "comment0"))
    fetch_comments.sync_user_comments(
        in_memory_db, mock_reddit, MOCK_USERNAME, 2
    )
    assert read_state(in_memory_db) == ("comment6", "comment1", "comment4")
    fetch_comments.sync_user_comments(
        in_memory_db, mock_reddit, MOCK_USERNAME, 2
    )
    assert read_state(in_memory_db) == ("comment1", None, None)
    assert "comment0" not in stored_ids(in_memory_db)
    fetch_comments.sync_user_comments(
        in_memory_db, mock_reddit, MOCK_USERNAME, 2
    )
    assert read_state(in_memory_db) == ("comment0", None, None)
    assert stored_ids(in_memory_db) == {comment.id for comment in comments}


def test_legacy_archive_rescans_accessible_history_to_fill_gaps(
    in_memory_db: sqlite3.Connection, mock_reddit: mock.MagicMock
) -> None:
    submission = create_mock_submission("submission1")
    comments = [
        create_mock_comment(submission, f"comment{i}") for i in range(1, 6)
    ]
    fetch_comments.save_comments_to_db(
        in_memory_db, submission_data(submission, [comments[0], comments[-1]])
    )
    configure_listing(mock_reddit, comments)

    for _ in range(3):
        fetch_comments.sync_user_comments(
            in_memory_db, mock_reddit, MOCK_USERNAME, 2
        )
    assert stored_ids(in_memory_db) == {comment.id for comment in comments}
    assert read_state(in_memory_db) == ("comment1", None, None)


def test_lookahead_confirms_exact_limit_exhaustion_without_an_empty_resume(
    in_memory_db: sqlite3.Connection, mock_reddit: mock.MagicMock
) -> None:
    submission = create_mock_submission("submission1")
    comments = [create_mock_comment(submission, f"comment{i}") for i in (1, 2)]
    configure_listing(mock_reddit, comments)
    assert fetch_comments.sync_user_comments(
        in_memory_db, mock_reddit, MOCK_USERNAME, 2
    ).complete
    assert read_state(in_memory_db) == ("comment1", None, None)
    mock_reddit.redditor.return_value.comments.new.assert_called_once_with(
        limit=3
    )


def test_missing_checkpoint_scans_to_exhaustion(
    in_memory_db: sqlite3.Connection, mock_reddit: mock.MagicMock
) -> None:
    submission = create_mock_submission("submission1")
    comments = [create_mock_comment(submission, "comment4")]
    configure_listing(mock_reddit, comments)
    fetch_comments.sync_user_comments(
        in_memory_db, mock_reddit, MOCK_USERNAME, 2
    )
    comments[:] = [
        create_mock_comment(submission, f"comment{i}") for i in range(1, 4)
    ]
    assert not fetch_comments.sync_user_comments(
        in_memory_db, mock_reddit, MOCK_USERNAME, 2
    ).complete
    batch = fetch_comments.sync_user_comments(
        in_memory_db, mock_reddit, MOCK_USERNAME, 2
    )
    assert batch.complete
    assert batch.checkpoint_missing
    assert stored_ids(in_memory_db) == {f"comment{i}" for i in range(1, 5)}
    assert read_state(in_memory_db) == ("comment1", None, None)


def test_unlimited_fetch_finishes_history_in_one_run(
    in_memory_db: sqlite3.Connection, mock_reddit: mock.MagicMock
) -> None:
    submission = create_mock_submission("submission1")
    comments = [
        create_mock_comment(submission, f"comment{i}") for i in range(1, 6)
    ]
    configure_listing(mock_reddit, comments)
    assert fetch_comments.sync_user_comments(
        in_memory_db, mock_reddit, MOCK_USERNAME, None
    ).complete
    assert stored_ids(in_memory_db) == {comment.id for comment in comments}
    assert read_state(in_memory_db) == ("comment1", None, None)


def test_empty_history_has_no_checkpoint(
    in_memory_db: sqlite3.Connection, mock_reddit: mock.MagicMock
) -> None:
    configure_listing(mock_reddit, [])
    assert fetch_comments.sync_user_comments(
        in_memory_db, mock_reddit, MOCK_USERNAME, 2
    ).complete
    assert read_state(in_memory_db) == (None, None, None)
    assert stored_ids(in_memory_db) == set()


def test_scan_checkpoint_is_per_user(
    in_memory_db: sqlite3.Connection, mock_reddit: mock.MagicMock
) -> None:
    submission = create_mock_submission("submission1")
    comments = [create_mock_comment(submission, "comment1")]
    configure_listing(mock_reddit, comments)
    fetch_comments.sync_user_comments(
        in_memory_db, mock_reddit, "first_user", None
    )
    comments[:] = [create_mock_comment(submission, "comment2")]
    fetch_comments.sync_user_comments(
        in_memory_db, mock_reddit, "second_user", None
    )
    assert read_state(in_memory_db, "FIRST_USER") == ("comment1", None, None)
    assert read_state(in_memory_db, "second_user") == ("comment2", None, None)
    assert stored_ids(in_memory_db) == {"comment1", "comment2"}


def test_failed_fetch_does_not_advance_cursor(
    in_memory_db: sqlite3.Connection, mock_reddit: mock.MagicMock
) -> None:
    submission = create_mock_submission("submission1")
    comments = [
        create_mock_comment(submission, f"comment{i}") for i in range(1, 5)
    ]
    configure_listing(mock_reddit, comments)
    fetch_comments.sync_user_comments(
        in_memory_db, mock_reddit, MOCK_USERNAME, 2
    )
    before = read_state(in_memory_db)

    def failed_listing(**kwargs: Any) -> Iterator[mock.MagicMock]:
        """Yield one comment, then simulate a failed Reddit listing request.

        Args:
            **kwargs: Ignored PRAW listing arguments accepted by the mock.

        Yields:
            The third mock comment from the enclosing test's listing.

        Raises:
            RuntimeError: When iteration resumes after the first comment.
        """
        yield comments[2]
        raise RuntimeError("simulated Reddit failure")

    mock_reddit.redditor.return_value.comments.new.side_effect = failed_listing
    with pytest.raises(RuntimeError, match="simulated Reddit failure"):
        fetch_comments.sync_user_comments(
            in_memory_db, mock_reddit, MOCK_USERNAME, 2
        )
    assert read_state(in_memory_db) == before
    assert stored_ids(in_memory_db) == {"comment1", "comment2"}


def test_failed_state_write_rolls_back_content_and_history(
    in_memory_db: sqlite3.Connection, mock_reddit: mock.MagicMock
) -> None:
    submission = create_mock_submission("submission1")
    comment = create_mock_comment(submission, "comment1")
    fetch_comments.save_comments_to_db(
        in_memory_db, submission_data(submission, [comment])
    )
    comment.body = "Edited answer"
    configure_listing(mock_reddit, [comment])
    in_memory_db.execute(
        "CREATE TRIGGER reject_state BEFORE INSERT ON fetch_state "
        "BEGIN SELECT RAISE(ABORT, 'simulated state failure'); END"
    )

    with pytest.raises(sqlite3.IntegrityError, match="simulated state failure"):
        fetch_comments.sync_user_comments(
            in_memory_db, mock_reddit, MOCK_USERNAME, 2
        )
    assert read_state(in_memory_db) is None
    assert in_memory_db.execute(
        "SELECT comment_body FROM comments"
    ).fetchone() == ("body_comment1",)
    assert (
        in_memory_db.execute("SELECT COUNT(*) FROM comment_history").fetchone()[
            0
        ]
        == 0
    )


@pytest.mark.parametrize("limit", [0, -1])
def test_invalid_limit_rejected_before_reddit_request(
    mock_reddit: mock.MagicMock, limit: int | None
) -> None:
    with pytest.raises(ValueError, match="LIMIT must be a positive integer"):
        fetch_comments.fetch_user_comments(mock_reddit, MOCK_USERNAME, limit)
    mock_reddit.redditor.assert_not_called()


@pytest.mark.parametrize("till_last_comment", ["true", "false"])
def test_main_uses_only_temporary_database(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_reddit: mock.MagicMock,
    till_last_comment: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLIENT_ID", "test_client_id")
    monkeypatch.setenv("CLIENT_SECRET", "test_client_secret")
    monkeypatch.setenv("USER_AGENT", "test_user_agent")
    monkeypatch.setenv("TARGET_USERNAME", MOCK_USERNAME)
    monkeypatch.setenv("TILL_LAST_COMMENT", till_last_comment)
    monkeypatch.setenv("LIMIT", "10")
    with (
        mock.patch("src.fetch_comments.dotenv.load_dotenv") as mock_load,
        mock.patch(
            "src.fetch_comments.praw.Reddit", return_value=mock_reddit
        ) as mock_client,
    ):
        fetch_comments.main()
    mock_load.assert_called_once_with(".secrets")
    mock_client.assert_called_once_with(
        client_id="test_client_id",
        client_secret="test_client_secret",
        user_agent="test_user_agent",
    )
    with contextlib.closing(
        sqlite3.connect(tmp_path / "reddit_comments.db")
    ) as conn:
        assert stored_ids(conn) == {"comment1", "comment2"}
        if till_last_comment == "true":
            assert read_state(conn) == ("comment1", None, None)
        else:
            assert read_state(conn) is None


def test_main_empty_first_run_creates_database(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_reddit: mock.MagicMock,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TARGET_USERNAME", MOCK_USERNAME)
    monkeypatch.delenv("LIMIT", raising=False)
    monkeypatch.delenv("TILL_LAST_COMMENT", raising=False)
    configure_listing(mock_reddit, [])
    with (
        mock.patch("src.fetch_comments.dotenv.load_dotenv"),
        mock.patch("src.fetch_comments.praw.Reddit", return_value=mock_reddit),
    ):
        fetch_comments.main()
    with contextlib.closing(
        sqlite3.connect(tmp_path / "reddit_comments.db")
    ) as conn:
        assert fetch_comments.get_latest_comment_id(conn) is None
        assert read_state(conn) == (None, None, None)


@pytest.mark.parametrize(
    ("variable", "value", "message"),
    [
        ("LIMIT", "0", "LIMIT must"),
        ("TARGET_USERNAME", " ", "TARGET_USERNAME must"),
    ],
)
def test_main_rejects_invalid_configuration_before_opening_database(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    value: str,
    message: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TARGET_USERNAME", MOCK_USERNAME)
    monkeypatch.setenv("LIMIT", "2")
    monkeypatch.setenv(variable, value)
    with (
        mock.patch("src.fetch_comments.dotenv.load_dotenv"),
        mock.patch("src.fetch_comments.praw.Reddit") as mock_client,
        pytest.raises(ValueError, match=message),
    ):
        fetch_comments.main()
    mock_client.assert_not_called()
    assert not (tmp_path / "reddit_comments.db").exists()


def test_main_resumes_capped_scan_on_subsequent_runs(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_reddit: mock.MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TARGET_USERNAME", MOCK_USERNAME)
    monkeypatch.setenv("LIMIT", "2")
    monkeypatch.setenv("TILL_LAST_COMMENT", "true")
    submission = create_mock_submission("submission1")
    comments = [
        create_mock_comment(submission, f"comment{i}") for i in range(1, 6)
    ]
    configure_listing(mock_reddit, comments)
    with (
        mock.patch("src.fetch_comments.dotenv.load_dotenv"),
        mock.patch("src.fetch_comments.praw.Reddit", return_value=mock_reddit),
    ):
        fetch_comments.main()
        assert "remaining history will resume" in capsys.readouterr().out
        fetch_comments.main()
        fetch_comments.main()
    with contextlib.closing(
        sqlite3.connect(tmp_path / "reddit_comments.db")
    ) as conn:
        assert stored_ids(conn) == {comment.id for comment in comments}
        assert read_state(conn) == ("comment1", None, None)


def test_main_warns_if_previous_checkpoint_is_no_longer_accessible(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_reddit: mock.MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TARGET_USERNAME", MOCK_USERNAME)
    monkeypatch.delenv("LIMIT", raising=False)
    monkeypatch.setenv("TILL_LAST_COMMENT", "true")
    submission = create_mock_submission("submission1")
    comments = [create_mock_comment(submission, "comment2")]
    configure_listing(mock_reddit, comments)
    with (
        mock.patch("src.fetch_comments.dotenv.load_dotenv"),
        mock.patch("src.fetch_comments.praw.Reddit", return_value=mock_reddit),
    ):
        fetch_comments.main()
        assert "Warning:" not in capsys.readouterr().out
        comments[:] = [create_mock_comment(submission, "comment1")]
        fetch_comments.main()
        assert (
            "Archive continuity cannot be verified" in capsys.readouterr().out
        )
    with contextlib.closing(
        sqlite3.connect(tmp_path / "reddit_comments.db")
    ) as conn:
        assert stored_ids(conn) == {"comment1", "comment2"}


def test_lookahead_is_not_archived_or_used_as_resume_cursor(
    in_memory_db: sqlite3.Connection, mock_reddit: mock.MagicMock
) -> None:
    submission = create_mock_submission("submission1")
    comments = [
        create_mock_comment(submission, f"comment{i}") for i in range(1, 4)
    ]
    configure_listing(mock_reddit, comments)

    batch = fetch_comments.sync_user_comments(
        in_memory_db, mock_reddit, MOCK_USERNAME, 2
    )
    assert not batch.complete
    assert stored_ids(in_memory_db) == {"comment1", "comment2"}
    assert read_state(in_memory_db) == (None, "comment1", "comment2")
    assert fetch_comments.sync_user_comments(
        in_memory_db, mock_reddit, MOCK_USERNAME, 2
    ).complete
    assert stored_ids(in_memory_db) == {"comment1", "comment2", "comment3"}


def test_checkpoint_in_lookahead_completes_scan(
    in_memory_db: sqlite3.Connection, mock_reddit: mock.MagicMock
) -> None:
    submission = create_mock_submission("submission1")
    comments = [create_mock_comment(submission, "comment3")]
    configure_listing(mock_reddit, comments)
    fetch_comments.sync_user_comments(
        in_memory_db, mock_reddit, MOCK_USERNAME, 2
    )
    comments[:0] = [
        create_mock_comment(submission, f"comment{i}") for i in (1, 2)
    ]

    batch = fetch_comments.sync_user_comments(
        in_memory_db, mock_reddit, MOCK_USERNAME, 2
    )
    assert batch.complete
    assert not batch.checkpoint_missing
    assert read_state(in_memory_db) == ("comment1", None, None)
    assert stored_ids(in_memory_db) == {"comment1", "comment2", "comment3"}


@pytest.mark.parametrize("existing_checkpoint", [False, True])
@pytest.mark.parametrize("resume_limit", [2, None])
def test_deleted_resume_cursor_restarts_without_skipping_history(
    in_memory_db: sqlite3.Connection,
    mock_reddit: mock.MagicMock,
    existing_checkpoint: bool,
    resume_limit: int | None,
) -> None:
    submission = create_mock_submission("submission1")
    comments = (
        [create_mock_comment(submission, "comment6")]
        if existing_checkpoint
        else []
    )
    configure_listing(mock_reddit, comments)
    fetch_comments.sync_user_comments(
        in_memory_db, mock_reddit, MOCK_USERNAME, 2
    )
    checkpoint = "comment6" if existing_checkpoint else None
    comments[:0] = [
        create_mock_comment(submission, f"comment{i}") for i in range(1, 6)
    ]
    fetch_comments.sync_user_comments(
        in_memory_db, mock_reddit, MOCK_USERNAME, 2
    )
    assert read_state(in_memory_db) == (checkpoint, "comment1", "comment2")

    del comments[1]  # Reddit no longer exposes the stored after cursor.
    comments.insert(0, create_mock_comment(submission, "comment0"))
    batch = fetch_comments.sync_user_comments(
        in_memory_db, mock_reddit, MOCK_USERNAME, resume_limit
    )
    assert batch.cursor_invalid
    assert not batch.complete
    assert not batch.checkpoint_missing
    assert read_state(in_memory_db) == (checkpoint, None, None)

    for _ in range(3):
        batch = fetch_comments.sync_user_comments(
            in_memory_db, mock_reddit, MOCK_USERNAME, 2
        )
    assert batch.complete
    assert read_state(in_memory_db) == ("comment0", None, None)
    assert stored_ids(in_memory_db) == {comment.id for comment in comments} | {
        "comment2"
    }


@pytest.mark.parametrize("first_limit", [2, 3])
def test_ignored_resume_cursor_restarts_when_cursor_reappears(
    in_memory_db: sqlite3.Connection,
    mock_reddit: mock.MagicMock,
    first_limit: int,
) -> None:
    submission = create_mock_submission("submission1")
    comments = [
        create_mock_comment(submission, f"comment{i}") for i in range(1, 6)
    ]
    configure_listing(mock_reddit, comments)
    fetch_comments.sync_user_comments(
        in_memory_db, mock_reddit, MOCK_USERNAME, first_limit
    )
    mock_reddit.redditor.return_value.comments.new.side_effect = (
        lambda limit, params: iter(comments[:limit])
    )

    batch = fetch_comments.sync_user_comments(
        in_memory_db, mock_reddit, MOCK_USERNAME, 2
    )
    assert batch.cursor_invalid
    assert not batch.complete
    assert read_state(in_memory_db) == (None, None, None)
    assert (
        in_memory_db.execute("SELECT COUNT(*) FROM comment_history").fetchone()[
            0
        ]
        == 0
    )

    configure_listing(mock_reddit, comments)
    for _ in range(3):
        batch = fetch_comments.sync_user_comments(
            in_memory_db, mock_reddit, MOCK_USERNAME, 2
        )
    assert batch.complete
    assert stored_ids(in_memory_db) == {comment.id for comment in comments}


def test_failed_cursor_reset_preserves_previous_progress(
    in_memory_db: sqlite3.Connection, mock_reddit: mock.MagicMock
) -> None:
    submission = create_mock_submission("submission1")
    comments = [
        create_mock_comment(submission, f"comment{i}") for i in range(1, 5)
    ]
    configure_listing(mock_reddit, comments)
    fetch_comments.sync_user_comments(
        in_memory_db, mock_reddit, MOCK_USERNAME, 2
    )
    before = read_state(in_memory_db)
    del comments[1]
    in_memory_db.execute(
        "CREATE TRIGGER reject_state BEFORE INSERT ON fetch_state "
        "BEGIN SELECT RAISE(ABORT, 'simulated state failure'); END"
    )

    with pytest.raises(sqlite3.IntegrityError, match="simulated state failure"):
        fetch_comments.sync_user_comments(
            in_memory_db, mock_reddit, MOCK_USERNAME, 2
        )
    assert read_state(in_memory_db) == before
    assert stored_ids(in_memory_db) == {"comment1", "comment2"}


def test_main_warns_and_keeps_checkpoint_when_resume_cursor_disappears(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_reddit: mock.MagicMock,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TARGET_USERNAME", MOCK_USERNAME)
    monkeypatch.setenv("LIMIT", "2")
    monkeypatch.setenv("TILL_LAST_COMMENT", "true")
    submission = create_mock_submission("submission1")
    comments = [
        create_mock_comment(submission, f"comment{i}") for i in range(1, 5)
    ]
    configure_listing(mock_reddit, comments)
    with (
        mock.patch("src.fetch_comments.dotenv.load_dotenv"),
        mock.patch("src.fetch_comments.praw.Reddit", return_value=mock_reddit),
    ):
        fetch_comments.main()
        capsys.readouterr()
        del comments[1]
        fetch_comments.main()
        output = capsys.readouterr().out
        assert "resume cursor is unavailable or repeated" in output
        assert "checkpoint was not advanced" in output
        assert "LIMIT reached" not in output
    with contextlib.closing(
        sqlite3.connect(tmp_path / "reddit_comments.db")
    ) as conn:
        assert read_state(conn) == (None, None, None)
        assert stored_ids(conn) == {"comment1", "comment2"}
