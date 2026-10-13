"""Select local Unicode fallbacks from the installed, allowlisted font families.

Fontconfig supplies cmap coverage, so supporting another character in an already
installed font never requires editing a hand-maintained character list. Neither
font names nor TeX commands are taken from archived content.
"""

from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Mapping
import pathlib
import re

# Keep these synchronized with the trusted metadata and book_header.tex.
PRIMARY_FONTS = ("Noto Serif", "Noto Sans", "Noto Sans Mono")
FALLBACK_FONTS = (
    ("Noto Serif", "archiveSerif"),
    ("Noto Serif CJK SC", "archiveCJK"),
    ("Symbola", "archiveSymbols"),
)
FONT_FAMILIES = tuple(
    dict.fromkeys((*PRIMARY_FONTS, *(family for family, _ in FALLBACK_FONTS)))
)


def parse_font_coverage(output: str, family: str) -> set[int]:
    """Validate Fontconfig's family match and Unicode character coverage.

    Args:
        output: Two-line ``fc-match`` output: comma-separated family names, then
            whitespace-separated hexadecimal points or inclusive
            ranges. A final newline is allowed.
        family: Required font family, matched exactly against listed aliases.

    Returns:
        Nonempty set of Unicode scalar values supported by the matched font.

    Raises:
        ValueError: The requested family is absent, the output shape is invalid,
            or coverage is empty, malformed, reversed, above U+10FFFF, or
            includes a surrogate code point.
    """
    lines = output.splitlines()
    if len(lines) != 2 or family not in (
        name.strip() for name in lines[0].split(",")
    ):
        raise ValueError(f"Required archive font is not installed: {family}")
    coverage: set[int] = set()
    for entry in lines[1].split():
        if not re.fullmatch(r"[0-9a-fA-F]{1,6}(?:-[0-9a-fA-F]{1,6})?", entry):
            raise ValueError(f"Invalid font coverage for {family}")
        bounds = [int(value, 16) for value in entry.split("-")]
        start, end = bounds[0], bounds[-1]
        intersects_surrogates = start <= 0xDFFF and end >= 0xD800
        if start > end or end > 0x10FFFF or intersects_surrogates:
            raise ValueError(f"Invalid font coverage for {family}")
        coverage.update(range(start, end + 1))
    if not coverage:
        raise ValueError(f"Empty font coverage for {family}")
    return coverage


def document_characters(value: object) -> set[str]:
    """Collect visible text characters from a restricted Pandoc AST.

    Args:
        value: Parsed Pandoc document or nested value with well-formed text and
            code nodes. It is inspected without mutation.

    Returns:
        Unique characters in Str, MetaString, Code, and CodeBlock nodes. Plain
        scalar values, including link destinations and attributes, are ignored.
        Writer-generated text is not included; the renderer must still reject
        missing glyphs reported by XeLaTeX.
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


def fallback_declarations(
    characters: Iterable[str], coverage: Mapping[str, set[int]]
) -> str:
    """Generate trusted TeX fallback declarations for non-ASCII characters.

    Args:
        characters: One-character strings to consider, consumed once. Callers
            normally provide a set to avoid repeated declarations.
        coverage: Validated scalar sets for every family in FONT_FAMILIES. The
            mapping and its sets are read without mutation.

    Returns:
        Declarations in ascending code-point order, using only numeric scalars
        and fixed macros. Nonempty output ends with a newline. ASCII and points
        covered by every primary font receive no declaration. Other supported
        points use the first matching fallback family; unsupported points are
        left for the renderer's missing-glyph check to reject.

    Raises:
        ValueError: A character is a surrogate instead of a Unicode scalar.
    """
    common_coverage = set.intersection(
        *(coverage[family] for family in PRIMARY_FONTS)
    )
    lines = []
    for codepoint in sorted(ord(character) for character in characters):
        # ASCII is escaped by Pandoc. Do not make TeX syntax characters active.
        if codepoint <= 0x7F or codepoint in common_coverage:
            continue
        if codepoint > 0x10FFFF or 0xD800 <= codepoint <= 0xDFFF:
            raise ValueError("Invalid Unicode scalar in archive text")
        for family, macro in FALLBACK_FONTS:
            if codepoint in coverage[family]:
                lines.append(
                    rf"\archiveDeclareFallback{{{codepoint:X}}}{{\{macro}}}"
                )
                break
        # Truly unsupported characters remain intact. The actual TeX missing-
        # glyph check rejects the build, including any context-specific gaps.
    return "\n".join(lines) + ("\n" if lines else "")


def unicode_fallbacks(
    document: object,
    workdir: pathlib.Path,
    run_command: Callable[..., str],
) -> str:
    """Discover installed font coverage and generate document fallbacks.

    Args:
        document: Restricted Pandoc AST to inspect without mutation.
        workdir: Existing staging directory passed to each font-discovery call.
        run_command: Runner accepting a list of command arguments, workdir, and
            ``executable_name="fc-match"``. It returns decoded stdout and must
            enforce the renderer's environment and timeout restrictions.

    Returns:
        Trusted fallback declarations from fallback_declarations, or an empty
        string without querying fonts when visible text is ASCII-only.

    Raises:
        ValueError: Required fonts or their reported coverage are invalid, or
            visible text contains a surrogate code point.
        RuntimeError: The supplied renderer runner cannot locate fc-match.
        OSError: Starting fc-match or accessing its staging directory fails.
        subprocess.SubprocessError: The renderer runner times out or fc-match
            exits unsuccessfully. Other callback exceptions also propagate.
    """
    characters = {
        character
        for character in document_characters(document)
        if ord(character) > 0x7F
    }
    if not characters:
        return ""
    coverage = {}
    for family in FONT_FAMILIES:
        output = run_command(
            ["-f", "%{family}\\n%{charset}\\n", family],
            workdir,
            executable_name="fc-match",
        )
        coverage[family] = parse_font_coverage(output, family)
    return fallback_declarations(characters, coverage)
