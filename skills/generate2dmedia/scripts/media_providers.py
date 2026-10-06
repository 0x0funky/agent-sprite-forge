#!/usr/bin/env python3
"""Provider adapters behind one interface: the image and video APIs ASF supports (owner request 2026-10-06).

  python media_providers.py list                       providers, key variables, models, tiers
  python media_providers.py show PROVIDER [MODEL]      one capability record as JSON

Providers, each with its own key (never another provider's): OpenAI images (OPENAI_API_KEY);
Google Gemini images (GOOGLE_API_KEY or GEMINI_API_KEY; GOOGLE_API_KEY wins); xAI images and
video (XAI_API_KEY); BytePlus ModelArk, Seedream images and Seedance video (ARK_API_KEY); fal.ai,
an aggregator with curated image edits and image-to-video models (FAL_KEY).

Every model has a capability record in references/capabilities.json, stamped with verifiedAt and
the vendor's doc URLs. Every adapter implements the same steps:
  check        capability gating: refuse what the model's record does not allow (nothing is sent)
  build        the HTTP request: endpoint, body, the options that identify it, how it is priced
  read_submit  the answer to the paid POST: the media itself, or an asynchronous job id
  status       one poll of a job: pending, done (media to download) or a terminal failure
  result       fal only: the result request after a job completes
  classify     provider errors onto ASF's outcome codes (auth, quota, rate_limit, entitlement,
               moderation, invalid_request, provider_error, ...)
generate_media.py owns the network, the keys, job.json and the ledger; this module only shapes
requests and reads answers, so every adapter is tested offline. Stdlib plus Pillow (padding a still
onto a 16:9 key-colour canvas for models without 1:1, with the crop recorded for later steps).
"""
from __future__ import annotations

import argparse
import base64
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
import io
import json
import math
from pathlib import Path
import re
import sys
from urllib import parse
import uuid

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import media_config  # noqa: E402  (sibling in this skill's scripts/)

CAPABILITIES_PATH = HERE.parent / "references" / "capabilities.json"
CAPABILITIES_SCHEMA = "generate2dmedia.capabilities.v1"
ORDER = ("openai", "gemini", "xai", "byteplus", "fal")
KINDS = ("image", "video")
TIERS = ("draft", "standard", "hero")
VIDEO_RESOLUTIONS = ("480p", "720p", "1080p")
SIZE = re.compile(r"auto|[1-9]\d{1,4}x[1-9]\d{1,4}")
MODEL_ID = re.compile(r"[A-Za-z0-9_.:-]{1,120}")
FAL_ID = re.compile(r"[a-z0-9][a-z0-9_.-]{0,60}(?:/[a-z0-9][a-z0-9_.-]{0,60}){1,6}")
OPTION_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}(?:\.[A-Za-z_][A-Za-z0-9_]{0,63}){0,3}")
REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,200}")
KEY_COLOUR = (255, 0, 255)
PAD_TOKEN = "forge-upload-"
TODAY: date | None = None  # tests pin the date that retirement checks compare against


class MediaError(Exception):
    """Locally authored, secret-free failure.

    code is the receipt outcomeCode. sent says whether a paid POST may have
    reached the provider: False (certainly not), True (the provider answered),
    None (unknown). provider holds whitelisted, scrubbed provider error fields.
    """

    def __init__(self, message, *, code="error", sent=None, provider=None):
        super().__init__(message)
        self.code, self.sent, self.provider = code, sent, provider


class JobFailure(MediaError):
    """A provider job reached a terminal failure: status is failed or expired."""

    def __init__(self, message, *, status, code, provider=None):
        super().__init__(message, code=code, sent=True, provider=provider)
        self.status = status


def refuse(message):
    return MediaError(message, code="invalid_input", sent=False)


# --------------------------------------------------------------------------- capabilities

_CACHE = {}


def capabilities(path=None) -> dict:
    """references/capabilities.json (cached), or another copy for tests."""
    path = Path(path) if path is not None else CAPABILITIES_PATH
    key = str(path)
    if key not in _CACHE:
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise MediaError(f"Cannot read {path.name} ({type(exc).__name__})", code="invalid_input",
                             sent=False) from None
        if not isinstance(data, dict) or data.get("schema") != CAPABILITIES_SCHEMA \
                or not isinstance(data.get("providers"), dict):
            raise MediaError(f"{path.name} is not a {CAPABILITIES_SCHEMA} file", code="invalid_input", sent=False)
        _CACHE[key] = data
    return _CACHE[key]


def today() -> date:
    return TODAY or date.today()


def retirement(provider, model):
    """(problem, warning): a problem when the model is dropped by ASF or past its shutdown date,
    a warning when it shuts down later."""
    for row in capabilities().get("retired", []):
        if row.get("provider") == provider and row.get("model") == model:
            hint = f"; use {row['use']}" if row.get("use") else ""
            shutdown = date.fromisoformat(row["shutdown"])
            if today() >= shutdown:
                return f"{model} was shut down on {row['shutdown']}{hint}", None
            if row.get("dropped"):
                return f"{model} shuts down on {row['shutdown']} and ASF no longer sends it{hint}", None
            return None, f"{model} shuts down on {row['shutdown']}{hint}"
    return None, None


def find_model(model):
    """[(provider, record)] of every provider whose capabilities list this model id."""
    return [(name, adapter(name).models().get(model)) for name in ORDER if model in adapter(name).models()]


# --------------------------------------------------------------------------- request and result shapes

@dataclass
class MediaRequest:
    """One provider-neutral request. Images are (meta, bytes) pairs from generate_media.inspect_image."""

    kind: str
    model: str
    prompt: str
    references: list = field(default_factory=list)
    last_frame: tuple | None = None
    keyframes: list = field(default_factory=list)  # [((meta, bytes), seconds)]
    size: str | None = None
    quality: str | None = None
    transparent: bool = False
    resolution: str | None = None
    aspect_ratio: str | None = None
    duration: int | None = None
    upload_url: str | None = None
    options: dict = field(default_factory=dict)


@dataclass
class DeferredBody:
    """A JSON body whose image fields are filled in at execution, after the inputs are uploaded (fal CDN).
    uploads: [(placeholder, meta, bytes, file name)]."""

    fields: dict
    uploads: list

    def render(self, urls: dict) -> bytes:
        def fill(value):
            if isinstance(value, str) and value.startswith(PAD_TOKEN):
                return urls[value]
            if isinstance(value, list):
                return [fill(v) for v in value]
            if isinstance(value, dict):
                return {k: fill(v) for k, v in value.items()}
            return value
        return json.dumps(fill(self.fields)).encode("utf-8")


@dataclass
class Built:
    endpoint: str
    body: object  # bytes or DeferredBody
    content_type: str
    options: dict
    pricing: dict
    extra: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)


@dataclass
class MediaRef:
    """Finished media: the bytes, or a URL to download at once (auth: the key may go to that host)."""

    data: bytes | None = None
    url: str | None = None
    auth: bool = False
    returned_model: str | None = None
    usage: dict | None = None
    cost_usd: float | None = None
    duration: float | None = None


@dataclass
class JobRef:
    """An accepted asynchronous job: its id and the fields job.json keeps to resume it."""

    id: str
    extra: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- helpers

def data_uri(meta, data) -> str:
    return "data:" + meta["mime"] + ";base64," + base64.b64encode(data).decode("ascii")


def ratio_value(name):
    try:
        width, height = (float(part) for part in name.split(":"))
    except (AttributeError, ValueError):
        return None
    return width / height if width > 0 and height > 0 else None


