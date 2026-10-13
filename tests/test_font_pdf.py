"""Opt-in PDF checks for Unicode rendering and failure-safe publication.

Run with ANIGHA_PDF_TESTS=1 uv run pytest tests/test_font_pdf.py.

Requires the documented Pandoc/XeLaTeX/Fontconfig/font installation. The normal
unit suite is intentionally usable without this system rendering toolchain.
"""

import os
import pathlib
import shutil
import subprocess

import pytest

from src import generate_markdown
from src import render_archive

pytestmark = pytest.mark.skipif(
    os.environ.get("ANIGHA_PDF_TESTS") != "1",
    reason="Set ANIGHA_PDF_TESTS=1 for real PDF tests",
)


def source_directory(tmp_path: pathlib.Path, body: str) -> pathlib.Path:
    """Write a 2026 Markdown volume beneath the temporary test directory.

    Args:
        tmp_path: Existing test directory with no input child directory yet.
        body: Markdown appended unchanged after the trusted metadata block.

    Returns:
        Path to the new input directory containing the source volume.

    Raises:
        OSError: The directory or Markdown file cannot be created.
    """
    inputs = tmp_path / "input"
    inputs.mkdir()
    (inputs / "ven_anigha_reddit_archive_2026.md").write_text(
        generate_markdown.metablock_template.format(year=2026) + body
    )
    return inputs


def test_new_unicode_in_prose_and_code_renders_without_source_execution(
    tmp_path: pathlib.Path,
) -> None:
    """Render CJK and emoji in styled text while keeping source TeX inert."""
    body = r"""
## New 你好 😀 {#discussion-future}

Prose 你好 😀, **bold 你好 😀**, *italic 你好 😀*.

> Quote ending in 😀

[Linked 你好 😀](https://example.com/)

Inline `code 你好 😀 \input{/etc/passwd}`.

```
Verbatim 你好 😀 \input{/etc/passwd}
```

\input{/etc/passwd}
"""
    inputs = source_directory(tmp_path, body)
    render_archive.convert_archive(
        inputs,
        tmp_path / "epub",
        tmp_path / "pdf",
        generate_markdown.metablock_template,
        expected_records={
            "ven_anigha_reddit_archive_2026": {"discussion-future"}
        },
    )
    output = tmp_path / "pdf/ven_anigha_reddit_archive_2026.pdf"
    assert output.read_bytes().startswith(b"%PDF-")
    if shutil.which("pdftotext"):
        text = subprocess.run(
            ["pdftotext", str(output), "-"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert text.count("你好") >= 8  # Includes the discussion TOC entry.
        assert text.count("😀") >= 9
        assert "input{/etc/passwd}" in text
        assert "root:x:" not in text


def test_unknown_glyph_fails_without_replacing_existing_outputs(
    tmp_path: pathlib.Path,
) -> None:
    """Keep prior EPUB and PDF files when TeX reports an unsupported glyph."""
    inputs = source_directory(tmp_path, "\nUnsupported scalar \U0010ffff\n")
    filename = "ven_anigha_reddit_archive_2026"
    for directory, suffix in (("pdf", ".pdf"), ("epub", ".epub")):
        (tmp_path / directory).mkdir()
        (tmp_path / directory / (filename + suffix)).write_bytes(
            b"previous output"
        )
    with pytest.raises(ValueError, match="unsupported glyphs"):
        render_archive.convert_archive(
            inputs,
            tmp_path / "epub",
            tmp_path / "pdf",
            generate_markdown.metablock_template,
            expected_records={"ven_anigha_reddit_archive_2026": set()},
        )
    assert (
        tmp_path / "pdf" / (filename + ".pdf")
    ).read_bytes() == b"previous output"
    assert (
        tmp_path / "epub" / (filename + ".epub")
    ).read_bytes() == b"previous output"
