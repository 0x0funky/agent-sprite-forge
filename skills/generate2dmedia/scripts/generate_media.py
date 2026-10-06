#!/usr/bin/env python3
"""One-request media API adapter. Dry-run by default; no paid POST retries.

Providers (media_providers.py, contracts checked against the vendors' docs on
2026-10-06): OpenAI and Google Gemini images; xAI images and video; BytePlus
ModelArk Seedream images and Seedance video; fal.ai image edits and video.
Generation is separate from asset QA. Each executed request is first reserved
in <project>/.forge/ledger.jsonl (opt-in spend caps, duplicate guard), then
committed with its outcome, the provider's job id and the artifact's sha256.
An asynchronous job's id is saved before polling and its media is downloaded
as soon as it is done; every clip is published without audio. Keys come from
each provider's own variables (OPENAI_API_KEY, GOOGLE_API_KEY or
GEMINI_API_KEY, XAI_API_KEY, ARK_API_KEY, FAL_KEY) or the user config file
(media_config.py); route_media.py is the entry point that sends at once when
a key is configured. Uses stdlib HTTP and Pillow.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import http.client
import io
import json
import math
import os
from pathlib import Path
import re
import socket
import ssl
import sys
import threading
import time
from urllib import error, parse, request
import uuid

from PIL import Image

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import media_config  # noqa: E402  (sibling modules in this skill's scripts/)
import media_ledger  # noqa: E402
import media_mp4  # noqa: E402
import media_providers  # noqa: E402

TOOL_VERSION = media_ledger.FORGE_PACKAGE_VERSION  # receipts name the package release (D29)
TOOL = "generate_media/" + TOOL_VERSION
USER_AGENT = "agent-sprite-forge-media/" + TOOL_VERSION
API = {name: media_providers.adapter(name).api_base for name in media_providers.ORDER}
KEY = dict(media_config.PROVIDERS)
MIME = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
EXT = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}
API_LIMIT = 100 * 1024 * 1024
DOWNLOAD_LIMIT = 256 * 1024 * 1024
REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,200}")
MODEL_ID = re.compile(r"[A-Za-z0-9_.:/-]{1,120}")
JOB_ID = re.compile(r"[A-Za-z0-9_.-]{1,80}")
ARTIFACT_NAME = re.compile(r"generated\.(?:png|jpg|webp|mp4)")
ERROR_FIELDS = ("code", "type", "param", "message")
# Batch outcomes that concern one job only; anything else (auth, quota, rate
# limit, moderation, entitlement, caps, missing key, network, unknown or
# possibly charged outcomes) stops dispatching further jobs.
CONTINUE_CODES = frozenset({"ok", "prior", "invalid_request", "duplicate", "output_exists",
                            "failed", "expired", "pending_timeout"})
BATCH_LEVEL_KEYS = frozenset({"execute", "allow_duplicate", "budget_usd", "max_calls", "project_dir",
                              "prices", "allow_custom_base_url", "help"})
JOB_PATH_KEYS = frozenset({"prompt_file", "reference", "last_frame", "out_dir", "keyframe"})
RAW_CLIP = "provider-download.mp4"  # the clip as downloaded, kept beside the silent generated.mp4
MAX_REDIRECTS = 3
# Id of the batch progress file (media.schema.json batch_progress_v1). Files from before the
# namespacing say "batch_progress_v1"; nothing reads a progress file back (a re-run resumes from
# each job folder and the ledger, then rewrites the file), so old files stay harmless.
BATCH_PROGRESS_SCHEMA = "generate2dmedia.batch_progress.v1"


# One error class for every module of this skill: code is the receipt outcomeCode, sent says whether
# a paid POST may have reached the provider (False, True or None) and provider holds whitelisted,
# scrubbed provider error fields.
MediaError = media_providers.MediaError
JobFailure = media_providers.JobFailure


class PartialArtifact(MediaError):
    """Returned bytes are kept in a .partial file because they could not be published."""

    def __init__(self, message, partial, code="partial_artifact"):
        super().__init__(f"{message}; the returned bytes are kept in {partial['path']}", code=code, sent=True)
        self.partial = partial


class NotSent(OSError):
    """The connection failed before any request byte could be written."""


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise MediaError("API redirect refused; credentials were not forwarded", code="redirect", sent=True)


class SafeRedirect(request.HTTPRedirectHandler):
    """Media downloads follow at most MAX_REDIRECTS HTTPS redirects and never carry a credential to the
    next host: the key (Gemini file downloads only) travels as an unredirected header, and every
    redirected request is rebuilt with nothing but the User-Agent."""

    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = parse.urlsplit(newurl)
        if target.scheme != "https" or not target.hostname or target.username or target.password:
            raise MediaError("Media download redirected to a non-HTTPS or credentialed URL; refused",
                             code="redirect", sent=True)
        return request.Request(newurl, headers={"User-Agent": USER_AGENT}, method="GET")


class _TrackedHTTPSConnection(http.client.HTTPSConnection):
    forge_connected = False

    def connect(self):
        super().connect()  # DNS, TCP, proxy tunnel and TLS handshake
        self.forge_connected = True


class _ConnectTracker(request.HTTPSHandler):
    """HTTPS handler that remembers whether its connection was established, so a
    failure is classified as certainly-not-sent or possibly-sent."""

    def __init__(self):
        self._forge_context = ssl.create_default_context()
        self._forge_context.set_alpn_protocols(["http/1.1"])
        super().__init__(context=self._forge_context)
        self.connection = None

    @property
    def connected(self):
        return self.connection is not None and self.connection.forge_connected

    def https_open(self, req):
        return self.do_open(self._connect, req, context=self._forge_context)

    def _connect(self, host, **kwargs):
        self.connection = _TrackedHTTPSConnection(host, **kwargs)
        return self.connection


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _secret_in(text, secrets):
    return any(s and len(s) >= 8 and s in text for s in secrets)


def redact(text, secrets=()):
    """Replace every secret (and its JSON-escaped form) with [redacted]."""
    for secret in secrets:
        if secret and len(secret) >= 8:
            text = text.replace(secret, "[redacted]").replace(json.dumps(secret)[1:-1], "[redacted]")
    return text


def _env_secrets():
    """Every configured key (environment and the user config file), for redaction only."""
    return media_config.known_secrets()


_SCRUB = ((re.compile(r"(?i)\b(?:https?|wss?)://\S+"), "[url]"),
          (re.compile(r"(?i)\bdata:\S+"), "[data]"),
          (re.compile(r"(?i)\b(?:sk|xai|rk)-[A-Za-z0-9*._-]{4,}|\bbearer\s+\S+"), "[redacted]"),
          (re.compile(r"[A-Za-z0-9+/=_-]{40,}"), "[blob]"))


def scrub(value, secrets=(), limit=300):
    """Provider text made safe to store and print: secrets, URLs, data URIs,
    key-like tokens and long blobs are removed before truncating."""
    text = redact(str(value), secrets)
    for pattern, token in _SCRUB:
        text = pattern.sub(token, text)
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    return " ".join(text.split())[:limit]


def _ascii(text):
    return text.encode("ascii", "backslashreplace").decode("ascii")


def _valid_id(value, secrets=()):
    return isinstance(value, str) and bool(REQUEST_ID.fullmatch(value)) and not _secret_in(value, secrets)


def _header(headers, *names):
    for name in names:
        value = headers.get(name) if headers is not None else None
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def provider_error_fields(status, body, headers, secrets=()):
    """Whitelisted, scrubbed provider error: httpStatus, code, type, param,
    message (at most 300 characters) and requestId. Everything else is dropped."""
    fields = {"httpStatus": int(status)}
    try:
        payload = json.loads(body.decode("utf-8", "replace")) if body else None
    except ValueError:
        payload = None
    source = {}
    if isinstance(payload, dict):
        nested = payload.get("error")
        detail = payload.get("detail")
        if isinstance(nested, dict):
            # OpenAI and BytePlus: {"error": {"message", "type", "param", "code"}}; Google:
            # {"error": {"code": 400, "message", "status": "INVALID_ARGUMENT", "details": [{"reason"}]}}
            source = dict(nested)
            if isinstance(nested.get("status"), str) and not nested.get("type"):
                source["type"] = nested["status"]
            reasons = [d["reason"] for d in nested.get("details") or [] if isinstance(d, dict)
                       and isinstance(d.get("reason"), str)]
            if reasons:
                source["code"] = reasons[0]
        elif detail is not None:  # fal.ai: {"detail": "text"} or {"detail": [{"loc", "msg", "type"}]}
            if isinstance(detail, list) and detail and isinstance(detail[0], dict):
                first = detail[0]
                loc = first.get("loc")
                source = {"message": first.get("msg"), "type": first.get("type"),
                          "param": ".".join(str(p) for p in loc[1:]) if isinstance(loc, list) and len(loc) > 1 else None}
            elif isinstance(detail, str):
                source = {"message": detail}
            if isinstance(payload.get("error_type"), str):
                source.setdefault("type", payload["error_type"])
        else:  # xAI: {"code": "...", "error": "message"}
            source = {**payload, "message": nested if isinstance(nested, str) else payload.get("message")}
    for name in ERROR_FIELDS:
        value = source.get(name)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            text = scrub(value, secrets, 300 if name == "message" else 100)
            if text:
                fields[name] = text
    if "type" not in fields:
        kind = _header(headers, "x-fal-error-type")
        if kind:
            fields["type"] = scrub(kind, secrets, 100)
    rid = _header(headers, "x-request-id", "request-id", "x-fal-request-id")
    if rid is None and isinstance(payload, dict):
        rid = payload.get("request_id")
    if _valid_id(rid, secrets):
        fields["requestId"] = rid
    return fields


def classify_http(status, fields):
    """Outcome code for an HTTP error; batches stop on auth, quota, rate_limit,
    moderation and entitlement."""
    text = " ".join(str(fields.get(k, "")) for k in ("code", "type", "message")).lower()
    if status == 401:
        return "auth"
    if status >= 500:
        return "provider_error"
    if any(w in text for w in ("moderation", "content_policy", "content policy", "safety", "flagged")):
        return "moderation"
    if status == 429:
        return "rate_limit" if ("rate_limit" in text or "rate limit" in text) and "quota" not in text else "quota"
    if status == 402 or any(w in text for w in ("insufficient_quota", "quota", "billing", "credit", "spend limit")):
        return "quota"
    if status == 403 or any(w in text for w in ("entitle", "permission", "not have access", "must be verified")):
        return "entitlement"
    if status == 404 and "model" in text:
        return "entitlement"
    return "invalid_request"


def _http_message(fields):
    text = f"API HTTP {fields['httpStatus']}"
    label = "/".join(fields[k] for k in ("type", "code") if k in fields)
    if label:
        text += " " + label
    if "param" in fields:
        text += f" (param {fields['param']})"
    if "message" in fields:
        text += ": " + fields["message"]
    if "requestId" in fields:
        text += f" [request id {fields['requestId']}]"
    return text + "; request was not retried"


def _reason_text(reason, host):
    if isinstance(reason, socket.gaierror):
        return f"DNS lookup failed for {host}"
    if isinstance(reason, ConnectionRefusedError):
        return f"Connection to {host} was refused"
    if isinstance(reason, ssl.SSLCertVerificationError):
        return f"TLS certificate verification failed for {host}"
    if isinstance(reason, TimeoutError):
        return f"Connecting to {host} timed out"
    return f"Could not connect to {host} ({type(reason).__name__})"


def _lost(paid, failure):
    """Error for a request that may have reached the provider."""
    if paid:
        return MediaError(failure + "; outcome unknown and not retried. Check the provider's usage history "
                          "before sending it again", code="submit_unknown")
    return MediaError(failure + "; the job is unchanged, poll it again with resume", code="network")


def _error_body(exc):
    try:
        return exc.read(65536)
    except (AttributeError, OSError, ValueError, http.client.HTTPException):
        return b""


def save_json(path, data, secrets=()):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(redact(json.dumps(data, ensure_ascii=False, indent=2), secrets) + "\n", encoding="utf-8")
    tmp.replace(path)


def _utcnow():
    return datetime.now(timezone.utc)


def _ms_between(start_iso, end):
    try:
        start = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return max(0, round((end - start).total_seconds() * 1000))


def _portable(path, base):
    path, base = Path(path).resolve(), Path(base).resolve()
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.as_posix()


def inspect_image(path):
    path = Path(path)
    if path.stat().st_size > 20 * 1024 * 1024:
        raise MediaError("Reference exceeds this adapter's 20 MiB upload limit")
    data = path.read_bytes()
    with Image.open(io.BytesIO(data)) as img:
        fmt, size = img.format, list(img.size)
        if fmt not in MIME:
            raise MediaError("References must be PNG, JPEG or WebP")
        img.verify()
    return {"path": str(path.resolve()), "sha256": digest(data), "size": size,
            "mime": MIME[fmt], "bytes": len(data)}, data


data_uri = media_providers.data_uri
multipart = media_providers.multipart


def parse_provider_options(items):
    """--provider-option KEY=VALUE pairs: VALUE is JSON when it parses (numbers, true, "quoted"), else text."""
    options = {}
    for item in items or ():
        key, sep, raw = str(item).partition("=")
        key = key.strip()
        if not sep or not media_providers.OPTION_KEY.fullmatch(key):
            raise MediaError(f"--provider-option needs KEY=VALUE with a letter-and-digit key (got {key[:40]!r})",
                             code="invalid_input", sent=False)
        try:
            value = json.loads(raw)
        except ValueError:
            value = raw
        options[key] = value
    return options


def parse_keyframe(text):
    """PATH@SECONDS -> (path, seconds) for one --keyframe."""
    path, sep, seconds = str(text).rpartition("@")
    try:
        value = float(seconds)
    except ValueError:
        value = None
    if not sep or not path or value is None or not math.isfinite(value):
        raise MediaError("--keyframe needs PATH@SECONDS, for example still.png@2.5", code="invalid_input", sent=False)
    return path, round(value, 3)


def bounded_time(args):
    for name, minimum, maximum in (("timeout", 1, 3600), ("submit_timeout", 1, 600), ("poll_interval", 1, 60)):
        value = getattr(args, name, None)
        if value is not None and (not math.isfinite(value) or not minimum <= value <= maximum):
            raise MediaError(f"{name.replace('_', '-')} must be within {minimum}..{maximum} seconds", code="invalid_input", sent=False)


def check_limits(args):
    budget = getattr(args, "budget_usd", None)
    if budget is not None and (not math.isfinite(budget) or budget < 0):
        raise MediaError("--budget-usd must be a finite amount >= 0", code="invalid_input", sent=False)
    if (getattr(args, "max_calls", None) or 0) < 0:
        raise MediaError("--max-calls must be >= 0", code="invalid_input", sent=False)
    if len(getattr(args, "purpose", None) or "") > 200:
        raise MediaError("--purpose must be at most 200 characters", code="invalid_input", sent=False)


def checked_base_url(url):
    parsed = parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise MediaError("--base-url must be an https URL without credentials, query or fragment", code="invalid_input", sent=False)
    return url.rstrip("/")


def api_base(args):
    """The provider's default base URL, or an explicitly allowed https --base-url."""
    custom = getattr(args, "base_url", None)
    if not custom:
        return API[args.provider]
    if not getattr(args, "allow_custom_base_url", False):
        raise MediaError("--base-url sends the API key to another host; add --allow-custom-base-url to confirm", code="invalid_input", sent=False)
    return checked_base_url(custom)


