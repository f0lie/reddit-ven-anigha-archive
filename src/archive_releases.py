"""Publish complete, pinned book snapshots through draft GitHub releases.

Only semantic archive changes create releases. A failed upload leaves a draft
whose manifest pins the original commit and bytes, so a rerun never mixes
renders. Run after committing and pushing the database and all rendered books.
"""

import argparse
import datetime
import hashlib
import json
import os
import pathlib
import re
import sqlite3
import subprocess
import tempfile
from typing import Any

from src import archive_manifest
from src import render_archive

SCHEMA = 1
MARKER = "<!-- anigha-archive-release-v1\n"
TAG_PREFIX = "archive-"
CONTENT_COLUMNS = {
    "submissions": "id,title,body,author,created_at,link,subreddit",
    "comments": (
        "id,submission_id,author,created_utc,parent_id,permalink,comment_body"
    ),
}


def semantic_fingerprint(connection: sqlite3.Connection) -> str:
    """Hash meaningful archive records independently of SQLite storage layout.

    Observation timestamps, fetch checkpoints, and revision surrogate IDs do
    not represent changed archived content. Identical historical observations
    are deduplicated. The caller owns the read-only, consistent connection.

    Args:
        connection: Archive connection containing current and history tables.

    Returns:
        A lowercase SHA-256 digest of canonical semantic records.

    Raises:
        sqlite3.Error: Required archive tables or columns cannot be queried.
        ValueError: A record contains a non-finite numeric value.
    """
    digest = hashlib.sha256(b"anigha-semantic-archive-v1\n")
    for table, columns in CONTENT_COLUMNS.items():
        for suffix in ("", "_history"):
            name = table if not suffix else table.rstrip("s") + suffix
            # SQL identifiers are constants above, never archive content.
            rows = connection.execute(f"SELECT DISTINCT {columns} FROM {name}")
            encoded = sorted(
                json.dumps(
                    row,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
                for row in rows
            )
            digest.update((name + "\n").encode())
            for row in encoded:
                digest.update((row + "\n").encode())
    return digest.hexdigest()


def _git(repo: pathlib.Path, *arguments: str) -> bytes:
    """Read Git data, propagating command failures without changing the tree."""
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout


def _commit(repo: pathlib.Path, revision: str) -> str:
    """Resolve a full immutable commit, rejecting option-like revisions."""
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision):
        raise ValueError("--commit must be a full hexadecimal commit SHA")
    return (
        _git(repo, "rev-parse", "--verify", revision + "^{commit}")
        .decode()
        .strip()
    )


def _file_record(path: pathlib.Path) -> dict[str, Any]:
    """Return a file's asset name, SHA-256 digest, and size in bytes."""
    data = path.read_bytes()
    return {
        "name": path.name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }


def build_snapshot(
    repo: pathlib.Path, commit: str, stage: pathlib.Path
) -> dict[str, Any]:
    """Extract and validate a complete snapshot from immutable Git objects.

    No worktree file or renderer is used. Temporary files are written in stage,
    which must be an existing directory owned and cleaned up by the caller.

    Args:
        repo: Local Git repository containing the original snapshot commit.
        commit: Full snapshot commit SHA, not a mutable branch or tag.
        stage: Directory receiving the database and flattened book files.

    Returns:
        Manifest with schema, semantic fingerprint, commit, and sorted books.
        Each book contains name, SHA-256 digest, and byte size.

    Raises:
        ValueError: Snapshot is empty, has extra/missing books, or is invalid.
        OSError: Staged files cannot be read or written.
        sqlite3.Error: Committed database cannot be queried.
        subprocess.CalledProcessError: Commit or required blobs are unavailable.
    """
    commit = _commit(repo, commit)
    database = stage / "reddit_comments.db"
    database.write_bytes(_git(repo, "show", f"{commit}:reddit_comments.db"))
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
    try:
        connection.execute("BEGIN")
        fingerprint = semantic_fingerprint(connection)
        volumes = archive_manifest.record_manifest(connection)
    finally:
        connection.close()
    if not volumes:
        raise ValueError("Cannot release an empty archive")
    books = []
    for extension in ("epub", "pdf"):
        directory = extension + "_files"
        expected = {f"{directory}/{stem}.{extension}" for stem in volumes}
        actual = set(
            _git(repo, "ls-tree", "-r", "--name-only", commit, "--", directory)
            .decode()
            .splitlines()
        )
        actual = {name for name in actual if name.endswith("." + extension)}
        if expected != actual:
            raise ValueError(
                f"Incomplete {extension} snapshot: {expected ^ actual}"
            )
        for name in sorted(expected):
            path = stage / pathlib.PurePosixPath(name).name
            path.write_bytes(_git(repo, "show", f"{commit}:{name}"))
            render_archive.validate_output(path)
            if extension == "epub":
                render_archive.validate_epub_records(path, volumes[path.stem])
            books.append(_file_record(path))
    return {
        "schema": SCHEMA,
        "fingerprint": fingerprint,
        "commit": commit,
        "books": sorted(books, key=lambda item: item["name"]),
    }


