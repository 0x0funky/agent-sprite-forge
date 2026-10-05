#!/usr/bin/env python3
"""Report which Agent Sprite Forge routes are usable on this machine.

Stdlib only and read-only: it reads no credential file, sends no request and
generates nothing. It may run a local --version of ffmpeg and of the media
CLIs it finds (skip with --no-exec). Run it once per session from the project
root and pass the media tools YOU (the calling agent) can see in your own tool
list; a script cannot see them:

  python "<skill-dir>/scripts/forge_doctor.py" --host-tools image_gen
  python "<skill-dir>/scripts/forge_doctor.py" --host-tools none --json --save outputs/doctor.json

Every check has one status: OK; WARN (works, with a known trap); FAIL (a Forge
step will break); MISSING (a route is unavailable); AGENT (only the calling
agent knows: declare your tools with --host-tools); UNKNOWN (cannot be proven
without an authorised call). FAIL and MISSING always come with a remedy.

Local CLI routes climb a readiness ladder: PRESENT (native executable found,
never an npm launcher) -> AUTH_MODE (sign-in mode known, --probe-auth) ->
TOOL_EXPOSED (the native media tool answered a cli_media.py run) -> VERIFIED
(a cli_media.py run of this exact CLI version and recipe published a verified
artifact; proofs live in <project>/.forge/route-proofs.json). An installed CLI
is not a connected tool: only VERIFIED CLI routes are offered in ROUTES.
--verify-route ROUTE --execute spends one quota call to record that proof; it
can be repeated after every CLI update (the version is part of its request).

ROUTES puts the local agent first (owner decision 13, D22). Images: the host's
own image tool, then Codex (local CLI), then Grok (local CLI, one-shot mode),
then the paid REST API with consent. Video: Grok (local CLI, ACP mode), then the
REST API with consent. A VERIFIED local route is "ready": it runs without a
per-call question within the session cap that cli_media.py enforces through the
ledger (8 images and 2 videos per 12 hours by default); name the route used.

Exit status 1 when any check FAILs (the report is still printed).
"""
from __future__ import annotations

import argparse
import codecs
from concurrent.futures import ThreadPoolExecutor
import contextlib
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import importlib.util
import json
import locale
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zlib

TOOL_NAME = "forge_doctor"
# The package release (D29). The doctor imports no sibling at start-up (it must report a damaged
# install), so it carries the constant itself; a test keeps it equal to media_ledger's and forge_core's.
FORGE_PACKAGE_VERSION = "0.4.0"
TOOL_VERSION = FORGE_PACKAGE_VERSION
DOCTOR_SCHEMA = "generate2dmedia.doctor.v1"
PROOFS_SCHEMA = "generate2dmedia.route_proofs.v1"
PROOFS_FILE = Path(".forge") / "route-proofs.json"
INSTALL_MANIFEST = ".agent-sprite-forge.install.json"
SKILLS = ("generate2dsprite", "generate2dmap", "video2dsprite", "generate2dmedia", "codeart2d")
STATUSES = ("OK", "WARN", "FAIL", "MISSING", "AGENT", "UNKNOWN")
LADDER = ("PRESENT", "AUTH_MODE", "TOOL_EXPOSED", "VERIFIED")
PYTHON_MIN = (3, 10)
FFMPEG_MIN = (5, 1)
VERSION_TIMEOUT = 5.0
# Characters Python tools commonly print (arrows, dashes, times, middle dot, ellipsis).
GLYPHS = "→←—×·…"
# (import name, distribution, minimum, status when missing or too old, what needs it)
REQUIREMENTS = (
    ("numpy", "numpy", (1, 26), "FAIL", "every image tool"),
    ("PIL", "Pillow", (10, 1), "FAIL", "every image tool"),
    ("scipy", "scipy", (1, 11), "WARN", "faster labelling and keying (forge_core falls back to numpy)"),
    ("resvg_py", "resvg-py", (0, 5), "MISSING", "codeart2d SVG rendering (PixelSpec needs only numpy and Pillow)"),
)
RESVG_TESTED_BELOW = (0, 6)
HOST_TOOL_ALIASES = {
    "image_gen": "image_gen", "imagegen": "image_gen", "image_generation": "image_gen", "generate_image": "image_gen",
    "image_edit": "image_edit", "imageedit": "image_edit", "edit_image": "image_edit",
    "image_to_video": "image_to_video", "imagetovideo": "image_to_video", "image2video": "image_to_video",
    "img2video": "image_to_video",
}
API_KEYS = (("OPENAI_API_KEY", "openai"), ("XAI_API_KEY", "xai"))
# Removed from every CLI child: the CLI routes run on the user's sign-in (subscription
# quota), never on a paid API key, and Grok login tokens are never reused for REST.
STRIPPED_ENV = ("OPENAI_API_KEY", "CODEX_API_KEY", "XAI_API_KEY")
# Also removed: anything that looks like a credential, and the calling agent's own session
# variables (a nested CLI must not inherit the host's identity, sandbox or lifecycle
# settings). The CLIs keep their own sign-in in CODEX_HOME / GROK_HOME.
CREDENTIAL_ENV = re.compile(r"(?i)(?:^|_)(?:api_?key|token|secret|password|passwd|credentials?|private_?key|"
                            r"access_?key|auth)(?:_|$)|_key$")
PARENT_AGENT_ENV = re.compile(r"(?i)^(?:claude_?code|claudecode$|codex_(?!home$)|grok_(?!home$))")
GROK_ISOLATION = {
    "RUST_LOG": "off", "GROK_MEMORY": "false", "GROK_SUBAGENTS": "0", "GROK_DISABLE_AUTOUPDATER": "true",
    "GROK_MANAGED_MCPS_ENABLED": "false", "GROK_MANAGED_MCP_GATEWAY_TOOLS_ENABLED": "false",
    **{f"GROK_{vendor}_{kind}_ENABLED": "false" for vendor in ("CLAUDE", "CURSOR", "CODEX")
       for kind in ("SKILLS", "RULES", "AGENTS", "MCPS", "HOOKS", "SESSIONS")},
}
OVERRIDE_ENV = {"codex": "FORGE_CODEX_EXE", "grok": "FORGE_GROK_EXE"}
NATIVE_MAGIC = (b"MZ", b"\x7fELF", b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe",
                b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe")
NPM_TARGETS = {
    ("win32", "x64"): ("x86_64-pc-windows-msvc", "codex-win32-x64"),
    ("win32", "arm64"): ("aarch64-pc-windows-msvc", "codex-win32-arm64"),
    ("linux", "x64"): ("x86_64-unknown-linux-musl", "codex-linux-x64"),
    ("linux", "arm64"): ("aarch64-unknown-linux-musl", "codex-linux-arm64"),
    ("darwin", "x64"): ("x86_64-apple-darwin", "codex-darwin-x64"),
    ("darwin", "arm64"): ("aarch64-apple-darwin", "codex-darwin-arm64"),
}
VERSION_RE = re.compile(r"\d+\.\d+(?:\.\d+)?(?:-[0-9A-Za-z.]+)?")
TEXT_SUFFIXES = frozenset({".py", ".json", ".md", ".js", ".mjs", ".txt", ".yaml", ".yml"})
# What an install never ships and a drift check never counts (tools/install_skills.py shipped(), D24):
# bytecode caches, dot files and OS junk that normal use writes into an installed skill.
SKIP_DIRS = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".git", "node_modules"})
SKIP_FILES = frozenset({"Thumbs.db", "desktop.ini"})
SKIP_SUFFIXES = frozenset({".pyc", ".pyo"})
# User-facing route names (D22): one "Grok (local CLI)" route, one-shot mode for images and ACP mode for
# video; the internal ids stay codex-cli, grok-cli and grok-acp.
ROUTE_LABEL = {"codex-cli": "Codex (local CLI)", "grok-cli": "Grok (local CLI, one-shot image mode)",
               "grok-acp": "Grok (local CLI, ACP video mode)"}
