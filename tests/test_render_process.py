import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from src import render_archive as render


@pytest.mark.skipif(os.name != "posix", reason="Process-group isolation is POSIX-specific")
def test_timeout_kills_renderer_descendants(tmp_path, monkeypatch):
    # Use a harmless local helper instead of running attacker-controlled TeX.
    script = (
        "import os,time; child=os.fork(); "
        "time.sleep(0.8 if child==0 else 3); "
        "open('descendant-survived','w').write('alive') if child==0 else None"
    )
    monkeypatch.setattr(render.shutil, "which", lambda name: sys.executable)
    monkeypatch.setattr(render, "TIMEOUT", 0.3)
    with pytest.raises(subprocess.TimeoutExpired):
        render.run_pandoc(["-c", script], tmp_path)
    time.sleep(0.7)
    assert not (tmp_path / "descendant-survived").exists()


def test_nonzero_process_status_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(render.shutil, "which", lambda name: sys.executable)
    with pytest.raises(subprocess.CalledProcessError) as failure:
        render.run_pandoc(["-c", "import sys; print('failed',file=sys.stderr); sys.exit(9)"], tmp_path)
    assert failure.value.returncode == 9
    assert "failed" in failure.value.stderr


def test_xelatex_uses_same_isolation_without_haskell_options(tmp_path, monkeypatch):
    monkeypatch.setattr(render.shutil, "which", lambda name: sys.executable)
    output = render.run_pandoc(["-c", "import sys; print(sys.argv[1:])"], tmp_path, executable_name="xelatex")
    assert output.strip() == "[]"


def test_other_executables_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="Unsupported rendering executable"):
        render.run_pandoc([], tmp_path, executable_name="sh")


def make_publication(tmp_path, count):
    pairs = []
    for index in range(count):
        source, target = tmp_path / f"source{index}.md", tmp_path / f"target{index}.md"
        source.write_text(f"new{index}")
        target.write_text(f"old{index}")
        pairs.append((source, target))
    return pairs


def test_replace_failure_restores_only_changed_files_atomically(tmp_path, monkeypatch):
    pairs = make_publication(tmp_path, 2)
    real_replace = render.os.replace
    calls = []

    def replace(source, target):
        calls.append((Path(source), Path(target)))
        if target == pairs[1][1]:
            raise PermissionError("Second target is read-only")
        return real_replace(source, target)

    monkeypatch.setattr(render.os, "replace", replace)
    with pytest.raises(PermissionError):
        render.publish_files(pairs)
    assert [target.read_text() for _, target in pairs] == ["old0", "old1"]
    assert len(calls) == 3
    assert calls[-1][0].parent.parent == pairs[0][1].parent
    assert not list(tmp_path.glob(".archive-backup-*"))


def test_failed_recovery_keeps_backup_and_recovers_other_files(tmp_path, monkeypatch):
    pairs = make_publication(tmp_path, 3)
    real_replace = render.os.replace

    def replace(source, target):
        if target == pairs[2][1]:
            raise PermissionError("Third publication fails")
        if target == pairs[1][1] and Path(source).parent.name.startswith(".archive-backup-"):
            raise PermissionError("Second recovery fails")
        return real_replace(source, target)

    monkeypatch.setattr(render.os, "replace", replace)
    with pytest.raises(RuntimeError, match="Previous files are preserved") as failure:
        render.publish_files(pairs)
    assert [target.read_text() for _, target in pairs] == ["old0", "new1", "old2"]
    assert isinstance(failure.value.__cause__, PermissionError)
    backups = list(tmp_path.glob(".archive-backup-*"))
    assert len(backups) == 1
    assert (backups[0] / "1").read_text() == "old1"
    assert str(backups[0]) in str(failure.value)


def test_all_backups_are_prepared_before_any_publication(tmp_path, monkeypatch):
    pairs = make_publication(tmp_path, 2)
    real_copy = render.shutil.copy2

    def copy(source, target):
        if source == pairs[1][1]:
            raise OSError("Backup filesystem is full")
        return real_copy(source, target)

    monkeypatch.setattr(render.shutil, "copy2", copy)
    with pytest.raises(OSError):
        render.publish_files(pairs)
    assert [target.read_text() for _, target in pairs] == ["old0", "old1"]
    assert all(source.exists() for source, _ in pairs)
    assert not list(tmp_path.glob(".archive-backup-*"))


def test_success_publishes_every_file_and_removes_backups(tmp_path):
    pairs = make_publication(tmp_path, 2)
    pairs[1][1].unlink()
    render.publish_files(iter(pairs))
    assert [target.read_text() for _, target in pairs] == ["new0", "new1"]
    assert not list(tmp_path.glob(".archive-backup-*"))


def test_rollback_removes_new_target(tmp_path, monkeypatch):
    pairs = make_publication(tmp_path, 2)
    pairs[0][1].unlink()
    real_replace = render.os.replace

    def replace(source, target):
        if target == pairs[1][1]:
            raise PermissionError("Second target is read-only")
        return real_replace(source, target)

    monkeypatch.setattr(render.os, "replace", replace)
    with pytest.raises(PermissionError):
        render.publish_files(pairs)
    assert not pairs[0][1].exists()
    assert pairs[1][1].read_text() == "old1"
