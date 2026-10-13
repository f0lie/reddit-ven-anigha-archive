"""Fail-closed conversion of archived, untrusted Reddit Markdown.

Pandoc's sandbox does not sandbox TeX. Defense in depth: only a restricted AST
reaches XeLaTeX, no resources are embedded, shell escape is disabled,
subprocesses
receive no credentials, and CI renders in a separate credential-free job.
"""

from collections.abc import Iterable
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
import zipfile

READER = (
    "markdown"
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
        executable_name: Supported executable name, either pandoc or xelatex.

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
    if executable_name not in {"pandoc", "xelatex"}:
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
        code, embedded resources, or user-selected classes and attributes.
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
    # Preserve anchors but discard user-selected classes and attributes.
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
        attributes[1] = []
        attributes[2] = []
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


def prepare_document(
    file: pathlib.Path, template: str, workdir: pathlib.Path
) -> str:
    """Builds a restricted Pandoc document with metadata from trusted code.

    Args:
        file: Generated Markdown named ven_anigha_reddit_archive_YYYY.md.
        template: Trusted Markdown metadata template with a year placeholder.
        workdir: Existing isolated directory for Pandoc's temporary files.

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
    return json.dumps(document, ensure_ascii=False)


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


def convert_archive(
    input_dir: str | os.PathLike[str],
    epub_dir: str | os.PathLike[str],
    pdf_dir: str | os.PathLike[str],
    template: str,
) -> None:
    """Renders every Markdown volume before publishing validated EPUB and PDF.

    Args:
        input_dir: Directory containing generated yearly Markdown volumes.
        epub_dir: Destination directory for EPUB files.
        pdf_dir: Destination directory for PDF files.
        template: Trusted Markdown metadata template with a year placeholder.

    Raises:
        ValueError: No Markdown exists or a source or rendered output is
            invalid.
        OSError: A source, staging directory, or published file is inaccessible.
        RuntimeError: A renderer is missing or publication recovery fails.
        subprocess.TimeoutExpired: A renderer exceeds the render timeout.
        subprocess.CalledProcessError: Pandoc or XeLaTeX exits unsuccessfully.
        zipfile.BadZipFile: A rendered EPUB is not a readable ZIP archive.
        KeyError: A rendered EPUB lacks its required mimetype entry.
    """
    files = sorted(pathlib.Path(input_dir).glob("*.md"))
    if not files:
        raise ValueError("No archive Markdown files were generated")
    epub_dir = pathlib.Path(epub_dir).resolve()
    pdf_dir = pathlib.Path(pdf_dir).resolve()
    # Stage on the destination filesystem, then publish the complete validated
    # set.
    epub_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".archive-render-", dir=epub_dir.parent
    ) as temporary_directory:
        workdir = pathlib.Path(temporary_directory).resolve()
        pairs: list[tuple[pathlib.Path, pathlib.Path]] = []
        for file in files:
            document = prepare_document(file, template, workdir)
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
                            "-o",
                            str(tex),
                        ],
                        workdir,
                        input_text=document,
                    )
                    for _ in range(
                        3
                    ):  # Resolve the TOC, page numbers, and links.
                        run_pandoc(
                            [
                                "-no-shell-escape",
                                "-interaction=nonstopmode",
                                "-halt-on-error",
                                tex.name,
                            ],
                            workdir,
                            executable_name="xelatex",
                        )
                else:
                    run_pandoc(args, workdir, input_text=document)
                validate_output(output)
                pairs.append((output, target_dir / output.name))
        publish_files(pairs)