def nearest_ratio(width, height, names):
    """The listed aspect ratio (e.g. "16:9") closest to width/height, by log distance."""
    target = width / height
    scored = [(abs(math.log(target / ratio_value(n))), n) for n in names if ratio_value(n)]
    return min(scored)[1] if scored else None


def exact_ratio(width, height, names, tolerance=0.01):
    name = nearest_ratio(width, height, names)
    return name if name and abs(math.log(width / height / ratio_value(name))) <= tolerance else None


def parse_size(size):
    if not size or size == "auto":
        return None
    width, height = (int(part) for part in size.split("x"))
    return width, height


def durations(video) -> list:
    spec = (video or {}).get("durations") or {}
    if "values" in spec:
        return sorted(int(v) for v in spec["values"])
    return list(range(int(spec.get("min", 1)), int(spec.get("max", 15)) + 1, int(spec.get("step", 1))))


def snap_duration(seconds, video):
    """The allowed duration nearest to ``seconds`` (a tie takes the longer one)."""
    allowed = durations(video)
    return min(allowed, key=lambda value: (abs(value - seconds), -value)) if allowed else seconds


def apply_options(fields, options, reserved):
    """Merge --provider-option values (dotted keys nest) into the request; reserved names are refused."""
    for key, value in options.items():
        if not OPTION_KEY.fullmatch(key):
            raise refuse(f"--provider-option {key!r}: use KEY=VALUE with a letter-and-digit key (dots nest)")
        if key.split(".")[0] in reserved:
            raise refuse(f"--provider-option {key} is set by ASF itself; it cannot be overridden")
        target, parts = fields, key.split(".")
        for part in parts[:-1]:
            if not isinstance(target.get(part, {}), dict):
                raise refuse(f"--provider-option {key} would replace a non-object field")
            target = target.setdefault(part, {})
        target[parts[-1]] = value
    return fields


def strip_images(value):
    """A copy of a request body without image payloads, for the options that identify a request."""
    if isinstance(value, dict):
        return {k: strip_images(v) for k, v in value.items()}
    if isinstance(value, list):
        return [strip_images(v) for v in value]
    if isinstance(value, str) and (value.startswith("data:") or value.startswith(PAD_TOKEN)):
        return "<image>"
    return value


def multipart(fields, refs):
    boundary = "forge-" + uuid.uuid4().hex
    chunks = []
    for name, value in fields.items():
        text = value if isinstance(value, str) else json.dumps(value)
        chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{text}\r\n').encode())
    for i, (meta, data) in enumerate(refs):
        suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[meta["mime"]]
        # Synthetic filename avoids path disclosure and multipart header injection.
        chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="image[]"; filename="reference-{i}{suffix}"\r\n'
                       f'Content-Type: {meta["mime"]}\r\n\r\n').encode())
        chunks.extend([data, b"\r\n"])
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), "multipart/form-data; boundary=" + boundary


def _text(fields):
    return " ".join(str(fields.get(k, "")) for k in ("code", "type", "message")).lower()


def _int_usage(mapping, names):
    if not isinstance(mapping, dict):
        return None
    found = {out: mapping[src] for src, out in names if isinstance(mapping.get(src), int)
             and not isinstance(mapping[src], bool) and mapping[src] >= 0}
    return found or None


def _b64(value, what):
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        raise MediaError(f"Invalid base64 {what} in the response", code="bad_response") from None


def _https(url):
    parts = parse.urlsplit(url) if isinstance(url, str) else None
    return bool(parts and parts.scheme == "https" and parts.hostname and not parts.username and not parts.password)


def key_colour(image):
    """The flat backdrop colour of a still: the dominant opaque border colour, else #FF00FF."""
    rgba = image.convert("RGBA")
    width, height = rgba.size
    pixels = rgba.load()
    border = [pixels[x, y] for x in range(width) for y in (0, height - 1)] + \
             [pixels[x, y] for y in range(height) for x in (0, width - 1)]
    opaque = [p[:3] for p in border if p[3] == 255]
    if opaque:
        colour, count = Counter(opaque).most_common(1)[0]
        if count >= 0.6 * len(border):
            return colour
    return KEY_COLOUR


