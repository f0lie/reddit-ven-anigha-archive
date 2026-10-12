import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from src import render_archive as render


def make_source(tmp_path, body="A discussion"):
    path = tmp_path / "ven_anigha_reddit_archive_2026.md"
    path.write_text("---\ntitle: Trusted\n---\n" + body)
    return path


@pytest.mark.skipif(not shutil.which("pandoc"), reason="Pandoc is not installed")
def test_reddit_content_cannot_inject_tex_metadata_or_resources(tmp_path):
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
    output = render.prepare_document(source, '---\ntitle: "Archive {year}"\n---\n', tmp_path)
    doc = json.loads(output)
    assert doc["meta"] == {
        "title": {"t": "MetaInlines", "c": [{"t": "Str", "c": "Archive"}, {"t": "Space"}, {"t": "Str", "c": "2026"}]}
    }
    assert '"t": "Image"' not in output
    assert '"t": "Raw' not in output
    assert '"t": "Math"' not in output
    latex = render.run_pandoc(["--sandbox", "-f", "json", "-t", "latex"], tmp_path, input_text=output)
    assert r"\input{" not in latex
    assert r"\includegraphics" not in latex
    assert "root:x:" not in latex
    assert "tracker.png" in latex  # Source text is preserved without fetching.


def test_restrict_ast_preserves_anchors_but_removes_arbitrary_attributes():
    node = {
        "t": "Header",
        "c": [3, ["comment-123", ["unlisted", "evil"], [["style", "evil"]]], [{"t": "Str", "c": "Hello"}]],
    }
    assert render.restrict_ast(node)["c"][1] == ["comment-123", ["unlisted", "unnumbered"], []]


def test_render_failure_keeps_all_previous_outputs(tmp_path, monkeypatch):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    make_source(inputs)
    epub, pdf = tmp_path / "epub", tmp_path / "pdf"
    epub.mkdir()
    pdf.mkdir()
    old_epub = epub / "ven_anigha_reddit_archive_2026.epub"
    old_pdf = pdf / "ven_anigha_reddit_archive_2026.pdf"
    old_epub.write_bytes(b"old epub")
    old_pdf.write_bytes(b"old pdf")
    monkeypatch.setattr(render, "prepare_document", lambda *args: "{}")

    def fail_pdf(args, *rest, **kwargs):
        output = Path(args[args.index("-o") + 1])
        if output.suffix == ".tex":
            raise subprocess.CalledProcessError(1, args)
        write_epub(output)

    monkeypatch.setattr(render, "run_pandoc", fail_pdf)
    with pytest.raises(subprocess.CalledProcessError):
        render.convert_archive(inputs, epub, pdf, "")
    assert old_epub.read_bytes() == b"old epub"
    assert old_pdf.read_bytes() == b"old pdf"


def write_epub(path):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", "<container/>")


def test_invalid_output_blocks_whole_publication(tmp_path):
    good, bad = tmp_path / "good.md", tmp_path / "bad.pdf"
    good.write_text("new")
    bad.write_bytes(b"partial")
    target = tmp_path / "published.md"
    target.write_text("old")
    with pytest.raises(ValueError):
        render.publish_files([(good, target), (bad, tmp_path / "published.pdf")])
    assert target.read_text() == "old"


def test_empty_archive_is_an_error(tmp_path):
    with pytest.raises(ValueError, match="No archive"):
        render.convert_archive(tmp_path, tmp_path / "epub", tmp_path / "pdf", "")


def test_subprocess_receives_no_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIENT_SECRET", "secret")
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    seen = {}

    class FakeProcess:
        returncode = 0

        def __init__(self, args, **kwargs):
            seen.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def communicate(self, **kwargs):
            seen.update(kwargs)
            return "{}", ""

    monkeypatch.setattr(subprocess, "Popen", FakeProcess)
    render.run_pandoc(["--version"], tmp_path)
    assert "CLIENT_SECRET" not in seen["env"]
    assert "GITHUB_TOKEN" not in seen["env"]
    assert seen["env"]["shell_escape"] == "f"
    assert seen["timeout"] == render.TIMEOUT
    assert seen["start_new_session"] is True


def test_build_failure_keeps_markdown_and_database_unchanged(tmp_path, monkeypatch):
    import sqlite3
    from contextlib import closing

    from src import generate_markdown as generate

    database = tmp_path / "archive.db"
    with closing(sqlite3.connect(database)) as conn:
        conn.execute("CREATE TABLE preserved (body TEXT)")
        conn.execute("INSERT INTO preserved VALUES ('original')")
        conn.commit()
    original = database.read_bytes()
    destination = tmp_path / "markdown_files"
    destination.mkdir()
    old = destination / "ven_anigha_reddit_archive_2026.md"
    old.write_text("old markdown")

    def stage_markdown(conn, markdown, source):
        # Connection must truly be read-only.
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM preserved")
        source.mkdir()
        (markdown / old.name).write_text("new markdown")
        make_source(source)

    def fail_conversion(*args):
        raise RuntimeError("conversion failed")

    monkeypatch.setattr(generate, "save_comments_to_markdown", stage_markdown)
    monkeypatch.setattr(generate, "convert_to_epub_and_pdf", fail_conversion)
    with pytest.raises(RuntimeError, match="conversion failed"):
        generate.build_archive(database, tmp_path)
    assert old.read_text() == "old markdown"
    assert database.read_bytes() == original
    assert not list(tmp_path.glob(".archive-build-*"))


