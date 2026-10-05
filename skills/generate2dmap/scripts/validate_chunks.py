#!/usr/bin/env python3
"""Validate room chunks (generate2dmap.room_chunk.v1): edge sockets, stitching and reachability.

A chunk is a rectangular room of `size` [w, h] world pixels with door sockets on
its N, E, S and W edges. A socket is {offset, width, material}: `offset` runs
along the edge from its top-left end (x for N/S, y for E/W).

The `graph` places chunks in one of two forms:

  rows   [["a", "b"], ["c", null]]   a layout grid; widths agree per column and
                                     heights per row; null leaves a cell empty; an
                                     id in several cells becomes one instance per
                                     cell, named id@row,col.
  edges  [{"from": "a", "to": "b", "side": "E", "offset": 0}]
                                     b sits on a's east edge, shifted `offset`
                                     px along it; each declared edge needs a door.

Wherever two placed chunks touch, every socket on the shared edge must meet a
socket on the other side with the same span and material. A socket on an edge
that touches nothing is reported as dangling (a warning: it may be a world exit).
Chunks are reachable when doors connect them to the start chunk. When every
placed chunk also has a walkability `grid` (rows of '.' walkable and '#'
blocked, `cell` px per character), the stitched grid is searched as well:
every chunk and door must be reachable on foot (4-neighbour moves), and doors
must not open into walls. Without --output-dir only the one-line summary is
printed; with it, chunk-report.json and chunk-debug.png are published into the
new folder. Exit 1 when a check fails; --strict-qc then publishes nothing.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from PIL import Image, ImageDraw

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import forge_core  # noqa: E402  (this skill's vendored copy)


INPUT_SCHEMA = "generate2dmap.room_chunk.v1"
REPORT_SCHEMA = "generate2dmap.chunk_validation.v1"
TOOL = {"name": "validate_chunks", "version": "1.0"}
SIDES = ("N", "E", "S", "W")
OPPOSITE = {"N": "S", "S": "N", "E": "W", "W": "E"}
WALKABLE, BLOCKED = ".", "#"
EPSILON = 1e-6
NOT_PROVEN = [
    "Doors are matched by span and material only; door art, collision and transitions are not checked.",
    "Grid reachability uses the declared walkability grids with 4-neighbour moves; side-view jumps, "
    "one-way passages and actor size are not modelled (see validate_layout.py for side-view levels).",
    "A dangling socket may be an intended world exit; the tool cannot tell.",
]
CHUNK_COLORS = ((86, 120, 168), (120, 150, 96), (164, 112, 92), (128, 104, 160), (92, 146, 148), (168, 140, 84))
SOCKET_COLORS = {"paired": (60, 220, 90), "mismatch": (235, 64, 64), "straddle": (235, 64, 64),
                 "dangling": (245, 200, 40)}


# --------------------------------------------------------------------------- input model

@dataclass
class Socket:
    chunk: str
    side: str
    index: int
    offset: float
    width: float
    material: str | None
    status: str = "unchecked"
    partner: tuple[str, str, int] | None = None
    world: tuple[float, float] | None = None
    messages: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.chunk}.{self.side}[{self.index}]"


@dataclass
class Chunk:
    id: str
    width: int
    height: int
    sockets: dict[str, list[Socket]]
    grid: np.ndarray | None = None  # bool walkable, rows x cols
    cell: int | None = None


def _number(value: Any, name: str, *, minimum: float | None = None, exclusive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number.")
    if minimum is not None and (value <= minimum if exclusive else value < minimum):
        raise ValueError(f"{name} must be {'greater than' if exclusive else 'at least'} {minimum:g}.")
    return float(value)


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a whole number of pixels, at least 1.")
    return value


def _parse_grid(rows: Any, cell: int | None, chunk_id: str, size: tuple[int, int]) -> np.ndarray:
    if not isinstance(rows, list) or not rows or not all(isinstance(row, str) for row in rows):
        raise ValueError(f"chunk {chunk_id}: grid must be a list of strings of '{WALKABLE}' and '{BLOCKED}'.")
    if cell is None:
        raise ValueError(f"chunk {chunk_id}: a grid needs `cell` (pixels per character) on the chunk or the file.")
    widths = {len(row) for row in rows}
    if len(widths) != 1:
        raise ValueError(f"chunk {chunk_id}: grid rows have different lengths {sorted(widths)}.")
    bad = sorted({char for row in rows for char in row} - {WALKABLE, BLOCKED})
    if bad:
        raise ValueError(f"chunk {chunk_id}: grid may only use '{WALKABLE}' and '{BLOCKED}', found {bad}.")
    cols, count = widths.pop(), len(rows)
    if size[0] != cols * cell or size[1] != count * cell:
        raise ValueError(f"chunk {chunk_id}: grid {cols}x{count} cells of {cell} px is "
                         f"{cols * cell}x{count * cell} px, but size is {size[0]}x{size[1]}.")
    return np.array([[char == WALKABLE for char in row] for row in rows], dtype=bool)


def parse_chunks(document: Any) -> list[Chunk]:
    """Parse and sanity-check the chunk list; raise ValueError with a readable message."""
    if not isinstance(document, dict):
        raise ValueError("The chunk file must be a JSON object.")
    if document.get("schema") != INPUT_SCHEMA:
        raise ValueError(f"schema must be {INPUT_SCHEMA!r} (got {document.get('schema')!r}).")
    top_cell = document.get("cell")
    if top_cell is not None:
        top_cell = _positive_int(top_cell, "cell")
    entries = document.get("chunks")
    if not isinstance(entries, list) or not entries:
        raise ValueError("chunks must be a non-empty list.")
    chunks: list[Chunk] = []
    seen: set[str] = set()
    for position, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"chunks[{position}] must be an object.")
        ident = entry.get("id")
        if not isinstance(ident, str) or not ident:
            raise ValueError(f"chunks[{position}] needs a non-empty string id.")
        if ident in seen:
            raise ValueError(f"chunk id {ident!r} is used twice.")
        seen.add(ident)
        size = entry.get("size")
        if not isinstance(size, list) or len(size) != 2:
            raise ValueError(f"chunk {ident}: size must be [width, height].")
        width, height = (_positive_int(value, f"chunk {ident} size") for value in size)
        sockets_in = entry.get("sockets", {})
        if not isinstance(sockets_in, dict):
            raise ValueError(f"chunk {ident}: sockets must be an object keyed by N, E, S, W.")
        sockets: dict[str, list[Socket]] = {side: [] for side in SIDES}
        for side, items in sockets_in.items():
            if side not in SIDES:
                raise ValueError(f"chunk {ident}: socket side {side!r} must be one of N, E, S, W.")
            if not isinstance(items, list):
                raise ValueError(f"chunk {ident}: sockets.{side} must be a list.")
            for index, item in enumerate(items):
                where = f"chunk {ident} socket {side}[{index}]"
                if not isinstance(item, dict):
                    raise ValueError(f"{where} must be an object with offset and width.")
                material = item.get("material")
                if material is not None and (not isinstance(material, str) or not material):
                    raise ValueError(f"{where}: material must be a non-empty string.")
                sockets[side].append(Socket(ident, side, index,
                                            _number(item.get("offset"), f"{where} offset", minimum=0),
                                            _number(item.get("width"), f"{where} width", minimum=0, exclusive=True),
                                            material))
        cell = entry.get("cell", top_cell)
        if cell is not None:
            cell = _positive_int(cell, f"chunk {ident} cell")
        grid = _parse_grid(entry["grid"], cell, ident, (width, height)) if "grid" in entry else None
        chunks.append(Chunk(ident, width, height, sockets, grid, cell))
    return chunks


def parse_graph(document: dict[str, Any], ids: set[str]) -> tuple[str, list[list[str | None]], list[dict[str, Any]]]:
    """Return ("rows" | "edges" | "kit", rows, edges)."""
    graph = document.get("graph")
    if not isinstance(graph, list):
        raise ValueError("graph must be a list (layout rows, edges, or empty for a kit).")
    if not graph:
        return "kit", [], []
    if all(isinstance(item, list) for item in graph):
        rows: list[list[str | None]] = []
        for r, row in enumerate(graph):
            cells: list[str | None] = []
            for c, value in enumerate(row):
                if value is None or value == "":
                    cells.append(None)
                elif isinstance(value, str) and value in ids:
                    cells.append(value)
                else:
                    raise ValueError(f"graph[{r}][{c}] = {value!r} is not a chunk id or null.")
            rows.append(cells)
        if not any(cell for row in rows for cell in row):
            raise ValueError("the layout rows place no chunk.")
        return "rows", rows, []
    if all(isinstance(item, dict) for item in graph):
        edges = []
        for index, item in enumerate(graph):
            source, target, side = item.get("from"), item.get("to"), item.get("side")
            if source not in ids or target not in ids:
                raise ValueError(f"graph[{index}] must name two chunk ids in from and to.")
            if source == target:
                raise ValueError(f"graph[{index}] connects chunk {source} to itself.")
            if side not in SIDES:
                raise ValueError(f"graph[{index}].side must be one of N, E, S, W.")
            offset = _number(item.get("offset", 0), f"graph[{index}].offset")
            edges.append({"from": source, "to": target, "side": side, "offset": offset, "index": index})
        return "edges", [], edges
    raise ValueError("graph must be either all layout rows (lists) or all edges (objects), not a mix.")


def parse_start(text: Any, ids: set[str]) -> tuple[str, tuple[float, float] | None] | None:
    """`a`, `a:40,90` (CLI) or {chunk, x, y} (file); the point is in that chunk's pixels."""
    if text is None:
        return None
    if isinstance(text, dict):
        chunk = text.get("chunk")
        point = None
        if "x" in text or "y" in text:
            point = (_number(text.get("x"), "start.x"), _number(text.get("y"), "start.y"))
    else:
        match = re.fullmatch(r"\s*([^:]+?)\s*(?::\s*(-?[0-9.]+)\s*,\s*(-?[0-9.]+)\s*)?", str(text))
        if not match:
            raise ValueError(f"start {text!r} must look like CHUNK or CHUNK:X,Y.")
        chunk = match.group(1)
        point = (float(match.group(2)), float(match.group(3))) if match.group(2) else None
    if chunk not in ids:
        raise ValueError(f"start chunk {chunk!r} is not a chunk id.")
    return chunk, point


