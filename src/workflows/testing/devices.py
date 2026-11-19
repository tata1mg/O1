"""The Android side of `execute_app` and `convert_flows`: devices, the app on them, and running one flow.

Everything here is Python driving tools that already exist — adb, the Maestro CLI and the
maestro-automation repo's own scripts — so none of it is a model's judgement:

  * which devices are ready (`adb devices`, `device` state only) and which build of the app is on each
    (`dumpsys package` -> versionName/versionCode, which names the Kiwi run's build);
  * one owner per device at a time (`DeviceLock`): a flow run, an agent session, a sign-in and a draft
    validation must never drive the same emulator at once;
  * signing a device in to an account profile (`scripts/ensure_account.sh`, static dev OTP only; exit 78
    = the profile cannot be signed in unattended, which a case reports as BLOCKED, never FAIL);
  * running one flow (`maestro test --format junit`) and enriching its report (`scripts/maestro_report.py`)
    so `maestro.attribute` can say which case a failure belongs to. The repo's wrapper scripts are not
    used for this: they publish to Kiwi themselves by default, and this workflow records through its own
    store.

`Android` is the seam tests replace: every subprocess goes through `_run`.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

#: scripts/app_id.sh, kept here too so a missing repo is reported as such rather than as a bad variant.
APP_IDS = {"debug": "com.aranoah.healthkart.plus.dev.debug", "dev-release": "com.aranoah.healthkart.plus.dev",
           "prod": "com.aranoah.healthkart.plus"}
SKIPPED_EXIT = 78  # ensure_account.sh / run_gated_group.sh: the precondition could not be established


def maestro_bin() -> str:
    """The Maestro CLI: on PATH, else where its installer puts it."""
    found = shutil.which("maestro")
    if found:
        return found
    home = Path.home() / ".maestro" / "bin" / "maestro"
    return str(home) if home.is_file() else ""


def adb_bin() -> str:
    found = shutil.which("adb")
    if found:
        return found
    for sdk in sdk_roots():
        candidate = Path(sdk) / "platform-tools" / "adb"
        if candidate.is_file():
            return str(candidate)
    return ""


def sdk_roots() -> list[str]:
    """Where an Android SDK may be: $ANDROID_HOME / $ANDROID_SDK_ROOT, Android Studio's default, and
    Homebrew's `android-commandlinetools` cask."""
    return [r for r in (os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT"),
                        str(Path.home() / "Library" / "Android" / "sdk"),
                        "/opt/homebrew/share/android-commandlinetools") if r]


#: Device roles, as `.maestro/devices.yaml` (the repo's parallel-dispatch manifest) names them: a `guest` device
#: runs only cases that need no account; a device whose role is an account profile (`care_plan_user`, ...) is
#: kept signed in to that account and runs only its cases; a `pool` device runs anything, signing in as needed.
GUEST, POOL = "guest", "pool"
MANIFEST = ".maestro/devices.yaml"
_MANIFEST_BLOCK = re.compile(r"^-\s+id:\s*\S+\s*\n\s+serial:\s*(?P<serial>\S+)\s*\n\s+role:\s*(?P<role>\S+)\s*$",
                             re.MULTILINE)


def parse_manifest(text: str) -> dict[str, str]:
    """{serial: role} from devices.yaml — the same lexical shape scripts/lib/read_devices.py reads; a
    commented-out block is not a device."""
    body = "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))
    return {m["serial"]: m["role"] for m in _MANIFEST_BLOCK.finditer(body)}


def parse_role_flag(text: str) -> dict[str, str]:
    """{serial: role} from `emulator-5554=guest,emulator-5556=care_plan_user` (testing.device_roles)."""
    out = {}
    for part in (text or "").split(","):
        serial, _, role = part.strip().partition("=")
        if serial.strip() and role.strip():
            out[serial.strip()] = role.strip()
    return out


def assign_roles(serials: list[str], manifest: dict[str, str], override: dict[str, str]) -> dict[str, str]:
    """The role of each ready device. testing.device_roles wins; else the repo's manifest, but only when more
    than one device is ready — the manifest describes the repo owners' rig (emulator-5554 is its guest), and a
    lone pilot emulator that happens to share the serial must still run every case. A device neither names
    is a pool device."""
    use = override or (manifest if len(serials) > 1 else {})
    return {s: use.get(s, POOL) for s in serials}


def accepts(role: str, profile: str) -> bool:
    """May a device with `role` run a case needing account `profile` ("" = a guest case)?"""
    return role == POOL or role == (profile or GUEST)