def upload_target(url):
    """Validate --upload-url; only its host and hash are stored (it may be a signed URL)."""
    parsed = parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise MediaError("--upload-url must be an https URL without embedded credentials", code="invalid_input", sent=False)
    return {"host": parsed.hostname, "sha256": digest(url.encode("utf-8"))}


def prepare(args):
    """Validate without keys, output writes, or network; return (plan, body, content type, prompt).

    The provider's adapter (media_providers.py) gates the request against the model's capability
    record and builds it. body is bytes, or a DeferredBody whose inputs are uploaded first (fal.ai)."""
    bounded_time(args)
    check_limits(args)
    prompt = Path(args.prompt_file).read_text(encoding="utf-8-sig").strip()
    if not prompt or len(prompt) > 30000:
        raise MediaError("Prompt must contain 1..30000 characters")
    adapter = media_providers.adapter(args.provider)
    pattern = media_providers.FAL_ID if args.provider == "fal" else media_providers.MODEL_ID
    if not pattern.fullmatch(args.model) or ".." in args.model:
        raise MediaError("Specify a concrete provider model identifier")
    try:
        prices = media_ledger.load_prices(args.prices)
    except media_ledger.LedgerError as exc:
        raise MediaError(str(exc), code="invalid_input", sent=False) from None
    base = api_base(args)
    refs = [inspect_image(p) for p in args.reference]
    video = args.command == "video"
    last = inspect_image(args.last_frame) if video and args.last_frame else None
    keyframes = []
    for item in (getattr(args, "keyframe", None) or []) if video else []:
        path, seconds = parse_keyframe(item)
        keyframes.append((inspect_image(path), seconds))
    images = [data for _, data in refs] + ([last[1]] if last else []) + [image[1] for image, _ in keyframes]
    if sum(len(data) for data in images) > 40 * 1024 * 1024:
        raise MediaError("References exceed this adapter's 40 MiB combined upload limit")
    upload = None
    if video and args.upload_url:
        if args.provider != "xai":
            raise MediaError("--upload-url is an xAI option", code="invalid_input", sent=False)
        upload = upload_target(args.upload_url)
    req = media_providers.MediaRequest(
        kind=args.command, model=args.model, prompt=prompt, references=refs, last_frame=last, keyframes=keyframes,
        size=getattr(args, "size", None), quality=getattr(args, "quality", None),
        transparent=bool(getattr(args, "transparent", False)), resolution=args.resolution,
        aspect_ratio=getattr(args, "aspect_ratio", None), duration=getattr(args, "duration", None),
        upload_url=args.upload_url if upload else None,
        options=parse_provider_options(getattr(args, "provider_option", None)))
    notes = adapter.check(req)
    built = adapter.build(req, base)
    notes += built.warnings
    record = adapter.model_record(args.model) or {}
    plan = {"schemaVersion": 2, "provider": args.provider, "kind": args.command, "route": "rest",
            "apiBase": base, "endpoint": built.endpoint, "requestedModel": args.model, "options": built.options,
            "promptSha256": digest(prompt.encode()), "references": [m for m, _ in refs],
            "lastFrame": last[0] if last else None}
    if keyframes:
        plan["keyframes"] = [{**meta, "seconds": seconds} for (meta, _), seconds in keyframes]
    plan.update({"keyEnv": adapter.key_env, "paidRequests": 1, "automaticPostRetries": 0,
                 "outDir": str(Path(args.out_dir).resolve()), "execution": "execute" if args.execute else "dry-run",
                 "capability": {"verifiedAt": record.get("verifiedAt") or adapter.record["verifiedAt"],
                                "docs": record.get("docs") or adapter.record["docs"][:2],
                                "async": adapter.is_async(args.command, args.model)}})
    plan.update(built.extra)
    if upload:
        plan["uploadUrl"] = upload
    if notes:
        plan["notes"] = notes
    ref_hashes = [m["sha256"] for m, _ in refs] + ([last[0]["sha256"]] if last else []) + \
        [meta["sha256"] for (meta, _), _ in keyframes]
    plan["pricing"] = built.pricing
    plan["fingerprint"] = media_ledger.fingerprint(plan, ref_hashes)
    plan["estimate"] = media_ledger.estimate(plan, prices)
    plan["consent"] = {"provider": args.provider, "model": args.model, "calls": 1,
                       "estimateUsd": plan["estimate"]["usd"], "apiHost": parse.urlsplit(base).hostname}
    return plan, built.body, built.content_type, prompt


