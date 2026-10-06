#!/usr/bin/env python3
"""API keys and model choices for the generate2dmedia API routes (owner decision 2026-10-06).

A key comes from the environment first (OPENAI_API_KEY, XAI_API_KEY), then from one JSON file in
the user's own configuration folder, outside every project:

  Windows    %APPDATA%\\agent-sprite-forge\\config.json
  elsewhere  $XDG_CONFIG_HOME/agent-sprite-forge/config.json (default ~/.config/agent-sprite-forge/config.json)

  {"OPENAI_API_KEY": "sk-...", "XAI_API_KEY": "xai-...",
   "models": {"openai-image": "gpt-image-2.5-sunburst", "xai-image": "grok-imagine-image-2.0",
              "xai-video": "grok-imagine-video-1.5"}}

Every field is optional; unknown fields are ignored. A configured key is the owner's standing
consent to spend on that provider through route_media.py; the ledger still records every call
and its estimate. Keys are read in-process only: never printed, logged, written, passed on a
command line or put into a child process's environment. A Codex or Grok CLI sign-in is never a
key. Stdlib only.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat

APP_FOLDER = "agent-sprite-forge"
CONFIG_NAME = "config.json"
CONFIG_LIMIT = 64 * 1024
# Provider -> the environment variable (and config field) that holds its key.
PROVIDERS = {"openai": "OPENAI_API_KEY", "xai": "XAI_API_KEY"}
# Model slots and their defaults (provider-survey.md, checked 2026-10-05).
MODEL_DEFAULTS = {"openai-image": "gpt-image-2.5-sunburst", "xai-image": "grok-imagine-image-2.0",
                  "xai-video": "grok-imagine-video-1.5"}
MODEL_ID = re.compile(r"[A-Za-z0-9_.:-]{1,120}")


def config_path(environ: dict | None = None) -> Path:
    """The user-level config file: %APPDATA%\\agent-sprite-forge\\config.json on Windows,
    $XDG_CONFIG_HOME (default ~/.config)/agent-sprite-forge/config.json elsewhere."""
    env = os.environ if environ is None else environ
    if os.name == "nt":
        base = (env.get("APPDATA") or "").strip()
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
    else:
        base = (env.get("XDG_CONFIG_HOME") or "").strip()
        root = Path(base) if base and os.path.isabs(base) else Path.home() / ".config"
    return root / APP_FOLDER / CONFIG_NAME


def load_config(path: Path | None = None) -> tuple[dict, str | None]:
    """(settings, problem). A missing file is ({}, None); an unreadable or malformed one is
    ({}, a reason that never quotes the file's content)."""
    path = config_path() if path is None else Path(path)
    try:
        with open(path, "rb") as stream:
            raw = stream.read(CONFIG_LIMIT + 1)
    except FileNotFoundError:
        return {}, None
    except OSError as exc:
        return {}, f"the user config file cannot be read ({type(exc).__name__})"
    if len(raw) > CONFIG_LIMIT:
        return {}, "the user config file is larger than 64 KiB"
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        return {}, "the user config file is not valid UTF-8 JSON"
    if not isinstance(data, dict):
        return {}, "the user config file must hold one JSON object"
    return data, None


def _settings(config: dict | None) -> dict:
    return load_config()[0] if config is None else config


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def key_source(provider: str, config: dict | None = None) -> str | None:
    """Where the provider's key is configured: "environment", "config" or None. Never the key."""
    name = PROVIDERS[provider]
    if _text(os.environ.get(name)):
        return "environment"
    return "config" if _text(_settings(config).get(name)) else None


def api_key(provider: str, config: dict | None = None) -> str | None:
    """The provider's key, environment first, then the user config file; None when neither has one."""
    name = PROVIDERS[provider]
    return _text(os.environ.get(name)) or _text(_settings(config).get(name))


def configured(config: dict | None = None) -> dict:
    """{provider: bool}: which providers have a key, without exposing any key."""
    settings = _settings(config)
    return {provider: key_source(provider, settings) is not None for provider in PROVIDERS}


def model_for(slot: str, config: dict | None = None) -> str:
    """The model of one slot (openai-image, xai-image, xai-video): the config's choice when it is a
    plausible model id, else the default."""
    models = _settings(config).get("models")
    chosen = _text(models.get(slot)) if isinstance(models, dict) else None
    return chosen if chosen and MODEL_ID.fullmatch(chosen) else MODEL_DEFAULTS[slot]


def known_secrets(config: dict | None = None) -> tuple[str, ...]:
    """Every configured key (environment and config file), for redaction only."""
    settings = _settings(config)
    found = []
    for name in PROVIDERS.values():
        for value in (_text(os.environ.get(name)), _text(settings.get(name))):
            if value and value not in found:
                found.append(value)
    return tuple(found)


def loose_permissions(path: Path | None = None) -> bool:
    """POSIX only: True when other users may read the config file (it should be chmod 600)."""
    if os.name == "nt":
        return False
    try:
        mode = os.stat(config_path() if path is None else path).st_mode
    except OSError:
        return False
    return bool(mode & (stat.S_IRWXG | stat.S_IRWXO))