# The session cap cli_media.py enforces through the ledger (media_ledger.SESSION_DEFAULTS; FORGE_SESSION_*).
SESSION_CAP_TEXT = "8 images and 2 videos per 12 hours unless FORGE_SESSION_IMAGES/VIDEOS/HOURS say otherwise"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_ASCII_MAP = str.maketrans({"→": "->", "←": "<-", "—": "-", "–": "-", "×": "x",
                            "·": ".", "…": "...", "‘": "'", "’": "'", "“": '"', "”": '"'})


@dataclass(frozen=True)
class Capability:
    """One opt-in CLI media capability: its ledger route, CLI binary, native tool and recipe."""

    route: str
    cli: str
    tool: str
    need: str
    recipe: str

    @property
    def key(self) -> str:
        return f"{self.route}:{self.tool}"


# A proof is valid only for the recipe that produced it: cli_media.py bumps a recipe
# id whenever its flags, prompts or verification rules change.
CAPABILITIES = (
    Capability("codex-cli", "codex", "image_gen", "image", "codex-exec-imagegen/1"),
    Capability("grok-cli", "grok", "image_gen", "image", "grok-headless-image/1"),
    Capability("grok-cli", "grok", "image_edit", "image_edit", "grok-headless-image/1"),
    Capability("grok-acp", "grok", "image_to_video", "video", "grok-acp-video/1"),
)


@dataclass
class Check:
    id: str
    status: str
    detail: str = ""
    remedy: str | None = None

    def as_dict(self) -> dict:
        record = {"id": self.id, "status": self.status, "detail": self.detail}
        if self.remedy:
            record["remedy"] = self.remedy
        return record


@dataclass
class CliInfo:
    """Where a media CLI lives. ``path`` is the native executable to run; a script
    launcher (npm .cmd/.ps1/.js shim) is recorded in ``shim`` and never run."""

    cli: str
    path: Path | None = None
    shim: Path | None = None
    source: str = ""
    problem: str | None = None
    version_text: str | None = None
    version: str | None = None
    version_source: str | None = None


class DoctorError(Exception):
    """A report that breaks its own contract, or an unusable option."""


# --------------------------------------------------------------------------- small helpers

