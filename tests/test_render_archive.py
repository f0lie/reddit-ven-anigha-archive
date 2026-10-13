"""Tests archive sanitization, validation, and failure isolation."""

import contextlib
import json
import pathlib
import shutil
import sqlite3
import subprocess
import zipfile

import pytest

from src import generate_markdown as generate
from src import render_archive as render


def make_source(
    tmp_path: pathlib.Path, body: str = "A discussion"
) -> pathlib.Path:
    """Writes a generated Markdown source with trusted metadata.

    Args:
        tmp_path: Directory in which to create the source volume.
        body: Untrusted Markdown content appended after the metadata.

    Returns:
        The path to the generated source volume.

    Raises:
        OSError: The source file cannot be written.
    """
    path = tmp_path / "ven_anigha_reddit_archive_2026.md"
    path.write_text("---\ntitle: Trusted\n---\n" + body)
    return path


@pytest.mark.skipif(
    not shutil.which("pandoc"), reason="Pandoc is not installed"
)
def test_reddit_content_cannot_inject_tex_metadata_or_resources(
    tmp_path: pathlib.Path,
) -> None:
    """Verifies Reddit content cannot inject TeX, metadata, or resources.

    Args:
        tmp_path: Isolated filesystem directory provided by pytest.
    """
    source = make_source(
        tmp_path,
        r"""
\input{/etc/passwd}
$\input{/etc/passwd}$
![secret](/etc/passwd)
![remote](https://example.test/tracker.png)
<iframe src="file:///etc/passwd"></iframe>

---
header-includes: '\input{/etc/passwd}'
---

[unsafe](javascript:alert%281%29)
[good](https://www.reddit.com/)
""",
    )
    output = render.prepare_document(
        source, '---\ntitle: "Archive {year}"\n---\n', tmp_path, ()
    )
    document = json.loads(output)
    assert document["meta"] == {
        "title": {
            "t": "MetaInlines",
            "c": [
                {"t": "Str", "c": "Archive"},
                {"t": "Space"},
                {"t": "Str", "c": "2026"},
            ],
        }
    }
    assert '"t": "Image"' not in output
    assert '"t": "Raw' not in output
    assert '"t": "Math"' not in output
    latex = render.run_pandoc(
        ["--sandbox", "-f", "json", "-t", "latex"], tmp_path, input_text=output
    )
    assert r"\input{" not in latex
    assert r"\includegraphics" not in latex
    assert "root:x:" not in latex
    assert "tracker.png" in latex  # Source text is preserved without fetching.


def test_restrict_ast_preserves_anchors_without_attributes() -> None:
    """Verifies sanitization keeps anchors and removes unsafe attributes."""
    node = {
        "t": "Header",
        "c": [
            3,
            ["comment-123", ["unlisted", "evil"], [["style", "evil"]]],
            [{"t": "Str", "c": "Hello"}],
        ],
    }
    assert render.restrict_ast(node)["c"][1] == [
        "comment-123",
        ["unlisted", "unnumbered"],
        [],
    ]