# --------------------------------------------------------------------------- checks

def _check(ident: str, status: str, value: Any = None, threshold: Any = None) -> dict[str, Any]:
    return {"id": ident, "status": status, "value": value, "threshold": threshold}


def _edge_length(chunk: Chunk, side: str) -> int:
    return chunk.width if side in ("N", "S") else chunk.height


def check_socket_geometry(chunks: list[Chunk]) -> list[dict[str, Any]]:
    """Sockets must lie on their edge and must not overlap each other."""
    outside, overlapping = [], []
    for chunk in chunks:
        for side in SIDES:
            length = _edge_length(chunk, side)
            spans = sorted(chunk.sockets[side], key=lambda socket: (socket.offset, socket.index))
            for socket in spans:
                if socket.offset + socket.width > length + EPSILON:
                    outside.append(f"{socket.key} [{socket.offset:g}, {socket.offset + socket.width:g}) runs past "
                                   f"the {length} px edge")
            for first, second in zip(spans, spans[1:]):
                if second.offset < first.offset + first.width - EPSILON:
                    overlapping.append(f"{first.key} and {second.key} overlap")
    return [_check("socket_bounds", "fail" if outside else "pass", outside),
            _check("socket_overlap", "fail" if overlapping else "pass", overlapping)]


def instance_rows(rows: list[list[str | None]], chunks: dict[str, Chunk]) -> tuple[list[list[str | None]],
                                                                                    dict[str, Chunk]]:
    """Give every layout cell its own chunk instance; an id used more than once becomes `id@row,col`."""
    counts: dict[str, int] = {}
    for row in rows:
        for ident in row:
            if ident:
                counts[ident] = counts.get(ident, 0) + 1
    named: list[list[str | None]] = []
    instances: dict[str, Chunk] = {}
    for r, row in enumerate(rows):
        named.append([])
        for c, ident in enumerate(row):
            if ident is None:
                named[-1].append(None)
                continue
            name = ident if counts[ident] == 1 else f"{ident}@{r},{c}"
            source = chunks[ident]
            sockets = {side: [Socket(name, s.side, s.index, s.offset, s.width, s.material)
                              for s in source.sockets[side]] for side in SIDES}
            instances[name] = Chunk(name, source.width, source.height, sockets, source.grid, source.cell)
            named[-1].append(name)
    return named, instances


