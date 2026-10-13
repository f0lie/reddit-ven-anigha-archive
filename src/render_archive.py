"""Fail-closed conversion of archived, untrusted Reddit Markdown.

Pandoc's sandbox does not sandbox TeX. Defense in depth: only a restricted AST
reaches XeLaTeX, no resources are embedded, shell escape is disabled,
subprocesses
receive no credentials, and CI renders in a separate credential-free job.
"""

from collections.abc import Collection
from collections.abc import Iterable
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import tempfile
from typing import Any
from urllib import parse
from xml.etree import ElementTree
import zipfile

# Retain the documented direct-script entry point as well as package imports.
if __package__:
    from src import archive_manifest
    from src import font_fallback
else:
    import archive_manifest  # pylint: disable=import-error
    import font_fallback  # pylint: disable=import-error

READER = (
    "markdown"
    "+autolink_bare_uris"
    "-raw_tex"
    "-raw_html"
    "-raw_attribute"
    "-yaml_metadata_block"
    "-latex_macros"
    "-tex_math_dollars"
    "-tex_math_single_backslash"
    "-tex_math_double_backslash"
)
TIMEOUT = 300


def run_pandoc(
    args: Sequence[str],
    workdir: pathlib.Path,
    *,
    input_text: str | None = None,
    executable_name: str = "pandoc",
) -> str:
    """Runs a renderer without inherited credentials or user TeX configuration.

    Timeouts and interruptions terminate the entire renderer process group on
    POSIX. Pandoc also receives a memory limit; XeLaTeX disables shell escape.

    Args:
        args: Command-line arguments for the selected renderer.
        workdir: Existing isolated directory for renderer files and its home.
        input_text: Standard-input content, or None to close standard input.
        executable_name: One of pandoc, xelatex, or fc-match.

    Returns:
        The renderer's standard output. Diagnostics are printed separately.

    Raises:
        ValueError: The executable name is not supported.
        RuntimeError: The required executable is not installed.
        OSError: The isolated directory or process cannot be created.
        subprocess.TimeoutExpired: Rendering exceeds TIMEOUT seconds.
        subprocess.CalledProcessError: The renderer exits unsuccessfully.
    """
    home = workdir / "home"
    home.mkdir(exist_ok=True)
    environment = {
        "PATH": os.defpath,
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "TMPDIR": str(workdir),
        "XDG_CACHE_HOME": str(home / "cache"),
        "TEXMFVAR": str(home / "texmf-var"),
        "TEXMFCONFIG": str(home / "texmf-config"),
        "openin_any": "p",
        "openout_any": "p",
        "shell_escape": "f",
    }
    if executable_name not in {"pandoc", "xelatex", "fc-match"}:
        raise ValueError(f"Unsupported rendering executable: {executable_name}")
    executable = shutil.which(executable_name)
    if not executable:
        raise RuntimeError(
            f"{executable_name} is required to build the archive"
        )
    command = [executable, *args]
    if executable_name == "pandoc":
        command += ["+RTS", "-M512M", "-RTS"]
    with subprocess.Popen(
        command,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=workdir,
        env=environment,
        start_new_session=os.name == "posix",
    ) as process:
        try:
            stdout, stderr = process.communicate(
                input=input_text, timeout=TIMEOUT
            )
        except BaseException:
            # Pandoc starts XeLaTeX as a child. Killing only Pandoc leaves the
            # engine running, possibly writing into an already-removed stage.
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
            process.communicate()
            raise
        if process.returncode:
            # Surface useful diagnostics in CI without dumping an entire book.
            print((stdout + "\n" + stderr)[-8000:])
            raise subprocess.CalledProcessError(
                process.returncode, command, output=stdout, stderr=stderr
            )
    if stderr:
        print(stderr, end="")
    return stdout