class Transport:
    """Real HTTPS transport; test fakes implement api() and download().

    Network boundary seams for tests: _https_handler() (the urllib HTTPS handler; a scripted
    handler replaces the network but keeps every other step real), _open() and _fetch()."""

    def _https_handler(self):
        return _ConnectTracker()

    def _open(self, req, timeout, limit):
        """Network boundary: return (headers, body, HTTP status) or raise NotSent when the
        connection failed before the request could be written."""
        tracker = self._https_handler()
        try:
            with request.build_opener(NoRedirect(), tracker).open(req, timeout=timeout) as response:
                return response.headers, response.read(limit + 1), getattr(response, "status", None) or response.getcode()
        except error.HTTPError:
            raise
        except error.URLError as exc:
            if not getattr(tracker, "connected", True):
                raise NotSent(_reason_text(exc.reason, req.host)) from None
            raise

    def _fetch(self, req, timeout, limit):
        """Media download boundary: follows at most MAX_REDIRECTS HTTPS redirects (SafeRedirect)."""
        with request.build_opener(SafeRedirect(), self._https_handler()).open(req, timeout=timeout) as response:
            return response.read(limit + 1)

    def api(self, method, url, key, body=None, content_type="application/json", timeout=90, meta=None):
        """One request without retries. meta (optional dict) may carry in clientRequestId, authHeader
        (default Authorization), authScheme (default Bearer; empty for a bare key such as
        x-goog-api-key), extraHeaders and free (a POST that costs nothing, such as an upload); it
        receives providerRequestId and httpStatus. The credential is an unredirected header."""
        meta = {} if meta is None else meta
        header = meta.get("authHeader") or "Authorization"
        scheme = meta.get("authScheme", "Bearer")
        headers = {"Content-Type": content_type, "User-Agent": USER_AGENT, **(meta.get("extraHeaders") or {})}
        if meta.get("clientRequestId"):
            headers["X-Client-Request-Id"] = meta["clientRequestId"]
        paid = method == "POST" and not meta.get("free")
        req = request.Request(url, data=body, method=method, headers=headers)
        req.add_unredirected_header(header, f"{scheme} {key}" if scheme else key)
        try:
            response_headers, data, status = self._open(req, timeout, API_LIMIT)
        except error.HTTPError as exc:
            # Only whitelisted, scrubbed fields; never a raw body, URL, prompt or credential.
            fields = provider_error_fields(exc.code, _error_body(exc), exc.headers, (key,))
            if "requestId" in fields:
                meta["providerRequestId"] = fields["requestId"]
            message = _http_message(fields)
            if paid and exc.code >= 500:
                message += "; the provider may still have processed it, so the outcome is unknown"
            raise MediaError(message, code=classify_http(exc.code, fields), sent=True, provider=fields) from None
        except NotSent as exc:
            raise MediaError(f"{exc}; the request was not sent", code="not_sent" if paid else "network", sent=False) from None
        except error.URLError as exc:
            if isinstance(exc.reason, (socket.gaierror, ConnectionRefusedError, str)):
                text = _reason_text(exc.reason, req.host) if not isinstance(exc.reason, str) else "Invalid request URL"
                raise MediaError(f"{text}; the request was not sent", code="not_sent" if paid else "network", sent=False) from None
            raise _lost(paid, f"Connection failed while sending ({type(exc.reason).__name__})") from None
        except (OSError, http.client.HTTPException) as exc:
            raise _lost(paid, f"No complete response after the request was sent ({type(exc).__name__})") from None
        meta["httpStatus"] = status
        rid = _header(response_headers, "x-request-id", "request-id", "x-fal-request-id")
        if _valid_id(rid, (key,)):
            meta["providerRequestId"] = rid
        if len(data) > API_LIMIT:
            raise MediaError("API response exceeds 100 MiB", code="bad_response")
        try:
            result = json.loads(data)
        except (ValueError, UnicodeError):
            raise MediaError("API returned invalid JSON; no request was retried", code="bad_response") from None
        if not isinstance(result, dict):
            raise MediaError("API returned an unexpected JSON shape", code="bad_response")
        return result

    def download(self, url, timeout=90, auth=None):
        """GET finished media. No credential is sent unless ``auth`` = (header, value) is given (a
        provider-hosted file, such as a Gemini file URI on the API host); it is an unredirected
        header, so a redirect to a storage host never receives it."""
        parsed = parse.urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise MediaError("Result download requires a public HTTPS URL without credentials", code="bad_response")
        req = request.Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
        if auth:
            req.add_unredirected_header(*auth)
        try:
            data = self._fetch(req, timeout, DOWNLOAD_LIMIT)
        except MediaError:
            raise
        except error.HTTPError as exc:
            raise MediaError(f"Media download failed (HTTP {exc.code}); a known job can be resumed", code="network") \
                from None
        except (OSError, http.client.HTTPException):
            raise MediaError("Media download failed; use resume for a known video request", code="network") from None
        if len(data) > DOWNLOAD_LIMIT:
            raise MediaError("Media exceeds the 256 MiB download limit", code="bad_response")
        return data


