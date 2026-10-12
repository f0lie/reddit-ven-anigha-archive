"""Select local Unicode fallbacks from the installed, allowlisted font families.

Fontconfig supplies cmap coverage, so supporting another character in an already
installed font never requires editing a hand-maintained character list. Neither
font names nor TeX commands are taken from archived content.
"""

import re

# Keep these synchronized with the trusted metadata and book_header.tex.
PRIMARY_FONTS = ("Noto Serif", "Noto Sans", "Noto Sans Mono")
FALLBACK_FONTS = (
    ("Noto Serif", "archiveSerif"),
    ("Noto Serif CJK SC", "archiveCJK"),
    ("Symbola", "archiveSymbols"),
)
FONT_FAMILIES = tuple(dict.fromkeys((*PRIMARY_FONTS, *(family for family, _ in FALLBACK_FONTS))))


def parse_font_coverage(output, family):
    """Validate fc-match's exact family and bounded, hexadecimal charset ranges."""
    lines = output.splitlines()
    if len(lines) != 2 or family not in (name.strip() for name in lines[0].split(",")):
        raise ValueError(f"Required archive font is not installed: {family}")
    coverage = set()
    for entry in lines[1].split():
        if not re.fullmatch(r"[0-9a-fA-F]{1,6}(?:-[0-9a-fA-F]{1,6})?", entry):
            raise ValueError(f"Invalid font coverage for {family}")
        bounds = [int(value, 16) for value in entry.split("-")]
        start, end = bounds[0], bounds[-1]
        if start > end or end > 0x10FFFF or start <= 0xDFFF and end >= 0xD800:
            raise ValueError(f"Invalid font coverage for {family}")
        coverage.update(range(start, end + 1))
    if not coverage:
        raise ValueError(f"Empty font coverage for {family}")
    return coverage


def document_characters(value):
    """Collect visible text from the restricted AST, including inline/block code.

    Link destinations and arbitrary attributes are not visible text. If a writer
    introduces any other unsupported glyph, the XeLaTeX log check still fails
    closed instead of publishing missing-glyph boxes.
    """
    if isinstance(value, list):
        return set().union(*(document_characters(item) for item in value))
    if not isinstance(value, dict):
        return set()
    kind, content = value.get("t"), value.get("c")
    if kind in {"Str", "MetaString"}:
        return set(content)
    if kind in {"Code", "CodeBlock"}:
        return set(content[1])
    return set().union(*(document_characters(item) for item in value.values()))


def fallback_declarations(characters, coverage):
    """Emit numeric scalar values and constant macros only; never source TeX."""
    common = set.intersection(*(coverage[family] for family in PRIMARY_FONTS))
    lines = []
    for codepoint in sorted(ord(char) for char in characters):
        # ASCII is escaped by Pandoc. Do not make TeX syntax characters active.
        if codepoint <= 0x7F or codepoint in common:
            continue
        if codepoint > 0x10FFFF or 0xD800 <= codepoint <= 0xDFFF:
            raise ValueError("Invalid Unicode scalar in archive text")
        for family, macro in FALLBACK_FONTS:
            if codepoint in coverage[family]:
                lines.append(r"\archiveDeclareFallback{" + f"{codepoint:X}" + "}{\\" + macro + "}")
                break
        # Truly unsupported characters remain intact. The actual TeX missing-
        # glyph check rejects the build, including any context-specific gaps.
    return "\n".join(lines) + ("\n" if lines else "")


def unicode_fallbacks(document, workdir, run_command):
    characters = {char for char in document_characters(document) if ord(char) > 0x7F}
    if not characters:
        return ""
    coverage = {
        family: parse_font_coverage(
            run_command(["-f", "%{family}\\n%{charset}\\n", family], workdir, executable_name="fc-match"), family
        )
        for family in FONT_FAMILIES
    }
    return fallback_declarations(characters, coverage)
