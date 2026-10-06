#!/usr/bin/env python3
"""One entry point for every generated image or video clip (owner decision 2026-10-06).

  route_media.py image --prompt-file P [--reference R ...] [--size 1024x1024] --out-dir D [--route ROUTE]
  route_media.py video --prompt-file P --reference FIRST.png [--last-frame LAST.png] [--duration 6]
                       [--resolution 720p] --out-dir D [--route ROUTE]
  route_media.py resolve --kind image|video [--route ROUTE] [--references N] [--resolution R]

Route order (--route auto), after the user's preference ("providers": {"order": [...]} in the
user config file, see media_config.py):
  1. api: every provider whose key is configured, in this order. The configured key is the
     owner's consent, so the request is sent at once through generate_media.py.
       images  OpenAI (gpt-image), Google Gemini, xAI (grok-imagine-image), BytePlus ModelArk
               (Seedream), fal.ai (reference edits)
       video   xAI (grok-imagine-video), BytePlus ModelArk (Seedance), fal.ai (Kling, Veo, Luma,
               MiniMax, Wan, Vidu, LTX)
  2. local: the user's own signed-in CLI through cli_media.py (subscription quota, never an API
     key). Images: Codex (codex exec image_gen, references attached), then Grok one-shot
     (image_gen, or image_edit of one reference). Video: Grok in ACP mode (image_to_video; it
     cannot pin a last frame). The first successful run of a CLI version records its proof.
  3. none: prints {"status":"no-route","fallback":"codeart2d"} and exits 3. Use codeart2d only
     then, or when the user asks for code-drawn art.

--route api or local keeps one group; openai, gemini, xai, byteplus, fal, codex-cli, grok-cli or
grok-acp names one route; fal:<endpoint-id> names one fal.ai model. --model picks a model (auto then
keeps the providers that list it), --tier draft|standard|hero picks each provider's model tier,
--provider-option KEY=VALUE passes a request field (PROVIDER:KEY=VALUE for one provider only).

Every API route is gated by its model's capability record (references/capabilities.json): a route
that cannot take the request is skipped before anything runs (too many references, a resolution it
does not render, a first frame it refuses). A duration is snapped to the model's nearest allowed
value; a last frame, keyframes or a transparent background the model cannot take are dropped with a
note (the result says lastFrameUsed). Within a group, a route whose account cannot serve the request
(no key, no credit, no model access, rate limited, unreachable, not signed in, tool missing) passes
it to the next route; any other failure stops. A refused API attempt's folder is kept beside the
output as <out-dir>.failed-<route>.

Success prints one ASCII JSON line and exits 0:
  {"status":"ok","route":"api:openai","artifact":"<out-dir>/generated.png",
   "sha256":"...","estimateUsd":null,...}
A failure prints one "error: ..." line and exits 1; usage errors exit 2. Every call is a line in
<project-dir>/.forge/ledger.jsonl with its estimate; there is no cap unless --budget-usd,
--max-calls or the FORGE_* cap variables set one. --dry-run prints the plan of the route that would
run: no key is used, nothing is sent or written.

Test seam: when FORGE_ROUTE_MEDIA_FAKE names a script, a command is checked as usual and then
handed to it (python <script> <the same arguments>); its output and exit code are returned unchanged.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
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
import media_providers  # noqa: E402

FAKE_ENV = "FORGE_ROUTE_MEDIA_FAKE"
NO_ROUTE_EXIT = 3
FALLBACK = "codeart2d"
API_ORDER = {kind: tuple(f"api:{name}" for name in media_providers.providers_for(kind)) for kind in media_providers.KINDS}
ORDER = {"image": (*API_ORDER["image"], "local:codex-cli", "local:grok-cli"),
         "video": (*API_ORDER["video"], "local:grok-acp")}
ROUTE_CHOICES = {kind: ("auto", "api", "local", *(name.split(":", 1)[1] for name in ORDER[kind]))
                 for kind in media_providers.KINDS}
LOCAL_REFERENCES = {"local:codex-cli": cli_media.MAX_IMAGE_REFERENCES, "local:grok-cli": 1}
MAX_IMAGE_REFERENCES = max(16, *LOCAL_REFERENCES.values())
LOCAL_CLI = {"codex-cli": "codex", "grok-cli": "grok", "grok-acp": "grok"}
VIDEO_RESOLUTIONS = ("480p", "720p", "1080p")
TIERS = media_providers.TIERS
XAI_RATIOS = {"1:1": 1.0, "16:9": 16 / 9, "9:16": 9 / 16, "4:3": 4 / 3, "3:4": 3 / 4, "3:2": 3 / 2, "2:3": 2 / 3}
SIZE = re.compile(r"auto|[1-9]\d{1,4}x[1-9]\d{1,4}")
FAL_ROUTE = re.compile(r"fal:(?P<model>.+)")
# Failures about the route or the account, never about the request: the next route may serve it. An API
# attempt passes it on only when its job.json shows a clean refusal (failed or not_sent): a video job the
# provider accepted before polling failed may still finish and be charged, so it stops for resume instead.
API_NEXT = frozenset({"no_key", "auth", "quota", "rate_limit", "entitlement", "not_sent"})
CLEAN_REFUSALS = frozenset({"failed", "not_sent"})
LOCAL_NEXT = frozenset({"NOT_INSTALLED", "SPAWN_FAILED", "AUTH_REQUIRED", "RATE_LIMIT", "TOOL_UNAVAILABLE",
                        "GENERATION_FAILED"})
DUMMY_IMAGE = ({"size": [1024, 1024], "mime": "image/png", "sha256": "0" * 64, "bytes": 0}, b"")


class RouteError(Exception):
    """A failure that ends the command: ``error: <message>``, exit 1."""


@dataclass
class Candidate:
    """One route that can take the request, with what it will actually send."""

    route: str
    model: str | None = None
    quality: str | None = None
    pin: bool = False
    keyframes: bool = False
    transparent: bool = False
    duration: int | None = None
    notes: list = field(default_factory=list)

    @property
    def family(self) -> str:
        return self.route.split(":", 1)[0]

    @property
    def target(self) -> str:
        return self.route.split(":", 1)[1]

    @property
    def note(self) -> str | None:
        return self.notes[0] if self.notes else None

    @property
    def label(self) -> str:
        if self.family == "api":
            return f"{media_providers.adapter(self.target).label} ({self.model})"
        return forge_doctor.ROUTE_LABEL[self.target]


@dataclass
class Resolution:
    kind: str
    requested: str
    order: list
    candidates: list
    skipped: list


@dataclass
class Wanted:
    """What the request asks for, as resolve() checks it against each route."""

    references: list = field(default_factory=list)  # [(meta, bytes)]
    last_frame: tuple | None = None
    keyframes: list = field(default_factory=list)  # [((meta, bytes), seconds)]
    size: str | None = "1024x1024"
    resolution: str = "720p"
    duration: int = 6
    transparent: bool = False
    model: str | None = None
    tier: str = "standard"
    options: dict = field(default_factory=dict)  # {provider or "*": {key: value}}


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


# --------------------------------------------------------------------------- arguments

def route_value(text: str) -> str:
    """argparse type of --route: a fixed name or fal:<endpoint-id>."""
    text = str(text).strip()
    if text in set(ROUTE_CHOICES["image"]) | set(ROUTE_CHOICES["video"]):
        return text
    match = FAL_ROUTE.fullmatch(text)
    if match and media_providers.FAL_ID.fullmatch(match.group("model")) and ".." not in text:
        return text
    raise argparse.ArgumentTypeError(f"invalid route {text!r} (choose auto, api, local, a provider, a local CLI route "
                                     "or fal:<endpoint-id>)")


def provider_options(items) -> dict:
    """--provider-option [PROVIDER:]KEY=VALUE -> {provider or "*": {key: value}}."""
    found = {}
    for item in items or ():
        scope, sep, rest = str(item).partition(":")
        if sep and scope in media_providers.ORDER and "=" in rest and "=" not in scope:
            target, item = scope, rest
        else:
            target = "*"
        try:
            found.setdefault(target, {}).update(generate_media.parse_provider_options([item]))
        except generate_media.MediaError as exc:
            raise RouteError(str(exc)) from None
    return found


def options_for(wanted: Wanted, provider: str) -> dict:
    return {**wanted.options.get("*", {}), **wanted.options.get(provider, {})}


# --------------------------------------------------------------------------- resolution

def apply_preference(order: list, preferred: list) -> list:
    """The routes the user named first (in their order), then the rest in the owner's order."""
    first = [name for want in preferred for name in order if name.split(":", 1)[1] == want]
    return first + [name for name in order if name not in first]