def test_missing_database_is_not_silently_created(tmp_path):
    import sqlite3

    from src.generate_markdown import build_archive

    missing = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        build_archive(missing, tmp_path)
    assert not missing.exists()


def test_long_text_wraps_without_interpreting_source_tex():
    text = r"/r/theravada/comments/long_title_with_punctuation/\input{secret}"
    node = {"t": "Para", "c": [{"t": "Str", "c": text}]}
    result = render.latex_line_breaks(node)["c"]
    assert "".join(item["c"] for item in result if item["t"] == "Str") == text
    commands = [item["c"] for item in result if item["t"] == "RawInline"]
    assert commands and all(command == ["latex", r"\allowbreak{}"] for command in commands)


@pytest.mark.skipif(not shutil.which("pandoc"), reason="Pandoc is not installed")
def test_bare_urls_are_links_for_safe_line_wrapping(tmp_path):
    source = make_source(tmp_path, "Visit https://www.reddit.com/r/HillsideHermitage/comments/example/ .")
    document = render.prepare_document(source, '---\ntitle: "Archive {year}"\n---\n', tmp_path)
    assert '"t": "Link"' in document


def test_formatted_literal_url_uses_url_writer_without_changing_target():
    url = "https://medium.com/@someone/" + "a-long-article-title-" * 5
    node = {"t": "Link", "c": [["", [], []], [{"t": "Emph", "c": [{"t": "Str", "c": url}]}], [url, ""]]}
    result = render.latex_line_breaks(node)
    assert result["c"][1] == [{"t": "Str", "c": url}]
    assert result["c"][2] == [url, ""]
    assert node["c"][1][0]["t"] == "Emph"


@pytest.mark.parametrize("kind", ["Span", "Cite"])
def test_visible_label_text_handles_wrapper_metadata(kind):
    assert (
        render.visible_inline_text(
            [{"t": kind, "c": [[], [{"t": "Str", "c": "a"}, {"t": "Space"}, {"t": "Str", "c": "b"}]]}]
        )
        == "a b"
    )


@pytest.mark.parametrize("kind", ["Emph", "Span", "Cite"])
def test_uncertain_nested_label_is_not_silently_discarded(kind):
    unknown = [{"t": "Code", "c": [["", [], []], "extra visible text"]}]
    content = [[], unknown] if kind in {"Span", "Cite"} else unknown
    assert render.visible_inline_text([{"t": kind, "c": content}, {"t": "Str", "c": "https://example.com"}]) is None


def test_restrict_ast_removes_attributes_from_every_table_and_figure_slot():
    def attr(identifier):
        return [
            identifier,
            ["evil"],
            [["style", "background-image:url(https://example.test/pixel.png)"], ["onclick", "alert(1)"]],
        ]

    def row(identifier):
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
    assert all(unsafe not in encoded for unsafe in ("evil", "onclick", "background-image", "pixel.png"))
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


@pytest.mark.skipif(not shutil.which("pandoc"), reason="Pandoc is not installed")
def test_table_attributes_cannot_add_active_content_to_epub(tmp_path):
    source = make_source(
        tmp_path,
        '| A |\n|---|\n|x|\n\n: caption {#id style="background-image:url(https://example.test/pixel.png)" '
        'onclick="alert(1)"}',
    )
    document = render.prepare_document(source, '---\ntitle: "Archive {year}"\n---\n', tmp_path)
    output = tmp_path / "book.epub"
    render.run_pandoc(["--sandbox", "-f", "json", "-o", str(output)], tmp_path, input_text=document)
    render.validate_output(output)
    with zipfile.ZipFile(output) as archive:
        pages = "\n".join(archive.read(name).decode() for name in archive.namelist() if name.endswith(".xhtml"))
    assert '<table id="id">' in pages
    assert "caption" in pages
    assert all(unsafe not in pages for unsafe in ("onclick", "alert(1)", "background-image", "pixel.png"))


@pytest.mark.parametrize("url", ["http://[", "https://example.com／bad", "https://foo＠bar"])
def test_malformed_url_is_plain_text(url):
    assert not render.safe_url(url)
    node = {"t": "Link", "c": [["", [], []], [{"t": "Str", "c": "visible label"}], [url, ""]]}
    result = render.restrict_ast(node)
    assert result["t"] == "Span"
    assert "visible label" in json.dumps(result)
    assert url in json.dumps(result, ensure_ascii=False)


@pytest.mark.skipif(not shutil.which("pandoc"), reason="Pandoc is not installed")
def test_malformed_authority_does_not_abort_document(tmp_path):
    source = make_source(tmp_path, "[visible label](https://example.com／bad)")
    document = render.prepare_document(source, '---\ntitle: "Archive {year}"\n---\n', tmp_path)
    parsed = json.loads(document)
    assert parsed["blocks"][0]["c"][0]["t"] == "Span"
    assert "visible" in document
    assert "https://example.com／bad" in document
