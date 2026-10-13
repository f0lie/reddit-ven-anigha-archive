"""Exercise release idempotence and recovery without contacting GitHub."""

from __future__ import annotations

import contextlib
import copy
import datetime
import hashlib
import pathlib
import sqlite3
import subprocess
from typing import Any
import zipfile

import pytest

from src import archive_releases
from src import fetch_comments

VOLUME = "ven_anigha_reddit_archive_2026"
TODAY = datetime.date(2026, 10, 13)


def populate_archive(connection: sqlite3.Connection) -> None:
    """Insert a tiny archive into a caller-owned temporary connection."""
    fetch_comments.initialize_db(connection)
    connection.execute(
        "INSERT INTO submissions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "post1",
            "A title",
            "A body",
            "writer",
            1767225600,
            "https://reddit.com/post1",
            "example",
            100,
        ),
    )
    connection.execute(
        "INSERT INTO comments VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "reply1",
            "post1",
            "Bhikkhu_Anigha",
            1767225601,
            None,
            "/comments/post1/reply1",
            "Preserved teaching",
            100,
        ),
    )
    connection.execute(
        "INSERT INTO comment_history SELECT NULL, id, submission_id, author, "
        "created_utc, parent_id, permalink, 'Earlier teaching', "
        "updated_at, 101 FROM comments"
    )
    connection.commit()


def test_fingerprint_ignores_polling_timestamps_and_checkpoint_state() -> None:
    with contextlib.closing(sqlite3.connect(":memory:")) as connection:
        populate_archive(connection)
        original = archive_releases.semantic_fingerprint(connection)
        connection.execute("UPDATE submissions SET updated_at = 999")
        connection.execute("UPDATE comments SET updated_at = 999")
        connection.execute(
            "UPDATE comment_history SET updated_at = 999, archived_at = 1000"
        )
        connection.execute(
            "INSERT INTO fetch_state VALUES ('user', 'new', 'pending', 'after')"
        )
        assert archive_releases.semantic_fingerprint(connection) == original
        assert len(original) == 64


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE submissions SET body = 'New discussion'",
        "UPDATE submissions SET title = 'New title'",
        "UPDATE comments SET comment_body = 'Changed teaching'",
        "UPDATE comment_history SET comment_body = 'Changed old teaching'",
        "DELETE FROM comment_history",
        "UPDATE comments SET parent_id = 'reply2'",
    ],
)
def test_fingerprint_includes_content_and_preserved_revisions(
    statement: str,
) -> None:
    with contextlib.closing(sqlite3.connect(":memory:")) as connection:
        populate_archive(connection)
        original = archive_releases.semantic_fingerprint(connection)
        connection.execute(statement)
        assert archive_releases.semantic_fingerprint(connection) != original


def test_fingerprint_ignores_history_row_ids_and_duplicate_observations() -> (
    None
):
    with contextlib.closing(sqlite3.connect(":memory:")) as connection:
        populate_archive(connection)
        original = archive_releases.semantic_fingerprint(connection)
        connection.execute("UPDATE comment_history SET history_id = 42")
        connection.execute(
            "INSERT INTO comment_history SELECT NULL, id, submission_id, "
            "author, created_utc, parent_id, permalink, comment_body, "
            "200, 201 FROM comment_history"
        )
        assert archive_releases.semantic_fingerprint(connection) == original


def test_fingerprint_ignores_physical_record_order() -> None:
    with contextlib.closing(sqlite3.connect(":memory:")) as connection:
        populate_archive(connection)
        connection.execute(
            "INSERT INTO comments SELECT 'reply2', submission_id, author, "
            "created_utc, parent_id, permalink, 'Another teaching', "
            "updated_at FROM comments"
        )
        original = archive_releases.semantic_fingerprint(connection)
        rows = connection.execute("SELECT * FROM comments").fetchall()
        connection.execute("DELETE FROM comments")
        connection.executemany(
            "INSERT INTO comments VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            reversed(rows),
        )
        assert archive_releases.semantic_fingerprint(connection) == original