def pad_to_aspect(images, aspect, fill=None):
    """Centre every image (all the same size) on one canvas of ``aspect`` (e.g. "16:9") filled with the
    key colour. Returns ([(meta, png bytes)], transform, crop): transform records the canvas and where
    the source sits in pixels; crop is the same box as fractions of a frame, so a later step can crop
    any output resolution back to the source aspect."""
    from PIL import Image  # noqa: PLC0415  (Pillow is only needed to pad)

    meta0, data0 = images[0]
    width, height = meta0["size"]
    ratio = ratio_value(aspect)
    if width / height < ratio:
        canvas = (int(math.ceil(height * ratio / 2) * 2), height)
    else:
        canvas = (width, int(math.ceil(width / ratio / 2) * 2))
    offset = ((canvas[0] - width) // 2, (canvas[1] - height) // 2)
    with Image.open(io.BytesIO(data0)) as first:
        colour = fill or key_colour(first)
    padded = []
    for meta, data in images:
        with Image.open(io.BytesIO(data)) as source:
            frame = source.convert("RGBA")
        board = Image.new("RGBA", canvas, (*colour, 255))
        board.alpha_composite(frame, offset)
        output = io.BytesIO()
        board.convert("RGB").save(output, "PNG")
        content = output.getvalue()
        padded.append(({**meta, "size": list(canvas), "mime": "image/png", "bytes": len(content),
                        "padded": True}, content))
    transform = {"type": "pad", "aspect": aspect, "source": [width, height], "canvas": list(canvas),
                 "offset": list(offset), "fill": "#%02X%02X%02X" % colour}
    crop = {"x": round(offset[0] / canvas[0], 6), "y": round(offset[1] / canvas[1], 6),
            "width": round(width / canvas[0], 6), "height": round(height / canvas[1], 6),
            "note": "fractions of each output frame: crop every frame to this box to return to the source aspect"}
    return padded, transform, crop


# --------------------------------------------------------------------------- the adapter interface

class Adapter:
    """One provider. Subclasses implement build, read_submit and (for async jobs) status."""

    name = ""
    auth_header = "Authorization"
    auth_scheme = "Bearer"
    reserved = frozenset({"model", "prompt"})

    def __init__(self, record):
        self.record = record

    @property
    def label(self):
        return self.record["label"]

    @property
    def api_base(self):
        return self.record["apiBase"]

    @property
    def kinds(self):
        return tuple(self.record["kinds"])

    @property
    def key_env(self):
        return media_config.PROVIDERS[self.name]

    def auth_meta(self):
        return {"authHeader": self.auth_header, "authScheme": self.auth_scheme}

    def models(self, kind=None):
        found = self.record.get("models", {})
        return {k: v for k, v in found.items() if kind is None or v.get("kind") == kind}

    def model_record(self, model):
        return self.record.get("models", {}).get(model)

    def tier(self, kind, tier="standard"):
        """{"model", "quality"?} of a tier (draft, standard, hero) for a kind, or None."""
        return ((self.record.get("tiers") or {}).get(kind) or {}).get(tier)

    def is_async(self, kind, model):
        record = self.model_record(model)
        if record is not None:
            return record.get("async") == "poll"
        return kind == "video"

    # -- capability gating ---------------------------------------------------------------------

    def check(self, req) -> list:
        """Refuse (MediaError invalid_input, nothing sent) what the model cannot do; return warnings."""
        if req.kind not in self.kinds:
            raise refuse(f"{self.label} has no {req.kind} route in ASF")
        problem, warning = retirement(self.name, req.model)
        if problem:
            raise refuse(problem)
        warnings = [warning] if warning else []
        record = self.model_record(req.model)
        if record is None:
            if self.record.get("unknownModels") != req.kind:
                known = ", ".join(self.models(req.kind)) or "none"
                raise refuse(f"Unsupported {req.kind} model {req.model} for {self.label}; verify its capabilities "
                             f"before adding it (known: {known})")
            warnings.append(f"{req.model} has no capability record (verified {self.record['verifiedAt']}); "
                            "its limits are not checked here")
        elif record.get("kind") != req.kind:
            raise refuse(f"{req.model} makes {record.get('kind')}s, not {req.kind}s")
        if req.kind == "image":
            self._check_image(req, record, warnings)
        else:
            self._check_video(req, record, warnings)
        return warnings

    def _check_image(self, req, record, warnings):
        image = (record or {}).get("image") or {}
        limit = image.get("refs_max")
        if limit is not None and len(req.references) > limit:
            raise refuse(f"{req.model} takes at most {limit} reference image{'s' if limit != 1 else ''}")
        if req.transparent and record is not None and image.get("transparent_bg") != "native":
            raise refuse(f"{req.model} has no native transparent background; prompt a flat key colour and key it")

    def _check_video(self, req, record, warnings):
        video = record["video"]
        if len(req.references) != 1:
            raise refuse("Video requires one approved --reference image (the first frame)")
        allowed = durations(video)
        if req.duration not in allowed:
            span = f"{allowed[0]}..{allowed[-1]}" if allowed == list(range(allowed[0], allowed[-1] + 1)) else \
                ", ".join(str(v) for v in allowed)
            raise refuse(f"{req.model} renders {span} seconds; --duration {req.duration} is not one of them")
        resolutions = video.get("resolutions")
        if resolutions != "any" and req.resolution not in resolutions:
            raise refuse(f"{req.model} renders {', '.join(resolutions)}; not {req.resolution}")
        first = req.references[0][0]
        if req.last_frame is not None:
            if not video.get("last_frame"):
                raise refuse(f"{req.model} cannot pin a last frame; drop --last-frame or choose a model that can")
            pins = record.get("pin_resolutions")
            if pins and req.resolution not in pins:
                raise refuse(f"{req.model} pins a last frame only at {' or '.join(pins)}")
            if req.last_frame[0]["size"] != first["size"]:
                raise refuse("First and last frames must have the same canvas size")
        if req.keyframes:
            limit = record.get("keyframes_max", 0)
            if not limit:
                raise refuse(f"{req.model} takes no keyframes")
            if len(req.keyframes) > limit:
                raise refuse(f"{req.model} takes at most {limit} keyframes")
            pins = record.get("pin_resolutions")
            if pins and req.resolution not in pins:
                raise refuse(f"{req.model} takes keyframes only at {' or '.join(pins)}")
            times = sorted(seconds for _, seconds in req.keyframes)
            if times[0] <= 0 or times[-1] >= req.duration:
                raise refuse("Keyframe times must lie strictly between 0 and --duration seconds")
            if any(b - a < 1 / 3 - 1e-9 for a, b in zip(times, times[1:])):
                raise refuse("Keyframes must be at least 1/3 s apart")
            for (meta, _), _seconds in req.keyframes:
                if meta["size"] != first["size"]:
                    raise refuse("Keyframes must have the same canvas size as the first frame")
        span = record.get("input_px")
        if span and not all(span[0] <= side <= span[1] for side in first["size"]):
            raise refuse(f"{req.model} needs a first frame of {span[0]}..{span[1]} px per side; "
                         f"got {first['size'][0]}x{first['size'][1]}")
        aspect = record.get("input_aspect")
        if aspect and not aspect[0] <= first["size"][0] / first["size"][1] <= aspect[1]:
            raise refuse(f"{req.model} needs a first frame with an aspect ratio of {aspect[0]}..{aspect[1]}")

    # -- the steps (overridden) ----------------------------------------------------------------

    def build(self, req, base) -> Built:
        raise NotImplementedError

    def refusal(self, result):
        """Raise for a successful HTTP answer that refuses to make the media (moderation and the like)."""

    def read_submit(self, result, kind):
        raise NotImplementedError

    def status_url(self, job, base):
        raise MediaError(f"{self.label} has no asynchronous jobs in ASF", code="invalid_input", sent=False)

    def status(self, result, job, http_status=None):
        raise NotImplementedError

    def result(self, result, job):
        raise NotImplementedError

    def classify(self, status, fields):
        """A provider-specific outcome code for an HTTP error, or None for the generic mapping."""
        return None


# --------------------------------------------------------------------------- OpenAI

class OpenAIAdapter(Adapter):
    name = "openai"
    reserved = frozenset({"model", "prompt", "n", "image", "image[]", "images", "mask"})

    def _check_image(self, req, record, warnings):
        if req.resolution or req.aspect_ratio:
            raise refuse("OpenAI uses --size; --resolution/--aspect-ratio are xAI image options")
        if req.size and not SIZE.fullmatch(req.size):
            raise refuse("Size must be auto or WIDTHxHEIGHT; provider validates model-specific limits")
        super()._check_image(req, record, warnings)
        if record is None:
            return
        qualities = record.get("qualities")
        if req.quality and qualities is not None and req.quality not in qualities:
            raise refuse(f"{req.model} quality is one of {', '.join(qualities)}")
        size = parse_size(req.size)
        rule = record.get("size_rule")
        if size and rule:
            width, height = size
            if width % rule["multiple"] or height % rule["multiple"]:
                raise refuse(f"{req.model}: width and height must both be divisible by {rule['multiple']}")
            if max(width, height) > rule["max_edge"] or not rule["min_pixels"] <= width * height <= rule["max_pixels"]:
                raise refuse(f"{req.model}: {req.size} is outside {rule['min_pixels']:,}..{rule['max_pixels']:,} "
                             f"pixels or above {rule['max_edge']} px per edge")
            if max(width, height) / min(width, height) > rule["max_ratio"]:
                raise refuse(f"{req.model}: the aspect ratio must lie between 1:{rule['max_ratio']} and "
                             f"{rule['max_ratio']}:1")
        elif size and req.size not in record["image"].get("sizes", []):
            raise refuse(f"{req.model} renders {', '.join(record['image']['sizes'])}")

    def build(self, req, base):
        fields = {"model": req.model, "prompt": req.prompt, "n": 1, "output_format": "png"}
        if req.size:
            fields["size"] = req.size
        if req.transparent:
            fields["background"] = "transparent"
        if req.quality:
            fields["quality"] = req.quality
        apply_options(fields, req.options, self.reserved)
        options = {k: v for k, v in fields.items() if k != "prompt"}
        if req.references:
            body, content_type = multipart(fields, req.references)
            endpoint = base + "/images/edits"
        else:
            body, content_type = json.dumps(fields).encode(), "application/json"
            endpoint = base + "/images/generations"
        pricing = {"unit": "output_image", "quantity": 1,
                   "qualifiers": {"quality": fields.get("quality"), "size": fields.get("size")}, "inputs": None,
                   "note": "input image and text tokens are not included"}
        return Built(endpoint, body, content_type, options, pricing)

    def read_submit(self, result, kind):
        items = result.get("data")
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            raise MediaError("Expected exactly one generated image", code="bad_response")
        encoded = items[0].get("b64_json")
        if not isinstance(encoded, str):
            raise MediaError("Expected base64 image response; no regeneration attempted", code="bad_response")
        return MediaRef(data=_b64(encoded, "image"), returned_model=result.get("model"),
                        usage=_int_usage(result.get("usage"), (("input_tokens", "input_tokens"),
                                                                 ("output_tokens", "output_tokens"),
                                                                 ("total_tokens", "total_tokens"))))

    def classify(self, status, fields):
        text = _text(fields)
        if "moderation_blocked" in text or "image_generation_user_error" in text:
            return "moderation"
        if any(word in text for word in ("credit_balance_exhausted", "spend_limit_exceeded", "usage_limit_exceeded",
                                         "insufficient_quota")):
            return "quota"
        if status == 429 and "slow_down" in text:
            return "rate_limit"
        if status == 403 and any(word in text for word in ("country", "region", "territory", "verif")):
            return "entitlement"
        return None


# --------------------------------------------------------------------------- Google Gemini

GEMINI_BLOCKED = frozenset({"SAFETY", "IMAGE_SAFETY", "PROHIBITED_CONTENT", "IMAGE_PROHIBITED_CONTENT", "BLOCKLIST",
                            "SPII", "RECITATION", "IMAGE_RECITATION"})
GEMINI_SIZES = {"512": 512, "1K": 1024, "2K": 2048, "4K": 4096}


class GeminiAdapter(Adapter):
    name = "gemini"
    auth_header = "x-goog-api-key"
    auth_scheme = ""
    reserved = frozenset({"contents", "model"})

    def _image_config(self, req, record):
        """{"aspectRatio", "imageSize"} from --size (or --aspect-ratio / --resolution)."""
        ratios = (record or {}).get("aspect_ratios") or self.models()["gemini-3.1-flash-image"]["aspect_ratios"]
        sizes = (record or {}).get("image_sizes") or list(GEMINI_SIZES)
        config = {}
        size = parse_size(req.size)
        if req.aspect_ratio:
            config["aspectRatio"] = req.aspect_ratio
        elif size:
            config["aspectRatio"] = nearest_ratio(size[0], size[1], ratios)
        if req.resolution:
            wanted = req.resolution.upper()
        elif size:
            edge = max(size)
            wanted = next((name for name, px in GEMINI_SIZES.items() if px >= edge), "4K")
        else:
            wanted = None
        if wanted:
            fitting = [name for name in sizes if GEMINI_SIZES.get(name, 0) >= GEMINI_SIZES.get(wanted, 0)]
            config["imageSize"] = min(fitting, key=GEMINI_SIZES.get) if fitting else max(sizes, key=GEMINI_SIZES.get)
        return config

    def _check_image(self, req, record, warnings):
        super()._check_image(req, record, warnings)
        if req.transparent:
            raise refuse("Gemini has no transparent background; prompt a flat key colour and key it")
        if req.size and not SIZE.fullmatch(req.size):
            raise refuse("Size must be auto or WIDTHxHEIGHT")
        if req.quality:
            raise refuse("Gemini has no quality option; choose the model (flash-lite, flash or pro) instead")
        if record is not None:
            if req.resolution and req.resolution.upper() not in record["image_sizes"]:
                raise refuse(f"{req.model} renders {', '.join(record['image_sizes'])}")
            if req.aspect_ratio and req.aspect_ratio not in record["aspect_ratios"]:
                raise refuse(f"{req.model} takes the aspect ratios {', '.join(record['aspect_ratios'])}")
        total = sum(len(data) for _, data in req.references)
        if total > 14 * 1024 * 1024:
            raise refuse("Gemini inline references must stay under about 20 MB per request; use fewer or smaller images")

    def build(self, req, base):
        if "/" in req.model:
            raise refuse("A Gemini model id has no slash")
        record = self.model_record(req.model)
        parts = [{"text": req.prompt}]
        parts += [{"inlineData": {"mimeType": meta["mime"], "data": base64.b64encode(data).decode("ascii")}}
                  for meta, data in req.references]
        generation = {"responseModalities": ["IMAGE"]}
        config = self._image_config(req, record)
        if config:
            generation["imageConfig"] = config
        fields = {"contents": [{"role": "user", "parts": parts}], "generationConfig": generation}
        apply_options(fields, req.options, self.reserved)
        options = {"model": req.model, **strip_images({k: v for k, v in fields.items() if k != "contents"})}
        endpoint = f"{base}/models/{req.model}:generateContent"
        priced_inputs = len(req.references) if req.model == "gemini-3-pro-image" else None
        pricing = {"unit": "output_image", "quantity": 1,
                   "qualifiers": {"resolution": config.get("imageSize", "1K")}, "inputs": priced_inputs,
                   "note": "thinking and text tokens are not included" + (
                       "" if priced_inputs is not None else "; input image tokens are not included")}
        return Built(endpoint, json.dumps(fields).encode(), "application/json", options, pricing)

    def refusal(self, result):
        feedback = result.get("promptFeedback") if isinstance(result.get("promptFeedback"), dict) else {}
        block = feedback.get("blockReason")
        if isinstance(block, str) and block:
            raise MediaError(f"Gemini blocked the prompt ({_short(block)}); no regeneration attempted",
                             code="moderation", sent=True, provider={"httpStatus": 200, "code": _short(block)})
        candidates = result.get("candidates")
        candidate = candidates[0] if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict) else {}
        reason = candidate.get("finishReason")
        if reason in GEMINI_BLOCKED:
            raise MediaError(f"Gemini withheld the image (finishReason {reason}); no regeneration attempted",
                             code="moderation", sent=True, provider={"httpStatus": 200, "code": reason})
        if not self._images(candidate) and isinstance(reason, str) and reason not in ("STOP", "MAX_TOKENS"):
            raise MediaError(f"Gemini returned no image (finishReason {_short(reason)}); no regeneration attempted",
                             code="failed", sent=True, provider={"httpStatus": 200, "code": _short(reason)})

    @staticmethod
    def _images(candidate):
        content = candidate.get("content") if isinstance(candidate.get("content"), dict) else {}
        parts = content.get("parts") if isinstance(content.get("parts"), list) else []
        found = []
        for part in parts:
            if not isinstance(part, dict) or part.get("thought") is True:
                continue  # interim "thinking" images are drafts, never the result
            for key in ("inlineData", "inline_data", "fileData", "file_data"):
                blob = part.get(key)
                mime = (blob or {}).get("mimeType") or (blob or {}).get("mime_type") if isinstance(blob, dict) else None
                if isinstance(mime, str) and mime.startswith("image/"):
                    found.append((key, blob))
        return found

    def read_submit(self, result, kind):
        candidates = result.get("candidates")
        candidate = candidates[0] if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict) else {}
        images = self._images(candidate)
        if not images:
            raise MediaError("Gemini returned no image part; no regeneration attempted", code="bad_response")
        key, blob = images[-1]  # the last non-thought image is the final render
        usage = _int_usage(result.get("usageMetadata"), (("promptTokenCount", "input_tokens"),
                                                        ("candidatesTokenCount", "output_tokens"),
                                                        ("thoughtsTokenCount", "thought_tokens"),
                                                        ("totalTokenCount", "total_tokens")))
        model = result.get("modelVersion")
        if key.startswith("inline"):
            data = blob.get("data")
            if not isinstance(data, str):
                raise MediaError("Gemini image part has no data", code="bad_response")
            return MediaRef(data=_b64(data, "image"), returned_model=model, usage=usage)
        uri = blob.get("fileUri") or blob.get("file_uri")
        if not _https(uri):
            raise MediaError("Gemini file part has no HTTPS URI", code="bad_response")
        same_host = parse.urlsplit(uri).hostname == parse.urlsplit(self.api_base).hostname
        return MediaRef(url=uri, auth=same_host, returned_model=model, usage=usage)

    def classify(self, status, fields):
        text = _text(fields)
        if "api_key_invalid" in text or "api key not valid" in text or "api key expired" in text \
                or "unauthenticated" in text:
            return "auth"
        if status == 402:
            return "quota"
        if status == 429:
            return "quota" if any(w in text for w in ("billing", "spend", "prepay", "credit")) else "rate_limit"
        if "failed_precondition" in text or status in (403, 404):
            return "entitlement"
        return None