def safe_url(url: str) -> bool:
    """Reports whether a URL can remain a link in a rendered archive.

    Args:
        url: A possibly malformed URL from untrusted Markdown.

    Returns:
        True for fragment links and HTTP, HTTPS, or mailto URLs. Malformed
        authorities and unsupported schemes return False.
    """
    try:
        return url.startswith("#") or parse.urlsplit(url).scheme.lower() in {
            "https",
            "http",
            "mailto",
        }
    except ValueError:
        # Malformed authorities must become visible text, not abort a volume.
        return False


# Pandoc node variants and table tuple slots are deliberately handled together.
# pylint: disable=too-many-locals,too-many-return-statements,too-many-branches
def restrict_ast(value: Any) -> Any:
    """Copies a Pandoc tree without executable content or embedded resources.

    Pandoc uses heterogeneous dictionaries, lists, and scalars, including
    untagged table tuples. Recursive values intentionally use Any to preserve
    those structures. The input must follow Pandoc's JSON schema.

    Args:
        value: A Pandoc node, container, or scalar to copy and restrict.

    Returns:
        A restricted tree preserving visible text and anchors without raw
        code, embedded resources, or arbitrary attributes. Only the trusted
        navigation classes unnumbered and unlisted survive.
    """
    if isinstance(value, list):
        return [restrict_ast(item) for item in value]
    if not isinstance(value, dict):
        return value
    node_type = value.get("t")
    content = value.get("c")
    if node_type in {"RawInline", "Math"}:
        return {"t": "Code", "c": [["", [], []], content[1]]}
    if node_type == "RawBlock":
        return {"t": "CodeBlock", "c": [["", [], []], content[1]]}
    if node_type in {"Link", "Image"}:
        _, label, target = content
        label = restrict_ast(label)
        if node_type == "Image":
            # Display original source rather than retrieving user-selected
            # files/URLs.
            label += [
                {"t": "Space"},
                {"t": "Str", "c": "[image source: " + target[0] + "]"},
            ]
        if safe_url(target[0]):
            return {"t": "Link", "c": [["", [], []], label, target]}
        visible_target = [
            {"t": "Space"},
            {"t": "Str", "c": "(" + target[0] + ")"},
        ]
        return {"t": "Span", "c": [["", [], []], label + visible_target]}
    result = {key: restrict_ast(item) for key, item in value.items()}
    # Preserve generated anchors and only safe navigation classes.
    attribute_index = {
        "Header": 1,
        "Div": 0,
        "Span": 0,
        "Code": 0,
        "CodeBlock": 0,
        "Table": 0,
        "Figure": 0,
    }.get(node_type)
    if attribute_index is not None:
        attributes = result["c"][attribute_index]
        attributes[1] = [
            name for name in attributes[1] if name in {"unnumbered", "unlisted"}
        ]
        attributes[2] = []
        if node_type == "Header" and not attributes[0].startswith(
            "discussion-"
        ):
            for name in ("unnumbered", "unlisted"):
                if name not in attributes[1]:
                    attributes[1].append(name)
    if node_type == "Table":
        # TableHead, TableBody, TableFoot, Row and Cell are untagged tuples,
        # so the normal node traversal cannot recognize their Attr fields.
        head, bodies, foot = result["c"][3:6]
        sections = [(head, [head[1]]), (foot, [foot[1]])]
        sections.extend((body, body[2:4]) for body in bodies)
        for section, row_groups in sections:
            section[0][1:] = [[], []]
            for rows in row_groups:
                for row in rows:
                    row[0][1:] = [[], []]
                    for cell in row[1]:
                        cell[0][1:] = [[], []]
    return result


# pylint: enable=too-many-locals,too-many-return-statements,too-many-branches