def _validate_artifact(content, kind):
    """Return (metadata, suffix) for returned media; writes nothing."""
    if kind == "image":
        try:
            with Image.open(io.BytesIO(content)) as img:
                fmt = img.format
                if fmt not in EXT:
                    raise MediaError("Unsupported returned image format", code="bad_response")
                meta = {"size": list(img.size), "format": fmt, "hasAlphaChannel": "A" in img.getbands()}
                img.verify()
        except (OSError, ValueError, Image.DecompressionBombError):
            raise MediaError("Returned image failed format validation", code="bad_response") from None
        return meta, EXT[fmt]
    if len(content) < 12 or content[4:8] != b"ftyp":
        raise MediaError("Returned video does not have an MP4 container header", code="bad_response")
    return {"validation": "container header only; run video2dsprite for decode and motion QA"}, ".mp4"


def _write_new(path, content):
    """Exclusive create + fsync; a half-written file of ours is removed on failure."""
    with path.open("xb") as handle:
        try:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            handle.close()
            path.unlink(missing_ok=True)
            raise


def _keep(out, content, sha, message, code="partial_artifact"):
    """Write returned bytes to a fresh .partial file; return the error to raise."""
    temporary = out / (".artifact-" + uuid.uuid4().hex + ".partial")
    try:
        _write_new(temporary, content)
    except OSError:
        return MediaError(message + "; the returned bytes could not be kept either", code=code, sent=True)
    return PartialArtifact(message, {"path": temporary.name, "sha256": sha, "bytes": len(content)}, code)


def write_artifact(out, content, kind, adopt_identical=False):
    """Validate and publish generated.<ext> without ever discarding paid bytes.

    The bytes first go to an exclusive temp file. Publication is a same-directory
    hard link (atomic, never replaces); where hard links are unsupported (exFAT,
    FAT32, some network or virtual drives) it is an exclusive create + fsync.
    The temp file is deleted only after the published file's sha256 matches;
    otherwise it is kept and PartialArtifact names it (job.json partialArtifact).
    """
    sha = digest(content)
    try:
        meta, suffix = _validate_artifact(content, kind)
    except MediaError as exc:
        if kind != "image":  # a video URL can be downloaded again by resume
            raise
        raise _keep(out, content, sha, str(exc), "bad_response") from None
    path = out / ("generated" + suffix)
    record = {"path": path.name, "bytes": len(content), "sha256": sha, **meta}
    # An interrupted video may have published its artifact before job.json was
    # committed. Adopt only byte-identical data returned for the same request ID.
    if path.exists():
        if adopt_identical and digest(path.read_bytes()) == sha:
            return record
        raise _keep(out, content, sha, "Artifact already exists and cannot be safely replaced")
    temporary = out / (".artifact-" + uuid.uuid4().hex + ".partial")
    try:
        _write_new(temporary, content)
    except OSError:
        raise MediaError("Could not write the returned media to disk", code="artifact_write", sent=True) from None
    partial = {"path": temporary.name, "sha256": sha, "bytes": len(content)}
    try:
        try:
            os.link(temporary, path)
        except FileExistsError:
            raise
        except OSError:
            # No hard links on this volume: exclusive create + fsync. Not atomic,
            # hence the sha256 check below before the temp copy is deleted.
            _write_new(path, content)
    except FileExistsError:
        if adopt_identical and digest(path.read_bytes()) == sha:
            temporary.unlink()
            return record
        raise PartialArtifact("Concurrent artifact publication conflict", partial) from None
    except OSError:
        raise PartialArtifact("Could not publish the artifact", partial) from None
    if digest(path.read_bytes()) != sha:
        path.unlink(missing_ok=True)
        raise PartialArtifact("Published artifact failed sha256 verification", partial)
    temporary.unlink()
    return record


def _returned_model(value, secrets):
    """The model id the provider reported (a result dict or the id itself), when it looks like one."""
    if isinstance(value, dict):
        value = value.get("model")
    return value if isinstance(value, str) and MODEL_ID.fullmatch(value) and not _secret_in(value, secrets) else None


def _duration(value):
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 3600
    return value if ok else None


def _usage(usage):
    """Whole-number token counts a provider reported (a usage dict, or a result holding "usage")."""
    if isinstance(usage, dict) and isinstance(usage.get("usage"), dict):
        usage = usage["usage"]
    if not isinstance(usage, dict):
        return None
    numbers = {k: v for k, v in usage.items()
               if re.fullmatch(r"[a-z_]{1,40}", str(k)) and isinstance(v, int) and not isinstance(v, bool) and v >= 0}
    return numbers or None


def _left(deadline, clock):
    return max(.1, min(90, deadline - clock()))


def _call(adapter, transport, method, url, key, body=None, content_type="application/json", timeout=90, meta=None):
    """One request with the provider's credential header; the adapter refines the outcome code of an
    HTTP error (a 5xx stays provider_error, so a possibly processed POST is never called refused)."""
    meta = {} if meta is None else meta
    meta.update(adapter.auth_meta())
    try:
        return transport.api(method, url, key, body, content_type, timeout=timeout, meta=meta)
    except MediaError as exc:
        fields = exc.provider if isinstance(exc.provider, dict) else None
        if fields and isinstance(fields.get("httpStatus"), int) and fields["httpStatus"] < 500:
            code = adapter.classify(fields["httpStatus"], fields)
            if code:
                exc.code = code
        raise


def _fetch_media(adapter, transport, ref, key, timeout):
    """The media bytes: inline, or downloaded at once (result URLs expire). The key goes only to a
    provider-hosted file on the API host, and never across a redirect."""
    if ref.data is not None:
        return ref.data
    if ref.auth:
        value = f"{adapter.auth_scheme} {key}" if adapter.auth_scheme else key
        return transport.download(ref.url, timeout=timeout, auth=(adapter.auth_header, value))
    return transport.download(ref.url, timeout=timeout)


def _keep_raw(folder, data):
    """Keep the clip exactly as downloaded (with its audio) beside the silent generated.mp4."""
    sha = digest(data)
    for name in (RAW_CLIP, f"provider-download-{sha[:12]}.mp4"):
        target = folder / name
        if target.exists():
            if digest(target.read_bytes()) == sha:
                return {"path": name, "sha256": sha, "bytes": len(data)}
            continue
        try:
            _write_new(target, data)
        except OSError:
            return None
        return {"path": name, "sha256": sha, "bytes": len(data)}
    return None


def _publish(folder, job, data, ref, secrets, adopt_identical=False):
    """Validate and publish the media into job["artifact"]. A clip is published without audio
    (media_mp4.remove_audio); the download itself is kept when that changed any byte."""
    kind = job.get("kind", "video")
    if kind == "video":
        silent, audio = media_mp4.remove_audio(data)
        if silent != data:
            audio["raw"] = _keep_raw(folder, data)
        job["audio"] = audio
        data = silent
    job["artifact"] = write_artifact(folder, data, kind, adopt_identical=adopt_identical)
    job["returnedModel"] = _returned_model(ref.returned_model, secrets)
    if kind == "video":
        job["durationReported"] = _duration(ref.duration)
    usage = _usage(ref.usage)
    if usage:
        job["providerUsage"] = usage
    if ref.cost_usd is not None:
        job["costUsd"] = ref.cost_usd


def _result(adapter, transport, url, key, timeout):
    """fal.ai: GET the result of a completed request. A 400 or 422 answer is the request's own final error."""
    try:
        return _call(adapter, transport, "GET", url, key, timeout=timeout)
    except MediaError as exc:
        status = exc.provider.get("httpStatus") if isinstance(exc.provider, dict) else None
        if exc.sent and status in (400, 422):
            raise JobFailure(f"{exc}", status="failed", code=exc.code, provider=exc.provider) from None
        raise