def _short(value):
    return re.sub(r"[^A-Za-z0-9_.:-]", "", str(value))[:60] or "unknown"


# --------------------------------------------------------------------------- xAI

class XAIAdapter(Adapter):
    name = "xai"
    reserved = frozenset({"model", "prompt", "n", "image", "images", "last_frame", "keyframes", "response_format"})

    def _check_image(self, req, record, warnings):
        if req.transparent or req.size:
            raise refuse("xAI has no transparency switch here; use a keyed backdrop and --resolution")
        if req.quality and (record is None or req.quality not in record.get("qualities", [])):
            raise refuse("xAI quality option requires image-2.0 and low/medium/auto")
        super()._check_image(req, record, warnings)
        if record is not None and req.resolution and req.resolution not in record["resolutions"]:
            raise refuse(f"{req.model} renders {', '.join(record['resolutions'])}")

    def build(self, req, base):
        record = self.model_record(req.model)
        if req.kind == "video":
            return self._build_video(req, base, record)
        fields = {"model": req.model, "prompt": req.prompt, "n": 1, "response_format": "b64_json"}
        if req.resolution:
            fields["resolution"] = req.resolution
        if req.aspect_ratio:
            fields["aspect_ratio"] = req.aspect_ratio
        if len(req.references) == 1:
            fields["image"] = {"url": data_uri(*req.references[0]), "type": "image_url"}
        elif req.references:
            fields["images"] = [{"type": "image_url", "url": data_uri(*ref)} for ref in req.references]
        if req.quality:
            fields["quality"] = req.quality
        apply_options(fields, req.options, self.reserved)
        endpoint = base + ("/images/edits" if req.references else "/images/generations")
        options = {k: v for k, v in fields.items() if k not in ("image", "images", "prompt")}
        quality = req.quality
        note = None
        if record is not None and record.get("qualities") and quality in (None, "auto"):
            quality = "medium" if req.references else "low"
            note = f"quality auto is priced as {quality}, the value xAI documents for {'edits' if req.references else 'generations'}"
        pricing = {"unit": "output_image", "quantity": 1,
                   "qualifiers": {"resolution": req.resolution or "1k", "quality": quality}, "inputs": len(req.references)}
        if note:
            pricing["note"] = note
        return Built(endpoint, json.dumps(fields).encode(), "application/json", options, pricing)

    def _build_video(self, req, base, record):
        fields = {"model": req.model, "prompt": req.prompt, "image": {"url": data_uri(*req.references[0])},
                  "duration": req.duration, "resolution": req.resolution}
        # Silent generation is sent only where it is documented for the model; every clip is
        # stripped of audio locally anyway.
        if record["video"].get("audio_off") is True:
            fields["generate_audio"] = False
        if req.last_frame is not None:
            fields["last_frame"] = {"url": data_uri(*req.last_frame)}
        if req.keyframes:
            fields["keyframes"] = [{"image": {"url": data_uri(*image)}, "timestamp_s": seconds}
                                   for image, seconds in sorted(req.keyframes, key=lambda item: item[1])]
        if req.upload_url:
            fields["output"] = {"upload_url": req.upload_url}
        apply_options(fields, req.options, self.reserved | {"output"})
        options = {k: v for k, v in fields.items() if k not in ("image", "last_frame", "prompt", "keyframes", "output")}
        if req.keyframes:
            options["keyframes"] = sorted(seconds for _, seconds in req.keyframes)
        inputs = 1 + (req.last_frame is not None) + len(req.keyframes)
        pricing = {"unit": "output_second", "quantity": req.duration, "qualifiers": {"resolution": req.resolution},
                   "inputs": inputs}
        return Built(base + "/videos/generations", json.dumps(fields).encode(), "application/json", options, pricing)

    def read_submit(self, result, kind):
        if kind == "video":
            rid = result.get("request_id")
            if not isinstance(rid, str) or not REQUEST_ID.fullmatch(rid):
                raise MediaError("Submit response omitted a valid request ID; check provider history before any new "
                                 "submission", code="bad_response")
            return JobRef(rid)
        items = result.get("data")
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            raise MediaError("Expected exactly one generated image", code="bad_response")
        encoded = items[0].get("b64_json")
        if not isinstance(encoded, str):
            raise MediaError("Expected base64 image response; no regeneration attempted", code="bad_response")
        return MediaRef(data=_b64(encoded, "image"), returned_model=result.get("model"),
                        usage=_int_usage(result.get("usage"), (("input_tokens", "input_tokens"),
                                                                 ("output_tokens", "output_tokens"),
                                                                 ("total_tokens", "total_tokens"))))

    def status_url(self, job, base):
        return base + "/videos/" + job["requestId"]

    def status(self, result, job, http_status=None):
        state = result.get("status")
        if state == "done":
            video = result.get("video")
            if isinstance(video, dict) and video.get("respect_moderation") is False:
                raise JobFailure("Provider did not release the video after moderation; no regeneration attempted",
                                 status="failed", code="moderation")
            if not isinstance(video, dict) or not isinstance(video.get("url"), str) or not video["url"]:
                hint = "; with --upload-url, check that destination" if job.get("uploadUrl") else ""
                raise MediaError("Completed video has no downloadable URL" + hint, code="bad_response")
            usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
            ticks = usage.get("cost_in_usd_ticks")
            cost = round(ticks / 1e10, 6) if isinstance(ticks, int) and not isinstance(ticks, bool) and ticks >= 0 else None
            return "done", MediaRef(url=video["url"], returned_model=result.get("model"),
                                    duration=video.get("duration"), cost_usd=cost)
        if state in ("failed", "expired"):
            error = result.get("error") if isinstance(result.get("error"), dict) else {}
            code = state
            text = " ".join(str(error.get(k, "")) for k in ("code", "message")).lower()
            if "moderat" in text or "content polic" in text or "blocked" in text:
                code = "moderation"
            elif "permission_denied" in text:
                code = "entitlement"
            provider = {"httpStatus": int(http_status or 200), "code": _short(error.get("code"))} if error else None
            raise JobFailure(f"Video job {state}; no regeneration attempted", status=state, code=code, provider=provider)
        if state == "pending" or (http_status == 202 and state is None):
            return "pending", None
        raise MediaError("Unknown video status; request ID retained, no regeneration attempted", code="bad_response")

    def classify(self, status, fields):
        text = _text(fields)
        if "incorrect api key" in text or "unauthenticated" in text or "no-credentials" in text:
            return "auth"
        if any(word in text for word in ("credits", "spending limit", "spending-limit")):
            return "quota"
        return None