def visible_inline_text(inlines: Sequence[dict[str, Any]]) -> str | None:
    """Read a label whose visible text can be determined without rendering.

    Args:
        inlines: Pandoc inline nodes from the restricted AST. Supported nodes
            include plain text, spaces, basic emphasis, spans, and citations.

    Returns:
        The concatenated visible text, or None if any node has uncertain display
        semantics. The input nodes are not modified.
    """
    parts = []
    for inline in inlines:
        kind, content = inline.get("t"), inline.get("c")
        if kind == "Str":
            parts.append(content)
        elif kind in {"Space", "SoftBreak"}:
            parts.append(" ")
        elif kind in {
            "Emph",
            "Strong",
            "Strikeout",
            "SmallCaps",
            "Superscript",
            "Subscript",
        }:
            nested = visible_inline_text(content)
            if nested is None:
                return None
            parts.append(nested)
        elif kind in {"Span", "Cite"}:
            nested = visible_inline_text(content[1])
            if nested is None:
                return None
            parts.append(nested)
        else:
            return (
                None  # Do not rewrite labels whose visible text is uncertain.
            )
    return "".join(parts)


def latex_line_breaks(value: Any) -> Any:
    """Add trusted PDF line-break opportunities to an already restricted AST.

    Call this only after restricting untrusted raw nodes. It inserts constant
    LaTeX commands between preserved text pieces and normalizes literal URL
    labels so Pandoc can use its URL line-breaking support. EPUB input should
    remain the original restricted AST.

    Args:
        value: A restricted Pandoc node, list, mapping, or scalar.

    Returns:
        A transformed AST value without mutating the input. Source characters
        and
        link destinations are preserved; unchanged attributes/targets may share
        references with the input. Only fixed layout commands become raw TeX.
    """
    if isinstance(value, list):
        result = []
        for item in value:
            if (
                isinstance(item, dict)
                and item.get("t") == "Str"
                and (
                    len(item["c"]) >= 40
                    or "/" in item["c"]
                    or "\u00a0" in item["c"]
                )
            ):
                pattern = (
                    r"(?<=[/_?=&,;:+.*…‧()\-\u00a0])"
                    if len(item["c"]) >= 40
                    else r"(?<=[/\u00a0])"
                )
                parts = [part for part in re.split(pattern, item["c"]) if part]
                for index, part in enumerate(parts):
                    if index:
                        result.append(
                            {"t": "RawInline", "c": ["latex", r"\allowbreak{}"]}
                        )
                    result.append({"t": "Str", "c": part})
            else:
                result.append(latex_line_breaks(item))
        return result
    if isinstance(value, dict):
        if value.get("t") == "Link":
            attr, label, target = value["c"]
            if (
                target[0].startswith(("https://", "http://"))
                and visible_inline_text(label) == target[0]
            ):
                # A formatted literal URL otherwise becomes an unbreakable
                # href label. Plain URL text lets Pandoc use xurl's breakpoints.
                return {
                    "t": "Link",
                    "c": [attr, [{"t": "Str", "c": target[0]}], target],
                }
        return {key: latex_line_breaks(item) for key, item in value.items()}
    return value


def _record_headers(value: Any) -> Iterator[str]:
    """Yield record-heading identifiers from nested Pandoc nodes.

    Args:
        value: Pandoc mapping, list, or scalar to traverse without mutation.

    Yields:
        Each encountered record-heading ID, including duplicate occurrences.
    """
    if isinstance(value, dict):
        if value.get("t") == "Header" and archive_manifest.is_record_anchor(
            value["c"][1][0]
        ):
            yield value["c"][1][0]
        for child in value.values():
            yield from _record_headers(child)
    elif isinstance(value, list):
        for child in value:
            yield from _record_headers(child)


def validate_document_records(
    document: Mapping[str, Any],
    expected_anchors: Collection[str],
    context: str,
) -> None:
    """Require one top-level parsed heading for every saved record.

    Text inside code fences is not a heading. A record heading nested in a quote
    or list is rejected even if its ID is otherwise present.

    Args:
        document: Restricted Pandoc document with a ``blocks`` list.
        expected_anchors: Reiterable collection of database-derived record IDs,
            each expected exactly once. Do not pass a one-shot iterator.
        context: Human-readable document label used in error messages.

    Raises:
        ValueError: If record headings are missing, duplicated, unexpected, or
            nested rather than top-level. No files or AST nodes are modified.
    """
    archive_manifest.validate_record_anchors(
        expected_anchors, _record_headers(document["blocks"]), context
    )
    # A body may also absorb later headings into a list, quote, or other block.
    top_level = [
        block["c"][1][0]
        for block in document["blocks"]
        if block["t"] == "Header"
        and archive_manifest.is_record_anchor(block["c"][1][0])
    ]
    archive_manifest.validate_record_anchors(
        expected_anchors, top_level, f"top-level headings in {context}"
    )


