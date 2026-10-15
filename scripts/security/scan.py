#!/usr/bin/env python3
"""Security gate for rfc-agent. Stdlib only. Fails closed: a scanner that is not installed is a FAIL, never a silent pass.

  scan.py push <base_sha> <head_sha>   the commits being pushed (pre-push hook, and PR/push diff in CI)
  scan.py full                         whole history + committed tree (manual, before going public)
  scan.py deps                         dependency audits only (manifest changes, scheduled runs)
  scan.py all                          full + deps (local `make security`)

Output shows file:line and the rule or detector name. Secret values are never printed. A trufflehog finding
is identified by the first 16 hex chars of sha256(value); that id is what goes in policy.json to allow a
reviewed fixture. Allowed ids are matched on the value, so they still hold after the line moves.
"""
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True)
            .stdout.strip())
POLICY = json.loads((ROOT / "scripts/security/policy.json").read_text(encoding="utf-8"))
ZERO = "0" * 40
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # git's empty tree: base for a brand-new branch
SCAN_EXT = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".sh", ".yml", ".yaml"}
PIP_AUDIT = "pip-audit==2.10.1"

failures: list[str] = []
warnings: list[str] = []

# Colour only in a terminal, and never when NO_COLOR is set (https://no-color.org).
COLOUR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def paint(text, code):
    return f"\033[{code}m{text}\033[0m" if COLOUR else text


def fail(msg):
    failures.append(msg)
    print(f"{paint('FAIL', '1;31')}  {msg}", flush=True)


def warn(msg):
    warnings.append(msg)
    print(f"{paint('WARN', '33')}  {msg}", flush=True)


def info(msg):
    print(f"      {msg}", flush=True)


def step(label, fn, *args):
    """Print the stage name, run it, print how long it took. Lets a long scan show where it is."""
    print(f"\n{paint('==> ' + label, '1;36')}", flush=True)
    start = time.monotonic()
    fn(*args)
    print(f"    done in {time.monotonic() - start:.0f}s", flush=True)


def need(tool, hint):
    if shutil.which(tool):
        return True
    fail(f"{tool} is not installed; {hint}")
    return False


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def run(cmd, cwd=ROOT, **kw):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, **kw)


def changed_files(base, head):
    return [f for f in git("diff", "--name-only", "--diff-filter=ACMR", base, head).splitlines() if f]


def tracked_files():
    return [f for f in git("ls-files").splitlines() if f]


def added_lines(base, head):
    """(path, line_no, text) for every line added between base and head."""
    path, n = None, 0
    for line in git("diff", "-U0", "--no-color", base, head).splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
        elif line.startswith("@@"):
            m = re.match(r"@@ -\S+ \+(\d+)", line)
            n = int(m.group(1)) if m else 0
        elif line.startswith("+") and not line.startswith("+++"):
            yield path, n, line[1:]
            n += 1


def excluded(path):
    return any(path == e or path.startswith(e) for e in POLICY["internal_refs"]["exclude"])


def blocked_rule(path):
    """Return the policy pattern a path matches, or None. '/x' = exact root path, 'dir/' = prefix, else basename glob."""
    base = os.path.basename(path)
    for pat in POLICY["blocked_files"]:
        if pat.startswith("/"):
            if path == pat[1:]:
                return pat
        elif pat.endswith("/"):
            if path.startswith(pat):
                return pat
        elif fnmatch.fnmatch(base, pat):
            return pat
    return None


# ---------- checks ----------

def check_blocked(files):
    info(f"{len(files)} files")
    for f in files:
        rule = blocked_rule(f)
        if rule:
            fail(f"blocked file {f} (matches policy rule {rule!r}); it must never be committed")


def check_internal_refs(lines):
    """lines: iterable of (path, line_no, text). Names are configured in policy.json; mode decides fail vs warn."""
    cfg = POLICY["internal_refs"]
    hits = {}
    for path, n, text in lines:
        if not path or excluded(path):
            continue
        for kind in ("fail", "warn"):
            for pat in cfg[kind]:
                if re.search(pat, text):
                    hits.setdefault((kind, pat), []).append(f"{path}:{n}")
    for (kind, pat), locs in sorted(hits.items()):
        msg = f"internal reference /{pat}/ x{len(locs)}; first at {', '.join(locs[:3])}"
        if kind == "fail" and cfg["mode"] == "fail":
            fail(msg)
        else:
            warn(msg)