def _local_utf8_stdio() -> None:
    """Console-safe output on legacy code pages (twin of forge_core.utf8_stdio)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(OSError, ValueError):
                reconfigure(encoding="utf-8", errors="backslashreplace")


def ascii_text(text: object) -> str:
    """Console-safe ASCII (twin of forge_core.ascii_text)."""
    return str(text).translate(_ASCII_MAP).encode("ascii", "backslashreplace").decode("ascii")


def utc_timestamp() -> str:
    moment = datetime.now(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def display_path(path: str | os.PathLike) -> str:
    """POSIX spelling with the home folder shown as ~, so reports carry no user name."""
    text = Path(path).as_posix()
    home = Path.home().as_posix().rstrip("/")
    fold = (lambda s: s.lower()) if os.name == "nt" else (lambda s: s)
    if home and (fold(text) == fold(home) or fold(text).startswith(fold(home) + "/")):
        return "~" + text[len(home):]
    return text


def _version_tuple(text: str | None) -> tuple[int, ...] | None:
    match = re.match(r"\s*v?(\d+)\.(\d+)", text or "")
    return (int(match.group(1)), int(match.group(2))) if match else None


def bare_version(text: str | None) -> str | None:
    """The dotted version inside a --version line ("codex-cli 0.155.1" -> "0.155.1")."""
    match = VERSION_RE.search(text or "")
    return match.group(0) if match else None


def run_quiet(argv: list[str], *, timeout: float, env: dict | None = None) -> subprocess.CompletedProcess | None:
    """Run a local command with captured UTF-8 output; None when it cannot run or times out."""
    try:
        return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, env=env, stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def content_digest(path: Path) -> str:
    """sha256 with CRLF normalised to LF for text files (as tools/vendor_sync.py)."""
    data = path.read_bytes()
    if path.suffix.lower() in TEXT_SUFFIXES:
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# --------------------------------------------------------------------------- CLI resolution

def codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def grok_home() -> Path:
    configured = os.environ.get("GROK_HOME", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".grok"


def isolated_env(cli: str) -> dict:
    """Environment for a CLI child: API keys, credential-like variables and the calling
    agent's session variables removed; for Grok, memory, sub-agents, managed MCPs, foreign
    agent rules and the auto-updater switched off."""
    env = {name: value for name, value in os.environ.items()
           if name.upper() not in STRIPPED_ENV and not CREDENTIAL_ENV.search(name) and not PARENT_AGENT_ENV.search(name)}
    env["NO_COLOR"] = "1"
    if cli == "grok":
        env.update(GROK_ISOLATION)
    return env


def is_native(path: Path) -> bool:
    """A real executable binary (PE, ELF or Mach-O), not a script launcher."""
    try:
        with open(path, "rb") as stream:
            head = stream.read(4)
    except OSError:
        return False
    if not head.startswith(NATIVE_MAGIC):
        return False
    return os.name == "nt" or os.access(path, os.X_OK)


def _path_candidates(cli: str) -> list[Path]:
    names = (f"{cli}.exe", f"{cli}.cmd", f"{cli}.bat", f"{cli}.ps1", cli) if os.name == "nt" else (cli,)
    found, seen = [], set()
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        if not folder.strip():
            continue
        for name in names:
            candidate = Path(folder.strip().strip('"')) / name
            key = os.path.normcase(str(candidate))
            if key not in seen and candidate.is_file():
                seen.add(key)
                found.append(candidate)
    return found


def _npm_target() -> tuple[str, str] | None:
    system = {"win32": "win32", "linux": "linux", "darwin": "darwin"}.get(sys.platform)
    # On Windows, read the architecture from the environment, as platform itself does. On Python 3.10 and
    # 3.11, platform.machine() goes through win32_ver(), which spawns `cmd /c ver`: a child process the
    # doctor does not promise to start (r3-platform finding 1).
    machine = ((os.environ.get("PROCESSOR_ARCHITEW6432") or os.environ.get("PROCESSOR_ARCHITECTURE", ""))
               if sys.platform == "win32" else platform.machine()).lower()
    arch = "x64" if machine in ("amd64", "x86_64", "x64") else "arm64" if machine in ("arm64", "aarch64") else None
    return NPM_TARGETS.get((system, arch))


def _npm_codex_native(shim: Path) -> Path | None:
    """The native codex binary behind an npm launcher (bin/codex.js of @openai/codex)."""
    target = _npm_target()
    if target is None:
        return None
    triple, package = target
    roots = []
    with contextlib.suppress(OSError):
        real = shim.resolve()
        if real.name == "codex.js" and real.parent.name == "bin":
            roots.append(real.parent.parent)
    roots += [shim.parent / "node_modules" / "@openai" / "codex",
              shim.parent.parent / "lib" / "node_modules" / "@openai" / "codex"]
    exe = "codex.exe" if os.name == "nt" else "codex"
    for root in roots:
        for vendor in (root / "node_modules" / "@openai" / package / "vendor", root.parent / package / "vendor",
                       root / "vendor"):
            candidate = vendor / triple / "bin" / exe
            if candidate.is_file() and is_native(candidate):
                return candidate
    return None


def resolve_cli(cli: str) -> CliInfo:
    """Find the native executable of ``codex`` or ``grok`` without running anything.

    Order: the FORGE_CODEX_EXE / FORGE_GROK_EXE override, Grok's own install folder
    ($GROK_HOME/bin), then PATH. An npm launcher on PATH is resolved to the native
    binary it would start (codex), since a .cmd/.ps1 launcher must never be run
    with untrusted arguments; a launcher without its binary is reported, not run.
    """
    override = os.environ.get(OVERRIDE_ENV[cli], "").strip()
    if override:
        path = Path(override).expanduser()
        if path.is_file() and is_native(path):
            return CliInfo(cli, path=path, source="override")
        return CliInfo(cli, source="override", problem=f"{OVERRIDE_ENV[cli]} does not name a native executable")
    if cli == "grok":
        candidate = grok_home() / "bin" / ("grok.exe" if os.name == "nt" else "grok")
        if candidate.is_file() and is_native(candidate):
            return CliInfo(cli, path=candidate, source="home")
    shim = None
    for candidate in _path_candidates(cli):
        if is_native(candidate):
            return CliInfo(cli, path=candidate, shim=shim, source="PATH")
        shim = shim or candidate
        if cli == "codex":
            native = _npm_codex_native(candidate)
            if native is not None:
                return CliInfo(cli, path=native, shim=candidate, source="npm")
    if shim is not None:
        return CliInfo(cli, shim=shim, source="PATH",
                       problem="only a script launcher was found on PATH; its native binary is missing")
    return CliInfo(cli, problem="not found on PATH" + (" or in $GROK_HOME/bin" if cli == "grok" else ""))


def package_version(info: CliInfo) -> str | None:
    """Version from the npm platform package beside the codex binary, without running it."""
    if info.cli != "codex" or info.path is None or info.source != "npm":
        return None
    try:
        data = json.loads((info.path.parents[3] / "package.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, IndexError):
        return None
    version = data.get("version") if isinstance(data, dict) else None
    if not isinstance(version, str):
        return None
    target = _npm_target()
    if target:
        version = version.removesuffix("-" + target[1].removeprefix("codex-"))
    return bare_version(version)


def probe_version(argv: list[str], cli: str, *, timeout: float = VERSION_TIMEOUT) -> tuple[str | None, str | None]:
    """Run ``<cli> --version`` (local; never a network call) -> (first line, bare version)."""
    result = run_quiet([*argv, "--version"], timeout=timeout, env=isolated_env(cli))
    if result is None or result.returncode != 0:
        return None, None
    lines = [line.strip() for line in (result.stdout or result.stderr or "").splitlines() if line.strip()]
    text = ascii_text(lines[0])[:100] if lines else None
    return text, bare_version(text)


def probe_codex_login(argv: list[str], *, timeout: float = 15.0) -> str:
    """Opt-in: run ``codex login status`` (the CLI reads its own store; this tool reads
    no credential) and map it to chatgpt | api-key | workload-identity | none | unknown.
    The raw output is never stored or printed: it can name the account or part of a key."""
    result = run_quiet([*argv, "login", "status"], timeout=timeout, env=isolated_env("codex"))
    if result is None:
        return "unknown"
    text = f"{result.stdout}\n{result.stderr}".lower()
    if "logged in using chatgpt" in text:
        return "chatgpt"
    if "logged in using an api key" in text:
        return "api-key"
    if "workload identity" in text:
        return "workload-identity"
    if "not logged in" in text:
        return "none"
    return "unknown"


# --------------------------------------------------------------------------- proofs

def load_proofs(project_dir: str | os.PathLike) -> tuple[list[dict], str | None]:
    """Records of <project>/.forge/route-proofs.json, plus a problem string when unreadable."""
    path = Path(project_dir) / PROOFS_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return [], None
    except (OSError, ValueError) as exc:
        return [], f"{PROOFS_FILE.as_posix()} is unreadable ({type(exc).__name__})"
    records = data.get("proofs") if isinstance(data, dict) else None
    if not isinstance(records, list):
        return [], f"{PROOFS_FILE.as_posix()} has no proofs list"
    required = ("route", "tool", "version", "recipe", "level", "verifiedAt")
    valid = [r for r in records if isinstance(r, dict) and all(isinstance(r.get(k), str) and r[k] for k in required)
             and r["level"] in ("TOOL_EXPOSED", "VERIFIED")]
    return valid, None


def find_proof(proofs: list[dict], capability: Capability, version: str | None) -> tuple[dict | None, dict | None]:
    """(best record for this exact version and recipe, newest record for any version)."""
    rank = {"TOOL_EXPOSED": 0, "VERIFIED": 1}
    mine = [p for p in proofs if (p["route"], p["tool"], p["recipe"]) == (capability.route, capability.tool, capability.recipe)]
    exact = [p for p in mine if version and p["version"] == version]
    best = max(exact, key=lambda p: (rank[p["level"]], p["verifiedAt"])) if exact else None
    newest = max(mine, key=lambda p: p["verifiedAt"]) if mine else None
    return best, newest


# --------------------------------------------------------------------------- checks

def python_checks() -> list[Check]:
    version = ".".join(map(str, sys.version_info[:3]))
    where = display_path(sys.executable)
    checks = [Check("python.version", "OK", f"Python {version} at {where}") if sys.version_info[:2] >= PYTHON_MIN
              else Check("python.version", "FAIL", f"Python {version} at {where}",
                         "install Python 3.10 or newer and run Forge with it")]
    if "WindowsApps" in sys.executable:
        checks.append(Check("python.interpreter", "WARN", "this is the Microsoft Store alias, which may install or "
                            "launch a different Python", "install CPython from python.org, or run Forge with py -3"))
    if os.name == "nt":
        mine = os.path.normcase(os.path.realpath(sys.executable))
        others = []
        for folder in filter(None, (f.strip().strip('"') for f in os.environ.get("PATH", "").split(os.pathsep))):
            candidate = Path(folder) / "python.exe"
            if candidate.is_file() and os.path.normcase(os.path.realpath(candidate)) != mine:
                text = display_path(candidate)
                if text not in others:
                    others.append(text)
        if others:
            checks.append(Check("python.path", "WARN", "other python.exe on PATH: " + "; ".join(others[:3]),
                                "PowerShell, cmd and Git Bash may resolve different interpreters; run Forge "
                                "with the one shown in python.version"))
    return checks


def _dist_version(distribution: str) -> str | None:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return None


def dependency_checks() -> list[Check]:
    """Presence and version of the Python packages, read from metadata without importing them."""
    checks = []
    for module, distribution, minimum, severity, purpose in REQUIREMENTS:
        version = _dist_version(distribution)
        found = version is not None or importlib.util.find_spec(module) is not None
        wanted = ".".join(map(str, minimum))
        pip = f'"{sys.executable}" -m pip install "{distribution}>={wanted}"'
        if distribution == "resvg-py":
            pip = f'"{sys.executable}" -m pip install "resvg-py>=0.5,<0.6"'
        check_id = f"deps.{distribution}"
        if not found:
            checks.append(Check(check_id, severity, f"not installed; needed for {purpose}", pip))
            continue
        parsed = _version_tuple(version)
        if parsed is not None and parsed < minimum:
            checks.append(Check(check_id, severity, f"{version} is older than {wanted}; needed for {purpose}", pip))
        elif distribution == "resvg-py" and parsed is not None and parsed >= RESVG_TESTED_BELOW:
            checks.append(Check(check_id, "WARN", f"{version} is newer than the tested 0.5 series",
                                'pin it with pip install "resvg-py>=0.5,<0.6" if SVG output changes'))
        else:
            checks.append(Check(check_id, "OK", version or "installed (version unknown)"))
        if distribution == "Pillow":
            spec = importlib.util.find_spec("PIL")
            folders = list(spec.submodule_search_locations or []) if spec else []
            webp = any(name.startswith("_webp") and name.endswith((".pyd", ".so"))
                       for folder in folders for name in (os.listdir(folder) if os.path.isdir(folder) else ()))
            if not webp:
                checks.append(Check("deps.Pillow-webp", "WARN", "this Pillow build has no WebP codec",
                                    "reinstall Pillow from the official wheels; WebP references fail without it"))
    return checks


def ffmpeg_probe() -> dict:
    """Locate ffmpeg/ffprobe and read the version and build configuration (one local call)."""
    info = {"ffmpeg": shutil.which("ffmpeg"), "ffprobe": shutil.which("ffprobe"), "version": None,
            "libvpx": None, "libx264": None}
    if info["ffmpeg"]:
        result = run_quiet([info["ffmpeg"], "-hide_banner", "-version"], timeout=VERSION_TIMEOUT)
        if result is not None and result.returncode == 0:
            lines = result.stdout.splitlines()
            words = lines[0].split() if lines else []
            info["version"] = words[2] if words[:2] == ["ffmpeg", "version"] and len(words) > 2 else None
            config = next((line for line in lines if line.startswith("configuration:")), "")
            info["libvpx"], info["libx264"] = "--enable-libvpx" in config, "--enable-libx264" in config
    return info


def ffmpeg_checks(info: dict) -> list[Check]:
    if not info["ffmpeg"]:
        return [Check("ffmpeg", "MISSING", "ffmpeg is not on PATH; video decode and encode are unavailable "
                      "(PNG-frame input still works)", "install ffmpeg 5.1 or newer (it ships ffprobe) and reopen the shell")]
    checks = []
    version = info["version"]
    numbers = _version_tuple((version or "").lstrip("n"))
    where = display_path(info["ffmpeg"])
    if version is None:
        checks.append(Check("ffmpeg", "UNKNOWN", f"{where}: the version could not be read",
                            "run ffmpeg -version; video2dsprite needs 5.1 or newer"))
    elif numbers is None:
        checks.append(Check("ffmpeg", "WARN", f"{where}: development build {version}; 5.1+ cannot be confirmed",
                            "prefer a release build 5.1 or newer"))
    elif numbers < FFMPEG_MIN:
        checks.append(Check("ffmpeg", "FAIL", f"{where}: version {version} is older than 5.1",
                            "install ffmpeg 5.1 or newer; video packaging and decoding need it"))
    else:
        checks.append(Check("ffmpeg", "OK", f"{version} at {where}"))
    if not info["ffprobe"]:
        checks.append(Check("ffprobe", "MISSING", "ffprobe is not on PATH",
                            "install the full ffmpeg package, which ships ffprobe"))
    for flag, use in (("libvpx", "VP9 alpha WebM"), ("libx264", "packed-alpha MP4")):
        if info[flag] is False:
            checks.append(Check(f"ffmpeg.{flag}", "WARN", f"this build has no {flag}: {use} export fails",
                                f"install an ffmpeg build with {flag} (for example a full build)"))
    return checks


def encoding_checks(console_encoding: str | None) -> list[Check]:
    """The console encoding this process started with, and the default file encoding."""
    name = console_encoding or "ascii"
    try:
        canonical = codecs.lookup(name).name
    except LookupError:
        canonical = name
    remedy = "set PYTHONUTF8=1 before running Python (PowerShell: $env:PYTHONUTF8=1; bash: export PYTHONUTF8=1)"
    checks = []
    if canonical == "utf-8":
        checks.append(Check("encoding.stdout", "OK", f"stdout={name}"))
    else:
        bad = []
        for glyph in GLYPHS:
            try:
                glyph.encode(name)
            except (UnicodeEncodeError, LookupError):
                bad.append(f"U+{ord(glyph):04X}")
        if bad:
            checks.append(Check("encoding.stdout", "FAIL", f"stdout={name} cannot print {', '.join(bad)}: Python "
                                "scripts that print them (your own helpers, older tools) crash with "
                                "UnicodeEncodeError after doing their work", remedy))
        else:
            checks.append(Check("encoding.stdout", "WARN", f"stdout={name}: non-ASCII text can be garbled in pipes", remedy))
    preferred = locale.getpreferredencoding(False)
    if not sys.flags.utf8_mode and codecs.lookup(preferred).name != "utf-8":
        checks.append(Check("encoding.files", "WARN", f"open() and read_text() without encoding= use {preferred}",
                            "pass encoding='utf-8' in your own scripts, or set PYTHONUTF8=1"))
    return checks


def path_checks(cwd: Path, skills_root: Path) -> list[Check]:
    checks = []
    text = str(cwd)
    if not text.isascii() or " " in text:
        checks.append(Check("paths.cwd", "WARN", "the working folder has spaces or non-ASCII characters",
                            "quote every path; prefer ASCII output folder names for ffmpeg frame patterns"))
    with contextlib.suppress(OSError, ValueError):
        if cwd.resolve().is_relative_to(skills_root.resolve()):
            checks.append(Check("paths.project", "WARN", "this shell is inside the skills folder; outputs would land "
                                "in the installed skill", "cd to the project root before running Forge tools"))
    return checks


def _vendored_files(root: Path) -> dict[str, list[Path]]:
    groups: dict[str, list[Path]] = {}
    for skill in SKILLS:
        folder = root / skill
        for path in [*folder.glob("scripts/forge_*.py"), *folder.glob("references/schemas/*.schema.json")]:
            if path.is_file():
                groups.setdefault(path.relative_to(folder).as_posix(), []).append(path)
    return groups


def skill_checks(root: Path) -> list[Check]:
    """Sibling skills, vendored-copy consistency, canonical sync (source checkout) and install drift."""
    checks = []
    present = [s for s in SKILLS if (root / s / "SKILL.md").is_file()]
    absent = [s for s in SKILLS if s not in present]
    if absent:
        checks.append(Check("skills.present", "WARN", f"{display_path(root)}: missing {', '.join(absent)}",
                            "install all five skills from one checkout (tools/install_skills.py --apply)"))
    else:
        checks.append(Check("skills.present", "OK", f"{display_path(root)}: all {len(SKILLS)} skills"))
    mismatched = sorted(name for name, paths in _vendored_files(root).items()
                        if len(paths) > 1 and len({content_digest(p) for p in paths}) > 1)
    if mismatched:
        checks.append(Check("skills.vendored", "WARN", "vendored copies differ between skills: " + ", ".join(mismatched[:4]),
                            "the skills come from different versions; reinstall all of them from one checkout"))
    else:
        checks.append(Check("skills.vendored", "OK", "vendored shared files agree across skills"))
    vendored = root.parent / "shared" / "VENDORED.json"
    if vendored.is_file():
        checks.append(_canonical_check(root.parent, vendored))
    manifest = root / INSTALL_MANIFEST
    if manifest.is_file():
        checks.append(_install_check(root, manifest))
    return checks


def _canonical_check(repo: Path, vendored: Path) -> Check:
    try:
        entries = json.loads(vendored.read_text(encoding="utf-8-sig"))["files"]
        stale = []
        for entry in entries:
            canonical = repo / entry["canonical"]
            if not canonical.is_file():
                continue
            expected = content_digest(canonical)
            stale += [t for t in entry["targets"] if not (repo / t).is_file() or content_digest(repo / t) != expected]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return Check("skills.canonical", "WARN", f"shared/VENDORED.json is unreadable ({type(exc).__name__})",
                     "python tools/vendor_sync.py --check")
    if stale:
        return Check("skills.canonical", "WARN", f"{len(stale)} vendored cop{'y is' if len(stale) == 1 else 'ies are'} "
                     f"out of date: {', '.join(stale[:3])}", "python tools/vendor_sync.py --write")
    return Check("skills.canonical", "OK", "source checkout: vendored copies match shared/")


def shipped(relative: Path) -> bool:
    """Whether a file inside a skill folder belongs to an install (twin of tools/install_skills.shipped):
    __pycache__, bytecode, dot files and OS junk never do, so they are never drift (D24)."""
    if any(part in SKIP_DIRS or part.startswith(".") for part in relative.parts[:-1]):
        return False
    name = relative.name
    return not (name.startswith(".") or name in SKIP_FILES or Path(name).suffix.lower() in SKIP_SUFFIXES)


def _install_check(root: Path, manifest: Path) -> Check:
    safe = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_.][A-Za-z0-9_. -]*)*")
    try:
        data = json.loads(manifest.read_text(encoding="utf-8-sig"))
        files = {item["path"]: item["sha256"] for item in data["files"]
                 if safe.fullmatch(item["path"]) and ".." not in item["path"].split("/")}
        skills = [s for s in data["skills"] if isinstance(s, str) and safe.fullmatch(s) and "/" not in s]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return Check("skills.install", "WARN", f"{INSTALL_MANIFEST} is unreadable ({type(exc).__name__})",
                     "reinstall with python tools/install_skills.py --apply from your checkout")
    changed, missing = [], []
    for rel, digest in files.items():
        path = root / rel
        if not path.is_file():
            missing.append(rel)
        elif file_sha256(path) != digest:
            changed.append(rel)
    extra = sorted(p.relative_to(root).as_posix() for skill in skills for p in (root / skill).rglob("*")
                   if p.is_file() and shipped(p.relative_to(root / skill))
                   and p.relative_to(root).as_posix() not in files)
    if changed or missing or extra:
        parts = [f"{len(changed)} changed", f"{len(missing)} missing", f"{len(extra)} extra"]
        sample = ", ".join((changed + missing + extra)[:3])
        return Check("skills.install", "WARN", f"installed skills drifted from their install manifest ({', '.join(parts)}: "
                     f"{sample})", "run python tools/install_skills.py --check, then --apply, from your checkout")
    version = data.get("source", {}).get("commit") if isinstance(data.get("source"), dict) else None
    return Check("skills.install", "OK", f"{len(files)} files match the install manifest"
                 + (f" (source {str(version)[:12]})" if version else ""))


def ledger_check(project: Path) -> Check | None:
    """Unsettled reservations in the project's spend ledger (media_ledger.py summary)."""
    if not (project / ".forge" / "ledger.jsonl").is_file():
        return None
    try:
        here = str(Path(__file__).resolve().parent)
        if here not in sys.path:
            sys.path.insert(0, here)
        import media_ledger  # noqa: PLC0415  (sibling in this skill; imported only when a ledger exists)
        summary = media_ledger.Ledger(project).summary()
    except Exception as exc:  # noqa: BLE001  (a diagnostic must not crash on a damaged sibling)
        return Check("media.ledger", "UNKNOWN", f"the ledger could not be read ({type(exc).__name__})",
                      "python \"<skill-dir>/scripts/media_ledger.py\" summary")
    unknown = [s for s in summary["unsettled"] if s.get("status") == "unknown"]
    if unknown:
        return Check("media.ledger", "WARN", f"{len(unknown)} request(s) with an unknown outcome are unsettled",
                     "check the provider's usage history, then media_ledger.py settle <reservation> --status ...")
    session = summary.get("session") or {}
    if "caps" not in session:  # an unreadable FORGE_SESSION_* value
        return Check("media.ledger", "WARN", f"session cap: {session.get('error', 'unreadable')}",
                     "set FORGE_SESSION_IMAGES / FORGE_SESSION_VIDEOS to whole numbers and FORGE_SESSION_HOURS to hours")
    used = ", ".join(f"{session[kind]} of {session['caps'][kind]} {kind}s" for kind in ("image", "video"))
    detail = (f"{summary['calls']} recorded call(s), {summary['usd']} USD; local CLI session ({session['hours']:g} h): "
              f"{used}")
    full = [kind for kind in ("image", "video") if session[kind] >= session["caps"][kind]]
    if full:
        return Check("media.ledger", "WARN", detail + f"; the session cap is reached for {' and '.join(full)}s",
                     "local CLI calls of that kind stop until the window moves on; only the user may raise "
                     "FORGE_SESSION_IMAGES / FORGE_SESSION_VIDEOS")
    return Check("media.ledger", "OK", detail)