def prepare_document(
    file: pathlib.Path,
    template: str,
    workdir: pathlib.Path,
    expected_anchors: Collection[str],
) -> str:
    """Builds a restricted Pandoc document with metadata from trusted code.

    Args:
        file: Generated Markdown named ven_anigha_reddit_archive_YYYY.md.
        template: Trusted Markdown metadata template with a year placeholder.
        workdir: Existing isolated directory for Pandoc's temporary files.
        expected_anchors: Database-derived record IDs expected exactly once as
            top-level headings in the generated document.

    Returns:
        JSON for the restricted document, with generated trusted metadata.

    Raises:
        ValueError: The filename, metadata block, or Pandoc JSON is invalid.
        OSError: The source cannot be read or Pandoc cannot be started.
        RuntimeError: Pandoc is not installed.
        subprocess.TimeoutExpired: Pandoc exceeds the render timeout.
        subprocess.CalledProcessError: Pandoc exits unsuccessfully.
    """
    match = re.fullmatch(r"ven_anigha_reddit_archive_(\d{4})", file.stem)
    if not match:
        raise ValueError(f"Unexpected archive filename: {file.name}")
    markdown_text = file.read_text(encoding="utf-8")
    if (
        not markdown_text.startswith("---\n")
        or "\n---\n" not in markdown_text[4:]
    ):
        raise ValueError(f"Missing generated metadata: {file.name}")
    body = markdown_text.split("\n---\n", 1)[1]
    document = json.loads(
        run_pandoc(
            ["--sandbox", "-f", READER, "-t", "json"], workdir, input_text=body
        )
    )
    # Metadata comes from code, never from a second YAML block in Reddit
    # content.
    trusted_document = json.loads(
        run_pandoc(
            ["--sandbox", "-f", "markdown", "-t", "json"],
            workdir,
            input_text=template.format(year=int(match[1])),
        )
    )
    document = restrict_ast(document)
    document["meta"] = trusted_document["meta"]
    validate_document_records(document, expected_anchors, file.name)
    return json.dumps(document, ensure_ascii=False)


def validate_epub_records(
    path: pathlib.Path, expected_anchors: Collection[str]
) -> None:
    """Check record destinations in the EPUB XHTML files before publication.

    Args:
        path: Path to a generated EPUB archive. The archive is only read.
        expected_anchors: Database-derived record IDs expected exactly once.

    Raises:
        ValueError: If destinations are missing, duplicated, or unexpected.
        OSError: If the EPUB cannot be read.
        zipfile.BadZipFile: If the EPUB ZIP container is malformed.
        ElementTree.ParseError: If an XHTML document is not well-formed XML.
    """
    anchors = []
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if name.endswith(".xhtml"):
                for element in ElementTree.fromstring(
                    archive.read(name)
                ).iter():
                    identifier = element.get("id", "")
                    if archive_manifest.is_record_anchor(identifier):
                        anchors.append(identifier)
    archive_manifest.validate_record_anchors(
        expected_anchors, anchors, path.name
    )


def validate_output(path: pathlib.Path) -> None:
    """Checks the basic integrity of an archive file before publication.

    Args:
        path: PDF, EPUB, or other nonempty output file to validate.

    Raises:
        ValueError: The output is missing, empty, or has an invalid format.
        OSError: The output cannot be read.
        zipfile.BadZipFile: An EPUB is not a readable ZIP archive.
        KeyError: An EPUB lacks its required mimetype entry.
    """
    if path.suffix == ".pdf":
        data = path.read_bytes()
        if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-1024:]:
            raise ValueError(f"Invalid or incomplete PDF: {path}")
    elif path.suffix == ".epub":
        with zipfile.ZipFile(path) as archive:
            if (
                archive.testzip()
                or archive.read("mimetype") != b"application/epub+zip"
            ):
                raise ValueError(f"Invalid EPUB: {path}")
            if "META-INF/container.xml" not in archive.namelist():
                raise ValueError(f"Missing EPUB container: {path}")
    elif not path.is_file() or not path.stat().st_size:
        raise ValueError(f"Missing or empty output: {path}")