def check_semgrep(files):
    files = [f for f in files if Path(f).suffix in SCAN_EXT and (ROOT / f).is_file()]
    if not files or not need("semgrep", "install with: pip install semgrep==1.164.0"):
        return
    info(f"{len(files)} files with rules {', '.join(POLICY['semgrep_configs'])}; "
         "the rule packs download on the first run, so this can take a few minutes")
    cfgs = [c for r in POLICY["semgrep_configs"] for c in ("--config", r)]
    p = run(["semgrep", "scan", "--metrics=off", "--quiet", "--json", *cfgs, *files])
    try:
        data = json.loads(p.stdout)
    except json.JSONDecodeError:
        fail(f"semgrep produced no JSON (exit {p.returncode}): {p.stderr.strip()[:200]}")
        return
    for e in data.get("errors", []):
        fail(f"semgrep error: {e.get('message', '')[:160]}")
    for r in data.get("results", []):
        loc = f"{r['path']}:{r['start']['line']}"
        rule = r["check_id"].split(".")[-1]
        if r["extra"]["severity"] == "ERROR":
            fail(f"semgrep {rule} at {loc}")
        else:
            warn(f"semgrep {rule} at {loc}")


def check_trufflehog(args):
    """Verified and unverified secret-like values both fail. Unverified is not "safe": trufflehog cannot check a
    database password or most private keys against a service. A reviewed fixture goes in policy.json's allowlist."""
    if not need("trufflehog", "install from https://github.com/trufflesecurity/trufflehog (pin 3.88.28)"):
        return
    info("scanning git history; each secret is checked with its provider, so this can take a few minutes")
    p = run(["trufflehog", "--no-update", "--json", *args])
    if p.returncode != 0:
        fail(f"trufflehog exited {p.returncode}: {p.stderr.strip()[-200:]}")
        return
    allowed = set(POLICY["trufflehog_allow"])
    for line in p.stdout.splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        allow_id = hashlib.sha256(d.get("Raw", "").encode()).hexdigest()[:16]  # the value is hashed, never printed
        if allow_id in allowed:
            continue
        g = d.get("SourceMetadata", {}).get("Data", {}).get("Git", {})
        loc = f"{g.get('file')}:{g.get('line')} commit {(g.get('commit') or '')[:10]}"
        label = f"trufflehog {d.get('DetectorName')} at {loc} (allow-id {allow_id})"
        if d.get("Verified"):
            fail(f"VERIFIED secret, {label}")
        else:
            fail(f"unverified secret-like value, {label}")


def check_trivy_tree():
    """Scans the committed tree (git archive HEAD), so untracked local files like .pytest_cache do not count."""
    if not need("trivy", "install trivy 0.70.0"):
        return
    info("scanning the committed tree; the first run downloads the vulnerability database, which can be large")
    with tempfile.TemporaryDirectory() as tmp:
        archive = subprocess.run(["git", "archive", "HEAD"], cwd=ROOT, capture_output=True, check=True).stdout
        subprocess.run(["tar", "-x", "-C", tmp], input=archive, check=True)
        out = Path(tmp) / "trivy.json"
        p = run(["trivy", "fs", "--scanners", "vuln,secret", "--severity", "HIGH,CRITICAL",
                 "--format", "json", "--quiet", "--output", str(out), tmp])
        if p.returncode != 0 or not out.exists():
            fail(f"trivy failed (exit {p.returncode}): {p.stderr.strip()[:200]}")
            return
        data = json.loads(out.read_text(encoding="utf-8"))
    for t in data.get("Results", []):
        target = t.get("Target", "")
        for v in t.get("Vulnerabilities") or []:
            fail(f"trivy {v['Severity']} vuln {v['VulnerabilityID']} in {v['PkgName']} ({target})")
        for s in t.get("Secrets") or []:
            fail(f"trivy secret rule {s['RuleID']} at {target}:{s['StartLine']}")


def check_python_deps():
    if not (ROOT / "uv.lock").exists():
        return
    if not need("uv", "install uv (https://docs.astral.sh/uv/)"):
        return
    exp = run(["uv", "export", "--frozen", "--no-hashes", "--no-emit-project", "--format", "requirements-txt"])
    if exp.returncode != 0:
        fail(f"uv export failed: {exp.stderr.strip()[:200]}")
        return
    info(f"auditing {sum(1 for l in exp.stdout.splitlines() if '==' in l)} pinned Python packages")
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(exp.stdout)
        req = f.name
    try:
        # The export is fully pinned, so --no-deps --disable-pip audits exactly these versions without building a venv
        # (a venv build crashed in this environment's uv-managed Python).
        p = run(["uvx", "--from", PIP_AUDIT, "pip-audit", "-r", req, "--no-deps", "--disable-pip",
                 "--format", "json", "--progress-spinner", "off"])
    finally:
        os.unlink(req)
    try:
        data = json.loads(p.stdout)
    except json.JSONDecodeError:
        fail(f"pip-audit produced no JSON (exit {p.returncode}): {p.stderr.strip()[:200]}")
        return
    for dep in data.get("dependencies", []):
        for v in dep.get("vulns", []):
            fix = ", ".join(v.get("fix_versions", [])) or "no fix yet"
            fail(f"python dependency {dep['name']}=={dep['version']} has {v['id']} (fix: {fix})")