class GitHubApi:
    """Access GitHub REST using gh, with credentials only from GITHUB_TOKEN.

    Attributes:
        repository: Validated owner/repository name for all API operations.
    """

    def __init__(self, repository: str):
        """Initialize a repository-scoped client without making network calls.

        Args:
            repository: GitHub owner/repository identifier.

        Raises:
            ValueError: Repository format is invalid or GITHUB_TOKEN is absent.
        """
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Expected GitHub owner/repository")
        if not os.environ.get("GITHUB_TOKEN"):
            raise ValueError("GITHUB_TOKEN is required for publishing")
        self.repository = repository

    def _call(self, arguments: list[str], data: bytes | None = None) -> bytes:
        """Invoke gh using GITHUB_TOKEN without logging authentication secrets.

        Args:
            arguments: gh api arguments; may perform repository mutations.
            data: Bytes sent to standard input, or None for no request body.

        Returns:
            Response bytes written to standard output.

        Raises:
            RuntimeError: gh fails; mutation outcomes may be uncertain. No
                automatic retry occurs, so a rerun must reconcile server state.
            OSError: gh cannot be executed.
        """
        environment = os.environ.copy()
        environment["GH_TOKEN"] = environment["GITHUB_TOKEN"]
        environment["GH_HOST"] = "github.com"
        result = subprocess.run(
            ["gh", "api", *arguments],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            check=False,
        )
        if result.returncode:
            # An uncertain mutation is not retried here. A later invocation
            # reads server state and either verifies it or fails closed.
            raise RuntimeError("GitHub API request failed; rerun to reconcile")
        return result.stdout

    def request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> Any:
        """Read or mutate a repository REST resource with JSON.

        Args:
            method: HTTP method, such as GET, POST, or PATCH.
            path: Repository-relative REST path beginning with a slash.
            payload: JSON object to transmit, or None for no request body.

        Returns:
            Parsed JSON response from GitHub.

        Raises:
            RuntimeError: GitHub returns an error or the operation is uncertain.
            OSError: gh cannot be executed.
            ValueError: GitHub returns invalid JSON.
        """
        arguments = [
            f"repos/{self.repository}{path}",
            "--method",
            method,
            "-H",
            "X-GitHub-Api-Version: 2022-11-28",
        ]
        data = None
        if payload is not None:
            arguments += ["--input", "-"]
            data = json.dumps(payload).encode()
        return json.loads(self._call(arguments, data))

    def list(self, path: str) -> list[dict[str, Any]]:
        """Read every page of a repository REST collection.

        Args:
            path: Repository-relative collection path without a query string.

        Returns:
            All records, preserving GitHub's page order.

        Raises:
            RuntimeError: A page request fails.
            OSError: gh cannot be executed.
            ValueError: A response is not valid JSON.
        """
        result = []
        page = 1
        while True:
            records = self.request("GET", f"{path}?per_page=100&page={page}")
            result.extend(records)
            if len(records) < 100:
                return result
            page += 1

    def upload(self, release_id: int, name: str, data: bytes) -> dict[str, Any]:
        """Upload one new asset; existing assets are never deleted or replaced.

        Args:
            release_id: Numeric draft release identifier.
            name: Trusted local asset basename containing URL-safe characters.
            data: Complete asset bytes.

        Returns:
            Created asset metadata.

        Raises:
            ValueError: Asset name is not URL-safe or response JSON is invalid.
            RuntimeError: Upload fails or its outcome is uncertain.
            OSError: gh cannot be executed.
        """
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise ValueError("Unsafe asset name")
        url = (
            f"https://uploads.github.com/repos/{self.repository}/releases/"
            f"{release_id}/assets?name={name}"
        )
        return json.loads(
            self._call(
                [
                    url,
                    "--method",
                    "POST",
                    "-H",
                    "Content-Type: application/octet-stream",
                    "-H",
                    f"Content-Length: {len(data)}",
                    "--input",
                    "-",
                ],
                data,
            )
        )

    def download(self, asset_id: int) -> bytes:
        """Download an asset for verification when GitHub omits its digest.

        Args:
            asset_id: Numeric repository release-asset identifier.

        Returns:
            Exact downloaded asset bytes.

        Raises:
            RuntimeError: Download fails.
            OSError: gh cannot be executed.
        """
        return self._call(
            [
                f"repos/{self.repository}/releases/assets/{asset_id}",
                "-H",
                "Accept: application/octet-stream",
            ]
        )