# --------------------------------------------------------------------------- the ladder and routes

def parse_host_tools(raw: str | None) -> tuple[bool, list[str], list[str]]:
    """(declared, canonical tool names, unrecognised names) from --host-tools."""
    if raw is None:
        return False, [], []
    tools, unknown = [], []
    for item in raw.split(","):
        name = re.sub(r"[\s-]+", "_", item.strip().lower())
        if not name or name == "none":
            continue
        canonical = HOST_TOOL_ALIASES.get(name)
        if canonical is None:
            unknown.append(ascii_text(item.strip())[:40])
        elif canonical not in tools:
            tools.append(canonical)
    return True, tools, unknown


def evaluate_ladder(capability: Capability, info: CliInfo, auth: str | None, proofs: list[dict]) -> dict:
    """Climb PRESENT -> AUTH_MODE -> TOOL_EXPOSED -> VERIFIED for one capability."""
    best, newest = find_proof(proofs, capability, info.version)
    steps = []
    if info.path is not None:
        how = {"npm": "native binary behind the npm launcher", "home": "Grok install folder",
               "override": OVERRIDE_ENV[info.cli], "PATH": "PATH"}.get(info.source, info.source)
        steps.append({"step": "PRESENT", "status": "OK",
                      "detail": f"{info.version_text or 'version not read'} ({how}: {display_path(info.path)})"})
    else:
        steps.append({"step": "PRESENT", "status": "WARN" if info.shim else "MISSING",
                      "detail": info.problem or "not found",
                      "remedy": f"install the {info.cli} CLI (optional route)" if not info.shim
                      else f"reinstall the {info.cli} CLI so its native binary is present"})
    present = info.path is not None
    implied = best is not None
    if auth in (None, "unknown"):
        steps.append({"step": "AUTH_MODE", "status": "OK" if implied else "UNKNOWN",
                      "detail": f"implied by the {best['level']} record of {best['verifiedAt'][:10]}" if implied
                      else "sign-in mode not checked" + (" (--probe-auth reads it)" if capability.cli == "codex"
                                                         else "; Grok has no read-only status command")})
    elif auth == "chatgpt":
        steps.append({"step": "AUTH_MODE", "status": "OK", "detail": "signed in with ChatGPT (subscription quota)"})
    elif auth == "none":
        steps.append({"step": "AUTH_MODE", "status": "MISSING", "detail": "not signed in", "remedy": "run codex login"})
    else:
        steps.append({"step": "AUTH_MODE", "status": "WARN", "detail": f"signed in with {auth}; cli_media.py strips API "
                      "keys and needs a ChatGPT sign-in", "remedy": "run codex login and choose ChatGPT"})
    if best is not None:
        steps.append({"step": "TOOL_EXPOSED", "status": "OK",
                      "detail": f"{capability.tool} answered on {best['verifiedAt'][:10]}"})
    elif newest is not None:
        steps.append({"step": "TOOL_EXPOSED", "status": "UNKNOWN",
                      "detail": f"seen with version {newest['version']}, not yet with {info.version or 'this version'}"})
    else:
        steps.append({"step": "TOOL_EXPOSED", "status": "UNKNOWN", "detail": f"{capability.tool} not seen yet"})
    if capability.tool == "image_edit":  # --verify-route grok-cli runs image_gen, which proves nothing about edits
        remedy = ("verify once with the user's consent (one quota call): a successful python "
                  "\"<skill-dir>/scripts/cli_media.py\" edit --route grok-cli --reference <image> --prompt-file <file> "
                  "--output-dir <new folder> --execute records the image_edit proof (--verify-route grok-cli "
                  "verifies image_gen only)")
    else:
        remedy = ("verify once with the user's consent (one quota call; repeat after a CLI update): python "
                  f"\"<skill-dir>/scripts/forge_doctor.py\" --verify-route {capability.route} --execute")
    if best is not None and best["level"] == "VERIFIED":
        steps.append({"step": "VERIFIED", "status": "OK",
                      "detail": f"proof for {best['version']} ({capability.recipe}) of {best['verifiedAt'][:10]}"})
    elif newest is not None and newest["level"] == "VERIFIED":
        steps.append({"step": "VERIFIED", "status": "WARN", "detail": f"the proof is for {newest['version']}; "
                      f"installed {info.version or 'version unknown'}", "remedy": remedy})
    else:
        steps.append({"step": "VERIFIED", "status": "UNKNOWN", "detail": "no proof for this version", "remedy": remedy})
    blocked = auth in ("none", "api-key", "workload-identity")
    if not present:
        level = None
    elif not blocked and best is not None:
        level = best["level"]
    elif auth == "chatgpt":
        level = "AUTH_MODE"
    else:
        level = "PRESENT"
    return {"route": capability.route, "tool": capability.tool, "recipe": capability.recipe, "level": level,
            "blocked": blocked, "version": info.version, "versionText": info.version_text, "steps": steps}


