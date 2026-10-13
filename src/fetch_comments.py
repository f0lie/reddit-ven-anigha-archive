"""Preserve Reddit comment snapshots and resume incremental archive scans."""

import contextlib
import dataclasses
import datetime
import os
import sqlite3
from typing import Any

import dotenv
import praw
import tqdm


@dataclasses.dataclass
class CommentBatch:
    """A fetched comment batch and the evidence needed to advance its cursor.

    Attributes:
        comments: Submission IDs mapped to dictionaries with title, body,
            author, created_at (Unix seconds), link, subreddit, and comments.
            The nested comments lists hold original PRAW objects in
            newest-first listing order; missing authors are None.
        newest_id: First included comment's bare ID, or None for an empty batch.
        last_id: Last included comment's bare ID, or None for an empty batch.
            Neither boundary includes a checkpoint or lookahead comment.
        complete: Whether the listing ended or the checkpoint was reached.
            This describes Reddit's accessible listing, not all past comments.
        checkpoint_missing: Whether a completed listing ended without finding
            the requested checkpoint; always False if no checkpoint was given.
        cursor_invalid: Whether the resume ID reappeared in the listing or a
            resumed listing was empty. Such a batch must not advance a
            checkpoint, even if comments were collected before the cursor.
    """

    comments: dict[str, dict[str, Any]]
    newest_id: str | None
    last_id: str | None
    complete: bool
    checkpoint_missing: bool
    cursor_invalid: bool = False


# Keep listing boundaries and cursor validation together for review.
# pylint: disable-next=too-many-locals
def _fetch_comment_batch(
    reddit: praw.Reddit,
    username: str,
    limit: int | None,
    till_comment_id: str | None = None,
    after_comment_id: str | None = None,
) -> CommentBatch:
    """Fetch one batch and distinguish a capped scan from a completed scan.

    Makes Reddit requests, including lazy submission lookups, and displays a
    progress bar. A finite limit requests one extra comment to detect listing
    exhaustion; that lookahead is not returned. An exception discards the
    partial result. No database state is written.

    Args:
        reddit: Configured PRAW client used to read the user's comments.
        username: Reddit username whose newest-first listing is read.
        limit: Maximum comments to include, or None for no client-side cap.
            Reddit's own listing/history limits still apply.
        till_comment_id: Bare comment ID at which to stop, excluding that
            comment, or None to read without a checkpoint.
        after_comment_id: Bare comment ID used as an exclusive resume cursor,
            or None to start at the newest comment.

    Returns:
        Grouped comments and scan-status flags in a CommentBatch. Included
        comments retain their original PRAW object identities.

    Raises:
        ValueError: If limit is not None and is zero or negative.
        praw.exceptions.PRAWException: If PRAW cannot process the request.
        prawcore.exceptions.PrawcoreException: If Reddit authentication,
            transport, or API access fails.
    """
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
        tqdm.tqdm(
            user.comments.new(**listing_kwargs),
            total=request_limit,
            desc=f"Fetching comments for {username}",
        ),
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
        checkpoint_missing=complete
        and till_comment_id is not None
        and not checkpoint_reached,
        cursor_invalid=cursor_invalid,
    )