def requested_routes(kind: str, route: str, preferred: list | None = None) -> list:
    order = apply_preference(list(ORDER[kind]), preferred or [])
    if route == "auto":
        return order
    if route in ("api", "local"):
        return [name for name in order if name.startswith(route + ":")]
    if FAL_ROUTE.fullmatch(route):
        return ["api:fal"] if "api:fal" in ORDER[kind] else []
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


def _api_request(kind, provider, candidate, wanted) -> media_providers.MediaRequest:
    """The request generate_media.py will build for this candidate, for its adapter's check()."""
    size = resolution = aspect = None
    if kind == "image":
        if provider == "xai":
            flags = xai_image_options(wanted.size, len(wanted.references))
            resolution = flags[1] if flags else None
            aspect = flags[3] if len(flags) > 2 else None
        else:
            size = wanted.size
    else:
        resolution = wanted.resolution
    return media_providers.MediaRequest(
        kind=kind, model=candidate.model, prompt="resolve", references=list(wanted.references) or [],
        last_frame=wanted.last_frame if candidate.pin else None,
        keyframes=list(wanted.keyframes) if candidate.keyframes else [], size=size, quality=candidate.quality,
        transparent=candidate.transparent, resolution=resolution, aspect_ratio=aspect,
        duration=candidate.duration if kind == "video" else None, options=options_for(wanted, provider))


