#!/usr/bin/env python3
"""Fake Grok Build CLI for tests/test_cli_media.py and tests/test_forge_doctor.py. Never contacts anything.

Speaks the subset of `grok` that cli_media.py and forge_doctor.py use:

  --version                                   prints FAKE_GROK_VERSION (default "grok 1.0.40")
  --prompt-file F --cwd D --tools T ...       headless run with --output-format streaming-json events
  agent --no-leader --agent-profile P stdio   an ACP (JSON-RPC 2.0 over stdio) agent with image_to_video

The behaviour comes from a marker in the prompt, [[fake:MODE]], or else from FAKE_CLI_MODE
(default success). Outputs go to $GROK_HOME/sessions/<url-encoded cwd>/<session>/{images,videos}/
as Grok does. Every invocation and every JSON-RPC method received is appended to FAKE_CLI_LOG.
"""
import json
import os
from pathlib import Path
import re
import struct
import sys
import time
from urllib.parse import quote
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


MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 4096


def log(**entry):
    target = os.environ.get("FAKE_CLI_LOG")
    if target:
        with open(target, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry) + "\n")


def emit(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def mode_of(text):
    marker = re.search(r"\[\[fake:([a-z_]+)\]\]", text)
    return marker.group(1) if marker else os.environ.get("FAKE_CLI_MODE", "success")


def session_folder(cwd, session, kind):
    home = Path(os.environ.get("GROK_HOME") or Path.home() / ".grok")
    folder = home / "sessions" / quote(os.path.abspath(cwd), safe="") / session / kind
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def option(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def headless(argv):
    prompt = Path(option(argv, "--prompt-file")).read_text(encoding="utf-8")
    tool, cwd = option(argv, "--tools"), option(argv, "--cwd")
    mode = mode_of(prompt)
    log(cli="grok", mode=mode, tool=tool)
    if mode == "auth":
        print("error: not logged in (401 Unauthorized); run grok login", file=sys.stderr)
        return 1
    session = os.environ.get("FAKE_SESSION_ID") or str(uuid.uuid4())
    emit({"type": "available_commands", "tools": [] if mode == "unavailable" else [tool]})
    if mode == "unavailable":
        emit({"type": "end", "sessionId": session, "stopReason": "end_turn", "text": "UNSUPPORTED"})
        return 0
    if mode == "timeout":
        time.sleep(60)
        return 0
    if mode == "unexpected_tool":
        emit({"type": "tool_call", "toolName": "Bash", "toolCallId": "call-9"})
        time.sleep(60)
        return 0
    emit({"type": "tool_call", "toolName": tool, "toolCallId": "call-1"})
    if mode == "two_calls":
        emit({"type": "tool_call", "toolName": tool, "toolCallId": "call-2"})
        time.sleep(60)
        return 0
    if mode == "outside":
        target = Path(cwd) / "1.png"
    else:
        target = session_folder(cwd, session, "images") / "1.png"
    target.write_bytes(png())
    output = {"type": {"image_gen": "ImageGen", "image_edit": "ImageEdit"}[tool], "path": str(target)}
    emit({"type": "tool_call_update", "toolCallId": "call-1", "status": "completed", "rawOutput": output})
    if mode == "hang_after_tool":  # the image exists, but the CLI never reaches its end event
        time.sleep(60)
        return 0
    emit({"type": "end", "sessionId": session, "stopReason": "end_turn", "text": "DONE"})
    return 0


class Acp:
    """A minimal ACP agent: one session, one image_to_video tool behind a permission request."""

    def __init__(self):
        self.session = os.environ.get("FAKE_SESSION_ID") or str(uuid.uuid4())
        self.cwd = os.getcwd()

    def read(self):
        line = sys.stdin.readline()
        if not line:
            raise EOFError
        message = json.loads(line)
        log(cli="grok-acp", received=message.get("method") or "response", id=message.get("id"))
        return message

    def wait_response(self, request_id):
        while True:
            message = self.read()
            if message.get("id") == request_id and "method" not in message:
                return message

    def notify(self, update):
        emit({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": self.session, "update": update}})

    def prompt(self, message):
        text = message["params"]["prompt"][0]["text"]
        mode = mode_of(text)
        start = text.index("{")
        args, _ = json.JSONDecoder().raw_decode(text[start:])
        log(cli="grok-acp", mode=mode, args={k: args[k] for k in ("image", "duration", "resolution_name")})
        if mode == "timeout":
            time.sleep(60)
            return
        if mode == "fs_request":
            emit({"jsonrpc": "2.0", "id": 900, "method": "fs/read_text_file", "params": {"path": "secret.txt"}})
            answer = self.wait_response(900)
            log(cli="grok-acp", fsAnswer=answer.get("error", {}).get("code"))
        if mode == "unexpected_tool":
            self.notify({"sessionUpdate": "tool_call", "toolCallId": "call-9", "title": "web_fetch", "status": "pending"})
            time.sleep(60)
            return
        raw = dict(args, duration=args["duration"] + 1) if mode == "mismatch" else args
        self.notify({"sessionUpdate": "tool_call", "toolCallId": "call-1", "title": "image_to_video",
                     "status": "pending", "rawInput": raw})
        emit({"jsonrpc": "2.0", "id": 1001, "method": "session/request_permission", "params": {
            "sessionId": self.session, "toolCall": {"toolCallId": "call-1", "rawInput": raw},
            "options": [{"optionId": "allow-once", "kind": "allow_once", "name": "Allow once"},
                        {"optionId": "reject-once", "kind": "reject_once", "name": "Reject"}]}})
        answer = self.wait_response(1001)
        outcome = answer.get("result", {}).get("outcome", {})
        log(cli="grok-acp", permission=outcome)
        if outcome.get("optionId") != "allow-once":
            emit({"jsonrpc": "2.0", "id": message["id"], "result": {"stopReason": "cancelled"}})
            return
        target = session_folder(self.cwd, self.session, "videos") / "1.mp4"
        target.write_bytes(MP4)
        self.notify({"sessionUpdate": "tool_call_update", "toolCallId": "call-1", "title": "image-to-video: clip",
                     "rawInput": dict(raw, variant="ImageToVideo"), "status": "completed",
                     "rawOutput": {"type": "ImageToVideo", "path": str(target), "filename": "1.mp4",
                                   "session_folder": "videos"}})
        self.notify({"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "thinking..."}})
        self.notify({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": str(target)}})
        emit({"jsonrpc": "2.0", "id": message["id"], "result": {"stopReason": "end_turn"}})

    def serve(self):
        mode = os.environ.get("FAKE_CLI_MODE", "success")
        while True:
            try:
                message = self.read()
            except EOFError:
                return 0
            method = message.get("method")
            if method == "initialize":
                emit({"jsonrpc": "2.0", "id": message["id"], "result": {
                    "protocolVersion": 1, "agentInfo": {"name": "fake-grok", "version": "1.0.40"}}})
            elif method == "session/new":
                if mode == "auth":
                    emit({"jsonrpc": "2.0", "id": message["id"], "error": {
                        "code": -32000, "message": "Authentication required: run grok login"}})
                    continue
                self.cwd = message["params"].get("cwd") or self.cwd
                emit({"jsonrpc": "2.0", "id": message["id"], "result": {"sessionId": self.session}})
            elif method == "session/prompt":
                self.prompt(message)


def main(argv):
    secretish = sorted(k for k in os.environ if re.search(r"(?:^|_)(?:API_?KEY|TOKEN|SECRET|PASSWORD)(?:_|$)", k, re.I))
    isolation = {k: os.environ.get(k) for k in ("GROK_DISABLE_AUTOUPDATER", "GROK_MEMORY", "GROK_SUBAGENTS")}
    log(cli="grok", argv=argv, cwd=os.getcwd(), credentialEnv=secretish, isolation=isolation)
    if argv[:1] == ["--version"]:
        print(os.environ.get("FAKE_GROK_VERSION", "grok 1.0.40"))
        return 0
    if argv[:1] == ["agent"] and argv[-1:] == ["stdio"]:
        return Acp().serve()
    if "--prompt-file" in argv:
        return headless(argv)
    print(f"fake grok: unsupported arguments {argv[:2]}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