# Keep backup, publication, and recovery in one auditable transaction scope.
# pylint: disable-next=too-many-branches
def publish_files(pairs: Iterable[tuple[pathlib.Path, pathlib.Path]]) -> None:
    """Publishes validated files atomically and rolls back failures.

    Backups live on each destination filesystem, so restoring them is an atomic
    rename and does not require space for another copy. An interrupted process
    is not a multi-file transaction; failed recoveries keep backups for repair.

    Args:
        pairs: Staged source and published destination paths. Sources are moved
            into place only after every output is validated and backed up.

    Raises:
        ValueError: A staged output fails validation.
        OSError: Validation, backup, or replacement fails; changed destinations
            are restored when possible.
        zipfile.BadZipFile: A staged EPUB is not a readable ZIP archive.
        KeyError: A staged EPUB lacks its required mimetype entry.
        RuntimeError: Publication and recovery both fail. Backup locations are
            included in the error, and those backups remain on disk.
    """
    pairs = list(pairs)
    for source, _ in pairs:
        validate_output(source)
    backup_dirs: dict[pathlib.Path, pathlib.Path] = {}
    planned: list[tuple[pathlib.Path, pathlib.Path, pathlib.Path, bool]] = []
    replaced: list[tuple[pathlib.Path, pathlib.Path, bool]] = []
    preserve_backups = False
    try:
        # Prepare every backup before replacing even the first published file.
        for index, (source, target) in enumerate(pairs):
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.parent not in backup_dirs:
                backup_dirs[target.parent] = pathlib.Path(
                    tempfile.mkdtemp(
                        prefix=".archive-backup-", dir=target.parent
                    )
                )
            backup = backup_dirs[target.parent] / str(index)
            existed = target.exists()
            if existed:
                shutil.copy2(target, backup)
            planned.append((source, target, backup, existed))
        for source, target, backup, existed in planned:
            os.replace(source, target)
            replaced.append((target, backup, existed))
    except BaseException as error:
        recovery_errors = []
        for target, backup, existed in reversed(replaced):
            try:
                if existed:
                    os.replace(backup, target)
                else:
                    target.unlink(missing_ok=True)
            # Interruptions must also preserve copies for manual recovery.
            # pylint: disable-next=broad-exception-caught
            except BaseException as recovery_error:
                recovery_errors.append(f"{target}: {recovery_error}")
        if recovery_errors:
            preserve_backups = True
            locations = ", ".join(str(path) for path in backup_dirs.values())
            raise RuntimeError(
                f"Archive publication failed; recovery also failed for "
                f"{'; '.join(recovery_errors)}. "
                f"Previous files are preserved in: {locations}"
            ) from error
        raise
    finally:
        if not preserve_backups:
            for directory in backup_dirs.values():
                shutil.rmtree(directory, ignore_errors=True)