def fit_api(kind: str, candidate: Candidate, wanted: Wanted, explicit: bool) -> str | None:
    """Adapt the candidate to what its model can do (snap the duration; drop a last frame, keyframes or a
    transparent background with a note), then gate it with the adapter's own check. Returns why the
    route cannot take the request, or None."""
    provider = candidate.target
    item = media_providers.adapter(provider)
    record = item.model_record(candidate.model)
    if record is None and not (explicit and item.record.get("unknownModels") == kind):
        return f"{candidate.model} is not a verified {item.label} {kind} model (references/capabilities.json)"
    if kind == "image":
        candidate.transparent = wanted.transparent
        if wanted.transparent and record is not None and record["image"].get("transparent_bg") != "native":
            candidate.transparent = False
            candidate.notes.append(f"{candidate.model} has no native transparent background: key the backdrop")
    else:
        video = record["video"]
        candidate.duration = media_providers.snap_duration(wanted.duration, video)
        if candidate.duration != wanted.duration:
            candidate.notes.append(f"{candidate.model} renders {candidate.duration} s, the nearest allowed length to "
                                   f"{wanted.duration} s")
        pins = record.get("pin_resolutions")
        if wanted.last_frame is not None:
            candidate.pin = bool(video.get("last_frame")) and (not pins or wanted.resolution in pins)
            if not candidate.pin:
                candidate.notes.append(f"the last frame is not pinned: {candidate.model} at {wanted.resolution} "
                                       "cannot take one")
        elif (record.get("fal") or {}).get("last_required"):
            return f"{candidate.model} needs a last frame (--last-frame; the still itself pins a loop)"
        if wanted.keyframes:
            candidate.keyframes = bool(record.get("keyframes_max")) and (not pins or wanted.resolution in pins)
            if not candidate.keyframes:
                candidate.notes.append(f"the keyframes are not sent: {candidate.model} at {wanted.resolution} "
                                       "takes none")
    try:
        candidate.notes += item.check(_api_request(kind, provider, candidate, wanted))
    except media_providers.MediaError as exc:
        return str(exc)
    return None


