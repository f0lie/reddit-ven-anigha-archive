"""Future Unicode gets installed font coverage without admitting source TeX."""

import json
import pathlib
import re
import subprocess
import sys

import pytest

from src import font_fallback


def example_coverage() -> dict[str, set[int]]:
    """Build independent font-coverage sets for ASCII, CJK, and emoji tests.

    Returns:
        Every allowlisted family mapped to a mutable set of code points. All
        fonts cover printable ASCII; the CJK and symbol fonts also cover their
        respective fixture characters.
    """
    coverage = {
        family: set(range(32, 127)) for family in font_fallback.FONT_FAMILIES
    }
    coverage["Noto Serif CJK SC"].update(map(ord, "你好非梵行》"))
    coverage["Symbola"].update(map(ord, "😀🤓🙏→"))
    return coverage


def test_future_cjk_and_emoji_use_installed_coverage() -> None:
    """Select sorted CJK and symbol fallbacks from discovered coverage."""
    generated = font_fallback.fallback_declarations(
        set("你好 😀🤓"), example_coverage()
    )
    assert generated == (
        "\\archiveDeclareFallback{4F60}{\\archiveCJK}\n"
        "\\archiveDeclareFallback{597D}{\\archiveCJK}\n"
        "\\archiveDeclareFallback{1F600}{\\archiveSymbols}\n"
        "\\archiveDeclareFallback{1F913}{\\archiveSymbols}\n"
    )


def test_common_primary_coverage_needs_no_fallback() -> None:
    """Add a serif fallback only when a primary face lacks the character."""
    coverage = example_coverage()
    for family in font_fallback.PRIMARY_FONTS:
        coverage[family].add(ord("ṁ"))
    assert font_fallback.fallback_declarations(set("ṁ"), coverage) == ""
    coverage["Noto Sans Mono"].remove(ord("ṁ"))
    assert (
        font_fallback.fallback_declarations(set("ṁ"), coverage)
        == "\\archiveDeclareFallback{1E41}{\\archiveSerif}\n"
    )


def test_hostile_source_cannot_add_tex_to_generated_header() -> None:
    """Keep source commands outside numeric, allowlisted declarations."""
    hostile = "你好😀" + r"\input{/etc/passwd}% # $ & _ ^ ~ { } \catcode`\A=13"
    output = font_fallback.fallback_declarations(
        set(hostile), example_coverage()
    )
    assert output == font_fallback.fallback_declarations(
        set("你好😀"), example_coverage()
    )
    assert "input" not in output and "/etc/passwd" not in output
    assert output.isascii()
    assert all(
        re.fullmatch(
            r"\\archiveDeclareFallback\{[0-9A-F]{2,6}\}"
            r"\{\\archive(?:Serif|CJK|Symbols)\}",
            line,
        )
        for line in output.splitlines()
    )


def test_unsupported_character_survives_for_tex_validation() -> None:
    """Leave unknown glyphs in the AST for the final TeX check to reject."""
    text = "A new unsupported character: \U0010ffff"
    document = {"t": "Para", "c": [{"t": "Str", "c": text}]}
    before = json.dumps(document)
    assert (
        font_fallback.fallback_declarations(
            font_fallback.document_characters(document), example_coverage()
        )
        == ""
    )
    assert json.dumps(document) == before
    assert "\U0010ffff" in font_fallback.document_characters(document)


def test_character_scan_excludes_urls_and_attributes() -> None:
    """Collect visible code and metadata text without treating URLs as prose."""
    document = {
        "meta": {"title": {"t": "MetaString", "c": "Title 😀"}},
        "blocks": [
            {"t": "Code", "c": [["unused-id-🤓", [], []], "你好"]},
            {"t": "CodeBlock", "c": [["", [], []], "🙏"]},
            {
                "t": "Link",
                "c": [
                    ["", [], []],
                    [{"t": "Str", "c": "行"}],
                    ["https://example.test/🤓", ""],
                ],
            },
        ],
    }
    characters = font_fallback.document_characters(document)
    assert set("😀你好🙏行").issubset(characters)
    assert "🤓" not in characters


