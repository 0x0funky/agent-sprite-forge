#!/usr/bin/env python3
"""Build sprite prompts and postprocess generated sprite sheets locally.

``process`` keys or validates a sheet, splits it into cells and places every
frame on one output grid (geometry v2):

* Geometry (components, boxes, anchors, edge checks, clamping) counts pixels
  with alpha above ``--alpha-geometry-threshold`` (16) and labels 8-connected
  components; the threshold never changes a pixel.
* ``native_alpha`` input gets ``--alpha-hygiene both``: generator haze and
  detached faint specks are removed and counted.
* Anchors sit on the ground line, the bottom edge of the main component's
  lowest row (``--anchor-mode feet``), measured on the subject itself.
* ``preserve`` and ``registered`` resample every cell on one shared sampling
  grid and move frames by whole output pixels only; ``nearest`` accepts
  integer scales only (``--pixel-scale``, ``--logical-pixel``).
* Scale QC is measured in output pixels; new scale profiles are version 2.

Every changed default keeps a legacy switch: ``--connectivity 4``,
``--anchor-mode legacy-p98``, ``--alpha-geometry-threshold 0``,
``--alpha-hygiene none``, ``--legacy-fractional-nearest``,
``--key-quality hard``, ``--profile-override`` and
``build-prompt --legacy-style``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from PIL import Image

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import forge_core  # noqa: E402  (this skill's vendored copy)
import forge_matte  # noqa: E402


TOOL_NAME = "generate2dsprite.py"
TOOL_VERSION = forge_core.FORGE_PACKAGE_VERSION  # QA envelopes record the package version (D29)
PIPELINE_META_SCHEMA = "generate2dsprite.pipeline_meta.v2"

BACKGROUND_MODES = ("chroma_key", "native_alpha", "opaque")
RESAMPLERS = {"nearest": Image.Resampling.NEAREST, "lanczos": Image.Resampling.LANCZOS}
KEY_QUALITIES = forge_matte.KEY_QUALITIES
KEY_NAMES = tuple(forge_matte.DECLARED_KEYS)
ALIGN_MODES = ("center", "feet", "bottom")
ANCHOR_MODES = ("feet", "stance", "bbox", "center", "centroid", "legacy-p98")
SCALE_STRATEGIES = ("fit", "preserve", "registered")
COMPONENT_MODES = ("all", "largest")
GRID_ROUNDINGS = ("exact", "nearest")
DIRECTION_NAMES = ("down", "down-left", "left", "up-left", "up", "up-right", "right", "down-right")
DEFAULT_DIRECTION_ORDER = ("down", "left", "right", "up")
DESPILL_MARGIN = 12
KEY_RING_SPILL_MAX = 0.01    # outer-ring key share that warns (video residue gate, report v2 P0-3)
ANCHOR_BAND_FRACTION = 0.12  # support band of the feet/stance anchor, as a share of the subject height
FAINT_ATTACH_RADIUS = 2      # px: faint pixels this close to a kept component stay with it
LOCOMOTION_MODES = frozenset({"walk", "run", "player_walk", "npc_walk", "player_sheet"})
RECOMMENDED_LOCOMOTION_POSES = (8, 12)
_EPSILON = 1e-9


LEGACY_ART_STYLE = (
    "Original digital monster creature. Digimon/Pokemon inspired pixel art, "
    "strong outlines, dynamic, battle-ready. NOT cute, NOT round. "
    "SOLID COLORED BODY. Background is 100% solid flat magenta (#FF00FF), no gradients. "
    "NO text, NO labels, NO words, NO letters anywhere."
)

LEGACY_CHAR_STYLE = (
    "Top-down 2D pixel art for a 16-bit RPG overworld. 3/4 view from slightly "
    "above, you can see the top of the head, shoulders and full body. Chunky "
    "pixel-art with crisp dark outlines and saturated colors. Character fills "
    "~60% of its cell with margin for the engine to render cleanly. "
    "Background is 100% solid flat magenta (#FF00FF), no gradients, no shadow "
    "under character. NO text, NO labels, NO UI, NO speech bubbles."
)

CREATURE_STYLE = (
    "Original creature design as game pixel art: strong outlines, a dynamic battle-ready "
    "silhouette and a SOLID COLORED BODY. Background is 100% solid flat magenta (#FF00FF), "
    "no gradients. NO text, NO labels, NO words, NO letters anywhere."
)

ASSET_STYLE = (
    "Game-ready 2D asset in clean pixel art: crisp outlines, a readable silhouette and one "
    "consistent light direction. Background is 100% solid flat magenta (#FF00FF), no gradients, "
    "no cast shadow. NO text, NO labels, NO words, NO letters anywhere."
)

CHAR_STYLE = (
    "Top-down 2D pixel art for an RPG overworld. 3/4 view from slightly "
    "above, you can see the top of the head, shoulders and full body. Chunky "
    "pixel-art with crisp dark outlines and saturated colors. Character fills "
    "~60% of its cell with margin for the engine to render cleanly. "
    "Background is 100% solid flat magenta (#FF00FF), no gradients, no shadow "
    "under character. NO text, NO labels, NO UI, NO speech bubbles."
)

GRID_RULES = (
    "ABSOLUTE RULES: "
    "1. EXACTLY 4 equal quadrants (2x2). "
    "2. NO borders, NO lines, NO frames between quadrants. "
    "3. NO text, NO labels. "
    "4. Keep one anatomical scale and shared registration origin, not an identical pose bounding box. "
    "Reserve the full motion envelope, including tail, ears, limbs and held equipment, "
    "inside the central 70% of each quadrant with at least 15% clear background on every side. "
    "No body part may touch or cross an internal cell boundary. "
    "5. Quadrants connected by magenta background only. Do not draw gutters or separators."
)

GRID_RULES_4X4 = (
    "ABSOLUTE RULES: "
    "1. EXACTLY 16 equal-size cells arranged in a 4x4 grid (4 rows of 4 columns, every cell the same width and height). "
    "2. NO borders, NO lines, NO frames between cells. "
    "3. NO text, NO labels, NO numbers, NO arrows. "
    "4. CRITICAL CONSISTENCY: keep the same anatomical scale, camera distance and shared registration origin "
    "in every cell. Do NOT fit each pose to an identical bounding box or zoom between cells. "
    "Preserve deliberate limb movement and body bob. "
    "5. Keep the entire motion envelope, including tail, ears, limbs and held equipment, inside the central 70% "
    "of each cell, with at least 15% clear background on every side. No part may touch or cross a cell boundary. "
    "6. Cells connected ONLY by solid magenta (#FF00FF) background."
)

NPC_ROLES = {
    "starter": "an experienced mentor who hands out starter monsters, wise and welcoming",
    "shop": "a merchant or vendor, apron or utility belt, counter accessories",
    "healer": "a healer or medic, soft uniform, healing tools, calm posture",
    "summoner": "a mystical summoner who calls forth monsters, arcane or gambling vibe",
    "sage": "an old wise sage, robes or long coat, staff or crystal",
    "trainer": "a rival trainer, confident athletic pose, slight smirk",
    "gym_leader": "a gym leader or boss, distinct outfit, most powerful regional trainer",
    "villager": "an ordinary townsperson, plain outfit, friendly body language",
    "guard": "a city guard, uniform or armor, alert posture",
}

GENERIC_ASSET_MODES = [
    "single",
    "idle",
    "cast",
    "attack",
    "hurt",
    "combat",
    "walk",
    "run",
    "hover",
    "charge",
    "projectile",
    "impact",
    "explode",
    "death",
    "fx",
    "sheet",
]

TARGET_MODES = {
    "creature": ["single", "evolution", "idle", "combat", "walk", "actions"],
    "player": ["player", "player_walk", "player_sheet", "player_actions"],
    "npc": ["npc", "npc_walk"],
    "asset": GENERIC_ASSET_MODES,
}

# Modes that normalise one isolated image (processed as a 1x1 grid, written as clean.png).
SINGLE_IMAGE_MODES = frozenset({"single", "player", "npc"})

GRID_SHAPES = {
    "evolution": (2, 2),
    "idle": (2, 2),
    "cast": (2, 3),
    "attack": (2, 2),
    "hurt": (2, 2),
    "combat": (2, 2),
    "actions": (2, 2),
    "walk": (2, 2),
    "run": (2, 2),
    "hover": (2, 2),
    "charge": (2, 2),
    "projectile": (1, 4),
    "impact": (2, 2),
    "explode": (2, 2),
    "death": (2, 3),
    "fx": (2, 2),
    "player_walk": (2, 2),
    "player_actions": (2, 2),
    "npc_walk": (2, 2),
    "player_sheet": (4, 4),
}

FRAME_LABELS = {
    "evolution": ["stage-1", "stage-2", "stage-3", "stage-4"],
    "idle": ["idle-1", "idle-2", "idle-3", "idle-4"],
    "cast": ["cast-1", "cast-2", "cast-3", "cast-4", "cast-5", "cast-6"],
    "attack": ["attack-1", "attack-2", "attack-3", "attack-4"],
    "hurt": ["hurt-1", "hurt-2", "hurt-3", "hurt-4"],
    "combat": ["attack-1", "attack-2", "hurt-1", "hurt-2"],
    "actions": ["idle-1", "idle-2", "attack", "hurt"],
    "walk": ["walk-1", "walk-2", "walk-3", "walk-4"],
    "run": ["run-1", "run-2", "run-3", "run-4"],
    "hover": ["hover-1", "hover-2", "hover-3", "hover-4"],
    "charge": ["charge-1", "charge-2", "charge-3", "charge-4"],
    "projectile": ["projectile-1", "projectile-2", "projectile-3", "projectile-4"],
    "impact": ["impact-1", "impact-2", "impact-3", "impact-4"],
    "explode": ["explode-1", "explode-2", "explode-3", "explode-4"],
    "death": ["death-1", "death-2", "death-3", "death-4", "death-5", "death-6"],
    "fx": ["fx-1", "fx-2", "fx-3", "fx-4"],
    "player_walk": ["walk-down-1", "walk-down-2", "walk-down-3", "walk-down-4"],
    "player_actions": ["idle", "walk", "attack", "hurt"],
    "npc_walk": ["walk-down-1", "walk-down-2", "walk-down-3", "walk-down-4"],
    "player_sheet": [
        "down-1",
        "down-2",
        "down-3",
        "down-4",
        "left-1",
        "left-2",
        "left-3",
        "left-4",
        "right-1",
        "right-2",
        "right-3",
        "right-4",
        "up-1",
        "up-2",
        "up-3",
        "up-4",
    ],
}

PROCESS_TARGETS = sorted(TARGET_MODES)

ARCHETYPES = {
    "beast": {"name": "Beast Evolution", "path": "primal beast to apex predator to mythic god-beast"},
    "mecha": {"name": "Mecha Evolution", "path": "organic to cybernetic to full mecha to mech-god"},
    "elemental": {"name": "Elemental Evolution", "path": "solid creature to elemental infused to pure energy being"},
    "void": {"name": "Void Evolution", "path": "shadow creature to twisted horror to abstract cosmic entity"},
    "crystal": {"name": "Crystal Evolution", "path": "rocky creature to crystalline to geometric god"},
    "angelic": {"name": "Angelic Evolution", "path": "creature to holy warrior to divine seraph"},
    "parasite": {"name": "Parasite Evolution", "path": "small symbiote to merged chimera to eldritch abomination"},
    "myth": {"name": "Myth Evolution", "path": "animal to mythical beast to ancient deity"},
}

MORPH_AXES = {
    "posture": ["quadrupedal", "bipedal", "floating", "abstract or formless"],
    "material": ["flesh or organic", "armored or plated", "energy-infused", "pure light or energy"],
    "anatomy": ["compact limbs", "extended limbs plus tail", "extra limbs or wings", "aura replaces body parts"],
}

SILHOUETTES = ["sharp angular", "bulky imposing", "elongated serpentine", "alien geometric"]
SURFACES = ["smooth organic", "armored plates", "crystalline facets", "energy veins"]
VIBES = ["elegant and swift", "brutal and heavy", "mysterious and dark", "sacred and divine"]


# --------------------------------------------------------------------------- prompts

def stable_seed(target: str, mode: str, prompt: str, role: str) -> int:
    raw = f"{target}|{mode}|{prompt}|{role}".encode("utf-8")
    return int(hashlib.sha256(raw).hexdigest()[:8], 16)


def is_known_target_mode(target: str, mode: str) -> bool:
    return target in TARGET_MODES and mode in TARGET_MODES[target]


def ensure_valid_target_mode(target: str, mode: str) -> None:
    if target not in TARGET_MODES:
        raise ValueError(f"Unknown target '{target}'. Valid targets: {', '.join(sorted(TARGET_MODES))}")
    if mode not in TARGET_MODES[target]:
        allowed = ", ".join(TARGET_MODES[target])
        raise ValueError(f"Mode '{mode}' is invalid for target '{target}'. Valid modes: {allowed}")


def locomotion_planning_warning(
    mode: str, rows: int, cols: int, intentional_low_frame_count: bool = False
) -> str | None:
    """Flag a planning review, never reject or reinterpret a legacy input grid."""
    if mode not in LOCOMOTION_MODES or intentional_low_frame_count:
        return None
    pose_count = cols if mode == "player_sheet" else rows * cols
    if pose_count >= RECOMMENDED_LOCOMOTION_POSES[0]:
        return None
    scope = "per direction" if mode == "player_sheet" else "in this action"
    return (
        f"Locomotion planning: {pose_count} pose cells {scope}; existing grid compatibility is preserved. "
        "For new locomotion, plan 8-12 useful poses unless a deliberately sparse style is intended. "
        "Cell count does not establish unique or convincing poses: inspect contact, passing, "
        "compression/flight as appropriate, opposite legs, and the loop seam at game size. "
        "Use --intentional-low-frame-count to record a deliberate low-pose choice."
    )


def build_evolution_descs(subject: str, rng: random.Random) -> dict[str, str]:
    arch_key = rng.choice(list(ARCHETYPES.keys()))
    arch = ARCHETYPES[arch_key]
    silhouette = rng.choice(SILHOUETTES)
    surface = rng.choice(SURFACES)
    vibe = rng.choice(VIBES)
    postures = MORPH_AXES["posture"]
    materials = MORPH_AXES["material"]
    anatomies = MORPH_AXES["anatomy"]

    design_rules = (
        f"Evolution archetype: {arch['name']} ({arch['path']}). "
        f"Design: {silhouette} silhouette, {surface} surface, {vibe} feel. "
        "Ensure DIFFERENT silhouette, texture, posture per stage. "
        "Avoid repeating limb structure or proportions."
    )

    return {
        "1-base": (
            f"Stage 1: Base form. The clearest first complete form of {subject}. "
            f"Posture: {postures[0]}. Material: {materials[0]}. Anatomy: {anatomies[0]}. "
            f"Strong identity and readable silhouette. {design_rules}"
        ),
        "2-risen": (
            f"Stage 2: Developed form. A more dangerous promoted version of {subject}. "
            f"Posture: {postures[1]} and clearly different from stage 1. "
            f"Material: {materials[1]}. Anatomy: {anatomies[1]}. "
            f"REDESIGNED, not just bigger. Combat specialist. {design_rules}"
        ),
        "3-elite": (
            f"Stage 3: Elite war form of {subject}. "
            f"Posture: {postures[2]}. Material: {materials[2]}. Anatomy: {anatomies[2]}. "
            f"Heavy battlefield presence and advanced redesign. {design_rules}"
        ),
        "4-mythic": (
            f"Stage 4: Mythic ascendant form of {subject}. "
            f"Posture: {postures[3]}. Material: {materials[3]}. Anatomy: {anatomies[3]}. "
            f"Abstract, cosmic, godlike final evolution. {design_rules}"
        ),
    }


def build_prompt(
    target: str, mode: str, prompt: str, role: str | None = None, seed: int | None = None,
    legacy_style: bool = False,
) -> tuple[str, int]:
    """Return ``(prompt, seed)``. The default style text names no franchise (DOC-16, S26);
    ``legacy_style`` restores the old creature and character style sentences verbatim."""
    ensure_valid_target_mode(target, mode)
    role = role or ""
    if seed is None:
        seed = stable_seed(target, mode, prompt, role)
    rng = random.Random(seed)
    creature_style = LEGACY_ART_STYLE if legacy_style else CREATURE_STYLE
    asset_style = LEGACY_ART_STYLE if legacy_style else ASSET_STYLE
    char_style = LEGACY_CHAR_STYLE if legacy_style else CHAR_STYLE

    if target == "creature":
        if mode == "single":
            result = f"Single pixel art creature sprite, centered, facing right. {prompt}. {creature_style}"
        elif mode == "evolution":
            descs = build_evolution_descs(prompt, rng)
            result = (
                f"A 2x2 pixel art image showing 4 evolution stages of {prompt}. "
                f"Top-left quadrant: {descs['1-base']} "
                f"Top-right quadrant: {descs['2-risen']} "
                f"Bottom-left quadrant: {descs['3-elite']} "
                f"Bottom-right quadrant: {descs['4-mythic']} "
                f"Same color palette in all 4. {creature_style} {GRID_RULES}"
            )
        elif mode == "idle":
            result = (
                f"A 2x2 pixel art idle animation sheet of the same {prompt}. "
                "Top-left quadrant: neutral idle pose, calm but alert. "
                "Top-right quadrant: subtle breath or flame pulse, same facing direction. "
                "Bottom-left quadrant: idle shift in weight or aura, still clearly looping. "
                "Bottom-right quadrant: strongest idle accent before returning to frame 1. "
                f"SAME creature, SAME size, SAME facing direction, SAME palette in all 4 cells. "
                f"{creature_style} {GRID_RULES}"
            )
        elif mode == "combat":
            result = (
                f"A 2x2 pixel art combat sheet of the same {prompt}. "
                "Top-left quadrant: attack wind-up, gathering force. "
                "Top-right quadrant: attack strike or release, aggressive impact. "
                "Bottom-left quadrant: hurt reaction at the moment of impact. "
                "Bottom-right quadrant: hurt recovery, regaining stance. "
                f"SAME creature, SAME size, SAME facing direction, SAME palette in all 4 cells. "
                f"{creature_style} {GRID_RULES}"
            )
        elif mode == "actions":
            result = (
                f"A 2x2 pixel art sprite sheet of the same {prompt} in 4 poses. "
                "Top-left quadrant: standing still, relaxed. "
                "Top-right quadrant: same pose, mouth open, one limb lifted. "
                "Bottom-left quadrant: lunging right, attacking fiercely. "
                "Bottom-right quadrant: leaning back, eyes closed, taking damage. "
                f"SAME character, SAME size, facing RIGHT. {creature_style} {GRID_RULES}"
            )
        else:
            result = (
                f"A 2x2 pixel art sprite sheet of a walk cycle of the same {prompt}. "
                "Top-left quadrant: walking right, right front leg forward. "
                "Top-right quadrant: walking right, legs under body, mid-stride. "
                "Bottom-left quadrant: walking right, left front leg forward. "
                "Bottom-right quadrant: walking right, legs extended, passing pose. "
                f"SAME character, SAME size, facing RIGHT. Only leg positions change. {creature_style} {GRID_RULES}"
            )
    elif target == "player":
        if mode == "player":
            result = (
                "Single hero sprite for a top-down RPG. "
                f"CHARACTER: {prompt}. Young adventurer protagonist, distinct heroic silhouette, strongly themed costume. "
                "Front-facing (toward camera), idle standing pose, centered in canvas with lots of magenta margin around. "
                f"{char_style}"
            )
        elif mode == "player_walk":
            result = (
                "A 2x2 pixel art sprite sheet of a top-down RPG hero walk cycle, ALL FRAMES FACING DOWN (toward camera). "
                f"CHARACTER: {prompt}. "
                "Top-left: neutral standing, both feet together. "
                "Top-right: LEFT foot stepping forward, right foot planted. "
                "Bottom-left: neutral standing again, both feet together. "
                "Bottom-right: RIGHT foot stepping forward, left foot planted. "
                "SAME character, SAME costume, SAME palette in every cell. "
                f"ONLY the legs and arms swing, head, torso, gear stay identical. {char_style} {GRID_RULES}"
            )
        elif mode == "player_sheet":
            result = (
                "A 4x4 pixel art sprite sheet, full 4-direction walk cycle for a top-down RPG hero. "
                f"CHARACTER: {prompt}. Young adventurer protagonist. "
                "SHEET LAYOUT (rows = facing direction, columns = walk frames): "
                "Row 1 (top): facing DOWN (toward camera, face fully visible). "
                "Row 2: facing LEFT (left profile or side view). "
                "Row 3: facing RIGHT (right profile or side view, mirror of row 2). "
                "Row 4 (bottom): facing UP (away from camera, back of head visible). "
                "COLUMN 1: neutral pose, both feet together. "
                "COLUMN 2: LEFT foot stepping forward. "
                "COLUMN 3: neutral pose again, both feet together. "
                "COLUMN 4: RIGHT foot stepping forward. "
                "IDENTICAL SIZE in every cell: same character height head-to-foot, same width shoulder-to-shoulder, "
                "same on-screen pixel scale. No zooming, no cropping differently, only pose and direction change. "
                "SAME character identity, SAME costume, SAME palette in all 16 cells. "
                "The head and torso orientation must clearly communicate which direction the character is facing in each row. "
                f"{char_style} {GRID_RULES_4X4}"
            )
        else:
            result = (
                "A 2x2 pixel art sprite sheet of a top-down RPG hero in 4 action states, all facing DOWN (toward camera). "
                f"CHARACTER: {prompt}. "
                "Top-left: IDLE, neutral standing, relaxed. "
                "Top-right: WALK, mid-step, one leg forward. "
                "Bottom-left: ATTACK, arm raised or weapon or fist swung forward aggressively. "
                "Bottom-right: HURT, knocked back slightly, expression of pain. "
                f"SAME character identity, SAME costume, SAME size in every cell. {char_style} {GRID_RULES}"
            )
    elif target == "npc":
        if role not in NPC_ROLES:
            allowed = ", ".join(sorted(NPC_ROLES))
            raise ValueError(f"NPC role is required for target=npc. Valid roles: {allowed}")
        role_desc = NPC_ROLES[role]
        if mode == "npc":
            result = (
                "Single NPC sprite for a top-down RPG. "
                f"ROLE: {role_desc}. "
                f"VISUAL DETAILS: {prompt}. "
                "Front-facing (toward camera), idle standing pose. "
                "Appearance should INSTANTLY communicate the role. "
                f"Distinct silhouette and palette so this NPC won't be confused with others. {char_style}"
            )
        else:
            result = (
                "A 2x2 pixel art sprite sheet, top-down RPG NPC walk cycle, ALL FRAMES FACING DOWN (toward camera). "
                f"ROLE: {role_desc}. "
                f"VISUAL DETAILS: {prompt}. "
                "Top-left: neutral standing, both feet together. "
                "Top-right: LEFT foot stepping forward. "
                "Bottom-left: neutral standing again. "
                "Bottom-right: RIGHT foot stepping forward. "
                f"SAME NPC, SAME costume, SAME palette in every cell. {char_style} {GRID_RULES}"
            )
    else:
        if mode == "single":
            result = (
                f"Single pixel art asset sprite. SUBJECT: {prompt}. "
                "Centered in the canvas with clear magenta margin around it. "
                "Readable silhouette, game-ready shape consistency, transparent-background-ready via magenta chroma key. "
                f"{asset_style}"
            )
        else:
            rows, cols = GRID_SHAPES.get(mode, (2, 2))
            result = (
                f"A {rows}x{cols} pixel art animation sheet of the same {prompt}. "
                "The same asset identity appears in every cell, with one anatomical scale and shared registration origin. "
                "Preserve intended compression, flight and bob; do not fit every pose to one bounding box. "
                "Keep the full motion envelope, including tail, ears, limbs and held equipment, inside the central 70% "
                "of each cell with at least 15% clear background on every side. No part may touch or cross a cell edge. "
                "Keep the animation readable for a 2D game sprite, not a splash illustration. "
                f"{asset_style}"
            )
    return result, seed


# --------------------------------------------------------------------------- keying (legacy API and shared keyer)

def remove_bg_magenta(img: Image.Image, threshold: int = 100, edge_threshold: int = 150) -> Image.Image:
    """Legacy binary magenta key, bit-exact through forge_matte.legacy_hard_key (S03, S11).

    Visible pixels closer than ``threshold`` to #FF00FF are cleared anywhere;
    pixels closer than ``edge_threshold`` are cleared where they connect to the
    canvas border through cleared or transparent pixels. Returns a new image.
    """
    return forge_matte.legacy_hard_key(img, threshold, edge_threshold)


def validate_despill_radius(radius: int) -> None:
    if type(radius) is not int or radius not in (0, 1, 2, 3):
        raise ValueError("Despill radius must be an integer from 0 to 3.")


def despill_chroma_edges(img: Image.Image, radius: int = 0) -> Image.Image:
    """Remove magenta excess only near existing alpha-zero pixels; preserve alpha.

    Distance is Chebyshev distance in source pixels (8-neighbor square radius).
    The canvas exterior does not count as transparency. Where min(R,B)-G > 12,
    subtract that excess from R and B, leaving G, alpha, and other pixels intact
    (forge_matte.despill edge mode). This opt-in heuristic cannot distinguish
    real purple edge art from spill.
    """
    validate_despill_radius(radius)
    if radius == 0:
        return img
    pixels, _report = forge_matte.despill(img, "edge", radius, DESPILL_MARGIN)
    return Image.fromarray(pixels)


def _validate_alpha_mode(img: Image.Image, background_mode: str) -> None:
    alpha_min, alpha_max = img.getchannel("A").getextrema()
    if background_mode == "native_alpha" and (alpha_min == 255 or alpha_max == 0):
        raise ValueError(
            "native_alpha requires actual transparency and visible pixels; "
            "fully opaque RGB/RGBA or a baked checkerboard is not transparent. "
            "Use chroma_key for a generated magenta background or opaque for opaque art."
        )
    if background_mode == "opaque" and (alpha_min, alpha_max) != (255, 255):
        raise ValueError("opaque background mode requires fully opaque input; use native_alpha instead.")


def prepare_background(
    img: Image.Image, background_mode: str, threshold: int, edge_threshold: int,
    despill_radius: int = 0,
) -> Image.Image:
    """Legacy preparation: the binary magenta key plus optional edge despill, or an alpha check.

    Kept for make_anchor_layout and older callers; ``process`` uses prepare_sheet,
    which adds the soft key, alpha hygiene and the matte report.
    """
    validate_despill_radius(despill_radius)
    if background_mode not in BACKGROUND_MODES:
        raise ValueError(f"Unknown background mode: {background_mode}")
    prepared = img.convert("RGBA")
    if background_mode == "chroma_key":
        keyed = remove_bg_magenta(prepared, threshold, edge_threshold)
        return despill_chroma_edges(keyed, despill_radius)
    _validate_alpha_mode(prepared, background_mode)
    return prepared


def resolve_key_quality(quality: str, key: str, resampler: str) -> str:
    """``auto`` keys pixel art (nearest) with the binary magenta keyer and everything else softly.

    The binary keyer is magenta-only, so a green or blue key always gets the soft matte.
    """
    if quality not in KEY_QUALITIES:
        raise ValueError(f"Unknown key quality {quality!r}; use one of {', '.join(KEY_QUALITIES)}.")
    if key not in KEY_NAMES:
        raise ValueError(f"Unknown key {key!r}; use one of {', '.join(KEY_NAMES)}.")
    if quality == "auto":
        return "hard" if resampler == "nearest" and key == "magenta" else "soft"
    if quality == "hard" and key != "magenta":
        raise ValueError("--key-quality hard is the legacy magenta keyer; use soft or dominance for a green or blue key.")
    return quality


def key_sheet(img: Image.Image, *, quality: str = "auto", key: str = "magenta", threshold: int = 100,
              edge_threshold: int = 150, resampler: str = "lanczos") -> tuple[Image.Image, dict[str, Any]]:
    """Key a chroma sheet with forge_matte.key_still and measure its residue (S24, report v2 P2-2, D15).

    ``quality`` resolves as resolve_key_quality (``auto`` is the binary key for
    nearest/pixel art on magenta, soft otherwise). ``hard`` is the legacy
    binary #FF00FF key with the given thresholds (bit exact, S03): it never
    estimates a backdrop, so the key and the QA key are #FF00FF. ``soft`` is
    the still-image soft matte (forge_matte.STILL_KEY_PARAMS) against the
    backdrop key estimated from the border, with interior despill when the
    subject owns no key-coloured material; key_still mattes only the parts of
    the sheet that hold content, byte for byte the whole-sheet result (D15).
    ``dominance`` is the fast dominance key. An image with real transparency
    and no key backdrop is returned unchanged (quality ``native_alpha``). The
    info dict is key_still's: ``quality``, ``requested_quality`` (as given
    here), ``key``, ``key_estimate``, the soft ``params``, ``thresholds``
    (hard) and ``qa`` (forge_matte.matte_qa of the result).
    """
    resolved = resolve_key_quality(quality, key, resampler)
    backdrop = forge_matte.DECLARED_KEYS["magenta"] if resolved == "hard" else key
    keyed, info = forge_matte.key_still(img, resolved, backdrop, resampler, threshold=threshold,
                                        edge_threshold=edge_threshold)
    info["requested_quality"] = quality
    return keyed, info


# --------------------------------------------------------------------------- grid options

@dataclass(frozen=True)
class GridOptions:
    """Every sheet-splitting setting in one validated object (partial S28).

    The first eighteen fields keep the legacy ``split_grid`` parameter order, so
    ``GridOptions(rows, cols, cell_size, threshold, edge_threshold, ...)`` and the
    old keyword calls still work; the rest are geometry v2. ``anchor_mode`` None
    means ``feet`` for feet alignment and ``center`` otherwise; ``alpha_hygiene``
    None means ``both`` for native_alpha and ``none`` otherwise.
    """

    rows: int
    cols: int
    cell_size: int
    threshold: int = 100
    edge_threshold: int = 150
    fit_scale: float = 0.85
    trim_border_px: int = 4
    edge_clean_depth: int = 3
    align: str = "center"
    shared_scale: bool = False
    component_mode: str = "all"
    component_padding: int = 0
    min_component_area: int = 1
    edge_touch_margin: int = 0
    scale_strategy: str = "fit"
    background_mode: str = "chroma_key"
    resampler: str = "lanczos"
    despill_radius: int = 0
    key_quality: str = "auto"
    key: str = "magenta"
    alpha_geometry_threshold: int = forge_core.ALPHA_GEOMETRY_THRESHOLD
    connectivity: int = 8
    anchor_mode: str | None = None
    anchor_px: tuple[float, float] | None = None
    alpha_hygiene: str | None = None
    alpha_floor: int = 4
    pixel_scale: int | None = None
    logical_pixel: int = 1
    legacy_fractional_nearest: bool = False
    grid_rounding: str = "exact"
    pad_to_grid: bool = False

    def __post_init__(self) -> None:
        if min(self.rows, self.cols, self.cell_size) <= 0:
            raise ValueError("Grid rows, columns, and output cell size must be positive.")
        if not math.isfinite(self.fit_scale) or not 0 < self.fit_scale <= 1:
            raise ValueError("fit_scale must be finite and in (0, 1].")
        if (min(self.trim_border_px, self.edge_clean_depth, self.component_padding, self.edge_touch_margin) < 0
                or self.min_component_area < 1):
            raise ValueError("Padding and edge settings must be nonnegative; component area must be positive.")
        if self.background_mode not in BACKGROUND_MODES:
            raise ValueError(f"Unknown background mode: {self.background_mode}")
        if self.background_mode == "opaque" and self.rows * self.cols > 1:
            raise ValueError(
                "opaque input cannot be split into sprite cells: every cell is one full-cell component that "
                "touches its edges (S22). Package complete opaque frames with assemble_frames.py, or supply "
                "native_alpha or chroma_key art.")
        if self.resampler not in RESAMPLERS:
            raise ValueError(f"Unknown resampler: {self.resampler}")
        validate_despill_radius(self.despill_radius)
        resolve_key_quality(self.key_quality, self.key, self.resampler)
        for name, value, choices in (("align", self.align, ALIGN_MODES),
                                     ("component mode", self.component_mode, COMPONENT_MODES),
                                     ("scale strategy", self.scale_strategy, SCALE_STRATEGIES),
                                     ("grid rounding", self.grid_rounding, GRID_ROUNDINGS)):
            if value not in choices:
                raise ValueError(f"Unknown {name} {value!r}; use one of {', '.join(choices)}.")
        if self.anchor_mode is not None and self.anchor_mode not in ANCHOR_MODES:
            raise ValueError(f"Unknown anchor mode {self.anchor_mode!r}; use one of {', '.join(ANCHOR_MODES)}.")
        if self.alpha_hygiene is not None and self.alpha_hygiene not in forge_core.HYGIENE_MODES:
            raise ValueError(f"Unknown alpha hygiene {self.alpha_hygiene!r}; use one of "
                             f"{', '.join(forge_core.HYGIENE_MODES)}.")
        if self.connectivity not in (4, 8):
            raise ValueError("Connectivity must be 4 or 8.")
        if not 0 <= self.alpha_geometry_threshold <= 254:
            raise ValueError("The alpha geometry threshold must be 0-254 (pixels with alpha above it count).")
        if not 0 <= self.alpha_floor <= 255:
            raise ValueError("The alpha floor must be 0-255.")
        if self.pixel_scale is not None and self.pixel_scale < 1:
            raise ValueError("--pixel-scale must be a positive integer.")
        if self.logical_pixel < 1:
            raise ValueError("--logical-pixel must be a positive integer.")
        if (self.pixel_scale is not None or self.logical_pixel > 1) and self.resampler != "nearest":
            raise ValueError("--pixel-scale and --logical-pixel need --resampler nearest.")
        if self.logical_pixel > 1 and self.scale_strategy == "fit":
            raise ValueError("--logical-pixel samples logical-pixel centres on the cell grid; use --scale-strategy "
                             "preserve or registered.")
        if self.anchor_px is not None:
            if self.scale_strategy == "fit":
                raise ValueError("--anchor-px needs --scale-strategy preserve or registered.")
            if len(self.anchor_px) != 2 or not all(math.isfinite(float(v)) for v in self.anchor_px):
                raise ValueError("--anchor-px needs two finite numbers: X,Y in source-cell pixels.")
        if self.legacy_fractional_nearest and self.scale_strategy == "registered":
            raise ValueError("--legacy-fractional-nearest restores the old fit/preserve resizing; registered sheets "
                             "need an integer nearest scale or lanczos.")

    @property
    def chroma(self) -> bool:
        return self.background_mode == "chroma_key"

    @property
    def ground_align(self) -> bool:
        return self.align in ("feet", "bottom")

    @property
    def resolved_anchor_mode(self) -> str:
        return self.anchor_mode or ("feet" if self.ground_align else "center")

    @property
    def hygiene_mode(self) -> str:
        return self.alpha_hygiene or ("both" if self.background_mode == "native_alpha" else "none")

    @property
    def ground_anchor(self) -> bool:
        """True when the anchor is a ground point (registered sheets then sit on their lowest anchor)."""
        mode = self.resolved_anchor_mode
        return mode in ("feet", "stance", "bbox") or (mode == "legacy-p98" and self.ground_align)

    @property
    def target(self) -> tuple[float, float]:
        """Output point that receives the anchor: cell centre, or the feet line above the bottom padding."""
        cell = self.cell_size
        if self.ground_align:
            return cell / 2, float(cell - max(0, int(cell * (1 - self.fit_scale) * 0.5)))
        return cell / 2, cell / 2


@dataclass
class SheetResult:
    """Everything process_sheet measured: frames, per-frame records and the sheet-level report."""

    frames: list[Image.Image]
    info: list[dict[str, Any]]
    cleaned: Image.Image
    report: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- small helpers

def resampling_filter(resampler: str) -> Image.Resampling:
    if resampler not in RESAMPLERS:
        raise ValueError(f"Unknown resampler: {resampler}")
    return RESAMPLERS[resampler]


def trim_border(img: Image.Image, px: int = 4) -> Image.Image:
    width, height = img.size
    if width > px * 2 and height > px * 2:
        return img.crop((px, px, width - px, height - px))
    return img


def clean_edges(img: Image.Image, depth: int = 3) -> Image.Image:
    """Clear dark (every channel < 40) or near-magenta (distance < 150) visible pixels in the
    outer ``depth`` rows and columns of a chroma cell. Returns a new image."""
    pixels = np.array(img.convert("RGBA"))
    if depth <= 0:
        return Image.fromarray(pixels)
    height, width = pixels.shape[:2]
    # The four border strips are views; a corner seen twice gets the same decision (cleared pixels are skipped).
    for strip in (pixels[:depth], pixels[max(0, height - depth):], pixels[:, :depth], pixels[:, max(0, width - depth):]):
        rgb = strip[..., :3].astype(np.int32)
        dark = (rgb < 40).all(axis=-1)
        near_key = (rgb[..., 0] - 255) ** 2 + rgb[..., 1] ** 2 + (rgb[..., 2] - 255) ** 2 < 150 * 150
        strip[(strip[..., 3] != 0) & (dark | near_key)] = 0
    return Image.fromarray(pixels)


def pad_bbox(bbox: tuple[int, int, int, int], padding: int, width: int, height: int) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = bbox
    return (
        max(0, x0 - padding),
        max(0, y0 - padding),
        min(width, x1 + padding),
        min(height, y1 + padding),
    )


def bbox_touches_edge(
    bbox: Sequence[float] | None, width: int, height: int, margin: int = 0
) -> bool:
    if not bbox:
        return False
    x0, y0, x1, y1 = bbox
    return x0 <= margin or y0 <= margin or x1 >= width - margin or y1 >= height - margin


def alpha_core_area(alpha: np.ndarray, horizontal_fraction: float = 0.5) -> int:
    """Visible pixels in the central band of a cell: the legacy (v1) body-scale proxy."""
    width = alpha.shape[1]
    half_width = max(1, int(round(width * horizontal_fraction / 2)))
    center_x = width // 2
    return int(np.count_nonzero(alpha[:, max(0, center_x - half_width):min(width, center_x + half_width)]))


def _mask_bbox(mask: np.ndarray) -> tuple[int, int, int, int] | None:
    return forge_core.subject_bbox(mask)


def _integer_ratio(scale: float) -> tuple[int, int] | None:
    """``(N, 1)`` or ``(1, N)`` when ``scale`` is a whole-pixel nearest scale, else None."""
    if scale >= 1:
        whole = round(scale)
        return (whole, 1) if abs(scale - whole) <= 1e-6 * whole else None
    whole = round(1.0 / scale)
    return (1, whole) if whole >= 1 and abs(1.0 / scale - whole) <= 1e-6 * whole else None


def _legacy_p98_anchor(mask: np.ndarray, bbox: tuple[int, int, int, int], align: str) -> tuple[float, float]:
    """The cfed170 anchor (``--anchor-mode legacy-p98``): bbox centre for centre alignment; otherwise
    the 98th-percentile row index and the median column of the lowest 15% of pixels, preferring the
    central 20-80% of the cell."""
    x0, y0, x1, y1 = bbox
    if align == "center":
        return ((x0 + x1) / 2, (y0 + y1) / 2)
    ys, xs = np.nonzero(mask)
    inside = (xs >= x0) & (xs < x1) & (ys >= y0) & (ys < y1)
    xs = xs[inside]
    ys = ys[inside]
    if xs.size == 0:
        return ((x0 + x1) / 2, float(y1))
    width = mask.shape[1]
    central = (xs >= width * 0.2) & (xs <= width * 0.8)
    if int(np.count_nonzero(central)) >= 8:
        xs = xs[central]
        ys = ys[central]
    lower = ys >= float(np.percentile(ys, 85))
    anchor_x = float(np.median(xs[lower])) if xs[lower].size else float(np.median(xs))
    return (anchor_x, float(np.percentile(ys, 98)))


def _off_grid_edges(frame: np.ndarray, block: int, origin: Sequence[int]) -> int:
    """Colour changes that do not fall on the ``block``-pixel output grid starting at ``origin``.

    0 means every logical pixel became a uniform ``block`` x ``block`` square (S06 run-length QC).
    """
    if block <= 1:
        return 0
    height, width = frame.shape[:2]
    change_x = np.any(frame[:, 1:] != frame[:, :-1], axis=-1)
    change_y = np.any(frame[1:] != frame[:-1], axis=-1)
    off_x = (np.arange(1, width) - int(origin[0])) % block != 0
    off_y = (np.arange(1, height) - int(origin[1])) % block != 0
    return int(change_x[:, off_x].sum() + change_y[off_y, :].sum())


# --------------------------------------------------------------------------- sheet preparation and measurement

def prepare_sheet(img: Image.Image, options: GridOptions) -> tuple[Image.Image, dict[str, Any]]:
    """Key or validate a whole sheet, despill, then apply alpha hygiene (DOC-04, report v2 P1-4).

    Returns the RGBA sheet and ``{"matte": key info or None, "hygiene": report}``.
    """
    rgba = img if img.mode == "RGBA" else img.convert("RGBA")
    report: dict[str, Any] = {"matte": None}
    if options.chroma:
        cleaned, matte = key_sheet(rgba, quality=options.key_quality, key=options.key, threshold=options.threshold,
                                   edge_threshold=options.edge_threshold, resampler=options.resampler)
        if options.despill_radius:
            pixels, despill_report = forge_matte.despill(cleaned, "edge", options.despill_radius, DESPILL_MARGIN,
                                                         key=options.key)
            cleaned = Image.fromarray(pixels)
            matte["despill"] = despill_report
            # The residue checks judge the published sheet: measure it again after the despill.
            matte["qa_after_despill"] = forge_matte.matte_qa(pixels, matte["key"])
        report["matte"] = matte
    else:
        _validate_alpha_mode(rgba, options.background_mode)
        cleaned = rgba
    if options.hygiene_mode == "none":
        report["hygiene"] = {"mode": "none"}
    else:
        cleaned, report["hygiene"] = forge_core.alpha_hygiene(cleaned, options.hygiene_mode, floor=options.alpha_floor)
    return cleaned, report


def _cell_boxes(width: int, height: int, options: GridOptions) -> list[tuple[int, int, int, int]]:
    rows, cols = options.rows, options.cols
    if width < cols or height < rows:
        raise ValueError(f"Grid cells must contain at least one source pixel: a {width}x{height} image cannot "
                         f"hold {rows} rows x {cols} columns.")
    if options.grid_rounding == "nearest":
        return forge_core.rounded_grid_boxes(width, height, rows, cols)
    if width % cols or height % rows:
        raise ValueError(
            f"Image {width}x{height} is not divisible into {rows} rows x {cols} columns; refusing to discard "
            "remainder pixels. Pass --pad-to-grid (lossless transparent padding) or --grid-rounding nearest "
            "(cells differ by at most 1 px)."
        )
    cell_w, cell_h = width // cols, height // rows
    return [(col * cell_w, row * cell_h, (col + 1) * cell_w, (row + 1) * cell_h)
            for row in range(rows) for col in range(cols)]


def _measure_cell(cell: Image.Image, options: GridOptions) -> dict[str, Any]:
    """Trim and clean one chroma cell, select components and measure its subject and anchor.

    Boxes and the anchor are in trimmed-frame pixels; ``trim`` maps them to the cell.
    """
    width, height = cell.size
    trimmable = options.chroma and min(width, height) > 2 * options.trim_border_px
    trim = options.trim_border_px if trimmable else 0
    frame = trim_border(cell, trim) if trim else cell
    if options.chroma and options.edge_clean_depth > 0:
        frame = clean_edges(frame, depth=options.edge_clean_depth)
    pixels = np.array(frame.convert("RGBA"))
    alpha = pixels[..., 3]
    geometry = alpha > options.alpha_geometry_threshold
    components = forge_core.connected_components(
        geometry, min_area=options.min_component_area, connectivity=options.connectivity, with_masks=True)
    kept = components[:1] if options.component_mode == "largest" else components
    kept_mask = np.zeros(geometry.shape, bool)
    for component in kept:
        x0, y0, x1, y1 = component["bbox"]
        kept_mask[y0:y1, x0:x1] |= component["mask"]
    if options.component_mode == "largest" or options.min_component_area > 1:
        # A bounding-box crop alone keeps unrelated pixels inside that box; faint pixels stay with
        # the component they fringe.
        keep = kept_mask.copy()
        if options.alpha_geometry_threshold > 0:
            keep |= (alpha > 0) & ~geometry & forge_core.dilate_square(kept_mask, FAINT_ATTACH_RADIUS)
        pixels[~keep] = 0
    main = kept[0] if kept else None
    subject_box = _mask_bbox(kept_mask)
    record: dict[str, Any] = {
        "trim": (trim, trim), "frame": pixels, "frame_size": (pixels.shape[1], pixels.shape[0]),
        "component_count": len(components), "main": main, "subject_box": subject_box,
        "kept_mask": kept_mask, "empty": main is None, "anchor": None, "crop_box": None,
    }
    if main is None:
        return record
    main_box = tuple(main["bbox"])
    main_mask = np.zeros(geometry.shape, bool)
    main_mask[main_box[1]:main_box[3], main_box[0]:main_box[2]] = main["mask"]
    record["crop_box"] = (pad_bbox(main_box, options.component_padding, pixels.shape[1], pixels.shape[0])
                          if options.component_mode == "largest" else subject_box)
    mode = options.resolved_anchor_mode
    if options.anchor_px is not None:
        anchor = (float(options.anchor_px[0]) - trim, float(options.anchor_px[1]) - trim)
    elif mode == "legacy-p98":
        anchor_box = main_box if options.component_mode == "largest" else subject_box
        anchor = _legacy_p98_anchor(kept_mask, anchor_box, options.align)
    elif mode in ("feet", "stance"):
        band = max(1, forge_core.round_half_up(ANCHOR_BAND_FRACTION * (main_box[3] - main_box[1])))
        anchor = forge_core.anchor_from_mask(main_mask, mode, band, subject_bbox=main_box)
    else:
        anchor = forge_core.anchor_from_mask(kept_mask, mode, subject_bbox=subject_box)
    record["anchor"] = (float(anchor[0]), float(anchor[1]))
    return record


def _frame_record(m: dict[str, Any], grid: tuple[int, int], box: Sequence[int], pad_offset: Sequence[int],
                  options: GridOptions) -> dict[str, Any]:
    """The per-frame metadata fields that do not depend on the output placement."""
    width, height = m["frame_size"]
    trim_x, trim_y = m["trim"]
    subject_box = m["subject_box"]
    main = m["main"]
    largest = options.component_mode == "largest"
    alpha = m["frame"][..., 3]
    core = alpha_core_area(alpha)
    source_rect = None
    if subject_box is not None:
        origin_x = box[0] + trim_x - pad_offset[0]
        origin_y = box[1] + trim_y - pad_offset[1]
        source_rect = [origin_x + subject_box[0], origin_y + subject_box[1],
                       origin_x + subject_box[2], origin_y + subject_box[3]]
    source_edge_touch = bbox_touches_edge(subject_box, width, height, options.edge_touch_margin)
    return {
        "grid": list(grid),
        "source_box": list(box),
        "trim_offset": [trim_x, trim_y],
        "source_rect": source_rect,
        "component_mode": options.component_mode,
        "component_count": m["component_count"],
        "selected_component_area": int(main["area"]) if largest and main else None,
        "selected_component_bbox": list(main["bbox"]) if largest and main else None,
        "crop_bbox": list(m["crop_box"]) if m["crop_box"] else None,
        "subject_bbox": list(subject_box) if subject_box else None,
        "source_frame_size": [width, height],
        "is_empty": bool(m["empty"]),
        "subject_area": int(m["kept_mask"].sum()),
        "body_core_area": core,
        "body_area_fraction": core / max(1, width * height) if not m["empty"] else 0.0,
        "anchor_source": list(m["anchor"]) if m["anchor"] else None,
        "anchor_cell": [m["anchor"][0] + trim_x, m["anchor"][1] + trim_y] if m["anchor"] else None,
        "source_edge_touch": source_edge_touch,
        "output_edge_touch": False,
        "edge_touch": source_edge_touch,
        "paste_clamped": False,
        "scale_strategy": options.scale_strategy,
    }


def _finish_record(info: dict[str, Any], frame: Image.Image, options: GridOptions) -> None:
    """Measure the placed frame: aligned box, output edge contact and visible subject height (S01)."""
    alpha = np.asarray(frame.getchannel("A"))
    aligned = forge_core.subject_bbox(alpha, options.alpha_geometry_threshold)
    cell = options.cell_size
    output_edge_touch = bbox_touches_edge(aligned, cell, cell, options.edge_touch_margin)
    height = float(aligned[3] - aligned[1]) if aligned else 0.0
    info.update(
        aligned_bbox=list(aligned) if aligned else None,
        output_edge_touch=output_edge_touch,
        edge_touch=bool(info["source_edge_touch"]) or output_edge_touch,
        output_subject_height_px=height,
        body_scale=height / cell,
    )


def _empty_placement(info: dict[str, Any], options: GridOptions) -> None:
    info.update(output_size=[0, 0], paste_position=[0, 0], unclamped_paste_position=[0, 0],
                source_to_output_scale=0.0, aligned_bbox=None, output_subject_height_px=0.0, body_scale=0.0)
    if options.scale_strategy != "fit":
        info["anchor_target"] = list(options.target)


# --------------------------------------------------------------------------- placement strategies

def _check_nearest_scale(scale: float, options: GridOptions, where: str) -> tuple[int, int] | None:
    ratio = _integer_ratio(scale)
    if ratio is None and not options.legacy_fractional_nearest:
        raise ValueError(
            f"--resampler nearest keeps pixel art crisp only at integer scales (N or 1/N), but {where} needs "
            f"{scale:.4f}. Pass --pixel-scale N (with --logical-pixel M for upscaled art), change --cell-size or "
            "--fit-scale so the scale is whole, use lanczos, or restore the old behaviour with "
            "--legacy-fractional-nearest (S06)."
        )
    return ratio


def _place_fit(measured: list[dict[str, Any]], infos: list[dict[str, Any]], options: GridOptions) -> list[Image.Image]:
    """Legacy fit: each subject box is scaled into the cell (per frame, or one --shared-scale).

    The scale and placement come from the measured subject box; faint pixels just outside it
    travel with the subject instead of being cut off.
    """
    cell = options.cell_size
    resize_filter = resampling_filter(options.resampler)
    valid = [m for m in measured if not m["empty"]]
    common_scale = None
    if options.pixel_scale is not None:
        common_scale = float(options.pixel_scale)
    elif options.shared_scale and valid:
        max_width = max(m["crop_box"][2] - m["crop_box"][0] for m in valid)
        max_height = max(m["crop_box"][3] - m["crop_box"][1] for m in valid)
        common_scale = min(cell / max_width, cell / max_height) * options.fit_scale
    frames = []
    for m, info in zip(measured, infos):
        canvas = Image.new("RGBA", (cell, cell), (0, 0, 0, 0))
        if m["empty"]:
            _empty_placement(info, options)
            frames.append(canvas)
            continue
        cx0, cy0, cx1, cy1 = m["crop_box"]
        crop_w, crop_h = cx1 - cx0, cy1 - cy0
        scale = common_scale or min(cell / crop_w, cell / crop_h) * options.fit_scale
        ratio = None
        if options.resampler == "nearest" and options.pixel_scale is None:
            ratio = _check_nearest_scale(scale, options, f"frame {info['grid']}")
        elif options.pixel_scale is not None:
            ratio = (options.pixel_scale, 1)
        new_width, new_height = max(1, int(crop_w * scale)), max(1, int(crop_h * scale))
        paste_x = (cell - new_width) // 2
        if options.ground_align:
            paste_y = cell - new_height - max(0, int(cell * (1 - options.fit_scale) * 0.5))
        else:
            paste_y = (cell - new_height) // 2
        frame_image = Image.fromarray(m["frame"])
        visible = _mask_bbox(m["frame"][..., 3] > 0) or m["crop_box"]
        ex0, ey0 = min(cx0, visible[0]), min(cy0, visible[1])
        ex1, ey1 = max(cx1, visible[2]), max(cy1, visible[3])
        if (ex0, ey0, ex1, ey1) == (cx0, cy0, cx1, cy1):
            piece = frame_image.crop((cx0, cy0, cx1, cy1)).resize((new_width, new_height), resize_filter)
            position = (paste_x, paste_y)
        else:
            sx, sy = new_width / crop_w, new_height / crop_h
            size = (max(1, forge_core.round_half_up((ex1 - ex0) * sx)),
                    max(1, forge_core.round_half_up((ey1 - ey0) * sy)))
            piece = frame_image.crop((ex0, ey0, ex1, ey1)).resize(size, resize_filter)
            position = (paste_x - forge_core.round_half_up((cx0 - ex0) * sx),
                        paste_y - forge_core.round_half_up((cy0 - ey0) * sy))
        canvas.paste(piece, position)
        anchor_x, anchor_y = m["anchor"]
        info.update(
            output_size=[new_width, new_height],
            paste_position=[paste_x, paste_y],
            source_to_output_scale=scale,
            output_anchor=[(anchor_x - cx0) * new_width / crop_w + paste_x,
                           (anchor_y - cy0) * new_height / crop_h + paste_y],
        )
        if ratio is not None:
            info["pixel_grid_off_edges"] = _off_grid_edges(np.asarray(canvas), ratio[0], position)
        _finish_record(info, canvas, options)
        frames.append(canvas)
    return frames


def _common_scale(measured: list[dict[str, Any]], options: GridOptions) -> tuple[float, tuple[int, int] | None]:
    """One source-to-output scale for every cell: ``--pixel-scale``/``--logical-pixel``, or the
    largest (trimmed) source cell fitted by ``fit_scale``. Nearest must land on whole pixels."""
    if options.pixel_scale is not None or options.logical_pixel > 1:
        numerator, denominator = options.pixel_scale or 1, options.logical_pixel
        return numerator / denominator, (numerator, denominator)
    source_w = max(m["frame_size"][0] for m in measured)
    source_h = max(m["frame_size"][1] for m in measured)
    scale = min(options.cell_size / source_w, options.cell_size / source_h) * options.fit_scale
    if options.resampler != "nearest":
        return scale, None
    where = f"cell {options.cell_size} / source {source_w}x{source_h} x fit {options.fit_scale:g}"
    return scale, _check_nearest_scale(scale, options, where)


def _registration_anchor(measured: list[dict[str, Any]], options: GridOptions) -> tuple[float, float] | None:
    """The cell point a registered sheet pins to the target: ``--anchor-px``, else the median anchor
    column on the lowest anchor row (the floor), or the median anchor for centre-type anchors."""
    if options.anchor_px is not None:
        return float(options.anchor_px[0]), float(options.anchor_px[1])
    anchors = [(m["anchor"][0] + m["trim"][0], m["anchor"][1] + m["trim"][1]) for m in measured if m["anchor"]]
    if not anchors:
        return None
    xs, ys = zip(*anchors)
    y = max(ys) if options.ground_anchor else float(np.median(ys))
    return float(np.median(xs)), float(y)


def _offset(target: Sequence[float], anchor: Sequence[float], scale: float) -> tuple[int, int]:
    return (forge_core.round_half_up(target[0] - anchor[0] * scale),
            forge_core.round_half_up(target[1] - anchor[1] * scale))


def _clamp_offset(offset: tuple[int, int], box: Sequence[float], scale: float, cell: int) -> tuple[int, int]:
    """Shift by whole pixels until the scaled subject box lies inside the cell (left/top wins)."""
    clamped = []
    for axis in (0, 1):
        low = math.ceil(-box[axis] * scale - _EPSILON)
        high = math.floor(cell - box[axis + 2] * scale + _EPSILON)
        clamped.append(max(low, min(high, offset[axis])))
    return clamped[0], clamped[1]


def _resample_cell(canvas: np.ndarray, scale: float, ratio: tuple[int, int] | None, offset: Sequence[int],
                   options: GridOptions) -> Image.Image:
    """Map cell pixel ``x`` to output ``x * scale + offset`` on the shared grid (S05, S06)."""
    size = (options.cell_size, options.cell_size)
    if options.resampler != "nearest":
        return forge_core.resample_rgba(canvas, scale, "lanczos", anchor_src=(0, 0), anchor_dst=offset, out_size=size)
    numerator, denominator = ratio
    if numerator > 1 and denominator > 1:
        # Sample the centre of every logical pixel on the cell grid, then repeat it numerator times.
        height, width = canvas.shape[:2]
        logical_size = (-(-width // denominator), -(-height // denominator))
        canvas = np.asarray(forge_core.resample_rgba(canvas, 1.0 / denominator, "nearest", anchor_src=(0, 0),
                                                     anchor_dst=(0, 0), out_size=logical_size))
        return forge_core.resample_rgba(canvas, numerator, "nearest", anchor_src=(0, 0), anchor_dst=offset,
                                        out_size=size)
    return forge_core.resample_rgba(canvas, numerator / denominator, "nearest", anchor_src=(0, 0),
                                    anchor_dst=offset, out_size=size)


def _cell_canvas(m: dict[str, Any], cell_size: tuple[int, int]) -> np.ndarray:
    canvas = np.zeros((cell_size[1], cell_size[0], 4), np.uint8)
    trim_x, trim_y = m["trim"]
    frame = m["frame"]
    canvas[trim_y:trim_y + frame.shape[0], trim_x:trim_x + frame.shape[1]] = frame
    return canvas


def _place_common_grid(measured: list[dict[str, Any]], infos: list[dict[str, Any]], options: GridOptions,
                       cell_sizes: list[tuple[int, int]], report: dict[str, Any]) -> list[Image.Image]:
    """preserve and registered: one scale and one sampling grid, whole-pixel offsets only.

    ``preserve`` moves each frame so its own anchor lands on the target (clamped
    inside the cell); ``registered`` gives every frame the same offset, so
    jumps, bob and recoil drawn in the sheet survive (S07).
    """
    scale, ratio = _common_scale(measured, options)
    if options.resampler == "nearest" and ratio is None:
        return _place_legacy_preserve(measured, infos, options, scale)
    target = options.target
    shared = None
    if options.scale_strategy == "registered":
        reference = _registration_anchor(measured, options)
        report["registration_anchor"] = list(reference) if reference else None
        if reference is not None:
            shared = _offset(target, reference, scale)
    frames = []
    for m, info, cell_size in zip(measured, infos, cell_sizes):
        info["source_to_output_scale"] = scale
        info["anchor_target"] = list(target)
        if m["empty"]:
            _empty_placement(info, options)
            frames.append(Image.new("RGBA", (options.cell_size, options.cell_size), (0, 0, 0, 0)))
            continue
        anchor = info["anchor_cell"]
        trim_x, trim_y = m["trim"]
        box = [m["subject_box"][0] + trim_x, m["subject_box"][1] + trim_y,
               m["subject_box"][2] + trim_x, m["subject_box"][3] + trim_y]
        unclamped = shared if shared is not None else _offset(target, anchor, scale)
        offset = unclamped if shared is not None else _clamp_offset(unclamped, box, scale, options.cell_size)
        frame = _resample_cell(_cell_canvas(m, cell_size), scale, ratio, offset, options)
        info.update(
            offset_px=list(offset),
            output_size=[forge_core.round_half_up((box[2] - box[0]) * scale),
                         forge_core.round_half_up((box[3] - box[1]) * scale)],
            paste_position=[math.floor(box[0] * scale + offset[0] + _EPSILON),
                            math.floor(box[1] * scale + offset[1] + _EPSILON)],
            unclamped_paste_position=[math.floor(box[0] * scale + unclamped[0] + _EPSILON),
                                      math.floor(box[1] * scale + unclamped[1] + _EPSILON)],
            paste_clamped=tuple(offset) != tuple(unclamped),
            output_anchor=[anchor[0] * scale + offset[0], anchor[1] * scale + offset[1]],
        )
        if ratio is not None:
            info["pixel_grid_off_edges"] = _off_grid_edges(np.asarray(frame), ratio[0], offset)
        _finish_record(info, frame, options)
        frames.append(frame)
    offsets = np.array([info["offset_px"] for info in infos if "offset_px" in info], float)
    if offsets.size:
        shift = offsets - np.median(offsets, axis=0)
        report["injected_shift_px"] = {"x": [float(shift[:, 0].min()), float(shift[:, 0].max())],
                                       "y": [float(shift[:, 1].min()), float(shift[:, 1].max())]}
    report["scale"] = scale
    report["scale_ratio"] = list(ratio) if ratio else None
    return frames


def _place_legacy_preserve(measured: list[dict[str, Any]], infos: list[dict[str, Any]], options: GridOptions,
                           scale: float) -> list[Image.Image]:
    """``--legacy-fractional-nearest`` preserve: the cfed170 crop, nearest resize and clamped paste."""
    cell = options.cell_size
    target_x, target_y = options.target
    frames = []
    for m, info in zip(measured, infos):
        info["anchor_target"] = [target_x, target_y]
        canvas = Image.new("RGBA", (cell, cell), (0, 0, 0, 0))
        if m["empty"]:
            _empty_placement(info, options)
            frames.append(canvas)
            continue
        cx0, cy0, cx1, cy1 = m["crop_box"]
        crop = Image.fromarray(m["frame"]).crop((cx0, cy0, cx1, cy1))
        new_width = max(1, int(round(crop.width * scale)))
        new_height = max(1, int(round(crop.height * scale)))
        anchor_x, anchor_y = m["anchor"]
        paste_x = int(round(target_x - (anchor_x - cx0) * scale))
        paste_y = int(round(target_y - (anchor_y - cy0) * scale))
        unclamped = [paste_x, paste_y]
        paste_x = max(0, min(cell - new_width, paste_x))
        paste_y = max(0, min(cell - new_height, paste_y))
        canvas.paste(crop.resize((new_width, new_height), RESAMPLERS["nearest"]), (paste_x, paste_y))
        info.update(
            output_size=[new_width, new_height],
            paste_position=[paste_x, paste_y],
            unclamped_paste_position=unclamped,
            paste_clamped=[paste_x, paste_y] != unclamped,
            source_to_output_scale=scale,
            output_anchor=[(anchor_x - cx0) * scale + paste_x, (anchor_y - cy0) * scale + paste_y],
        )
        _finish_record(info, canvas, options)
        frames.append(canvas)
    return frames


def process_sheet(img: Image.Image, options: GridOptions) -> SheetResult:
    """Key or validate ``img``, split it into ``rows`` x ``cols`` cells and place every frame.

    The grid is checked before any keying, so a bad layout fails fast. The
    report holds ``matte``, ``hygiene``, ``pad_offset``, the shared ``scale``
    (preserve/registered), ``registration_anchor`` and ``injected_shift_px``.
    """
    width, height = img.size
    if options.pad_to_grid:
        width, height = width + (-width % options.cols), height + (-height % options.rows)
    _cell_boxes(width, height, options)
    cleaned, report = prepare_sheet(img, options)
    pad_offset = (0, 0)
    if options.pad_to_grid:
        cleaned, pad_offset = forge_core.pad_to_grid(cleaned, options.rows, options.cols)
    report["pad_offset"] = list(pad_offset)
    boxes = _cell_boxes(cleaned.width, cleaned.height, options)
    measured, infos, cell_sizes = [], [], []
    for index, box in enumerate(boxes):
        m = _measure_cell(cleaned.crop(box), options)
        measured.append(m)
        cell_sizes.append((box[2] - box[0], box[3] - box[1]))
        infos.append(_frame_record(m, divmod(index, options.cols), box, pad_offset, options))
    if options.scale_strategy == "fit":
        frames = _place_fit(measured, infos, options)
    else:
        frames = _place_common_grid(measured, infos, options, cell_sizes, report)
    return SheetResult(frames, infos, cleaned, report)


def split_grid(img: Image.Image, *args: Any, **kwargs: Any) -> tuple[list[Image.Image], list[dict[str, Any]]]:
    """Split a sheet into placed frames and per-frame records.

    Call ``split_grid(img, GridOptions(...))`` or with the legacy parameters
    (``rows, cols, cell_size, threshold, edge_threshold, ...`` and keywords).
    """
    if len(args) == 1 and not kwargs and isinstance(args[0], GridOptions):
        options = args[0]
    else:
        options = GridOptions(*args, **kwargs)
    result = process_sheet(img, options)
    return result.frames, result.info


def summarize_frame_qc(frame_info: list[dict[str, Any]]) -> dict[str, Any]:
    """Sheet QC in output space (S02, S10): subject heights after scaling, not cell-area fractions.

    ``body_scale`` is the visible output subject height over the cell size;
    ``scale_reference_height_px`` is the median output subject height of the
    grounded frames (registered sheets leave airborne frames out), the value a
    version 2 scale profile stores. ``legacy_body_scale_*`` keep the v1 metric
    for version 1 profiles.
    """
    valid = [info for info in frame_info if not bool(info.get("is_empty"))]
    heights = np.asarray([float(info.get("output_subject_height_px", 0.0)) for info in valid
                          if info.get("aligned_bbox")], float)
    body = np.asarray([float(info.get("body_scale", 0.0)) for info in valid if info.get("aligned_bbox")], float)
    legacy = np.asarray([math.sqrt(float(info.get("body_area_fraction", 0.0))) for info in valid], float)
    anchor_x = np.asarray([float(info["anchor_source"][0]) / max(1, int(info["source_frame_size"][0]))
                           for info in valid if info.get("anchor_source")], float)
    anchor_y = np.asarray([float(info["anchor_source"][1]) / max(1, int(info["source_frame_size"][1]))
                           for info in valid if info.get("anchor_source")], float)
    grounded = [info for info in valid if info.get("aligned_bbox")]
    if grounded and all(info.get("scale_strategy") == "registered" for info in grounded):
        floor = max(float(info["aligned_bbox"][3]) for info in grounded)
        grounded = [info for info in grounded if floor - float(info["aligned_bbox"][3]) <= 1.0]
    reference = [float(info["output_subject_height_px"]) for info in grounded]

    def mean_cv(values: np.ndarray) -> tuple[float, float]:
        mean = float(np.mean(values)) if values.size else 0.0
        return mean, (float(np.std(values) / mean) if values.size and mean > 0 else 0.0)

    body_mean, body_cv = mean_cv(body)
    legacy_mean, legacy_cv = mean_cv(legacy)
    off_grid = [info["pixel_grid_off_edges"] for info in frame_info if "pixel_grid_off_edges" in info]
    return {
        "frame_count": len(frame_info),
        "valid_frame_count": len(valid),
        "empty_count": sum(bool(info.get("is_empty")) for info in frame_info),
        "edge_touch_count": sum(bool(info.get("edge_touch")) for info in frame_info),
        "paste_clamped_count": sum(bool(info.get("paste_clamped")) for info in frame_info),
        "body_scale_mean": body_mean,
        "body_scale_cv": body_cv,
        "legacy_body_scale_mean": legacy_mean,
        "legacy_body_scale_cv": legacy_cv,
        "output_subject_height_mean": float(np.mean(heights)) if heights.size else 0.0,
        "output_subject_height_median": float(np.median(heights)) if heights.size else 0.0,
        "scale_reference_height_px": float(np.median(reference)) if reference else 0.0,
        "scale_reference_frames": [info["grid"] for info in grounded],
        "anchor_x_std": float(np.std(anchor_x)) if anchor_x.size else 0.0,
        "anchor_y_std": float(np.std(anchor_y)) if anchor_y.size else 0.0,
        "anchor_y_mean": float(np.mean(anchor_y)) if anchor_y.size else 0.0,
        "pixel_grid_off_edges": int(sum(off_grid)) if off_grid else None,
    }


# --------------------------------------------------------------------------- Godot Sprite3D

def build_godot_sprite3d_metadata(
    metadata: dict[str, Any],
    world_height: float,
    locked_pixel_size: float | None = None,
    frame_dir: str = "",
) -> dict[str, Any]:
    """Build a Godot Sprite3D runtime contract from validated grid output.

    ``frame_dir`` is the POSIX path from the contract file to the frame PNGs
    (S19), so the listed frames resolve wherever the contract is written.
    """
    if world_height <= 0:
        raise ValueError("Godot Sprite3D world height must be greater than zero.")

    cell_size = int(metadata.get("cell_size", 0))
    labels = list(metadata.get("frame_labels") or [])
    if cell_size <= 0 or not labels:
        raise ValueError("Godot Sprite3D metadata requires processed grid frames.")

    origin = metadata.get("output_origin") or processing_output_origin(metadata)
    origin_x = float(origin[0])
    origin_y = float(origin[1])
    subject_height = float(
        dict(metadata.get("qc_summary") or {}).get("output_subject_height_mean", 0.0)
    )
    if subject_height <= 0:
        raise ValueError("Godot Sprite3D metadata requires a valid output subject height.")
    if locked_pixel_size is not None and locked_pixel_size <= 0:
        raise ValueError("Locked Godot Sprite3D pixel size must be greater than zero.")

    duration_ms = int(metadata.get("duration", 200))
    if duration_ms <= 0:
        raise ValueError("Animation duration must be greater than zero.")

    pixel_size = locked_pixel_size or (float(world_height) / subject_height)
    prefix = f"{frame_dir.rstrip('/')}/" if frame_dir else ""
    return {
        "schema": "generate2dsprite.godot_sprite3d.v1",
        "frame_size": [cell_size, cell_size],
        "output_origin": [origin_x, origin_y],
        # Godot's Sprite3D offset uses +Y upward from the texture center.
        "sprite3d_offset": [cell_size / 2 - origin_x, origin_y - cell_size / 2],
        "reference_subject_height_px": subject_height,
        "world_height": float(world_height),
        "recommended_pixel_size": pixel_size,
        "rendered_subject_height_world": subject_height * pixel_size,
        "scale_source": "scale_profile" if locked_pixel_size is not None else "measured_subject_height",
        "billboard": "enabled",
        "duration_ms": duration_ms,
        "fps": 1000.0 / duration_ms,
        "frames": [f"{prefix}{label}.png" for label in labels],
    }


def build_godot_sprite3d_bundle(
    action_contracts: dict[str, tuple[str, dict[str, Any]]],
    default_action: str,
    one_shot_actions: set[str] | None = None,
    max_world_height_drift: float = 0.02,
    max_pixel_size_drift: float = 0.02,
) -> dict[str, Any]:
    """Combine per-action Sprite3D contracts into one validated runtime bundle."""
    if not action_contracts:
        raise ValueError("Godot Sprite3D bundles require at least one action contract.")
    if default_action not in action_contracts:
        raise ValueError(f"Default action '{default_action}' is not present in the bundle.")
    if max_world_height_drift < 0:
        raise ValueError("Maximum world-height drift cannot be negative.")
    if max_pixel_size_drift < 0:
        raise ValueError("Maximum pixel-size drift cannot be negative.")

    one_shots = set(one_shot_actions or set())
    unknown_one_shots = one_shots.difference(action_contracts)
    if unknown_one_shots:
        raise ValueError(
            "One-shot actions are missing contracts: " + ", ".join(sorted(unknown_one_shots))
        )

    action_payload: dict[str, Any] = {}
    reference_world_height = 0.0
    reference_pixel_size = 0.0
    maximum_drift = 0.0
    maximum_pixel_size_drift = 0.0
    for action, (contract_ref, contract) in action_contracts.items():
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", action):
            raise ValueError(
                f"Invalid action name '{action}'; use lowercase letters, digits, hyphens, or underscores."
            )
        if contract.get("schema") != "generate2dsprite.godot_sprite3d.v1":
            raise ValueError(f"Action '{action}' is not a Godot Sprite3D v1 contract.")
        world_height = float(contract.get("world_height", 0.0))
        if world_height <= 0:
            raise ValueError(f"Action '{action}' has no valid world height.")
        if not list(contract.get("frames") or []):
            raise ValueError(f"Action '{action}' has no animation frames.")
        pixel_size = float(contract.get("recommended_pixel_size", 0.0))
        if pixel_size <= 0:
            raise ValueError(f"Action '{action}' has no valid recommended pixel size.")

        if reference_world_height <= 0:
            reference_world_height = world_height
            reference_pixel_size = pixel_size
        drift = abs(world_height - reference_world_height) / reference_world_height
        maximum_drift = max(maximum_drift, drift)
        if drift > max_world_height_drift:
            raise ValueError(
                f"Action '{action}' world-height drift {drift:.4f} exceeds "
                f"{max_world_height_drift:.4f}."
            )
        pixel_size_drift = abs(pixel_size - reference_pixel_size) / reference_pixel_size
        maximum_pixel_size_drift = max(maximum_pixel_size_drift, pixel_size_drift)
        if pixel_size_drift > max_pixel_size_drift:
            raise ValueError(
                f"Action '{action}' pixel-size drift {pixel_size_drift:.4f} exceeds "
                f"{max_pixel_size_drift:.4f}; reuse the reference action's scale profile."
            )
        action_payload[action] = {
            "contract": contract_ref,
            "loop": action not in one_shots,
        }

    return {
        "schema": "generate2dsprite.godot_sprite3d_bundle.v1",
        "default_action": default_action,
        "world_height": reference_world_height,
        "world_height_max_drift": maximum_drift,
        "pixel_size": reference_pixel_size,
        "pixel_size_max_drift": maximum_pixel_size_drift,
        "actions": action_payload,
    }


def _relative_posix(path: Path, base: Path, what: str) -> str:
    """POSIX path from directory ``base`` to ``path``; refuses another drive (no relative route)."""
    try:
        return Path(os.path.relpath(path, base)).as_posix()
    except ValueError:
        raise ValueError(f"{what} must be on the same drive as {base}; a relative path cannot reach {path}.") from None


def cmd_build_godot_bundle(args: argparse.Namespace) -> dict[str, Any]:
    """Validate per-action contracts and write a new bundle; never replaces an existing file (S19)."""
    output = args.output.resolve()
    if os.path.lexists(output):
        raise FileExistsError(f"Refusing to overwrite existing bundle: {output}")
    action_contracts: dict[str, tuple[str, dict[str, Any]]] = {}
    for action_spec in args.action:
        if "=" not in action_spec:
            raise ValueError("Each --action must use ACTION=PATH syntax.")
        action, raw_path = action_spec.split("=", 1)
        action = action.strip()
        if action in action_contracts:
            raise ValueError(f"Duplicate action '{action}'.")
        contract_path = Path(raw_path.strip()).resolve()
        if not contract_path.is_file():
            raise ValueError(f"Action contract does not exist: {contract_path}")
        try:
            contract = forge_core.read_json(contract_path)  # UTF-8 with or without a BOM (D28)
        except ValueError as error:
            raise ValueError(f"Action contract '{action}' is not valid JSON ({contract_path.name}): {error}") from None
        if not isinstance(contract, dict):
            raise ValueError(f"Action contract '{action}' must be a JSON object: {contract_path.name}")
        missing = [frame for frame in contract.get("frames") or []
                   if not (contract_path.parent / frame).is_file()]
        if missing:
            raise ValueError(f"Action '{action}' lists frames that do not resolve next to its contract: "
                             f"{', '.join(missing[:4])}")
        contract_ref = _relative_posix(contract_path, output.parent, f"Action contract '{action}'")
        action_contracts[action] = (contract_ref, contract)

    payload = build_godot_sprite3d_bundle(
        action_contracts,
        args.default_action,
        set(args.one_shot or []),
        args.max_world_height_drift,
        args.max_pixel_size_drift,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    forge_core.write_json(output, payload, no_clobber=True)
    summary = {"output": str(output), "actions": sorted(action_contracts),
               "default_action": args.default_action}
    print(json.dumps(summary))
    return summary


# --------------------------------------------------------------------------- scale profiles

SCALE_PROFILE_VERSION = 2
SCALE_PROFILE_SCHEMA = "generate2dsprite.scale_profile.v2"
SCALE_PROFILE_PROCESSING_KEYS = (
    "cell_size",
    "fit_scale",
    "trim_border",
    "edge_clean_depth",
    "align",
    "shared_scale",
    "scale_strategy",
    "component_mode",
    "component_padding",
    "min_component_area",
    "edge_touch_margin",
)
# Processing settings a version 2 profile also locks, with the values older profiles imply.
SCALE_PROFILE_OPTIONAL_KEYS = {
    "background_mode": "chroma_key",
    "resampler": "lanczos",
    "despill_radius": 0,
    "key_quality": "auto",
    "key": "magenta",
    "alpha_geometry_threshold": forge_core.ALPHA_GEOMETRY_THRESHOLD,
    "connectivity": 8,
    "anchor_mode": None,
    "alpha_hygiene": None,
    "alpha_floor": 4,
    "pixel_scale": None,
    "logical_pixel": 1,
}
PROFILE_KEYS = SCALE_PROFILE_PROCESSING_KEYS + tuple(SCALE_PROFILE_OPTIONAL_KEYS)


def processing_output_origin(processing: dict[str, Any]) -> list[float]:
    cell_size = int(processing["cell_size"])
    target_x = cell_size / 2
    if str(processing["align"]) in {"bottom", "feet"}:
        fit_scale = float(processing["fit_scale"])
        output_pad = max(0, int(cell_size * (1 - fit_scale) * 0.5))
        target_y = cell_size - output_pad
    else:
        target_y = cell_size / 2
    return [target_x, target_y]


def build_scale_profile(
    metadata: dict[str, Any],
    name: str,
    max_body_scale_drift: float,
) -> dict[str, Any]:
    """Write a version 2 profile: the processing contract plus the reference output height (S02)."""
    qc_summary = dict(metadata.get("qc_summary") or {})
    body_scale_mean = float(qc_summary.get("body_scale_mean", 0.0))
    reference_height = float(qc_summary.get("scale_reference_height_px", 0.0))
    if body_scale_mean <= 0 or reference_height <= 0:
        raise ValueError("Cannot create a scale profile without a measured output subject height.")

    processing = {key: metadata[key] for key in SCALE_PROFILE_PROCESSING_KEYS}
    for key, default in SCALE_PROFILE_OPTIONAL_KEYS.items():
        processing[key] = metadata.get(key, default)
    profile = {
        "version": SCALE_PROFILE_VERSION,
        "schema": SCALE_PROFILE_SCHEMA,
        "name": name,
        "processing": processing,
        "output_origin": list(metadata.get("output_origin") or processing_output_origin(processing)),
        "reference": {
            "target": metadata.get("target"),
            "mode": metadata.get("mode"),
            "rows": metadata.get("rows"),
            "cols": metadata.get("cols"),
            "body_scale_mean": body_scale_mean,
            "body_scale_cv": float(qc_summary.get("body_scale_cv", 0.0)),
            "anchor_y_mean": float(qc_summary.get("anchor_y_mean", 0.0)),
            "output_subject_height_px": reference_height,
            "legacy_body_scale_mean": float(qc_summary.get("legacy_body_scale_mean", 0.0)),
        },
        "qc": {
            "max_body_scale_drift": max_body_scale_drift,
        },
    }
    godot_contract = metadata.get("godot_sprite3d")
    if isinstance(godot_contract, dict):
        profile["godot_sprite3d"] = {
            "world_height": float(godot_contract["world_height"]),
            "pixel_size": float(godot_contract["recommended_pixel_size"]),
        }
    return profile


def load_scale_profile(path: Path) -> dict[str, Any]:
    """Read a version 1 or 2 profile; version 1 keeps its cfed170 contract and drift metric."""
    try:
        payload = forge_core.read_json(path)  # UTF-8 with or without a BOM (D28)
    except ValueError as error:
        raise ValueError(f"Scale profile {Path(path).name} is not valid JSON: {error}") from None
    if not isinstance(payload, dict):
        raise ValueError(f"Scale profile {Path(path).name} must be a JSON object.")
    version = payload.get("version")
    if version not in (1, 2):
        raise ValueError(f"Unsupported scale profile version {version!r}; expected 1 or 2.")
    processing = payload.get("processing")
    if not isinstance(processing, dict):
        raise ValueError("Scale profile is missing its processing contract.")
    missing = [key for key in SCALE_PROFILE_PROCESSING_KEYS if key not in processing]
    if missing:
        raise ValueError(f"Scale profile processing contract is missing: {', '.join(missing)}")
    if processing.get("background_mode", "chroma_key") not in BACKGROUND_MODES:
        raise ValueError("Scale profile has an invalid background_mode.")
    resampling_filter(str(processing.get("resampler", "lanczos")))
    validate_despill_radius(processing.get("despill_radius", 0))
    reference = payload.get("reference")
    if not isinstance(reference, dict) or float(reference.get("body_scale_mean", 0.0)) <= 0:
        raise ValueError("Scale profile is missing a valid reference body-scale mean.")
    if version == 2 and float(reference.get("output_subject_height_px", 0.0)) <= 0:
        raise ValueError("A version 2 scale profile needs reference.output_subject_height_px.")
    if "output_origin" not in payload:
        payload["output_origin"] = processing_output_origin(processing)
    return payload


def apply_scale_profile(args: argparse.Namespace, profile: dict[str, Any]) -> None:
    """Copy the profile's processing contract onto ``args`` (keys it does not hold are left alone)."""
    for key, value in dict(profile["processing"]).items():
        if key in PROFILE_KEYS:
            setattr(args, key, value)