def git(repo: pathlib.Path, *arguments: str) -> str:
    """Run Git against the isolated test repository and return its stdout.

    Args:
        repo: Temporary repository; never the checkout under test.
        *arguments: Arguments to the Git executable.

    Returns:
        Stripped standard output.

    Raises:
        subprocess.CalledProcessError: The fixture command fails.
    """
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def commit_archive(repo: pathlib.Path, text: str = "Preserved teaching") -> str:
    """Commit deterministic fixture outputs and return the new commit ID."""
    for directory, suffix, prefix in (
        ("markdown_files", "md", b"# Archive\n"),
        ("epub_files", "epub", b"PK\x03\x04"),
        ("pdf_files", "pdf", b"%PDF-1.4\n"),
    ):
        target = repo / directory / f"{VOLUME}.{suffix}"
        target.parent.mkdir(exist_ok=True)
        if suffix == "epub":
            with zipfile.ZipFile(target, "w") as archive:
                archive.writestr("mimetype", "application/epub+zip")
                archive.writestr("META-INF/container.xml", "<container/>")
                archive.writestr(
                    "book.xhtml",
                    '<html><h1 id="discussion-post1">Title</h1>'
                    '<p id="comment-reply1">' + text + "</p></html>",
                )
        else:
            target.write_bytes(prefix + text.encode() + b"\n%%EOF")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Archive fixture")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def archive_repo(tmp_path: pathlib.Path) -> pathlib.Path:
    """Create an isolated local repository with one committed archive year.

    Args:
        tmp_path: Pytest-owned temporary directory, removed after the test.

    Returns:
        Local repository path. No remotes or credentials are configured.
    """
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.name", "Release Test")
    git(tmp_path, "config", "user.email", "release@example.invalid")
    with contextlib.closing(
        sqlite3.connect(tmp_path / "reddit_comments.db")
    ) as connection:
        populate_archive(connection)
    commit_archive(tmp_path)
    return tmp_path


def change_archive(repo: pathlib.Path, text: str) -> str:
    """Commit changed database content and matching test-only book bytes."""
    with contextlib.closing(
        sqlite3.connect(repo / "reddit_comments.db")
    ) as connection:
        connection.execute("UPDATE comments SET comment_body = ?", (text,))
        connection.commit()
    return commit_archive(repo, text)