def _option(route: str, status: str, detail: str, **extra) -> dict:
    return {"route": route, "status": status, "detail": detail, **extra}


def plan_routes(host: list[str], declared: bool, keys: dict, ladders: dict, ffmpeg: dict, deps: dict,
                skills_root: Path) -> dict:
    """Usable art routes, local agent first (owner decision 13, D22). ``route`` is the first option
    that is ready (a host tool, or a local CLI route VERIFIED for the installed version, which runs
    without a per-call question within the session cap) or needs consent (the paid REST API).
    Unverified CLI routes are listed as options, never chosen."""
    undeclared = "" if declared else "; host tools undeclared (rerun with --host-tools)"

    def cli_options(*keys_):
        verified, unverified = [], []
        for key in keys_:
            ladder = ladders.get(key)
            if ladder is None or ladder["level"] is None or ladder["blocked"]:
                continue
            route = ladder["route"]
            label = ROUTE_LABEL[route]
            if ladder["level"] == "VERIFIED":
                verified.append(_option(route, "ready", f"{label}, verified for {ladder['version']}: runs without a "
                                        f"per-call question within the session cap ({SESSION_CAP_TEXT}); name the "
                                        "route; spends the user's subscription quota, not API credit",
                                        consent="quota", level="VERIFIED", label=label))
            else:
                unverified.append(_option(route, "unverified", f"{label} at {ladder['level']}: unused until verified "
                                          "(the one verification call needs the user's consent)", consent="quota",
                                          level=ladder["level"], label=label))
        return verified, unverified

    def api_option(providers):
        named = [p for p in providers if keys.get(p)]
        if not named:
            return []
        return [_option("api", "consent", f"generate2dmedia REST ({', '.join(named)} key set); paid per call, "
                        "dry-run and consent first", consent="paid", providers=named)]

    def choose(options, fallback):
        chosen = next((o for o in options if o["status"] in ("ready", "consent")), None)
        if chosen is None:
            return {"route": "none", "status": "none", "detail": fallback + undeclared, "options": options}
        return {"route": chosen["route"], "status": chosen["status"], "detail": chosen["detail"], "options": options}

    routes = {}
    for need, host_tool, providers, fallback in (
            ("image", "image_gen", ("openai", "xai"),
             "no image generation route: write the prompt and asset contract, report the missing capability, "
             "or use codeart2d inside its size envelope"),
            ("image_edit", "image_edit", ("openai", "xai"),
             "no reference-edit route: describe the edit and report the missing capability"),
            ("video", "image_to_video", ("xai",),
             "no video generation route: write a motion brief (an installed CLI is not a connected tool); "
             "supplied clips can still be processed")):
        options = [_option(f"host_{need}", "ready", "the calling agent's own media tool")] if host_tool in host else []
        verified, unverified = cli_options(*(c.key for c in CAPABILITIES if c.need == need))
        options += verified + api_option(providers) + unverified
        routes[need] = choose(options, fallback)
    numbers = _version_tuple((ffmpeg.get("version") or "").lstrip("n"))
    if not (ffmpeg.get("ffmpeg") and ffmpeg.get("ffprobe")):
        routes["clip"] = {"route": "png-frames", "status": "limited",
                          "detail": "ffmpeg or ffprobe is missing: process supplied PNG frames only"}
    elif ffmpeg.get("version") is None:
        routes["clip"] = {"route": "ffmpeg", "status": "unknown", "detail": "ffmpeg found; its version was not read"}
    elif numbers is not None and numbers >= FFMPEG_MIN:
        routes["clip"] = {"route": "ffmpeg", "status": "ready",
                          "detail": f"supplied clips decode and encode with ffmpeg {ffmpeg['version']}"}
    else:
        routes["clip"] = {"route": "png-frames", "status": "limited",
                          "detail": f"ffmpeg {ffmpeg['version']} is not a 5.1+ release: process supplied PNG frames only"}
    codeart = (skills_root / "codeart2d" / "scripts").is_dir()
    if codeart and deps.get("numpy") and deps.get("Pillow"):
        detail = "codeart2d PixelSpec" + (" and SVG (resvg-py)" if deps.get("resvg-py") else "; SVG needs resvg-py")
        routes["code_art"] = {"route": "codeart2d", "status": "ready", "detail": detail}
    else:
        routes["code_art"] = {"route": "none", "status": "none",
                              "detail": "codeart2d is not installed" if not codeart else "numpy and Pillow are required"}
    return routes