def profile_scale_drift(
    qc_summary: dict[str, Any], profile: dict[str, Any]
) -> float:
    """Relative scale drift from a profile: output subject height for version 2 (S02), the
    legacy cell-area metric for version 1 profiles."""
    reference = dict(profile["reference"])
    if int(profile.get("version", 1)) >= 2:
        current = float(qc_summary.get("scale_reference_height_px", 0.0))
        expected = float(reference.get("output_subject_height_px", 0.0))
    else:
        current = float(qc_summary.get("legacy_body_scale_mean", qc_summary.get("body_scale_mean", 0.0)))
        expected = float(reference.get("body_scale_mean", 0.0))
    if current <= 0 or expected <= 0:
        return 0.0
    return abs(current / expected - 1.0)


# --------------------------------------------------------------------------- output helpers

def compose_sheet(frames: list[Image.Image], rows: int, cols: int, cell_size: int) -> Image.Image:
    canvas = Image.new("RGBA", (cols * cell_size, rows * cell_size), (0, 0, 0, 0))
    for index, frame in enumerate(frames):
        row, col = divmod(index, cols)
        canvas.paste(frame, (col * cell_size, row * cell_size))
    return canvas


def save_transparent_gif(frames: list[Image.Image], out_path: Path, duration: int) -> None:
    """Encode a shared palette; GIF alpha is binary (source alpha >= 128 is opaque).

    Reserve a transparent index only when required, and assign it from the alpha
    mask rather than matching an RGB sentinel that could also be foreground art.
    """
    if not frames:
        raise ValueError("No frames to encode.")
    if duration <= 0:
        raise ValueError("Animation duration must be positive.")
    width, height = frames[0].size
    if any(frame.size != (width, height) for frame in frames):
        raise ValueError("GIF frames must have identical dimensions.")
    stacked = Image.new("RGB", (width, height * len(frames)))
    opaque_masks = []
    for index, frame in enumerate(frames):
        r, g, b, a = frame.convert("RGBA").split()
        hard_mask = a.point(lambda value: 255 if value >= 128 else 0)
        opaque_masks.append(np.asarray(hard_mask) > 0)
        rgb = Image.merge("RGB", (r, g, b))
        stacked.paste(rgb, (0, index * height), hard_mask)
    opaque = np.concatenate(opaque_masks, axis=0)
    has_transparency = not bool(opaque.all())
    paletted = stacked.quantize(
        colors=255 if has_transparency else 256,
        method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE,
    )
    save_options = {}
    if has_transparency:
        palette = (paletted.getpalette() or [])[:255 * 3]
        palette.extend([0] * (255 * 3 - len(palette)))
        indices = (np.asarray(paletted).astype(np.uint16) + 1).astype(np.uint8)
        indices[~opaque] = 0
        paletted = Image.frombytes("P", paletted.size, indices.tobytes())
        paletted.putpalette([0, 0, 0] + palette)
        save_options = {"transparency": 0, "background": 0}

    out_frames = [
        paletted.crop((0, index * height, width, (index + 1) * height))
        for index in range(len(frames))
    ]
    if has_transparency:
        # Pillow (12.3) writes a second GIF header, a corrupt file, for a later frame equal to the
        # disposal-2 background when every frame shares one palette. An invisible change to colour 0
        # (the transparent index) keeps a fully transparent frame an ordinary frame.
        for frame, mask in zip(out_frames[1:], opaque_masks[1:]):
            if not mask.any():
                frame.putpalette([1, 1, 1] + palette)
    out_frames[0].save(
        out_path,
        format="GIF",
        save_all=True,
        append_images=out_frames[1:],
        duration=duration,
        loop=0,
        disposal=2,
        optimize=False,
        **save_options,
    )