def unsupported_local(kind: str, candidate: Candidate, wanted: Wanted) -> str | None:
    if kind == "image":
        limit = LOCAL_REFERENCES[candidate.route]
        if len(wanted.references) > limit:
            return f"takes at most {limit} reference image{'s' if limit > 1 else ''}"
        return None
    if candidate.route == "local:grok-acp" and wanted.resolution not in cli_media.VIDEO_RESOLUTIONS:
        return "Grok (local CLI) image_to_video renders 480p or 720p"
    return None


def resolve(kind: str, route: str = "auto", *, references: int | None = None, resolution: str = "720p",
            last_frame: bool = False, settings: dict | None = None, wanted: Wanted | None = None) -> Resolution:
    """The candidates for one request, in order, and why the others were skipped. Spawns nothing:
    a local route counts when its CLI's native executable is installed. ``wanted`` carries the real
    request; without it, ``references`` dummy references, ``resolution`` and ``last_frame`` describe one."""
    problem = None
    if settings is None:
        settings, problem = media_config.load_config()
    if wanted is None:
        count = (0 if kind == "image" else 1) if references is None else references
        wanted = Wanted(references=[DUMMY_IMAGE] * count, resolution=resolution,
                        last_frame=DUMMY_IMAGE if last_frame else None)
    preferred, problems = media_config.provider_order(kind, settings)
    order = requested_routes(kind, route, preferred)
    candidates, skipped, clis = [], [], {}
    if problem and any(name.startswith("api:") for name in order):
        skipped.append(f"user config file ignored: {problem}")
    skipped += problems
    fal_model = FAL_ROUTE.fullmatch(route)
    for name in order:
        family, target = name.split(":", 1)
        if family == "api":
            if not media_config.api_key(target, settings):
                skipped.append(f"{name}: no {media_config.PROVIDERS[target]} in the environment or the user config file")
                continue
            explicit = route == target or bool(fal_model)
            if fal_model:
                model, quality = fal_model.group("model"), None
            elif wanted.model:
                model, quality = wanted.model, None
                if not explicit and wanted.model not in media_providers.adapter(target).models():
                    skipped.append(f"{name}: does not list the model {wanted.model}")
                    continue
            else:
                model, quality = media_providers.default_model(target, kind, wanted.tier, settings)
            candidate = Candidate(name, model=model, quality=quality)
            reason = fit_api(kind, candidate, wanted, explicit or bool(wanted.model))
        else:
            if wanted.model or fal_model:
                skipped.append(f"{name}: a local CLI chooses its own model (--model applies to the API routes)")
                continue
            cli = LOCAL_CLI[target]
            if cli not in clis:
                clis[cli] = cli_media.resolve_route_cli(cli)
            info = clis[cli].info
            if info.path is None or not clis[cli].prefix:
                skipped.append(f"{name}: {cli_media.CLI_NAME[cli]} is not installed ({info.problem or 'not found'})")
                continue
            candidate = Candidate(name, duration=wanted.duration)
            reason = unsupported_local(kind, candidate, wanted)
            if not reason and kind == "video" and wanted.last_frame is not None:
                candidate.notes.append("the last frame is not pinned: Grok (local CLI) image_to_video takes the first "
                                       "frame only")
            if not reason and kind == "video" and wanted.keyframes:
                candidate.notes.append("the keyframes are not sent: Grok (local CLI) takes the first frame only")
        if reason:
            skipped.append(f"{name}: {reason}")
            continue
        candidates.append(candidate)
    return Resolution(kind, route, list(order), candidates, skipped)


def no_route(resolution: Resolution) -> dict:
    return {"status": "no-route", "fallback": FALLBACK, "kind": resolution.kind, "requested": resolution.requested,
            "skipped": resolution.skipped}