def check_npm_deps():
    """npm: only packages with declared dependencies are audited. The current pi_ext package declares none."""
    for pkg in ROOT.rglob("package.json"):
        if "node_modules" in pkg.parts:
            continue
        meta = json.loads(pkg.read_text(encoding="utf-8"))
        rel = pkg.parent.relative_to(ROOT)
        deps = {**meta.get("dependencies", {}), **meta.get("devDependencies", {})}
        if not deps:
            info(f"{rel}/package.json declares no dependencies; nothing to audit")
            continue
        if not (pkg.parent / "package-lock.json").exists():
            fail(f"{rel}/package.json declares dependencies but has no package-lock.json; audit cannot run")
            continue
        if not need("npm", "install Node 22 + npm"):
            continue
        info(f"npm audit in {rel}")
        p = run(["npm", "audit", "--omit=dev", "--json"], cwd=pkg.parent)
        try:
            data = json.loads(p.stdout or "{}")
        except json.JSONDecodeError:
            fail(f"npm audit produced no JSON in {rel}")
            continue
        for name, v in (data.get("vulnerabilities") or {}).items():
            sev = v.get("severity")
            msg = f"npm package {name} in {rel} has {sev} severity advisory"
            (fail if sev in ("high", "critical") else warn)(msg)


def check_internal_refs_tree():
    files = tracked_files()
    info(f"{len(files)} tracked files")
    check_internal_refs(
        (f, n, text) for f in files if (ROOT / f).is_file()
        for n, text in enumerate(_read_lines(ROOT / f), 1))


# ---------- modes ----------

def mode_push(base, head):
    if head == ZERO:
        return  # deleting a remote branch pushes no content
    diff_base = EMPTY_TREE if base == ZERO else base
    files = changed_files(diff_base, head)
    step("blocked file names", check_blocked, files)
    step("internal names (added lines)", check_internal_refs, added_lines(diff_base, head))
    step("semgrep on changed files", check_semgrep, files)
    args = ["git", f"file://{ROOT}", "--branch", head]
    if base != ZERO:
        args[2:2] = ["--since-commit", base]
    step("trufflehog on pushed commits", check_trufflehog, args)
    if any(f == "uv.lock" or f.endswith(("pyproject.toml", "package.json", "package-lock.json")) for f in files):
        step("python dependencies", check_python_deps)
        step("npm dependencies", check_npm_deps)


def mode_full():
    files = tracked_files()
    step("blocked file names", check_blocked, files)
    step("internal names (all tracked files)", check_internal_refs_tree)
    step("semgrep on all tracked files", check_semgrep, files)
    step("trufflehog on the whole history", check_trufflehog, ["git", f"file://{ROOT}"])
    step("trivy on the committed tree", check_trivy_tree)


def mode_deps():
    step("python dependencies", check_python_deps)
    step("npm dependencies", check_npm_deps)


def _read_lines(path):
    try:
        return path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []


def main(argv):
    sys.stdout.reconfigure(line_buffering=True)  # show progress live, even when make pipes the output
    mode = argv[1] if len(argv) > 1 else ""
    if mode == "push" and len(argv) == 4:
        mode_push(argv[2], argv[3])
    elif mode == "full":
        mode_full()
    elif mode == "deps":
        mode_deps()
    elif mode == "all":
        mode_full()
        mode_deps()
    else:
        print(__doc__)
        return 2
    print()
    if failures:
        # Repeat every failure at the end, so none is lost in the scroll-back.
        print(paint("FAILURES (these block the push or merge):", "1;31"), flush=True)
        for f in failures:
            print(paint(f"  - {f}", "31"), flush=True)
        print(paint(f"BLOCKED: {len(failures)} failure(s), {len(warnings)} warning(s)", "1;31"), flush=True)
        return 1
    print(paint(f"PASS: {len(warnings)} warning(s), no failures", "1;32"), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