# --------------------------------------------------------------------------- BytePlus ModelArk

BYTEPLUS_CODES = (
    ("AuthenticationError", "auth"), ("AccountOverdueError", "quota"), ("OperationDenied.ServiceOverdue", "quota"),
    ("QuotaExceeded", "quota"), ("SetLimitExceeded", "quota"), ("RateLimitExceeded", "rate_limit"),
    ("ModelAccountRpmRateLimitExceeded", "rate_limit"), ("ModelAccountIpmRateLimitExceeded", "rate_limit"),
    ("AccountRateLimitExceeded", "rate_limit"), ("RequestBurstTooFast", "rate_limit"), ("ServerOverloaded", "rate_limit"),
    ("ModelNotOpen", "entitlement"), ("InvalidEndpointOrModel", "entitlement"),
    ("OperationDenied.ServiceNotOpen", "entitlement"), ("AccessDenied", "entitlement"),
    ("InvalidParameter", "invalid_request"), ("MissingParameter", "invalid_request"),
    ("InvalidImageURL", "invalid_request"), ("InternalServiceError", "provider_error"),
)
SEEDREAM_PRO_LIMIT = 2_610_000  # list price steps up above 2.61 megapixels


def byteplus_code(code):
    code = str(code or "")
    if "SensitiveContentDetected" in code:
        return "moderation"
    for prefix, outcome in BYTEPLUS_CODES:
        if code.startswith(prefix):
            return outcome
    return None