# --------------------------------------------------------------------------- inputs

def check_inputs(args: argparse.Namespace) -> list:
    """The checks every command passes before any route (or the test fake) sees it. Returns warnings."""
    warnings = []
    try:
        keyframes = [generate_media.parse_keyframe(item)[0] for item in getattr(args, "keyframe", None) or []] \
            if args.command == "video" else []
    except generate_media.MediaError as exc:
        raise RouteError(str(exc)) from None
    for label, value in (("prompt file", args.prompt_file), *(("reference image", r) for r in references_of(args)),
                         ("last frame", getattr(args, "last_frame", None)), *(("keyframe", k) for k in keyframes)):
        if value is not None and not Path(value).is_file():
            raise RouteError(f"the {label} {Path(value).name} does not exist")
    if os.path.lexists(args.out_dir):
        if not args.dry_run:
            raise RouteError(f"{Path(args.out_dir).name} already exists; choose a new --out-dir")
        warnings.append("the output folder already exists; a real run will refuse it")
    if args.model and not (media_providers.MODEL_ID.fullmatch(args.model) or media_providers.FAL_ID.fullmatch(args.model)):
        raise RouteError("--model must be a provider model id (fal.ai: its endpoint id)")
    args.options = provider_options(args.provider_option)
    if args.command == "image":
        if args.size is not None and not SIZE.fullmatch(args.size):
            raise RouteError("--size must be auto or WIDTHxHEIGHT, for example 1024x1024")
        if len(args.reference) > MAX_IMAGE_REFERENCES:
            raise RouteError(f"at most {MAX_IMAGE_REFERENCES} reference images")
    else:
        if not 1 <= args.duration <= 15:
            raise RouteError("--duration must be 1..15 seconds")
        if len(keyframes) > 4:
            raise RouteError("at most 4 keyframes")
    return warnings


def references_of(args: argparse.Namespace) -> list:
    if args.command == "image":
        return list(args.reference)
    return [args.reference] if getattr(args, "reference", None) else []


def wanted_of(args: argparse.Namespace) -> Wanted:
    """The request with its images read (sizes, hashes, bytes), for the capability checks."""
    try:
        refs = [generate_media.inspect_image(path) for path in references_of(args)]
        last = generate_media.inspect_image(args.last_frame) if getattr(args, "last_frame", None) else None
        keyframes = []
        for item in getattr(args, "keyframe", None) or []:
            path, seconds = generate_media.parse_keyframe(item)
            keyframes.append((generate_media.inspect_image(path), seconds))
    except generate_media.MediaError as exc:
        raise RouteError(str(exc)) from None
    video = args.command == "video"
    return Wanted(references=refs, last_frame=last, keyframes=keyframes,
                  size=None if video else args.size, resolution=args.resolution if video else "720p",
                  duration=args.duration if video else 6, transparent=bool(getattr(args, "transparent", False)),
                  model=args.model, tier=args.tier, options=args.options)


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
    provider = candidate.target
    argv = [args.command, "--provider", provider, "--model", candidate.model, "--prompt-file",
            str(args.prompt_file), "--out-dir", str(args.out_dir), *_common(args), "--allow-duplicate"]
    if args.budget_usd is not None:
        argv += ["--budget-usd", str(args.budget_usd)]
    if args.command == "image":
        for reference in args.reference:
            argv += ["--reference", str(reference)]
        if provider == "xai":
            argv += xai_image_options(args.size, len(args.reference))
        elif args.size:
            argv += ["--size", args.size]
        if candidate.quality:
            argv += ["--quality", candidate.quality]
        if candidate.transparent:
            argv.append("--transparent")
    else:
        argv += ["--reference", str(args.reference), "--duration", str(candidate.duration or args.duration),
                 "--resolution", args.resolution]
        if candidate.pin:
            argv += ["--last-frame", str(args.last_frame)]
        if candidate.keyframes:
            for item in args.keyframe:
                argv += ["--keyframe", item]
    for key, value in options_for(args.wanted, provider).items():  # JSON keeps "5" a string and 5 a number
        argv += ["--provider-option", f"{key}={json.dumps(value)}"]
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
    extra = {k: job[k] for k in ("crop",) if k in job}
    if args.dry_run:
        return {"estimateUsd": job["estimate"]["usd"], "estimate": job["estimate"]["basis"],
                "warnings": job.get("warnings", []), **extra}
    artifact = job["artifact"]
    notes = list(job.get("notes", []))
    audio = job.get("audio")
    if isinstance(audio, dict) and audio.get("reason"):
        notes.append("the clip's audio could not be removed: " + audio["reason"])
    return {"artifact": str(Path(args.out_dir) / artifact["path"]), "sha256": artifact["sha256"],
            "estimateUsd": job["estimate"]["usd"], "job": str(Path(args.out_dir) / "job.json"), "notes": notes,
            **extra}


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
            result.update({k: outcome[k] for k in ("job", "verifiedBefore", "estimate", "command", "crop")
                           if k in outcome})
            if args.command == "video":
                result["lastFrameUsed"] = candidate.pin
                if candidate.duration and candidate.duration != args.duration:
                    result["duration"] = candidate.duration
            notes = [*candidate.notes, *(n for n in outcome.get("notes", []) if n not in candidate.notes)]
            warnings = [*args.input_warnings, *(w for w in outcome.get("warnings", []) if w not in notes)]
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