# --------------------------------------------------------------------------- the report

def diagnose(*, host_tools: str | None = None, project_dir: str | os.PathLike = ".", skills_root: Path | None = None,
             run_versions: bool = True, probe_auth: bool = False, console_encoding: str | None = None,
             started: float | None = None) -> dict:
    """Build the doctor_v1 report. Runs only local --version calls (and, with
    probe_auth, ``codex login status``), concurrently, unless run_versions is false."""
    started = time.perf_counter() if started is None else started
    project = Path(project_dir)
    root = (skills_root or Path(__file__).resolve().parents[2]).resolve()
    declared, host, unrecognised = parse_host_tools(host_tools)
    infos = {cli: resolve_cli(cli) for cli in ("codex", "grok")}
    with ThreadPoolExecutor(max_workers=4) as pool:
        ffmpeg_future = pool.submit(ffmpeg_probe) if run_versions else None
        version_futures = {cli: pool.submit(probe_version, [str(info.path)], cli)
                           for cli, info in infos.items() if run_versions and info.path is not None}
        login_future = (pool.submit(probe_codex_login, [str(infos["codex"].path)])
                        if probe_auth and infos["codex"].path is not None else None)
        checks = python_checks() + dependency_checks() + encoding_checks(console_encoding)
        checks += path_checks(Path.cwd(), root) + skill_checks(root)
        proofs, proof_problem = load_proofs(project)
        ffmpeg = ffmpeg_future.result() if ffmpeg_future else {"ffmpeg": shutil.which("ffmpeg"),
                                                               "ffprobe": shutil.which("ffprobe"), "version": None,
                                                               "libvpx": None, "libx264": None}
        for cli, future in version_futures.items():
            infos[cli].version_text, infos[cli].version = future.result()
            infos[cli].version_source = "--version" if infos[cli].version else None
        login = login_future.result() if login_future else None
    for info in infos.values():
        if info.version is None and info.path is not None:
            info.version = package_version(info)
            if info.version:
                info.version_text, info.version_source = f"{info.cli} {info.version} (package.json)", "package.json"
    if run_versions:
        checks += ffmpeg_checks(ffmpeg)
    else:
        checks.append(Check("ffmpeg", "UNKNOWN" if ffmpeg["ffmpeg"] else "MISSING",
                            "found on PATH; version not read (--no-exec)" if ffmpeg["ffmpeg"] else "ffmpeg is not on PATH",
                            None if ffmpeg["ffmpeg"] else "install ffmpeg 5.1 or newer (it ships ffprobe)"))
    if declared:
        checks.append(Check("media.host-tools", "OK", "declared: " + (", ".join(host) or "none")))
    else:
        checks.append(Check("media.host-tools", "AGENT", "a script cannot see the calling agent's tools",
                            "look at your own tool list and rerun with --host-tools image_gen[,image_edit,image_to_video] "
                            "or --host-tools none"))
    if unrecognised:
        checks.append(Check("media.host-tools.names", "WARN", "unrecognised tool names: " + ", ".join(unrecognised),
                            "use image_gen, image_edit, image_to_video or none"))
    keys = {}
    for variable, provider in API_KEYS:
        keys[provider] = bool(os.environ.get(variable, "").strip())
        checks.append(Check(f"media.api.{provider}", "UNKNOWN", f"{variable} is set (value not read); credit and model "
                            "access are unproven until an approved paid call") if keys[provider] else
                      Check(f"media.api.{provider}", "MISSING", f"{variable} is not set",
                            f"only if the user wants the paid {provider} API route: set {variable} in the environment"))
    ledger = ledger_check(project)
    if ledger is not None:
        checks.append(ledger)
    if proof_problem:
        checks.append(Check("media.proofs", "WARN", proof_problem, f"delete or repair {PROOFS_FILE.as_posix()}"))
    auth = {"codex": login, "grok": None}
    for cli, info in infos.items():
        name = {"codex": "Codex CLI", "grok": "Grok Build CLI"}[cli]
        if info.path is not None:
            detail = f"{info.version_text or 'version not read'} at {display_path(info.path)}"
            if info.shim is not None:
                detail += f" (behind the launcher {display_path(info.shim)})"
            checks.append(Check(f"cli.{cli}", "OK", detail))
        elif info.shim is not None:
            checks.append(Check(f"cli.{cli}", "WARN", f"{info.problem}: {display_path(info.shim)}",
                                f"reinstall the {name} so its native binary is present"))
        else:
            checks.append(Check(f"cli.{cli}", "MISSING", f"{name} {info.problem}",
                                f"optional: install the {name} and sign in, then verify a route"))
        if probe_auth and info.path is not None:
            if cli == "grok":
                checks.append(Check("cli.grok.auth", "UNKNOWN", "Grok Build has no read-only sign-in status command"))
            else:
                status = {"chatgpt": "OK", "none": "MISSING", "unknown": "UNKNOWN"}.get(login or "unknown", "WARN")
                checks.append(Check("cli.codex.auth", status, f"sign-in mode: {login}",
                                    None if status in ("OK", "UNKNOWN") else "run codex login and choose ChatGPT"))
    ladders = {}
    for capability in CAPABILITIES:
        info = infos[capability.cli]
        if info.path is None and info.shim is None:
            continue
        ladder = evaluate_ladder(capability, info, auth[capability.cli], proofs)
        ladders[capability.key] = ladder
        if info.path is not None:
            status = "OK" if ladder["level"] == "VERIFIED" else "WARN" if ladder["blocked"] else "UNKNOWN"
            top = next(s for s in reversed(ladder["steps"]) if s["step"] == "VERIFIED")
            checks.append(Check(f"route.{capability.key}", status, f"{ladder['level']}: {top['detail']}",
                                top.get("remedy") if status != "OK" else None))
    dep_ok = {c.id.removeprefix("deps."): c.status in ("OK", "WARN") for c in checks if c.id.startswith("deps.")}
    routes = plan_routes(host, declared, keys, ladders, ffmpeg, dep_ok, root)
    proof_view = {}
    for key, ladder in ladders.items():
        capability = next(c for c in CAPABILITIES if c.key == key)
        best, newest = find_proof(proofs, capability, ladder["version"])
        record = best or newest
        if record is not None:
            proof_view[key] = {"level": record["level"], "version": record["version"], "recipe": record["recipe"],
                               "verifiedAt": record["verifiedAt"], "matchesInstalled": best is not None}
    statuses = [c.status for c in checks]
    overall = "FAIL" if "FAIL" in statuses else "WARN" if "WARN" in statuses else "OK"
    return {"schema": DOCTOR_SCHEMA, "tool": {"name": TOOL_NAME, "version": TOOL_VERSION}, "createdAt": utc_timestamp(),
            "overall": overall, "host": {"declared": declared, "tools": host, "unrecognised": unrecognised},
            "checks": [c.as_dict() for c in checks], "cli": ladders, "routes": routes, "proofs": proof_view,
            "elapsedMs": round((time.perf_counter() - started) * 1000)}