def poll_job(job_path, job, key, transport, timeout, interval, clock=None, sleep=None, *, adapter=None, api_base=None):
    """Poll a saved asynchronous job (GET only: a job is never submitted again) until its media is
    downloaded and published, it fails for good, or the time is up."""
    clock, sleep = clock or time.monotonic, sleep or time.sleep
    adapter = adapter or media_providers.adapter(job.get("provider") or "xai")
    base = api_base or adapter.api_base
    secrets = (key,)
    deadline = clock() + timeout
    if not _valid_id(job.get("requestId", ""), secrets):
        raise MediaError("No valid request ID; automatic resubmission is deliberately disabled", sent=False)
    while clock() < deadline:
        meta = {}
        result = _call(adapter, transport, "GET", adapter.status_url(job, base), key, timeout=_left(deadline, clock),
                       meta=meta)
        try:
            state, value = adapter.status(result, job, meta.get("httpStatus"))
            if state == "result":
                value = adapter.result(_result(adapter, transport, value, key, _left(deadline, clock)), job)
                state = "done"
        except JobFailure as exc:
            job["status"] = exc.status
            if exc.provider:
                job["error"] = exc.provider
            save_json(job_path, job, secrets)
            raise
        if state == "done":
            receipt = job.get("receipt")
            if isinstance(receipt, dict):
                receipt["providerMs"] = _ms_between(receipt.get("submittedAt"), _utcnow())
            job["status"] = "downloading"
            save_json(job_path, job, secrets)
            data = _fetch_media(adapter, transport, value, key, _left(deadline, clock))
            _publish(job_path.parent, job, data, value, secrets, adopt_identical=True)
            job["status"] = "done"
            save_json(job_path, job, secrets)
            return job
        job["status"] = "pending"
        save_json(job_path, job, secrets)
        sleep(min(interval, max(0, deadline - clock())))
    job["status"] = "pending_timeout"
    save_json(job_path, job, secrets)
    raise MediaError("Polling timeout; resume this job to poll again without a new paid request", code="pending_timeout")


def poll_video(job_path, job, key, transport, timeout, interval, clock=None, sleep=None, *, api_base=None):
    """The earlier name of poll_job: polls the provider recorded in job.json (xAI for old jobs)."""
    return poll_job(job_path, job, key, transport, timeout, interval, clock, sleep, api_base=api_base)


class _Run:
    """One job.json plus its ledger reservation, settled exactly when the outcome is known."""

    def __init__(self, path, job, secrets, ledger):
        self.path, self.job, self.secrets, self.ledger = path, job, secrets, ledger

    def save(self):
        save_json(self.path, self.job, self.secrets)

    def finish(self):
        for stale in ("error", "errorType"):  # left by an earlier interrupted attempt
            self.job.pop(stale, None)
        self._close("ok", "done")

    def fail(self, exc):
        """Map a failure to job status and ledger outcome (None keeps the
        reservation open while the provider job may still finish)."""
        job = self.job
        if isinstance(exc, MediaError):
            code, sent = exc.code, exc.sent
            if exc.provider:
                job["error"] = exc.provider
        else:
            code, sent = ("error" if isinstance(exc, Exception) else "interrupted"), None
        if isinstance(exc, PartialArtifact):
            job["partialArtifact"] = exc.partial
        job["errorType"] = type(exc).__name__
        status = job["status"]
        if status == "submitting":
            if sent is False:
                job["status"], outcome = "not_sent", "not_sent"
            elif sent is True and code != "provider_error":
                job["status"], outcome = "failed", "failed"
            else:  # the provider may have received it: keep the reservation counted
                job["status"], outcome = "submit_unknown", "unknown"
        elif status == "processing_result":  # the provider returned a result body
            job["status"] = "interrupted"
            outcome = "done" if code == "partial_artifact" else "unknown"
        elif status in ("failed", "expired"):  # provider-terminal video job
            outcome = "unknown" if code == "moderation" else "failed"
        elif status == "pending_timeout":
            outcome = None
        else:  # pending or downloading: the provider job lives on; resume finishes it
            job["status"], outcome = "interrupted", None
        try:
            self._close(code, outcome)
        except OSError:
            pass  # the caller re-raises the original error

    def _close(self, code, outcome):
        receipt = self.job.get("receipt")
        if isinstance(receipt, dict):
            receipt["outcomeCode"] = code
            if outcome is not None:
                now = _utcnow()
                receipt["completedAt"] = media_ledger.utc_timestamp(now)
                receipt["wallMs"] = _ms_between(receipt.get("startedAt"), now)
        info = self.job.get("ledger")
        if outcome is not None and self.ledger is not None and isinstance(info, dict) and info.get("reservationId"):
            job_id = self.job.get("requestId")
            artifact = self.job.get("artifact") if outcome == "done" else None
            try:
                self.ledger.commit(info["reservationId"], status=outcome,
                                   job_id=job_id if _valid_id(job_id, self.secrets) else None,
                                   sha256=(artifact or {}).get("sha256"),
                                   actual_usd=self.job.get("costUsd") if outcome == "done" else None)
                info["status"] = outcome
                info.pop("error", None)
            except (media_ledger.LedgerError, OSError, ValueError) as exc:
                info["error"] = f"{type(exc).__name__}: outcome not committed; settle it with media_ledger.py"
        self.save()


def dry_run_report(plan, args, ledger):
    """The plan plus read-only ledger facts. Writes nothing and needs no key."""
    report = dict(plan)
    warnings = list(plan.get("notes", []))
    if Path(args.out_dir).exists():
        warnings.append("output directory already exists; --execute will refuse it")
    states = ledger.entries()
    prior = ledger.find(plan["fingerprint"], ("done", "reserved", "unknown"), states=states)
    if prior is not None:
        warnings.append(str(media_ledger.DuplicateRequest(prior)) + (
            "; --allow-duplicate is set" if args.allow_duplicate else "; --execute refuses it without --allow-duplicate"))
    try:
        totals = ledger.check_caps(args.budget_usd, args.max_calls, next_usd=plan["estimate"]["usd"], states=states)
    except media_ledger.LedgerError as exc:
        totals = ledger.totals(states)
        warnings.append(f"--execute would be refused: {exc}")
    report["ledger"] = {"path": ledger.path.as_posix(), **{k: totals[k] for k in ("calls", "paidCalls", "usd", "unpricedCalls")}}
    report["warnings"] = warnings
    return report


def execute(args, transport=None):
    transport = transport or Transport()
    plan, body, content_type, prompt = prepare(args)
    ledger = media_ledger.Ledger(args.project_dir)
    if not args.execute:
        return dry_run_report(plan, args, ledger)
    key = media_config.api_key(args.provider) or ""
    if not key:
        raise MediaError(f"Missing {KEY[args.provider]} (environment or the user config file); no request sent",
                         code="no_key", sent=False)
    if _secret_in(prompt, (key,)):
        raise MediaError("The prompt file contains the API key; nothing was sent or written", code="invalid_input", sent=False)
    out = Path(args.out_dir)
    # A new directory is the job reservation. Existing results are never reused
    # as an excuse to submit another paid POST.
    if out.exists():
        raise MediaError("Output already exists; choose a new job directory or use resume", code="output_exists", sent=False)
    started = _utcnow()
    attempt = 1 + sum(1 for s in ledger.entries().values()
                      if s.get("fingerprint") == plan["fingerprint"] and s["status"] != "not_sent")
    entry = {"jobDir": _portable(out, args.project_dir), "fingerprint": plan["fingerprint"], "provider": plan["provider"],
             "model": plan["requestedModel"], "kind": plan["kind"], "route": "rest",
             "reservedUsd": plan["estimate"]["usd"], "quotaCall": False}
    try:
        reservation = ledger.reserve(entry, budget_usd=args.budget_usd, max_calls=args.max_calls,
                                     refuse_duplicate=not args.allow_duplicate)
    except media_ledger.DuplicateRequest as exc:
        raise MediaError(f"{exc}; reuse it, or pass --allow-duplicate to pay for the same request again",
                         code="duplicate", sent=False) from None
    except media_ledger.CapExceeded as exc:
        raise MediaError(f"{exc}; nothing was sent", code="cap", sent=False) from None
    except media_ledger.LedgerError as exc:
        raise MediaError(f"{exc}; nothing was sent", code="ledger", sent=False) from None
    job = {**plan, "status": "submitting", "clientRequestId": uuid.uuid4().hex,
           "receipt": {"startedAt": media_ledger.utc_timestamp(started), "submittedAt": None, "completedAt": None,
                       "wallMs": None, "providerMs": None, "attempt": attempt, "purpose": args.purpose,
                       "toolVersion": TOOL, "outcomeCode": None},
           "ledger": {"projectDir": str(Path(args.project_dir).resolve()), "reservationId": reservation, "status": "reserved"}}
    run = _Run(out / "job.json", job, (key,), ledger)
    try:
        out.mkdir(parents=True, exist_ok=False)
        (out / "prompt.txt").write_text(prompt + "\n", encoding="utf-8")
        run.save()
    except BaseException as exc:
        try:
            ledger.commit(reservation, status="not_sent")
        except (media_ledger.LedgerError, OSError):
            pass  # the reservation stays held rather than masking the original error
        if isinstance(exc, FileExistsError):
            raise MediaError("Output already exists; choose a new job directory or use resume",
                             code="output_exists", sent=False) from None
        raise
    return _submit(run, args, transport, body, content_type, key)


