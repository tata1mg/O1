"""stage_tools broker: the sanctioned awk/sed/redirection replacement. Confinement is the contract —
every destination resolves inside the run dir (the process cwd), traversal and symlinks included."""

import base64
import hashlib
import subprocess
import sys
from pathlib import Path

BROKER = Path(__file__).resolve().parents[2] / "src" / "rfc_agent" / "hooks" / "stage_tools.py"


def _run(run_dir, *args):
    return subprocess.run([sys.executable, str(BROKER), *args],
                          cwd=run_dir, capture_output=True, text=True, timeout=10)


def test_slice_line_range_and_regex(tmp_path):
    src = tmp_path / "meta.xml"
    src.write_text("".join(f"line {i}\n" for i in range(1, 11)))
    r = _run(tmp_path, "slice", str(src), "3-5")
    assert r.returncode == 0 and r.stdout == "line 3\nline 4\nline 5\n"
    r = _run(tmp_path, "slice", str(src), "line 1")
    assert r.returncode == 0 and r.stdout == "1:line 1\n10:line 10\n"


def test_slice_out_lands_inside_run_dir(tmp_path):
    src = tmp_path / "meta.xml"
    src.write_text("a\nb\nc\n")
    r = _run(tmp_path, "slice", str(src), "1-2", "--out", "raw/slices/head.xml")
    assert r.returncode == 0
    assert (tmp_path / "raw" / "slices" / "head.xml").read_text() == "a\nb\n"


def test_slice_out_traversal_escape_refused(tmp_path):
    src = tmp_path / "meta.xml"
    src.write_text("a\n")
    r = _run(tmp_path, "slice", str(src), "1-1", "--out", "../../etc/evil")
    assert r.returncode == 2 and "escapes the run dir" in r.stderr
    assert not (tmp_path.parent / "etc").exists()


def test_symlink_escape_refused(tmp_path):
    run_dir, outside = tmp_path / "run", tmp_path / "outside"
    run_dir.mkdir()
    outside.mkdir()
    (run_dir / "link").symlink_to(outside)
    src = run_dir / "meta.xml"
    src.write_text("a\n")
    r = _run(run_dir, "slice", str(src), "1-1", "--out", "link/evil.txt")
    assert r.returncode == 2 and "escapes the run dir" in r.stderr
    assert not (outside / "evil.txt").exists()


def test_hash_prints_sha256(tmp_path):
    src = tmp_path / "meta.xml"
    src.write_bytes(b"payload")
    r = _run(tmp_path, "hash", str(src))
    assert r.returncode == 0
    assert r.stdout.strip() == hashlib.sha256(b"payload").hexdigest()


def test_save_image_decodes_b64_file_inside_run_dir(tmp_path):
    png = b"\x89PNG fake bytes"
    b64 = tmp_path / "shot.b64"
    b64.write_text("data:image/png;base64," + base64.b64encode(png).decode())
    r = _run(tmp_path, "save-image", str(b64), "raw/shots/s1.png")
    assert r.returncode == 0
    assert (tmp_path / "raw" / "shots" / "s1.png").read_bytes() == png


def test_save_image_escape_refused(tmp_path):
    b64 = tmp_path / "shot.b64"
    b64.write_text(base64.b64encode(b"x").decode())
    assert _run(tmp_path, "save-image", str(b64), "../evil.png").returncode == 2
    assert _run(tmp_path, "save-image", str(b64), "/etc/evil.png").returncode == 2


def test_bad_input_is_a_usage_error_not_a_traceback(tmp_path):
    assert _run(tmp_path, "slice").returncode == 2          # missing args
    assert _run(tmp_path, "explode", "x").returncode == 2   # unknown subcommand
    b64 = tmp_path / "not.b64"
    b64.write_text("!!! not base64 !!!")
    r = _run(tmp_path, "save-image", str(b64), "out.png")
    assert r.returncode == 2 and "base64" in r.stderr


def test_help_is_self_describing(tmp_path):
    r = _run(tmp_path, "help")
    assert r.returncode == 0 and "slice" in r.stdout and "run dir" in r.stdout