class BytePlusAdapter(Adapter):
    name = "byteplus"
    reserved = frozenset({"model", "prompt", "image", "content", "response_format"})

    def _check_image(self, req, record, warnings):
        super()._check_image(req, record, warnings)
        if req.transparent:
            raise refuse("Seedream returns transparency only for one RGBA input image; use a flat key colour")
        if req.quality or req.resolution or req.aspect_ratio:
            raise refuse("Seedream takes --size only (WIDTHxHEIGHT or auto)")
        size = parse_size(req.size)
        if size and record is not None:
            low, high = record["pixel_range"]
            if size[0] * size[1] > high:
                raise refuse(f"{req.model} renders at most {high:,} pixels; {req.size} is larger")
            if not 1 / 16 <= size[0] / size[1] <= 16:
                raise refuse(f"{req.model} needs an aspect ratio between 1:16 and 16:1")

    def _size(self, req, record, warnings):
        size = parse_size(req.size)
        if not size:
            return None, None
        width, height = size
        low = (record or {}).get("pixel_range", [921600])[0]
        if width * height < low:
            scale = math.sqrt(low / (width * height))
            width, height = int(math.ceil(width * scale / 8) * 8), int(math.ceil(height * scale / 8) * 8)
            warnings.append(f"Seedream renders at least {low:,} pixels: {req.size} was raised to {width}x{height}")
        return f"{width}x{height}", width * height

    def build(self, req, base):
        record = self.model_record(req.model)
        if req.kind == "video":
            return self._build_video(req, base, record)
        warnings = []
        size, pixels = self._size(req, record, warnings)
        fields = {"model": req.model, "prompt": req.prompt, "response_format": "b64_json", "output_format": "png",
                  "watermark": False}
        if size:
            fields["size"] = size
        if len(req.references) == 1:
            fields["image"] = data_uri(*req.references[0])
        elif req.references:
            fields["image"] = [data_uri(*ref) for ref in req.references]
        apply_options(fields, req.options, self.reserved)
        options = strip_images({k: v for k, v in fields.items() if k != "prompt"})
        if req.model.startswith("dola-seedream-5-0-pro"):
            tier = "up-to-2.61MP" if (pixels or 2048 * 2048) <= SEEDREAM_PRO_LIMIT else "above-2.61MP"
            pricing = {"unit": "output_image", "quantity": 1, "qualifiers": {"resolution": tier},
                       "inputs": max(0, len(req.references) - 1),
                       "note": "the first reference image is free"}
        else:
            pricing = {"unit": "output_image", "quantity": 1, "qualifiers": {}, "inputs": None,
                       "note": "reference images are free on this model"}
        return Built(base + "/images/generations", json.dumps(fields).encode(), "application/json", options, pricing,
                     warnings=warnings)

    def _build_video(self, req, base, record):
        first_meta = req.references[0][0]
        width, height = first_meta["size"]
        ratios = record.get("ratios", ["adaptive"])
        ratio = "1:1" if width == height and "1:1" in ratios else "adaptive"
        content = [{"type": "text", "text": req.prompt},
                   {"type": "image_url", "image_url": {"url": data_uri(*req.references[0])}, "role": "first_frame"}]
        if req.last_frame is not None:
            content.append({"type": "image_url", "image_url": {"url": data_uri(*req.last_frame)}, "role": "last_frame"})
        fields = {"model": req.model, "content": content, "ratio": ratio, "duration": req.duration,
                  "resolution": req.resolution, "generate_audio": False, "watermark": False}
        apply_options(fields, req.options, self.reserved)
        options = {k: v for k, v in fields.items() if k != "content"}
        options["frames"] = [item["role"] for item in content if item["type"] == "image_url"]
        pricing = {"unit": "output_second", "quantity": req.duration, "qualifiers": {"resolution": req.resolution},
                   "inputs": None,
                   "note": "16:9 list price per second (billed by tokens, so square clips cost less); input images are not billed"}
        return Built(base + "/contents/generations/tasks", json.dumps(fields).encode(), "application/json", options,
                     pricing)

    def refusal(self, result):
        error = result.get("error")
        if isinstance(error, dict) and not result.get("data"):
            code = byteplus_code(error.get("code")) or "failed"
            raise MediaError(f"Seedream returned no image ({_short(error.get('code'))}); no regeneration attempted",
                             code=code, sent=True, provider={"httpStatus": 200, "code": _short(error.get("code"))})

    def read_submit(self, result, kind):
        if kind == "video":
            rid = result.get("id")
            if not isinstance(rid, str) or not REQUEST_ID.fullmatch(rid):
                raise MediaError("Submit response omitted a valid task id; check the ModelArk console before any new "
                                 "submission", code="bad_response")
            return JobRef(rid)
        items = result.get("data")
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            raise MediaError("Expected exactly one generated image", code="bad_response")
        encoded = items[0].get("b64_json")
        if isinstance(encoded, str):
            data = _b64(encoded, "image")
            return MediaRef(data=data, returned_model=result.get("model"),
                            usage=_int_usage(result.get("usage"), (("output_tokens", "output_tokens"),
                                                                     ("total_tokens", "total_tokens"))))
        if _https(items[0].get("url")):
            return MediaRef(url=items[0]["url"], returned_model=result.get("model"))
        raise MediaError("Expected a base64 image or an HTTPS URL", code="bad_response")

    def status_url(self, job, base):
        return base + "/contents/generations/tasks/" + job["requestId"]

    def status(self, result, job, http_status=None):
        state = result.get("status")
        if state in ("queued", "running"):
            return "pending", None
        if state == "succeeded":
            content = result.get("content") if isinstance(result.get("content"), dict) else {}
            if not _https(content.get("video_url")):
                raise MediaError("Completed task has no downloadable video URL", code="bad_response")
            return "done", MediaRef(url=content["video_url"], returned_model=result.get("model"),
                                    duration=result.get("duration"),
                                    usage=_int_usage(result.get("usage"), (("completion_tokens", "output_tokens"),
                                                                           ("total_tokens", "total_tokens"))))
        if state in ("failed", "cancelled", "expired"):
            error = result.get("error") if isinstance(result.get("error"), dict) else {}
            terminal = "expired" if state == "expired" else "failed"
            code = byteplus_code(error.get("code")) or terminal
            provider = {"httpStatus": int(http_status or 200), "code": _short(error.get("code"))} if error else None
            raise JobFailure(f"Video task {state}; no regeneration attempted", status=terminal, code=code,
                             provider=provider)
        raise MediaError("Unknown task status; task id retained, no regeneration attempted", code="bad_response")

    def classify(self, status, fields):
        return byteplus_code(fields.get("code"))


