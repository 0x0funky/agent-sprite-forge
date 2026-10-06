#!/usr/bin/env python3
"""API keys, model choices and provider order for the generate2dmedia API routes (owner decision 2026-10-06).

A key comes from the environment first, then from one JSON file in the user's own configuration
folder, outside every project:

  Windows    %APPDATA%\\agent-sprite-forge\\config.json
  elsewhere  $XDG_CONFIG_HOME/agent-sprite-forge/config.json (default ~/.config/agent-sprite-forge/config.json)

  {"OPENAI_API_KEY": "sk-...", "GEMINI_API_KEY": "...", "XAI_API_KEY": "xai-...",
   "ARK_API_KEY": "...", "FAL_KEY": "key-id:key-secret",
   "models": {"gemini-image": "gemini-3.1-flash-image", "gemini-image-hero": "gemini-3-pro-image",
              "xai-video-draft": "grok-imagine-video-1.5-lite"},
   "providers": {"order": ["gemini", "openai"]}}

Each provider has its own key variables (the vendors' official names): OpenAI OPENAI_API_KEY;
Google Gemini GOOGLE_API_KEY or GEMINI_API_KEY (GOOGLE_API_KEY wins when both are set, as in
Google's own SDK); xAI XAI_API_KEY; BytePlus ModelArk ARK_API_KEY; fal.ai FAL_KEY. A provider
never falls back to another provider's key. The environment wins over the file.

Every field is optional; unknown fields are ignored. "models" slots are <provider>-<kind> (the
standard tier) and <provider>-<kind>-draft / -hero. "providers.order" lists the providers (and,
optionally, the local routes codex-cli, grok-cli, grok-acp) to try first with --route auto; it is
a list for both kinds or {"image": [...], "video": [...]}. A configured key is the owner's
standing consent to spend on that provider through route_media.py; the ledger still records
every call and its estimate. Keys are read in-process only: never printed, logged, written,
passed on a command line or put into a child process's environment. A Codex or Grok CLI
sign-in is never a key. Stdlib only.
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
# Provider -> the environment variables (and config fields) that hold its key, in precedence order.
PROVIDER_KEYS = {
    "openai": ("OPENAI_API_KEY",),
    "gemini": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "xai": ("XAI_API_KEY",),
    "byteplus": ("ARK_API_KEY",),
    "fal": ("FAL_KEY",),
}
# Provider -> how to name its key to a person (route_media's "no ... configured" reasons).
PROVIDERS = {name: " or ".join(names) for name, names in PROVIDER_KEYS.items()}
KEY_VARIABLES = tuple(name for names in PROVIDER_KEYS.values() for name in names)
# The standard-tier model of each <provider>-<kind> slot (capabilities.json "tiers", verified 2026-10-06;
# a test keeps the two equal). Draft and hero slots add -draft / -hero and default to the tiers there.
MODEL_DEFAULTS = {
    "openai-image": "gpt-image-2.5-sunburst",
    "gemini-image": "gemini-3.1-flash-image",
    "xai-image": "grok-imagine-image-2.0",
    "xai-video": "grok-imagine-video-1.5",
    "byteplus-image": "dola-seedream-5-0-pro-260628",
    "byteplus-video": "dreamina-seedance-2-0-260128",
    "fal-image": "fal-ai/nano-banana-2/edit",
    "fal-video": "fal-ai/kling-video/v3/pro/image-to-video",
}
LOCAL_ROUTES = ("codex-cli", "grok-cli", "grok-acp")
MODEL_ID = re.compile(r"[A-Za-z0-9_.:/-]{1,160}")


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


def _lookup(provider: str, config: dict | None) -> tuple[str | None, str | None, str | None]:
    """(key, source, variable): the environment in precedence order, then the user config file."""
    names = PROVIDER_KEYS[provider]
    for name in names:
        value = _text(os.environ.get(name))
        if value:
            return value, "environment", name
    settings = _settings(config)
    for name in names:
        value = _text(settings.get(name))
        if value:
            return value, "config", name
    return None, None, None


def key_source(provider: str, config: dict | None = None) -> str | None:
    """Where the provider's key is configured: "environment", "config" or None. Never the key."""
    return _lookup(provider, config)[1]


def key_variable(provider: str, config: dict | None = None) -> str | None:
    """The variable (or config field) the provider's key was read from, e.g. GOOGLE_API_KEY. Never the key."""
    return _lookup(provider, config)[2]


def api_key(provider: str, config: dict | None = None) -> str | None:
    """The provider's key, environment first, then the user config file; None when neither has one."""
    return _lookup(provider, config)[0]


def configured(config: dict | None = None) -> dict:
    """{provider: bool}: which providers have a key, without exposing any key."""
    settings = _settings(config)
    return {provider: key_source(provider, settings) is not None for provider in PROVIDER_KEYS}


def model_for(slot: str, config: dict | None = None, default: str | None = None) -> str | None:
    """The model of one slot (<provider>-<kind>, optionally -draft or -hero): the config's choice when it
    is a plausible model id, else ``default``, else the standard default of the slot (MODEL_DEFAULTS)."""
    models = _settings(config).get("models")
    chosen = _text(models.get(slot)) if isinstance(models, dict) else None
    if chosen and MODEL_ID.fullmatch(chosen):
        return chosen
    return default if default is not None else MODEL_DEFAULTS.get(slot)


def provider_order(kind: str, config: dict | None = None) -> tuple[list, list]:
    """(preferred names, problems) from "providers": {"order": [...]} (both kinds) or
    {"order": {"image": [...], "video": [...]}}. Names are providers (openai, gemini, ...) or local
    routes (codex-cli, grok-cli, grok-acp); unknown names are reported and ignored."""
    section = _settings(config).get("providers")
    if section is None:
        return [], []
    if not isinstance(section, dict):
        return [], ["providers must be an object"]
    order = section.get("order")
    if isinstance(order, dict):
        order = order.get(kind)
    if order is None:
        return [], []
    if not isinstance(order, list):
        return [], ["providers.order must be a list of names (or {\"image\": [...], \"video\": [...]})"]
    names, problems = [], []
    for item in order:
        name = _text(item)
        name = name.lower() if name else None
        if name in PROVIDER_KEYS or name in LOCAL_ROUTES:
            if name not in names:
                names.append(name)
        else:
            problems.append(f"providers.order: unknown name {str(item)[:40]!r} ignored")
    return names, problems


def known_secrets(config: dict | None = None) -> tuple[str, ...]:
    """Every configured key (environment and config file), for redaction only. A fal key
    (key-id:key-secret) also contributes its two halves."""
    settings = _settings(config)
    found = []
    for name in KEY_VARIABLES:
        for value in (_text(os.environ.get(name)), _text(settings.get(name))):
            if not value:
                continue
            for part in (value, *value.split(":")):
                if len(part) >= 8 and part not in found:
                    found.append(part)
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
