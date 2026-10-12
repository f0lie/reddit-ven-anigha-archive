import datetime
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from typing import Any

import praw
from dotenv import load_dotenv
from tqdm import tqdm


@dataclass
class CommentBatch:
    comments: dict[str, dict[str, Any]]
    newest_id: str | None
    last_id: str | None
    complete: bool
    checkpoint_missing: bool
    cursor_invalid: bool = False


def _fetch_comment_batch(
    reddit: praw.Reddit,
    username: str,
    limit: int | None,
    till_comment_id: str | None = None,
    after_comment_id: str | None = None,
) -> CommentBatch:
    """Fetch one batch, distinguishing a capped scan from a completed scan."""
    if limit is not None and limit <= 0:
        raise ValueError("LIMIT must be a positive integer or unset.")

    user = reddit.redditor(username)
    comments_by_submission = {}
    # One lookahead distinguishes an exactly full final batch from a capped
    # batch. Only LIMIT comments are archived; the lookahead is fetched again.
    request_limit = None if limit is None else limit + 1
    listing_kwargs = {"limit": request_limit}
    if after_comment_id:
        listing_kwargs["params"] = {"after": f"t1_{after_comment_id}"}
    newest_id = last_id = None
    count = 0
    complete = False
    checkpoint_reached = False
    cursor_invalid = False

    for count, comment in enumerate(
        tqdm(user.comments.new(**listing_kwargs), total=request_limit, desc=f"Fetching comments for {username}"),
        start=1,
    ):
        if comment.id == after_comment_id:
            # A cursor is exclusive. Seeing it again means the server did not
            # honor it; treating this as progress can stall the scan forever.
            cursor_invalid = True
            break
        if comment.id == till_comment_id:
            complete = checkpoint_reached = True
            break
        if limit is not None and count > limit:
            break
        if newest_id is None:
            newest_id = comment.id
        last_id = comment.id
        submission_id = comment.submission.id
        if submission_id not in comments_by_submission:
            submission = comment.submission
            comments_by_submission[submission_id] = {
                "title": submission.title,
                "body": submission.selftext,
                "author": submission.author.name if submission.author else None,
                "created_at": submission.created_utc,
                "link": submission.url,
                "subreddit": submission.subreddit_name_prefixed,
                "comments": [],
            }
        comments_by_submission[submission_id]["comments"].append(comment)
    else:
        complete = True

    # A deleted/unavailable resume ID can produce an empty listing even while
    # older comments remain available from the head. Never promote a checkpoint
    # from that ambiguous result. Lookahead avoids this extra run at normal EOF.
    if after_comment_id is not None and count == 0:
        cursor_invalid = True
        complete = False

    return CommentBatch(
        comments_by_submission,
        newest_id,
        last_id,
        complete,
        checkpoint_missing=complete and till_comment_id is not None and not checkpoint_reached,
        cursor_invalid=cursor_invalid,
    )


def fetch_user_comments(
    reddit: praw.Reddit,
    username: str,
    limit: int | None,
    till_comment_id: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Fetch comments grouped by submission, optionally stopping at a known ID.

    Use sync_user_comments for persistent incremental scans with a finite LIMIT.
    Reddit's own listing/history limits still apply even when LIMIT is unset.
    """
    return _fetch_comment_batch(reddit, username, limit, till_comment_id).comments


def initialize_db(conn: sqlite3.Connection):
    """Create missing tables without modifying existing archive records."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS submissions (
            id TEXT PRIMARY KEY,
            title TEXT,
            body TEXT,
            author TEXT,
            created_at REAL,
            link TEXT,
            subreddit TEXT,
            updated_at REAL
        )
        """
    )
    conn.execute(
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS submission_history (
            history_id INTEGER PRIMARY KEY,
            id TEXT,
            title TEXT,
            body TEXT,
            author TEXT,
            created_at REAL,
            link TEXT,
            subreddit TEXT,
            updated_at REAL,
            archived_at REAL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS comment_history (
            history_id INTEGER PRIMARY KEY,
            id TEXT,
            submission_id TEXT,
            author TEXT,
            created_utc INTEGER,
            parent_id TEXT,
            permalink TEXT,
            comment_body TEXT,
            updated_at REAL,
            archived_at REAL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fetch_state (
            username TEXT PRIMARY KEY COLLATE NOCASE,
            checkpoint_id TEXT,
            pending_newest_id TEXT,
            after_comment_id TEXT
        )
        """
    )


def _is_placeholder(value: str | None) -> bool:
    return value is None or value.strip().lower() in {
        "",
        "[deleted]",
        "[removed]",
        "[deleted by user]",
        "[removed by reddit]",
    }


def _save_snapshot(
    conn: sqlite3.Connection,
    table: str,
    history_table: str,
    columns: tuple[str, ...],
    values: tuple,
    protected_fields: tuple[str, ...],
):
    """Keep useful text on deletion; retain each previous version on real edits.

    Table and column names are internal constants, never external input.
    """
    column_names = ", ".join(columns)
    existing = conn.execute(f"SELECT {column_names} FROM {table} WHERE id = ?", (values[0],)).fetchone()
    if existing:
        merged = list(values)
        for field in protected_fields:
            index = columns.index(field)
            if _is_placeholder(merged[index]) and not _is_placeholder(existing[index]):
                merged[index] = existing[index]
        values = tuple(merged)
        # Repeated reads, including placeholder-only changes, are not versions.
        if values[:-1] == existing[:-1]:
            return
        conn.execute(
            f"INSERT INTO {history_table} ({column_names}, archived_at) "
            f"VALUES ({', '.join('?' for _ in range(len(columns) + 1))})",
            (*existing, values[-1]),
        )

    assignments = ", ".join(f"{column}=excluded.{column}" for column in columns[1:])
    conn.execute(
        f"INSERT INTO {table} ({column_names}) VALUES ({', '.join('?' for _ in columns)}) "
        f"ON CONFLICT(id) DO UPDATE SET {assignments}",
        values,
    )


