#!/usr/bin/env python3
"""One entry point for every generated image or video clip (owner decision 2026-10-06).

  route_media.py image --prompt-file P [--reference R ...] [--size 1024x1024] --out-dir D [--route ROUTE]
  route_media.py video --prompt-file P --reference FIRST.png [--last-frame LAST.png] [--duration 6]
                       [--resolution 720p] --out-dir D [--route ROUTE]
  route_media.py resolve --kind image|video [--route ROUTE] [--references N] [--resolution R]

Route order (--route auto):
  1. api: when a key is configured (OPENAI_API_KEY / XAI_API_KEY in the environment or
     in the user config file, see media_config.py). The configured key is the owner's
     consent, so the request is sent at once through generate_media.py. Images: OpenAI
     (gpt-image), then xAI (grok-imagine-image). Video: xAI (grok-imagine-video;
     --last-frame pins the end frame where the model allows it).
  2. local: the user's own signed-in CLI through cli_media.py (subscription quota,
     never an API key). Images: Codex (codex exec image_gen, references attached), then
     Grok one-shot (image_gen, or image_edit of one reference). Video: Grok in ACP mode
     (image_to_video; it cannot pin a last frame). The first successful run of a CLI
     version records its VERIFIED proof.
  3. none: prints {"status":"no-route","fallback":"codeart2d"} and exits 3. Use
     codeart2d only then, or when the user asks for code-drawn art.

--route api or local keeps one group; openai, xai, codex-cli, grok-cli or grok-acp names
one route. Within a group, a route whose account cannot serve the request (no key, no
credit, no model access, rate limited, unreachable, not signed in, tool missing) passes
it to the next route; any other failure stops. A refused API attempt's folder is kept
beside the output as <out-dir>.failed-<route>.

Success prints one ASCII JSON line and exits 0:
  {"status":"ok","route":"api:openai","artifact":"<out-dir>/generated.png",
   "sha256":"...","estimateUsd":null,...}
A failure prints one "error: ..." line and exits 1; usage errors exit 2. Every call is a
line in <project-dir>/.forge/ledger.jsonl with its estimate; there is no cap unless
--budget-usd, --max-calls or the FORGE_* cap variables set one. --dry-run prints the
plan of the route that would run: no key is used, nothing is sent or written.

Test seam: when FORGE_ROUTE_MEDIA_FAKE names a script, a command is checked as usual
and then handed to it (python <script> <the same arguments>); its output and exit code
are returned unchanged.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.dont_write_bytecode = True  # never leave __pycache__ inside an installed skill
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import cli_media  # noqa: E402  (siblings in this skill's scripts/)
import forge_doctor  # noqa: E402
import generate_media  # noqa: E402
import media_config  # noqa: E402
import media_ledger  # noqa: E402

FAKE_ENV = "FORGE_ROUTE_MEDIA_FAKE"
NO_ROUTE_EXIT = 3
FALLBACK = "codeart2d"
ORDER = {"image": ("api:openai", "api:xai", "local:codex-cli", "local:grok-cli"),
         "video": ("api:xai", "local:grok-acp")}
ROUTE_CHOICES = {"image": ("auto", "api", "local", "openai", "xai", "codex-cli", "grok-cli"),
                 "video": ("auto", "api", "local", "xai", "grok-acp")}
MAX_REFERENCES = {"api:openai": 16, "api:xai": 1, "local:codex-cli": cli_media.MAX_IMAGE_REFERENCES,
                  "local:grok-cli": 1}
LOCAL_CLI = {"codex-cli": "codex", "grok-cli": "grok", "grok-acp": "grok"}
VIDEO_RESOLUTIONS = ("480p", "720p", "1080p")
XAI_RATIOS = {"1:1": 1.0, "16:9": 16 / 9, "9:16": 9 / 16, "4:3": 4 / 3, "3:4": 3 / 4, "3:2": 3 / 2, "2:3": 2 / 3}
PINNING_MODELS = ("grok-imagine-video-1.5",)
SIZE = re.compile(r"auto|[1-9]\d{1,4}x[1-9]\d{1,4}")
# Failures about the route or the account, never about the request: the next route may serve it. An API
# attempt passes it on only when its job.json shows a clean refusal (failed or not_sent): a video job the
# provider accepted before polling failed may still finish and be charged, so it stops for resume instead.
API_NEXT = frozenset({"no_key", "auth", "quota", "rate_limit", "entitlement", "not_sent"})
CLEAN_REFUSALS = frozenset({"failed", "not_sent"})
LOCAL_NEXT = frozenset({"NOT_INSTALLED", "SPAWN_FAILED", "AUTH_REQUIRED", "RATE_LIMIT", "TOOL_UNAVAILABLE",
                        "GENERATION_FAILED"})


class RouteError(Exception):
    """A failure that ends the command: ``error: <message>``, exit 1."""


@dataclass
class Candidate:
    """One route that can take the request: its id, the API model and the last-frame decision."""

    route: str
    model: str | None = None
    pin: bool = False
    note: str | None = None

    @property
    def family(self) -> str:
        return self.route.split(":", 1)[0]

    @property
    def target(self) -> str:
        return self.route.split(":", 1)[1]

    @property
    def label(self) -> str:
        if self.family == "api":
            return f"{forge_doctor.API_LABEL[self.target]} ({self.model})"
        return forge_doctor.ROUTE_LABEL[self.target]


@dataclass
class Resolution:
    kind: str
    requested: str
    order: list
    candidates: list
    skipped: list


# --------------------------------------------------------------------------- output

def _ascii(text: object) -> str:
    return str(text).encode("ascii", "backslashreplace").decode("ascii")


def redact(text: object) -> str:
    """Console-safe text without any configured key."""
    text = str(text)
    for secret in media_config.known_secrets():
        if len(secret) >= 8:
            text = text.replace(secret, "[redacted]")
    return _ascii(text)


def emit(result: dict) -> None:
    print(redact(json.dumps(result, ensure_ascii=True, separators=(",", ":"))))


def error(message: object) -> None:
    print("error: " + redact(message), file=sys.stderr)


# --------------------------------------------------------------------------- resolution

def requested_routes(kind: str, route: str) -> list:
    order = ORDER[kind]
    if route == "auto":
        return list(order)
    if route in ("api", "local"):
        return [name for name in order if name.startswith(route + ":")]
    return [name for name in order if name.split(":", 1)[1] == route]


def xai_image_options(size: str | None, references: int) -> list:
    """--size as xAI understands it: the nearest supported aspect ratio (not for edits, which keep the
    reference's shape) and 1k up to 1024 px on the long side, else 2k."""
    if not size or size == "auto":
        return []
    width, height = (int(part) for part in size.split("x"))
    options = ["--resolution", "1k" if max(width, height) <= 1024 else "2k"]
    if not references:
        ratio = min(XAI_RATIOS, key=lambda name: abs(math.log(width / height / XAI_RATIOS[name])))
        options += ["--aspect-ratio", ratio]
    return options


def resolve(kind: str, route: str = "auto", *, references: int | None = None, resolution: str = "720p",
            last_frame: bool = False, settings: dict | None = None) -> Resolution:
    """The candidates for one request, in order, and why the others were skipped. Spawns nothing:
    a local route counts when its CLI's native executable is installed."""
    problem = None
    if settings is None:
        settings, problem = media_config.load_config()
    references = (0 if kind == "image" else 1) if references is None else references
    order = requested_routes(kind, route)
    candidates, skipped, clis = [], [], {}
    if problem and any(name.startswith("api:") for name in order):
        skipped.append(f"user config file ignored: {problem}")
    for name in order:
        family, target = name.split(":", 1)
        if family == "api":
            if not media_config.api_key(target, settings):
                skipped.append(f"{name}: no {media_config.PROVIDERS[target]} in the environment or the user config file")
                continue
            candidate = Candidate(name, model=media_config.model_for(f"{target}-{kind}", settings))
        else:
            cli = LOCAL_CLI[target]
            if cli not in clis:
                clis[cli] = cli_media.resolve_route_cli(cli)
            info = clis[cli].info
            if info.path is None or not clis[cli].prefix:
                skipped.append(f"{name}: {cli_media.CLI_NAME[cli]} is not installed ({info.problem or 'not found'})")
                continue
            candidate = Candidate(name)
        problem = unsupported(kind, candidate, references, resolution)
        if problem:
            skipped.append(f"{name}: {problem}")
            continue
        if kind == "video" and last_frame:
            candidate.pin = candidate.route == "api:xai" and candidate.model in PINNING_MODELS \
                and resolution in ("480p", "720p")
            if not candidate.pin:
                candidate.note = ("the last frame is not pinned: " + (
                    f"{candidate.model} at {resolution} cannot take one" if candidate.family == "api"
                    else "Grok (local CLI) image_to_video takes the first frame only"))
        candidates.append(candidate)
    return Resolution(kind, route, list(order), candidates, skipped)


def unsupported(kind: str, candidate: Candidate, references: int, resolution: str) -> str | None:
    if kind == "image":
        limit = MAX_REFERENCES[candidate.route]
        if references > limit:
            return f"takes at most {limit} reference image{'s' if limit > 1 else ''}"
        return None
    if candidate.route == "api:xai" and candidate.model == "grok-imagine-video" and resolution == "1080p":
        return "grok-imagine-video renders 480p or 720p"
    if candidate.route == "local:grok-acp" and resolution not in cli_media.VIDEO_RESOLUTIONS:
        return "Grok (local CLI) image_to_video renders 480p or 720p"
    return None


def no_route(resolution: Resolution) -> dict:
    return {"status": "no-route", "fallback": FALLBACK, "kind": resolution.kind, "requested": resolution.requested,
            "skipped": resolution.skipped}


# --------------------------------------------------------------------------- inputs

def check_inputs(args: argparse.Namespace) -> list:
    """The checks every command passes before any route (or the test fake) sees it. Returns warnings."""
    warnings = []
    for label, value in (("prompt file", args.prompt_file), *(("reference image", r) for r in references_of(args)),
                         ("last frame", getattr(args, "last_frame", None))):
        if value is not None and not Path(value).is_file():
            raise RouteError(f"the {label} {Path(value).name} does not exist")
    if os.path.lexists(args.out_dir):
        if not args.dry_run:
            raise RouteError(f"{Path(args.out_dir).name} already exists; choose a new --out-dir")
        warnings.append("the output folder already exists; a real run will refuse it")
    if args.command == "image":
        if args.size is not None and not SIZE.fullmatch(args.size):
            raise RouteError("--size must be auto or WIDTHxHEIGHT, for example 1024x1024")
        if len(args.reference) > max(MAX_REFERENCES.values()):
            raise RouteError(f"at most {max(MAX_REFERENCES.values())} reference images")
    else:
        if not 1 <= args.duration <= 15:
            raise RouteError("--duration must be 1..15 seconds")
        if args.last_frame and args.resolution == "1080p":
            warnings.append("no route pins a last frame at 1080p")
    return warnings


def references_of(args: argparse.Namespace) -> list:
    if args.command == "image":
        return list(args.reference)
    return [args.reference] if getattr(args, "reference", None) else []


# --------------------------------------------------------------------------- the routes

def _common(args: argparse.Namespace) -> list:
    argv = ["--project-dir", str(args.project_dir), "--purpose", args.purpose or f"route_media {args.command}"]
    for name in ("max_calls", "timeout"):
        value = getattr(args, name)
        if value is not None:
            argv += ["--" + name.replace("_", "-"), str(value)]
    return argv


def api_argv(candidate: Candidate, args: argparse.Namespace) -> list:
    """generate_media.py arguments; --execute because the configured key is the owner's consent."""
    argv = [args.command, "--provider", candidate.target, "--model", candidate.model, "--prompt-file",
            str(args.prompt_file), "--out-dir", str(args.out_dir), *_common(args), "--allow-duplicate"]
    if args.budget_usd is not None:
        argv += ["--budget-usd", str(args.budget_usd)]
    if args.command == "image":
        for reference in args.reference:
            argv += ["--reference", str(reference)]
        if candidate.target == "openai":
            argv += ["--size", args.size] if args.size else []
        else:
            argv += xai_image_options(args.size, len(args.reference))
    else:
        argv += ["--reference", str(args.reference), "--duration", str(args.duration), "--resolution", args.resolution]
        if candidate.pin:
            argv += ["--last-frame", str(args.last_frame)]
    if not args.dry_run:
        argv.append("--execute")
    return argv


def local_argv(candidate: Candidate, args: argparse.Namespace) -> list:
    """cli_media.py arguments for the one named local route (its first success records the proof)."""
    if args.command == "video":
        verb, references = "video", [args.reference]
    elif candidate.target == "grok-cli" and args.reference:
        verb, references = "edit", args.reference[:1]
    else:
        verb, references = "image", list(args.reference)
    argv = [verb, "--route", candidate.target, "--prompt-file", str(args.prompt_file), "--output-dir",
            str(args.out_dir), *_common(args), "--allow-duplicate"]
    for reference in references:
        argv += ["--reference", str(reference)]
    if verb == "video":
        argv += ["--duration", str(args.duration), "--resolution", args.resolution]
    if not args.dry_run:
        argv.append("--execute")
    return argv


def run_api(candidate: Candidate, args: argparse.Namespace, transport) -> dict:
    job_args = generate_media.parser(generate_media._JobArgsParser).parse_args(api_argv(candidate, args))
    job = generate_media.execute(job_args, transport)
    if args.dry_run:
        return {"estimateUsd": job["estimate"]["usd"], "estimate": job["estimate"]["basis"],
                "warnings": job.get("warnings", [])}
    artifact = job["artifact"]
    return {"artifact": str(Path(args.out_dir) / artifact["path"]), "sha256": artifact["sha256"],
            "estimateUsd": job["estimate"]["usd"], "job": str(Path(args.out_dir) / "job.json")}


def run_local(candidate: Candidate, args: argparse.Namespace) -> dict:
    job_args = cli_media.build_parser(cli_media._ArgsParser).parse_args(local_argv(candidate, args))
    request = cli_media.build_request(job_args)
    cli = cli_media.resolve_route_cli(request.capability.cli)
    if args.dry_run:
        plan = cli_media.dry_run(request, cli)
        return {"estimateUsd": 0.0, "estimate": plan["estimate"]["basis"], "warnings": plan["warnings"],
                "command": plan["command"]}
    result = cli_media.execute(request, cli, job_args)
    return {"artifact": result["artifact"], "sha256": result["sha256"], "estimateUsd": 0.0, "job": result["metadata"],
            "verifiedBefore": result["verifiedBefore"]}


def model_of(candidate: Candidate, args: argparse.Namespace) -> str:
    """The requested model: the API model, or <cli>-<tool> for a local route (as job.json records it)."""
    if candidate.family == "api":
        return candidate.model
    tool = "image_to_video" if args.command == "video" else (
        "image_edit" if candidate.target == "grok-cli" and args.reference else "image_gen")
    return f"{LOCAL_CLI[candidate.target]}-{tool}"


def refused_cleanly(out_dir: Path) -> bool:
    """True when a failed API attempt certainly left no provider job behind: no job folder, or a job.json whose
    status is failed or not_sent."""
    try:
        status = json.loads((out_dir / "job.json").read_text(encoding="utf-8-sig")).get("status")
    except FileNotFoundError:
        return not os.path.lexists(out_dir)
    except (OSError, ValueError, AttributeError):
        return False
    return status in CLEAN_REFUSALS


def set_aside(out_dir: Path, candidate: Candidate) -> str | None:
    """Move a refused API attempt's job folder out of the way (it documents the refusal); None when there
    is nothing to move."""
    if not os.path.lexists(out_dir):
        return None
    base = out_dir.parent / f"{out_dir.name}.failed-{candidate.route.replace(':', '-')}"
    for index in range(1, 100):
        target = base if index == 1 else base.with_name(f"{base.name}-{index}")
        if os.path.lexists(target):
            continue
        try:
            os.rename(out_dir, target)
        except OSError as exc:
            raise RouteError(f"{candidate.route} was refused and its job folder could not be moved aside "
                             f"({type(exc).__name__}); no other route was tried") from None
        return target.name
    raise RouteError(f"{candidate.route} was refused and too many earlier attempt folders exist beside the output")


def generate(args: argparse.Namespace, resolution: Resolution, transport=None) -> dict:
    """Run the candidates in order until one succeeds or one fails for a reason about the request."""
    attempts = []
    out_dir = Path(args.out_dir)
    for index, candidate in enumerate(resolution.candidates):
        last = index == len(resolution.candidates) - 1
        try:
            outcome = run_api(candidate, args, transport) if candidate.family == "api" else run_local(candidate, args)
        except generate_media.MediaError as exc:
            code, message = exc.code, str(exc)
            passes = code in API_NEXT and refused_cleanly(out_dir)
        except cli_media.CliMediaError as exc:
            code, message = exc.code, str(exc)
            passes = code in LOCAL_NEXT
        else:
            result = {"status": "dry-run" if args.dry_run else "ok", "route": candidate.route}
            result.update({k: outcome[k] for k in ("artifact", "sha256") if k in outcome})
            result["estimateUsd"] = outcome["estimateUsd"]
            result.update(kind=args.command, label=candidate.label, model=model_of(candidate, args))
            result.update({k: outcome[k] for k in ("job", "verifiedBefore", "estimate", "command") if k in outcome})
            if args.command == "video":
                result["lastFrameUsed"] = candidate.pin
            notes = [candidate.note] if candidate.note else []
            warnings = [*args.input_warnings, *outcome.get("warnings", [])]
            if notes:
                result["notes"] = notes
            if warnings and args.dry_run:
                result["warnings"] = warnings
            if attempts:
                result["attempts"] = attempts
            return result
        attempt = {"route": candidate.route, "code": code, "message": redact(message)[:300]}
        if passes and not last and not args.dry_run:
            if candidate.family == "api":
                moved = set_aside(out_dir, candidate)
                if moved:
                    attempt["keptIn"] = moved
            attempts.append(attempt)
            continue
        attempts.append(attempt)
        if len(attempts) == 1:
            raise RouteError(f"{candidate.route}: {code}: {message}")
        tried = "; ".join(f"{a['route']} {a['code']}" for a in attempts[:-1])
        raise RouteError(f"{candidate.route}: {code}: {message} (earlier routes refused the request: {tried})")
    raise RouteError("no route was tried")  # unreachable: callers handle an empty resolution


# --------------------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    for kind, text in (("image", "generate one image (a still, an edit from references or a sheet)"),
                       ("video", "animate one approved still (image to video)")):
        command = commands.add_parser(kind, help=text, description=text)
        command.add_argument("--prompt-file", required=True, help="UTF-8 prompt written by the agent")
        if kind == "image":
            command.add_argument("--reference", action="append", default=[],
                                 help="reference image (PNG, JPEG or WebP), repeatable, in the order the prompt names "
                                      "them; xAI and Grok (local CLI) take one, Codex up to 8, OpenAI up to 16")
            command.add_argument("--size", default="1024x1024",
                                 help="WIDTHxHEIGHT or auto (default 1024x1024); the local CLIs take the size from the "
                                      "prompt, so state it there too")
        else:
            command.add_argument("--reference", required=True, help="the first frame: the approved still")
            command.add_argument("--last-frame", help="pin the end frame (same canvas as --reference) where the route "
                                                      "allows it; the result says lastFrameUsed")
            command.add_argument("--duration", type=int, default=6, help="seconds, 1..15 (default 6)")
            command.add_argument("--resolution", choices=VIDEO_RESOLUTIONS, default="720p")
        command.add_argument("--out-dir", required=True, help="new folder for generated.<ext>, job.json and prompt.txt")
        command.add_argument("--route", choices=ROUTE_CHOICES[kind], default="auto",
                             help="auto (default): " + " > ".join(ORDER[kind]) + "; api or local: one group; or one route")
        command.add_argument("--project-dir", default=".", help="project root holding .forge/ (ledger, proofs)")
        command.add_argument("--purpose", help="free text for the receipt (at most 200 characters)")
        command.add_argument("--timeout", type=float, help="seconds a route may take (default: the route's own)")
        command.add_argument("--budget-usd", type=float, help="opt-in: refuse a paid call that would take this project's "
                                                              "recorded spend above this many USD")
        command.add_argument("--max-calls", type=int, help="opt-in: refuse once the project's ledger holds this many calls")
        command.add_argument("--dry-run", action="store_true", help="print the plan of the route that would run; "
                                                                    "nothing is sent or written")
    resolve_cmd = commands.add_parser("resolve", help="print the route a request would use",
                                      description="Print the route a request would use (nothing runs).")
    resolve_cmd.add_argument("--kind", required=True, choices=("image", "video"))
    resolve_cmd.add_argument("--route", default="auto", choices=sorted(set(ROUTE_CHOICES["image"] + ROUTE_CHOICES["video"])))
    resolve_cmd.add_argument("--references", type=int, help="reference images of the request (default: 0 for an "
                                                            "image, 1 for a video)")
    resolve_cmd.add_argument("--resolution", choices=VIDEO_RESOLUTIONS, default="720p", help="video resolution")
    return parser


