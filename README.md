# Ven. Anīgha Reddit Archive

An archive of available comments by [Venerable Anīgha on Reddit](https://www.reddit.com/user/Bhikkhu_Anigha/comments/), with the original submission questions and links back to Reddit. Read it online as Markdown, or download a PDF or EPUB.

## Read the archive

| Year | PDF | EPUB | Markdown |
| --- | --- | --- | --- |
| 2026 | [Download PDF](pdf_files/ven_anigha_reddit_archive_2026.pdf?raw=true) | [Download EPUB](epub_files/ven_anigha_reddit_archive_2026.epub?raw=true) | [Read online](markdown_files/ven_anigha_reddit_archive_2026.md) |
| 2025 | [Download PDF](pdf_files/ven_anigha_reddit_archive_2025.pdf?raw=true) | [Download EPUB](epub_files/ven_anigha_reddit_archive_2025.epub?raw=true) | [Read online](markdown_files/ven_anigha_reddit_archive_2025.md) |
| 2024 | [Download PDF](pdf_files/ven_anigha_reddit_archive_2024.pdf?raw=true) | [Download EPUB](epub_files/ven_anigha_reddit_archive_2024.epub?raw=true) | [Read online](markdown_files/ven_anigha_reddit_archive_2024.md) |
| 2023 | [Download PDF](pdf_files/ven_anigha_reddit_archive_2023.pdf?raw=true) | [Download EPUB](epub_files/ven_anigha_reddit_archive_2023.epub?raw=true) | [Read online](markdown_files/ven_anigha_reddit_archive_2023.md) |

The SQLite database, `reddit_comments.db`, is the source for the generated books. Discussions are grouped by the original submission's year in UTC, so a book can contain comments made in a later year.

### Scope and freshness

- This is a snapshot of what the fetcher has been able to retrieve and retain, not a guarantee that every comment or complete conversation is present. Reddit's available listings, deletions, and fetch limits affect coverage.
- The archive collects the target account's comments and their original submissions. Replies from other people and intermediate parent comments may be missing; the books mark replies whose parent is unavailable.
- A generation timestamp describes when a book was built. It does not prove that its source data is current, that every past edit was captured, or that the archive is complete. Rendering the checked-in database does not contact Reddit.
- Previously captured text is retained when a later fetch returns a removal marker. Text already missing before capture cannot be recovered by this project. Routine incremental fetching is not a revision history of all old comments.
- Quotation markers come from Reddit. A missing marker in the original can make quoted text look like the author's words; follow the discussion link when context is unclear.

The updater is scheduled for Sunday at 00:00 UTC. GitHub schedules can be delayed, and failed runs do not publish; check the repository's Actions tab and commit history for the last successful update.

## Generate books locally

1. Install **Python 3.13 or newer** and [`uv`](https://docs.astral.sh/uv/getting-started/installation/). CI uses Python 3.13 and uv 0.12.23.
2. Install XeLaTeX and the required fonts. On Ubuntu 24.04:

   ```sh
   sudo apt-get update
   sudo apt-get install -y --no-install-recommends \
     texlive-xetex texlive-latex-extra texlive-plain-generic texlive-lang-chinese fontconfig \
     fonts-noto-core fonts-noto-cjk fonts-symbola
   ```

   Use [official Pandoc 3.12.1](https://github.com/jgm/pandoc/releases/tag/3.12.1), which embeds the EPUB resources required by sandbox mode. Some distribution packages omit those resources. CI uses the Linux amd64 release and verifies its SHA-256 before installation:

   ```sh
   directory=$(mktemp -d)
   archive="$directory/pandoc.tar.gz"
   curl --fail --location --retry 3 \
     https://github.com/jgm/pandoc/releases/download/3.12.1/pandoc-3.12.1-linux-amd64.tar.gz \
     --output "$archive"
   echo "d0c90410e90204c9ca83b8539fac5c7aed01fd537207e4585849f8abc5df20b8  $archive" | sha256sum --check
   tar -xzf "$archive" -C "$directory"
   sudo install -m 0755 "$directory/pandoc-3.12.1/bin/pandoc" /usr/local/bin/pandoc
   ```

   On other systems, choose the matching official release asset and verify its published checksum. See [Pandoc installation](https://pandoc.org/installing.html) for platform instructions.

   The PDF setup uses Noto Serif, Noto Serif CJK SC, and Symbola. Other operating systems need the same font families and the XeLaTeX packages used by `src/book_header.tex`. The renderer queries those fixed font families with Fontconfig and generates per-document fallback mappings from their installed Unicode coverage. New characters supported by those fonts need no code change. Unsupported glyphs fail the build; complex emoji sequences still require visual checks.
3. From the repository root, install the locked dependencies:

   ```sh
   uv sync --frozen --group lint
   ```
4. Generate from the included database; Reddit credentials are not needed:

   ```sh
   uv run --frozen python src/generate_markdown.py
   ```

Markdown, EPUB, and PDF output goes into `markdown_files/`, `epub_files/`, and `pdf_files/`. Generation stages and validates the complete set before replacing published outputs. A conversion or validation failure exits with an error rather than publishing a mixture of new and old books.

Every build derives a record inventory from the same read-only database snapshot used for generation. Parsed record headings and EPUB destinations must contain each discussion/comment exactly once. Malformed Markdown that swallows a later record (for example, fences spanning two comments) fails the build and preserves the previous outputs rather than publishing an incomplete book.

Reddit text is untrusted input. The renderer disables raw markup and resource embedding, uses a restricted conversion environment, and applies timeouts. Run it in an isolated environment without credentials or private files; these defenses are not a general-purpose sandbox for arbitrary documents.

## Fetch newer comments

Book generation works without this step. To fetch from Reddit, configure a Reddit API application and create a local `.secrets` file in the repository root:

```dotenv
CLIENT_ID=your_reddit_app_client_id
CLIENT_SECRET=your_reddit_app_client_secret
USER_AGENT=script:reddit-ven-anigha-archive:0.1.0 (by /u/your_username)
TARGET_USERNAME=Bhikkhu_Anigha
LIMIT=
TILL_LAST_COMMENT=true
```

- `CLIENT_ID`, `CLIENT_SECRET`, and `USER_AGENT` identify your Reddit API application.
- `TARGET_USERNAME` is the account to archive, without `/u/`.
- Leave `LIMIT` empty to fetch the available listing without a configured item limit, or set a positive archive-batch size. The fetcher requests one extra comment as lookahead to distinguish a full batch from the end of a listing; that extra comment is saved in a later batch. This does not override Reddit's own availability limits.
- `TILL_LAST_COMMENT=true` enables incremental fetching; `false` requests a refresh of the available listing. Neither setting proves full historical coverage.
- Existing environment variables take precedence over `.secrets`. Keep the file private; it is ignored by Git. Never commit credentials.

```sh
uv run --frozen python src/fetch_comments.py
uv run --frozen python src/generate_markdown.py
```

The fetcher initializes a new database when needed. Incremental fetches use a per-account checkpoint and resume capped batches with a stored cursor instead of silently skipping the remainder. Existing databases without checkpoints scan the accessible listing once. A missing or stalled resume cursor triggers a warning and a safe restart from the newest listing rather than advancing the checkpoint. A warning is also emitted when a listing ends before the old checkpoint can be found; review it before treating continuity as established. Previously observed text is retained in history tables when genuine edits are fetched; this only records revisions the fetcher sees. Reddit may no longer expose older data, so resumable fetching cannot establish complete historical coverage. Keep a backup before changing accounts or performing a full refresh of an existing archive.

## Development checks

```sh
uv sync --frozen --group lint
uv run --frozen --group lint pytest --cov=src --cov-report=term-missing
uv run --frozen --group lint ruff check .
uv audit --frozen
```

With the complete rendering toolchain installed, run `ANIGHA_PDF_TESTS=1 uv run --frozen pytest tests/test_font_pdf.py` for real-PDF Unicode and failure-preservation checks.

Tests use mocks and temporary databases; they do not need Reddit credentials. Unit tests do not replace visually checking generated PDFs and EPUBs after font or layout changes. Review dependency updates and regenerate `uv.lock` with `uv lock`; use `--frozen` when reproducing an existing lock.

## GitHub Actions

`Tests and lint` runs read-only checks on pull requests and pushes. The scheduled/manual updater runs those checks first, then uses three separate jobs:

1. **Fetch:** Reddit secrets are available only to the fetching step. Its database is passed as a workflow artifact.
2. **Render:** A fresh, read-only job checks out without stored Git credentials and generates books without Reddit secrets or inherited credentials. Only a successful complete generation uploads the book artifact.
3. **Publish:** Runs only after fetch and render succeed. It copies only the expected database/book paths and commits them together using repository-write permission.

Set repository Actions secrets for `CLIENT_ID`, `CLIENT_SECRET`, `USER_AGENT`, and `TARGET_USERNAME`; `LIMIT` is optional. Repository policy and branch protection must permit the publisher's commit. Updates only publish from the default branch. Runs on the same branch are serialized, and publication refuses a non-fast-forward push if the branch moved while books were rendering. Rerun against the new revision rather than force-pushing or merging a stale database.

To check the read-only CI locally with [act](https://github.com/nektos/act), use `act pull_request -W .github/workflows/ci.yml -j tests`. A local run of the updater can contact Reddit and reach its publishing step, so do not use it as a harmless test run.

## Remaining limitations

- Missing intermediate replies and text deleted before capture cannot be reconstructed from the stored target comments.
- Incremental fetching does not continuously revisit every old comment for edits.
- Unusual glyphs, emoji sequences, and very long links still warrant visual checks in generated books.