class FakeGitHub:
    """In-memory GitHub boundary that forbids mutating published releases.

    Attributes:
        releases: Release API records, newest first.
        tags: Full tag references mapped to commit IDs.
        writes: Recorded non-read operations for no-op assertions.
        upload_failure: If set, fail once when uploading this asset name.
        content: Uploaded bytes indexed by asset ID.
    """

    def __init__(self) -> None:
        """Initialize an empty server without any network transport."""
        self.releases: list[dict[str, Any]] = []
        self.tags: dict[str, str] = {}
        self.writes: list[tuple[str, str]] = []
        self.upload_failure: str | None = None
        self.content: dict[int, bytes] = {}

    def list(self, path: str) -> list[dict[str, Any]]:
        """Return deep-copied release or asset inventories."""
        if "/git/matching-refs/tags/" in path:
            prefix = "refs/tags/" + path.split("/tags/", 1)[1]
            return [
                {"ref": tag, "object": {"sha": commit, "type": "commit"}}
                for tag, commit in self.tags.items()
                if tag.startswith(prefix)
            ]
        if path.endswith("/releases") or path == "releases":
            return copy.deepcopy(self.releases)
        if path.endswith("/assets"):
            release_id = int(path.split("/")[-2])
            return copy.deepcopy(self._release(release_id)["assets"])
        raise AssertionError(f"Unexpected listing: {path}")

    def _release(self, release_id: int) -> dict[str, Any]:
        """Find the mutable internal record for a numeric release ID."""
        return next(row for row in self.releases if row["id"] == release_id)

    def upload(self, release_id: int, name: str, data: bytes) -> dict[str, Any]:
        """Store draft bytes, optionally failing once during upload."""
        release = self._release(release_id)
        assert release["draft"], "Published releases must never be changed"
        self.writes.append(("upload", name))
        if name == self.upload_failure:
            self.upload_failure = None
            raise RuntimeError("Simulated interrupted upload")
        assert name not in {asset["name"] for asset in release["assets"]}
        asset_id = max(self.content, default=0) + 1
        asset = {
            "id": asset_id,
            "name": name,
            "size": len(data),
            "state": "uploaded",
            "digest": f"sha256:{hashlib.sha256(data).hexdigest()}",
        }
        self.content[asset_id] = data
        release["assets"].append(asset)
        return copy.deepcopy(asset)

    def download(self, asset_id: int) -> bytes:
        """Return the exact stored bytes of a release asset."""
        return self.content[asset_id]

    def request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """Simulate only the REST operations authorized by the publisher."""
        if method == "GET":
            return self._read(path)
        self.writes.append((method, path))
        assert payload is not None
        if method == "POST" and path.endswith("/releases"):
            release = dict(
                payload,
                id=len(self.releases) + 1,
                assets=[],
                published_at=None,
            )
            self.releases.insert(0, release)
            return copy.deepcopy(release)
        if method == "POST" and path.endswith("/git/refs"):
            assert payload["ref"] not in self.tags
            self.tags[payload["ref"]] = payload["sha"]
            return {"ref": payload["ref"], "object": {"sha": payload["sha"]}}
        if method == "PATCH" and "/releases/" in path:
            release = self._release(int(path.rsplit("/", 1)[1]))
            assert release["draft"], "Published releases must never be changed"
            release.update(payload)
            if not release["draft"]:
                release["published_at"] = "2026-10-13T12:00:00Z"
            return copy.deepcopy(release)
        raise AssertionError(f"Unexpected write: {method} {path}")

    def _read(self, path: str) -> dict[str, Any] | list[dict[str, Any]]:
        """Resolve fake GitHub reads, rejecting unknown endpoint spellings."""
        if (
            path.endswith("/releases")
            or path.endswith("/assets")
            or "/git/matching-refs/tags/" in path
        ):
            return self.list(path)
        if "/git/ref/tags/" in path:
            tag = "refs/tags/" + path.split("/git/ref/tags/", 1)[1]
            if tag in self.tags:
                return {
                    "ref": tag,
                    "object": {
                        "sha": self.tags[tag],
                        "type": "commit",
                    },
                }
            raise AssertionError(f"Missing fake tag: {tag}")
        if "/releases/" in path:
            return copy.deepcopy(self._release(int(path.rsplit("/", 1)[1])))
        raise AssertionError(f"Unexpected read: {path}")


def publish(repo: pathlib.Path, api: FakeGitHub) -> str:
    """Publish the current fixture commit through the in-memory API only."""
    return archive_releases.publish_archive(
        repo, git(repo, "rev-parse", "HEAD"), api, today=TODAY
    )


def test_initial_publication_contains_complete_committed_edition(
    archive_repo: pathlib.Path,
) -> None:
    api = FakeGitHub()
    commit = git(archive_repo, "rev-parse", "HEAD")
    publish(archive_repo, api)
    assert len(api.releases) == 1
    release = api.releases[0]
    assert release["draft"] is False
    assert "2026-10-13" in release["tag_name"]
    assert api.tags[f"refs/tags/{release['tag_name']}"] == commit
    names = {asset["name"] for asset in release["assets"]}
    assert {f"{VOLUME}.{suffix}" for suffix in ("epub", "pdf")} <= names
    for asset in release["assets"]:
        if asset["name"].startswith(VOLUME):
            directory = {
                ".md": "markdown_files",
                ".epub": "epub_files",
                ".pdf": "pdf_files",
            }[pathlib.Path(asset["name"]).suffix]
            assert (
                api.download(asset["id"])
                == (archive_repo / directory / asset["name"]).read_bytes()
            )