def delegate(fake: str, argv: list) -> int:
    """Hand the checked command to the test fake named by FORGE_ROUTE_MEDIA_FAKE; relay its output."""
    path = Path(fake)
    command = [sys.executable, str(path), *argv] if path.suffix.lower() == ".py" else [str(path), *argv]
    try:
        done = subprocess.run(command, capture_output=True, stdin=subprocess.DEVNULL, check=False)
    except OSError as exc:
        error(f"{FAKE_ENV} could not be started ({type(exc).__name__})")
        return 1
    sys.stdout.write(done.stdout.decode("utf-8", "replace"))
    sys.stderr.write(done.stderr.decode("utf-8", "replace"))
    sys.stdout.flush()
    sys.stderr.flush()
    return done.returncode


def main(argv: list | None = None, transport=None) -> int:
    """Exit 0 on success (and for resolve and --dry-run), 1 on failure (one ``error:`` line), 2 on a
    usage error, 3 when no route exists, 130 on Ctrl+C. ``transport`` replaces the API's HTTPS
    transport in tests."""
    media_ledger._local_utf8_stdio()
    raw = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(raw)
    if args.command == "resolve" and args.route not in ROUTE_CHOICES[args.kind]:
        parser.error(f"--route {args.route} is not a {args.kind} route (choose from {', '.join(ROUTE_CHOICES[args.kind])})")
    try:
        args.input_warnings = check_inputs(args) if args.command != "resolve" else []
        fake = os.environ.get(FAKE_ENV, "").strip()
        if fake:
            return delegate(fake, raw)
        if args.command == "resolve":
            found = resolve(args.kind, args.route, references=args.references, resolution=args.resolution)
            if not found.candidates:
                emit(no_route(found))
                return NO_ROUTE_EXIT
            chosen = found.candidates[0]
            result = {"status": "ok", "kind": args.kind, "route": chosen.route, "label": chosen.label,
                      "order": found.order, "available": [c.route for c in found.candidates], "skipped": found.skipped}
            if chosen.model:
                result["model"] = chosen.model
            if args.kind == "video":
                result["pinsLastFrame"] = resolve(args.kind, args.route, references=args.references,
                                                  resolution=args.resolution, last_frame=True).candidates[0].pin
            emit(result)
            return 0
        found = resolve(args.command, args.route, references=len(references_of(args)),
                        resolution=getattr(args, "resolution", "720p"), last_frame=bool(getattr(args, "last_frame", None)))
        if not found.candidates:
            emit(no_route(found))
            return NO_ROUTE_EXIT
        emit(generate(args, found, transport))
        return 0
    except RouteError as exc:
        error(exc)
    except KeyboardInterrupt:
        error("interrupted; nothing was retried, and job.json and the ledger record the state")
        return 130
    except OSError as exc:
        error(f"{type(exc).__name__}: local input/output failed")
    except Exception as exc:  # noqa: BLE001  (D27: tracebacks are never user-facing)
        error(f"internal error ({type(exc).__name__}: {cli_media.scrub(exc)}); nothing was retried")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
