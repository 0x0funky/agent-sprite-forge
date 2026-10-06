#!/usr/bin/env python3
"""Fake Codex CLI for tests/test_cli_media.py and tests/test_forge_doctor.py. Never contacts anything.

Speaks the subset of `codex` that cli_media.py and forge_doctor.py use:

  --version        prints FAKE_CODEX_VERSION (default "codex-cli 0.155.1")
  login status     prints the sign-in mode chosen by FAKE_CODEX_LOGIN (chatgpt | apikey | none)
  exec ... -       reads the prompt on stdin and emits `codex exec --json` events

The behaviour of `exec` comes from a marker in the prompt, [[fake:MODE]], or else from
FAKE_CLI_MODE (default success). Images go to $CODEX_HOME/generated_images/<thread>/ as
Codex does. Every invocation is appended to FAKE_CLI_LOG as one JSON line (argv, cwd and
the names, never the values, of environment variables that look like credentials). The
--image files are read as the image tool reads them; one that cannot be read (or mode
access_denied) ends the turn without an image, as the live Windows sandbox failure did.
"""
import json
import os
from pathlib import Path
import re
import struct
import sys
import time
import uuid
import zlib


def png(size=48):
    """A deterministic RGB gradient PNG of a few hundred bytes."""
    raw = b"".join(b"\x00" + b"".join(bytes((x * 5 % 256, y * 5 % 256, x * y % 256)) for x in range(size))
                   for y in range(size))
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def log(**entry):
    target = os.environ.get("FAKE_CLI_LOG")
    if target:
        with open(target, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry) + "\n")


def emit(event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def main(argv):
    secretish = sorted(k for k in os.environ if re.search(r"(?:^|_)(?:API_?KEY|TOKEN|SECRET|PASSWORD)(?:_|$)", k, re.I))
    log(cli="codex", argv=argv, cwd=os.getcwd(), credentialEnv=secretish)
    if argv[:1] == ["--version"]:
        print(os.environ.get("FAKE_CODEX_VERSION", "codex-cli 0.155.1"))
        return 0
    if argv[:2] == ["login", "status"]:
        mode = os.environ.get("FAKE_CODEX_LOGIN", "chatgpt")
        print({"chatgpt": "Logged in using ChatGPT", "apikey": "Logged in using an API key - sk-proj-***1234",
               "none": "Not logged in"}[mode], file=sys.stderr)  # Codex prints the status on stderr
        return 0 if mode != "none" else 1
    if argv[:1] != ["exec"]:
        print(f"fake codex: unsupported arguments {argv[:2]}", file=sys.stderr)
        return 2
    prompt = sys.stdin.read()
    marker = re.search(r"\[\[fake:([a-z_]+)\]\]", prompt)
    mode = marker.group(1) if marker else os.environ.get("FAKE_CLI_MODE", "success")
    log(cli="codex", mode=mode, promptChars=len(prompt))
    if mode == "auth":
        print("Error: unexpected status 401 Unauthorized: please sign in again with codex login", file=sys.stderr)
        return 1
    if mode == "rate":
        print("Error: 429 Too Many Requests: usage limit reached", file=sys.stderr)
        return 1
    thread = os.environ.get("FAKE_THREAD_ID") or str(uuid.uuid4())
    emit({"type": "thread.started", "thread_id": thread})
    emit({"type": "turn.started"})
    if mode == "timeout":
        time.sleep(60)
        return 0
    if mode == "unexpected_tool":
        emit({"type": "item.started", "item": {"id": "item_0", "type": "command_execution", "command": "dir"}})
        time.sleep(60)
        return 0
    if mode == "second_image_item":
        for index in range(2):
            emit({"type": "item.started", "item": {"id": f"img_{index}", "type": "image_generation"}})
        time.sleep(60)
        return 0
    if mode == "flood":
        sys.stdout.write("x" * (5 * 1024 * 1024) + "\n")
        sys.stdout.flush()
        time.sleep(60)
        return 0
    if mode == "unavailable":
        emit({"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": "IMAGE_UNAVAILABLE"}})
        emit({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}})
        return 0
    attached = [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == "--image"]
    unreadable = []
    for image in attached:
        try:
            Path(image).read_bytes()
        except OSError:
            unreadable.append(image)
    if mode == "access_denied" or unreadable:
        # The live failure (2026-10-06): the sandboxed image tool could not read an attached reference, so the
        # turn ended without an image and with only this answer.
        denied = (unreadable or attached or ["the attached reference"])[0]
        emit({"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text":
              f"The image tool could not read {denied}: access denied (os error 5). No image was generated."}})
        emit({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}})
        return 0
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    folder = home / "generated_images" / thread
    folder.mkdir(parents=True, exist_ok=True)
    names = [f"exec-{uuid.uuid4()}.png" for _ in range(2 if mode == "two_images" else 1)]
    for name in names:
        data = b"this is not an image at all, only text" * 8 if mode == "bad_magic" else png()
        (folder / name).write_bytes(data)
        if mode == "stale":
            old = time.time() - 2 * 86400
            os.utime(folder / name, (old, old))
    answer = str(folder / names[0])
    if mode == "wrong_path":
        answer = str(home.parent / "elsewhere" / "other.png")
    emit({"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": answer}})
    if mode == "hang_after_image":
        time.sleep(60)
        return 0
    emit({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