def release_manifest(release: dict[str, Any]) -> dict[str, Any] | None:
    """Read an owned release manifest, rejecting malformed archive releases.

    Args:
        release: GitHub release record. Unrelated tags are ignored.

    Returns:
        The embedded manifest, or None for unrelated releases.

    Raises:
        ValueError: An archive-prefixed release has invalid manifest metadata.
    """
    if not release.get("tag_name", "").startswith(TAG_PREFIX):
        return None
    body = release.get("body") or ""
    if MARKER not in body:
        raise ValueError("Archive release lacks its snapshot manifest")
    manifest = json.loads(body.split(MARKER, 1)[1].split("\n-->", 1)[0])
    if (
        manifest.get("schema") != SCHEMA
        or manifest.get("tag") != release["tag_name"]
        or not re.fullmatch(r"[0-9a-f]{64}", manifest.get("fingerprint", ""))
        or not re.fullmatch(
            r"[0-9a-f]{40}|[0-9a-f]{64}", manifest.get("commit", "")
        )
        or not manifest.get("books")
    ):
        raise ValueError("Invalid archive release manifest")
    return manifest


def _metadata(
    stage: pathlib.Path, manifest: dict[str, Any]
) -> list[dict[str, Any]]:
    """Write deterministic JSON and checksum assets into the staging directory.

    Args:
        stage: Caller-owned directory containing the validated book bytes.
        manifest: Pinned release metadata including tag, commit, and books.

    Returns:
        Book, JSON, and checksum asset records with names, digests, and sizes.
        SHA256SUMS covers books and JSON, excluding its own circular digest.

    Raises:
        OSError: Metadata files cannot be written or read.
    """
    path = stage / "archive-release.json"
    path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    records = [*manifest["books"], _file_record(path)]
    checksums = stage / "SHA256SUMS"
    checksums.write_text(
        "".join(
            f"{item['sha256']}  {item['name']}\n"
            for item in sorted(records, key=lambda item: item["name"])
        ),
        encoding="utf-8",
    )
    return [*records, _file_record(checksums)]