def fetch_user_comments(
    reddit: praw.Reddit,
    username: str,
    limit: int | None,
    till_comment_id: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Fetch comments grouped by submission, stopping before an optional ID.

    Makes Reddit requests and displays a progress bar. Use sync_user_comments
    when finite batches need persistent resume state. This function does not
    save data or expose whether the available history was exhausted.

    Args:
        reddit: Configured PRAW client used to read comments and submissions.
        username: Reddit username whose newest-first listing is read.
        limit: Maximum comments to include, or None for no client-side cap.
            A finite cap allows one additional, unreturned lookahead comment.
            Reddit's own listing/history limits still apply.
        till_comment_id: Bare comment ID to stop before, or None for no stop ID.

    Returns:
        A dictionary keyed by submission ID. Values contain title, body,
        author (a username or None), created_at (Unix seconds), link, subreddit,
        and comments (original PRAW objects in newest-first order). An empty
        result is {}. The dictionaries and lists are newly allocated.

    Raises:
        ValueError: If limit is not None and is zero or negative.
        praw.exceptions.PRAWException: If PRAW cannot process the request.
        prawcore.exceptions.PrawcoreException: If Reddit authentication,
            transport, or API access fails. No partial result is returned.
    """
    return _fetch_comment_batch(
        reddit, username, limit, till_comment_id
    ).comments


def initialize_db(conn: sqlite3.Connection) -> None:
    """Create missing archive, history, and per-user scan-state tables.

    Existing tables and records are left unchanged; this is not a schema
    migration. No commit or rollback is issued. DDL follows the connection's
    transaction settings, so tables created before an error may remain.

    Args:
        conn: Open, writable SQLite connection; ownership stays with the caller.

    Raises:
        sqlite3.Error: If SQLite cannot create or access the required tables.
    """
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
    """Check whether text represents absent or removed Reddit content.

    Args:
        value: Text to check, or None. Whitespace and case are ignored when
            comparing against the known deletion/removal markers.

    Returns:
        True for None, blank text, or a recognized marker; otherwise False.
        The input is not changed.
    """
    return value is None or value.strip().lower() in {
        "",
        "[deleted]",
        "[removed]",
        "[deleted by user]",
        "[removed by reddit]",
    }


# Paired table names and ordered fields describe one snapshot write.
# pylint: disable-next=too-many-arguments,too-many-positional-arguments
def _save_snapshot(
    conn: sqlite3.Connection,
    table: str,
    history_table: str,
    columns: tuple[str, ...],
    values: tuple[Any, ...],
    protected_fields: tuple[str, ...],
) -> None:
    """Upsert a snapshot while preserving useful text and prior versions.

    Protected text survives incoming deletion placeholders. A change to fields
    other than updated_at archives the previous row using the new timestamp
    as archived_at. Identical content leaves both tables, including updated_at,
    unchanged. Input tuples are not mutated. No commit or rollback is issued;
    the caller must handle a partially applied write if an error occurs.

    Args:
        conn: Open SQLite connection with the current and history tables.
        table: Trusted internal name of the current-snapshot table.
        history_table: Trusted internal name of its matching history table.
        columns: Trusted column names in values order, with id first and
            updated_at last. Identifiers are interpolated, not SQL parameters.
        values: Column values for the new snapshot. The final value is a Unix
            timestamp in seconds; protected text values are strings or None.
        protected_fields: Names in columns whose existing useful text must
            survive an incoming deletion placeholder.

    Raises:
        sqlite3.Error: If reading, archiving, or upserting a snapshot fails.
    """
    column_names = ", ".join(columns)
    existing = conn.execute(
        f"SELECT {column_names} FROM {table} WHERE id = ?", (values[0],)
    ).fetchone()
    if existing:
        merged = list(values)
        for field in protected_fields:
            index = columns.index(field)
            if _is_placeholder(merged[index]) and not _is_placeholder(
                existing[index]
            ):
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

    assignments = ", ".join(
        f"{column}=excluded.{column}" for column in columns[1:]
    )
    conn.execute(
        f"INSERT INTO {table} ({column_names}) "
        f"VALUES ({', '.join('?' for _ in columns)}) "
        f"ON CONFLICT(id) DO UPDATE SET {assignments}",
        values,
    )


def save_comments_to_db(
    conn: sqlite3.Connection,
    comments: dict[str, dict[str, Any]],
    *,
    commit: bool = True,
) -> None:
    """Save current snapshots and retain prior versions of edited content.

    Creates missing tables and protects useful titles, bodies, and authors
    from deletion placeholders. All writes in this call share one Unix-seconds
    update timestamp. The input dictionaries, lists, and comment fields are
    not replaced. Accessing lazy PRAW attributes can make network requests and
    populate PRAW caches. Errors propagate without a local rollback; earlier
    writes may be pending
    or persisted, depending on the connection's transaction mode.

    Args:
        conn: Open, writable SQLite connection; it is not closed here.
        comments: Submission-ID mapping in fetch_user_comments's result shape.
            Each value must contain title, body, author, created_at (Unix
            seconds), link, subreddit, and an iterable of PRAW comments.
            Comment creation timestamps also use Unix seconds.
        commit: Whether to commit the connection after saving all records.
            True also commits any unrelated pending work on this connection.
            False leaves transaction completion to the caller.

    Raises:
        KeyError: If a submission dictionary lacks a required field.
        sqlite3.Error: If schema creation, snapshot storage, or commit fails.
        praw.exceptions.PRAWException: If lazy PRAW data access fails locally.
        prawcore.exceptions.PrawcoreException: If lazy PRAW data access fails
            because of authentication, transport, or API access.
    """
    initialize_db(conn)
    now = datetime.datetime.now().timestamp()
    for submission_id, submission_data in comments.items():
        _save_snapshot(
            conn,
            "submissions",
            "submission_history",
            (
                "id",
                "title",
                "body",
                "author",
                "created_at",
                "link",
                "subreddit",
                "updated_at",
            ),
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
    """Return the newest stored comment ID, creating missing tables first.

    This is not an ingestion checkpoint: a capped batch can store the newest
    comment before it has reached all comments behind it. No commit or rollback
    is issued, but schema creation can persist under SQLite's DDL semantics.

    Args:
        conn: Open SQLite connection that can create any missing archive tables.

    Returns:
        The ID with the greatest created_utc value, breaking ties by descending
        ID, or None on a fresh or empty database. Comments from all users count.

    Raises:
        sqlite3.Error: If schema creation or the query fails.
    """
    initialize_db(conn)
    latest_comment = conn.execute(
        "SELECT id FROM comments ORDER BY created_utc DESC, id DESC LIMIT 1"
    ).fetchone()
    return latest_comment[0] if latest_comment else None


def sync_user_comments(
    conn: sqlite3.Connection,
    reddit: praw.Reddit,
    username: str,
    limit: int | None,
) -> CommentBatch:
    """Resume one incremental batch and save its content and scan state.

    Missing per-user state starts a scan of accessible history instead of
    trusting the newest archived timestamp. A completed scan advances the
    checkpoint; an invalid cursor clears pending progress for a fresh retry.
    New comments arriving during backfill are read after that scan completes.

    Creates missing tables before reading Reddit and displays fetch progress.
    Fetch failures leave stored content and progress untouched, though schema
    creation may persist. With a transaction-enabled connection, content,
    history, and cursor writes commit together or roll back together. The
    connection context also commits or rolls back any pre-existing pending
    transaction; callers should supply a connection without unrelated writes.

    Args:
        conn: Open, writable SQLite connection with transactions enabled, not
            autocommit mode. The connection remains open after this call.
        reddit: Configured PRAW client used to fetch comments and submissions.
        username: Reddit username. Persisted scan-state lookup is
            case-insensitive.
        limit: Maximum comments to include in this batch, or None for no
            client-side cap. Reddit's own listing/history limits still apply.

    Returns:
        The fetched CommentBatch, including completion and cursor flags. Its
        status describes the fetch; an invalid cursor schedules a restart even
        when some comments from that batch were saved.

    Raises:
        ValueError: If limit is not None and is zero or negative.
        sqlite3.Error: If schema creation, state lookup, writes, or commit fail.
        praw.exceptions.PRAWException: If PRAW cannot process the request.
        prawcore.exceptions.PrawcoreException: If Reddit authentication,
            transport, or API access fails.
    """
    initialize_db(conn)
    state = conn.execute(
        "SELECT checkpoint_id, pending_newest_id, after_comment_id "
        "FROM fetch_state WHERE username = ?",
        (username,),
    ).fetchone()
    checkpoint_id, pending_newest_id, after_comment_id = state or (
        None,
        None,
        None,
    )
    batch = _fetch_comment_batch(
        reddit, username, limit, checkpoint_id, after_comment_id
    )
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
            INSERT INTO fetch_state (
                username, checkpoint_id, pending_newest_id, after_comment_id
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                checkpoint_id=excluded.checkpoint_id,
                pending_newest_id=excluded.pending_newest_id,
                after_comment_id=excluded.after_comment_id
            """,
            (username, checkpoint_id, pending_newest_id, after_comment_id),
        )
    return batch