def _route_help(kind):
    return ("auto (default): " + " > ".join(ORDER[kind]) + " (after providers.order in the user config file); "
            "api or local: one group; " + ", ".join(ROUTE_CHOICES[kind][3:]) + ": one route; fal:<endpoint-id>: "
            "one fal.ai model")


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
                                      "them; OpenAI takes up to 16, Gemini and fal.ai Nano Banana 14, BytePlus 10, "
                                      "Codex 8, xAI 5, Grok (local CLI) 1")
            command.add_argument("--size", default="1024x1024",
                                 help="WIDTHxHEIGHT or auto (default 1024x1024); the local CLIs take the size from the "
                                      "prompt, so state it there too")
            command.add_argument("--transparent", action="store_true",
                                 help="ask for a native transparent background where the model has one (OpenAI GPT "
                                      "Image); elsewhere a note says to key the backdrop")
        else:
            command.add_argument("--reference", required=True, help="the first frame: the approved still")
            command.add_argument("--last-frame", help="pin the end frame (same canvas as --reference) where the route "
                                                      "allows it; the result says lastFrameUsed")
            command.add_argument("--keyframe", action="append", default=[], metavar="PATH@SECONDS",
                                 help="an intermediate frame at a time, up to 4 (xAI grok-imagine-video-1.5 at 480p "
                                      "or 720p; other routes drop them with a note)")
            command.add_argument("--duration", type=int, default=6, help="seconds, 1..15 (default 6); snapped to the "
                                                                         "nearest length the model renders")
            command.add_argument("--resolution", choices=VIDEO_RESOLUTIONS, default="720p")
        command.add_argument("--out-dir", required=True, help="new folder for generated.<ext>, job.json and prompt.txt")
        command.add_argument("--route", type=route_value, default="auto", metavar="ROUTE", help=_route_help(kind))
        command.add_argument("--model", help="the API model to use (fal.ai: its endpoint id); with auto, only the "
                                             "providers that list it are tried")
        command.add_argument("--tier", choices=TIERS, default="standard",
                             help="each provider's draft, standard (default) or hero model (capabilities.json tiers; "
                                  "the user config file can override each slot)")
        command.add_argument("--provider-option", action="append", default=[], metavar="[PROVIDER:]KEY=VALUE",
                             help="extra request field passed as is (repeatable); PROVIDER: limits it to one provider")
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
    resolve_cmd.add_argument("--kind", required=True, choices=media_providers.KINDS)
    resolve_cmd.add_argument("--route", type=route_value, default="auto", metavar="ROUTE",
                             help="as for image and video (auto, api, local, a route or fal:<endpoint-id>)")
    resolve_cmd.add_argument("--references", type=int, help="reference images of the request (default: 0 for an "
                                                            "image, 1 for a video)")
    resolve_cmd.add_argument("--resolution", choices=VIDEO_RESOLUTIONS, default="720p", help="video resolution")
    resolve_cmd.add_argument("--model", help="the API model to use")
    resolve_cmd.add_argument("--tier", choices=TIERS, default="standard", help="draft, standard (default) or hero")
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