def _verify_asset(
    api: GitHubApi, asset: dict[str, Any], expected: dict[str, Any]
) -> None:
    """Require the expected bytes using GitHub's digest or a verified download.

    Args:
        api: Client used to download assets only if their digest is absent.
        asset: Server asset metadata including state, byte size, and asset ID.
        expected: Expected asset name, SHA-256 digest, and size in bytes.

    Raises:
        ValueError: Upload state, size, or digest differs from expectations.
        RuntimeError: A required asset download fails.
        OSError: gh cannot be executed.
    """
    if (
        asset.get("state") != "uploaded"
        or asset.get("size") != expected["size"]
    ):
        raise ValueError(f"Incomplete or conflicting asset: {expected['name']}")
    digest = asset.get("digest")
    if digest is None:
        digest = (
            "sha256:" + hashlib.sha256(api.download(asset["id"])).hexdigest()
        )
    if digest != "sha256:" + expected["sha256"]:
        raise ValueError(f"Conflicting asset digest: {expected['name']}")


def _verify_tag(api: GitHubApi, tag: str, commit: str) -> None:
    """Resolve a remote lightweight or annotated tag to its pinned commit.

    Args:
        api: Repository-scoped read client.
        tag: Trusted release tag name to resolve.
        commit: Full expected snapshot commit SHA.

    Raises:
        ValueError: The tag has a cyclic chain or points to another object.
        RuntimeError: A tag lookup fails, including a missing tag.
        OSError: gh cannot be executed.
    """
    reference = api.request("GET", f"/git/ref/tags/{tag}")["object"]
    seen = set()
    while reference["type"] == "tag":
        if reference["sha"] in seen:
            raise ValueError("Cyclic annotated release tag")
        seen.add(reference["sha"])
        reference = api.request("GET", f"/git/tags/{reference['sha']}")[
            "object"
        ]
    if reference["type"] != "commit" or reference["sha"] != commit:
        raise ValueError("Release tag does not point to the pinned snapshot")


def _ensure_tag(api: GitHubApi, tag: str, commit: str) -> None:
    """Create a missing tag and verify it without overwriting existing refs.

    Args:
        api: Repository-scoped client allowed to create tag references.
        tag: Trusted release tag name to create or verify.
        commit: Full expected snapshot commit SHA.

    Raises:
        ValueError: An existing or concurrently created tag points elsewhere.
        RuntimeError: GitHub request fails or its outcome is uncertain.
        OSError: gh cannot be executed.
    """
    references = api.request("GET", "/git/matching-refs/tags/" + tag)
    if not any(item["ref"] == "refs/tags/" + tag for item in references):
        api.request(
            "POST", "/git/refs", {"ref": "refs/tags/" + tag, "sha": commit}
        )
    _verify_tag(api, tag, commit)


def _complete_assets(
    api: GitHubApi,
    release: dict[str, Any],
    stage: pathlib.Path,
    expected: list[dict[str, Any]],
) -> None:
    """Preflight existing assets, upload missing draft assets, and verify all.

    Published assets are never changed. Upload failures leave the draft and
    successfully uploaded assets intact for a later original-snapshot retry.

    Args:
        api: Repository-scoped API client.
        release: Draft or published release whose assets will be verified.
        stage: Caller-owned directory containing the expected asset bytes.
        expected: Complete asset inventory with names, SHA-256 digests, sizes.

    Raises:
        ValueError: An asset conflicts, is unexpected, or is incomplete.
        RuntimeError: GitHub request fails or its outcome is uncertain.
        OSError: Staged assets or gh cannot be accessed.
    """
    path = f"/releases/{release['id']}/assets"
    assets = api.list(path)
    names = [asset["name"] for asset in assets]
    wanted = {item["name"] for item in expected}
    if len(names) != len(set(names)) or set(names) - wanted:
        raise ValueError("Duplicate or unexpected release assets")
    existing = {asset["name"]: asset for asset in assets}
    for item in expected:
        if item["name"] in existing:
            _verify_asset(api, existing[item["name"]], item)
    for item in expected:
        asset = existing.get(item["name"])
        if asset is None:
            if not release["draft"]:
                raise ValueError(
                    "Published release is missing an expected asset"
                )
            asset = api.upload(
                release["id"], item["name"], (stage / item["name"]).read_bytes()
            )
        _verify_asset(api, asset, item)
    assets = api.list(path)
    if (
        len(assets) != len(expected)
        or {item["name"] for item in assets} != wanted
    ):
        raise ValueError("Release asset set changed during verification")
    by_name = {item["name"]: item for item in expected}
    for asset in assets:
        _verify_asset(api, asset, by_name[asset["name"]])