def gif_decoded_durations(path: Path) -> list[int]:
    """Per-frame durations a decoder reads back; GIF stores 10 ms units and merges identical frames."""
    with Image.open(path) as gif:
        durations = []
        for index in range(getattr(gif, "n_frames", 1)):
            gif.seek(index)
            durations.append(int(gif.info.get("duration") or 0))
    return durations


def sanitize_slug(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return slug or "sprite"


# --------------------------------------------------------------------------- list-options and build-prompt

def cmd_list_options() -> None:
    print(
        json.dumps(
            {
                "targets": TARGET_MODES,
                "npc_roles": NPC_ROLES,
                "grid_shapes": GRID_SHAPES,
                "frame_labels": FRAME_LABELS,
                "locomotion_planning": {
                    "recommended_poses_per_action_or_direction": list(RECOMMENDED_LOCOMOTION_POSES),
                    "legacy_grid_defaults_preserved": True,
                    "low_pose_count_override": "--intentional-low-frame-count",
                    "note": "A planning range, not a quality gate; inspect actual poses and timing at game size.",
                },
                "processor": {
                    "component_mode": list(COMPONENT_MODES),
                    "align": ["center", "feet"],
                    "align_deprecated_aliases": {"bottom": "feet"},
                    "anchor_mode": list(ANCHOR_MODES),
                    "scale_strategy": list(SCALE_STRATEGIES),
                    "background_mode": list(BACKGROUND_MODES),
                    "key_quality": list(KEY_QUALITIES),
                    "key": list(KEY_NAMES),
                    "resampler": list(RESAMPLERS),
                    "despill_radius": [0, 1, 2, 3],
                    "alpha_hygiene": list(forge_core.HYGIENE_MODES),
                    "connectivity": [8, 4],
                    "grid_rounding": list(GRID_ROUNDINGS),
                    "direction_names": list(DIRECTION_NAMES),
                    "defaults": {
                        "alpha_geometry_threshold": forge_core.ALPHA_GEOMETRY_THRESHOLD,
                        "alpha_hygiene": "both for native_alpha, none otherwise",
                        "anchor_mode": "feet for --align feet, center otherwise",
                        "key_quality": "auto: hard for nearest + magenta, soft otherwise",
                    },
                    "strict_qc": {
                        "structural_checks": ["empty", "edge_touch", "paste_clamped"],
                        "optional_metrics": [
                            "max_body_scale_cv",
                            "max_anchor_y_std",
                            "max_profile_scale_drift",
                        ],
                    },
                    "pipeline_meta_schema": PIPELINE_META_SCHEMA,
                    "scale_profile_version": SCALE_PROFILE_VERSION,
                    "scale_profile_versions_read": [1, 2],
                },
            },
            indent=2,
        )
    )


def cmd_build_prompt(args: argparse.Namespace) -> None:
    legacy_style = bool(getattr(args, "legacy_style", False))
    prompt_text, seed = build_prompt(args.target, args.mode, args.prompt, args.role, args.seed, legacy_style)
    intentional = getattr(args, "intentional_low_frame_count", False)
    warning = locomotion_planning_warning(args.mode, *GRID_SHAPES.get(args.mode, (1, 1)), intentional)
    if warning:
        print(warning, file=sys.stderr)
    payload = {
        "target": args.target,
        "mode": args.mode,
        "prompt": args.prompt,
        "role": args.role or "",
        "seed": seed,
        "style": "legacy" if legacy_style else "default",
        "generated_prompt": prompt_text,
        "intentional_low_frame_count": intentional,
        "planning_warnings": [warning] if warning else [],
    }
    if args.write:
        args.write.parent.mkdir(parents=True, exist_ok=True)
        args.write.write_text(prompt_text, encoding="utf-8")
    if args.write_json:
        args.write_json.parent.mkdir(parents=True, exist_ok=True)
        args.write_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(prompt_text)


# --------------------------------------------------------------------------- process: planning

@dataclass
class ProcessPlan:
    """Everything ``process`` validated before it reads pixels."""

    destination: Path
    single: bool
    rows: int
    cols: int
    options: GridOptions
    labels: list[str] | None
    directions: list[str] | None
    planning_warning: str | None
    profile: dict[str, Any] | None
    profile_report: dict[str, Any] | None
    prompt_text: str | None
    prompt_source: str | None
    sidecars: dict[str, Path]
    warnings: list[str] = field(default_factory=list)


def _same_setting(key: str, first: Any, second: Any) -> bool:
    if key == "align":
        first, second = ("feet" if v == "bottom" else v for v in (first, second))
    if isinstance(first, float) or isinstance(second, float):
        return first is not None and second is not None and math.isclose(float(first), float(second), abs_tol=1e-9)
    return first == second


def _flag(key: str) -> str:
    return "--" + key.replace("_", "-")


def _resolve_profile(args: argparse.Namespace) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Apply --scale-profile without silently overriding explicit flags (S09).

    A flag that differs from the profile is an error unless --profile-override,
    which lets the flag win. Applied values go to stderr and the metadata.
    """
    if args.scale_profile is None:
        return None, None
    if args.write_scale_profile:
        raise ValueError("Use either --scale-profile or --write-scale-profile, not both.")
    profile = load_scale_profile(args.scale_profile)
    name = str(profile.get("name", ""))
    explicit = {key: getattr(args, key) for key in PROFILE_KEYS if getattr(args, key, None) is not None}
    applied: dict[str, Any] = {}
    overrides: list[dict[str, Any]] = []
    conflicts: list[str] = []
    for key, value in dict(profile["processing"]).items():
        if key not in PROFILE_KEYS or value is None:
            continue
        if key in explicit and not _same_setting(key, explicit[key], value):
            if args.profile_override:
                overrides.append({"key": key, "profile": value, "flag": explicit[key]})
            else:
                conflicts.append(f"{_flag(key)} {explicit[key]} (profile: {value})")
            continue
        setattr(args, key, value)
        applied[key] = value
    if conflicts:
        raise ValueError(
            f"--scale-profile '{name}' conflicts with explicit flags: {'; '.join(conflicts)}. Drop those flags to "
            "use the profile's contract, or pass --profile-override to let the flags win."
        )
    print(forge_core.ascii_text(
        f"scale profile '{name}' (v{profile['version']}): applied "
        + ", ".join(f"{key}={value}" for key, value in applied.items())
        + ("; flags kept: " + ", ".join(f"{item['key']}={item['flag']}" for item in overrides) if overrides else "")
    ), file=sys.stderr)
    report = {
        "name": name,
        "version": profile["version"],
        "path": None,  # filled once the output directory is known
        "applied": applied,
        "overrides": overrides,
    }
    return profile, report


def _validate_sidecars(args: argparse.Namespace, destination: Path) -> dict[str, Path]:
    requested: dict[str, Path] = {}
    for option in ("write_godot_sprite3d_meta", "write_scale_profile"):
        path = getattr(args, option)
        if path is None:
            continue
        final_path = Path(path).resolve()
        if final_path == destination or final_path.exists() or final_path.is_symlink():
            raise FileExistsError(f"Refusing to overwrite output path: {final_path}")
        if destination.is_relative_to(final_path):
            raise ValueError("A sidecar file cannot be an ancestor of the output directory.")
        if final_path in requested.values():
            raise ValueError("Godot metadata and scale profile must use different output paths.")
        if any(final_path.is_relative_to(other) or other.is_relative_to(final_path) for other in requested.values()):
            raise ValueError("Sidecar output files cannot be ancestors of each other.")
        requested[option] = final_path
    contract = requested.get("write_godot_sprite3d_meta")
    if contract is not None:
        _relative_posix(destination, contract.parent, "--write-godot-sprite3d-meta")
    return requested


def _direction_order(args: argparse.Namespace, rows: int) -> list[str] | None:
    if args.direction_order is None:
        return list(DEFAULT_DIRECTION_ORDER) if args.mode == "player_sheet" and (args.rows, args.cols) == (None, None) \
            else None
    names = [name.strip().lower() for name in args.direction_order.split(",") if name.strip()]
    unknown = [name for name in names if name not in DIRECTION_NAMES]
    if unknown:
        raise ValueError(f"Unknown direction(s) {', '.join(unknown)}; use {', '.join(DIRECTION_NAMES)}.")
    if len(set(names)) != len(names):
        raise ValueError("--direction-order names must be distinct.")
    if len(names) != rows:
        raise ValueError(f"--direction-order lists {len(names)} direction(s) for a sheet of {rows} rows; name one "
                         "direction per row, top to bottom.")
    return names


def plan_process(args: argparse.Namespace) -> ProcessPlan:
    """Validate every flag before any output exists (S08, S09, S17, S18, S22, S27)."""
    ensure_valid_target_mode(args.target, args.mode)
    destination = Path(args.output_dir).resolve()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"Refusing to overwrite existing output directory: {destination}")
    has_custom_grid = args.rows is not None or args.cols is not None
    if has_custom_grid and (args.rows is None or args.cols is None):
        raise ValueError("Custom grids require both --rows and --cols.")
    if has_custom_grid:
        rows, cols, single = args.rows, args.cols, False
    elif args.mode in GRID_SHAPES:
        (rows, cols), single = GRID_SHAPES[args.mode], False
    elif args.mode in SINGLE_IMAGE_MODES:
        rows, cols, single = 1, 1, True
    else:
        raise ValueError(f"Mode '{args.mode}' has no predefined grid; pass --rows and --cols for the measured layout.")
    if args.label_prefix is not None and not has_custom_grid:
        raise ValueError("--label-prefix names custom-grid frames; pass --rows and --cols with it.")
    if has_custom_grid:
        prefix = args.label_prefix or args.mode
        if not prefix or re.search(r'[<>:"/\\|?*\x00-\x1f]', prefix) or prefix.rstrip(". ") != prefix:
            raise ValueError("Frame label prefix must be a filename, not a path or reserved filename characters.")
    if single and args.direction_order is not None:
        raise ValueError("--direction-order names the rows of a sheet; single-image modes have none.")
    directions = None if single else _direction_order(args, rows)

    profile, profile_report = _resolve_profile(args)
    warnings: list[str] = []
    if args.align == "bottom":
        warnings.append("--align bottom is a deprecated alias of --align feet (S18).")
        args.align = "feet"
    defaults = {
        "background_mode": "chroma_key", "key_quality": "auto", "key": "magenta", "resampler": "lanczos",
        "despill_radius": 0, "alpha_geometry_threshold": forge_core.ALPHA_GEOMETRY_THRESHOLD, "connectivity": 8,
        "alpha_floor": 4, "align": "center", "shared_scale": False, "scale_strategy": "fit",
        "component_mode": "all", "component_padding": 0, "min_component_area": 1, "edge_touch_margin": 0,
        "logical_pixel": 1,
        "fit_scale": 0.9 if single else 0.85,
        "trim_border": 0 if single else 4,
        "edge_clean_depth": 0 if single else 3,
    }
    for key, value in defaults.items():
        if getattr(args, key, None) is None:
            setattr(args, key, value)
    if single:
        cell_size = args.single_size
    else:
        cell_size = args.cell_size or (96 if (rows, cols) == (4, 4) else 128)
    options = GridOptions(
        rows, cols, cell_size, args.threshold, args.edge_threshold,
        fit_scale=args.fit_scale, trim_border_px=args.trim_border, edge_clean_depth=args.edge_clean_depth,
        align=args.align, shared_scale=bool(args.shared_scale), component_mode=args.component_mode,
        component_padding=args.component_padding, min_component_area=args.min_component_area,
        edge_touch_margin=args.edge_touch_margin, scale_strategy=args.scale_strategy,
        background_mode=args.background_mode, resampler=args.resampler, despill_radius=args.despill_radius,
        key_quality=args.key_quality, key=args.key, alpha_geometry_threshold=args.alpha_geometry_threshold,
        connectivity=args.connectivity, anchor_mode=args.anchor_mode, anchor_px=args.anchor_px,
        alpha_hygiene=args.alpha_hygiene, alpha_floor=args.alpha_floor, pixel_scale=args.pixel_scale,
        logical_pixel=args.logical_pixel, legacy_fractional_nearest=args.legacy_fractional_nearest,
        grid_rounding=args.grid_rounding, pad_to_grid=args.pad_to_grid,
    )
    if args.write_godot_sprite3d_meta and args.godot_world_height is None:
        raise ValueError("--write-godot-sprite3d-meta requires --godot-world-height.")
    if single and args.godot_world_height is not None:
        raise ValueError("Godot Sprite3D metadata currently requires processed grid frames.")
    sidecars = _validate_sidecars(args, destination)
    prompt_text, prompt_source = None, None
    if args.prompt_file is not None:
        if not Path(args.prompt_file).is_file():
            raise ValueError(f"--prompt-file not found: {args.prompt_file}")
        # UTF-8 with or without a BOM (PowerShell 5.1 writes one, D28); the BOM is not prompt text.
        prompt_text, prompt_source = Path(args.prompt_file).read_text(encoding="utf-8-sig"), "file"
    elif args.prompt:
        prompt_text, prompt_source = args.prompt, "argument"

    labels = None
    if not single:
        if has_custom_grid:
            prefix = args.label_prefix or args.mode
            labels = ([f"{prefix}-{direction}-{col + 1}" for direction in directions for col in range(cols)]
                      if directions else [f"{prefix}-{index + 1}" for index in range(rows * cols)])
        elif directions and args.mode == "player_sheet":
            labels = [f"{direction}-{col + 1}" for direction in directions for col in range(cols)]
        elif directions:
            labels = [f"{args.mode}-{direction}-{col + 1}" for direction in directions for col in range(cols)]
        else:
            labels = FRAME_LABELS[args.mode]
    intentional = getattr(args, "intentional_low_frame_count", False)
    planning_warning = None if single else locomotion_planning_warning(args.mode, rows, cols, intentional)
    if not single and args.duration % 10:
        warnings.append(f"GIF stores frame times in 10 ms units, so --duration {args.duration} plays as "
                        f"{args.duration // 10 * 10} ms in the GIF preview; PNG frames and metadata keep "
                        f"{args.duration} ms.")
    return ProcessPlan(destination, single, rows, cols, options, labels, directions, planning_warning, profile,
                       profile_report, prompt_text, prompt_source, sidecars, warnings)


# --------------------------------------------------------------------------- process: metadata and QC

def _qc_errors_and_checks(metadata: dict[str, Any], args: argparse.Namespace,
                          profile: dict[str, Any] | None) -> tuple[list[str], list[dict[str, Any]]]:
    """Strict-QC errors (empty, clamped, edge contact, optional metrics, profile drift) and the
    qaEnvelope check list."""
    errors: list[str] = []
    checks: list[dict[str, Any]] = []
    strict = bool(args.strict_qc)
    summary = metadata["qc_summary"]

    def check(check_id: str, failed: bool, value: Any, threshold: Any, fatal: bool, message: str) -> None:
        status = "pass"
        if failed:
            status = "fail" if fatal else "warn"
            if fatal:
                errors.append(message)
        checks.append({"id": check_id, "status": status, "value": value, "threshold": threshold})

    edge = metadata["edge_touch_frames"]
    if args.reject_edge_touch:
        check("edge_touch_frames", bool(edge), edge, [], True, f"frames touch a cell edge: {edge}")
    check("empty_frames", bool(metadata["empty_frames"]), metadata["empty_frames"], [], strict,
          f"empty frames: {metadata['empty_frames']}")
    check("paste_clamped_frames", bool(metadata["paste_clamped_frames"]), metadata["paste_clamped_frames"], [], strict,
          f"clamped frames: {metadata['paste_clamped_frames']}")
    output_edge = metadata["output_edge_touch_frames"]
    check("output_edge_touch_frames", bool(output_edge), output_edge, [], strict and not args.reject_edge_touch,
          f"processed frames touch an output edge: {output_edge}")
    source_edge = metadata["source_edge_touch_frames"]
    source_fatal = strict and not args.allow_source_edge_touch and not args.reject_edge_touch
    check("source_edge_touch_frames", bool(source_edge), source_edge, [], source_fatal,
          f"raw subjects touch a source-cell edge: {source_edge}")
    for check_id, key, limit in (("body_scale_cv", "body_scale_cv", args.max_body_scale_cv),
                                 ("anchor_y_std", "anchor_y_std", args.max_anchor_y_std)):
        value = float(summary.get(key, 0.0))
        if limit is None:
            checks.append({"id": check_id, "status": "skipped", "value": value, "threshold": None})
        else:
            label = "body scale CV" if key == "body_scale_cv" else "anchor Y std"
            check(check_id, value > limit, value, limit, strict, f"{label} {value:.4f} exceeds {limit:.4f}")
    if profile is not None:
        limit = metadata["qc_config"]["max_profile_scale_drift"]
        drift = float(summary.get("profile_body_scale_drift", 0.0))
        check("profile_scale_drift", drift > limit, drift, limit, strict,
              f"profile body-scale drift {drift:.4f} exceeds {limit:.4f}")
    off_grid = summary.get("pixel_grid_off_edges")
    if off_grid is not None:
        check("pixel_grid", off_grid > 0, off_grid, 0, False,
              f"{off_grid} colour edges fall off the integer pixel grid")
    matte = metadata.get("matte")
    if matte:
        residue = matte.get("qa_after_despill") or matte["qa"]  # what is published
        opaque_key = int(residue["opaque_key_px"])
        check("key_residue", opaque_key > 0, opaque_key, 0, False, f"{opaque_key} opaque key-coloured pixels remain")
        # A binary key (hard, or auto for nearest) leaves a key-coloured fringe on anti-aliased edges: warn
        # like the video residue gate does (report v2 P0-3); --despill-radius or --key-quality soft fix it.
        ring = round(float(residue["outer_ring_spill_fraction"]), 6)
        check("key_ring_spill", ring > KEY_RING_SPILL_MAX, ring, KEY_RING_SPILL_MAX, False,
              f"{ring:.3f} of the outer edge ring is key-coloured")
    return errors, checks


def _base_metadata(args: argparse.Namespace, plan: ProcessPlan, source_info: dict[str, Any],
                   raw_name: str) -> dict[str, Any]:
    options = plan.options
    metadata: dict[str, Any] = {
        "schema": PIPELINE_META_SCHEMA,
        "target": args.target,
        "mode": args.mode,
        "prompt": args.prompt or "",
        "role": args.role or "",
        "input": Path(args.input).name,
        "threshold": args.threshold,
        "edge_threshold": args.edge_threshold,
        "duration": args.duration,
        "background_mode": options.background_mode,
        "resampler": options.resampler,
        "key_quality": options.key_quality,
        "key": options.key,
        "despill_radius": options.despill_radius if options.chroma else 0,
        "alpha_geometry_threshold": options.alpha_geometry_threshold,
        "connectivity": options.connectivity,
        "anchor_mode": options.resolved_anchor_mode,
        "alpha_hygiene": options.hygiene_mode,
        "alpha_floor": options.alpha_floor,
        "pixel_scale": options.pixel_scale,
        "logical_pixel": options.logical_pixel,
        "rows": plan.rows,
        "cols": plan.cols,
        "cell_size": options.cell_size,
        "fit_scale": options.fit_scale,
        "trim_border": options.trim_border_px if options.chroma else 0,
        "edge_clean_depth": options.edge_clean_depth if options.chroma else 0,
        "align": options.align,
        "shared_scale": options.shared_scale,
        "scale_strategy": options.scale_strategy,
        "component_mode": options.component_mode,
        "component_padding": options.component_padding,
        "min_component_area": options.min_component_area,
        "edge_touch_margin": options.edge_touch_margin,
        "provenance": {
            "input_sha256": source_info["sha256"],
            "source_mode": source_info["source_mode"],
            "bit_depth": source_info["bit_depth"],
            "conversion": source_info["conversion"],
            "raw_copy": raw_name,
            "bytes": source_info["bytes"],
            "format": source_info["format"],
            "size": source_info["size"],
        },
    }
    if plan.single:
        metadata["single_size"] = options.cell_size
    else:
        metadata["intentional_low_frame_count"] = bool(getattr(args, "intentional_low_frame_count", False))
        metadata["planning_warnings"] = [plan.planning_warning] if plan.planning_warning else []
    return metadata


def _sheet_metadata(metadata: dict[str, Any], plan: ProcessPlan, result: SheetResult) -> None:
    options = plan.options
    frame_qc = result.info
    report = result.report
    metadata["geometry"] = {
        "alpha_geometry_threshold": options.alpha_geometry_threshold,
        "connectivity": options.connectivity,
        "anchor_mode": options.resolved_anchor_mode,
        "scale_strategy": options.scale_strategy,
        "pixel_scale": options.pixel_scale,
        "logical_pixel": options.logical_pixel,
        "grid_rounding": options.grid_rounding,
        "pad_offset": report["pad_offset"],
        "anchor_px": list(options.anchor_px) if options.anchor_px is not None else None,
        "anchor_band_fraction": ANCHOR_BAND_FRACTION,
        "scale": report.get("scale"),
        "scale_ratio": report.get("scale_ratio"),
        "registration_anchor": report.get("registration_anchor"),
        "injected_shift_px": report.get("injected_shift_px"),
        "legacy_fractional_nearest": options.legacy_fractional_nearest,
    }
    metadata["hygiene"] = report["hygiene"]
    if report["matte"] is not None:
        metadata["matte"] = report["matte"]
    if plan.labels is not None:
        metadata["frame_labels"] = plan.labels
    if plan.directions:
        metadata["directions"] = plan.directions
    metadata["frames"] = frame_qc
    for key, field_name in (("edge_touch_frames", "edge_touch"), ("source_edge_touch_frames", "source_edge_touch"),
                            ("output_edge_touch_frames", "output_edge_touch"), ("empty_frames", "is_empty"),
                            ("paste_clamped_frames", "paste_clamped")):
        metadata[key] = [info["grid"] for info in frame_qc if bool(info.get(field_name))]
    metadata["qc_summary"] = summarize_frame_qc(frame_qc)
    origins = [info["anchor_target"] for info in frame_qc if info.get("anchor_target")]
    if origins:
        metadata["output_origin"] = origins[0]


def _qa_envelope(status_checks: list[dict[str, Any]], inputs: list[dict[str, Any]],
                 outputs: list[dict[str, Any]]) -> dict[str, Any]:
    failed = any(item["status"] in ("fail", "warn") for item in status_checks)
    return {
        "status": "warn" if failed else "pass",
        "method": ("generate2dsprite process geometry v2: alpha above the geometry threshold, 8- or 4-connected "
                   "components, ground-line anchors, one shared sampling grid with whole-pixel offsets, output-space "
                   "scale metrics, forge_matte.matte_qa key residue"),
        "notProven": [
            "pose quality, anatomy, identity and facing",
            "loop seam, phase order and timing at game size",
            "colour fidelity of keyed edges beyond the matte_qa residue counts",
            "that the subject is complete where it does not touch a cell edge",
        ],
        "checks": status_checks,
        "inputs": inputs,
        "outputs": outputs,
        "tool": {"name": f"{TOOL_NAME} process", "version": TOOL_VERSION},
    }


# --------------------------------------------------------------------------- process: command

def _write_outputs(stage: Path, plan: ProcessPlan, args: argparse.Namespace, result: SheetResult,
                   raw_name: str, metadata: dict[str, Any]) -> list[Path]:
    """Write the raw copy, cleaned sheet, frames, sheet, GIF previews and prompt; return frame-like outputs."""
    shutil.copyfile(args.input, stage / raw_name)
    if forge_core.sha256_file(stage / raw_name) != metadata["provenance"]["input_sha256"]:
        raise ValueError(f"The input changed while it was processed: {args.input}")
    written: list[Path] = []
    if plan.single:
        forge_core.save_png(result.frames[0], stage / "clean.png")
        written.append(stage / "clean.png")
    else:
        forge_core.save_png(result.cleaned, stage / "raw-sheet-clean.png")
        for label, frame in zip(plan.labels, result.frames):
            forge_core.save_png(frame, stage / f"{label}.png")
            written.append(stage / f"{label}.png")
        cell = plan.options.cell_size
        forge_core.save_png(compose_sheet(result.frames, plan.rows, plan.cols, cell), stage / "sheet-transparent.png")
        written.append(stage / "sheet-transparent.png")
        gifs: dict[str, list[int]] = {}
        if plan.directions:
            for row_index, direction in enumerate(plan.directions):
                row_frames = result.frames[row_index * plan.cols:(row_index + 1) * plan.cols]
                strip = stage / f"{direction}-strip.png"
                forge_core.save_png(compose_sheet(row_frames, 1, plan.cols, cell), strip)
                written.append(strip)
                gif = stage / f"{direction}.gif"
                save_transparent_gif(row_frames, gif, args.duration)
                gifs[gif.name] = gif_decoded_durations(gif)
        else:
            gif = stage / "animation.gif"
            save_transparent_gif(result.frames, gif, args.duration)
            gifs[gif.name] = gif_decoded_durations(gif)
        metadata["gif_decoded_duration_ms"] = gifs
    if plan.prompt_text is not None:
        (stage / "prompt-used.txt").write_text(plan.prompt_text, encoding="utf-8")
        metadata["prompt_source"] = plan.prompt_source
        metadata["prompt_sha256"] = forge_core.sha256_bytes(plan.prompt_text.encode("utf-8"))
        if plan.prompt_source == "file":
            metadata["prompt_file"] = Path(args.prompt_file).name
    return written


def cmd_process(args: argparse.Namespace) -> dict[str, Any]:
    """Validate everything, process in memory, run QC, then publish a new output directory.

    A strict-QC failure raises before anything is written. Outputs are staged
    beside the destination and published with forge_core's no-replace rename;
    sidecars outside it are published file by file first and rolled back if
    the directory publish fails (several paths cannot form one atomic step).
    """
    plan = plan_process(args)
    for warning in plan.warnings:
        print(forge_core.ascii_text(f"warning: {warning}"), file=sys.stderr)
    if plan.planning_warning:
        print(plan.planning_warning, file=sys.stderr)
    source, source_info = forge_core.load_rgba(args.input)
    result = process_sheet(source, plan.options)
    destination = plan.destination
    raw_name = ("raw" if plan.single else "raw-sheet") + (Path(args.input).suffix.lower() or ".png")
    metadata = _base_metadata(args, plan, source_info, raw_name)
    _sheet_metadata(metadata, plan, result)
    if plan.warnings:
        metadata["warnings"] = plan.warnings

    scale_profile = plan.profile
    effective_profile_limit = args.max_profile_scale_drift
    if scale_profile is not None:
        drift = profile_scale_drift(metadata["qc_summary"], scale_profile)
        metadata["qc_summary"]["profile_body_scale_drift"] = drift
        if effective_profile_limit is None:
            effective_profile_limit = float(dict(scale_profile.get("qc") or {}).get("max_body_scale_drift", 0.10))
        profile_ref = forge_core.file_ref(Path(args.scale_profile), destination)
        plan.profile_report["path"] = profile_ref
        metadata["profile_applied"] = plan.profile_report
        metadata["scale_profile"] = {
            "path": profile_ref,
            "version": scale_profile["version"],
            "name": scale_profile.get("name", ""),
            "reference_mode": dict(scale_profile["reference"]).get("mode"),
            "processing_contract_applied": True,
        }
    metadata["qc_config"] = {
        "strict_qc": args.strict_qc,
        "reject_edge_touch": args.reject_edge_touch,
        "allow_source_edge_touch": args.allow_source_edge_touch,
        "max_body_scale_cv": args.max_body_scale_cv,
        "max_anchor_y_std": args.max_anchor_y_std,
        "max_profile_scale_drift": effective_profile_limit,
    }
    qc_errors, checks = _qc_errors_and_checks(metadata, args, scale_profile)
    if qc_errors:
        raise ValueError("QC failed: " + "; ".join(qc_errors))

    godot_payload = None
    godot_final = plan.sidecars.get("write_godot_sprite3d_meta") or (
        destination / "godot-sprite3d.json" if args.godot_world_height is not None else None)
    if args.godot_world_height is not None:
        godot_profile = dict(scale_profile.get("godot_sprite3d") or {}) if scale_profile else {}
        profile_world_height = float(godot_profile.get("world_height", 0.0))
        if profile_world_height > 0 and not math.isclose(
            profile_world_height, args.godot_world_height, rel_tol=1e-6, abs_tol=1e-6
        ):
            raise ValueError(
                "--godot-world-height must match the scale profile's reference world height "
                f"({profile_world_height})."
            )
        locked_pixel_size = godot_profile.get("pixel_size")
        frame_dir = _relative_posix(destination, godot_final.parent, "--write-godot-sprite3d-meta")
        godot_payload = build_godot_sprite3d_metadata(
            metadata, args.godot_world_height,
            float(locked_pixel_size) if locked_pixel_size is not None else None,
            "" if frame_dir == "." else frame_dir,
        )
        metadata["godot_sprite3d"] = godot_payload
    profile_payload = None
    profile_final = plan.sidecars.get("write_scale_profile")
    if profile_final is not None:
        profile_limit = args.max_profile_scale_drift if args.max_profile_scale_drift is not None else 0.10
        profile_payload = build_scale_profile(
            metadata, args.profile_name or sanitize_slug(f"{args.target}-{args.mode}"), profile_limit)

    published: list[tuple[Path, int, int]] = []
    try:
        with (forge_core.staged_output(destination) as stage,
              tempfile.TemporaryDirectory(prefix=f".{destination.name}.sidecars-", dir=destination.parent) as side):
            written = _write_outputs(stage, plan, args, result, raw_name, metadata)
            external: list[tuple[Path, Path]] = []
            for key, payload, final in (("godot_sprite3d_output", godot_payload, godot_final),
                                        ("scale_profile_output", profile_payload, profile_final)):
                if payload is None:
                    continue
                staged = (stage / final.relative_to(destination) if final.is_relative_to(destination)
                          else Path(side) / final.name)
                staged.parent.mkdir(parents=True, exist_ok=True)
                forge_core.write_json(staged, payload, no_clobber=True)
                metadata[key] = forge_core.file_ref(final, destination, sha256=forge_core.sha256_file(staged),
                                                     size=staged.stat().st_size)
                if not final.is_relative_to(destination):
                    external.append((staged, final))
            inputs = [{"path": raw_name, "sha256": source_info["sha256"], "bytes": source_info["bytes"]}]
            outputs = [forge_core.file_ref(path, stage) for path in written]
            metadata["qa"] = _qa_envelope(checks, inputs, outputs)
            forge_core.write_json(stage / "pipeline-meta.json", metadata, no_clobber=True)
            for staged, final in external:
                forge_core.publish_file_no_replace(staged, final)
                identity = final.stat()
                published.append((final, identity.st_dev, identity.st_ino))
    except BaseException:
        for path, device, inode in reversed(published):
            # Never remove a different file that replaced our sidecar.
            if path.exists():
                current = path.stat()
                if (current.st_dev, current.st_ino) == (device, inode):
                    path.unlink()
        raise
    summary = {
        "output_dir": str(destination),
        "metadata": str(destination / "pipeline-meta.json"),
        "frames": 1 if plan.single else len(plan.labels),
        "qa_status": metadata["qa"]["status"],
    }
    if godot_payload is not None:
        summary["godot_sprite3d"] = str(godot_final)
    if profile_final is not None:
        summary["scale_profile"] = str(profile_final)
    print(json.dumps(summary))
    return summary


# --------------------------------------------------------------------------- argument parsing

def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {text!r}") from None
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {value}")
    return value


def _nonnegative_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected an integer >= 0, got {text!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be an integer >= 0, got {value}")
    return value


def _positive_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a positive number, got {text!r}") from None
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive number, got {text}")
    return value


def _point(text: str) -> tuple[float, float]:
    parts = [part.strip() for part in text.split(",")]
    try:
        x, y = (float(part) for part in parts)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected X,Y in source-cell pixels, got {text!r}") from None
    if not (math.isfinite(x) and math.isfinite(y)):
        raise argparse.ArgumentTypeError(f"expected finite X,Y, got {text!r}")
    return x, y


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list-options", help="Print supported targets, modes, and NPC roles.")

    build_prompt_parser = subparsers.add_parser("build-prompt", help="Build a generation prompt.")
    build_prompt_parser.add_argument("--target", required=True, choices=sorted(TARGET_MODES))
    build_prompt_parser.add_argument("--mode", required=True)
    build_prompt_parser.add_argument("--prompt", required=True)
    build_prompt_parser.add_argument("--role")
    build_prompt_parser.add_argument("--seed", type=int)
    build_prompt_parser.add_argument("--write", type=Path)
    build_prompt_parser.add_argument("--write-json", type=Path)
    build_prompt_parser.add_argument(
        "--intentional-low-frame-count", action="store_true",
        help="Record deliberately sparse locomotion and suppress its advisory pose-count warning; does not change the grid.",
    )
    build_prompt_parser.add_argument(
        "--legacy-style", action="store_true",
        help="Use the pre-0.4 style sentences verbatim (they name commercial franchises); the default names none.",
    )

    bundle_parser = subparsers.add_parser(
        "build-godot-bundle",
        help="Combine per-action Sprite3D metadata into one validated animation bundle.",
    )
    bundle_parser.add_argument(
        "--action",
        action="append",
        required=True,
        help="Action contract in ACTION=PATH form; repeat for each action.",
    )
    bundle_parser.add_argument("--default-action", required=True)
    bundle_parser.add_argument(
        "--one-shot",
        action="append",
        help="Action that returns to the default action after its final frame.",
    )
    bundle_parser.add_argument("--max-world-height-drift", type=float, default=0.02)
    bundle_parser.add_argument("--max-pixel-size-drift", type=float, default=0.02)
    bundle_parser.add_argument("--output", required=True, type=Path,
                               help="New bundle JSON; an existing file is never replaced.")

    process_parser = subparsers.add_parser(
        "process", help="Postprocess a generated sprite image.",
        description="Key or validate a generated sheet, split it into cells and register every frame. Settings "
                    "that a scale profile can lock default to unset; an explicit flag that differs from the "
                    "profile is an error unless --profile-override.")
    process_parser.add_argument("--input", required=True, type=Path)
    process_parser.add_argument("--target", required=True, choices=PROCESS_TARGETS)
    process_parser.add_argument("--mode", required=True,
                                help="A mode valid for --target (see list-options); unknown modes fail.")
    process_parser.add_argument(
        "--output-dir", required=True, type=Path,
        help="New output directory, published only after QC succeeds; existing directories are not overwritten.",
    )
    process_parser.add_argument("--role")
    process_parser.add_argument("--prompt")
    process_parser.add_argument("--prompt-file", type=Path, help="Prompt text to store; it must exist.")
    process_parser.add_argument("--threshold", type=int, default=100,
                                help="Binary key: clear pixels closer than this to #FF00FF (default 100).")
    process_parser.add_argument("--edge-threshold", type=int, default=150,
                                help="Binary key: border-connected clearing distance (default 150).")
    process_parser.add_argument(
        "--background-mode", choices=BACKGROUND_MODES,
        help=(
            "chroma_key (default) keys a flat key backdrop and applies border/edge cleanup; native_alpha needs "
            "real transparent pixels plus visible content and keeps RGBA (an opaque checkerboard is rejected); "
            "opaque needs fully opaque input and only works for single images."
        ),
    )
    process_parser.add_argument(
        "--key-quality", choices=KEY_QUALITIES,
        help=("chroma_key keyer: auto (default) uses the binary magenta key for nearest output and the soft matte "
              "otherwise; hard is the legacy binary keyer (magenta only); soft keeps anti-aliased edges and "
              "un-mixes key colour; dominance is a fast channel-dominance key."),
    )
    process_parser.add_argument("--key", choices=KEY_NAMES, help="Backdrop key colour (default magenta).")
    process_parser.add_argument(
        "--resampler", choices=tuple(RESAMPLERS),
        help="lanczos (default) for smooth art; nearest keeps pixel-art colours and needs an integer scale.",
    )
    process_parser.add_argument(
        "--despill-radius", type=int, choices=(0, 1, 2, 3),
        help=(
            "Opt-in chroma_key-only edge despill: 0 disables it (default); 1-3 source pixels from an actual "
            "alpha-zero boundary. Preserves alpha, reduces the key channels where their excess exceeds 12, and "
            "may desaturate legitimate purple at the boundary; ignored for native_alpha/opaque."
        ),
    )
    process_parser.add_argument(
        "--alpha-hygiene", choices=forge_core.HYGIENE_MODES,
        help=("Remove generator alpha noise before measuring: floor zeroes alpha <= --alpha-floor, detached "
              "drops faint islands away from solid art, both does both. Default both for native_alpha, none "
              "otherwise; the counts are recorded."),
    )
    process_parser.add_argument("--alpha-floor", type=_nonnegative_int,
                                help="Hygiene floor: alpha at or below it becomes 0 (default 4).")
    process_parser.add_argument(
        "--alpha-geometry-threshold", type=_nonnegative_int,
        help=("Geometry counts pixels with alpha above this (default 16; 0 restores the legacy alpha > 0). "
              "Pixels are never changed by it."),
    )
    process_parser.add_argument("--connectivity", type=int, choices=(8, 4),
                                help="Component connectivity (default 8; 4 is the legacy labelling).")
    process_parser.add_argument(
        "--anchor-mode", choices=ANCHOR_MODES,
        help=("Subject point placed on the output origin: feet (ground line, median support column), stance "
              "(ground line, middle of the support span), bbox, center, centroid, or legacy-p98 (the old 98th "
              "percentile row). Default feet with --align feet, center otherwise."),
    )
    process_parser.add_argument("--anchor-px", type=_point,
                                help="Declared anchor X,Y in source-cell pixels for every frame (registered art).")
    process_parser.add_argument("--cell-size", type=_positive_int,
                                help="Output cell size in px (default 96 for 4x4 grids, 128 otherwise).")
    process_parser.add_argument("--rows", type=_positive_int)
    process_parser.add_argument("--cols", type=_positive_int)
    process_parser.add_argument("--grid-rounding", choices=GRID_ROUNDINGS, default="exact",
                                help="exact (default) refuses sizes that do not divide; nearest rounds cell edges "
                                     "so cells differ by at most 1 px.")
    process_parser.add_argument("--pad-to-grid", action="store_true",
                                help="Pad the sheet losslessly with transparency until rows x cols divide it.")
    process_parser.add_argument("--direction-order",
                                help="Comma-separated facing of each row, top to bottom, for per-direction strips "
                                     "and GIFs (player_sheet default: down,left,right,up).")
    process_parser.add_argument("--label-prefix")
    process_parser.add_argument(
        "--intentional-low-frame-count", action="store_true",
        help="Record deliberately sparse locomotion and suppress its advisory pose-count warning; does not change the grid.",
    )
    process_parser.add_argument("--fit-scale", type=float,
                                help="Share of the cell the subject may fill (default 0.85, single images 0.9).")
    process_parser.add_argument("--trim-border", type=_nonnegative_int,
                                help="chroma_key cell border trim in px (default 4, single images 0).")
    process_parser.add_argument("--edge-clean-depth", type=_nonnegative_int,
                                help="chroma_key dark/magenta edge cleanup depth in px (default 3, single images 0).")
    process_parser.add_argument("--align", choices=ALIGN_MODES,
                                help="center (default) or feet; bottom is a deprecated alias of feet.")
    process_parser.add_argument("--shared-scale", action=argparse.BooleanOptionalAction,
                                help="fit: one bbox-derived scale for the whole sheet.")
    process_parser.add_argument(
        "--scale-strategy", choices=SCALE_STRATEGIES,
        help=(
            "fit (default) scales each subject box into the cell; preserve uses one sheet scale and moves each "
            "frame by whole pixels to the shared anchor; registered uses one scale and one offset for every "
            "frame, keeping jumps, bob and recoil drawn in the sheet."
        ),
    )
    process_parser.add_argument("--pixel-scale", type=_positive_int,
                                help="nearest: output pixels per logical pixel (an integer scale).")
    process_parser.add_argument("--logical-pixel", type=_positive_int,
                                help="nearest: source pixels per logical pixel of upscaled pixel art; samples each "
                                     "logical pixel's centre (preserve/registered).")
    process_parser.add_argument("--legacy-fractional-nearest", action="store_true",
                                help="Allow nearest at a fractional scale, the pre-0.4 behaviour (uneven pixels).")
    process_parser.add_argument("--component-mode", choices=COMPONENT_MODES,
                                help="all (default) keeps every component; largest keeps only the main one.")
    process_parser.add_argument("--component-padding", type=_nonnegative_int)
    process_parser.add_argument("--min-component-area", type=_positive_int)
    process_parser.add_argument("--edge-touch-margin", type=_nonnegative_int)
    process_parser.add_argument("--reject-edge-touch", action="store_true")
    process_parser.add_argument(
        "--allow-source-edge-touch",
        action="store_true",
        help=(
            "Under strict QC, allow a visually reviewed raw source-cell edge touch while still "
            "rejecting output-edge contact, clamping, and empty frames."
        ),
    )
    process_parser.add_argument("--strict-qc", action="store_true")
    process_parser.add_argument(
        "--scale-profile",
        type=Path,
        help="Apply one character bundle's locked output scale, anchor, and processor contract (version 1 or 2).",
    )
    process_parser.add_argument(
        "--profile-override", action="store_true",
        help="Let explicit flags win over a conflicting --scale-profile (recorded in the metadata).",
    )
    process_parser.add_argument(
        "--write-scale-profile",
        type=Path,
        help="Write a reusable version 2 scale profile after this sheet passes QC.",
    )
    process_parser.add_argument("--profile-name")
    process_parser.add_argument(
        "--max-body-scale-cv",
        type=float,
        help="Strict-QC maximum coefficient of variation of the output subject height, for example 0.08.",
    )
    process_parser.add_argument(
        "--max-anchor-y-std",
        type=float,
        help="Strict-QC maximum normalized vertical-anchor standard deviation, for example 0.05.",
    )
    process_parser.add_argument(
        "--max-profile-scale-drift",
        type=float,
        help="Strict-QC maximum output-height drift from --scale-profile, for example 0.08 (default 0.10).",
    )
    process_parser.add_argument("--single-size", type=_positive_int, default=256,
                                help="Output size of single-image modes (default 256).")
    process_parser.add_argument("--duration", type=_positive_int, default=200,
                                help="Frame duration in ms for metadata and the GIF preview (default 200).")
    process_parser.add_argument(
        "--godot-world-height",
        type=_positive_float,
        help=(
            "Desired subject height in Godot world units. Writes godot-sprite3d.json "
            "with a QC-derived pixel size, feet origin, and animation timing."
        ),
    )
    process_parser.add_argument(
        "--write-godot-sprite3d-meta",
        type=Path,
        help="Optional output path for Godot Sprite3D metadata; its frame paths are relative to it.",
    )

    return parser


def _run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "list-options":
        cmd_list_options()
    elif args.command == "build-prompt":
        cmd_build_prompt(args)
    elif args.command == "build-godot-bundle":
        cmd_build_godot_bundle(args)
    else:
        cmd_process(args)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry (D26, D27): usage errors exit 2 (argparse); every other failure prints one
    ``error: ...`` line and exits 1, an unexpected one as ``error: internal error (...)``."""
    return forge_core.run_cli(_run, argv)


if __name__ == "__main__":
    raise SystemExit(main())