def save_comments_to_db(conn: sqlite3.Connection, comments: dict[str, dict[str, Any]], *, commit: bool = True):
    """Save current snapshots and retain earlier versions of edited content.

    Incremental ingestion passes commit=False so data and cursor commit together.
    """
    initialize_db(conn)
    now = datetime.datetime.now().timestamp()
    for submission_id, submission_data in comments.items():
        _save_snapshot(
            conn,
            "submissions",
            "submission_history",
            ("id", "title", "body", "author", "created_at", "link", "subreddit", "updated_at"),
            (
                submission_id,
                submission_data["title"],
                submission_data["body"],
                submission_data["author"],
                submission_data["created_at"],
                submission_data["link"],
                submission_data["subreddit"],
                now,
            ),
            ("title", "body", "author"),
        )
        for comment in submission_data["comments"]:
            parent_id = comment.parent_id[3:] if not comment.is_root else None
            _save_snapshot(
                conn,
                "comments",
                "comment_history",
                (
                    "id",
                    "submission_id",
                    "author",
                    "created_utc",
                    "parent_id",
                    "permalink",
                    "comment_body",
                    "updated_at",
                ),
                (
                    comment.id,
                    submission_id,
                    comment.author.name if comment.author else None,
                    comment.created_utc,
                    parent_id,
                    "https://www.reddit.com" + comment.permalink,
                    comment.body,
                    now,
                ),
                ("author", "comment_body"),
            )
    if commit:
        conn.commit()


def get_latest_comment_id(conn: sqlite3.Connection) -> str | None:
    """Return the newest stored ID, or None on a fresh or empty database.

    This is not an ingestion checkpoint: a capped batch can store the newest
    comment before it has reached all comments behind it.
    """
    initialize_db(conn)
    latest_comment = conn.execute("SELECT id FROM comments ORDER BY created_utc DESC, id DESC LIMIT 1").fetchone()
    return latest_comment[0] if latest_comment else None


def sync_user_comments(conn: sqlite3.Connection, reddit: praw.Reddit, username: str, limit: int | None) -> CommentBatch:
    """Resume an incremental scan and atomically save its content and progress.

    A legacy database with no fetch_state starts a fresh scan of accessible
    history rather than trusting MAX(created_utc), which may hide old gaps.
    New comments arriving during a backfill are picked up after it completes.
    """
    initialize_db(conn)
    state = conn.execute(
        "SELECT checkpoint_id, pending_newest_id, after_comment_id FROM fetch_state WHERE username = ?",
        (username,),
    ).fetchone()
    checkpoint_id, pending_newest_id, after_comment_id = state or (None, None, None)
    batch = _fetch_comment_batch(reddit, username, limit, checkpoint_id, after_comment_id)
    pending_newest_id = pending_newest_id or batch.newest_id
    if batch.cursor_invalid:
        # Restart safely without trusting the incomplete scan's newest ID.
        pending_newest_id = after_comment_id = None
    elif batch.complete:
        checkpoint_id = pending_newest_id or checkpoint_id
        pending_newest_id = after_comment_id = None
    else:
        after_comment_id = batch.last_id

    with conn:
        save_comments_to_db(conn, batch.comments, commit=False)
        conn.execute(
            """
            INSERT INTO fetch_state (username, checkpoint_id, pending_newest_id, after_comment_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                checkpoint_id=excluded.checkpoint_id,
                pending_newest_id=excluded.pending_newest_id,
                after_comment_id=excluded.after_comment_id
            """,
            (username, checkpoint_id, pending_newest_id, after_comment_id),
        )
    return batch


def main():
    """Fetch and archive one batch of Reddit comments."""
    load_dotenv(".secrets")
    limit = int(os.getenv("LIMIT")) if os.getenv("LIMIT") else None
    if limit is not None and limit <= 0:
        raise ValueError("LIMIT must be a positive integer or unset.")

    username = os.getenv("TARGET_USERNAME")
    if not username or not username.strip():
        raise ValueError("TARGET_USERNAME must be set.")
    reddit = praw.Reddit(
        client_id=os.getenv("CLIENT_ID"),
        client_secret=os.getenv("CLIENT_SECRET"),
        user_agent=os.getenv("USER_AGENT"),
    )
    till_last_comment = os.getenv("TILL_LAST_COMMENT", "true").lower() == "true"
    with closing(sqlite3.connect("reddit_comments.db")) as conn, conn:
        initialize_db(conn)
        if till_last_comment:
            batch = sync_user_comments(conn, reddit, username, limit)
            comments_by_submission = batch.comments
            if batch.cursor_invalid:
                print(
                    "Warning: Reddit's resume cursor is unavailable or repeated. "
                    "The checkpoint was not advanced; the next run will restart from the newest comments."
                )
            elif not batch.complete:
                print("LIMIT reached; remaining history will resume on the next run.")
            if batch.checkpoint_missing:
                print(
                    "Warning: Reddit's available listing ended before the previous checkpoint. "
                    "Archive continuity cannot be verified."
                )
        else:
            comments_by_submission = fetch_user_comments(reddit, username, limit)
            save_comments_to_db(conn, comments_by_submission)

        if comments_by_submission:
            print("Database updated with comments organized by submission.")
        else:
            print(f"No new comments found for {username}.")


if __name__ == "__main__":
    main()