def place_rows(rows: list[list[str | None]], chunks: dict[str, Chunk]) -> tuple[dict[str, tuple[int, int]], list[str]]:
    """Positions from layout rows; widths must agree per column and heights per row."""
    problems: list[str] = []
    columns = max(len(row) for row in rows)
    widths: list[int | None] = [None] * columns
    heights: list[int | None] = [None] * len(rows)
    for r, row in enumerate(rows):
        for c, ident in enumerate(row):
            if ident is None:
                continue
            chunk = chunks[ident]
            if widths[c] not in (None, chunk.width):
                problems.append(f"column {c} mixes widths {widths[c]} and {chunk.width} ({ident})")
            if heights[r] not in (None, chunk.height):
                problems.append(f"row {r} mixes heights {heights[r]} and {chunk.height} ({ident})")
            widths[c] = widths[c] or chunk.width
            heights[r] = heights[r] or chunk.height
    for c, width in enumerate(widths):
        if width is None:
            problems.append(f"column {c} is empty in every row; its width is unknown")
    for r, height in enumerate(heights):
        if height is None:
            problems.append(f"row {r} is empty; its height is unknown")
    if problems:
        return {}, problems
    xs = np.concatenate([[0], np.cumsum(widths)]).astype(int)
    ys = np.concatenate([[0], np.cumsum(heights)]).astype(int)
    placements: dict[str, tuple[int, int]] = {}
    for r, row in enumerate(rows):
        for c, ident in enumerate(row):
            if ident is None:
                continue
            placements[ident] = (int(xs[c]), int(ys[r]))
    return placements, problems


def _relative(source: Chunk, target: Chunk, side: str, offset: float) -> tuple[float, float]:
    """Top-left of `target` relative to `source` when it sits on `source`'s `side`."""
    if side == "E":
        return source.width, offset
    if side == "W":
        return -target.width, offset
    if side == "S":
        return offset, source.height
    return offset, -target.height


def place_edges(edges: list[dict[str, Any]], chunks: dict[str, Chunk],
                start: str) -> tuple[dict[str, tuple[float, float]], list[str]]:
    """Breadth-first placement from `start`; a chunk placed twice must land on the same spot."""
    links: dict[str, list[tuple[str, float, float]]] = {}
    for edge in edges:
        dx, dy = _relative(chunks[edge["from"]], chunks[edge["to"]], edge["side"], edge["offset"])
        links.setdefault(edge["from"], []).append((edge["to"], dx, dy))
        links.setdefault(edge["to"], []).append((edge["from"], -dx, -dy))
    placements = {start: (0.0, 0.0)}
    problems: list[str] = []
    queue = deque([start])
    while queue:
        current = queue.popleft()
        cx, cy = placements[current]
        for neighbour, dx, dy in links.get(current, []):
            spot = (cx + dx, cy + dy)
            if neighbour not in placements:
                placements[neighbour] = spot
                queue.append(neighbour)
            elif max(abs(spot[0] - placements[neighbour][0]), abs(spot[1] - placements[neighbour][1])) > EPSILON:
                problems.append(f"chunk {neighbour} is placed at {placements[neighbour]} by one edge and at "
                                f"{spot} by another (via {current})")
    return placements, sorted(set(problems))


def _rect(chunk: Chunk, at: tuple[float, float]) -> tuple[float, float, float, float]:
    return at[0], at[1], at[0] + chunk.width, at[1] + chunk.height


def find_overlaps(placements: dict[str, tuple[float, float]], chunks: dict[str, Chunk]) -> list[str]:
    names = sorted(placements)
    problems = []
    for i, first in enumerate(names):
        a = _rect(chunks[first], placements[first])
        for second in names[i + 1:]:
            b = _rect(chunks[second], placements[second])
            if min(a[2], b[2]) - max(a[0], b[0]) > EPSILON and min(a[3], b[3]) - max(a[1], b[1]) > EPSILON:
                problems.append(f"chunks {first} and {second} overlap")
    return problems