def main() -> None:
    """Fetch and archive one configured batch in the current directory.

    Loads .secrets into the process environment without overriding existing
    variables. Reads CLIENT_ID, CLIENT_SECRET, USER_AGENT, and TARGET_USERNAME
    for Reddit access. LIMIT is an optional positive comment count.
    TILL_LAST_COMMENT defaults to true; only a case-insensitive "true" enables
    persistent incremental scans. Other values fetch from the listing head.

    Opens or creates reddit_comments.db, updates snapshots and scan state as
    applicable, and prints status or continuity warnings. Fetching also writes
    a progress bar. The database connection is closed on success or failure;
    database work follows the transaction behavior of the ingestion helpers.

    Raises:
        ValueError: If LIMIT is not an integer greater than zero or the target
            username is missing or blank.
        sqlite3.Error: If opening or updating the archive fails.
        praw.exceptions.PRAWException: If Reddit client configuration or local
            request processing fails.
        prawcore.exceptions.PrawcoreException: If Reddit authentication,
            transport, or API access fails.
    """
    dotenv.load_dotenv(".secrets")
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
    with contextlib.closing(sqlite3.connect("reddit_comments.db")) as conn:
        with conn:
            initialize_db(conn)
            if till_last_comment:
                batch = sync_user_comments(conn, reddit, username, limit)
                comments_by_submission = batch.comments
                if batch.cursor_invalid:
                    print(
                        "Warning: Reddit's resume cursor is unavailable "
                        "or repeated. "
                        "The checkpoint was not advanced; the next run will "
                        "restart from the newest comments."
                    )
                elif not batch.complete:
                    print(
                        "LIMIT reached; remaining history will resume "
                        "on the next run."
                    )
                if batch.checkpoint_missing:
                    print(
                        "Warning: Reddit's available listing ended before "
                        "the previous checkpoint. "
                        "Archive continuity cannot be verified."
                    )
            else:
                comments_by_submission = fetch_user_comments(
                    reddit, username, limit
                )
                save_comments_to_db(conn, comments_by_submission)

            if comments_by_submission:
                print("Database updated with comments organized by submission.")
            else:
                print(f"No new comments found for {username}.")


if __name__ == "__main__":
    main()