def _upload_inputs(adapter, transport, key, deferred, timeout):
    """fal.ai: upload the input images to the fal CDN (free requests, as the official fal_client does)
    and return the request body with their URLs. Any failure here means the paid request was never sent."""
    try:
        token = transport.api("POST", adapter.TOKEN_URL, key, b"{}", "application/json", timeout=timeout,
                              meta={**adapter.auth_meta(), "free": True})
        value, scheme = token.get("token"), token.get("token_type")
        if not isinstance(value, str) or len(value) < 8 or not isinstance(scheme, str) \
                or not re.fullmatch(r"[A-Za-z]{1,20}", scheme):
            raise MediaError("the fal.ai storage token answer is incomplete", code="bad_response")
        urls = {}
        for placeholder, meta, data, name in deferred.uploads:
            reply = transport.api("POST", adapter.UPLOAD_URL, value, data, meta["mime"], timeout=timeout,
                                  meta={"authScheme": scheme, "extraHeaders": {"X-Fal-File-Name": name}, "free": True})
            url = reply.get("access_url")
            if not isinstance(url, str) or parse.urlsplit(url).scheme != "https":
                raise MediaError("the fal.ai upload answer has no HTTPS access_url", code="bad_response")
            urls[placeholder] = url
    except MediaError as exc:
        code = exc.code if exc.code in ("auth", "quota", "rate_limit", "entitlement") else "not_sent"
        if exc.code == "invalid_request" and isinstance(exc.provider, dict):
            code = adapter.classify(exc.provider.get("httpStatus", 0), exc.provider) or code
        raise MediaError(f"fal.ai input upload failed before the paid request: {exc}", code=code, sent=False,
                         provider=exc.provider) from None
    return deferred.render(urls)


def _submit(run, args, transport, body, content_type, key):
    """The one paid POST, then (asynchronous providers) the job id is saved and polled, or (synchronous
    images) the answer is published. Nothing is ever sent twice."""
    job = run.job
    adapter = media_providers.adapter(job["provider"])
    is_async = adapter.is_async(job["kind"], job["requestedModel"])
    meta = {"clientRequestId": job["clientRequestId"]}
    try:
        if isinstance(body, media_providers.DeferredBody):
            body = _upload_inputs(adapter, transport, key, body, min(args.timeout, args.submit_timeout))
        job["receipt"]["submittedAt"] = media_ledger.utc_timestamp()
        run.save()
        started = time.monotonic()
        try:
            result = _call(adapter, transport, "POST", job["endpoint"], key, body, content_type,
                           timeout=min(args.timeout, args.submit_timeout), meta=meta)
        except MediaError as exc:
            if exc.sent and not is_async:  # the provider answered: time it, success or not
                job["receipt"]["providerMs"] = round((time.monotonic() - started) * 1000)
            raise
        if meta.get("providerRequestId"):
            job["providerRequestId"] = meta["providerRequestId"]
        adapter.refusal(result)  # a 200 answer that withholds the media (moderation): a clean, final failure
        if is_async:
            handle = adapter.read_submit(result, job["kind"])
            if not _valid_id(handle.id, run.secrets):
                raise MediaError("Submit response omitted a valid request ID; check provider history before any "
                                 "new submission", code="bad_response")
            job.update(status="pending", requestId=handle.id, **handle.extra)
            run.save()  # the job id is in job.json before the first poll; the ledger line gets it on commit
            poll_job(run.path, job, key, transport, args.timeout, args.poll_interval, adapter=adapter,
                     api_base=job["apiBase"])
        else:  # an asynchronous job's providerMs is submit -> completion, set by poll_job
            job["receipt"]["providerMs"] = round((time.monotonic() - started) * 1000)
            job["status"] = "processing_result"
            run.save()
            ref = adapter.read_submit(result, job["kind"])
            content = _fetch_media(adapter, transport, ref, key, max(.1, min(90, args.timeout)))
            _publish(run.path.parent, job, content, ref, run.secrets)
            job["status"] = "done"
        run.finish()
        return job
    except BaseException as exc:
        if meta.get("providerRequestId"):
            job["providerRequestId"] = meta["providerRequestId"]
        run.fail(exc)
        raise


def resume(args, transport=None):
    """Poll and download a saved asynchronous job (xAI and Seedance video, every fal.ai request) without
    a new paid request."""
    bounded_time(args)
    path = Path(args.job)
    job = json.loads(path.read_text(encoding="utf-8-sig"))
    version = job.get("schemaVersion")
    provider = job.get("provider")
    if version not in (1, 2) or provider not in media_providers.ORDER or job.get("kind") not in ("image", "video") \
            or (version == 1 and (provider != "xai" or job.get("kind") != "video")):
        raise MediaError("Only known API jobs can be resumed")
    adapter = media_providers.adapter(provider)
    if not adapter.is_async(job["kind"], job.get("requestedModel")):
        raise MediaError(f"{adapter.label} answers {job['kind']} requests at once; there is no job to resume")
    default = adapter.api_base
    base = default if version == 1 else checked_base_url(job.get("apiBase") or default)
    if base != default and not args.allow_custom_base_url:
        raise MediaError(f"This job uses a custom API base URL ({parse.urlsplit(base).hostname}); "
                         f"pass --allow-custom-base-url to send {adapter.key_env} there", code="invalid_input", sent=False)
    status = job.get("status")
    if status == "done":
        artifact = job.get("artifact", {})
        name = artifact.get("path", "")
        target = path.parent / name if isinstance(name, str) and ARTIFACT_NAME.fullmatch(name) else None
        if target is None or not target.is_file() or digest(target.read_bytes()) != artifact.get("sha256"):
            raise MediaError("Completed artifact is missing or changed; no regeneration attempted")
        return job
    if status in ("failed", "expired"):
        raise MediaError("This job is terminal; no resubmission attempted")
    if status == "not_sent":
        raise MediaError("This job was never sent; run the original command again with a new --out-dir", sent=False)
    if not _valid_id(job.get("requestId")):
        raise MediaError("No valid request ID; automatic resubmission is deliberately disabled", sent=False)
    key = media_config.api_key(provider) or ""
    if not key:
        raise MediaError(f"Missing {adapter.key_env} (environment or the user config file)", code="no_key", sent=False)
    transport = transport or Transport()
    if version == 1:
        return poll_job(path, job, key, transport, args.timeout, args.poll_interval, adapter=adapter)
    project = args.project_dir or (job.get("ledger") or {}).get("projectDir")
    run = _Run(path, job, (key,), media_ledger.Ledger(project) if project else None)
    try:
        poll_job(path, job, key, transport, args.timeout, args.poll_interval, adapter=adapter, api_base=base)
        run.finish()
        return job
    except BaseException as exc:
        run.fail(exc)
        raise


class _JobArgsParser(argparse.ArgumentParser):
    """Raises instead of exiting so one malformed job is reported, not fatal mid-batch."""

    def error(self, message):
        raise MediaError("invalid job options: " + message, code="invalid_input", sent=False)