# --------------------------------------------------------------------------- fal.ai

class FalAdapter(Adapter):
    """fal.ai queue: submit, then status, then result, with a curated parameter map per model
    (capabilities.json "fal"). Inputs are uploaded to the fal CDN first (free requests, before the
    paid one); --provider-option fal_upload=data sends data URIs instead."""

    name = "fal"
    auth_scheme = "Key"
    reserved = frozenset({"prompt"})
    TOKEN_URL = "https://rest.fal.ai/storage/auth/token?storage_type=fal-cdn-v3"
    UPLOAD_URL = "https://v3.fal.media/files/upload"

    def _check_image(self, req, record, warnings):
        super()._check_image(req, record, warnings)
        spec = record["fal"]
        if len(req.references) < spec.get("refs_min", 0):
            raise refuse(f"{req.model} edits reference images: add at least {spec['refs_min']} --reference")
        if req.transparent and not spec.get("background"):
            raise refuse(f"{req.model} has no transparent background")
        if req.resolution or req.aspect_ratio:
            raise refuse("fal image models take --size (WIDTHxHEIGHT or auto)")
        if req.quality and not spec.get("quality"):
            raise refuse(f"{req.model} has no quality option")

    def _check_video(self, req, record, warnings):
        super()._check_video(req, record, warnings)
        if record["fal"].get("last_required") and req.last_frame is None:
            raise refuse(f"{req.model} needs both frames: add --last-frame (the still itself pins a loop)")

    def build(self, req, base):
        if not FAL_ID.fullmatch(req.model) or ".." in req.model:
            raise refuse(f"Not a fal endpoint id: {req.model}")
        record = self.model_record(req.model)
        options = dict(req.options)
        mode = options.pop("fal_upload", "cdn")
        if mode not in ("cdn", "data"):
            raise refuse("--provider-option fal_upload is cdn (default) or data")
        fill = options.pop("pad_colour", None)
        spec = record["fal"]
        uploads, extra, warnings = [], {}, []

        def image_value(image, name):
            if mode == "data":
                return data_uri(*image)
            token = f"{PAD_TOKEN}{len(uploads)}"
            meta, data = image
            suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[meta["mime"]]
            uploads.append((token, meta, data, f"{name}{suffix}"))
            return token

        fields = {spec.get("prompt", "prompt"): req.prompt}
        if req.kind == "video":
            fields.update(self._video_fields(req, spec, record, image_value, extra, warnings, fill))
            unit_qualifier = fields.get((spec.get("resolution") or {}).get("field"))
            pricing = {"unit": "output_second", "quantity": req.duration,
                       "qualifiers": {"resolution": unit_qualifier} if unit_qualifier else {}, "inputs": None,
                       "note": "audio-off list price per second; input images are not billed"}
        else:
            fields.update(self._image_fields(req, spec, image_value, warnings))
            pricing = self._image_pricing(req, spec, fields)
        fields.update(spec.get("fixed") or {})
        protected = {spec[k] for k in ("first", "last", "refs", "loop") if spec.get(k)}
        apply_options(fields, options, self.reserved | protected)
        if mode == "data" or not uploads:
            body = json.dumps(fields).encode()
        else:
            body = DeferredBody(fields, uploads)
            extra["uploads"] = {"to": parse.urlsplit(self.UPLOAD_URL).hostname, "count": len(uploads)}
        plan_options = {"model": req.model, **strip_images({k: v for k, v in fields.items()
                                                            if k != spec.get("prompt", "prompt")})}
        if mode == "data":
            plan_options["fal_upload"] = "data"
        return Built(f"{base}/{req.model}", body, "application/json", plan_options, pricing, extra, warnings)

    def _video_fields(self, req, spec, record, image_value, extra, warnings, fill):
        first, last = req.references[0], req.last_frame
        aspect = spec.get("aspect") or {}
        fields = {}
        if aspect.get("pad"):
            frames = [first] + ([last] if last is not None else [])
            colour = None
            if fill:
                match = re.fullmatch(r"#?([0-9A-Fa-f]{6})", str(fill))
                if not match:
                    raise refuse("--provider-option pad_colour must be #RRGGBB")
                colour = tuple(int(match.group(1)[i:i + 2], 16) for i in (0, 2, 4))
            width, height = first[0]["size"]
            if exact_ratio(width, height, [aspect["pad"]]):
                fields[aspect["field"]] = aspect["pad"]
            else:
                padded, transform, crop = pad_to_aspect(frames, aspect["pad"], colour)
                first, last = padded[0], (padded[1] if last is not None else None)
                extra["inputTransform"], extra["crop"] = transform, crop
                warnings.append(f"{req.model} renders {aspect['pad']} only: the frames were padded with "
                                f"{transform['fill']} and job.json records the crop back to {width}x{height}")
                fields[aspect["field"]] = aspect["pad"]
        elif aspect.get("field"):
            width, height = first[0]["size"]
            chosen = exact_ratio(width, height, aspect["values"]) or aspect.get("default") \
                or nearest_ratio(width, height, aspect["values"])
            fields[aspect["field"]] = chosen
        fields[spec["first"]] = image_value(first, "first-frame")
        loop = spec.get("loop")
        if last is not None:
            if loop and last[1] == first[1]:
                fields[loop] = True  # the model's own loop: the end blends back into the start
                extra["pin"] = "loop"
            else:
                fields[spec["last"]] = image_value(last, "last-frame")
                extra["pin"] = "last_frame"
        duration = spec.get("duration") or {}
        if duration.get("field"):
            form = duration.get("format", "int")
            fields[duration["field"]] = req.duration if form == "int" else (
                f"{req.duration}s" if form == "s" else str(req.duration))
        resolution = spec.get("resolution") or {}
        if resolution.get("field"):
            fields[resolution["field"]] = resolution["map"][req.resolution]
            if resolution["map"][req.resolution] != req.resolution:
                warnings.append(f"{req.model} renders {resolution['map'][req.resolution]} for a {req.resolution} request")
        elif record["video"].get("resolutions") == "any":
            warnings.append(f"{req.model} renders at its own resolution; --resolution is not sent")
        audio = spec.get("audio")
        if audio:
            fields[audio["field"]] = audio["off"]
        return fields

    def _image_fields(self, req, spec, image_value, warnings):
        fields = {spec["refs"]: [image_value(ref, f"reference-{i}") for i, ref in enumerate(req.references)]}
        size = parse_size(req.size)
        sizing = spec.get("size") or {}
        if sizing.get("kind") == "aspect_resolution" and size:
            fields[sizing["aspect_field"]] = nearest_ratio(size[0], size[1], sizing["aspects"])
            edge = max(size)
            ladder = sorted(sizing["resolutions"].items(), key=lambda item: item[1])
            fields[sizing["resolution_field"]] = next((name for name, px in ladder if px >= edge), ladder[-1][0])
        elif sizing.get("kind") == "width_height" and size:
            fields[sizing["field"]] = {"width": size[0], "height": size[1]}
        if spec.get("quality"):
            fields[spec["quality"]] = req.quality or spec.get("quality_default", "high")
        if req.transparent:
            fields[spec["background"]] = "transparent"
        return fields

    def _image_pricing(self, req, spec, fields):
        pricing = {"unit": "output_image", "quantity": 1, "qualifiers": {}, "inputs": None,
                   "note": "list price on fal.ai"}
        sizing = spec.get("size") or {}
        if sizing.get("kind") == "aspect_resolution":
            pricing["qualifiers"]["resolution"] = fields.get(sizing["resolution_field"], sizing.get("default", "1K"))
        elif sizing.get("kind") == "width_height":
            box = fields.get(sizing["field"])
            pixels = box["width"] * box["height"] if isinstance(box, dict) else None
            for name, limit in spec.get("price_tiers", {}).items():
                if pixels is not None and pixels <= limit:
                    pricing["qualifiers"]["resolution"] = name
                    break
            if spec.get("quality"):
                pricing["qualifiers"]["quality"] = fields[spec["quality"]]
                if isinstance(box, dict):
                    pricing["qualifiers"]["size"] = f"{box['width']}x{box['height']}"
        if spec.get("extra_inputs_priced"):
            pricing["inputs"] = max(0, len(req.references) - 1)
            pricing["note"] += "; the first reference image is free"
        return pricing

    def read_submit(self, result, kind):
        rid = result.get("request_id")
        if not isinstance(rid, str) or not REQUEST_ID.fullmatch(rid):
            raise MediaError("Submit response omitted a valid request id; check fal.ai's request history before any "
                             "new submission", code="bad_response")
        queue = {}
        for key, name in (("status_url", "statusUrl"), ("response_url", "responseUrl"), ("cancel_url", "cancelUrl")):
            url = result.get(key)
            if isinstance(url, str) and _https(url) and not parse.urlsplit(url).query and "#" not in url:
                queue[name] = url
        if "statusUrl" not in queue or "responseUrl" not in queue:
            raise MediaError(f"Submit response for fal.ai request {rid} has no valid queue status or result URL; "
                             "look the request up in fal.ai's request history before any new submission",
                             code="bad_response")
        return JobRef(rid, {"queue": queue})

    def _queue_url(self, url, base=None):
        """A queue URL fal.ai returned, on the queue host (the key is sent there): never rebuilt, never elsewhere."""
        host = parse.urlsplit(base or self.api_base).hostname
        parts = parse.urlsplit(url)
        return _https(url) and parts.hostname == host and not parts.query and not parts.fragment

    def status_url(self, job, base):
        url = (job.get("queue") or {}).get("statusUrl")
        if not isinstance(url, str) or not self._queue_url(url, base):
            raise MediaError("job.json has no valid fal.ai status URL; never rebuilt from the model id",
                             code="bad_response", sent=False)
        return url

    def status(self, result, job, http_status=None):
        state = result.get("status")
        if state in ("IN_QUEUE", "IN_PROGRESS"):
            return "pending", None
        if state == "COMPLETED":
            error_type = result.get("error_type")
            if result.get("error") or error_type:
                code = "moderation" if "content_policy" in str(error_type or result.get("error")).lower() else "failed"
                provider = {"httpStatus": int(http_status or 200), "type": _short(error_type or "error")}
                raise JobFailure(f"fal.ai request completed with an error ({_short(error_type or 'error')}); "
                                 "no regeneration attempted", status="failed", code=code, provider=provider)
            url = (job.get("queue") or {}).get("responseUrl")
            if not isinstance(url, str) or not self._queue_url(url, job.get("apiBase")):
                raise MediaError("job.json has no valid fal.ai result URL", code="bad_response", sent=False)
            return "result", url
        raise MediaError("Unknown fal.ai queue status; request id retained, no regeneration attempted",
                         code="bad_response")

    def result(self, result, job):
        if job.get("kind") == "video":
            video = result.get("video") if isinstance(result.get("video"), dict) else {}
            url = video.get("url")
        else:
            images = result.get("images") if isinstance(result.get("images"), list) else []
            url = images[0].get("url") if images and isinstance(images[0], dict) else None
        if isinstance(url, str) and url.startswith("data:"):
            return MediaRef(data=_b64(url.split(",", 1)[-1], "media"))
        if not _https(url):
            raise MediaError("The fal.ai result has no downloadable media URL", code="bad_response")
        return MediaRef(url=url)

    def classify(self, status, fields):
        text = _text(fields)
        if "content_policy" in text:
            return "moderation"
        if status == 401:
            return "auth"
        if status == 403:
            return "quota" if any(w in text for w in ("balance", "locked", "billing", "payment")) else "entitlement"
        if status == 404:
            return "entitlement"
        if status == 429:
            return "rate_limit"
        if "no_media_generated" in text:
            return "failed"
        if status == 422:
            return "invalid_request"
        return None