def test_operational_update_and_new_render_do_not_republish(
    archive_repo: pathlib.Path,
) -> None:
    api = FakeGitHub()
    publish(archive_repo, api)
    successful = copy.deepcopy(api.releases)
    writes = list(api.writes)
    with contextlib.closing(
        sqlite3.connect(archive_repo / "reddit_comments.db")
    ) as connection:
        connection.execute("UPDATE comments SET updated_at = 99999")
        connection.execute(
            "INSERT INTO fetch_state VALUES ('user', 'new', NULL, NULL)"
        )
        connection.commit()
    commit_archive(archive_repo, "Different build metadata, same archive")
    publish(archive_repo, api)
    assert api.releases == successful
    assert api.writes == writes


def test_changed_content_creates_collision_safe_dated_edition(
    archive_repo: pathlib.Path,
) -> None:
    api = FakeGitHub()
    publish(archive_repo, api)
    first = copy.deepcopy(api.releases[0])
    commit = change_archive(archive_repo, "New archived teaching")
    publish(archive_repo, api)
    assert len(api.releases) == 2
    second = api.releases[0]
    assert api.releases[1] == first
    assert second["tag_name"] != first["tag_name"]
    assert "2026-10-13" in second["tag_name"]
    assert api.tags[f"refs/tags/{second['tag_name']}"] == commit
    assert second["draft"] is False


@pytest.mark.parametrize("next_content", [None, "Another newer teaching"])
def test_partial_upload_retry_keeps_original_committed_snapshot(
    archive_repo: pathlib.Path,
    next_content: str | None,
) -> None:
    api = FakeGitHub()
    original_commit = git(archive_repo, "rev-parse", "HEAD")
    original_bytes = {
        path.name: path.read_bytes() for path in archive_repo.glob("*_files/*")
    }
    api.upload_failure = f"{VOLUME}.pdf"
    with pytest.raises(RuntimeError, match="interrupted upload"):
        publish(archive_repo, api)
    draft_id = api.releases[0]["id"]
    assert api.releases[0]["draft"] is True
    if next_content is None:
        commit_archive(archive_repo, "Newly rendered bytes, unchanged content")
    else:
        change_archive(archive_repo, next_content)
    publish(archive_repo, api)
    assert len(api.releases) == (1 if next_content is None else 2)
    assert all(not row["draft"] for row in api.releases)
    completed = next(row for row in api.releases if row["id"] == draft_id)
    assert completed["draft"] is False
    assert api.tags[f"refs/tags/{completed['tag_name']}"] == original_commit
    for asset in completed["assets"]:
        if asset["name"] in original_bytes:
            assert api.download(asset["id"]) == original_bytes[asset["name"]]


@pytest.mark.parametrize("conflict", ["extra", "corrupt", "wrong_tag"])
def test_retry_rejects_conflicting_state_without_publishing(
    archive_repo: pathlib.Path,
    conflict: str,
) -> None:
    api = FakeGitHub()
    api.upload_failure = f"{VOLUME}.pdf"
    with pytest.raises(RuntimeError, match="interrupted upload"):
        publish(archive_repo, api)
    draft = api.releases[0]
    if conflict == "extra":
        api.upload(draft["id"], "unrecognized.txt", b"foreign content")
    elif conflict == "corrupt":
        asset = draft["assets"][0]
        api.content[asset["id"]] = b"wrong bytes"
        asset["size"] = len(b"wrong bytes")
        asset["digest"] = "sha256:" + hashlib.sha256(b"wrong bytes").hexdigest()
    else:
        api.tags[f"refs/tags/{draft['tag_name']}"] = "f" * 40
    writes = list(api.writes)
    with pytest.raises((ValueError, RuntimeError)):
        publish(archive_repo, api)
    assert api.releases[0]["draft"] is True
    assert api.writes == writes


def test_failed_draft_does_not_count_as_successful_publication(
    archive_repo: pathlib.Path,
) -> None:
    api = FakeGitHub()
    api.upload_failure = f"{VOLUME}.pdf"
    with pytest.raises(RuntimeError, match="interrupted upload"):
        publish(archive_repo, api)
    assert not any(not row["draft"] for row in api.releases)
    publish(archive_repo, api)
    assert len(api.releases) == 1
    assert api.releases[0]["draft"] is False