#: The repo `preflight setup-android --clone` clones, by name, next to the user's other checkouts.
MAESTRO_REPO_NAME = "maestro-automation"
NOT_SET_UP = ("Android runs are not set up on this machine: no maestro-automation checkout is configured. Run "
              "/rfc-agent:setup and answer yes to Android (it clones the repo and saves its path), or point at a "
              "checkout you have: `python -m workflows.testing.preflight setup-android --path <checkout>`")


def is_checkout(path: Path) -> bool:
    """A maestro-automation checkout this workflow can use: its flows and the scripts it runs."""
    return (path / ".maestro" / "flows").is_dir() and (path / "scripts" / "ensure_account.sh").is_file()


def maestro_repo(value: str) -> Path | None:
    """`testing.maestro_repo` as a path, or None when nothing is configured (Android not set up)."""
    return Path(value).expanduser() if str(value or "").strip() else None


def parse_adb_devices(text: str) -> list[str]:
    """Serials in `device` state from `adb devices` output (offline / unauthorized / booting are not ready)."""
    out = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            out.append(parts[0])
    return out


def parse_version(dumpsys: str) -> str:
    """`versionName=2.4.1 … versionCode=1234 …` -> `2.4.1 (1234)`; "" when the package is not installed."""
    name = re.search(r"versionName=(\S+)", dumpsys)
    code = re.search(r"versionCode=(\d+)", dumpsys)
    if not name:
        return ""
    return f"{name.group(1)} ({code.group(1)})" if code else name.group(1)


@dataclass
class FlowRun:
    """One `maestro test` of one flow on one device."""
    flow: str
    serial: str
    report: Path           # the JUnit report (enriched when `enriched`)
    exit_code: int
    duration_s: float
    enriched: bool
    output: str            # the CLI's last lines, for an ERROR's reason


class DeviceLock:
    """One owner per serial. In-process (`execute_app` runs its devices as threads) and across processes
    (a lock file under the run dir), so `convert_flows` started beside a run cannot take a busy device."""

    _locks: dict[str, threading.Lock] = {}
    _guard = threading.Lock()

    def __init__(self, serial: str, folder: Path) -> None:
        self.serial, self.path = serial, Path(folder) / f"device-{re.sub(r'[^A-Za-z0-9._-]', '_', serial)}.lock"
        with DeviceLock._guard:
            self._lock = DeviceLock._locks.setdefault(serial, threading.Lock())
        self._fh = None

    def __enter__(self):
        import fcntl

        self._lock.acquire()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "w")  # noqa: SIM115 - held for the lock's lifetime
        fcntl.flock(self._fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc) -> None:
        import fcntl

        try:
            if self._fh:
                fcntl.flock(self._fh, fcntl.LOCK_UN)
                self._fh.close()
        finally:
            self._lock.release()