def _body(manifest: dict[str, Any]) -> str:
    """Describe the pinned snapshot and retain machine-readable metadata."""
    return (
        "Complete yearly PDF and EPUB archive. SHA256SUMS verifies all books "
        "and archive-release.json.\n\n"
        f"Snapshot commit: {manifest['commit']}\n\n"
        + MARKER
        + json.dumps(manifest, sort_keys=True)
        + "\n-->\n"
    )


def _choose_release(
    api: GitHubApi, manifest: dict[str, Any], today: datetime.date
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Select the latest equivalent release or create a uniquely named draft.

    Existing published releases and tags are never overwritten. A selected
    draft may pin an older commit with different renderer bytes. Only the latest
    published archive fingerprint suppresses a new release after a reversion.

    Args:
        api: Repository-scoped client used to list and create tags/releases.
        manifest: Current validated snapshot metadata, without a release tag.
        today: UTC date used in a new tag and release title.

    Returns:
        A pair of the selected release record and its pinned manifest. Existing
        manifests take precedence over the caller's current render bytes.

    Raises:
        ValueError: Existing archive metadata or the chosen tag conflicts.
        RuntimeError: GitHub request fails or its outcome is uncertain.
        OSError: gh cannot be executed.
    """
    releases = api.list("/releases")
    owned = [(release, release_manifest(release)) for release in releases]
    owned = [(release, saved) for release, saved in owned if saved is not None]
    published = sorted(
        (
            (release, saved)
            for release, saved in owned
            if not release["draft"] and not release.get("prerelease", False)
        ),
        key=lambda pair: (pair[0].get("published_at") or "", pair[0]["id"]),
        reverse=True,
    )
    if published and published[0][1]["fingerprint"] == manifest["fingerprint"]:
        return published[0]
    drafts = [
        (release, saved)
        for release, saved in owned
        if release["draft"] and saved["fingerprint"] == manifest["fingerprint"]
    ]
    if drafts:
        return min(drafts, key=lambda pair: pair[0]["id"])
    base = f"{TAG_PREFIX}{today.isoformat()}-{manifest['fingerprint'][:16]}"
    occupied = {release["tag_name"] for release in releases}
    occupied.update(
        item["ref"].removeprefix("refs/tags/")
        for item in api.request("GET", "/git/matching-refs/tags/" + base)
    )
    tag = base
    sequence = 2
    while tag in occupied:
        tag = f"{base}-{sequence}"
        sequence += 1
    manifest = {**manifest, "tag": tag}
    # Pin the tag explicitly: target_commitish is ignored for existing tags.
    _ensure_tag(api, tag, manifest["commit"])
    release = api.request(
        "POST",
        "/releases",
        {
            "tag_name": tag,
            "target_commitish": manifest["commit"],
            "name": (
                f"Archive {today.isoformat()} ({manifest['fingerprint'][:12]})"
            ),
            "body": _body(manifest),
            "draft": True,
            "prerelease": False,
        },
    )
    return release, manifest


def publish_archive(
    repo: pathlib.Path,
    commit: str,
    api: GitHubApi,
    today: datetime.date | None = None,
) -> str:
    """Publish changed content, resume a draft, or verify an existing release.

    Calls GitHub to create a tag and draft, upload missing assets, and publish
    only after every expected asset is verified. Conflicts never overwrite
    existing data. Failures preserve drafts. Retries use the original commit,
    including its original render bytes even if current renders differ. All
    existing pending drafts are completed oldest-first before comparing and
    publishing the current snapshot. A conflicting old draft blocks publication.

    Args:
        repo: Repository containing the current and any pending snapshot.
        commit: Exact committed and pushed database/book snapshot SHA.
        api: Repository-scoped GitHub client; injectable for offline tests.
        today: UTC release date, defaulting to today's UTC date.

    Returns:
        Human-readable publication or unchanged-release result.

    Raises:
        ValueError: Snapshot metadata or assets are incomplete or conflict.
        RuntimeError: GitHub API operation fails or has an uncertain outcome.
        OSError: Temporary files or gh cannot be accessed.
        sqlite3.Error: Archive database cannot be read.
        subprocess.CalledProcessError: An original commit is unavailable.
    """
    today = today or datetime.datetime.now(datetime.UTC).date()
    with tempfile.TemporaryDirectory(prefix="archive-release-") as temporary:
        stage = pathlib.Path(temporary)
        current = build_snapshot(repo, commit, stage)
        # Snapshot the recovery queue once. New concurrent drafts are left
        # for a later run rather than creating an unbounded recovery loop.
        pending = []
        for release in api.list("/releases"):
            manifest = release_manifest(release)
            if manifest is not None and release["draft"]:
                pending.append((release, manifest))
        for release, manifest in sorted(
            pending, key=lambda pair: pair[0]["id"]
        ):
            recovery = stage / f"recovery-{release['id']}"
            recovery.mkdir()
            _finish_release(repo, api, release, manifest, recovery)
        release, manifest = _choose_release(api, current, today)
        selected = stage / "selected"
        selected.mkdir()
        return _finish_release(repo, api, release, manifest, selected)


def _finish_release(
    repo: pathlib.Path,
    api: GitHubApi,
    release: dict[str, Any],
    manifest: dict[str, Any],
    stage: pathlib.Path,
) -> str:
    """Verify original Git bytes and complete one pinned release safely.

    Args:
        repo: Repository with the pinned commit available locally.
        api: Repository-scoped API client.
        release: Existing draft or published release record.
        manifest: Expected pinned metadata from its release body.
        stage: Empty caller-owned staging directory for original snapshot bytes.

    Returns:
        Human-readable publication or unchanged status.

    Raises:
        ValueError: Saved metadata, tag, or assets conflict with the snapshot.
        RuntimeError: A GitHub operation fails or has an uncertain outcome.
        OSError: Temporary files or gh cannot be accessed.
        sqlite3.Error: The original archive cannot be queried.
        subprocess.CalledProcessError: Original commit objects are unavailable.
    """
    restored = build_snapshot(repo, manifest["commit"], stage)
    if restored != {
        key: value for key, value in manifest.items() if key != "tag"
    }:
        raise ValueError("Original snapshot differs from the release manifest")
    release = api.request("GET", f"/releases/{release['id']}")
    if release_manifest(release) != manifest:
        raise ValueError("Release metadata changed before asset verification")
    _verify_tag(api, manifest["tag"], manifest["commit"])
    _complete_assets(api, release, stage, _metadata(stage, manifest))
    # Re-read immediately before publishing: never modify a release that
    # another actor published or repointed while uploads were in flight.
    latest = api.request("GET", f"/releases/{release['id']}")
    if release_manifest(latest) != manifest:
        raise ValueError("Release metadata changed during publication")
    _verify_tag(api, manifest["tag"], manifest["commit"])
    if not latest["draft"]:
        return f"Unchanged archive: {manifest['tag']}"
    published = api.request(
        "PATCH", f"/releases/{release['id']}", {"draft": False}
    )
    if (
        published.get("draft") is not False
        or release_manifest(published) != manifest
    ):
        raise ValueError("Publication response does not confirm the snapshot")
    return f"Published archive: {manifest['tag']}"


def main() -> None:
    """Publish the pinned CI snapshot using environment credentials."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--repo", type=pathlib.Path, default=pathlib.Path.cwd())
    arguments = parser.parse_args()
    api = GitHubApi(os.environ.get("GITHUB_REPOSITORY", ""))
    print(publish_archive(arguments.repo.resolve(), arguments.commit, api))


if __name__ == "__main__":
    main()