def validate_report(report: dict) -> None:
    """The report's own contract (doctor_v1): refuse to publish one that breaks it."""
    problems = []
    if report.get("schema") != DOCTOR_SCHEMA:
        problems.append("schema id")
    for check in report.get("checks", []):
        if not isinstance(check.get("id"), str) or not check["id"]:
            problems.append("a check without an id")
        elif check.get("status") not in STATUSES:
            problems.append(f"{check['id']}: unknown status {check.get('status')!r}")
        elif check["status"] in ("FAIL", "MISSING") and not check.get("remedy"):
            problems.append(f"{check['id']}: {check['status']} without a remedy")
    if not isinstance(report.get("routes"), dict) or not isinstance(report.get("checks"), list):
        problems.append("routes or checks missing")
    if problems:
        raise DoctorError("the report breaks doctor_v1: " + "; ".join(problems))


def save_report(report: dict, target: Path) -> None:
    """Write the report as new UTF-8 JSON: staged beside the target, then linked into
    place so an existing file is never replaced and a failure leaves nothing."""
    target = Path(target)
    if os.path.lexists(target):
        raise FileExistsError(f"refusing to replace {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(report, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    handle, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".staged", dir=target.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            raise
        except OSError:  # no hard links on this volume: exclusive create
            with open(target, "xb") as stream:
                stream.write(payload)
    finally:
        Path(temporary).unlink(missing_ok=True)


def render_text(report: dict) -> str:
    tag = {status: f"[{status:<7}]" for status in STATUSES}
    lines = []
    for check in report["checks"]:
        lines.append(f"{tag[check['status']]} {check['id']:<30} {check['detail']}")
        if check.get("remedy") and check["status"] != "OK":
            lines.append(f"{'':11}-> {check['remedy']}")
    if report["cli"]:
        lines.append(f"CLI LADDER ({' -> '.join(LADDER)})")
        for key, ladder in report["cli"].items():
            marks = " ".join(f"{s['step']}={s['status']}" for s in ladder["steps"])
            lines.append(f"  {key:<30} {ladder['level'] or 'NONE':<13} {marks}")
    lines.append("ROUTES")
    for need, route in report["routes"].items():
        lines.append(f"  {need:<11} {route['route']:<11} {route['status']:<10} {route['detail']}")
        for option in route.get("options", []):
            if option["route"] != route["route"]:
                lines.append(f"  {'':11} option: {option['route']} ({option['status']}) {option['detail']}")
    counts = {status: sum(c["status"] == status for c in report["checks"]) for status in ("FAIL", "WARN", "MISSING")}
    lines.append(f"overall {report['overall']}: {counts['FAIL']} FAIL, {counts['WARN']} WARN, {counts['MISSING']} MISSING "
                 f"({report['elapsedMs']} ms)")
    return ascii_text("\n".join(lines))


# --------------------------------------------------------------------------- route verification (opt-in)

VERIFY_PROMPT = ("A single small pixel-art red apple icon, centred, on a flat pure magenta (#FF00FF) background. "
                 "Simple shapes, no text, no shadow on the background.")
VERIFY_VIDEO_PROMPT = "The red square pulses gently in place. Locked camera, flat magenta background, no text."


def _tiny_png(size: int = 256) -> bytes:
    """A deterministic magenta PNG with a red square (stdlib), the verification reference."""
    row_bg = b"\xff\x00\xff" * size
    lo, hi = size // 3, 2 * size // 3
    row_sq = b"\xff\x00\xff" * lo + b"\xd0\x20\x20" * (hi - lo) + b"\xff\x00\xff" * (size - hi)
    raw = b"".join(b"\x00" + (row_sq if lo <= y < hi else row_bg) for y in range(size))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def verification_argv(route: str, project: Path, work: Path) -> list[str]:
    """The cli_media.py arguments that verify one route (without --execute).

    Re-verification after a CLI update must work (D23): --verification puts the installed CLI
    version into the request's fingerprint, and --allow-duplicate lets an explicitly consented
    verification run again although an identical earlier one succeeded."""
    # A unique folder per verification: two checks within one second must not collide (OUTPUT_EXISTS).
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + os.urandom(3).hex()
    output = project / ".forge" / "route-checks" / f"{route}-{stamp}"
    prompt = work / "verify-prompt.txt"
    common = ["--output-dir", str(output), "--project-dir", str(project), "--purpose", "route verification",
              "--verification", "--allow-duplicate"]
    if route == "grok-acp":
        return ["video", "--route", route, "--prompt-file", str(prompt), "--reference", str(work / "verify-reference.png"),
                "--duration", "2", "--resolution", "480p", *common]
    return ["image", "--route", route, "--prompt-file", str(prompt), *common]


def verify_route(route: str, project: Path, execute: bool) -> int:
    """Dry-run (default) or run the one quota call that records a route proof."""
    if not execute:
        argv = verification_argv(route, project, Path("<temp>"))
        print(json.dumps({"verifyRoute": route, "execution": "dry-run", "calls": 1, "consent": "quota",
                          "note": "add --execute after the user approves one call on their subscription quota",
                          "command": ["python", "<skill-dir>/scripts/cli_media.py", *argv, "--execute"]},
                         ensure_ascii=True))
        return 0
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    import cli_media  # noqa: PLC0415  (sibling; loaded only for an approved verification)
    with tempfile.TemporaryDirectory(prefix="forge-verify-") as folder:
        work = Path(folder)
        (work / "verify-prompt.txt").write_text(VERIFY_VIDEO_PROMPT if route == "grok-acp" else VERIFY_PROMPT,
                                                encoding="utf-8")
        (work / "verify-reference.png").write_bytes(_tiny_png())
        return cli_media.main([*verification_argv(route, project, work), "--execute"])


# --------------------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host-tools", metavar="LIST",
                        help="comma list of the media tools in YOUR tool list (image_gen, image_edit, image_to_video) "
                             "or none; scripts cannot see them")
    parser.add_argument("--json", action="store_true", help="print the report as one line of ASCII JSON")
    parser.add_argument("--save", type=Path, metavar="FILE", help="also write the report to this new JSON file "
                        "(refuses an existing file), for example beside your outputs")
    parser.add_argument("--project-dir", type=Path, default=Path("."),
                        help="project root holding .forge/ (ledger and route proofs; default: current folder)")
    parser.add_argument("--no-exec", action="store_true", help="run nothing at all, not even --version")
    parser.add_argument("--probe-auth", action="store_true",
                        help="also run 'codex login status' (local; the CLI reads its own sign-in, this tool reads none)")
    parser.add_argument("--skills-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--verify-route", choices=sorted({c.route for c in CAPABILITIES}),
                        help="print (dry-run) or, with --execute, run one cli_media.py call that records a route proof")
    parser.add_argument("--execute", action="store_true", help="with --verify-route: spend the one quota call")
    return parser