def find_contacts(placements: dict[str, tuple[float, float]],
                  chunks: dict[str, Chunk]) -> list[dict[str, Any]]:
    """Every shared edge segment: chunk `a` on its `side` touches chunk `b` over [start, end)."""
    contacts = []
    names = sorted(placements)
    for first in names:
        a = _rect(chunks[first], placements[first])
        for second in names:
            if second == first:
                continue
            b = _rect(chunks[second], placements[second])
            if abs(a[2] - b[0]) <= EPSILON:  # a.E meets b.W
                start, end = max(a[1], b[1]), min(a[3], b[3])
                if end - start > EPSILON:
                    contacts.append({"a": first, "side": "E", "b": second, "start": start, "end": end})
            if abs(a[3] - b[1]) <= EPSILON:  # a.S meets b.N
                start, end = max(a[0], b[0]), min(a[2], b[2])
                if end - start > EPSILON:
                    contacts.append({"a": first, "side": "S", "b": second, "start": start, "end": end})
    return contacts


def _world_span(socket: Socket, at: tuple[float, float]) -> tuple[float, float]:
    base = at[0] if socket.side in ("N", "S") else at[1]
    return base + socket.offset, base + socket.offset + socket.width


def pair_sockets(contacts: list[dict[str, Any]], placements: dict[str, tuple[float, float]],
                 chunks: dict[str, Chunk]) -> list[tuple[Socket, Socket]]:
    """Match the sockets facing each other across every contact; mark the rest."""
    pairs: list[tuple[Socket, Socket]] = []
    for ident, at in placements.items():
        for side in SIDES:
            for socket in chunks[ident].sockets[side]:
                socket.world = _world_span(socket, at)
    for contact in contacts:
        a, b = chunks[contact["a"]], chunks[contact["b"]]
        side = contact["side"]
        facing = [(socket, b.sockets[OPPOSITE[side]]) for socket in a.sockets[side]] + \
                 [(socket, a.sockets[side]) for socket in b.sockets[OPPOSITE[side]]]
        for socket, others in facing:
            s0, s1 = socket.world
            if s1 <= contact["start"] + EPSILON or s0 >= contact["end"] - EPSILON:
                continue
            if s0 < contact["start"] - EPSILON or s1 > contact["end"] + EPSILON:
                socket.status = "straddle"
                socket.messages.append(f"{socket.key} [{s0:g}, {s1:g}) straddles the end of the shared edge "
                                       f"[{contact['start']:g}, {contact['end']:g})")
                continue
            match = [other for other in others
                     if abs(other.world[0] - s0) <= EPSILON and abs(other.world[1] - s1) <= EPSILON]
            if match:
                partner = match[0]
                socket.partner = (partner.chunk, partner.side, partner.index)
                if socket.status == "unchecked":
                    socket.status = "paired"
                if socket.chunk == contact["a"]:
                    pairs.append((socket, partner))
                continue
            socket.status = "mismatch"
            near = [other for other in others if other.world[0] < s1 and other.world[1] > s0]
            if near:
                other = near[0]
                socket.messages.append(
                    f"{socket.key} [{s0:g}, {s1:g}) width {socket.width:g} does not match {other.key} "
                    f"[{other.world[0]:g}, {other.world[1]:g}) width {other.width:g}")
            else:
                other_chunk = contact["b"] if socket.chunk == contact["a"] else contact["a"]
                socket.messages.append(f"{socket.key} [{s0:g}, {s1:g}) opens into {other_chunk}, which has no "
                                       f"socket there")
    for chunk in chunks.values():
        for side in SIDES:
            for socket in chunk.sockets[side]:
                if socket.status == "unchecked" and socket.world is not None:
                    socket.status = "dangling"
    return pairs


def material_problems(pairs: list[tuple[Socket, Socket]]) -> tuple[list[str], list[str]]:
    failures, warnings = [], []
    for first, second in pairs:
        if first.material and second.material and first.material != second.material:
            failures.append(f"{first.key} is {first.material!r} but {second.key} is {second.material!r}")
            first.status = second.status = "mismatch"
        elif bool(first.material) != bool(second.material):
            warnings.append(f"only one of {first.key} and {second.key} declares a material")
    return failures, warnings


def graph_reachability(placed: list[str], pairs: list[tuple[Socket, Socket]], start: str) -> set[str]:
    links: dict[str, set[str]] = {ident: set() for ident in placed}
    for first, second in pairs:
        if first.status == "paired" and second.status == "paired":
            links[first.chunk].add(second.chunk)
            links[second.chunk].add(first.chunk)
    reached = {start}
    queue = deque([start])
    while queue:
        for neighbour in sorted(links[queue.popleft()]):
            if neighbour not in reached:
                reached.add(neighbour)
                queue.append(neighbour)
    return reached


class _UnionFind:
    def __init__(self, size: int) -> None:
        self.parent = np.arange(size)

    def find(self, item: int) -> int:
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:
            self.parent[item], item = root, self.parent[item]
        return int(root)

    def union(self, first: int, second: int) -> None:
        a, b = self.find(first), self.find(second)
        if a != b:
            self.parent[max(a, b)] = min(a, b)