class Android:
    """adb, Maestro and the repo's scripts, for one run. `repo` is the maestro-automation checkout."""

    def __init__(self, repo: Path, variant: str = "dev-release") -> None:
        self.repo = Path(repo).expanduser()
        self.variant = variant
        self.app_id = APP_IDS.get(variant, "")

    # -- the seam -----------------------------------------------------------------------------------------
    def _run(self, cmd: list[str], *, timeout: float, env: dict | None = None,
             cwd: Path | None = None) -> tuple[int, str]:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd,
                                  env={**os.environ, **(env or {})})
        except subprocess.TimeoutExpired as exc:
            partial = exc.stdout[-2000:] if isinstance(exc.stdout, str) else ""
            return 124, f"timed out after {timeout:.0f}s: {partial}"
        except OSError as exc:
            return 127, str(exc)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    # -- what is there --------------------------------------------------------------------------------------
    def problem(self) -> str:
        """Why Android cases cannot run on this machine at all, or ""."""
        if not adb_bin():
            return "adb is not installed (Android SDK platform-tools); no device can be reached"
        if not maestro_bin():
            return "the Maestro CLI is not installed (curl -Ls https://get.maestro.mobile.dev | bash)"
        if not (self.repo / ".maestro" / "flows").is_dir():
            return (f"no maestro-automation checkout at {self.repo} (the path in testing.maestro_repo). Re-run "
                    f"/rfc-agent:setup's Android step, or `preflight setup-android --path <checkout>`")
        if not self.app_id:
            return f"unknown app variant {self.variant!r} (debug, dev-release or prod)"
        return ""

    def ready(self, wanted: list[str] | None = None) -> list[str]:
        code, out = self._run([adb_bin(), "devices"], timeout=20)
        serials = parse_adb_devices(out) if code == 0 else []
        return [s for s in serials if not wanted or s in wanted]

    def why_not_ready(self, booted: list[str], wanted: list[str] | None = None) -> str:
        """Why no device can run this variant, in words that name the fix. `booted` = `ready(wanted)`.

        One message used to cover two causes (2026-11-18: the emulator had shut down, AND the run asked for the
        dev-release build while the emulator carries the debug one) — neither was visible from it."""
        if not booted:
            narrowed = f" among {', '.join(wanted)}" if wanted else ""
            avds = self._avds()
            boot = (f" Boot one: `emulator -avd {avds[0]}`" + (f" (AVDs here: {', '.join(avds)})" if len(avds) > 1
                                                               else "") + "." if avds else
                    " Create and boot an emulator, or plug in a device.")
            return f"no Android emulator or device is running{narrowed} (`adb devices` lists none).{boot}"
        found = []
        for serial in booted:
            others = [v for v, app in APP_IDS.items() if v != self.variant and self._installed(serial, app)]
            if others:
                found.append(f"{serial} has the {others[0]} build ({APP_IDS[others[0]]})")
        if found:
            hint = found[0].split(" has the ")[1].split(" build")[0]
            return (f"{'; '.join(found)}, not the {self.variant} build ({self.app_id}). Add --app-variant {hint}, "
                    f"or install the {self.variant} build")
        return f"{', '.join(booted)} {'is' if len(booted) == 1 else 'are'} running but none has {self.app_id} " \
               f"(the {self.variant} build) installed. Install it with `adb install <apk>`"

    def _installed(self, serial: str, app_id: str) -> bool:
        code, out = self._run([adb_bin(), "-s", serial, "shell", "pm", "list", "packages", app_id], timeout=20)
        return code == 0 and f"package:{app_id}" in out.split()

    def _avds(self) -> list[str]:
        for root in sdk_roots():
            exe = Path(root) / "emulator" / "emulator"
            if exe.is_file():
                code, out = self._run([str(exe), "-list-avds"], timeout=20)
                return [line.strip() for line in out.splitlines() if code == 0 and line.strip()
                        and not line.startswith(("INFO", "WARNING"))]
        return []

    def app_version(self, serial: str) -> str:
        code, out = self._run([adb_bin(), "-s", serial, "shell", "dumpsys", "package", self.app_id], timeout=30)
        return parse_version(out) if code == 0 else ""

    # -- acting -------------------------------------------------------------------------------------------
    def sign_in(self, serial: str, profile: str) -> tuple[int, str]:
        """`ensure_account.sh <profile> <serial>` -> (exit code, last output). 78 = cannot sign in unattended."""
        script = self.repo / "scripts" / "ensure_account.sh"
        code, out = self._run(["bash", str(script), profile, serial], timeout=600, cwd=self.repo,
                              env={"BUILD_VARIANT": self.variant, "PATH": _path_with_maestro()})
        return code, out[-1500:]

    def run_flow(self, flow: Path, serial: str, out_dir: Path, timeout: float) -> FlowRun:
        """`maestro test` one flow on one device into `<out_dir>/<flow>.xml`, then enrich the report."""
        out_dir.mkdir(parents=True, exist_ok=True)
        report = out_dir / f"{flow.stem}.xml"
        tests = Path.home() / ".maestro" / "tests"
        before = set(tests.iterdir()) if tests.is_dir() else set()
        t0 = time.monotonic()
        code, out = self._run([maestro_bin(), "--device", serial, "test", "-e", f"APP_ID={self.app_id}",
                               "--format", "junit", "--output", str(report), str(flow)],
                              timeout=timeout, cwd=self.repo)
        duration = round(time.monotonic() - t0, 1)
        enriched = False
        if report.is_file():
            cmd = ["python3", str(self.repo / "scripts" / "maestro_report.py"), "--report", str(report),
                   "--flows-root", str(self.repo / ".maestro" / "flows")]
            test_dir = _artifacts_dir(tests, before, flow.stem)
            if test_dir:
                cmd += ["--test-dir", str(test_dir)]
            enriched = self._run(cmd, timeout=300, cwd=self.repo)[0] == 0
        return FlowRun(flow=flow.stem, serial=serial, report=report, exit_code=code, duration_s=duration,
                       enriched=enriched, output=out[-1500:])


def _path_with_maestro() -> str:
    bins = [str(Path(p).parent) for p in (maestro_bin(), adb_bin()) if p]
    return os.pathsep.join([*bins, os.environ.get("PATH", "")])


def _artifacts_dir(tests: Path, before: set, flow_name: str) -> Path | None:
    """The `~/.maestro/tests/<stamp>/` folder this run created. With several devices running at once there
    may be more than one new folder, so the one holding this flow's files is taken; None leaves the
    reporter to its own default (the newest folder)."""
    if not tests.is_dir():
        return None
    new = sorted((d for d in tests.iterdir() if d.is_dir() and d not in before),
                 key=lambda d: d.stat().st_mtime, reverse=True)
    for d in new:
        if any(flow_name in p.name for p in d.rglob("*")):
            return d
    return new[0] if len(new) == 1 else None