def _mask_home(text: str) -> str:
    home = str(Path.home())
    return text if len(home) <= 3 else text.replace(home, "~").replace(home.replace("\\", "/"), "~")


def main(argv: list[str] | None = None) -> int:
    """Usage errors exit 2 (argparse, D26); a report with a FAIL check is printed and exits 1; any
    other failure prints ``error: <message>`` or ``error: internal error (<Type>: <message>)`` (D27)."""
    started = time.perf_counter()
    console = getattr(sys.stdout, "encoding", None)  # read before the streams are reconfigured
    _local_utf8_stdio()
    sys.dont_write_bytecode = True  # never leave __pycache__ inside an installed skill
    args = build_parser().parse_args(argv)
    try:
        if args.verify_route:
            return verify_route(args.verify_route, args.project_dir, args.execute)
        if args.execute:
            raise DoctorError("--execute only applies to --verify-route")
        report = diagnose(host_tools=args.host_tools, project_dir=args.project_dir, skills_root=args.skills_root,
                          run_versions=not args.no_exec, probe_auth=args.probe_auth and not args.no_exec,
                          console_encoding=console, started=started)
        validate_report(report)
        if args.save:
            save_report(report, args.save)
        if args.json:
            shown = {**report, "saved": str(args.save)} if args.save else report
            text = json.dumps(shown, ensure_ascii=True, separators=(",", ":"))
        else:
            text = render_text(report) + (ascii_text(f"\nsaved: {args.save}") if args.save else "")
    except (DoctorError, OSError) as exc:
        print("error: " + ascii_text(_mask_home(str(exc))), file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001  (D27: a diagnostic never ends in a traceback)
        print("error: " + ascii_text(_mask_home(f"internal error ({type(exc).__name__}: {exc})")), file=sys.stderr)
        return 1
    print(text)
    failed = [c["id"] for c in report["checks"] if c["status"] == "FAIL"]
    if failed:
        print("error: " + ascii_text(f"{len(failed)} check(s) failed: {', '.join(failed)}"), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