def _socket_cells(socket: Socket, chunk: Chunk) -> tuple[np.ndarray, np.ndarray]:
    """Grid (row, col) indices of the cells along a socket's span on its own edge."""
    cell = chunk.cell
    start = socket.offset / cell
    end = (socket.offset + socket.width) / cell
    along = np.arange(int(math.floor(start + EPSILON)), int(math.ceil(end - EPSILON)))
    rows, cols = chunk.grid.shape
    along = along[(along >= 0) & (along < (cols if socket.side in ("N", "S") else rows))]
    if socket.side == "N":
        return np.zeros_like(along), along
    if socket.side == "S":
        return np.full_like(along, rows - 1), along
    if socket.side == "W":
        return along, np.zeros_like(along)
    return along, np.full_like(along, cols - 1)


def _local_grid_reachability(placed: list[str], chunks: dict[str, Chunk], placements: dict[str, tuple[float, float]],
                             pairs: list[tuple[Socket, Socket]],
                             start: tuple[str, tuple[float, float] | None]) -> dict[str, Any]:
    """Stitched walkability search: per-chunk 4-connected components joined through paired doors.

    Private on purpose: integration consolidates it into map_nav's grid search (plan B14-T3).
    """
    cells = {chunks[ident].cell for ident in placed}
    if len(cells) != 1:
        return {"status": "fail", "problems": [f"placed chunks use different cell sizes {sorted(cells)}"]}
    cell = cells.pop()
    problems, warnings = [], []
    for ident in placed:
        x, y = placements[ident]
        if abs(x / cell - round(x / cell)) > EPSILON or abs(y / cell - round(y / cell)) > EPSILON:
            problems.append(f"chunk {ident} sits at ({x:g}, {y:g}), off the {cell} px grid")
    if problems:
        return {"status": "fail", "problems": problems}
    labels, offsets, total = {}, {}, 0
    for ident in placed:
        label, count = forge_core.label_components(chunks[ident].grid, connectivity=4)
        labels[ident], offsets[ident] = label, total
        total += count
    if total == 0:
        return {"status": "fail", "problems": ["no chunk has a walkable cell"]}
    union = _UnionFind(total + 1)
    doors = []
    for first, second in pairs:
        if first.status != "paired" or second.status != "paired":
            continue
        rows_a, cols_a = _socket_cells(first, chunks[first.chunk])
        rows_b, cols_b = _socket_cells(second, chunks[second.chunk])
        label_a = labels[first.chunk][rows_a, cols_a]
        label_b = labels[second.chunk][rows_b, cols_b]
        count = min(len(label_a), len(label_b))
        open_cells = (label_a[:count] > 0) & (label_b[:count] > 0)
        for a, b in zip(label_a[:count][open_cells], label_b[:count][open_cells]):
            union.union(offsets[first.chunk] + int(a), offsets[second.chunk] + int(b))
        if not open_cells.any():
            problems.append(f"door {first.key} <-> {second.key} opens into a wall on "
                            f"{'both sides' if not (label_a > 0).any() and not (label_b > 0).any() else 'one side'}")
        doors.append((first, second, label_a, label_b))
    start_chunk, point = start
    grid = chunks[start_chunk].grid
    if point is not None:
        row, col = int(point[1] // cell), int(point[0] // cell)
        if not (0 <= row < grid.shape[0] and 0 <= col < grid.shape[1]) or not grid[row, col]:
            return {"status": "fail", "problems": [f"start point {point} in chunk {start_chunk} is not on a "
                                                   f"walkable cell"]}
        start_label = int(labels[start_chunk][row, col])
    else:
        sizes = np.bincount(labels[start_chunk].ravel())
        sizes[0] = 0
        if not sizes.any():
            return {"status": "fail", "problems": [f"start chunk {start_chunk} has no walkable cell"]}
        start_label = int(np.argmax(sizes))
    root = union.find(offsets[start_chunk] + start_label)
    roots = np.array([union.find(item) for item in range(total + 1)])
    per_chunk = {}
    for ident in placed:
        label = labels[ident]
        reached_mask = (label > 0) & (roots[offsets[ident] + label] == root)
        walkable = int((label > 0).sum())
        reached = int(reached_mask.sum())
        per_chunk[ident] = {"walkable_cells": walkable, "reached_cells": reached}
        if reached == 0:
            problems.append(f"chunk {ident} cannot be reached on foot from {start_chunk}")
        elif reached < walkable:
            warnings.append(f"chunk {ident}: {walkable - reached} walkable cells cannot be reached (islands)")
    for first, second, label_a, label_b in doors:
        hit = (label_a > 0) & (roots[offsets[first.chunk] + label_a] == root)
        if not hit.any() and not any(p.startswith(f"door {first.key}") for p in problems):
            problems.append(f"door {first.key} <-> {second.key} cannot be reached on foot")
    warnings += _open_edges_without_sockets(placed, chunks, placements)
    return {"status": "fail" if problems else ("warn" if warnings else "pass"), "cell": cell,
            "start": {"chunk": start_chunk, "component_cells": int(sum(item["reached_cells"]
                                                                        for item in per_chunk.values()))},
            "chunks": per_chunk, "problems": problems, "warnings": warnings,
            "_reached": {ident: (labels[ident] > 0) & (roots[offsets[ident] + labels[ident]] == root)
                         for ident in placed}}


def _open_edges_without_sockets(placed: list[str], chunks: dict[str, Chunk],
                                placements: dict[str, tuple[float, float]]) -> list[str]:
    """Walkable edge cells no door covers: they meet a touching chunk's walkable cells (an undeclared door)
    or face no chunk at all (a way off the map)."""
    warnings = []
    contacts = find_contacts({ident: placements[ident] for ident in placed}, chunks)
    for ident in placed:
        chunk = chunks[ident]
        cell = chunk.cell
        x, y = placements[ident]
        for side in SIDES:
            if side in ("N", "S"):
                edge = chunk.grid[0 if side == "N" else -1, :]
                coords = x + (np.arange(edge.size) + 0.5) * cell
            else:
                edge = chunk.grid[:, 0 if side == "W" else -1]
                coords = y + (np.arange(edge.size) + 0.5) * cell
            base = x if side in ("N", "S") else y
            covered = np.zeros(edge.size, bool)
            for socket in chunk.sockets[side]:
                covered |= (coords >= base + socket.offset) & (coords < base + socket.offset + socket.width)
            facing_walkable = np.zeros(edge.size, bool)
            facing_any = np.zeros(edge.size, bool)
            for contact in contacts:
                if (contact["a"], contact["side"]) == (ident, side):
                    other, other_side = chunks[contact["b"]], OPPOSITE[side]
                elif (contact["b"], OPPOSITE[contact["side"]]) == (ident, side):
                    other, other_side = chunks[contact["a"]], contact["side"]
                else:
                    continue
                inside = (coords >= contact["start"]) & (coords < contact["end"])
                facing_any |= inside
                ox, oy = placements[other.id]
                along = ((coords[inside] - (ox if side in ("N", "S") else oy)) // cell).astype(int)
                if other_side in ("N", "S"):
                    facing = other.grid[0 if other_side == "N" else -1, along]
                else:
                    facing = other.grid[along, 0 if other_side == "W" else -1]
                facing_walkable[np.flatnonzero(inside)] = facing
            leaks = int((edge & ~covered & facing_walkable).sum())
            void = int((edge & ~covered & ~facing_any).sum())
            if leaks:
                warnings.append(f"{ident}.{side} has {leaks} open cells meeting open cells of the next chunk "
                                f"without a socket (a door the data does not declare)")
            if void:
                warnings.append(f"{ident}.{side} has {void} walkable cells on the outer edge without a socket "
                                f"(a way off the map)")
    return warnings


def kit_partner_warnings(chunks: list[Chunk]) -> list[str]:
    """For a kit without a layout: each socket should fit some socket on the opposite side of some chunk."""
    warnings = []
    for chunk in chunks:
        for side in SIDES:
            for socket in chunk.sockets[side]:
                fits = any(abs(other.width - socket.width) <= EPSILON
                           and (not socket.material or not other.material or socket.material == other.material)
                           for candidate in chunks for other in candidate.sockets[OPPOSITE[side]])
                if not fits:
                    warnings.append(f"{socket.key} (width {socket.width:g}, {socket.material or 'no material'}) has "
                                    f"no partner on any {OPPOSITE[side]} edge in the kit")
    return warnings


# --------------------------------------------------------------------------- validation

def validate(document: Any, start_override: str | None = None) -> dict[str, Any]:
    """Run every check and return the report (without file references)."""
    definitions = parse_chunks(document)
    chunks = {chunk.id: chunk for chunk in definitions}
    mode, rows, edges = parse_graph(document, set(chunks))
    checks = check_socket_geometry(definitions)
    referenced: set[str] = set()
    used: list[str] = []
    if mode == "rows":
        referenced = {ident for row in rows for ident in row if ident}
        rows, instances = instance_rows(rows, chunks)
        chunks = {**{ident: chunk for ident, chunk in chunks.items() if ident not in referenced}, **instances}
        used = list(dict.fromkeys(ident for row in rows for ident in row if ident))
    elif mode == "edges":
        used = list(dict.fromkeys(name for edge in edges for name in (edge["from"], edge["to"])))
        referenced = set(used)
    chunk_list = list(chunks.values())
    requested = start_override if start_override is not None else document.get("start")
    named = requested.get("chunk") if isinstance(requested, dict) else str(requested or "").split(":")[0].strip()
    if mode == "rows" and named in referenced and named not in chunks:
        copies = sorted(ident for ident in chunks if ident.startswith(f"{named}@"))
        raise ValueError(f"chunk {named} is placed {len(copies)} times; start at one of {', '.join(copies)}.")
    start = parse_start(requested, set(chunks))
    if start is None and used:
        start = (used[0], None)
    if start is not None and used and start[0] not in used:
        raise ValueError(f"start chunk {start[0]!r} is not part of the graph.")
    report: dict[str, Any] = {"mode": mode, "start": None if start is None else
                              {"chunk": start[0], "point": None if start[1] is None else list(start[1])},
                              "unused": sorted({chunk.id for chunk in definitions} - referenced)}
    if mode == "kit":
        warnings = kit_partner_warnings(chunk_list)
        checks.append(_check("kit_partners", "warn" if warnings else "pass", warnings))
        report.update(placements=[], unplaced=[], contacts=[], sockets=_socket_rows(chunk_list), reachability=None)
        return _finish(report, checks)
    if mode == "rows":
        placements, problems = place_rows(rows, chunks)
    else:
        placements, problems = place_edges(edges, chunks, start[0])
    unplaced = [ident for ident in used if ident not in placements]
    if mode == "edges" and unplaced:
        problems.append(f"chunks {unplaced} are not connected to start chunk {start[0]} by any edge")
    overlaps = find_overlaps(placements, chunks)
    checks.append(_check("placement", "fail" if problems or overlaps else "pass", problems + overlaps))
    contacts = find_contacts(placements, chunks)
    pairs = pair_sockets(contacts, placements, chunks)
    bad = [message for chunk in chunk_list for side in SIDES for socket in chunk.sockets[side]
           for message in socket.messages]
    checks.append(_check("socket_pairs", "fail" if bad else "pass", bad))
    material_failures, material_warnings = material_problems(pairs)
    checks.append(_check("socket_materials", "fail" if material_failures else ("warn" if material_warnings else "pass"),
                         material_failures + material_warnings))
    dangling = [f"{socket.key} [{socket.world[0]:g}, {socket.world[1]:g}) touches no chunk"
                for chunk in chunk_list for side in SIDES for socket in chunk.sockets[side]
                if socket.status == "dangling"]
    checks.append(_check("dangling_sockets", "warn" if dangling else "pass", dangling))
    if mode == "edges":
        doorless = []
        for edge in edges:
            if edge["from"] not in placements or edge["to"] not in placements:
                continue
            linked = any(first.chunk in (edge["from"], edge["to"]) and second.chunk in (edge["from"], edge["to"])
                         and first.status == "paired" for first, second in pairs)
            if not linked:
                doorless.append(f"edge {edge['from']}-{edge['side']}-{edge['to']} has no compatible door between "
                                f"the chunks")
        checks.append(_check("edges_connected", "fail" if doorless else "pass", doorless))
    placed = [ident for ident in used if ident in placements]
    reached = graph_reachability(placed, pairs, start[0]) if start[0] in placements else set()
    unreached = [ident for ident in used if ident not in reached]
    checks.append(_check("graph_reachability", "fail" if unreached else "pass",
                         {"start": start[0], "reached": sorted(reached), "unreached": unreached}))
    grid = None
    if placed and all(chunks[ident].grid is not None for ident in placed) and start[0] in placements:
        grid = _local_grid_reachability(placed, chunks, placements, pairs, start)
        checks.append(_check("grid_reachability", grid["status"],
                             {"problems": grid["problems"], "warnings": grid.get("warnings", [])}))
    else:
        missing = [ident for ident in placed if chunks[ident].grid is None]
        checks.append(_check("grid_reachability", "skipped",
                             f"no walkability grid on {missing}" if missing else "nothing placed"))
    report.update(
        placements=[{"id": ident, "rect": [placements[ident][0], placements[ident][1],
                                           chunks[ident].width, chunks[ident].height]} for ident in placed],
        unplaced=unplaced,
        contacts=[{**contact, "b_side": OPPOSITE[contact["side"]]} for contact in contacts],
        sockets=_socket_rows(chunk_list),
        reachability={"graph": {"reached": sorted(reached), "unreached": unreached},
                      "grid": None if grid is None else {key: value for key, value in grid.items()
                                                         if not key.startswith("_")}})
    report["_placements"] = placements
    report["_chunks"] = chunks
    report["_grid_reached"] = None if grid is None else grid.get("_reached")
    return _finish(report, checks)


def _socket_rows(chunk_list: list[Chunk]) -> list[dict[str, Any]]:
    rows = []
    for chunk in chunk_list:
        for side in SIDES:
            for socket in chunk.sockets[side]:
                rows.append({"chunk": chunk.id, "side": side, "index": socket.index, "offset": socket.offset,
                             "width": socket.width, "material": socket.material,
                             "status": "unplaced" if socket.world is None else socket.status,
                             "world_span": None if socket.world is None else list(socket.world),
                             "partner": None if socket.partner is None else
                             {"chunk": socket.partner[0], "side": socket.partner[1], "index": socket.partner[2]}})
    return rows


def _finish(report: dict[str, Any], checks: list[dict[str, Any]]) -> dict[str, Any]:
    statuses = {check["status"] for check in checks}
    report["status"] = "fail" if "fail" in statuses else ("warn" if "warn" in statuses else "pass")
    report["checks"] = checks
    return report


def messages_of(report: dict[str, Any], status: str = "fail", limit: int = 8) -> list[str]:
    """The first messages of checks with `status` (fail or warn), for the console summary."""
    found = []
    for check in report["checks"]:
        if check["status"] != status:
            continue
        value = check["value"]
        if isinstance(value, str):
            items = [value]
        elif isinstance(value, list):
            items = [item for item in value if isinstance(item, str)]
        elif isinstance(value, dict):
            keys = ("problems",) if status == "fail" else ("warnings", "impassable")
            items = [item for key in keys for item in value.get(key) or []]
            if value.get("unreached"):
                items.append(f"unreachable chunks {value['unreached']}")
        else:
            items = []
        found += [f"{check['id']}: {item}" for item in items]
    return [forge_core.ascii_text(item) for item in found[:limit]]


# --------------------------------------------------------------------------- debug image

def render_debug(report: dict[str, Any]) -> Image.Image:
    """Placed chunks, walkability (blocked dark, reached green, unreached orange) and doors by status."""
    placements: dict[str, tuple[float, float]] = report["_placements"]
    chunks: dict[str, Chunk] = report["_chunks"]
    reached_masks = report["_grid_reached"]
    rects = {ident: _rect(chunks[ident], at) for ident, at in placements.items()}
    x0 = min(rect[0] for rect in rects.values())
    y0 = min(rect[1] for rect in rects.values())
    x1 = max(rect[2] for rect in rects.values())
    y1 = max(rect[3] for rect in rects.values())
    extent = max(x1 - x0, y1 - y0)
    scale = 1024 / extent if extent > 1024 else float(max(1, 768 // max(1, int(extent))))
    pad = 16
    width, height = int(math.ceil((x1 - x0) * scale)) + 2 * pad, int(math.ceil((y1 - y0) * scale)) + 2 * pad
    image = Image.new("RGBA", (width, height), (30, 32, 38, 255))

    def to_px(x: float, y: float) -> tuple[float, float]:
        return pad + (x - x0) * scale, pad + (y - y0) * scale

    draw = ImageDraw.Draw(image)
    for number, ident in enumerate(sorted(placements)):
        rect = rects[ident]
        draw.rectangle([*to_px(rect[0], rect[1]), to_px(rect[2], rect[3])[0] - 1, to_px(rect[2], rect[3])[1] - 1],
                       fill=CHUNK_COLORS[number % len(CHUNK_COLORS)] + (255,), outline=(230, 232, 236, 255))
    if reached_masks:
        for ident, reached in reached_masks.items():
            chunk = chunks[ident]
            overlay = np.zeros(chunk.grid.shape + (4,), np.uint8)
            overlay[~chunk.grid] = (18, 18, 22, 210)
            overlay[chunk.grid & reached] = (90, 210, 120, 110)
            overlay[chunk.grid & ~reached] = (245, 150, 50, 170)
            left, top = to_px(*placements[ident])
            size = (max(1, int(round(chunk.width * scale))), max(1, int(round(chunk.height * scale))))
            tile = Image.fromarray(overlay).resize(size, Image.Resampling.NEAREST)
            image.alpha_composite(tile, (int(round(left)), int(round(top))))
        draw = ImageDraw.Draw(image)
    thickness = max(3, int(round(3 * min(scale, 3))))
    for ident in sorted(placements):
        chunk = chunks[ident]
        for side in SIDES:
            for socket in chunk.sockets[side]:
                if socket.world is None:
                    continue
                color = SOCKET_COLORS.get(socket.status, (200, 200, 200)) + (255,)
                rect = rects[ident]
                s0, s1 = socket.world
                if side == "N":
                    points = [to_px(s0, rect[1]), to_px(s1, rect[1])]
                elif side == "S":
                    points = [to_px(s0, rect[3]), to_px(s1, rect[3])]
                elif side == "W":
                    points = [to_px(rect[0], s0), to_px(rect[0], s1)]
                else:
                    points = [to_px(rect[2], s0), to_px(rect[2], s1)]
                draw.line(points, fill=color, width=thickness)
    return image


# --------------------------------------------------------------------------- CLI

def _local_file_ref(path: Path, base_dir: Path) -> dict[str, Any]:
    """Manifest-relative POSIX path, or only the file name when there is no relative route (another drive)."""
    relative = forge_core.portable_path(path, base_dir)
    if relative.startswith("/") or re.match(r"^[A-Za-z]:", relative):
        relative = Path(path).name
    return {"path": relative, "sha256": forge_core.sha256_file(path), "bytes": Path(path).stat().st_size}


def _public(report: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in report.items() if not key.startswith("_")}


def run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    document = json.loads(Path(args.chunks).read_text(encoding="utf-8"))
    report = validate(document, args.start)
    failed = [check["id"] for check in report["checks"] if check["status"] == "fail"]
    summary: dict[str, Any] = {"status": report["status"], "mode": report["mode"],
                               "chunks": len(document["chunks"]), "placed": len(report["placements"]),
                               "failed": failed, "problems": messages_of(report),
                               "warnings": messages_of(report, "warn")}
    if args.output_dir is None:
        return summary, 1 if failed else 0
    if args.strict_qc and failed:
        raise ValueError(f"strict QC failed ({', '.join(failed)}); nothing was written.")
    final = Path(args.output_dir)
    with forge_core.staged_output(final) as stage:
        outputs = []
        if report["placements"]:
            forge_core.save_png(render_debug(report), stage / "chunk-debug.png")
            outputs.append(_local_file_ref(stage / "chunk-debug.png", stage))
        checks = report["checks"]
        document_out = {"schema": REPORT_SCHEMA, "tool": dict(TOOL), **_public(report),
                        "qa": {"status": report["status"],
                               "method": "validate_chunks: socket spans on placed chunk edges compared exactly "
                                         "(offset, width, material); chunk graph search through paired doors; "
                                         "with walkability grids, 4-connected components joined through doors.",
                               "notProven": list(NOT_PROVEN), "checks": checks,
                               "inputs": [_local_file_ref(Path(args.chunks), final)], "outputs": outputs,
                               "tool": dict(TOOL)}}
        document_out.pop("checks")
        forge_core.write_json(stage / "chunk-report.json", document_out)
    summary.update(output_dir=str(final.resolve()), report=str((final / "chunk-report.json").resolve()),
                   metadata=str((final / "chunk-report.json").resolve()))
    if report["placements"]:
        summary["debug"] = str((final / "chunk-debug.png").resolve())
    return summary, 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--chunks", type=Path, required=True,
                        help="room_chunk.v1 JSON (schema generate2dmap.room_chunk.v1).")
    parser.add_argument("--start", help="Start chunk or instance, optionally with a point in its pixels: a, "
                                        "a:40,90 or corridor@0,1 (default: the file's start, else the first "
                                        "chunk of the graph).")
    parser.add_argument("--output-dir", type=Path,
                        help="New folder for chunk-report.json and chunk-debug.png; must not exist. "
                             "Omit to print the summary only.")
    parser.add_argument("--strict-qc", action="store_true",
                        help="With --output-dir: publish nothing when a check fails.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    forge_core.utf8_stdio()
    args = build_parser().parse_args(argv)
    try:
        summary, code = run(args)
    except (ValueError, OSError, Image.DecompressionBombError) as error:
        print(f"error: {forge_core.ascii_text(str(error) or type(error).__name__)}", file=sys.stderr)
        return 1
    if code:
        print("error: chunk validation failed: " + "; ".join(summary["problems"][:3] or summary["failed"]),
              file=sys.stderr)
    print(json.dumps(summary, ensure_ascii=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
