"""Fail-closed conversion of archived, untrusted Reddit Markdown.

Pandoc's sandbox does not sandbox TeX. Defense in depth: only a restricted AST
reaches XeLaTeX, no resources are embedded, shell escape is disabled, subprocesses
receive no credentials, and CI renders in a separate credential-free job.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

READER = (
    "markdown-raw_tex-raw_html-raw_attribute-yaml_metadata_block"
    "-latex_macros-tex_math_dollars-tex_math_single_backslash-tex_math_double_backslash"
)
TIMEOUT = 300


def run_pandoc(args, workdir, *, input_text=None, executable_name="pandoc"):
    """Do not inherit Reddit/GitHub tokens, user TeX config, or shell escape."""
    home = workdir / "home"
    home.mkdir(exist_ok=True)
    env = {
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
        raise RuntimeError(f"{executable_name} is required to build the archive")
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
        env=env,
        start_new_session=os.name == "posix",
    ) as process:
        try:
            stdout, stderr = process.communicate(input=input_text, timeout=TIMEOUT)
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
            raise subprocess.CalledProcessError(process.returncode, command, output=stdout, stderr=stderr)
    if stderr:
        print(stderr, end="")
    return stdout


def safe_url(url):
    try:
        return url.startswith("#") or urlsplit(url).scheme.lower() in {"https", "http", "mailto"}
    except ValueError:
        # Malformed authorities must become visible text, not abort a volume.
        return False


def restrict_ast(value):
    """Preserve visible text while preventing raw code and embedded resources."""
    if isinstance(value, list):
        return [restrict_ast(item) for item in value]
    if not isinstance(value, dict):
        return value
    kind, content = value.get("t"), value.get("c")
    if kind in {"RawInline", "Math"}:
        return {"t": "Code", "c": [["", [], []], content[1]]}
    if kind == "RawBlock":
        return {"t": "CodeBlock", "c": [["", [], []], content[1]]}
    if kind in {"Link", "Image"}:
        attr, label, target = content
        label = restrict_ast(label)
        if kind == "Image":
            # Display original source rather than retrieving user-selected files/URLs.
            label += [{"t": "Space"}, {"t": "Str", "c": "[image source: " + target[0] + "]"}]
        if safe_url(target[0]):
            return {"t": "Link", "c": [["", [], []], label, target]}
        return {"t": "Span", "c": [["", [], []], label + [{"t": "Space"}, {"t": "Str", "c": "(" + target[0] + ")"}]]}
    result = {key: restrict_ast(item) for key, item in value.items()}
    # Preserve anchors but discard user-selected classes and attributes.
    attr_index = {"Header": 1, "Div": 0, "Span": 0, "Code": 0, "CodeBlock": 0, "Table": 0, "Figure": 0}.get(kind)
    if attr_index is not None:
        attr = result["c"][attr_index]
        attr[1] = []
        attr[2] = []
    if kind == "Table":
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


def prepare_document(file, template, workdir):
    match = re.fullmatch(r"ven_anigha_reddit_archive_(\d{4})", file.stem)
    if not match:
        raise ValueError(f"Unexpected archive filename: {file.name}")
    text = file.read_text(encoding="utf-8")
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise ValueError(f"Missing generated metadata: {file.name}")
    body = text.split("\n---\n", 1)[1]
    document = json.loads(run_pandoc(["--sandbox", "-f", READER, "-t", "json"], workdir, input_text=body))
    # Metadata comes from code, never from a second YAML block in Reddit content.
    trusted = json.loads(
        run_pandoc(
            ["--sandbox", "-f", "markdown", "-t", "json"], workdir, input_text=template.format(year=int(match[1]))
        )
    )
    document = restrict_ast(document)
    document["meta"] = trusted["meta"]
    return json.dumps(document, ensure_ascii=False)


def validate_output(path):
    if path.suffix == ".pdf":
        data = path.read_bytes()
        if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-1024:]:
            raise ValueError(f"Invalid or incomplete PDF: {path}")
    elif path.suffix == ".epub":
        with zipfile.ZipFile(path) as archive:
            if archive.testzip() or archive.read("mimetype") != b"application/epub+zip":
                raise ValueError(f"Invalid EPUB: {path}")
            if "META-INF/container.xml" not in archive.namelist():
                raise ValueError(f"Missing EPUB container: {path}")
    elif not path.is_file() or not path.stat().st_size:
        raise ValueError(f"Missing or empty output: {path}")


def publish_files(pairs):
    """Validate all outputs, replace per-file atomically, and roll back failures.

    Backups live on each destination filesystem, so restoring them is an atomic
    rename and does not require space for another copy. An interrupted process
    is not a multi-file transaction; failed recoveries keep backups for repair.
    """
    pairs = list(pairs)
    for source, _ in pairs:
        validate_output(source)
    backup_dirs = {}
    planned, replaced = [], []
    preserve_backups = False
    try:
        # Prepare every backup before replacing even the first published file.
        for index, (source, target) in enumerate(pairs):
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.parent not in backup_dirs:
                backup_dirs[target.parent] = Path(tempfile.mkdtemp(prefix=".archive-backup-", dir=target.parent))
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
            except BaseException as recovery_error:  # noqa: BLE001 - Interruptions must also preserve recovery copies.
                recovery_errors.append(f"{target}: {recovery_error}")
        if recovery_errors:
            preserve_backups = True
            locations = ", ".join(str(path) for path in backup_dirs.values())
            raise RuntimeError(
                f"Archive publication failed; recovery also failed for {'; '.join(recovery_errors)}. "
                f"Previous files are preserved in: {locations}"
            ) from error
        raise
    finally:
        if not preserve_backups:
            for directory in backup_dirs.values():
                shutil.rmtree(directory, ignore_errors=True)


def convert_archive(input_dir, epub_dir, pdf_dir, template):
    files = sorted(Path(input_dir).glob("*.md"))
    if not files:
        raise ValueError("No archive Markdown files were generated")
    epub_dir, pdf_dir = Path(epub_dir).resolve(), Path(pdf_dir).resolve()
    # Stage on the destination filesystem, then publish the complete validated set.
    epub_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".archive-render-", dir=epub_dir.parent) as temp:
        workdir = Path(temp).resolve()
        pairs = []
        for file in files:
            document = prepare_document(file, template, workdir)
            for suffix, target_dir in ((".epub", epub_dir), (".pdf", pdf_dir)):
                output = workdir / (file.stem + suffix)
                args = ["--sandbox", "-f", "json", "-o", str(output), "--toc", "--toc-depth=2"]
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
                    for _ in range(3):  # Resolve the TOC, page numbers, and links.
                        run_pandoc(
                            ["-no-shell-escape", "-interaction=nonstopmode", "-halt-on-error", tex.name],
                            workdir,
                            executable_name="xelatex",
                        )
                else:
                    run_pandoc(args, workdir, input_text=document)
                validate_output(output)
                pairs.append((output, target_dir / output.name))
        publish_files(pairs)