def test_coverage_uses_fixed_font_queries_only(tmp_path: pathlib.Path) -> None:
    """Query only trusted families through the configured staging runner."""
    calls = []

    def record_font_query(
        args: list[str], workdir: pathlib.Path, **kwargs: object
    ) -> str:
        """Record a font query and return identical coverage for all families.

        Args:
            args: Fontconfig flags ending in the requested family name.
            workdir: Staging path recorded with the call.
            **kwargs: Runner options recorded without changes.

        Returns:
            Two-line Fontconfig output covering ASCII and the fixture glyphs.
        """
        calls.append((args, workdir, kwargs))
        return args[-1] + "\n20-7e 4f60 597d 1f600\n"

    assert (
        font_fallback.unicode_fallbacks(
            {"t": "Str", "c": "你好😀"}, tmp_path, record_font_query
        )
        == ""
    )
    assert len(calls) == len(font_fallback.FONT_FAMILIES)
    for family, (args, workdir, kwargs) in zip(
        font_fallback.FONT_FAMILIES, calls, strict=True
    ):
        assert args == ["-f", "%{family}\\n%{charset}\\n", family]
        assert workdir == tmp_path
        assert kwargs == {"executable_name": "fc-match"}


def test_ascii_does_not_require_font_discovery(tmp_path: pathlib.Path) -> None:
    """Avoid invoking external font discovery for ASCII-only documents."""

    def unexpected(*args: object, **kwargs: object) -> None:
        """Fail if ASCII-only text triggers an unnecessary font query.

        Args:
            *args: Unused positional arguments accepted by the runner interface.
            **kwargs: Unused keywords accepted by the runner interface.

        Raises:
            pytest.fail.Exception: This runner is called.
        """
        del args, kwargs  # Unused runner arguments.
        pytest.fail("No font discovery needed for ASCII-only text")

    assert (
        font_fallback.unicode_fallbacks(
            {"t": "Str", "c": "ASCII"}, tmp_path, unexpected
        )
        == ""
    )


def test_fontconfig_ranges_are_validated_and_exact_family_is_required() -> None:
    """Accept exact aliases and inclusive hexadecimal character ranges."""
    assert font_fallback.parse_font_coverage(
        "Symbola\n20-22 1f600\n", "Symbola"
    ) == {32, 33, 34, 0x1F600}
    assert font_fallback.parse_font_coverage(
        "Symbola,Other Alias\n20\n", "Symbola"
    ) == {32}
    with pytest.raises(ValueError, match="not installed"):
        font_fallback.parse_font_coverage(
            "Some replacement font\n20-7e\n", "Symbola"
        )


@pytest.mark.parametrize(
    "charset",
    ["", "0-110000", "D800", "D700-E000", "22-20", "xyz", r"20 \input{evil}"],
)
def test_invalid_fontconfig_charset_fails_closed(charset: str) -> None:
    """Reject malformed ranges, surrogate coverage, and embedded TeX."""
    with pytest.raises(ValueError, match="font coverage"):
        font_fallback.parse_font_coverage(
            "Symbola\n" + charset + "\n", "Symbola"
        )


def test_surrogate_scalar_cannot_become_a_tex_definition() -> None:
    """Reject surrogate code points before producing a fallback declaration."""
    with pytest.raises(ValueError, match="Invalid Unicode"):
        font_fallback.fallback_declarations({"\ud800"}, example_coverage())


def test_fallback_macro_definitions_are_scoped_and_numeric_only() -> None:
    """Keep font macros locally scoped and independent of source text."""
    header = pathlib.Path("src/book_header.tex").read_text(encoding="utf-8")
    assert r"\newcommand{\archiveDeclareFallback}[2]" in header
    assert '\\lowercase{\\endgroup\\protected\\def~}{{#2\\char"#1}}' in header
    assert "ucharclasses" not in header and r"\setTransitionsFor" not in header
    assert r"\newunicodechar{非}" not in header


def test_direct_script_import_still_works(tmp_path: pathlib.Path) -> None:
    """Load the renderer as a script outside the project working directory."""
    source = str(pathlib.Path("src").resolve())
    script = (
        f"import sys\nsys.path.insert(0, {source!r})\nimport render_archive\n"
    )
    subprocess.run(
        [sys.executable, "-I", "-c", script],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