def _known_model(kind: str, args: argparse.Namespace) -> None:
    """An explicitly named model must be one the route can send: a model is a usage error, never a reason
    to answer no-route (which sends the caller to code-drawn art)."""
    match = FAL_ROUTE.fullmatch(args.route)
    model = match.group("model") if match else args.model
    if not model:
        return
    if match:
        if args.model and args.model != model:
            raise RouteError(f"--route {args.route} already names the model; drop --model {args.model}")
        provider = "fal"
    elif args.route in ("auto", "api", "local"):
        if not any(record.get("kind") == kind for _, record in media_providers.find_model(model)):
            raise RouteError(f"--model {model} is not a verified {kind} model (references/capabilities.json); name "
                             "its provider with --route to send an unverified image model")
        return
    elif args.route in media_providers.ORDER:
        provider = args.route
    else:
        raise RouteError(f"--model applies to the API routes; {args.route} is a local CLI that chooses its own model")
    item = media_providers.adapter(provider)
    record = item.model_record(model)
    if record is not None and record.get("kind") != kind:
        raise RouteError(f"{model} makes {record.get('kind')}s, not {kind}s")
    if record is None and item.record.get("unknownModels") != kind:
        raise RouteError(f"{model} is not a verified {item.label} {kind} model (references/capabilities.json lists "
                         f"{', '.join(item.models(kind)) or 'none'})")


def main(argv: list | None = None, transport=None) -> int:
    """Exit 0 on success (and for resolve and --dry-run), 1 on failure (one ``error:`` line), 2 on a
    usage error, 3 when no route exists, 130 on Ctrl+C. ``transport`` replaces the API's HTTPS
    transport in tests."""
    media_ledger._local_utf8_stdio()
    raw = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(raw)
    kind = args.kind if args.command == "resolve" else args.command
    if args.route not in ROUTE_CHOICES[kind] and not (FAL_ROUTE.fullmatch(args.route) and "api:fal" in ORDER[kind]):
        parser.error(f"--route {args.route} is not a {kind} route (choose from {', '.join(ROUTE_CHOICES[kind])} "
                     "or fal:<endpoint-id>)")
    try:
        args.input_warnings = check_inputs(args) if args.command != "resolve" else []
        fake = os.environ.get(FAKE_ENV, "").strip()
        if fake:
            return delegate(fake, raw)
        _known_model(kind, args)
        if args.command == "resolve":
            count = (0 if kind == "image" else 1) if args.references is None else args.references
            base = dict(references=[DUMMY_IMAGE] * count, resolution=args.resolution, model=args.model, tier=args.tier)
            found = resolve(kind, args.route, wanted=Wanted(**base))
            if not found.candidates:
                emit(no_route(found))
                return NO_ROUTE_EXIT
            chosen = found.candidates[0]
            result = {"status": "ok", "kind": kind, "route": chosen.route, "label": chosen.label,
                      "order": found.order, "available": [c.route for c in found.candidates], "skipped": found.skipped}
            if chosen.model:
                result["model"] = chosen.model
            if kind == "video":
                pinned = resolve(kind, args.route, wanted=Wanted(**base, last_frame=DUMMY_IMAGE))
                result["pinsLastFrame"] = bool(pinned.candidates) and pinned.candidates[0].pin
            emit(result)
            return 0
        args.wanted = wanted_of(args)
        found = resolve(args.command, args.route, wanted=args.wanted)
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