def test_existing_tag_collision_uses_new_name_without_retargeting(
    archive_repo: pathlib.Path,
) -> None:
    api = FakeGitHub()
    with contextlib.closing(
        sqlite3.connect(archive_repo / "reddit_comments.db")
    ) as connection:
        fingerprint = archive_releases.semantic_fingerprint(connection)
    occupied = f"archive-{TODAY.isoformat()}-{fingerprint[:16]}"
    api.tags[f"refs/tags/{occupied}"] = "f" * 40
    publish(archive_repo, api)
    assert api.tags[f"refs/tags/{occupied}"] == "f" * 40
    assert api.releases[0]["tag_name"] == occupied + "-2"
    assert api.releases[0]["draft"] is False


def test_snapshot_uses_commit_even_when_worktree_changes(
    archive_repo: pathlib.Path,
    tmp_path: pathlib.Path,
) -> None:
    commit = git(archive_repo, "rev-parse", "HEAD")
    pdf = archive_repo / "pdf_files" / f"{VOLUME}.pdf"
    original = pdf.read_bytes()
    pdf.write_bytes(b"Uncommitted broken PDF")
    stage = tmp_path / "staging"
    stage.mkdir()
    manifest = archive_releases.build_snapshot(archive_repo, commit, stage)
    assert (stage / pdf.name).read_bytes() == original
    assert manifest["commit"] == commit
    assert len(manifest["books"]) == 2


@pytest.mark.parametrize("damage", ["missing", "corrupt", "unexpected"])
def test_invalid_committed_snapshot_fails_before_api_writes(
    archive_repo: pathlib.Path,
    damage: str,
) -> None:
    target = archive_repo / "pdf_files" / f"{VOLUME}.pdf"
    if damage == "missing":
        target.unlink()
    elif damage == "corrupt":
        target.write_bytes(b"%PDF-1.4\ntruncated")
    else:
        target.with_name("ven_anigha_reddit_archive_2025.pdf").write_bytes(
            target.read_bytes()
        )
    git(archive_repo, "add", ".")
    git(archive_repo, "commit", "-m", "Broken fixture")
    api = FakeGitHub()
    with pytest.raises(ValueError):
        publish(archive_repo, api)
    assert not api.writes
    assert not api.releases


def test_published_release_with_missing_asset_is_never_repaired_in_place(
    archive_repo: pathlib.Path,
) -> None:
    api = FakeGitHub()
    publish(archive_repo, api)
    api.releases[0]["assets"].pop()
    writes = list(api.writes)
    with pytest.raises(ValueError, match="missing an expected asset"):
        publish(archive_repo, api)
    assert api.writes == writes
    assert api.releases[0]["draft"] is False


def test_existing_assets_without_github_digests_are_verified_by_download(
    archive_repo: pathlib.Path,
) -> None:
    api = FakeGitHub()
    publish(archive_repo, api)
    for asset in api.releases[0]["assets"]:
        del asset["digest"]
    writes = list(api.writes)
    publish(archive_repo, api)
    assert api.writes == writes
    asset = api.releases[0]["assets"][0]
    api.content[asset["id"]] = b"x" * asset["size"]
    with pytest.raises(ValueError, match="digest"):
        publish(archive_repo, api)
    assert api.writes == writes


def test_existing_late_asset_conflict_prevents_earlier_missing_upload(
    archive_repo: pathlib.Path,
) -> None:
    api = FakeGitHub()
    publish(archive_repo, api)
    draft = api.releases[0]
    draft["draft"] = True
    draft["published_at"] = None
    draft["assets"].pop(0)
    draft["assets"][-1]["digest"] = "sha256:" + "0" * 64
    writes = list(api.writes)
    with pytest.raises(ValueError, match="digest"):
        publish(archive_repo, api)
    assert api.writes == writes
    assert draft["draft"] is True


def test_latest_successful_release_is_selected_by_publication_time(
    archive_repo: pathlib.Path,
) -> None:
    api = FakeGitHub()
    publish(archive_repo, api)
    change_archive(archive_repo, "A new teaching")
    publish(archive_repo, api)
    newest = api.releases[0]
    newest["published_at"] = "2026-10-14T12:00:00Z"
    api.releases.reverse()
    writes = list(api.writes)
    publish(archive_repo, api)
    assert len(api.releases) == 2
    assert api.writes == writes