def test_render_failure_keeps_all_previous_outputs(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifies render failure keeps all previous outputs.

    Args:
        tmp_path: Isolated filesystem directory provided by pytest.
        monkeypatch: Pytest helper for temporarily replacing process behavior.
    """
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    make_source(inputs)
    epub = tmp_path / "epub"
    pdf = tmp_path / "pdf"
    epub.mkdir()
    pdf.mkdir()
    old_epub = epub / "ven_anigha_reddit_archive_2026.epub"
    old_pdf = pdf / "ven_anigha_reddit_archive_2026.pdf"
    old_epub.write_bytes(b"old epub")
    old_pdf.write_bytes(b"old pdf")
    monkeypatch.setattr(render, "prepare_document", lambda *args: "{}")

    def fail_pdf(args: list[str], *rest: object, **kwargs: object) -> None:
        """Simulates successful EPUB generation followed by a PDF failure.

        Args:
            args: Renderer command-line arguments identifying the output.
            *rest: Unused positional renderer options.
            **kwargs: Unused keyword renderer options.

        Raises:
            subprocess.CalledProcessError: The requested output is a TeX
                document.
        """
        del rest, kwargs  # Unused renderer options.
        output = pathlib.Path(args[args.index("-o") + 1])
        if output.suffix == ".tex":
            raise subprocess.CalledProcessError(1, args)
        write_epub(output)

    monkeypatch.setattr(render, "run_pandoc", fail_pdf)
    with pytest.raises(subprocess.CalledProcessError):
        render.convert_archive(
            inputs,
            epub,
            pdf,
            "",
            expected_records={"ven_anigha_reddit_archive_2026": ()},
        )
    assert old_epub.read_bytes() == b"old epub"
    assert old_pdf.read_bytes() == b"old pdf"


def write_epub(path: pathlib.Path) -> None:
    """Writes a minimal EPUB container accepted by output validation.

    Args:
        path: Destination path for the test EPUB.

    Raises:
        OSError: The EPUB file cannot be written.
    """
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", "<container/>")


def test_invalid_output_blocks_whole_publication(
    tmp_path: pathlib.Path,
) -> None:
    """Verifies invalid output blocks whole publication.

    Args:
        tmp_path: Isolated filesystem directory provided by pytest.
    """
    good, bad = tmp_path / "good.md", tmp_path / "bad.pdf"
    good.write_text("new")
    bad.write_bytes(b"partial")
    target = tmp_path / "published.md"
    target.write_text("old")
    with pytest.raises(ValueError):
        render.publish_files(
            [(good, target), (bad, tmp_path / "published.pdf")]
        )
    assert target.read_text() == "old"


def test_empty_archive_is_an_error(tmp_path: pathlib.Path) -> None:
    """Verifies empty archive is an error.

    Args:
        tmp_path: Isolated filesystem directory provided by pytest.
    """
    with pytest.raises(ValueError, match="No archive"):
        render.convert_archive(
            tmp_path,
            tmp_path / "epub",
            tmp_path / "pdf",
            "",
            expected_records={},
        )


def test_subprocess_receives_no_credentials(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifies subprocess receives no credentials.

    Args:
        tmp_path: Isolated filesystem directory provided by pytest.
        monkeypatch: Pytest helper for temporarily replacing process behavior.
    """
    monkeypatch.setenv("CLIENT_SECRET", "secret")
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    seen = {}

    class FakeProcess:
        """Captures renderer options using the Popen test protocol."""

        returncode = 0

        def __init__(self, args: list[str], **kwargs: object) -> None:
            """Records process options without launching a subprocess.

            Args:
                args: Unused renderer command arguments.
                **kwargs: Process options captured for assertions.
            """
            del args  # The test inspects process options, not the command.
            seen.update(kwargs)

        def __enter__(self) -> "FakeProcess":
            """Provides the fake process for the context-manager protocol.

            Returns:
                This fake process instance.
            """
            return self

        def __exit__(self, *args: object) -> None:
            """Exits the fake context without suppressing exceptions.

            Args:
                *args: Unused exception details supplied by the context manager.
            """
            del args  # No cleanup or exception suppression is needed.

        def communicate(self, **kwargs: object) -> tuple[str, str]:
            """Records communication options and returns successful output.

            Args:
                **kwargs: Communication options captured for assertions.

            Returns:
                Standard-output JSON and empty standard-error text.
            """
            seen.update(kwargs)
            return "{}", ""

    monkeypatch.setattr(subprocess, "Popen", FakeProcess)
    render.run_pandoc(["--version"], tmp_path)
    assert "CLIENT_SECRET" not in seen["env"]
    assert "GITHUB_TOKEN" not in seen["env"]
    assert seen["env"]["shell_escape"] == "f"
    assert seen["timeout"] == render.TIMEOUT
    assert seen["start_new_session"] is True


def test_build_failure_keeps_markdown_and_database_unchanged(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifies build failure keeps markdown and database unchanged.

    Args:
        tmp_path: Isolated filesystem directory provided by pytest.
        monkeypatch: Pytest helper for temporarily replacing process behavior.
    """
    database = tmp_path / "archive.db"
    with contextlib.closing(sqlite3.connect(database)) as conn:
        conn.execute("CREATE TABLE preserved (body TEXT)")
        conn.execute("CREATE TABLE submissions (id TEXT, created_at REAL)")
        conn.execute("CREATE TABLE comments (id TEXT, submission_id TEXT)")
        conn.execute("INSERT INTO preserved VALUES ('original')")
        conn.commit()
    original = database.read_bytes()
    destination = tmp_path / "markdown_files"
    destination.mkdir()
    old = destination / "ven_anigha_reddit_archive_2026.md"
    old.write_text("old markdown")

    def stage_markdown(
        conn: sqlite3.Connection, markdown: pathlib.Path, source: pathlib.Path
    ) -> None:
        # Connection must truly be read-only.
        """Checks read-only access while staging replacement Markdown.

        Args:
            conn: Read-only connection supplied by the archive builder.
            markdown: Directory for indented Markdown outputs.
            source: Directory for generated rendering sources.
        """
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM preserved")
        source.mkdir()
        (markdown / old.name).write_text("new markdown")
        make_source(source)

    def fail_conversion(*args: object, **kwargs: object) -> None:
        """Simulates an archive conversion failure.

        Args:
            *args: Unused conversion paths.
            **kwargs: Unused record-inventory options.

        Raises:
            RuntimeError: Always raised to exercise build cleanup.
        """
        raise RuntimeError("conversion failed")

    monkeypatch.setattr(generate, "save_comments_to_markdown", stage_markdown)
    monkeypatch.setattr(generate, "convert_to_epub_and_pdf", fail_conversion)
    with pytest.raises(RuntimeError, match="conversion failed"):
        generate.build_archive(database, tmp_path)
    assert old.read_text() == "old markdown"
    assert database.read_bytes() == original
    assert not list(tmp_path.glob(".archive-build-*"))


def test_missing_database_is_not_silently_created(
    tmp_path: pathlib.Path,
) -> None:
    """Verifies missing database is not silently created.

    Args:
        tmp_path: Isolated filesystem directory provided by pytest.
    """
    missing = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        generate.build_archive(missing, tmp_path)
    assert not missing.exists()


def test_long_text_wraps_without_interpreting_source_tex() -> None:
    """Verifies long text wraps without interpreting source tex."""
    text = r"/r/theravada/comments/long_title_with_punctuation/\input{secret}"
    node = {"t": "Para", "c": [{"t": "Str", "c": text}]}
    result = render.latex_line_breaks(node)["c"]
    assert "".join(item["c"] for item in result if item["t"] == "Str") == text
    commands = [item["c"] for item in result if item["t"] == "RawInline"]
    assert commands and all(
        command == ["latex", r"\allowbreak{}"] for command in commands
    )


@pytest.mark.skipif(
    not shutil.which("pandoc"), reason="Pandoc is not installed"
)
def test_bare_urls_are_links_for_safe_line_wrapping(
    tmp_path: pathlib.Path,
) -> None:
    """Verifies bare urls are links for safe line wrapping.

    Args:
        tmp_path: Isolated filesystem directory provided by pytest.
    """
    source = make_source(
        tmp_path,
        "Visit https://www.reddit.com/r/HillsideHermitage/comments/example/ .",
    )
    document = render.prepare_document(
        source, '---\ntitle: "Archive {year}"\n---\n', tmp_path, ()
    )
    assert '"t": "Link"' in document


def test_formatted_literal_url_keeps_its_target() -> None:
    """Verifies formatted URLs gain breaks while keeping their destination."""
    url = "https://medium.com/@someone/" + "a-long-article-title-" * 5
    node = {
        "t": "Link",
        "c": [
            ["", [], []],
            [{"t": "Emph", "c": [{"t": "Str", "c": url}]}],
            [url, ""],
        ],
    }
    result = render.latex_line_breaks(node)
    assert result["c"][1] == [{"t": "Str", "c": url}]
    assert result["c"][2] == [url, ""]
    assert node["c"][1][0]["t"] == "Emph"


@pytest.mark.parametrize("kind", ["Span", "Cite"])
def test_visible_label_text_handles_wrapper_metadata(kind: str) -> None:
    """Verifies visible label text handles wrapper metadata.

    Args:
        kind: Pandoc node type supplied by the parametrized test.
    """
    assert (
        render.visible_inline_text(
            [
                {
                    "t": kind,
                    "c": [
                        [],
                        [
                            {"t": "Str", "c": "a"},
                            {"t": "Space"},
                            {"t": "Str", "c": "b"},
                        ],
                    ],
                }
            ]
        )
        == "a b"
    )


@pytest.mark.parametrize("kind", ["Emph", "Span", "Cite"])
def test_uncertain_nested_label_is_not_silently_discarded(kind: str) -> None:
    """Verifies uncertain nested label is not silently discarded.

    Args:
        kind: Pandoc node type supplied by the parametrized test.
    """
    unknown = [{"t": "Code", "c": [["", [], []], "extra visible text"]}]
    content = [[], unknown] if kind in {"Span", "Cite"} else unknown
    assert (
        render.visible_inline_text(
            [
                {"t": kind, "c": content},
                {"t": "Str", "c": "https://example.com"},
            ]
        )
        is None
    )


def test_restrict_ast_cleans_table_and_figure_attributes() -> None:
    """Verifies all table and figure attribute slots are sanitized."""

    def attr(identifier: str) -> list[object]:
        """Builds an attribute tuple containing deliberately unsafe values.

        Args:
            identifier: Anchor identifier that sanitization should preserve.

        Returns:
            Pandoc identifier, class, and key/value attribute fields.
        """
        return [
            identifier,
            ["evil"],
            [
                [
                    "style",
                    "background-image:url(https://example.test/pixel.png)",
                ],
                ["onclick", "alert(1)"],
            ],
        ]

    def row(identifier: str) -> list[object]:
        """Builds a table row with unsafe row and cell attributes.

        Args:
            identifier: Anchor identifier used for the row and its cell.

        Returns:
            An untagged Pandoc table row containing one text cell.
        """
        cell = [
            attr(identifier + "-cell"),
            {"t": "AlignDefault"},
            1,
            1,
            [{"t": "Plain", "c": [{"t": "Str", "c": "Cell text"}]}],
        ]
        return [attr(identifier), [cell]]

    table = {
        "t": "Table",
        "c": [
            attr("table"),
            [None, []],
            [[{"t": "AlignDefault"}, {"t": "ColWidthDefault"}]],
            [attr("head"), [row("head-row")]],
            [[attr("body"), 0, [row("body-head-row")], [row("body-row")]]],
            [attr("foot"), [row("foot-row")]],
        ],
    }
    figure = {"t": "Figure", "c": [attr("figure"), [None, []], [table]]}
    output = render.restrict_ast(figure)
    encoded = json.dumps(output)
    assert all(
        unsafe not in encoded
        for unsafe in ("evil", "onclick", "background-image", "pixel.png")
    )
    assert "Cell text" in encoded
    assert output["c"][0] == ["figure", [], []]
    content = output["c"][2][0]["c"]
    assert content[0] == ["table", [], []]
    sections = [(content[3], [content[3][1]]), (content[5], [content[5][1]])]
    sections.extend((body, body[2:4]) for body in content[4])
    for section, row_groups in sections:
        assert section[0][1:] == [[], []]
        for rows in row_groups:
            for table_row in rows:
                assert table_row[0][1:] == [[], []]
                for cell in table_row[1]:
                    assert cell[0][1:] == [[], []]
    assert "onclick" in json.dumps(figure)  # Do not mutate the original AST.


@pytest.mark.skipif(
    not shutil.which("pandoc"), reason="Pandoc is not installed"
)
def test_table_attributes_cannot_add_active_content_to_epub(
    tmp_path: pathlib.Path,
) -> None:
    """Verifies table attributes cannot add active content to EPUB.

    Args:
        tmp_path: Isolated filesystem directory provided by pytest.
    """
    source = make_source(
        tmp_path,
        "| A |\n|---|\n|x|\n\n: caption {#id "
        'style="background-image:url(https://example.test/pixel.png)" '
        'onclick="alert(1)"}',
    )
    document = render.prepare_document(
        source, '---\ntitle: "Archive {year}"\n---\n', tmp_path, ()
    )
    output = tmp_path / "book.epub"
    render.run_pandoc(
        ["--sandbox", "-f", "json", "-o", str(output)],
        tmp_path,
        input_text=document,
    )
    render.validate_output(output)
    with zipfile.ZipFile(output) as archive:
        pages = "\n".join(
            archive.read(name).decode()
            for name in archive.namelist()
            if name.endswith(".xhtml")
        )
    assert '<table id="id">' in pages
    assert "caption" in pages
    assert all(
        unsafe not in pages
        for unsafe in ("onclick", "alert(1)", "background-image", "pixel.png")
    )


@pytest.mark.parametrize(
    "url", ["http://[", "https://example.com／bad", "https://foo＠bar"]
)
def test_malformed_url_is_plain_text(url: str) -> None:
    """Verifies malformed url is plain text.

    Args:
        url: Malformed URL supplied by the parametrized test.
    """
    assert not render.safe_url(url)
    node = {
        "t": "Link",
        "c": [["", [], []], [{"t": "Str", "c": "visible label"}], [url, ""]],
    }
    result = render.restrict_ast(node)
    assert result["t"] == "Span"
    assert "visible label" in json.dumps(result)
    assert url in json.dumps(result, ensure_ascii=False)


@pytest.mark.skipif(
    not shutil.which("pandoc"), reason="Pandoc is not installed"
)
def test_malformed_authority_does_not_abort_document(
    tmp_path: pathlib.Path,
) -> None:
    """Verifies malformed authority does not abort document.

    Args:
        tmp_path: Isolated filesystem directory provided by pytest.
    """
    source = make_source(tmp_path, "[visible label](https://example.com／bad)")
    document = render.prepare_document(
        source, '---\ntitle: "Archive {year}"\n---\n', tmp_path, set()
    )
    parsed = json.loads(document)
    assert parsed["blocks"][0]["c"][0]["t"] == "Span"
    assert "visible" in document
    assert "https://example.com／bad" in document


@pytest.mark.parametrize(
    "text",
    [
        "faith/dhamma",
        "absorbed/proliferating/dwelling",
        "dependence/simultaneity",
    ],
)
def test_short_slash_terms_wrap_without_changing_text_or_link_target(
    text: str,
) -> None:
    """Verifies short slash terms wrap without changing text or link target.

    Args:
        text: Visible source text supplied by the parametrized test.
    """
    target = ["https://example.com/original/path", "Original title"]
    node = {
        "t": "Link",
        "c": [["discussion-test", [], []], [{"t": "Str", "c": text}], target],
    }
    result = render.latex_line_breaks(node)
    label = result["c"][1]
    assert "".join(item["c"] for item in label if item["t"] == "Str") == text
    assert sum(item["t"] == "RawInline" for item in label) == text.count("/")
    assert result["c"][2] == target
    assert node["c"][1] == [{"t": "Str", "c": text}]


def test_italic_nonbreaking_spaces_wrap_without_text_changes() -> None:
    """Verifies italic quotations keep their characters and gain safe breaks."""
    text = (
        "yaṁ\xa0nimittaṁ\xa0manasikaroto\xa0uppajjanti\xa0pāpakā\xa0akusalā\xa0"
        "vitakkā"
    )
    node = {"t": "Emph", "c": [{"t": "Str", "c": text}]}
    result = render.latex_line_breaks(node)
    assert result["t"] == "Emph"
    assert (
        "".join(item["c"] for item in result["c"] if item["t"] == "Str") == text
    )
    assert [item["c"] for item in result["c"] if item["t"] == "RawInline"] == [
        ["latex", r"\allowbreak{}"]
    ] * text.count("\u00a0")


def test_short_prose_retains_normal_punctuation_and_decimal_spacing() -> None:
    """Verifies short prose retains normal punctuation and decimal spacing."""
    for text in ["12.5", "e.g.", "well-being", "(i.e.\u00a0becoming"]:
        node = {"t": "Para", "c": [{"t": "Str", "c": text}]}
        result = render.latex_line_breaks(node)
        assert (
            "".join(item["c"] for item in result["c"] if item["t"] == "Str")
            == text
        )
        assert sum(
            item["t"] == "RawInline" for item in result["c"]
        ) == text.count("\u00a0")