def load_jobs(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except ValueError:
        raise MediaError("Batch file is not valid JSON") from None
    jobs = data.get("jobs") if isinstance(data, dict) else data
    if not isinstance(jobs, list) or not jobs:
        raise MediaError('Batch file needs a non-empty list of jobs or {"jobs": [...]}')
    seen = set()
    for index, spec in enumerate(jobs):
        if not isinstance(spec, dict) or not isinstance(spec.get("id"), str) or not JOB_ID.fullmatch(spec["id"]):
            raise MediaError(f"Job {index} needs an id of 1-80 letters, digits, dots, dashes or underscores")
        if spec["id"] in seen:
            raise MediaError(f"Duplicate job id {spec['id']}")
        seen.add(spec["id"])
        if spec.get("command") not in ("image", "video"):
            raise MediaError(f"Job {spec['id']}: command must be image or video")
    return jobs


def job_argv(spec, base, args):
    """Translate one job object into image/video arguments. Keys are the CLI
    option names (snake_case or kebab-case); paths resolve against base."""
    argv = [spec["command"]]
    for raw, value in spec.items():
        if raw in ("id", "command"):
            continue
        key = raw.replace("-", "_")
        if key in BATCH_LEVEL_KEYS:
            raise MediaError(f"{raw} is set once for the whole batch, not per job")
        flag = "--" + key.replace("_", "-")
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, bool):
                if item:
                    argv.append(flag)
            elif isinstance(item, (str, int, float)):
                if key == "keyframe":  # PATH@SECONDS: only the path is relative to the jobs file
                    path, seconds = parse_keyframe(item)
                    argv += [flag, f"{base / path}@{seconds:g}"]
                else:
                    argv += [flag, str(base / str(item)) if key in JOB_PATH_KEYS else str(item)]
            elif item is not None:
                raise MediaError(f"{raw} must be a string, number, boolean or list")
    if "purpose" not in spec:
        argv += ["--purpose", "batch job " + spec["id"]]
    argv += ["--project-dir", args.project_dir]
    for name in ("budget_usd", "max_calls", "prices"):
        if getattr(args, name) is not None:
            argv += ["--" + name.replace("_", "-"), str(getattr(args, name))]
    for name in ("allow_duplicate", "allow_custom_base_url", "execute"):
        if getattr(args, name):
            argv.append("--" + name.replace("_", "-"))
    return argv


def prior_state(out):
    """('reused', status) for a verified done job folder, else ('prior', status)."""
    try:
        job = json.loads((out / "job.json").read_text(encoding="utf-8-sig"))
        artifact = job.get("artifact") or {}
        name = artifact.get("path")
        if (job.get("status") == "done" and isinstance(name, str) and ARTIFACT_NAME.fullmatch(name)
                and (out / name).is_file() and digest((out / name).read_bytes()) == artifact.get("sha256")):
            return "reused", "done"
        return "prior", job.get("status")
    except (OSError, ValueError, AttributeError):
        return "prior", None


def _batch_entries(args):
    jobs_path = Path(args.jobs)
    base = jobs_path.resolve().parent
    entries, outputs, prints = [], {}, {}
    for spec in load_jobs(jobs_path):
        try:
            job_args = parser(_JobArgsParser).parse_args(job_argv(spec, base, args))
            plan = prepare(job_args)[0]
        except MediaError as exc:
            raise MediaError(f"Job {spec['id']}: {exc}", code=exc.code, sent=False) from None
        out = str(Path(job_args.out_dir).resolve())
        if out in outputs:
            raise MediaError(f"Jobs {outputs[out]} and {spec['id']} share an output directory")
        outputs[out] = spec["id"]
        if plan["fingerprint"] in prints and not args.allow_duplicate:
            raise MediaError(f"Jobs {prints[plan['fingerprint']]} and {spec['id']} are identical requests; "
                             "remove one or pass --allow-duplicate")
        prints[plan["fingerprint"]] = spec["id"]
        entries.append((spec["id"], job_args, plan))
    return jobs_path, entries


def _batch_preview(jobs_path, entries, ledger, args):
    rows, total, unpriced = [], 0.0, 0
    for job_id, job_args, plan in entries:
        out = Path(job_args.out_dir)
        if out.exists():
            state, prior = prior_state(out)
            rows.append({"id": job_id, "action": "reuse" if state == "reused" else "leave-for-human", "priorStatus": prior})
            continue
        duplicate = ledger.find(plan["fingerprint"], ("done", "reserved", "unknown"))
        if duplicate is not None and not args.allow_duplicate:
            rows.append({"id": job_id, "action": "refuse", "reason": str(media_ledger.DuplicateRequest(duplicate))})
            continue
        usd = plan["estimate"]["usd"]
        rows.append({"id": job_id, "action": "send", "provider": plan["provider"], "model": plan["requestedModel"],
                     "calls": 1, "estimateUsd": usd, "basis": plan["estimate"]["basis"]})
        unpriced += usd is None
        total += usd or 0.0
    calls = sum(r["action"] == "send" for r in rows)
    totals = ledger.totals()
    warnings = []
    try:
        paid_cap = media_ledger.max_paid_requests()
    except media_ledger.LedgerError as exc:
        paid_cap = None
        warnings.append(f"--execute would be refused: {exc}")
    if paid_cap is not None and totals["paidCalls"] + calls > paid_cap:
        warnings.append(f"{media_ledger.MAX_PAID_ENV}={paid_cap} stops the batch after {max(0, paid_cap - totals['paidCalls'])} calls")
    if args.max_calls is not None and totals["calls"] + calls > args.max_calls:
        warnings.append(f"--max-calls {args.max_calls} stops the batch after {max(0, args.max_calls - totals['calls'])} calls")
    if args.budget_usd is not None and (unpriced or totals["usd"] + total > args.budget_usd + 1e-9):
        warnings.append(f"--budget-usd {args.budget_usd} cannot cover every job (recorded {totals['usd']} USD, "
                        f"this batch {round(total, 6)} USD, {unpriced} unpriced)")
    return {"execution": "dry-run", "jobsFile": str(jobs_path.resolve()), "jobs": len(entries), "calls": calls,
            "estimateUsd": None if unpriced else round(total, 6), "pricedUsd": round(total, 6),
            "unpricedCalls": unpriced, "consent": rows,
            "ledger": {"path": ledger.path.as_posix(), "calls": totals["calls"], "usd": totals["usd"]}, "warnings": warnings}


def _batch_job(job_id, job_args, transport_factory, job_dir):
    """One job's progress result; job_dir is its folder relative to the progress file (D25)."""
    out = Path(job_args.out_dir)
    if out.exists():  # earlier successes are reused; earlier failures are left for a human
        state, prior = prior_state(out)
        if state == "reused":
            return {"id": job_id, "status": "reused", "outcomeCode": "ok", "jobDir": job_dir}
        return {"id": job_id, "status": "left-for-human", "priorStatus": prior, "outcomeCode": "prior", "jobDir": job_dir}
    try:
        job = execute(job_args, transport_factory())
    except MediaError as exc:
        return {"id": job_id, "status": "failed", "outcomeCode": exc.code, "jobDir": job_dir, "error": str(exc)}
    except Exception as exc:
        return {"id": job_id, "status": "failed", "outcomeCode": "error", "jobDir": job_dir,
                "error": f"unexpected {type(exc).__name__}"}
    return {"id": job_id, "status": "generated", "outcomeCode": "ok", "jobDir": job_dir,
            "artifact": job["artifact"]["path"], "estimateUsd": job["estimate"]["usd"]}


def _progress_relative(path, folder):
    """path relative to the progress file's folder: progress files hold no absolute path (D25)."""
    try:
        return Path(os.path.relpath(Path(path).resolve(), folder)).as_posix()
    except ValueError:
        raise MediaError("The progress file, the jobs file and every output directory must be on one drive "
                         "(progress files record relative paths)", code="invalid_input", sent=False) from None


