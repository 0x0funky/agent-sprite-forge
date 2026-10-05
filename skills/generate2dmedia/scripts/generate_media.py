#!/usr/bin/env python3
"""One-request media API adapter. Dry-run by default; no paid POST retries.

Provider contracts checked 2026-10-05. Generation is separate from asset QA.
Uses stdlib HTTP and Pillow (already a Forge dependency).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from urllib import error, parse, request
import uuid

from PIL import Image

API = {"openai": "https://api.openai.com/v1", "xai": "https://api.x.ai/v1"}
KEY = {"openai": "OPENAI_API_KEY", "xai": "XAI_API_KEY"}
MIME = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
EXT = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}


class MediaError(Exception):
    pass


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise MediaError("API redirect refused; credentials were not forwarded")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def save_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


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


def data_uri(meta, data):
    return "data:" + meta["mime"] + ";base64," + base64.b64encode(data).decode("ascii")


def multipart(fields, refs):
    boundary = "forge-" + uuid.uuid4().hex
    chunks = []
    for name, value in fields.items():
        chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n').encode())
    for i, (meta, data) in enumerate(refs):
        suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[meta["mime"]]
        # Synthetic filename avoids path disclosure and multipart header injection.
        chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="image[]"; filename="reference-{i}{suffix}"\r\nContent-Type: {meta["mime"]}\r\n\r\n').encode())
        chunks.extend([data, b"\r\n"])
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), "multipart/form-data; boundary=" + boundary


def bounded_time(args):
    for name, minimum, maximum in (("timeout", 1, 3600), ("poll_interval", 1, 60)):
        value = getattr(args, name)
        if not math.isfinite(value) or not minimum <= value <= maximum:
            raise MediaError(f"{name} must be within {minimum}..{maximum} seconds")


def prepare(args):
    """Validate without keys, output writes, or network; return plan + transport."""
    bounded_time(args)
    prompt = Path(args.prompt_file).read_text(encoding="utf-8-sig").strip()
    if not prompt or len(prompt) > 30000:
        raise MediaError("Prompt must contain 1..30000 characters")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", args.model):
        raise MediaError("Specify a concrete provider model identifier")
    refs = [inspect_image(p) for p in args.reference]
    max_refs = 16 if args.provider == "openai" and args.command == "image" else 1
    if len(refs) > max_refs:
        raise MediaError(f"This provider/mode supports at most {max_refs} references in this adapter")
    if sum(len(data) for _, data in refs) > 40 * 1024 * 1024:
        raise MediaError("References exceed this adapter's 40 MiB combined upload limit")
    fields = {"model": args.model, "prompt": prompt}
    last = None
    if args.command == "video":
        if len(refs) != 1:
            raise MediaError("Video requires one approved --reference image")
        if not 1 <= args.duration <= 15:
            raise MediaError("Video duration must be 1..15 seconds")
        if args.model not in ("grok-imagine-video", "grok-imagine-video-1.5", "grok-imagine-video-1.5-lite"):
            raise MediaError("Unsupported video model; verify capabilities before adding another model")
        if args.resolution == "1080p" and args.model == "grok-imagine-video":
            raise MediaError("Classic grok-imagine-video supports 480p/720p")
        fields.update(image={"url": data_uri(*refs[0])}, duration=args.duration, resolution=args.resolution)
        # Silent generation is documented for 1.5, not assumed for every model.
        if args.model == "grok-imagine-video-1.5":
            fields["generate_audio"] = False
        if args.last_frame:
            if args.model != "grok-imagine-video-1.5" or args.resolution == "1080p":
                raise MediaError("Last-frame pinning is limited here to video-1.5 at 480p/720p")
            last = inspect_image(args.last_frame)
            if last[0]["size"] != refs[0][0]["size"]:
                raise MediaError("First and last frames must have the same canvas size")
            fields["last_frame"] = {"url": data_uri(*last)}
        endpoint = API[args.provider] + "/videos/generations"
    else:
        fields["n"] = 1
        endpoint = API[args.provider] + ("/images/edits" if refs else "/images/generations")
        if args.provider == "openai":
            if args.resolution or args.aspect_ratio:
                raise MediaError("OpenAI uses --size; --resolution/--aspect-ratio are xAI image options")
            if args.size and not re.fullmatch(r"auto|[1-9]\d{1,4}x[1-9]\d{1,4}", args.size):
                raise MediaError("Size must be auto or WIDTHxHEIGHT; provider validates model-specific limits")
            fields["output_format"] = "png"
            if args.size:
                fields["size"] = args.size
            if args.transparent:
                fields["background"] = "transparent"
        else:
            if args.transparent or args.size:
                raise MediaError("xAI has no transparency switch here; use a keyed backdrop and --resolution")
            if args.quality and (args.model != "grok-imagine-image-2.0" or args.quality == "high"):
                raise MediaError("xAI quality option requires image-2.0 and low/medium/auto")
            fields["response_format"] = "b64_json"
            if args.resolution:
                fields["resolution"] = args.resolution
            if args.aspect_ratio:
                fields["aspect_ratio"] = args.aspect_ratio
            if refs:
                fields["image"] = {"url": data_uri(*refs[0]), "type": "image_url"}
        if args.quality:
            fields["quality"] = args.quality
    options = {k: v for k, v in fields.items() if k not in ("image", "last_frame", "prompt")}
    plan = {"schemaVersion": 1, "provider": args.provider, "kind": args.command,
            "endpoint": endpoint, "requestedModel": args.model, "options": options,
            "promptSha256": digest(prompt.encode()), "references": [m for m, _ in refs],
            "lastFrame": last[0] if last else None, "keyEnv": KEY[args.provider],
            "paidRequests": 1, "automaticPostRetries": 0,
            "outDir": str(Path(args.out_dir).resolve()), "execution": "execute" if args.execute else "dry-run"}
    if args.provider == "openai" and refs:
        body, content_type = multipart(fields, refs)
    else:
        body, content_type = json.dumps(fields).encode(), "application/json"
    return plan, body, content_type, prompt


class Transport:
    def api(self, method, url, key, body=None, content_type="application/json", timeout=90):
        req = request.Request(url, data=body, method=method, headers={
            "Authorization": "Bearer " + key, "Content-Type": content_type,
            "User-Agent": "agent-sprite-forge-media/1"})
        try:
            with request.build_opener(NoRedirect()).open(req, timeout=timeout) as response:
                data = response.read(100 * 1024 * 1024 + 1)
            if len(data) > 100 * 1024 * 1024:
                raise MediaError("API response exceeds 100 MiB")
            result = json.loads(data)
            if not isinstance(result, dict):
                raise MediaError("API returned an unexpected JSON shape")
            return result
        except error.HTTPError as exc:
            # Do not echo a provider body, URL, prompt, reference or credentials.
            raise MediaError(f"API HTTP {exc.code}; request was not retried") from None
        except (error.URLError, TimeoutError, OSError):
            raise MediaError("Network failure; outcome may be unknown; no paid request was retried") from None
        except (ValueError, UnicodeError):
            raise MediaError("API returned invalid JSON; no request was retried") from None

    def download(self, url, timeout=90):
        parsed = parse.urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise MediaError("Result download requires a public HTTPS URL without credentials")
        # No authorization header is sent to the media host. Reject redirects:
        # the returned signed URL should already identify the final artifact.
        try:
            with request.build_opener(NoRedirect()).open(request.Request(url), timeout=timeout) as response:
                data = response.read(256 * 1024 * 1024 + 1)
            if len(data) > 256 * 1024 * 1024:
                raise MediaError("Media exceeds the 256 MiB download limit")
            return data
        except (error.URLError, OSError, TimeoutError):
            raise MediaError("Media download failed; use resume for a known video request") from None


def write_artifact(out, content, kind, adopt_identical=False):
    meta = {}
    if kind == "image":
        try:
            with Image.open(io.BytesIO(content)) as img:
                fmt = img.format
                if fmt not in EXT:
                    raise MediaError("Unsupported returned image format")
                meta = {"size": list(img.size), "format": fmt, "hasAlphaChannel": "A" in img.getbands()}
                img.verify()
            suffix = EXT[fmt]
        except (OSError, ValueError):
            raise MediaError("Returned image failed format validation") from None
    else:
        if len(content) < 12 or content[4:8] != b"ftyp":
            raise MediaError("Returned video does not have an MP4 container header")
        suffix = ".mp4"
        meta["validation"] = "container header only; run video2dsprite for decode and motion QA"
    path = out / ("generated" + suffix)
    # An interrupted video may have published its artifact before job.json was
    # committed. Adopt only byte-identical data returned for the same request ID.
    if path.exists():
        if not adopt_identical or digest(path.read_bytes()) != digest(content):
            raise MediaError("Artifact already exists and cannot be safely replaced")
    else:
        temporary = out / (".artifact-" + uuid.uuid4().hex + ".partial")
        try:
            with temporary.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            # Same-directory hard-link publication is atomic and fails on an
            # existing path. No partially written final media can be observed.
            try:
                os.link(temporary, path)
            except FileExistsError:
                if not adopt_identical or digest(path.read_bytes()) != digest(content):
                    raise MediaError("Concurrent artifact publication conflict") from None
        finally:
            temporary.unlink(missing_ok=True)
    return {"path": path.name, "bytes": len(content), "sha256": digest(content), **meta}


def poll_video(job_path, job, key, transport, timeout, interval, clock=time.monotonic, sleep=time.sleep):
    deadline = clock() + timeout
    request_id = job.get("requestId", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", request_id):
        raise MediaError("No valid request ID; automatic resubmission is deliberately disabled")
    while clock() < deadline:
        result = transport.api("GET", API["xai"] + "/videos/" + request_id, key,
                               timeout=max(.1, min(90, deadline - clock())))
        status = result.get("status")
        if status == "done":
            video = result.get("video")
            if not isinstance(video, dict) or not isinstance(video.get("url"), str):
                raise MediaError("Completed video has no downloadable URL")
            if video.get("respect_moderation") is False:
                raise MediaError("Provider did not release the video after moderation")
            job["status"] = "downloading"
            save_json(job_path, job)
            data = transport.download(video["url"], timeout=max(.1, min(90, deadline - clock())))
            job["artifact"] = write_artifact(job_path.parent, data, "video", adopt_identical=True)
            job["returnedModel"] = result.get("model")
            job["durationReported"] = video.get("duration")
            job["status"] = "done"
            save_json(job_path, job)
            return job
        if status in ("failed", "expired"):
            job["status"] = status
            save_json(job_path, job)
            raise MediaError(f"Video job {status}; no regeneration attempted")
        if status != "pending":
            raise MediaError("Unknown video status; request ID retained, no regeneration attempted")
        job["status"] = "pending"
        save_json(job_path, job)
        sleep(min(interval, max(0, deadline - clock())))
    job["status"] = "pending_timeout"
    save_json(job_path, job)
    raise MediaError("Polling timeout; resume this job to poll again without a new paid request")


def execute(args, transport=None):
    transport = transport or Transport()
    plan, body, content_type, prompt = prepare(args)
    if not args.execute:
        return plan
    key = os.environ.get(KEY[args.provider])
    if not key:
        raise MediaError(f"Missing {KEY[args.provider]}; no request sent")
    out = Path(args.out_dir)
    # A new directory is the job reservation. Existing results are never reused
    # as an excuse to submit another paid POST.
    try:
        out.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise MediaError("Output already exists; choose a new job directory or use resume") from None
    job_path = out / "job.json"
    job = {**plan, "status": "submitting"}
    (out / "prompt.txt").write_text(prompt + "\n", encoding="utf-8")
    save_json(job_path, job)
    try:
        result = transport.api("POST", plan["endpoint"], key, body, content_type,
                               timeout=min(args.timeout, 180))
        if args.command == "video":
            rid = result.get("request_id")
            if not isinstance(rid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", rid):
                raise MediaError("Submit response omitted a valid request ID; check provider history before any new submission")
            job.update(status="pending", requestId=rid)
            save_json(job_path, job)
            return poll_video(job_path, job, key, transport, args.timeout, args.poll_interval)
        job["status"] = "processing_result"
        save_json(job_path, job)
        items = result.get("data")
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            raise MediaError("Expected exactly one generated image")
        encoded = items[0].get("b64_json")
        if not isinstance(encoded, str):
            raise MediaError("Expected base64 image response; no regeneration attempted")
        try:
            content = base64.b64decode(encoded, validate=True)
        except ValueError:
            raise MediaError("Invalid base64 image response") from None
        job["artifact"] = write_artifact(out, content, "image")
        job["returnedModel"] = result.get("model")  # null if provider does not report it
        job["status"] = "done"
        save_json(job_path, job)
        return job
    except Exception as exc:
        if job["status"] == "submitting":
            job["status"] = "submit_unknown"
        elif job["status"] not in ("pending_timeout", "failed", "expired"):
            job["status"] = "interrupted"
        # Store exception type, never arbitrary server text or signed URL.
        job["errorType"] = type(exc).__name__
        save_json(job_path, job)
        raise


def resume(args, transport=None):
    bounded_time(args)
    path = Path(args.job)
    job = json.loads(path.read_text(encoding="utf-8"))
    if job.get("schemaVersion") != 1 or job.get("provider") != "xai" or job.get("kind") != "video":
        raise MediaError("Only known xAI video jobs can be resumed")
    if job.get("status") == "done":
        artifact = job.get("artifact", {})
        name = artifact.get("path", "")
        if name != "generated.mp4" or digest((path.parent / name).read_bytes()) != artifact.get("sha256"):
            raise MediaError("Completed artifact is missing or changed; no regeneration attempted")
        return job
    if job.get("status") in ("failed", "expired"):
        raise MediaError("This job is terminal; no resubmission attempted")
    key = os.environ.get("XAI_API_KEY")
    if not key:
        raise MediaError("Missing XAI_API_KEY")
    return poll_video(path, job, key, transport or Transport(), args.timeout, args.poll_interval)


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    modes = p.add_subparsers(dest="command", required=True)
    for name in ("image", "video"):
        c = modes.add_parser(name)
        c.add_argument("--provider", choices=("openai", "xai") if name == "image" else ("xai",), required=True)
        c.add_argument("--model", required=True)
        c.add_argument("--prompt-file", required=True)
        c.add_argument("--reference", action="append", default=[])
        c.add_argument("--out-dir", required=True)
        c.add_argument("--execute", action="store_true", help="Send one paid API request; default is dry-run")
        c.add_argument("--timeout", type=float, default=600)
        c.add_argument("--poll-interval", type=float, default=5)
        if name == "image":
            c.add_argument("--size")
            c.add_argument("--resolution", choices=("1k", "2k"))
            c.add_argument("--aspect-ratio", choices=("1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3"))
            c.add_argument("--quality", choices=("auto", "low", "medium", "high"))
            c.add_argument("--transparent", action="store_true")
        else:
            c.add_argument("--duration", type=int, default=4)
            c.add_argument("--resolution", choices=("480p", "720p", "1080p"), default="720p")
            c.add_argument("--last-frame")
    r = modes.add_parser("resume", help="Poll a saved video job; never submits generation")
    r.add_argument("--job", required=True)
    r.add_argument("--timeout", type=float, default=600)
    r.add_argument("--poll-interval", type=float, default=5)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = resume(args) if args.command == "resume" else execute(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (MediaError, OSError, ValueError) as exc:
        # MediaError messages are authored locally; other errors may include paths.
        print(str(exc) if isinstance(exc, MediaError) else type(exc).__name__ + ": local input/output failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