ADAPTER_CLASSES = {"openai": OpenAIAdapter, "gemini": GeminiAdapter, "xai": XAIAdapter, "byteplus": BytePlusAdapter,
                   "fal": FalAdapter}
_ADAPTERS = {}


def adapter(name) -> Adapter:
    """The adapter of a provider (openai, gemini, xai, byteplus or fal)."""
    if name not in _ADAPTERS:
        record = capabilities()["providers"].get(name)
        if name not in ADAPTER_CLASSES or record is None:
            raise MediaError(f"Unknown provider {name}", code="invalid_input", sent=False)
        _ADAPTERS[name] = ADAPTER_CLASSES[name](record)
    return _ADAPTERS[name]


def providers_for(kind):
    return [name for name in ORDER if kind in adapter(name).kinds]


def default_model(provider, kind, tier="standard", settings=None):
    """(model, quality) of a tier: the user's config slot (<provider>-<kind>[-draft|-hero]) or capabilities.json."""
    record = adapter(provider).tier(kind, tier) or adapter(provider).tier(kind, "standard") or {}
    slot = f"{provider}-{kind}" + ("" if tier == "standard" else f"-{tier}")
    model = media_config.model_for(slot, settings, default=record.get("model"))
    quality = record.get("quality") if model == record.get("model") else None
    return model, quality


# --------------------------------------------------------------------------- CLI

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="every provider: key variables, models, tiers, verifiedAt")
    show = commands.add_parser("show", help="one provider or model record as JSON")
    show.add_argument("provider", choices=ORDER)
    show.add_argument("model", nargs="?")
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            rows = []
            for name in ORDER:
                item = adapter(name)
                rows.append({"provider": name, "label": item.label, "keyEnv": list(media_config.PROVIDER_KEYS[name]),
                             "configured": media_config.key_source(name) is not None, "kinds": list(item.kinds),
                             "verifiedAt": item.record["verifiedAt"],
                             "tiers": {kind: {t: (item.tier(kind, t) or {}).get("model") for t in TIERS}
                                       for kind in item.kinds},
                             "models": sorted(item.models())})
            print(json.dumps({"providers": rows}, ensure_ascii=True))
        else:
            item = adapter(args.provider)
            record = item.model_record(args.model) if args.model else {k: v for k, v in item.record.items()
                                                                        if k != "models"}
            if record is None:
                raise MediaError(f"{args.provider} has no model {args.model}")
            print(json.dumps(record, ensure_ascii=True, indent=2))
    except MediaError as exc:
        print("error: " + str(exc).encode("ascii", "backslashreplace").decode("ascii"), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
