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
            components = connected_components(frame, min_area=min_component_area)
            bbox = None
            selected_component = None
            if component_mode == "largest" and components:
                selected_component = components[0]
                bbox = pad_bbox(tuple(selected_component["bbox"]), component_padding, frame.width, frame.height)
            else:
                bbox = frame.getbbox()
            if bbox:
                frame = frame.crop(bbox)
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
                    "edge_touch": bbox_touches_edge(bbox, cell_width, cell_height, edge_touch_margin),
                }
            )

    common_scale = None
    if shared_scale:
        max_width = max((frame.size[0] for frame in cropped_frames), default=0)
        max_height = max((frame.size[1] for frame in cropped_frames), default=0)
        if max_width > 0 and max_height > 0:
            common_scale = min(cell_size / max_width, cell_size / max_height) * fit_scale

    frames: list[Image.Image] = []
    for index, frame in enumerate(cropped_frames):
        frame_width, frame_height = frame.size
        canvas = Image.new("RGBA", (cell_size, cell_size), (0, 0, 0, 0))
        if frame_width > 0 and frame_height > 0:
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
            frame_info[index]["output_size"] = [new_width, new_height]
            frame_info[index]["paste_position"] = [paste_x, paste_y]
        else:
            frame_info[index]["output_size"] = [0, 0]
            frame_info[index]["paste_position"] = [0, 0]
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
        metadata["component_mode"] = args.component_mode
        metadata["component_padding"] = args.component_padding
        metadata["min_component_area"] = args.min_component_area
        metadata["edge_touch_margin"] = args.edge_touch_margin
        metadata["frame_labels"] = labels
        metadata["frames"] = frame_qc
        metadata["edge_touch_frames"] = [
            info["grid"] for info in frame_qc if bool(info.get("edge_touch"))
        ]
        if args.reject_edge_touch and metadata["edge_touch_frames"]:
            raise ValueError(f"Frames touch a cell edge: {metadata['edge_touch_frames']}")
    else:
        raw.save(out_dir / "raw.png")
        centered = center_single_sprite(raw, args.single_size, args.threshold, args.edge_threshold)
        centered.save(out_dir / "clean.png")
        metadata["single_size"] = args.single_size

    if args.prompt_file and args.prompt_file.exists():
        prompt_text = args.prompt_file.read_text(encoding="utf-8")
        (out_dir / "prompt-used.txt").write_text(prompt_text, encoding="utf-8")
    elif args.prompt:
        (out_dir / "prompt-used.txt").write_text(args.prompt, encoding="utf-8")

    (out_dir / "pipeline-meta.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
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
    process_parser.add_argument("--component-mode", choices=["all", "largest"], default="all")
    process_parser.add_argument("--component-padding", type=int, default=0)
    process_parser.add_argument("--min-component-area", type=int, default=1)
    process_parser.add_argument("--edge-touch-margin", type=int, default=0)
    process_parser.add_argument("--reject-edge-touch", action="store_true")
    process_parser.add_argument("--single-size", type=int, default=256)
    process_parser.add_argument("--duration", type=int, default=200)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "list-options":
        cmd_list_options()
    elif args.command == "build-prompt":
        cmd_build_prompt(args)
    else:
        cmd_process(args)


if __name__ == "__main__":
    main()