def test_reverting_content_publishes_new_collision_safe_edition(
    archive_repo: pathlib.Path,
) -> None:
    api = FakeGitHub()
    publish(archive_repo, api)
    first = copy.deepcopy(api.releases[0])
    change_archive(archive_repo, "A changed teaching")
    publish(archive_repo, api)
    change_archive(archive_repo, "Preserved teaching")
    publish(archive_repo, api)
    assert len(api.releases) == 3
    assert api.releases[2] == first
    assert api.releases[0]["tag_name"] == first["tag_name"] + "-2"


def test_github_api_paginates_until_short_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "fixture-token-not-a-credential")
    api = archive_releases.GitHubApi("example/archive")
    paths = []

    def request(method: str, path: str, payload: object = None) -> list:
        """Return two fake pages without making a network request."""
        assert method == "GET"
        assert payload is None
        paths.append(path)
        return (
            [{"id": value} for value in range(100)] if len(paths) == 1 else []
        )

    monkeypatch.setattr(api, "request", request)
    assert len(api.list("/releases")) == 100
    assert paths == [
        "/releases?per_page=100&page=1",
        "/releases?per_page=100&page=2",
    ]


def test_github_api_overrides_host_and_token_without_command_line_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "fixture-token-not-a-credential")
    monkeypatch.setenv("GH_TOKEN", "incorrect-fixture-token")
    monkeypatch.setenv("GH_HOST", "wrong.example.invalid")
    api = archive_releases.GitHubApi("example/archive")
    calls = []

    def run(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        """Capture subprocess settings and return a fake JSON response."""
        calls.append((arguments, kwargs))
        return subprocess.CompletedProcess(arguments, 0, b"{}", b"")

    monkeypatch.setattr(archive_releases.subprocess, "run", run)
    assert api.request("GET", "/releases/1") == {}
    arguments, kwargs = calls[0]
    assert kwargs["env"]["GH_TOKEN"] == "fixture-token-not-a-credential"
    assert kwargs["env"]["GH_HOST"] == "github.com"
    assert "fixture-token" not in " ".join(arguments)


def test_uncertain_successful_upload_is_verified_instead_of_duplicated(
    archive_repo: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeGitHub()
    real_upload = api.upload

    def interrupted_upload(
        release_id: int,
        name: str,
        data: bytes,
    ) -> dict[str, Any]:
        """Lose the response after the server has durably accepted the file."""
        real_upload(release_id, name, data)
        raise RuntimeError("Lost upload response")

    monkeypatch.setattr(api, "upload", interrupted_upload)
    with pytest.raises(RuntimeError, match="Lost upload response"):
        publish(archive_repo, api)
    uploaded_name = api.releases[0]["assets"][0]["name"]
    monkeypatch.setattr(api, "upload", real_upload)
    publish(archive_repo, api)
    assert api.writes.count(("upload", uploaded_name)) == 1
    assert api.releases[0]["draft"] is False


def test_upload_sets_binary_content_length_for_stdin_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "fixture-token-not-a-credential")
    api = archive_releases.GitHubApi("example/archive")
    calls = []

    def run(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        """Capture gh upload arguments without contacting a remote host."""
        calls.append((arguments, kwargs))
        return subprocess.CompletedProcess(arguments, 0, b'{"id": 1}', b"")

    monkeypatch.setattr(archive_releases.subprocess, "run", run)
    payload = b"binary\x00\xff\n"
    assert api.upload(7, "fixture.epub", payload) == {"id": 1}
    arguments, kwargs = calls[0]
    assert arguments[:2] == ["gh", "api"]
    assert arguments[2] == (
        "https://uploads.github.com/repos/example/archive/"
        "releases/7/assets?name=fixture.epub"
    )
    assert f"Content-Length: {len(payload)}" in arguments
    assert "Content-Type: application/octet-stream" in arguments
    assert kwargs["input"] == payload
