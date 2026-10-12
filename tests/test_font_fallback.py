"""Future Unicode gets installed font coverage without admitting source TeX."""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from src.font_fallback import (
    FONT_FAMILIES,
    PRIMARY_FONTS,
    document_characters,
    fallback_declarations,
    parse_font_coverage,
    unicode_fallbacks,
)


def example_coverage():
    coverage = {family: set(range(32, 127)) for family in FONT_FAMILIES}
    coverage["Noto Serif CJK SC"].update(map(ord, "你好非梵行》"))
    coverage["Symbola"].update(map(ord, "😀🤓🙏→"))
    return coverage


def test_future_cjk_and_emoji_use_installed_coverage():
    generated = fallback_declarations(set("你好 😀🤓"), example_coverage())
    assert generated == (
        "\\archiveDeclareFallback{4F60}{\\archiveCJK}\n"
        "\\archiveDeclareFallback{597D}{\\archiveCJK}\n"
        "\\archiveDeclareFallback{1F600}{\\archiveSymbols}\n"
        "\\archiveDeclareFallback{1F913}{\\archiveSymbols}\n"
    )


def test_fallback_is_local_and_keeps_primary_fonts_when_all_cover_character():
    coverage = example_coverage()
    for family in PRIMARY_FONTS:
        coverage[family].add(ord("ṁ"))
    assert fallback_declarations(set("ṁ"), coverage) == ""
    coverage["Noto Sans Mono"].remove(ord("ṁ"))
    assert fallback_declarations(set("ṁ"), coverage) == "\\archiveDeclareFallback{1E41}{\\archiveSerif}\n"


def test_hostile_source_cannot_add_tex_to_generated_header():
    hostile = "你好😀" + r"\input{/etc/passwd}% # $ & _ ^ ~ { } \catcode`\A=13"
    output = fallback_declarations(set(hostile), example_coverage())
    assert output == fallback_declarations(set("你好😀"), example_coverage())
    assert "input" not in output and "/etc/passwd" not in output
    assert output.isascii()
    assert all(
        re.fullmatch(r"\\archiveDeclareFallback\{[0-9A-F]{2,6}\}\{\\archive(?:Serif|CJK|Symbols)\}", line)
        for line in output.splitlines()
    )


def test_truly_unsupported_character_is_preserved_for_fail_closed_tex_check():
    text = "A new unsupported character: \U0010ffff"
    document = {"t": "Para", "c": [{"t": "Str", "c": text}]}
    before = json.dumps(document)
    assert fallback_declarations(document_characters(document), example_coverage()) == ""
    assert json.dumps(document) == before
    assert "\U0010ffff" in document_characters(document)


def test_code_and_metadata_are_included_but_link_targets_and_attributes_are_not():
    document = {
        "meta": {"title": {"t": "MetaString", "c": "Title 😀"}},
        "blocks": [
            {"t": "Code", "c": [["unused-id-🤓", [], []], "你好"]},
            {"t": "CodeBlock", "c": [["", [], []], "🙏"]},
            {"t": "Link", "c": [["", [], []], [{"t": "Str", "c": "行"}], ["https://example.test/🤓", ""]]},
        ],
    }
    chars = document_characters(document)
    assert set("😀你好🙏行").issubset(chars)
    assert "🤓" not in chars


def test_coverage_uses_fixed_font_queries_only(tmp_path):
    calls = []

    def run(args, workdir, **kwargs):
        calls.append((args, workdir, kwargs))
        return args[-1] + "\n20-7e 4f60 597d 1f600\n"

    assert unicode_fallbacks({"t": "Str", "c": "你好😀"}, tmp_path, run) == ""
    assert len(calls) == len(FONT_FAMILIES)
    for family, (args, workdir, kwargs) in zip(FONT_FAMILIES, calls, strict=True):
        assert args == ["-f", "%{family}\\n%{charset}\\n", family]
        assert workdir == tmp_path
        assert kwargs == {"executable_name": "fc-match"}


def test_ascii_does_not_require_font_discovery(tmp_path):
    def unexpected(*args, **kwargs):
        pytest.fail("No font discovery needed for ASCII-only text")

    assert unicode_fallbacks({"t": "Str", "c": "ASCII"}, tmp_path, unexpected) == ""


def test_fontconfig_ranges_are_validated_and_exact_family_is_required():
    assert parse_font_coverage("Symbola\n20-22 1f600\n", "Symbola") == {32, 33, 34, 0x1F600}
    assert parse_font_coverage("Symbola,Other Alias\n20\n", "Symbola") == {32}
    with pytest.raises(ValueError, match="not installed"):
        parse_font_coverage("Some replacement font\n20-7e\n", "Symbola")


@pytest.mark.parametrize("charset", ["", "0-110000", "D800", "D700-E000", "22-20", "xyz", r"20 \input{evil}"])
def test_invalid_fontconfig_charset_fails_closed(charset):
    with pytest.raises(ValueError, match="font coverage"):
        parse_font_coverage("Symbola\n" + charset + "\n", "Symbola")


def test_surrogate_scalar_cannot_become_a_tex_definition():
    with pytest.raises(ValueError, match="Invalid Unicode"):
        fallback_declarations({"\ud800"}, example_coverage())


def test_fallback_macro_definitions_are_scoped_and_numeric_only():
    header = Path("src/book_header.tex").read_text()
    assert r"\newcommand{\archiveDeclareFallback}[2]" in header
    assert '\\lowercase{\\endgroup\\protected\\def~}{{#2\\char"#1}}' in header
    assert "ucharclasses" not in header and r"\setTransitionsFor" not in header
    assert r"\newunicodechar{非}" not in header


def test_direct_script_import_still_works(tmp_path):
    source = str(Path("src").resolve())
    subprocess.run(
        [sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {source!r}); import render_archive"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