# Keep each volume's coverage, font setup, and staged outputs together.
# pylint: disable-next=too-many-locals
def convert_archive(
    input_dir: str | os.PathLike[str],
    epub_dir: str | os.PathLike[str],
    pdf_dir: str | os.PathLike[str],
    template: str,
    *,
    expected_records: Mapping[str, Collection[str]],
) -> None:
    """Renders every Markdown volume before publishing validated EPUB and PDF.

    Args:
        input_dir: Directory containing generated yearly Markdown volumes.
        epub_dir: Destination directory for EPUB files.
        pdf_dir: Destination directory for PDF files.
        template: Trusted Markdown metadata template with a year placeholder.
        expected_records: Volume stems mapped to database-derived record IDs.
            Markdown volumes, parsed headings, and EPUB destinations must match.

    Raises:
        ValueError: Volume, heading, or EPUB coverage is incomplete; no Markdown
            exists; or a font, glyph, or rendered output is invalid.
        OSError: A source, staging directory, or published file is inaccessible.
        RuntimeError: A renderer is missing or publication recovery fails.
        subprocess.TimeoutExpired: A renderer exceeds the render timeout.
        subprocess.CalledProcessError: Pandoc, XeLaTeX, or fc-match fails.
        zipfile.BadZipFile: A rendered EPUB is not a readable ZIP archive.
        KeyError: A rendered EPUB lacks its required mimetype entry.
        ElementTree.ParseError: Rendered EPUB XHTML is not well-formed XML.
    """
    files = sorted(pathlib.Path(input_dir).glob("*.md"))
    if not files:
        raise ValueError("No archive Markdown files were generated")
    actual_volumes = {file.stem for file in files}
    expected_volumes = set(expected_records)
    if actual_volumes != expected_volumes:
        raise ValueError(
            "Archive volume coverage failed: "
            f"missing {sorted(expected_volumes - actual_volumes)}, "
            f"unexpected {sorted(actual_volumes - expected_volumes)}"
        )
    epub_dir = pathlib.Path(epub_dir).resolve()
    pdf_dir = pathlib.Path(pdf_dir).resolve()
    # Stage on the destination filesystem before publishing the complete set.
    epub_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".archive-render-", dir=epub_dir.parent
    ) as temp:
        workdir = pathlib.Path(temp).resolve()
        header_template = (
            pathlib.Path(__file__)
            .with_name("book_header.tex")
            .read_text(encoding="utf-8")
        )
        pairs = []
        for file in files:
            expected_anchors = expected_records[file.stem]
            document = prepare_document(
                file, template, workdir, expected_anchors
            )
            for suffix, target_dir in ((".epub", epub_dir), (".pdf", pdf_dir)):
                output = workdir / (file.stem + suffix)
                args = [
                    "--sandbox",
                    "-f",
                    "json",
                    "-o",
                    str(output),
                    "--toc",
                    "--toc-depth=2",
                ]
                if suffix == ".pdf":
                    # A relative TeX input works with paranoid openin_any=p.
                    # Pandoc's PDF wrapper passes an absolute input path, which
                    # that policy correctly rejects on some TeX installations.
                    tex = output.with_suffix(".tex")
                    header = output.with_suffix(".header.tex")
                    header.write_text(
                        header_template
                        + font_fallback.unicode_fallbacks(
                            json.loads(document), workdir, run_pandoc
                        ),
                        encoding="utf-8",
                    )
                    run_pandoc(
                        [
                            "--sandbox",
                            "-f",
                            "json",
                            "-t",
                            "latex",
                            "--standalone",
                            "--toc",
                            "--toc-depth=2",
                            "-H",
                            str(header),
                            "-o",
                            str(tex),
                        ],
                        workdir,
                        input_text=json.dumps(
                            latex_line_breaks(json.loads(document)),
                            ensure_ascii=False,
                        ),
                    )
                    for _ in range(
                        3
                    ):  # Resolve the TOC, page numbers, and links.
                        log = run_pandoc(
                            [
                                "-no-shell-escape",
                                "-interaction=nonstopmode",
                                "-halt-on-error",
                                tex.name,
                            ],
                            workdir,
                            executable_name="xelatex",
                        )
                        missing = [
                            line
                            for line in log.splitlines()
                            if "Missing character:" in line
                        ]
                        if missing:
                            raise ValueError(
                                "PDF has unsupported glyphs: "
                                + "\n".join(missing)
                            )
                else:
                    args += ["--split-level=2"]
                    run_pandoc(args, workdir, input_text=document)
                validate_output(output)
                if suffix == ".epub":
                    validate_epub_records(output, expected_anchors)
                pairs.append((output, target_dir / output.name))
        publish_files(pairs)
