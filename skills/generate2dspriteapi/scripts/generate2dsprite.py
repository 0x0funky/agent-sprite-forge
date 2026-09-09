#!/usr/bin/env python3
"""Build sprite prompts and postprocess generated sprite sheets locally."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image


ART_STYLE = (
    "Original digital monster creature. Digimon/Pokemon inspired pixel art, "
    "strong outlines, dynamic, battle-ready. NOT cute, NOT round. "
    "SOLID COLORED BODY. Background is 100% solid flat magenta (#FF00FF), no gradients. "
    "NO text, NO labels, NO words, NO letters anywhere."
)

CHAR_STYLE = (
    "Top-down 2D pixel art for a 16-bit RPG overworld. 3/4 view from slightly "
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
    "4. Each character fills 80%+ of its quadrant, SAME SIZE in every quadrant. "
    "5. Quadrants connected by magenta background only."
)

GRID_RULES_4X4 = (
    "ABSOLUTE RULES: "
    "1. EXACTLY 16 equal-size cells arranged in a 4x4 grid (4 rows of 4 columns, every cell the same width and height). "
    "2. NO borders, NO lines, NO frames between cells. "
    "3. NO text, NO labels, NO numbers, NO arrows. "
    "4. CRITICAL CONSISTENCY: the character in every single cell has the IDENTICAL height and IDENTICAL width "
    "(same bounding box, same pixel scale). Do NOT zoom in or out between cells. "
    "Do NOT crop tighter in some cells. The character's head-to-foot height must be visibly identical in all 16 cells. "
    "5. Character is CENTERED horizontally and vertically within its cell. Fills ~60% of the cell, leaving equal magenta margin on all four sides. "
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


def build_prompt(target: str, mode: str, prompt: str, role: str | None = None, seed: int | None = None) -> tuple[str, int]:
    ensure_valid_target_mode(target, mode)
    role = role or ""
    if seed is None:
        seed = stable_seed(target, mode, prompt, role)
    rng = random.Random(seed)

    if target == "creature":
        if mode == "single":
            result = f"Single pixel art creature sprite, centered, facing right. {prompt}. {ART_STYLE}"
        elif mode == "evolution":
            descs = build_evolution_descs(prompt, rng)
            result = (
                f"A 2x2 pixel art image showing 4 evolution stages of {prompt}. "
                f"Top-left quadrant: {descs['1-base']} "
                f"Top-right quadrant: {descs['2-risen']} "
                f"Bottom-left quadrant: {descs['3-elite']} "
                f"Bottom-right quadrant: {descs['4-mythic']} "
                f"Same color palette in all 4. {ART_STYLE} {GRID_RULES}"
            )
        elif mode == "idle":
            result = (
                f"A 2x2 pixel art idle animation sheet of the same {prompt}. "
                "Top-left quadrant: neutral idle pose, calm but alert. "
                "Top-right quadrant: subtle breath or flame pulse, same facing direction. "
                "Bottom-left quadrant: idle shift in weight or aura, still clearly looping. "
                "Bottom-right quadrant: strongest idle accent before returning to frame 1. "
                f"SAME creature, SAME size, SAME facing direction, SAME palette in all 4 cells. {ART_STYLE} {GRID_RULES}"
            )
        elif mode == "combat":
            result = (
                f"A 2x2 pixel art combat sheet of the same {prompt}. "
                "Top-left quadrant: attack wind-up, gathering force. "
                "Top-right quadrant: attack strike or release, aggressive impact. "
                "Bottom-left quadrant: hurt reaction at the moment of impact. "
                "Bottom-right quadrant: hurt recovery, regaining stance. "
                f"SAME creature, SAME size, SAME facing direction, SAME palette in all 4 cells. {ART_STYLE} {GRID_RULES}"
            )
        elif mode == "actions":
            result = (
                f"A 2x2 pixel art sprite sheet of the same {prompt} in 4 poses. "
                "Top-left quadrant: standing still, relaxed. "
                "Top-right quadrant: same pose, mouth open, one limb lifted. "
                "Bottom-left quadrant: lunging right, attacking fiercely. "
                "Bottom-right quadrant: leaning back, eyes closed, taking damage. "
                f"SAME character, SAME size, facing RIGHT. {ART_STYLE} {GRID_RULES}"
            )
        else:
            result = (
                f"A 2x2 pixel art sprite sheet of a walk cycle of the same {prompt}. "
                "Top-left quadrant: walking right, right front leg forward. "
                "Top-right quadrant: walking right, legs under body, mid-stride. "
                "Bottom-left quadrant: walking right, left front leg forward. "
                "Bottom-right quadrant: walking right, legs extended, passing pose. "
                f"SAME character, SAME size, facing RIGHT. Only leg positions change. {ART_STYLE} {GRID_RULES}"
            )
    elif target == "player":
        if mode == "player":
            result = (
                "Single hero sprite for a top-down RPG. "
                f"CHARACTER: {prompt}. Young adventurer protagonist, distinct heroic silhouette, strongly themed costume. "
                "Front-facing (toward camera), idle standing pose, centered in canvas with lots of magenta margin around. "
                f"{CHAR_STYLE}"
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
                f"ONLY the legs and arms swing, head, torso, gear stay identical. {CHAR_STYLE} {GRID_RULES}"
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
                f"{CHAR_STYLE} {GRID_RULES_4X4}"
            )
        else:
            result = (
                "A 2x2 pixel art sprite sheet of a top-down RPG hero in 4 action states, all facing DOWN (toward camera). "
                f"CHARACTER: {prompt}. "
                "Top-left: IDLE, neutral standing, relaxed. "
                "Top-right: WALK, mid-step, one leg forward. "
                "Bottom-left: ATTACK, arm raised or weapon or fist swung forward aggressively. "
                "Bottom-right: HURT, knocked back slightly, expression of pain. "
                f"SAME character identity, SAME costume, SAME size in every cell. {CHAR_STYLE} {GRID_RULES}"
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
                f"Distinct silhouette and palette so this NPC won't be confused with others. {CHAR_STYLE}"
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
                f"SAME NPC, SAME costume, SAME palette in every cell. {CHAR_STYLE} {GRID_RULES}"
            )
    else:
        if mode == "single":
            result = (
                f"Single pixel art asset sprite. SUBJECT: {prompt}. "
                "Centered in the canvas with clear magenta margin around it. "
                "Readable silhouette, game-ready shape consistency, transparent-background-ready via magenta chroma key. "
                f"{ART_STYLE}"
            )
        else:
            rows, cols = GRID_SHAPES.get(mode, (2, 2))
            result = (
                f"A {rows}x{cols} pixel art animation sheet of the same {prompt}. "
                "The same asset identity appears in every cell, with the same bounding box, the same pixel scale, "
                "and no part crossing a cell edge. "
                "Keep the animation readable for a 2D game sprite, not a splash illustration. "
                f"{ART_STYLE}"
            )
    return result, seed


def remove_bg_magenta(img: Image.Image, threshold: int = 100, edge_threshold: int = 150) -> Image.Image:
    pixels = img.load()
    width, height = img.size

    def dist(r: int, g: int, b: int) -> float:
        return math.sqrt((r - 255) ** 2 + g**2 + (b - 255) ** 2)

    for x in range(width):
        for y in range(height):
            r, g, b, a = pixels[x, y]
            if a == 0:
                continue
            if dist(r, g, b) < threshold:
                pixels[x, y] = (0, 0, 0, 0)

    visited: set[tuple[int, int]] = set()
    queue: deque[tuple[int, int]] = deque()
    for x in range(width):
        queue.append((x, 0))
        queue.append((x, height - 1))
    for y in range(height):
        queue.append((0, y))
        queue.append((width - 1, y))

    while queue:
        x, y = queue.popleft()
        if (x, y) in visited or x < 0 or x >= width or y < 0 or y >= height:
            continue
        visited.add((x, y))
        r, g, b, a = pixels[x, y]
        if a == 0:
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    if (x + dx, y + dy) not in visited:
                        queue.append((x + dx, y + dy))
        elif dist(r, g, b) < edge_threshold:
            pixels[x, y] = (0, 0, 0, 0)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    if (x + dx, y + dy) not in visited:
                        queue.append((x + dx, y + dy))
    return img


def trim_border(img: Image.Image, px: int = 4) -> Image.Image:
    width, height = img.size
    if width > px * 2 and height > px * 2:
        return img.crop((px, px, width - px, height - px))
    return img


def clean_edges(img: Image.Image, depth: int = 3) -> Image.Image:
    pixels = img.load()
    width, height = img.size
    for d in range(depth):
        for x in range(width):
            for y in (d, height - 1 - d):
                if y < 0 or y >= height:
                    continue
                r, g, b, a = pixels[x, y]
                if a == 0:
                    continue
                if (r < 40 and g < 40 and b < 40) or math.sqrt((r - 255) ** 2 + g**2 + (b - 255) ** 2) < 150:
                    pixels[x, y] = (0, 0, 0, 0)
        for y in range(height):
            for x in (d, width - 1 - d):
                if x < 0 or x >= width:
                    continue
                r, g, b, a = pixels[x, y]
                if a == 0:
                    continue
                if (r < 40 and g < 40 and b < 40) or math.sqrt((r - 255) ** 2 + g**2 + (b - 255) ** 2) < 150:
                    pixels[x, y] = (0, 0, 0, 0)
    return img


def connected_components(img: Image.Image, min_area: int = 1) -> list[dict[str, object]]:
    alpha = img.getchannel("A")
    pixels = alpha.load()
    width, height = img.size
    visited = [[False] * width for _ in range(height)]
    components: list[dict[str, object]] = []

    for y in range(height):
        for x in range(width):
            if pixels[x, y] == 0 or visited[y][x]:
                continue
            queue: deque[tuple[int, int]] = deque([(x, y)])
            visited[y][x] = True
            area = 0
            min_x = max_x = x
            min_y = max_y = y
            touches_edge = x == 0 or y == 0 or x == width - 1 or y == height - 1

            while queue:
                cx, cy = queue.popleft()
                area += 1
                min_x = min(min_x, cx)
                min_y = min(min_y, cy)
                max_x = max(max_x, cx)
                max_y = max(max_y, cy)
                if cx == 0 or cy == 0 or cx == width - 1 or cy == height - 1:
                    touches_edge = True
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nx, ny = cx + dx, cy + dy
                    if 0 <= nx < width and 0 <= ny < height and pixels[nx, ny] > 0 and not visited[ny][nx]:
                        visited[ny][nx] = True
                        queue.append((nx, ny))

            if area >= min_area:
                components.append(
                    {
                        "area": area,
                        "bbox": (min_x, min_y, max_x + 1, max_y + 1),
                        "touches_edge": touches_edge,
                    }
                )

    components.sort(key=lambda item: int(item["area"]), reverse=True)
    return components


def pad_bbox(bbox: tuple[int, int, int, int], padding: int, width: int, height: int) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = bbox
    return (
        max(0, x0 - padding),
        max(0, y0 - padding),
        min(width, x1 + padding),
        min(height, y1 + padding),
    )


def bbox_touches_edge(
    bbox: tuple[int, int, int, int] | None, width: int, height: int, margin: int = 0
) -> bool:
    if not bbox:
        return False
    x0, y0, x1, y1 = bbox
    return x0 <= margin or y0 <= margin or x1 >= width - margin or y1 >= height - margin


def alpha_area(img: Image.Image) -> int:
    return int(np.count_nonzero(np.asarray(img.getchannel("A"))))


def alpha_core_area(img: Image.Image, horizontal_fraction: float = 0.5) -> int:
    alpha = np.asarray(img.getchannel("A"))
    half_width = max(1, int(round(img.width * horizontal_fraction / 2)))
    center_x = img.width // 2
    x0 = max(0, center_x - half_width)
    x1 = min(img.width, center_x + half_width)
    return int(np.count_nonzero(alpha[:, x0:x1]))


def estimate_anchor(
    img: Image.Image,
    bbox: tuple[int, int, int, int] | None,
    align: str,
) -> tuple[float, float]:
    if not bbox:
        return (img.width / 2, img.height / 2)

    x0, y0, x1, y1 = bbox
    if align == "center":
        return ((x0 + x1) / 2, (y0 + y1) / 2)

    alpha = np.asarray(img.getchannel("A"))
    ys, xs = np.nonzero(alpha > 0)
    inside = (xs >= x0) & (xs < x1) & (ys >= y0) & (ys < y1)
    xs = xs[inside]
    ys = ys[inside]
    if xs.size == 0:
        return ((x0 + x1) / 2, float(y1))

    central = (xs >= img.width * 0.2) & (xs <= img.width * 0.8)
    if int(np.count_nonzero(central)) >= 8:
        xs = xs[central]
        ys = ys[central]

    lower_cutoff = float(np.percentile(ys, 85))
    lower = ys >= lower_cutoff
    lower_xs = xs[lower]
    anchor_x = float(np.median(lower_xs)) if lower_xs.size else float(np.median(xs))
    anchor_y = float(np.percentile(ys, 98))
    return (anchor_x, anchor_y)


def summarize_frame_qc(frame_info: list[dict[str, object]]) -> dict[str, object]:
    valid = [info for info in frame_info if not bool(info.get("is_empty"))]
    subject_heights = np.asarray(
        [
            float(info["aligned_bbox"][3]) - float(info["aligned_bbox"][1])
            for info in valid
            if info.get("aligned_bbox")
        ],
        dtype=float,
    )
    scale_proxy = np.asarray(
        [math.sqrt(float(info.get("body_area_fraction", 0.0))) for info in valid],
        dtype=float,
    )
    anchor_x = np.asarray(
        [
            float(info["anchor_source"][0]) / max(1, int(info["source_frame_size"][0]))
            for info in valid
            if info.get("anchor_source")
        ],
        dtype=float,
    )
    anchor_y = np.asarray(
        [
            float(info["anchor_source"][1]) / max(1, int(info["source_frame_size"][1]))
            for info in valid
            if info.get("anchor_source")
        ],
        dtype=float,
    )

    scale_mean = float(np.mean(scale_proxy)) if scale_proxy.size else 0.0
    return {
        "frame_count": len(frame_info),
        "valid_frame_count": len(valid),
        "empty_count": sum(bool(info.get("is_empty")) for info in frame_info),
        "edge_touch_count": sum(bool(info.get("edge_touch")) for info in frame_info),
        "paste_clamped_count": sum(bool(info.get("paste_clamped")) for info in frame_info),
        "body_scale_mean": scale_mean,
        "output_subject_height_mean": (
            float(np.mean(subject_heights)) if subject_heights.size else 0.0
        ),
        "body_scale_cv": (
            float(np.std(scale_proxy) / scale_mean) if scale_proxy.size and scale_mean > 0 else 0.0
        ),
        "anchor_x_std": float(np.std(anchor_x)) if anchor_x.size else 0.0,
        "anchor_y_std": float(np.std(anchor_y)) if anchor_y.size else 0.0,
        "anchor_y_mean": float(np.mean(anchor_y)) if anchor_y.size else 0.0,
    }


def build_godot_sprite3d_metadata(
    metadata: dict[str, object],
    world_height: float,
    locked_pixel_size: float | None = None,
) -> dict[str, object]:
    """Build a Godot Sprite3D runtime contract from validated grid output."""
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
        "frames": [f"{label}.png" for label in labels],
    }


def build_godot_sprite3d_bundle(
    action_contracts: dict[str, tuple[str, dict[str, object]]],
    default_action: str,
    one_shot_actions: set[str] | None = None,
    max_world_height_drift: float = 0.02,
    max_pixel_size_drift: float = 0.02,
) -> dict[str, object]:
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

    action_payload: dict[str, object] = {}
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


def cmd_build_godot_bundle(args: argparse.Namespace) -> None:
    action_contracts: dict[str, tuple[str, dict[str, object]]] = {}
    output_parent = args.output.resolve().parent
    for action_spec in args.action:
        if "=" not in action_spec:
            raise ValueError("Each --action must use ACTION=PATH syntax.")
        action, raw_path = action_spec.split("=", 1)
        action = action.strip()
        if action in action_contracts:
            raise ValueError(f"Duplicate action '{action}'.")
        contract_path = Path(raw_path.strip()).resolve()
        if not contract_path.exists():
            raise ValueError(f"Action contract does not exist: {contract_path}")
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        try:
            contract_ref = contract_path.relative_to(output_parent).as_posix()
        except ValueError:
            contract_ref = contract_path.as_posix()
        action_contracts[action] = (contract_ref, contract)

    payload = build_godot_sprite3d_bundle(
        action_contracts,
        args.default_action,
        set(args.one_shot or []),
        args.max_world_height_drift,
        args.max_pixel_size_drift,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(str(args.output.resolve()))


SCALE_PROFILE_VERSION = 1
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


def processing_output_origin(processing: dict[str, object]) -> list[float]:
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
    metadata: dict[str, object],
    name: str,
    max_body_scale_drift: float,
) -> dict[str, object]:
    qc_summary = dict(metadata.get("qc_summary") or {})
    body_scale_mean = float(qc_summary.get("body_scale_mean", 0.0))
    if body_scale_mean <= 0:
        raise ValueError("Cannot create a scale profile without a valid body-scale mean.")

    processing = {key: metadata[key] for key in SCALE_PROFILE_PROCESSING_KEYS}
    profile = {
        "version": SCALE_PROFILE_VERSION,
        "name": name,
        "processing": processing,
        "output_origin": processing_output_origin(processing),
        "reference": {
            "target": metadata.get("target"),
            "mode": metadata.get("mode"),
            "rows": metadata.get("rows"),
            "cols": metadata.get("cols"),
            "body_scale_mean": body_scale_mean,
            "body_scale_cv": float(qc_summary.get("body_scale_cv", 0.0)),
            "anchor_y_mean": float(qc_summary.get("anchor_y_mean", 0.0)),
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


def load_scale_profile(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if int(payload.get("version", 0)) != SCALE_PROFILE_VERSION:
        raise ValueError(
            f"Unsupported scale profile version {payload.get('version')!r}; "
            f"expected {SCALE_PROFILE_VERSION}."
        )
    processing = payload.get("processing")
    if not isinstance(processing, dict):
        raise ValueError("Scale profile is missing its processing contract.")
    missing = [key for key in SCALE_PROFILE_PROCESSING_KEYS if key not in processing]
    if missing:
        raise ValueError(f"Scale profile processing contract is missing: {', '.join(missing)}")
    reference = payload.get("reference")
    if not isinstance(reference, dict) or float(reference.get("body_scale_mean", 0.0)) <= 0:
        raise ValueError("Scale profile is missing a valid reference body-scale mean.")
    if "output_origin" not in payload:
        payload["output_origin"] = processing_output_origin(processing)
    return payload


def apply_scale_profile(args: argparse.Namespace, profile: dict[str, object]) -> None:
    processing = dict(profile["processing"])
    for key in SCALE_PROFILE_PROCESSING_KEYS:
        setattr(args, key, processing[key])


def profile_scale_drift(
    qc_summary: dict[str, object], profile: dict[str, object]
) -> float:
    current = float(qc_summary.get("body_scale_mean", 0.0))
    reference = float(dict(profile["reference"]).get("body_scale_mean", 0.0))
    if current <= 0 or reference <= 0:
        return 0.0
    return abs(current / reference - 1.0)


def center_single_sprite(img: Image.Image, size: int, threshold: int, edge_threshold: int) -> Image.Image:
    cleaned = remove_bg_magenta(img.convert("RGBA"), threshold, edge_threshold)
    bbox = cleaned.getbbox()
    if bbox:
        cleaned = cleaned.crop(bbox)
    width, height = cleaned.size
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    if width > 0 and height > 0:
        scale = min(size / width, size / height) * 0.9
        new_width = max(1, int(width * scale))
        new_height = max(1, int(height * scale))
        cleaned = cleaned.resize((new_width, new_height), Image.Resampling.LANCZOS)
        canvas.paste(cleaned, ((size - new_width) // 2, (size - new_height) // 2))
    return canvas


def split_grid(
    img: Image.Image,
    rows: int,
    cols: int,
    cell_size: int,
    threshold: int,
    edge_threshold: int,
    fit_scale: float = 0.85,
    trim_border_px: int = 4,
    edge_clean_depth: int = 3,
    align: str = "center",
    shared_scale: bool = False,
    component_mode: str = "all",
    component_padding: int = 0,
    min_component_area: int = 1,
    edge_touch_margin: int = 0,
    scale_strategy: str = "fit",
) -> tuple[list[Image.Image], list[dict[str, object]]]:
    cleaned = remove_bg_magenta(img.convert("RGBA"), threshold, edge_threshold)
    width, height = cleaned.size
    cell_width, cell_height = width // cols, height // rows
    cropped_frames: list[Image.Image] = []
    frame_info: list[dict[str, object]] = []
    for row in range(rows):
        for col in range(cols):
            box = (col * cell_width, row * cell_height, (col + 1) * cell_width, (row + 1) * cell_height)
            frame = cleaned.crop(box)
            if trim_border_px > 0:
                frame = trim_border(frame, px=trim_border_px)
            if edge_clean_depth > 0:
                frame = clean_edges(frame, depth=edge_clean_depth)
            source_frame_size = frame.size
            components = connected_components(frame, min_area=min_component_area)
            bbox = None
            selected_component = None
            if component_mode == "largest" and components:
                selected_component = components[0]
                bbox = pad_bbox(tuple(selected_component["bbox"]), component_padding, frame.width, frame.height)
            else:
                bbox = frame.getbbox()
            is_empty = bbox is None
            subject_area = int(selected_component["area"]) if selected_component else alpha_area(frame)
            body_core_area = alpha_core_area(frame)
            anchor_bbox = tuple(selected_component["bbox"]) if selected_component else bbox
            anchor_source = estimate_anchor(frame, anchor_bbox, align) if not is_empty else None
            source_edge_touch = bbox_touches_edge(
                bbox,
                frame.width,
                frame.height,
                edge_touch_margin,
            )
            if bbox:
                frame = frame.crop(bbox)
            else:
                frame = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
            cropped_frames.append(frame)
            frame_info.append(
                {
                    "grid": [row, col],
                    "source_box": list(box),
                    "component_mode": component_mode,
                    "component_count": len(components),
                    "selected_component_area": int(selected_component["area"]) if selected_component else None,
                    "selected_component_bbox": list(selected_component["bbox"]) if selected_component else None,
                    "crop_bbox": list(bbox) if bbox else None,
                    "source_frame_size": list(source_frame_size),
                    "is_empty": is_empty,
                    "subject_area": subject_area,
                    "body_core_area": body_core_area,
                    "body_area_fraction": (
                        body_core_area / max(1, source_frame_size[0] * source_frame_size[1])
                        if not is_empty
                        else 0.0
                    ),
                    "anchor_source": list(anchor_source) if anchor_source else None,
                    "source_edge_touch": source_edge_touch,
                    "output_edge_touch": False,
                    "edge_touch": source_edge_touch,
                }
            )

    if scale_strategy == "preserve":
        target_x = cell_size / 2
        if align in {"bottom", "feet"}:
            output_pad = max(0, int(cell_size * (1 - fit_scale) * 0.5))
            target_y = cell_size - output_pad
        else:
            target_y = cell_size / 2

        frames: list[Image.Image] = []
        for index, frame in enumerate(cropped_frames):
            info = frame_info[index]
            canvas = Image.new("RGBA", (cell_size, cell_size), (0, 0, 0, 0))
            if not bool(info["is_empty"]):
                source_width, source_height = (int(value) for value in info["source_frame_size"])
                crop_x0, crop_y0, _crop_x1, _crop_y1 = (int(value) for value in info["crop_bbox"])
                anchor_x, anchor_y = (float(value) for value in info["anchor_source"])
                base_scale = min(cell_size / source_width, cell_size / source_height) * fit_scale
                scale_adjustment = 1.0
                source_to_output_scale = base_scale * scale_adjustment
                new_width = max(1, int(round(frame.width * source_to_output_scale)))
                new_height = max(1, int(round(frame.height * source_to_output_scale)))
                scaled = frame.resize((new_width, new_height), Image.Resampling.LANCZOS)
                anchor_in_crop_x = (anchor_x - crop_x0) * source_to_output_scale
                anchor_in_crop_y = (anchor_y - crop_y0) * source_to_output_scale
                paste_x = int(round(target_x - anchor_in_crop_x))
                paste_y = int(round(target_y - anchor_in_crop_y))
                unclamped_paste = [paste_x, paste_y]
                paste_x = max(0, min(cell_size - new_width, paste_x))
                paste_y = max(0, min(cell_size - new_height, paste_y))
                paste_clamped = [paste_x, paste_y] != unclamped_paste
                canvas.paste(scaled, (paste_x, paste_y), scaled)
                aligned_bbox = canvas.getbbox()
                output_edge_touch = bbox_touches_edge(
                    aligned_bbox,
                    cell_size,
                    cell_size,
                    edge_touch_margin,
                )
                info["output_size"] = [new_width, new_height]
                info["preserved_subject_size"] = [new_width, new_height]
                info["paste_position"] = [paste_x, paste_y]
                info["unclamped_paste_position"] = unclamped_paste
                info["paste_clamped"] = paste_clamped
                info["aligned_bbox"] = list(aligned_bbox) if aligned_bbox else None
                info["scale_adjustment"] = scale_adjustment
                info["source_to_output_scale"] = source_to_output_scale
                info["bbox_scale_applied"] = False
                info["scale_changed"] = bool(info["bbox_scale_applied"])
                info["anchor_target"] = [target_x, target_y]
                info["output_edge_touch"] = output_edge_touch
                info["edge_touch"] = bool(info["source_edge_touch"]) or output_edge_touch
            else:
                info["output_size"] = [0, 0]
                info["preserved_subject_size"] = [0, 0]
                info["paste_position"] = [0, 0]
                info["unclamped_paste_position"] = [0, 0]
                info["paste_clamped"] = False
                info["aligned_bbox"] = None
                info["scale_adjustment"] = 1.0
                info["source_to_output_scale"] = 0.0
                info["bbox_scale_applied"] = False
                info["scale_changed"] = False
                info["anchor_target"] = [target_x, target_y]
            frames.append(canvas)

        for info in frame_info:
            info["scale_strategy"] = scale_strategy
            info["shared_center_x"] = target_x
            info["shared_feet_y"] = target_y
        return frames, frame_info

    common_scale = None
    if shared_scale:
        valid_frames = [
            frame for frame, info in zip(cropped_frames, frame_info) if not bool(info["is_empty"])
        ]
        max_width = max((frame.size[0] for frame in valid_frames), default=0)
        max_height = max((frame.size[1] for frame in valid_frames), default=0)
        if max_width > 0 and max_height > 0:
            common_scale = min(cell_size / max_width, cell_size / max_height) * fit_scale

    frames: list[Image.Image] = []
    for index, frame in enumerate(cropped_frames):
        frame_width, frame_height = frame.size
        canvas = Image.new("RGBA", (cell_size, cell_size), (0, 0, 0, 0))
        info = frame_info[index]
        if not bool(info["is_empty"]):
            scale = common_scale or (min(cell_size / frame_width, cell_size / frame_height) * fit_scale)
            new_width = max(1, int(frame_width * scale))
            new_height = max(1, int(frame_height * scale))
            frame = frame.resize((new_width, new_height), Image.Resampling.LANCZOS)
            paste_x = (cell_size - new_width) // 2
            if align in {"bottom", "feet"}:
                pad = max(0, int(cell_size * (1 - fit_scale) * 0.5))
                paste_y = cell_size - new_height - pad
            else:
                paste_y = (cell_size - new_height) // 2
            canvas.paste(frame, (paste_x, paste_y))
            output_bbox = canvas.getbbox()
            output_edge_touch = bbox_touches_edge(
                output_bbox,
                cell_size,
                cell_size,
                edge_touch_margin,
            )
            info["output_size"] = [new_width, new_height]
            info["paste_position"] = [paste_x, paste_y]
            info["paste_clamped"] = False
            info["aligned_bbox"] = list(output_bbox) if output_bbox else None
            info["output_edge_touch"] = output_edge_touch
            info["edge_touch"] = bool(info["source_edge_touch"]) or output_edge_touch
            info["source_to_output_scale"] = scale
            info["bbox_scale_applied"] = True
        else:
            info["output_size"] = [0, 0]
            info["paste_position"] = [0, 0]
            info["paste_clamped"] = False
            info["aligned_bbox"] = None
            info["source_to_output_scale"] = 0.0
            info["bbox_scale_applied"] = False
        info["scale_strategy"] = "fit"
        info["scale_changed"] = bool(info["bbox_scale_applied"])
        frames.append(canvas)
    return frames, frame_info


def compose_sheet(frames: list[Image.Image], rows: int, cols: int, cell_size: int) -> Image.Image:
    canvas = Image.new("RGBA", (cols * cell_size, rows * cell_size), (0, 0, 0, 0))
    for index, frame in enumerate(frames):
        row, col = divmod(index, cols)
        canvas.paste(frame, (col * cell_size, row * cell_size), frame)
    return canvas


def save_transparent_gif(frames: list[Image.Image], out_path: Path, duration: int) -> None:
    if not frames:
        raise ValueError("No frames to encode.")

    key = (255, 0, 254)
    width, height = frames[0].size
    stacked = Image.new("RGB", (width, height * len(frames)), key)

    for index, frame in enumerate(frames):
        r, g, b, a = frame.split()
        hard_mask = a.point(lambda value: 255 if value >= 128 else 0)
        rgb = Image.merge("RGB", (r, g, b))
        stacked.paste(rgb, (0, index * height), hard_mask)

    paletted = stacked.convert("P", palette=Image.Palette.ADAPTIVE, colors=256, dither=Image.Dither.NONE)
    palette = list(paletted.getpalette() or [])
    while len(palette) < 256 * 3:
        palette.append(0)

    key_index = None
    for index in range(256):
        if palette[index * 3 : index * 3 + 3] == list(key):
            key_index = index
            break
    if key_index is None:
        best_distance = None
        best_index = 0
        for index in range(256):
            r, g, b = palette[index * 3], palette[index * 3 + 1], palette[index * 3 + 2]
            distance = (r - key[0]) ** 2 + (g - key[1]) ** 2 + (b - key[2]) ** 2
            if best_distance is None or distance < best_distance:
                best_distance = distance
                best_index = index
        key_index = best_index

    if key_index != 0:
        lut = np.arange(256, dtype=np.uint8)
        lut[0], lut[key_index] = key_index, 0
        arr = np.array(paletted)
        arr = lut[arr]
        paletted = Image.fromarray(arr, mode="P")
        for channel in range(3):
            zero_idx = channel
            key_idx = key_index * 3 + channel
            palette[zero_idx], palette[key_idx] = palette[key_idx], palette[zero_idx]
        paletted.putpalette(palette)

    out_frames = [
        paletted.crop((0, index * height, width, (index + 1) * height))
        for index in range(len(frames))
    ]
    out_frames[0].save(
        out_path,
        format="GIF",
        save_all=True,
        append_images=out_frames[1:],
        duration=duration,
        loop=0,
        disposal=2,
        transparency=0,
        background=0,
    )


def sanitize_slug(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return slug or "sprite"


def cmd_list_options() -> None:
    print(
        json.dumps(
            {
                "targets": TARGET_MODES,
                "npc_roles": NPC_ROLES,
                "grid_shapes": GRID_SHAPES,
                "frame_labels": FRAME_LABELS,
                "processor": {
                    "component_mode": ["all", "largest"],
                    "align": ["center", "bottom", "feet"],
                    "scale_strategy": ["fit", "preserve"],
                    "strict_qc": {
                        "structural_checks": ["empty", "edge_touch", "paste_clamped"],
                        "optional_metrics": [
                            "max_body_scale_cv",
                            "max_anchor_y_std",
                            "max_profile_scale_drift",
                        ],
                    },
                    "scale_profile_version": SCALE_PROFILE_VERSION,
                },
            },
            indent=2,
        )
    )


def cmd_build_prompt(args: argparse.Namespace) -> None:
    prompt_text, seed = build_prompt(args.target, args.mode, args.prompt, args.role, args.seed)
    payload = {
        "target": args.target,
        "mode": args.mode,
        "prompt": args.prompt,
        "role": args.role or "",
        "seed": seed,
        "generated_prompt": prompt_text,
    }
    if args.write:
        args.write.parent.mkdir(parents=True, exist_ok=True)
        args.write.write_text(prompt_text, encoding="utf-8")
    if args.write_json:
        args.write_json.parent.mkdir(parents=True, exist_ok=True)
        args.write_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(prompt_text)


def cmd_process(args: argparse.Namespace) -> None:
    if args.target not in PROCESS_TARGETS:
        raise ValueError(f"Unknown process target '{args.target}'. Valid targets: {', '.join(PROCESS_TARGETS)}")
    out_dir = args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    godot_sprite3d_payload = None
    godot_sprite3d_path = None

    if args.write_godot_sprite3d_meta and args.godot_world_height is None:
        raise ValueError("--write-godot-sprite3d-meta requires --godot-world-height.")

    if args.scale_profile and args.write_scale_profile:
        raise ValueError("Use either --scale-profile or --write-scale-profile, not both.")
    scale_profile = load_scale_profile(args.scale_profile) if args.scale_profile else None
    if scale_profile:
        apply_scale_profile(args, scale_profile)

    raw = Image.open(args.input).convert("RGBA")
    metadata = {
        "target": args.target,
        "mode": args.mode,
        "prompt": args.prompt or "",
        "role": args.role or "",
        "input": str(args.input),
        "threshold": args.threshold,
        "edge_threshold": args.edge_threshold,
        "duration": args.duration,
    }

    has_custom_grid = args.rows is not None or args.cols is not None
    if has_custom_grid and (args.rows is None or args.cols is None):
        raise ValueError("Custom grids require both --rows and --cols.")

    if has_custom_grid or args.mode in GRID_SHAPES:
        if has_custom_grid:
            rows, cols = args.rows, args.cols
        else:
            rows, cols = GRID_SHAPES[args.mode]
        cell_size = args.cell_size or (96 if (rows, cols) == (4, 4) else 128)
        raw.save(out_dir / "raw-sheet.png")
        cleaned = remove_bg_magenta(raw.copy(), args.threshold, args.edge_threshold)
        cleaned.save(out_dir / "raw-sheet-clean.png")

        frames, frame_qc = split_grid(
            raw,
            rows,
            cols,
            cell_size,
            args.threshold,
            args.edge_threshold,
            fit_scale=args.fit_scale,
            trim_border_px=args.trim_border,
            edge_clean_depth=args.edge_clean_depth,
            align=args.align,
            shared_scale=args.shared_scale,
            component_mode=args.component_mode,
            component_padding=args.component_padding,
            min_component_area=args.min_component_area,
            edge_touch_margin=args.edge_touch_margin,
            scale_strategy=args.scale_strategy,
        )
        if has_custom_grid:
            prefix = args.label_prefix or args.mode
            labels = [f"{prefix}-{index + 1}" for index in range(rows * cols)]
        else:
            labels = FRAME_LABELS[args.mode]
        for label, frame in zip(labels, frames):
            frame.save(out_dir / f"{label}.png")

        compose_sheet(frames, rows, cols, cell_size).save(out_dir / "sheet-transparent.png")

        if args.mode == "player_sheet" and not has_custom_grid and (rows, cols) == (4, 4):
            directions = ["down", "left", "right", "up"]
            for row_index, direction in enumerate(directions):
                row_frames = frames[row_index * cols : (row_index + 1) * cols]
                compose_sheet(row_frames, 1, cols, cell_size).save(out_dir / f"{direction}-strip.png")
                save_transparent_gif(row_frames, out_dir / f"{direction}.gif", args.duration)
            metadata["directions"] = directions
        else:
            save_transparent_gif(frames, out_dir / "animation.gif", args.duration)

        metadata["rows"] = rows
        metadata["cols"] = cols
        metadata["cell_size"] = cell_size
        metadata["fit_scale"] = args.fit_scale
        metadata["trim_border"] = args.trim_border
        metadata["edge_clean_depth"] = args.edge_clean_depth
        metadata["align"] = args.align
        metadata["shared_scale"] = args.shared_scale
        metadata["scale_strategy"] = args.scale_strategy
        metadata["component_mode"] = args.component_mode
        metadata["component_padding"] = args.component_padding
        metadata["min_component_area"] = args.min_component_area
        metadata["edge_touch_margin"] = args.edge_touch_margin
        metadata["frame_labels"] = labels
        metadata["frames"] = frame_qc
        metadata["edge_touch_frames"] = [
            info["grid"] for info in frame_qc if bool(info.get("edge_touch"))
        ]
        metadata["source_edge_touch_frames"] = [
            info["grid"] for info in frame_qc if bool(info.get("source_edge_touch"))
        ]
        metadata["output_edge_touch_frames"] = [
            info["grid"] for info in frame_qc if bool(info.get("output_edge_touch"))
        ]
        metadata["empty_frames"] = [
            info["grid"] for info in frame_qc if bool(info.get("is_empty"))
        ]
        metadata["paste_clamped_frames"] = [
            info["grid"] for info in frame_qc if bool(info.get("paste_clamped"))
        ]
        metadata["qc_summary"] = summarize_frame_qc(frame_qc)
        valid_origins = [info.get("anchor_target") for info in frame_qc if info.get("anchor_target")]
        if valid_origins:
            metadata["output_origin"] = valid_origins[0]
        if scale_profile:
            drift = profile_scale_drift(metadata["qc_summary"], scale_profile)
            metadata["qc_summary"]["profile_body_scale_drift"] = drift
            metadata["scale_profile"] = {
                "path": str(args.scale_profile),
                "version": scale_profile["version"],
                "name": scale_profile.get("name", ""),
                "reference_mode": dict(scale_profile["reference"]).get("mode"),
                "processing_contract_applied": True,
            }
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
            godot_sprite3d_payload = build_godot_sprite3d_metadata(
                metadata,
                args.godot_world_height,
                float(locked_pixel_size) if locked_pixel_size is not None else None,
            )
            godot_sprite3d_path = args.write_godot_sprite3d_meta or (
                out_dir / "godot-sprite3d.json"
            )
            metadata["godot_sprite3d"] = godot_sprite3d_payload
            metadata["godot_sprite3d_output"] = str(godot_sprite3d_path)
    else:
        if args.godot_world_height is not None:
            raise ValueError("Godot Sprite3D metadata currently requires processed grid frames.")
        raw.save(out_dir / "raw.png")
        centered = center_single_sprite(raw, args.single_size, args.threshold, args.edge_threshold)
        centered.save(out_dir / "clean.png")
        metadata["single_size"] = args.single_size

    if args.prompt_file and args.prompt_file.exists():
        prompt_text = args.prompt_file.read_text(encoding="utf-8")
        (out_dir / "prompt-used.txt").write_text(prompt_text, encoding="utf-8")
    elif args.prompt:
        (out_dir / "prompt-used.txt").write_text(args.prompt, encoding="utf-8")

    effective_profile_limit = args.max_profile_scale_drift
    if effective_profile_limit is None and scale_profile:
        effective_profile_limit = float(
            dict(scale_profile.get("qc") or {}).get("max_body_scale_drift", 0.10)
        )
    metadata["qc_config"] = {
        "strict_qc": args.strict_qc,
        "reject_edge_touch": args.reject_edge_touch,
        "allow_source_edge_touch": args.allow_source_edge_touch,
        "max_body_scale_cv": args.max_body_scale_cv,
        "max_anchor_y_std": args.max_anchor_y_std,
        "max_profile_scale_drift": effective_profile_limit,
    }
    (out_dir / "pipeline-meta.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    qc_errors = []
    if args.reject_edge_touch and metadata.get("edge_touch_frames"):
        qc_errors.append(f"frames touch a cell edge: {metadata['edge_touch_frames']}")
    if args.strict_qc:
        if metadata.get("empty_frames"):
            qc_errors.append(f"empty frames: {metadata['empty_frames']}")
        if metadata.get("paste_clamped_frames"):
            qc_errors.append(f"clamped frames: {metadata['paste_clamped_frames']}")
        if metadata.get("output_edge_touch_frames") and not args.reject_edge_touch:
            qc_errors.append(
                f"processed frames touch an output edge: {metadata['output_edge_touch_frames']}"
            )
        if (
            metadata.get("source_edge_touch_frames")
            and not args.allow_source_edge_touch
            and not args.reject_edge_touch
        ):
            qc_errors.append(
                f"raw subjects touch a source-cell edge: {metadata['source_edge_touch_frames']}"
            )
        qc_summary = metadata.get("qc_summary") or {}
        if (
            args.max_body_scale_cv is not None
            and float(qc_summary.get("body_scale_cv", 0.0)) > args.max_body_scale_cv
        ):
            qc_errors.append(
                f"body scale CV {float(qc_summary['body_scale_cv']):.4f} exceeds "
                f"{args.max_body_scale_cv:.4f}"
            )
        if (
            args.max_anchor_y_std is not None
            and float(qc_summary.get("anchor_y_std", 0.0)) > args.max_anchor_y_std
        ):
            qc_errors.append(
                f"anchor Y std {float(qc_summary['anchor_y_std']):.4f} exceeds "
                f"{args.max_anchor_y_std:.4f}"
            )
        if scale_profile:
            profile_limit = effective_profile_limit
            if profile_limit is None:
                profile_limit = 0.10
            drift = float(qc_summary.get("profile_body_scale_drift", 0.0))
            if drift > profile_limit:
                qc_errors.append(
                    f"profile body-scale drift {drift:.4f} exceeds {profile_limit:.4f}"
                )
    if qc_errors:
        raise ValueError("QC failed: " + "; ".join(qc_errors))
    if godot_sprite3d_payload is not None and godot_sprite3d_path is not None:
        godot_sprite3d_path.parent.mkdir(parents=True, exist_ok=True)
        godot_sprite3d_path.write_text(
            json.dumps(godot_sprite3d_payload, indent=2), encoding="utf-8"
        )
        (out_dir / "pipeline-meta.json").write_text(
            json.dumps(metadata, indent=2), encoding="utf-8"
        )
    if args.write_scale_profile:
        if "qc_summary" not in metadata:
            raise ValueError("Scale profiles can only be written from processed grid sheets.")
        profile_limit = args.max_profile_scale_drift
        if profile_limit is None:
            profile_limit = 0.10
        profile_payload = build_scale_profile(
            metadata,
            args.profile_name or sanitize_slug(f"{args.target}-{args.mode}"),
            profile_limit,
        )
        args.write_scale_profile.parent.mkdir(parents=True, exist_ok=True)
        args.write_scale_profile.write_text(
            json.dumps(profile_payload, indent=2), encoding="utf-8"
        )
        metadata["scale_profile_output"] = str(args.write_scale_profile)
        (out_dir / "pipeline-meta.json").write_text(
            json.dumps(metadata, indent=2), encoding="utf-8"
        )
    print(str(out_dir.resolve()))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
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
    bundle_parser.add_argument("--output", required=True, type=Path)

    process_parser = subparsers.add_parser("process", help="Postprocess a generated sprite image.")
    process_parser.add_argument("--input", required=True, type=Path)
    process_parser.add_argument("--target", required=True, choices=PROCESS_TARGETS)
    process_parser.add_argument("--mode", required=True)
    process_parser.add_argument("--output-dir", required=True, type=Path)
    process_parser.add_argument("--role")
    process_parser.add_argument("--prompt")
    process_parser.add_argument("--prompt-file", type=Path)
    process_parser.add_argument("--threshold", type=int, default=100)
    process_parser.add_argument("--edge-threshold", type=int, default=150)
    process_parser.add_argument("--cell-size", type=int)
    process_parser.add_argument("--rows", type=int)
    process_parser.add_argument("--cols", type=int)
    process_parser.add_argument("--label-prefix")
    process_parser.add_argument("--fit-scale", type=float, default=0.85)
    process_parser.add_argument("--trim-border", type=int, default=4)
    process_parser.add_argument("--edge-clean-depth", type=int, default=3)
    process_parser.add_argument("--align", choices=["center", "bottom", "feet"], default="center")
    process_parser.add_argument("--shared-scale", action="store_true")
    process_parser.add_argument(
        "--scale-strategy",
        choices=["fit", "preserve"],
        default="fit",
        help=(
            "fit crops to the detected bbox and scales it into the output cell; "
            "preserve applies one uniform raw-cell scale and translates each subject to a shared anchor"
        ),
    )
    process_parser.add_argument("--component-mode", choices=["all", "largest"], default="all")
    process_parser.add_argument("--component-padding", type=int, default=0)
    process_parser.add_argument("--min-component-area", type=int, default=1)
    process_parser.add_argument("--edge-touch-margin", type=int, default=0)
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
        help="Apply one character bundle's locked output scale, anchor, and processor contract.",
    )
    process_parser.add_argument(
        "--write-scale-profile",
        type=Path,
        help="Write a reusable character scale profile after this sheet passes QC.",
    )
    process_parser.add_argument("--profile-name")
    process_parser.add_argument(
        "--max-body-scale-cv",
        type=float,
        help="Strict-QC maximum body-scale coefficient of variation, for example 0.08.",
    )
    process_parser.add_argument(
        "--max-anchor-y-std",
        type=float,
        help="Strict-QC maximum normalized vertical-anchor standard deviation, for example 0.05.",
    )
    process_parser.add_argument(
        "--max-profile-scale-drift",
        type=float,
        help="Strict-QC maximum body-scale drift from --scale-profile, for example 0.10.",
    )
    process_parser.add_argument("--single-size", type=int, default=256)
    process_parser.add_argument("--duration", type=int, default=200)
    process_parser.add_argument(
        "--godot-world-height",
        type=float,
        help=(
            "Desired subject height in Godot world units. Writes godot-sprite3d.json "
            "with a QC-derived pixel size, feet origin, and animation timing."
        ),
    )
    process_parser.add_argument(
        "--write-godot-sprite3d-meta",
        type=Path,
        help="Optional output path for Godot Sprite3D metadata.",
    )

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "list-options":
        cmd_list_options()
    elif args.command == "build-prompt":
        cmd_build_prompt(args)
    elif args.command == "build-godot-bundle":
        cmd_build_godot_bundle(args)
    else:
        cmd_process(args)


if __name__ == "__main__":
    main()