def run_batch(args, transport=None):
    """Validate every job, then dry-run (default) or execute them with at most two
    workers. Never retries; stops dispatching on account-level or unknown
    outcomes. Returns (summary, problem message or None)."""
    jobs_path, entries = _batch_entries(args)
    ledger = media_ledger.Ledger(args.project_dir)
    if not args.execute:
        return _batch_preview(jobs_path, entries, ledger, args), None
    progress_path = Path(args.progress) if args.progress else jobs_path.with_name(jobs_path.stem + ".progress.json")
    folder = progress_path.resolve().parent
    job_dirs = {job_id: _progress_relative(job_args.out_dir, folder) for job_id, job_args, _ in entries}
    folder.mkdir(parents=True, exist_ok=True)  # --progress may name a folder that does not exist yet
    factory = (lambda: transport) if transport is not None else Transport
    queue = list(entries)
    lock = threading.Lock()
    state = {"schema": BATCH_PROGRESS_SCHEMA, "jobsFile": _progress_relative(jobs_path, folder), "execution": "execute",
             "workers": args.workers, "startedAt": media_ledger.utc_timestamp(), "updatedAt": None,
             "results": [], "inFlight": [], "remaining": [e[0] for e in entries],
             "stopped": False, "stopReason": None, "complete": False}

    def write_progress(final=False):
        state["updatedAt"] = media_ledger.utc_timestamp()
        try:
            save_json(progress_path, state, _env_secrets())
        except OSError:
            if final:  # job folders and the ledger still hold the truth
                raise

    def worker():
        while True:
            with lock:
                if state["stopped"] or not queue:
                    return
                job_id, job_args, _ = queue.pop(0)
                state["remaining"].remove(job_id)
                state["inFlight"].append(job_id)
                write_progress()
            result = _batch_job(job_id, job_args, factory, job_dirs[job_id])
            with lock:
                state["inFlight"].remove(job_id)
                state["results"].append(result)
                if result["outcomeCode"] not in CONTINUE_CODES and not state["stopped"]:
                    state["stopped"], state["stopReason"] = True, f"{job_id}: {result['outcomeCode']}"
                write_progress()

    write_progress(final=True)
    threads = [threading.Thread(target=worker, name=f"forge-batch-{i}") for i in range(args.workers)]
    for thread in threads:
        thread.start()
    try:
        _join(threads)
    except KeyboardInterrupt:  # finish in-flight jobs, dispatch nothing new
        with lock:
            state["stopped"], state["stopReason"] = True, state["stopReason"] or "interrupted by the user"
        _join(threads)
    state["complete"] = not state["remaining"] and not state["inFlight"]
    write_progress(final=True)
    good = sum(r["status"] in ("generated", "reused") for r in state["results"])
    summary = {**state, "progressFile": str(progress_path.resolve())}
    if good == len(entries):
        return summary, None
    stop = f"; stopped at {state['stopReason']}" if state["stopped"] else ""
    return summary, (f"batch incomplete: {good} of {len(entries)} jobs generated or reused{stop}; "
                     f"see {progress_path.as_posix()}")


def _join(threads):
    for thread in threads:
        while thread.is_alive():
            thread.join(0.2)  # a timed join lets Ctrl+C through on Windows


def _add_job_options(c, batch=False):
    c.add_argument("--project-dir", default=".", help="Project root; the spend ledger is <project-dir>/.forge/ledger.jsonl (default: current directory)")
    c.add_argument("--budget-usd", type=float, help="Refuse to send when recorded + held + estimated USD in this project's ledger would exceed this")
    c.add_argument("--max-calls", type=int, help="Refuse to send when this project's ledger already holds this many paid or quota calls")
    c.add_argument("--allow-duplicate", action="store_true", help="Send even if an identical request already succeeded or is unsettled")
    c.add_argument("--prices", help="prices_v1 JSON used for estimates (default: references/prices.json of this skill)")
    c.add_argument("--allow-custom-base-url", action="store_true", help="Confirm that a --base-url host may receive the API key")
    if not batch:
        c.add_argument("--base-url", help="https API base URL replacing the provider default; needs --allow-custom-base-url")
        c.add_argument("--purpose", help="Free text for the job receipt (at most 200 characters)")


def parser(parser_class=argparse.ArgumentParser):
    p = parser_class(description=__doc__)
    modes = p.add_subparsers(dest="command", required=True)
    for name in ("image", "video"):
        c = modes.add_parser(name, help=f"Plan (default) or send one {name} request")
        c.add_argument("--provider", choices=media_providers.providers_for(name), required=True,
                       help="API provider: " + ", ".join(media_providers.providers_for(name)))
        c.add_argument("--model", required=True, help="the provider's model id (fal.ai: the endpoint id); "
                                                      "references/capabilities.json lists the verified ones")
        c.add_argument("--prompt-file", required=True)
        c.add_argument("--reference", action="append", default=[])
        c.add_argument("--out-dir", required=True)
        c.add_argument("--execute", action="store_true", help="Send one paid API request; default is dry-run")
        c.add_argument("--timeout", type=float, default=600, help="Polling budget in seconds (1-3600); also caps the submit timeout")
        c.add_argument("--submit-timeout", type=float, default=300, help="Seconds to wait for the paid POST response (1-600, default 300)")
        c.add_argument("--poll-interval", type=float, default=5)
        c.add_argument("--provider-option", action="append", default=[], metavar="KEY=VALUE",
                       help="extra request field passed to the provider as is (repeatable; dotted keys nest; VALUE is "
                            "JSON when it parses, else text); fields ASF sets itself are refused")
        _add_job_options(c)
        if name == "image":
            c.add_argument("--size", help="WIDTHxHEIGHT or auto (OpenAI, Gemini, BytePlus, fal.ai)")
            c.add_argument("--resolution", choices=("512", "1k", "1.5k", "2k", "4k"),
                           help="xAI resolution (1k, 1.5k, 2k) or Gemini image size (512, 1k, 2k, 4k)")
            c.add_argument("--aspect-ratio", choices=("1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3"))
            c.add_argument("--quality", choices=("auto", "low", "medium", "high", "xhigh", "max"))
            c.add_argument("--transparent", action="store_true", help="native transparent background (OpenAI GPT Image)")
        else:
            c.add_argument("--duration", type=int, default=4)
            c.add_argument("--resolution", choices=("480p", "720p", "1080p"), default="720p")
            c.add_argument("--last-frame", help="pin the end frame (same canvas size as --reference)")
            c.add_argument("--keyframe", action="append", default=[], metavar="PATH@SECONDS",
                           help="xAI grok-imagine-video-1.5: an intermediate frame at a time, up to 4 (repeatable)")
            c.add_argument("--upload-url", help="xAI zero-data-retention upload URL, sent as output.upload_url; stored only as host + sha256")
    r = modes.add_parser("resume", help="Poll a saved asynchronous job (video, fal.ai); never submits generation")
    r.add_argument("--job", required=True)
    r.add_argument("--timeout", type=float, default=600)
    r.add_argument("--poll-interval", type=float, default=5)
    r.add_argument("--project-dir", help="Project whose ledger holds the reservation (default: the one recorded in job.json)")
    r.add_argument("--allow-custom-base-url", action="store_true", help="Confirm that the job's custom API host may receive the key")
    b = modes.add_parser("batch", help="Dry-run (default) or execute a jobs file; never retries")
    b.add_argument("jobs", help='JSON list of jobs (or {"jobs": [...]}); each has id, command and CLI option keys; paths are relative to the file')
    b.add_argument("--workers", type=int, choices=(1, 2), default=1, help="Concurrent jobs (1 or 2)")
    b.add_argument("--execute", action="store_true", help="Send the jobs; default is a dry-run consent list")
    b.add_argument("--progress", help="Progress file (default: <jobs stem>.progress.json beside the jobs file)")
    _add_job_options(b, batch=True)
    return p


def _emit(result):
    print(redact(json.dumps(result, ensure_ascii=True), _env_secrets()))


def _error(message):
    print("error: " + _ascii(redact(message, _env_secrets())), file=sys.stderr)


def main(argv=None, transport=None):
    """CLI entry point; transport is injectable for offline tests. Usage errors exit 2 (argparse,
    D26); failures print one ``error: ...`` line and exit 1 (130 when one request is interrupted);
    anything unexpected prints ``error: internal error (<Type>: <message>)`` (D27)."""
    media_ledger._local_utf8_stdio()
    args = parser().parse_args(argv)
    try:
        if args.command == "batch":
            summary, problem = run_batch(args, transport)
            _emit(summary)
            if problem:
                _error(problem)
                return 1
            return 0
        _emit(resume(args, transport) if args.command == "resume" else execute(args, transport))
        return 0
    except MediaError as exc:
        _error(str(exc))
    except KeyboardInterrupt:
        _error("interrupted; nothing was retried, and job.json and the ledger record the state")
        return 130
    except (OSError, ValueError) as exc:
        # Messages of local errors may include paths; report the type only.
        _error(type(exc).__name__ + ": local input/output failed")
    except Exception as exc:  # D27: never a traceback; the message is scrubbed (secrets, URLs, blobs)
        _error(f"internal error ({type(exc).__name__}: {scrub(exc, _env_secrets())}); nothing was retried")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
