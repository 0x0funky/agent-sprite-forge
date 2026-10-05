#!/usr/bin/env python3
"""Build a single-file playable preview of a map_bundle.v2 scene (preview.html).

The page embeds every image as a data URI and inlines
references/runtime/map-runtime.mjs verbatim, so it opens straight from disk
with no server and no network. Image layers are drawn at the world origin,
tile layers are pre-rendered from their tileset manifests, and objects plus a
debug actor are Y-sorted by (sortY, x, id) with the actor in front on ties.
The actor walks with the runtime's Appendix C collision (keyboard, or click to
walk). The Debug toggle draws walk regions, solids, footprints, blocked
material cells, portals with their zones, spawns, interactions, anchors and
the path; window.__scene exposes a snapshot and a route check that walks from
the spawns to every exit, interaction, approach point and slot.

Outputs, in a new --output-dir that is staged and published only after QA:
  preview.html     the page (ASCII, deterministic for the same inputs)
  preview-qa.json  QA envelope: inputs and outputs with sha256, checks, warnings
With --verify, when node and the playwright npm package can open headless
Chromium: scene-snapshot.json, preview-screen.png and preview-debug.png.
"""

from __future__ import annotations

import argparse
import base64
import html
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, NoReturn

import numpy as np
from PIL import Image

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import forge_core  # noqa: E402  (this skill's vendored copy)


TOOL = {"name": "build_scene_preview", "version": "1.0"}
REPORT_SCHEMA = "generate2dmap.scene_preview_qa.v1"
RUNTIME_PATH = Path(__file__).resolve().parents[1] / "references" / "runtime" / "map-runtime.mjs"
BUNDLE_SCHEMAS = ("generate2dmap.map_bundle.v1", "generate2dmap.map_bundle.v2")
TILESET_SCHEMA = "generate2dmap.tileset.v1"
PREVIEW_NAME = "preview.html"
REPORT_NAME = "preview-qa.json"
SNAPSHOT_NAME = "scene-snapshot.json"
SCREEN_NAME = "preview-screen.png"
DEBUG_NAME = "preview-debug.png"
DEFAULT_MAX_BYTES = 16_000_000
MAX_ZOOM = 8
AUTO_ZOOM_SPAN = 1280
EMBED_FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}
MATERIAL_CLASSES = ("solid", "one_way", "liquid", "hazard", "decor")
ACTIVATIONS = ("crossing", "intent")
FOOTPRINT_SHAPES = ("ellipse", "rect", "none")
IMAGE_BASES = ("image_px", "prop_px", "world_px")

NOT_PROVEN = [
    "Collision parity with map_nav.py: this build runs no collision code; integration tests the JS and Python queries "
    "against each other.",
    "Without --verify, or when it prints SKIPPED, the page was assembled and scanned but never run in a browser.",
    "The actor is a debug marker sized from collision.actorRadius, not character art; animation, lights, atmosphere, "
    "stage data, camera bounds and occluder masks are not drawn.",
    "Draw order is whole-object Y-sort by (sortY, x, id); per-pixel occlusion, fades and cut-aways are not simulated.",
    "Route checks walk the Appendix C grid with the runtime walker at --speed; an engine controller (acceleration, "
    "physics colliders, other speeds) may behave differently.",
    "Validity is sampled (Appendix C): a solid thinner than half a nav cell can be crossed. Tile collision shapes and "
    "tileset walkable flags are not read; walls must be in collision or material_map.",
    "Only headless Chromium is exercised (with --verify); other browsers, devices and touch input are not tested.",
]

# --------------------------------------------------------------------------- page template

_STYLE = """\
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin: 0; background: #12171c; color: #dfe7ec; font: 14px/1.45 system-ui, sans-serif; }
header { max-width: 1320px; margin: 0 auto; padding: 14px 16px 8px; display: flex; flex-wrap: wrap;
  gap: 10px 24px; align-items: flex-end; justify-content: space-between; }
h1 { font-size: 20px; margin: 0; font-weight: 600; }
.sub { margin: 2px 0 0; color: #9fb0bb; font-size: 13px; }
nav { display: flex; flex-wrap: wrap; gap: 8px 14px; align-items: center; }
label { cursor: pointer; user-select: none; }
button, select { background: #24313b; color: inherit; border: 1px solid #3c5262; border-radius: 4px;
  padding: 4px 10px; font: inherit; cursor: pointer; }
main { max-width: 1320px; margin: 0 auto; padding: 0 16px 24px; }
canvas { display: block; max-width: 100%; height: auto; image-rendering: pixelated; border: 1px solid #33414c;
  background: #0b0f12; outline: none; touch-action: none; }
canvas:focus { border-color: #6fa8dc; }
footer { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 6px 20px; margin-top: 8px;
  color: #9fb0bb; font-size: 13px; }
#status { color: #dfe7ec; font-family: ui-monospace, monospace; }
details { margin-top: 10px; }
textarea { width: 100%; height: 260px; background: #0b0f12; color: #cfe3ee; border: 1px solid #33414c;
  font: 12px ui-monospace, monospace; }
"""

_PAGE_SCRIPT = r"""
// ---- scene preview (build_scene_preview.py); the runtime above is map-runtime.mjs, inlined verbatim ----
const SCENE = @@SCENE@@;
const IMAGE_DATA = @@IMAGES@@;

const view = document.getElementById("view");
const ctx = view.getContext("2d");
const statusLine = document.getElementById("status");
const W = SCENE.world.width, H = SCENE.world.height, Z = SCENE.zoom;
const BUDGET = SCENE.speed / TICK_HZ;
const KEY_DIRECTIONS = {
  arrowup: [0, -1], w: [0, -1], arrowdown: [0, 1], s: [0, 1],
  arrowleft: [-1, 0], a: [-1, 0], arrowright: [1, 0], d: [1, 0],
};
const MAX_EVENTS = 200;
const sortKeys = SCENE.objects.map((object) => object.sortY);
const images = {};
const alphaCache = {};
const state = {
  ready: false, error: null, tick: 0, debug: false, grid: false, props: true, actorVisible: true,
  keys: new Set(), events: [], exitsFired: [], reached: new Set(), routes: null, message: "",
  waiters: [], assets: {total: 0, loaded: 0, failed: []}, gridCanvas: null, materialCanvas: null,
};
let world = null, actor = null, startSpawn = null;

view.width = Math.ceil(W * Z);
view.height = Math.ceil(H * Z);

function control(id) { return document.getElementById(id); }

function record(event) {
  state.events.push({tick: state.tick, ...event});
  if (state.events.length > MAX_EVENTS) state.events.splice(0, state.events.length - MAX_EVENTS);
}

function say(message) { state.message = message; }

function fail(error) {
  state.error = String((error && error.message) || error);
  statusLine.textContent = "error: " + state.error;
}

function needReady() {
  if (!state.ready) throw new Error(state.error || "the scene is still loading");
}

// ---- simulation ----

function keyIntent() {
  let x = 0, y = 0;
  for (const key of state.keys) {
    const direction = KEY_DIRECTIONS[key];
    if (direction) {
      x += direction[0];
      y += direction[1];
    }
  }
  return {x: Math.sign(x), y: Math.sign(y)};
}

function exitFired(portal) {
  record({type: "exit", id: portal.id, to: portal.to});
  if (!state.exitsFired.includes(portal.id)) state.exitsFired.push(portal.id);
  state.keys.clear();
  actor.path = null;
  const back = returnSpawn(world, portal);
  if (back) {
    arrive(world, actor, back.x, back.y);
    record({type: "arrive", id: back.id, via: portal.id});
    say(`exit ${portal.id} -> ${portal.to}; back through spawn ${back.id}`);
  } else {
    say(`exit ${portal.id} -> ${portal.to}; no entrance back from ${portal.toMap || "there"}`);
  }
}

function tick() {
  const intent = keyIntent();
  const step = stepActor(world, actor, intent.x || intent.y ? intent : null, BUDGET);
  state.tick += 1;
  if (step.fired) exitFired(step.fired);
  for (const item of world.interactions) {
    if (state.reached.has(item.id)) continue;
    const dx = actor.x - item.x, dy = actor.y - item.y;
    if (Math.sqrt(dx * dx + dy * dy) <= item.reach) {
      state.reached.add(item.id);
      record({type: "reach", id: item.id});
    }
  }
  if (state.waiters.length) {
    const due = state.waiters.filter((waiter) => waiter.tick <= state.tick);
    state.waiters = state.waiters.filter((waiter) => waiter.tick > state.tick);
    for (const waiter of due) waiter.resolve(state.tick);
  }
}

function interact() {
  let best = null, bestDistance = Infinity;
  for (const item of world.interactions) {
    const distance = Math.hypot(actor.x - item.x, actor.y - item.y);
    if (distance <= item.reach && distance < bestDistance) {
      best = item;
      bestDistance = distance;
    }
  }
  if (best) {
    record({type: "interact", id: best.id});
    say(`interact ${best.id}`);
  } else {
    say("nothing in reach");
  }
}

// ---- drawing ----

function tracePolygon(shape) {
  ctx.beginPath();
  ctx.moveTo(shape.xs[0], shape.ys[0]);
  for (let k = 1; k < shape.xs.length; k++) ctx.lineTo(shape.xs[k], shape.ys[k]);
  ctx.closePath();
}

function traceShape(shape) {
  if (shape.kind === "rect") {
    ctx.beginPath();
    ctx.rect(shape.x, shape.y, shape.x1 - shape.x, shape.y1 - shape.y);
  } else if (shape.kind === "ellipse") {
    ctx.beginPath();
    ctx.ellipse(shape.cx, shape.cy, Math.max(shape.rx, 0), Math.max(shape.ry, 0), shape.rotate * Math.PI / 180, 0, Math.PI * 2);
  } else {
    tracePolygon(shape);
  }
}

function dot(x, y, radius, color) {
  ctx.beginPath();
  ctx.arc(x, y, radius, 0, Math.PI * 2);
  ctx.fillStyle = color;
  ctx.fill();
}

function square(x, y, half, color) {
  ctx.fillStyle = color;
  ctx.fillRect(x - half, y - half, 2 * half, 2 * half);
}

function ring(x, y, radius, color) {
  ctx.setLineDash([4 / Z, 3 / Z]);
  ctx.beginPath();
  ctx.arc(x, y, radius, 0, Math.PI * 2);
  ctx.strokeStyle = color;
  ctx.stroke();
  ctx.setLineDash([]);
}

function roundedRect(x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

function arrow(x, y, dx, dy, length, color) {
  const tx = x + dx * length, ty = y + dy * length, head = Math.max(3 / Z, length * 0.3);
  ctx.beginPath();
  ctx.moveTo(x, y);
  ctx.lineTo(tx, ty);
  ctx.lineTo(tx - head * (dx - dy * 0.5), ty - head * (dy + dx * 0.5));
  ctx.moveTo(tx, ty);
  ctx.lineTo(tx - head * (dx + dy * 0.5), ty - head * (dy - dx * 0.5));
  ctx.strokeStyle = color;
  ctx.stroke();
}

function drawPortal(portal) {
  ctx.beginPath();
  if (portal.kind === "rect") ctx.rect(portal.x, portal.y, portal.x1 - portal.x, portal.y1 - portal.y);
  else ctx.arc(portal.cx, portal.cy, portal.r, 0, Math.PI * 2);
  ctx.fillStyle = "rgba(255, 159, 67, 0.22)";
  ctx.strokeStyle = "#ff9f43";
  ctx.fill();
  ctx.stroke();
  if (portal.activation === "intent" && portal.radius > 0) {
    ctx.setLineDash([4 / Z, 3 / Z]);
    const r = portal.radius;
    if (portal.kind === "rect") {
      roundedRect(portal.x - r, portal.y - r, portal.x1 - portal.x + 2 * r, portal.y1 - portal.y + 2 * r, r);
    } else {
      ctx.beginPath();
      ctx.arc(portal.cx, portal.cy, portal.r + r, 0, Math.PI * 2);
    }
    ctx.stroke();
    ctx.setLineDash([]);
  }
  if (portal.hasDirection) arrow(portal.cx, portal.cy, portal.dirX, portal.dirY, Math.max(10, portal.radius), "#ff9f43");
}

function drawObject(object) {
  const image = object.image === null ? null : images[object.image];
  if (image) {
    const s = object.scale;
    ctx.drawImage(image, Math.round(object.x - object.anchor[0] * s), Math.round(object.y - object.anchor[1] * s),
      image.width * s, image.height * s);
  } else {
    ctx.fillStyle = "rgba(255, 0, 255, 0.85)";
    ctx.fillRect(object.x - 2, object.y - 12, 4, 12);
  }
}

function drawActor() {
  const r = world.actorRadius, ry = world.footprintRy, h = SCENE.actor.height;
  const bw = Math.max(2, Math.round(r * 1.2)), x = actor.x, y = actor.y;
  ctx.beginPath();
  ctx.ellipse(x, y, Math.max(r, 1), Math.max(ry, 0.5), 0, 0, Math.PI * 2);
  ctx.fillStyle = "rgba(0, 0, 0, 0.35)";
  ctx.fill();
  ctx.fillStyle = "#1b2a33";
  ctx.fillRect(x - bw / 2 - 1, y - h - 1, bw + 2, h - 1);
  ctx.fillStyle = "#f2f5ea";
  ctx.fillRect(x - bw / 2, y - h, bw, h - 3);
  ctx.fillStyle = "#e0623a";
  ctx.fillRect(x + actor.facingX * bw / 2 - 1, y - h * 0.8 + actor.facingY * 2 - 1, 2, 2);
}

function drawObjects() {
  const at = state.actorVisible ? actorDrawIndex(sortKeys, actor.y) : -1;
  for (let k = 0; k <= SCENE.objects.length; k++) {
    if (k === at) drawActor();
    if (k < SCENE.objects.length && state.props) drawObject(SCENE.objects[k]);
  }
}

function buildMaterialCanvas() {
  const grid = world.material;
  if (grid === null) return null;
  const canvas = document.createElement("canvas");
  canvas.width = grid.width;
  canvas.height = grid.height;
  const g = canvas.getContext("2d"), image = g.createImageData(grid.width, grid.height);
  for (let k = 0; k < grid.width * grid.height; k++) {
    if ((grid.bytes[k >> 3] >> (k & 7)) & 1) image.data.set([52, 152, 219, 150], k * 4);
  }
  g.putImageData(image, 0, 0);
  return canvas;
}

function buildGridCanvas() {
  const field = flood(world, actor.x, actor.y), nav = field.nav;
  const canvas = document.createElement("canvas");
  canvas.width = nav.cols;
  canvas.height = nav.rows;
  const g = canvas.getContext("2d"), image = g.createImageData(nav.cols, nav.rows);
  for (let k = 0; k < nav.cols * nav.rows; k++) {
    if (field.dist[k] >= 0) image.data.set([62, 224, 122, 90], k * 4);
    else if (cellValid(world, k)) image.data.set([244, 208, 63, 90], k * 4);
    else image.data.set([255, 59, 48, 70], k * 4);
  }
  g.putImageData(image, 0, 0);
  return {canvas, width: nav.cols * nav.cell, height: nav.rows * nav.cell};
}

function drawDebug() {
  ctx.lineWidth = 1.5 / Z;
  if (state.materialCanvas) ctx.drawImage(state.materialCanvas, 0, 0, W, H);
  if (state.grid && state.gridCanvas) ctx.drawImage(state.gridCanvas.canvas, 0, 0, state.gridCanvas.width, state.gridCanvas.height);
  ctx.strokeStyle = "#3ee07a";
  if (!world.walkRegions.length) ctx.strokeRect(0, 0, W, H);
  for (const region of world.walkRegions) {
    tracePolygon(region.outer);
    ctx.strokeStyle = "#3ee07a";
    ctx.stroke();
    for (const hole of region.holes) {
      tracePolygon(hole);
      ctx.strokeStyle = "#f4d03f";
      ctx.stroke();
    }
  }
  for (const shape of world.solids) {
    traceShape(shape);
    const prop = shape.objectId !== undefined;
    ctx.fillStyle = prop ? "rgba(255, 77, 255, 0.30)" : "rgba(231, 76, 60, 0.30)";
    ctx.strokeStyle = prop ? "#ff4dff" : "#e74c3c";
    ctx.fill();
    ctx.stroke();
  }
  for (const portal of world.portals) drawPortal(portal);
  for (const spawn of world.spawns) dot(spawn.x, spawn.y, 3.5 / Z, "#4aa3ff");
  for (const item of world.interactions) {
    ring(item.x, item.y, item.reach, "#1abc9c");
    dot(item.x, item.y, 3 / Z, "#1abc9c");
  }
  for (const anchor of world.anchors) {
    dot(anchor.point[0], anchor.point[1], 3.5 / Z, "#b36bff");
    for (const [x, y] of anchor.slots) square(x, y, 2.5 / Z, "#b36bff");
    for (const [x, y] of anchor.approach) square(x, y, 2.5 / Z, "#f4d03f");
  }
  if (actor.path) {
    ctx.beginPath();
    ctx.moveTo(actor.x, actor.y);
    for (const point of actor.path) ctx.lineTo(point.x, point.y);
    ctx.strokeStyle = "#ffe066";
    ctx.stroke();
  }
  for (const [x, y] of footprintSamples(world, actor.x, actor.y)) {
    dot(x, y, 1.6 / Z, pointFree(world, x, y) ? "#3ee07a" : "#ff3b30");
  }
}

function drawLabels() {
  ctx.font = "11px ui-monospace, monospace";
  ctx.lineWidth = 3;
  ctx.strokeStyle = "rgba(0, 0, 0, 0.8)";
  ctx.fillStyle = "#ffffff";
  const label = (text, x, y) => {
    ctx.strokeText(text, x * Z + 5, y * Z - 5);
    ctx.fillText(text, x * Z + 5, y * Z - 5);
  };
  for (const spawn of world.spawns) label(spawn.id, spawn.x, spawn.y);
  for (const portal of world.portals) label(`${portal.id} -> ${portal.to}`, portal.cx, portal.cy);
  for (const item of world.interactions) label(item.id, item.x, item.y);
  for (const anchor of world.anchors) label(anchor.name, anchor.point[0], anchor.point[1]);
}

function render() {
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.fillStyle = "#0b0f12";
  ctx.fillRect(0, 0, view.width, view.height);
  ctx.setTransform(Z, 0, 0, Z, 0, 0);
  ctx.imageSmoothingEnabled = false;
  for (const layer of SCENE.layers) {
    if (layer.kind === "objects") drawObjects();
    else if (images[layer.image]) ctx.drawImage(images[layer.image], 0, 0);
  }
  if (state.debug) drawDebug();
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  if (state.debug) drawLabels();
}

function updateStatus() {
  const parts = [`x ${actor.x.toFixed(1)} y ${actor.y.toFixed(1)}`, isBlocked(world, actor.x, actor.y) ? "BLOCKED" : "free"];
  if (actor.latched.size) parts.push("latched " + [...actor.latched].join(","));
  const near = world.interactions.filter((item) => Math.hypot(actor.x - item.x, actor.y - item.y) <= item.reach);
  if (near.length) parts.push("in reach " + near.map((item) => item.id).join(",") + " (E)");
  if (state.message) parts.push(state.message);
  statusLine.textContent = parts.join(" | ");
}

// ---- checks used by window.__scene and the buttons ----

function alphaAt(imageId, ix, iy) {
  let cache = alphaCache[imageId];
  if (!cache) {
    const image = images[imageId], canvas = document.createElement("canvas");
    canvas.width = image.width;
    canvas.height = image.height;
    const g = canvas.getContext("2d");
    g.drawImage(image, 0, 0);
    cache = alphaCache[imageId] = {width: image.width, height: image.height,
      alpha: g.getImageData(0, 0, image.width, image.height).data};
  }
  if (ix < 0 || iy < 0 || ix >= cache.width || iy >= cache.height) return 0;
  return cache.alpha[(iy * cache.width + ix) * 4 + 3];
}

function objectAlpha(object, wx, wy) {
  const s = object.scale;
  const left = Math.round(object.x - object.anchor[0] * s), top = Math.round(object.y - object.anchor[1] * s);
  return alphaAt(object.image, Math.floor((wx + 0.5 - left) / s), Math.floor((wy + 0.5 - top) / s));
}

function canvasPixel(wx, wy) {
  return [...ctx.getImageData(Math.floor((wx + 0.5) * Z), Math.floor((wy + 0.5) * Z), 1, 1).data];
}

// Put the marker just behind and just in front of an object's sort line where the object's
// art is opaque: behind, the pixel must not change; in front, it must.
function probeYSort() {
  needReady();
  const saved = {x: actor.x, y: actor.y, debug: state.debug, props: state.props, visible: state.actorVisible};
  const h = SCENE.actor.height;
  let result = {status: "skipped", reason: "no object art is opaque where the marker would stand behind it"};
  state.debug = false;
  state.props = true;
  const sample = (y, wx, wy, visible) => {
    actor.y = y;
    state.actorVisible = visible;
    render();
    return canvasPixel(wx, wy);
  };
  try {
    for (const object of SCENE.objects) {
      if (object.image === null || !images[object.image]) continue;
      const wx = Math.round(object.x), behind = object.sortY - 1, front = object.sortY + 1;
      const by = Math.floor(behind - h / 2), fy = Math.floor(front - h / 2);
      if (objectAlpha(object, wx, by) < 255) continue;
      actor.x = object.x;
      const hiddenWith = sample(behind, wx, by, true), hiddenWithout = sample(behind, wx, by, false);
      const shownWith = sample(front, wx, fy, true), shownWithout = sample(front, wx, fy, false);
      const hidden = hiddenWith.join() === hiddenWithout.join(), shown = shownWith.join() !== shownWithout.join();
      if (hidden && !shown) continue;  // something drawn later also covers the marker in front: try another object
      result = {status: hidden && shown ? "pass" : "fail", object: object.id, pixel: [wx, by, fy],
        behind: {withActor: hiddenWith, withoutActor: hiddenWithout}, front: {withActor: shownWith, withoutActor: shownWithout}};
      break;
    }
  } finally {
    Object.assign(state, {debug: saved.debug, props: saved.props, actorVisible: saved.visible});
    actor.x = saved.x;
    actor.y = saved.y;
    render();
  }
  return result;
}

function snapshot() {
  const extra = {
    ready: state.ready, error: state.error, tick: state.tick, title: SCENE.title, source: SCENE.source,
    start: SCENE.start, speed: SCENE.speed, zoom: Z, debug: state.debug, assets: state.assets,
    drawOrder: SCENE.objects.map((object) => object.id), exitsFired: [...state.exitsFired],
    interactionsReached: [...state.reached], events: state.events.slice(), routes: state.routes,
  };
  if (world === null || actor === null) return {schema: SNAPSHOT_SCHEMA, runtime: {name: "map-runtime", version: RUNTIME_VERSION}, ...extra};
  return runtimeSnapshot(world, actor, extra);
}

function showSnapshot() {
  control("snapshot").value = JSON.stringify(snapshot(), null, 2);
  control("snapshot-panel").open = true;
}

function traverseAll() {
  needReady();
  state.routes = traverseRoutes(world, {speed: SCENE.speed});
  const failing = state.routes.results.filter((item) => !item.ok).length;
  record({type: "routes", ok: state.routes.ok, failing});
  say(state.routes.ok ? `routes: all ${state.routes.results.length} targets passed` : `routes: ${failing} failing (see snapshot)`);
  return state.routes;
}

function reset(spawnId) {
  needReady();
  const spawn = spawnId === undefined ? startSpawn : world.spawnById.get(spawnId);
  if (!spawn) throw new Error(`no spawn ${spawnId}`);
  state.keys.clear();
  arrive(world, actor, spawn.x, spawn.y);
  if (spawn.facing) [actor.facingX, actor.facingY] = spawn.facing;
  record({type: "arrive", id: spawn.id});
  say(`at spawn ${spawn.id}`);
  return {x: actor.x, y: actor.y};
}

function teleport(x, y) {
  needReady();
  arrive(world, actor, x, y);
  record({type: "teleport", x, y});
  return {x: actor.x, y: actor.y, blocked: isBlocked(world, x, y)};
}

function walkTo(x, y) {
  needReady();
  const path = findPath(world, actor.x, actor.y, x, y);
  actor.path = path && path.length ? path : null;
  say(path ? `walking to ${x.toFixed(1)}, ${y.toFixed(1)}` : `no path to ${x.toFixed(1)}, ${y.toFixed(1)}`);
  return path !== null;
}

function setDebug(on) {
  state.debug = Boolean(on);
  control("debug").checked = state.debug;
}

function setGrid(on) {
  state.grid = Boolean(on);
  control("grid").checked = state.grid;
  state.gridCanvas = state.grid && state.ready ? buildGridCanvas() : null;
}

window.__scene = Object.freeze({
  version: RUNTIME_VERSION,
  get ready() { return state.ready; },
  get error() { return state.error; },
  get tick() { return state.tick; },
  snapshot,
  position: () => (actor === null ? null : {x: actor.x, y: actor.y}),
  reset, teleport, walkTo, traverseAll, probeYSort, setDebug, setGrid,
  hold: (key) => { state.keys.add(String(key).toLowerCase()); },
  release: (key) => { state.keys.delete(String(key).toLowerCase()); },
  step: (count = 1) => {
    needReady();
    for (let k = 0; k < count; k++) tick();
    render();
    return {x: actor.x, y: actor.y, tick: state.tick};
  },
  waitTicks: (count = 1) => new Promise((resolve) => state.waiters.push({tick: state.tick + count, resolve})),
  canMove: (dx, dy) => {
    needReady();
    const next = moveWithCollision(world, actor.x, actor.y, dx * BUDGET, dy * BUDGET);
    return next.x !== actor.x || next.y !== actor.y;
  },
  isBlocked: (x, y) => isBlocked(world, x, y),
  segmentClear: (ax, ay, bx, by) => segmentClear(world, ax, ay, bx, by),
  findPath: (x, y) => findPath(world, actor.x, actor.y, x, y),
});

// ---- input ----

view.addEventListener("keydown", (event) => {
  const key = event.key.toLowerCase();
  if (KEY_DIRECTIONS[key]) {
    event.preventDefault();
    if (state.ready) actor.path = null;
    state.keys.add(key);
  } else if (key === "e" && !event.repeat && state.ready) {
    interact();
  }
});
view.addEventListener("keyup", (event) => state.keys.delete(event.key.toLowerCase()));
view.addEventListener("blur", () => state.keys.clear());
view.addEventListener("pointerdown", (event) => {
  if (!state.ready || event.button > 0) return;
  view.focus();
  const rect = view.getBoundingClientRect();
  walkTo((event.clientX - rect.left) / rect.width * W, (event.clientY - rect.top) / rect.height * H);
});
control("debug").addEventListener("change", (event) => setDebug(event.target.checked));
control("grid").addEventListener("change", (event) => setGrid(event.target.checked));
control("props").addEventListener("change", (event) => { state.props = event.target.checked; });
control("actor").addEventListener("change", (event) => { state.actorVisible = event.target.checked; });
control("reset").addEventListener("click", () => { reset(control("spawn").value); view.focus(); });
control("spawn").addEventListener("change", () => { reset(control("spawn").value); view.focus(); });
control("routes").addEventListener("click", () => { traverseAll(); showSnapshot(); });
control("snap").addEventListener("click", showSnapshot);

// ---- start ----

let last = null, accumulated = 0;
function frame(time) {
  try {
    if (last === null) last = time;
    accumulated += Math.min(0.25, (time - last) / 1000);
    last = time;
    let ticks = 0;
    while (accumulated >= 1 / TICK_HZ && ticks < 8) {
      tick();
      accumulated -= 1 / TICK_HZ;
      ticks += 1;
    }
    if (ticks === 8) accumulated = 0;
    render();
    updateStatus();
    requestAnimationFrame(frame);
  } catch (error) {
    fail(error);
  }
}

function loadImage(id, [mime, payload]) {
  return new Promise((resolve) => {
    const image = new Image();
    image.onload = () => {
      images[id] = image;
      state.assets.loaded += 1;
      resolve();
    };
    image.onerror = () => {
      state.assets.failed.push(id);
      resolve();
    };
    image.src = "data" + String.fromCharCode(58) + mime + ";base64," + payload;
  });
}

try {
  world = createMapRuntime(SCENE.bundle, {materialGrid: SCENE.materialGrid});
  startSpawn = world.spawnById.get(SCENE.start);
  actor = createActor(world, startSpawn.x, startSpawn.y, startSpawn.facing);
  for (const spawn of world.spawns) control("spawn").add(new Option(spawn.id, spawn.id, false, spawn.id === SCENE.start));
  state.materialCanvas = buildMaterialCanvas();
  const entries = Object.entries(IMAGE_DATA);
  state.assets.total = entries.length;
  Promise.all(entries.map(([id, item]) => loadImage(id, item))).then(() => {
    state.ready = true;
    record({type: "arrive", id: startSpawn.id});
    say(state.assets.failed.length ? `${state.assets.failed.length} images failed to load` : `at spawn ${startSpawn.id}`);
    requestAnimationFrame(frame);
  });
} catch (error) {
  fail(error);
}
"""

_HTML = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>@@TITLE@@ - scene preview</title>
<style>
@@STYLE@@</style>
</head>
<body>
<header>
<div><h1>@@TITLE@@</h1><p class="sub">@@SUBTITLE@@</p></div>
<nav>
<label><input type="checkbox" id="debug"> Debug</label>
<label><input type="checkbox" id="grid"> Nav grid</label>
<label><input type="checkbox" id="props" checked> Props</label>
<label><input type="checkbox" id="actor" checked> Actor</label>
<select id="spawn" aria-label="Spawn"></select>
<button id="reset" type="button">Reset</button>
<button id="routes" type="button">Check routes</button>
<button id="snap" type="button">Snapshot</button>
</nav>
</header>
<main>
<canvas id="view" tabindex="0" aria-label="Scene preview"></canvas>
<footer><span id="status">loading</span><span>Click the map, then WASD or arrow keys to walk; click to walk there; E interacts.</span></footer>
<details id="snapshot-panel"><summary>window.__scene snapshot</summary><textarea id="snapshot" readonly></textarea></details>
</main>
<script type="module">
@@RUNTIME@@
@@SCRIPT@@</script>
</body>
</html>
"""

# --------------------------------------------------------------------------- verifier (node + playwright)

_VERIFY_SCRIPT = r"""
const config = JSON.parse(process.env.FORGE_SCENE_VERIFY || "{}");
const emit = (data) => process.stdout.write(JSON.stringify(data) + "\n");
const message = (error) => String((error && error.message) || error).split("\n")[0];
let playwright = null;
for (const name of ["playwright", "playwright-core"]) {
  try {
    playwright = require(name);
    break;
  } catch (error) {
    if (!error || error.code !== "MODULE_NOT_FOUND") throw error;
  }
}
(async () => {
  if (playwright === null) {
    emit({skipped: "the playwright npm package was not found (npm install playwright, then npx playwright install chromium)"});
    return;
  }
  let browser;
  try {
    browser = await playwright.chromium.launch({headless: true});
  } catch (error) {
    emit({skipped: "chromium did not launch: " + message(error)});
    return;
  }
  const result = {browser: browser.version(), pageErrors: [], consoleErrors: [], requests: []};
  try {
    const page = await browser.newPage({viewport: {width: 1280, height: 900}, deviceScaleFactor: 1});
    page.on("pageerror", (error) => result.pageErrors.push(message(error)));
    page.on("console", (entry) => { if (entry.type() === "error") result.consoleErrors.push(entry.text()); });
    page.on("request", (request) => {
      const url = request.url();
      if (url !== config.url && !/^(data|about):/i.test(url)) result.requests.push(url.slice(0, 200));
    });
    await page.goto(config.url, {waitUntil: "load", timeout: config.timeoutMs});
    await page.waitForFunction(() => window.__scene && (window.__scene.ready || window.__scene.error), null,
      {timeout: config.timeoutMs});
    result.error = await page.evaluate(() => window.__scene.error);
    if (!result.error) {
      result.assets = await page.evaluate(() => window.__scene.snapshot().assets);
      await page.focus("#view");
      result.keyboard = [];
      for (const [key, dx, dy] of [["ArrowRight", 1, 0], ["ArrowLeft", -1, 0], ["ArrowDown", 0, 1], ["ArrowUp", 0, -1]]) {
        const before = await page.evaluate(() => window.__scene.reset());
        const free = await page.evaluate(([x, y]) => window.__scene.canMove(x, y), [dx, dy]);
        await page.keyboard.down(key);
        await page.evaluate(() => window.__scene.waitTicks(12));
        await page.keyboard.up(key);
        const after = await page.evaluate(() => window.__scene.position());
        result.keyboard.push({key, free, moved: (after.x - before.x) * dx + (after.y - before.y) * dy});
      }
      await page.evaluate(() => window.__scene.reset());
      result.routes = await page.evaluate(() => window.__scene.traverseAll());
      result.ysort = await page.evaluate(() => window.__scene.probeYSort());
      await page.evaluate(() => { window.__scene.reset(); window.__scene.setDebug(false); });
      await page.locator("#view").screenshot({path: config.screen});
      await page.evaluate(() => window.__scene.setDebug(true));
      await page.locator("#view").screenshot({path: config.debug});
      await page.evaluate(() => window.__scene.setDebug(false));
      result.snapshot = await page.evaluate(() => window.__scene.snapshot());
    }
  } catch (error) {
    result.failure = message(error);
  } finally {
    await browser.close();
  }
  emit(result);
})().catch((error) => emit({failure: message(error)}));
"""


# --------------------------------------------------------------------------- errors and small readers

class QAFailure(ValueError):
    """A QA gate failed; nothing is published."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        print(f"error: {forge_core.ascii_text(message)}", file=sys.stderr)
        raise SystemExit(1)


def _number(value: Any, where: str, *, minimum: float | None = None, positive: bool = False) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{where} must be a finite number.")
    if positive and value <= 0:
        raise ValueError(f"{where} must be positive.")
    if minimum is not None and value < minimum:
        raise ValueError(f"{where} must be at least {minimum:g}.")
    return value


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{where} must be a non-empty string.")
    return value


def _point(value: Any, where: str) -> list[float | int]:
    """[x, y], {x, y} or {point: [x, y]} as [x, y]."""
    if isinstance(value, dict):
        if "point" in value:
            return _point(value["point"], f"{where}.point")
        return [_number(value.get("x"), f"{where}.x"), _number(value.get("y"), f"{where}.y")]
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"{where} must be [x, y].")
    return [_number(value[0], f"{where}[0]"), _number(value[1], f"{where}[1]")]


def _polygon(value: Any, where: str) -> list[list[float | int]]:
    if not isinstance(value, list) or len(value) < 3:
        raise ValueError(f"{where} must list at least 3 [x, y] points.")
    return [_point(item, f"{where}[{index}]") for index, item in enumerate(value)]


def _list(value: Any, where: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{where} must be a list.")
    return value


def _object(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be an object.")
    return value


def _unique(items: list[dict[str, Any]], what: str) -> None:
    seen: set[str] = set()
    for item in items:
        if item["id"] in seen:
            raise ValueError(f"Duplicate {what} id {item['id']!r}.")
        seen.add(item["id"])


def _local_file_ref(path: Path, base_dir: Path, sha256: str | None = None) -> dict[str, Any]:
    """fileRef for a manifest in base_dir: a relative POSIX path, or the bare file name when no
    relative route exists (another drive), since forge_core.portable_path then returns an absolute path."""
    relative = forge_core.portable_path(path, base_dir)
    if relative.startswith("/") or re.match(r"^[A-Za-z]:", relative):
        relative = Path(path).name
    return {"path": relative, "sha256": sha256 or forge_core.sha256_file(path), "bytes": Path(path).stat().st_size}


# --------------------------------------------------------------------------- build context

def _local_png_bytes(pixels: np.ndarray) -> bytes:
    """forge_core.save_png into memory: deterministic, no metadata chunks, RGB zeroed under alpha 0."""
    rgba = np.array(pixels, dtype=np.uint8, copy=True)
    rgba[rgba[..., 3] == 0] = 0
    buffer = io.BytesIO()
    Image.fromarray(rgba).save(buffer, format="PNG", optimize=False, compress_level=6)
    return buffer.getvalue()


class Build:
    """Inputs read (with sha256), embedded images and warnings of one build."""

    def __init__(self, bundle_path: Path, report_dir: Path) -> None:
        self.bundle_path = bundle_path
        self.bundle_dir = bundle_path.parent
        self.report_dir = report_dir
        self.inputs: list[dict[str, Any]] = []
        self._input_keys: set[str] = set()
        self.warnings: list[str] = []
        self.images: dict[str, list[str]] = {}
        self._image_ids: dict[str, str] = {}
        self._embedded_files: dict[str, tuple[str, list[int]]] = {}
        self.image_bytes = 0
        self.hashes_checked = 0
        self.hashes_missing = 0

    def resolve(self, value: Any, where: str, base: Path | None = None) -> Path:
        """A manifest-relative file reference (POSIX, no drive, scheme or leading slash) that must exist."""
        reference = _text(value, where)
        if "\\" in reference or reference.startswith("/") or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", reference):
            raise ValueError(f"{where} must be a relative POSIX path, not {reference!r}.")
        path = ((base or self.bundle_dir) / reference).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"{where} names a missing file: {reference}")
        return path

    def read(self, path: Path, where: str, expected_sha256: Any = None) -> bytes:
        """Read an input, check its sha256 when one is given, and record it once."""
        data = path.read_bytes()
        digest = forge_core.sha256_bytes(data)
        if expected_sha256 is not None:
            if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
                raise ValueError(f"{where}: sha256 must be 64 lowercase hex digits.")
            if digest != expected_sha256:
                raise ValueError(f"{where}: sha256 mismatch for {path.name} (expected {expected_sha256}, found {digest}).")
            self.hashes_checked += 1
        elif path != self.bundle_path:
            self.hashes_missing += 1
        key = os.path.normcase(str(path))
        if key not in self._input_keys:
            self._input_keys.add(key)
            self.inputs.append(_local_file_ref(path, self.report_dir, digest))
        return data

    def embed_file(self, path: Path, where: str, expected_sha256: Any = None) -> tuple[str, list[int]]:
        """Embed an image file as it is (PNG, JPEG, WebP, GIF) or re-encoded to PNG; returns (id, [w, h]).
        Every reference is read and hash-checked; each file is decoded once."""
        data = self.read(path, where, expected_sha256)
        key = os.path.normcase(str(path))
        if key in self._embedded_files:
            return self._embedded_files[key]
        try:
            image, info = forge_core.load_rgba(path)
        except ValueError as error:
            raise ValueError(f"{where}: {error}") from None
        except OSError:
            raise ValueError(f"{where}: {path.name} is not a readable image.") from None
        mime = EMBED_FORMATS.get(info.get("format") or "")
        if mime is None:
            data, mime = _local_png_bytes(np.asarray(image)), "image/png"
        self._embedded_files[key] = (self.embed_bytes(data, mime), list(image.size))
        return self._embedded_files[key]

    def embed_bytes(self, data: bytes, mime: str) -> str:
        digest = forge_core.sha256_bytes(data)
        if digest not in self._image_ids:
            image_id = f"i{len(self._image_ids)}"
            self._image_ids[digest] = image_id
            self.images[image_id] = [mime, base64.b64encode(data).decode("ascii")]
            self.image_bytes += len(data)
        return self._image_ids[digest]

    def warn(self, message: str) -> None:
        self.warnings.append(forge_core.ascii_text(message))


# --------------------------------------------------------------------------- collision, portals and points

def _clean_solid(value: Any, where: str) -> dict[str, Any]:
    solid = _object(value, where)
    shape = solid.get("shape")
    if shape == "rect":
        return {"shape": "rect", "x": _number(solid.get("x"), f"{where}.x"), "y": _number(solid.get("y"), f"{where}.y"),
                "w": _number(solid.get("w"), f"{where}.w", minimum=0), "h": _number(solid.get("h"), f"{where}.h", minimum=0)}
    if shape == "ellipse":
        out = {"shape": "ellipse", "cx": _number(solid.get("cx"), f"{where}.cx"), "cy": _number(solid.get("cy"), f"{where}.cy"),
               "rx": _number(solid.get("rx"), f"{where}.rx", minimum=0), "ry": _number(solid.get("ry"), f"{where}.ry", minimum=0)}
        if "rotate" in solid:
            out["rotate"] = _number(solid["rotate"], f"{where}.rotate")
        return out
    if shape == "polygon":
        return {"shape": "polygon", "points": _polygon(solid.get("points"), f"{where}.points")}
    raise ValueError(f"{where}.shape must be rect, ellipse or polygon.")


def clean_collision(value: Any) -> dict[str, Any]:
    """The collision block as the runtime reads it (Appendix C), with every number checked."""
    if not isinstance(value, dict):
        raise ValueError("The bundle needs collision {actorRadius, ...}: the preview walks with the actor footprint.")
    out: dict[str, Any] = {"actorRadius": _number(value.get("actorRadius"), "collision.actorRadius", minimum=0)}
    if "ySquash" in value:
        out["ySquash"] = _number(value["ySquash"], "collision.ySquash", positive=True)
    regions = []
    for index, region in enumerate(_list(value.get("walkRegions"), "collision.walkRegions")):
        where = f"collision.walkRegions[{index}]"
        region = _object(region, where)
        holes = _list(region.get("holes"), f"{where}.holes")
        regions.append({"polygon": _polygon(region.get("polygon"), f"{where}.polygon"),
                        "holes": [_polygon(hole, f"{where}.holes[{h}]") for h, hole in enumerate(holes)]})
    if regions:
        out["walkRegions"] = regions
    solids = [_clean_solid(solid, f"collision.solids[{index}]")
              for index, solid in enumerate(_list(value.get("solids"), "collision.solids"))]
    if solids:
        out["solids"] = solids
    rects = []
    for index, rect in enumerate(_list(value.get("rects"), "collision.rects")):
        where = f"collision.rects[{index}]"
        if not isinstance(rect, list) or len(rect) != 4:
            raise ValueError(f"{where} must be [x, y, w, h].")
        rects.append([_number(rect[0], f"{where}[0]"), _number(rect[1], f"{where}[1]"),
                      _number(rect[2], f"{where}[2]", minimum=0), _number(rect[3], f"{where}[3]", minimum=0)])
    if rects:
        out["rects"] = rects
    return out


def clean_portals(value: Any) -> list[dict[str, Any]]:
    portals = []
    for index, item in enumerate(_list(value, "portals")):
        where = f"portals[{index}]"
        portal = _object(item, where)
        entry: dict[str, Any] = {"id": _text(portal.get("id"), f"{where}.id"), "to": _text(portal.get("to"), f"{where}.to")}
        if ("rect" in portal) == ("circle" in portal):
            raise ValueError(f"{where} needs exactly one of rect [x, y, w, h] or circle [cx, cy, r].")
        if "rect" in portal:
            rect = portal["rect"]
            if not isinstance(rect, list) or len(rect) != 4:
                raise ValueError(f"{where}.rect must be [x, y, w, h].")
            entry["rect"] = [_number(rect[0], f"{where}.rect[0]"), _number(rect[1], f"{where}.rect[1]"),
                             _number(rect[2], f"{where}.rect[2]", minimum=0), _number(rect[3], f"{where}.rect[3]", minimum=0)]
        else:
            circle = portal["circle"]
            if not isinstance(circle, list) or len(circle) != 3:
                raise ValueError(f"{where}.circle must be [cx, cy, r].")
            entry["circle"] = [_number(circle[0], f"{where}.circle[0]"), _number(circle[1], f"{where}.circle[1]"),
                               _number(circle[2], f"{where}.circle[2]", minimum=0)]
        activation = portal.get("activation", "crossing")
        if activation not in ACTIVATIONS:
            raise ValueError(f"{where}.activation must be crossing or intent.")
        entry["activation"] = activation
        if "travelDirection" in portal:
            dx, dy = _point(portal["travelDirection"], f"{where}.travelDirection")
            if not math.sqrt(dx * dx + dy * dy) > 0:
                raise ValueError(f"{where}.travelDirection must not be zero.")
            entry["travelDirection"] = [dx, dy]
        if "radius" in portal:
            entry["radius"] = _number(portal["radius"], f"{where}.radius", minimum=0)
        if activation == "intent" and ("travelDirection" not in entry or "radius" not in entry):
            raise ValueError(f"{where}: intent portals need travelDirection and radius.")
        if "entranceByFrom" in portal:
            mapping = _object(portal["entranceByFrom"], f"{where}.entranceByFrom")
            entry["entranceByFrom"] = {str(key): _text(spawn, f"{where}.entranceByFrom.{key}") for key, spawn in mapping.items()}
        for flag in ("latch", "requiresMovement"):
            if flag in portal:
                if not isinstance(portal[flag], bool):
                    raise ValueError(f"{where}.{flag} must be true or false.")
                entry[flag] = portal[flag]
        portals.append(entry)
    _unique(portals, "portal")
    return portals


def clean_spawns(value: Any) -> list[dict[str, Any]]:
    spawns = []
    for index, item in enumerate(_list(value, "spawns")):
        where = f"spawns[{index}]"
        spawn = _object(item, where)
        entry: dict[str, Any] = {"id": _text(spawn.get("id"), f"{where}.id"),
                                 "x": _number(spawn.get("x"), f"{where}.x"), "y": _number(spawn.get("y"), f"{where}.y")}
        facing = spawn.get("facing")
        if isinstance(facing, str) or (isinstance(facing, (int, float)) and not isinstance(facing, bool)
                                       and math.isfinite(facing)):
            entry["facing"] = facing
        spawns.append(entry)
    _unique(spawns, "spawn")
    return spawns


def clean_interactions(value: Any) -> list[dict[str, Any]]:
    interactions = []
    for index, item in enumerate(_list(value, "interactions")):
        where = f"interactions[{index}]"
        interaction = _object(item, where)
        entry: dict[str, Any] = {"id": _text(interaction.get("id"), f"{where}.id"),
                                 "x": _number(interaction.get("x"), f"{where}.x"),
                                 "y": _number(interaction.get("y"), f"{where}.y")}
        if "reach" in interaction:
            entry["reach"] = _number(interaction["reach"], f"{where}.reach", minimum=0)
        interactions.append(entry)
    _unique(interactions, "interaction")
    return interactions


def clean_anchors(value: Any) -> dict[str, Any]:
    anchors: dict[str, Any] = {}
    for name, item in (_object(value, "anchors") if value is not None else {}).items():
        where = f"anchors.{name}"
        anchor = _object(item, where)
        entry: dict[str, Any] = {"point": _point(anchor.get("point"), f"{where}.point"),
                                 "slots": [_point(slot, f"{where}.slots[{k}]")
                                           for k, slot in enumerate(_list(anchor.get("slots"), f"{where}.slots"))]}
        approach = anchor.get("approach")
        if approach is not None:
            single = isinstance(approach, dict) or (isinstance(approach, list) and len(approach) == 2
                                                    and not isinstance(approach[0], (list, dict)))
            entry["approach"] = (_point(approach, f"{where}.approach") if single else
                                 [_point(p, f"{where}.approach[{k}]") for k, p in enumerate(_list(approach, f"{where}.approach"))])
        anchors[str(name)] = entry
    return anchors


def _clean_footprint(value: Any, where: str, build: Build) -> dict[str, Any]:
    footprint = _object(value, where)
    shape = footprint.get("shape")
    if shape not in FOOTPRINT_SHAPES:
        raise ValueError(f"{where}.shape must be ellipse, rect or none.")
    out: dict[str, Any] = {"shape": shape}
    if shape == "none":
        return out
    out["width"] = _number(footprint.get("width"), f"{where}.width", minimum=0)
    out["depth"] = _number(footprint.get("depth"), f"{where}.depth", minimum=0)
    if "offset" in footprint:
        out["offset"] = _point(footprint["offset"], f"{where}.offset")
    if "rotate" in footprint:
        out["rotate"] = _number(footprint["rotate"], f"{where}.rotate")
    basis = footprint.get("basis")
    if basis is not None:
        if basis not in IMAGE_BASES:
            build.warn(f"{where}.basis {basis!r} is not image_px, prop_px or world_px; it is scaled like image_px.")
        out["basis"] = str(basis)
    return out


# --------------------------------------------------------------------------- props and objects

class PropPacks:
    """Prop labels from prop-pack manifests (v1 or v2): label -> (manifest dir, image, sha256, where)."""

    def __init__(self, build: Build, bundle: dict[str, Any], cli_paths: list[Path]) -> None:
        self.items: dict[str, tuple[Path, Any, Any, str]] = {}
        manifests: list[tuple[Path, str, Any]] = []
        for path in cli_paths:
            resolved = Path(path).resolve()
            if not resolved.is_file():
                raise FileNotFoundError(f"--prop-pack names a missing file: {path}")
            manifests.append((resolved, f"--prop-pack {Path(path).name}", None))
        for index, entry in enumerate(_list(bundle.get("prop_packs"), "prop_packs")):
            where = f"prop_packs[{index}]"
            entry = _object(entry, where)
            manifests.append((build.resolve(entry.get("manifest"), f"{where}.manifest"), where, entry.get("sha256")))
        seen: set[str] = set()
        for path, where, sha in manifests:
            if os.path.normcase(str(path)) in seen:
                continue
            seen.add(os.path.normcase(str(path)))
            try:
                manifest = json.loads(build.read(path, where, sha).decode("utf-8-sig"))
            except json.JSONDecodeError as error:
                raise ValueError(f"{where}: {path.name} is not valid JSON (line {error.lineno}).") from None
            for item in _list(_object(manifest, where).get("accepted"), f"{where} accepted"):
                if not isinstance(item, dict) or item.get("status", "accepted") != "accepted":
                    continue
                label, image = item.get("label"), item.get("image")
                if not isinstance(label, str) or not isinstance(image, str):
                    continue
                if label in self.items:
                    build.warn(f"prop label {label!r} is in more than one prop pack; the first one is used.")
                    continue
                self.items[label] = (path.parent, image, item.get("sha256"), f"{where} item {label!r}")


def _object_art(obj: dict[str, Any], where: str, build: Build, packs: PropPacks) -> tuple[str | None, list[int] | None, str]:
    """(image id, [w, h], source) for an object: its image, its prop's prop-pack image, its occluder source."""
    if "image" in obj:
        path = build.resolve(obj["image"], f"{where}.image")
        return (*build.embed_file(path, f"{where}.image", obj.get("image_sha256")), "object.image")
    label = obj.get("prop")
    if isinstance(label, str) and label in packs.items:
        base, image, sha, item_where = packs.items[label]
        path = build.resolve(image, f"{item_where} image", base=base)
        return (*build.embed_file(path, f"{item_where} image", sha), "prop-pack")
    occluder = obj.get("occluder")
    if isinstance(occluder, dict) and occluder.get("source") is not None:
        path = build.resolve(occluder["source"], f"{where}.occluder.source")
        return (*build.embed_file(path, f"{where}.occluder.source"), "occluder.source")
    return None, None, "missing"


def compile_objects(bundle: dict[str, Any], build: Build, packs: PropPacks) -> tuple[list[dict], list[dict]]:
    """(objects for the runtime, objects for drawing in draw order: sortY or y, x, id, bundle order)."""
    runtime: list[dict[str, Any]] = []
    drawn: list[dict[str, Any]] = []
    keys: list[tuple[Any, ...]] = []
    seen: set[str] = set()
    for index, item in enumerate(_list(bundle.get("objects"), "objects")):
        where = f"objects[{index}]"
        obj = _object(item, where)
        ident = _text(obj.get("id"), f"{where}.id")
        if ident in seen:
            build.warn(f"{where}: object id {ident!r} is used twice; ties are drawn in bundle order.")
        seen.add(ident)
        x, y = _number(obj.get("x"), f"{where}.x"), _number(obj.get("y"), f"{where}.y")
        scale = _number(obj.get("scale", 1), f"{where}.scale", positive=True)
        entry: dict[str, Any] = {"id": ident, "x": x, "y": y, "scale": scale}
        if obj.get("footprint") is not None:
            entry["footprint"] = _clean_footprint(obj["footprint"], f"{where}.footprint", build)
        if "solid" in obj:
            if not isinstance(obj["solid"], bool):
                raise ValueError(f"{where}.solid must be true or false.")
            entry["solid"] = obj["solid"]
        runtime.append(entry)
        image_id, size, art = _object_art(obj, where, build, packs)
        if image_id is None:
            build.warn(f"{where} ({ident}): no art (object image, prop pack label or occluder source); "
                       f"drawn as a magenta post.")
        if obj.get("anchor_px") is not None:
            anchor = _point(obj["anchor_px"], f"{where}.anchor_px")
        elif size is not None:
            anchor = [size[0] / 2, size[1]]
            build.warn(f"{where} ({ident}) has no anchor_px; the bottom centre of its image is used.")
        else:
            anchor = [0, 0]
        sort_y = _number(obj["sortY"], f"{where}.sortY") if "sortY" in obj else y
        drawn.append({"id": ident, "prop": str(obj.get("prop", "")), "x": x, "y": y, "scale": scale,
                      "anchor": anchor, "sortY": sort_y, "image": image_id, "size": size, "art": art})
        keys.append((sort_y, x, ident, index))
    order = sorted(range(len(drawn)), key=keys.__getitem__)
    return runtime, [drawn[k] for k in order]


# --------------------------------------------------------------------------- layers and tiles

def _tile_size(value: Any, where: str) -> tuple[int, int]:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value, value
    if (isinstance(value, list) and len(value) == 2
            and all(isinstance(v, int) and not isinstance(v, bool) and v >= 1 for v in value)):
        return value[0], value[1]
    raise ValueError(f"{where} must be a positive integer or [width, height].")


class Tileset:
    """A tileset manifest's atlas as (count, tile_h, tile_w, 4) RGBA, tile i at column i % columns."""

    def __init__(self, ident: str, tile_w: int, tile_h: int, atlas: np.ndarray) -> None:
        self.ident, self.tile_w, self.tile_h, self.atlas = ident, tile_w, tile_h, atlas


def load_tilesets(bundle: dict[str, Any], build: Build) -> dict[str, Tileset]:
    tilesets: dict[str, Tileset] = {}
    for index, item in enumerate(_list(bundle.get("tilesets"), "tilesets")):
        where = f"tilesets[{index}]"
        entry = _object(item, where)
        ident = _text(entry.get("id"), f"{where}.id")
        if ident in tilesets:
            raise ValueError(f"Duplicate tileset id {ident!r}.")
        manifest_path = build.resolve(entry.get("manifest"), f"{where}.manifest")
        try:
            manifest = json.loads(build.read(manifest_path, f"{where}.manifest", entry.get("sha256")).decode("utf-8-sig"))
        except json.JSONDecodeError as error:
            raise ValueError(f"{where}: {manifest_path.name} is not valid JSON (line {error.lineno}).") from None
        manifest = _object(manifest, f"{where} manifest")
        if manifest.get("schema") not in (None, TILESET_SCHEMA):
            raise ValueError(f"{where}: {manifest_path.name} is not a {TILESET_SCHEMA} manifest.")
        tile_w, tile_h = _tile_size(manifest.get("tile_size"), f"tileset {ident} tile_size")
        columns = manifest.get("columns")
        if not isinstance(columns, int) or isinstance(columns, bool) or columns < 1:
            raise ValueError(f"tileset {ident} columns must be a positive integer.")
        image_path = build.resolve(manifest.get("image"), f"tileset {ident} image", base=manifest_path.parent)
        build.read(image_path, f"tileset {ident} image", manifest.get("sha256"))
        image, _ = forge_core.load_rgba(image_path)
        pixels = np.asarray(image)
        rows = pixels.shape[0] // tile_h
        if rows < 1 or pixels.shape[1] < columns * tile_w:
            raise ValueError(f"tileset {ident}: a {image.width}x{image.height} image holds no {columns}-column grid "
                             f"of {tile_w}x{tile_h} tiles.")
        atlas = (pixels[:rows * tile_h, :columns * tile_w].reshape(rows, tile_h, columns, tile_w, 4)
                 .transpose(0, 2, 1, 3, 4).reshape(rows * columns, tile_h, tile_w, 4))
        tilesets[ident] = Tileset(ident, tile_w, tile_h, atlas)
    return tilesets


def _tile_rows(data: Any, where: str) -> list[list[Any]]:
    """Rows of a tile grid given as rows, as {data: rows} or as {width, data: flat list}."""
    if isinstance(data, dict):
        width, cells = data.get("width"), data.get("data")
        if (isinstance(width, int) and not isinstance(width, bool) and width > 0 and isinstance(cells, list)
                and cells and not isinstance(cells[0], list)):
            if len(cells) % width:
                raise ValueError(f"{where}: {len(cells)} cells do not fill rows of width {width}.")
            return [cells[k:k + width] for k in range(0, len(cells), width)]
        data = cells
    if not isinstance(data, list) or not all(isinstance(row, list) for row in data):
        raise ValueError(f"{where} must be a grid of tile indices (rows of integers).")
    return data


def read_tile_grid(layer: dict[str, Any], where: str, build: Build) -> np.ndarray:
    """Tile indices of a tiles layer: 0-based into the tileset; a negative index or null is an empty cell."""
    data = layer.get("data")
    if isinstance(data, str):
        path = build.resolve(data, f"{where}.data")
        raw = build.read(path, f"{where}.data", layer.get("sha256")).decode("utf-8-sig")
        if path.suffix.lower() == ".json":
            try:
                rows = _tile_rows(json.loads(raw), f"{where}.data")
            except json.JSONDecodeError as error:
                raise ValueError(f"{where}.data: {path.name} is not valid JSON (line {error.lineno}).") from None
        else:  # CSV; one comma ending a row (Tiled style) is a row separator, not an empty cell
            lines = [line.strip() for line in raw.splitlines() if line.strip()]
            rows = [[cell.strip() for cell in (line[:-1] if line.endswith(",") else line).split(",")] for line in lines]
    else:
        rows = _tile_rows(data, f"{where}.data")
    if not rows or not rows[0] or any(len(row) != len(rows[0]) for row in rows):
        raise ValueError(f"{where}.data must be a non-empty rectangular grid.")
    grid = np.empty((len(rows), len(rows[0])), np.int64)
    for j, row in enumerate(rows):
        for i, cell in enumerate(row):
            value: Any = -1 if cell is None or cell == "" else cell
            if isinstance(value, str):
                value = int(value) if re.fullmatch(r"-?\d+", value) else None
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{where}.data row {j} column {i}: {cell!r} is not a tile index.")
            grid[j, i] = value
    return grid


def render_tile_layer(layer: dict[str, Any], where: str, build: Build, tilesets: dict[str, Tileset],
                      canvas_size: tuple[int, int], bundle_tile_size: Any) -> np.ndarray:
    """Pre-render a tiles layer at world size with one vectorised atlas gather; cells past the world are cropped."""
    grid = read_tile_grid(layer, where, build)
    name = layer.get("tileset")
    if name is None:
        if len(tilesets) != 1:
            raise ValueError(f"{where} must name its tileset (the bundle lists {len(tilesets)}).")
        tileset = next(iter(tilesets.values()))
    elif name in tilesets:
        tileset = tilesets[name]
    else:
        raise ValueError(f"{where}.tileset {name!r} is not in tilesets.")
    if bundle_tile_size is not None and _tile_size(bundle_tile_size, "tile_size") != (tileset.tile_w, tileset.tile_h):
        raise ValueError(f"{where}: tileset {tileset.ident} tiles are {tileset.tile_w}x{tileset.tile_h}, "
                         f"the bundle tile_size is {bundle_tile_size}.")
    count = tileset.atlas.shape[0]
    if int(grid.max()) >= count:
        raise ValueError(f"{where}: tile index {int(grid.max())} is outside tileset {tileset.ident} ({count} tiles).")
    tw, th = tileset.tile_w, tileset.tile_h
    atlas = np.concatenate([tileset.atlas, np.zeros((1, th, tw, 4), np.uint8)])
    rows, cols = grid.shape
    layer_pixels = atlas[np.where(grid < 0, count, grid)].transpose(0, 2, 1, 3, 4).reshape(rows * th, cols * tw, 4)
    width, height = canvas_size
    if layer_pixels.shape[0] > height or layer_pixels.shape[1] > width:
        build.warn(f"{where}: {cols}x{rows} tiles reach past the {width}x{height} world and are cropped.")
    canvas = np.zeros((height, width, 4), np.uint8)
    h, w = min(height, layer_pixels.shape[0]), min(width, layer_pixels.shape[1])
    canvas[:h, :w] = layer_pixels[:h, :w]
    return canvas


def compile_layers(bundle: dict[str, Any], build: Build, canvas_size: tuple[int, int], has_objects: bool) -> list[dict]:
    """Render order: image layers as given, tiles layers pre-rendered, one objects band (appended when absent)."""
    tilesets = load_tilesets(bundle, build)
    layers: list[dict[str, Any]] = []
    band = False
    for index, item in enumerate(_list(bundle.get("layers"), "layers")):
        where = f"layers[{index}]"
        layer = _object(item, where)
        name, kind = _text(layer.get("name"), f"{where}.name"), layer.get("kind")
        if kind == "image":
            path = build.resolve(layer.get("image"), f"{where}.image")
            image_id, size = build.embed_file(path, f"{where}.image", layer.get("sha256"))
            if tuple(size) != canvas_size:
                build.warn(f"{where} ({name}) is {size[0]}x{size[1]}, the world is {canvas_size[0]}x{canvas_size[1]}; "
                           f"it is drawn at the origin, 1 image px per world px.")
            layers.append({"name": name, "kind": "image", "image": image_id, "source": "image", "size": size})
        elif kind == "tiles":
            pixels = render_tile_layer(layer, where, build, tilesets, canvas_size, bundle.get("tile_size"))
            image_id = build.embed_bytes(_local_png_bytes(pixels), "image/png")
            layers.append({"name": name, "kind": "image", "image": image_id, "source": "tiles",
                           "size": list(canvas_size)})
        elif kind == "objects":
            if band:
                build.warn(f"{where} ({name}): only the first objects layer draws the objects and the actor.")
                continue
            band = True
            layers.append({"name": name, "kind": "objects", "source": "objects"})
        else:
            raise ValueError(f"{where}.kind must be tiles, image or objects.")
    if not band:
        if has_objects:
            build.warn("No objects layer: objects and the actor are drawn above every layer.")
        layers.append({"name": "objects", "kind": "objects", "source": "objects"})
    return layers


# --------------------------------------------------------------------------- material map

def _color(value: Any, where: str) -> tuple[int, int, int]:
    if isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?", value):
        return int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16)
    if (isinstance(value, list) and len(value) == 3
            and all(isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 255 for v in value)):
        return value[0], value[1], value[2]
    raise ValueError(f"{where} must be #rrggbb or [r, g, b].")


def material_blocks(material_class: str, walkable: Any) -> bool:
    """Appendix C for a top-down walker: solid blocks; liquid and hazard block unless walkable is true."""
    return material_class == "solid" or (material_class in ("liquid", "hazard") and walkable is not True)


def compile_material_grid(bundle: dict[str, Any], build: Build,
                          world: tuple[float, float]) -> tuple[dict | None, dict | None]:
    """The blocked-cell bit grid of material_map for map-runtime.mjs, and a summary for the report."""
    spec = bundle.get("material_map")
    if spec is None:
        return None, None
    spec = _object(spec, "material_map")
    path = build.resolve(spec.get("image"), "material_map.image")
    build.read(path, "material_map.image", spec.get("sha256"))
    materials = _object(spec.get("materials"), "material_map.materials")
    try:
        with Image.open(path) as source:
            if int(getattr(source, "n_frames", 1)) > 1:
                raise ValueError("material_map.image is animated; export one still frame.")
            source.load()
            mode = source.mode
            indices = np.asarray(source) if mode in ("P", "L") else None
            rgba = np.asarray(source.convert("RGBA"))
    except OSError:
        raise ValueError(f"material_map.image: {path.name} is not a readable image.") from None
    height, width = rgba.shape[:2]
    opaque = rgba[..., 3] > 0
    matched = np.zeros((height, width), np.uint16)
    blocked = np.zeros((height, width), bool)
    counts: dict[str, int] = {}
    for name, item in materials.items():
        where = f"material_map.materials.{name}"
        material = _object(item, where)
        material_class = material.get("class")
        if material_class not in MATERIAL_CLASSES:
            raise ValueError(f"{where}.class must be one of {', '.join(MATERIAL_CLASSES)}.")
        walkable = material.get("walkable")
        if walkable is not None and not isinstance(walkable, bool):
            raise ValueError(f"{where}.walkable must be true or false.")
        if "color" in material:
            r, g, b = _color(material["color"], f"{where}.color")
            hit = (rgba[..., 0] == r) & (rgba[..., 1] == g) & (rgba[..., 2] == b) & opaque
        elif "index" in material:
            if indices is None:
                raise ValueError(f"{where}.index needs a palette or greyscale material image; {path.name} is {mode}.")
            hit = (indices == int(_number(material["index"], f"{where}.index", minimum=0))) & opaque
        else:
            raise ValueError(f"{where} needs color or index.")
        matched += hit
        if material_blocks(material_class, walkable):
            blocked |= hit
        counts[str(name)] = int(hit.sum())
    overlap = int((matched > 1).sum())
    if overlap:
        raise ValueError(f"material_map: {overlap} pixels match more than one material.")
    unclassified = int((opaque & (matched == 0)).sum())
    if unclassified:
        build.warn(f"material_map: {unclassified} opaque pixels match no material and do not block.")
    cell_w, cell_h = world[0] / width, world[1] / height
    if not math.isclose(cell_w, cell_h, rel_tol=1e-9):
        build.warn(f"material_map: cells are {cell_w:g}x{cell_h:g} world px (the image and world aspects differ).")
    bits = np.packbits(blocked.ravel(), bitorder="little")
    grid = {"width": width, "height": height, "cellWidth": cell_w, "cellHeight": cell_h,
            "bits": base64.b64encode(bits.tobytes()).decode("ascii")}
    info = {"size": [width, height], "cell": [cell_w, cell_h], "blockedCells": int(blocked.sum()),
            "pixelsPerMaterial": counts, "unclassified": unclassified}
    return grid, info


# --------------------------------------------------------------------------- page assembly

_JSON_STRING = re.compile(r'"[^"\\]*(?:\\.[^"\\]*)*"')


def script_json(value: Any) -> str:
    """JSON that is safe inside an inline script: no '<', '>' or '&' anywhere and no ':' inside strings,
    so data can neither close the script nor spell a URL. JavaScript reads the escapes back unchanged."""
    text = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    text = _JSON_STRING.sub(lambda match: match.group(0).replace(":", "\\u003a"), text)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _html_text(text: str) -> str:
    """Escaped ASCII text for markup; ':' is escaped too, so no text can spell a URL scheme."""
    return html.escape(text, quote=True).replace(":", "&#58;").encode("ascii", "xmlcharrefreplace").decode("ascii")


def read_runtime() -> tuple[str, str, str]:
    """(source with LF line ends, its sha256, RUNTIME_VERSION) of references/runtime/map-runtime.mjs."""
    source = RUNTIME_PATH.read_text(encoding="utf-8").replace("\r\n", "\n")
    if not source.isascii() or re.search(r"(?i)</script|<!--|<script", source):
        raise ValueError(f"{RUNTIME_PATH.name} must be ASCII without script or comment markup.")
    version = re.search(r'export const RUNTIME_VERSION = "([^"]+)";', source)
    if version is None:
        raise ValueError(f"{RUNTIME_PATH.name} does not declare RUNTIME_VERSION.")
    return source, forge_core.sha256_bytes(source.encode("ascii")), version.group(1)


def render_page(title: str, subtitle: str, runtime: str, scene_json: str, images_json: str) -> str:
    """The page: data goes into the script first, so text inside the runtime or the data is never rescanned."""
    script = _PAGE_SCRIPT.strip("\n").replace("@@SCENE@@", scene_json, 1).replace("@@IMAGES@@", images_json, 1)
    values = {"TITLE": _html_text(title), "SUBTITLE": _html_text(subtitle), "STYLE": _STYLE,
              "RUNTIME": runtime.rstrip("\n"), "SCRIPT": script + "\n"}
    return re.sub(r"@@(TITLE|SUBTITLE|STYLE|RUNTIME|SCRIPT)@@", lambda match: values[match.group(1)], _HTML)


# --------------------------------------------------------------------------- QA

_LOADING_TAGS = r"(?:link|iframe|frame|object|embed|base|img|image|video|audio|source|track|form|portal|script)"
_PAGE_PATTERNS = (  # anywhere in the page: markup, style or code
    ("url", re.compile(r"://")),
    ("network-scheme", re.compile(r"(?i)\b(?:https?|wss?|ftp|file|blob|javascript)\s*:")),
    ("protocol-relative", re.compile(r"[\"'`]\s*//")),
    ("css-import", re.compile(r"(?i)@import\b")),
    ("css-url", re.compile(r"(?i)\burl\s*\(")),
    ("http-equiv", re.compile(r"(?i)http-equiv")),
    ("network-api", re.compile(r"\b(?:fetch|XMLHttpRequest|WebSocket|EventSource|sendBeacon|importScripts|Worker|SharedWorker)\b")),
    ("module-import", re.compile(r"\bimport\s*\(|^\s*import\b", re.M)),
    ("markup-injection", re.compile(r"\b(?:innerHTML|outerHTML|insertAdjacentHTML|createContextualFragment)\b|document\.write")),
    ("loading-element", re.compile(r"(?i)createElement(?:NS)?\(\s*[\"'`]" + _LOADING_TAGS + r"\b")),
)
_MARKUP_PATTERNS = (  # outside the script element, where the HTML parser reads tags and attributes
    ("loading-tag", re.compile(r"(?i)<" + _LOADING_TAGS + r"\b")),
    ("loading-attribute", re.compile(
        r"(?i)\s(?:src|href|action|formaction|poster|srcset|background|codebase|cite|longdesc|manifest|ping|data)\s*=")),
)
_SCRIPT_ELEMENT = re.compile(r"(?is)<script\b[^>]*>.*?</script\s*>")
_BASE64 = re.compile(r"[A-Za-z0-9+/]*={0,2}")


def external_references(skeleton: str, texts: list[str], scene_json: str, images_json: str) -> list[str]:
    """Everything in the page that could load from outside it. The skeleton is the page built with neutral
    texts and both data blobs set to null, so the scan reads only this tool's markup and code; the escaped
    texts and the data blobs are checked on their own (no markup, no URL, allow-listed image payloads)."""
    findings = [f"{name}: {match.group(0)!r}" for name, pattern in _PAGE_PATTERNS for match in pattern.finditer(skeleton)]
    markup = _SCRIPT_ELEMENT.sub("", skeleton)
    findings += [f"{name}: {match.group(0).strip()!r}" for name, pattern in _MARKUP_PATTERNS
                 for match in pattern.finditer(markup)]
    findings += [f"text {text[:40]!r} is not escaped" for text in texts if re.search(r"[<>]|://", text)]
    if re.search(r"[<>&]", scene_json) or "://" in scene_json:
        findings.append("scene data is not script-safe")
    for key, value in json.loads(images_json).items():
        if (not re.fullmatch(r"i\d+", key) or not isinstance(value, list) or len(value) != 2
                or value[0] not in EMBED_FORMATS.values() or not _BASE64.fullmatch(value[1])):
            findings.append(f"image {key!r} is not an allow-listed base64 payload")
    return findings


def _check(check_id: str, status: str, value: Any = None, threshold: Any = None) -> dict[str, Any]:
    return {"id": check_id, "status": status, "value": value, "threshold": threshold}


def page_checks(page: str, data: bytes, runtime: str, texts: tuple[str, str], scene_json: str, images_json: str,
                max_bytes: int) -> list[dict[str, Any]]:
    """The gates every published page must pass: size, nothing external, ASCII, one inline script, the runtime."""
    lowered = page.lower()
    script_ok = lowered.count("<script") == 1 and lowered.count("</script") == 1 and "<!--" not in page
    skeleton = render_page("title", "subtitle", runtime, "null", "null")
    findings = external_references(skeleton, [_html_text(text) for text in texts], scene_json, images_json)
    return [
        _check("html-size", "pass" if len(data) <= max_bytes else "fail", len(data), max_bytes),
        _check("external-references", "pass" if not findings else "fail", findings, []),
        _check("ascii-only", "pass" if page.isascii() else "fail", page.isascii(), True),
        _check("single-inline-script", "pass" if script_ok else "fail", script_ok, True),
        _check("runtime-inlined", "pass" if runtime.rstrip("\n") in page else "fail", RUNTIME_PATH.name, None),
    ]


# --------------------------------------------------------------------------- verification

def run_verify(stage: Path, page_path: Path, timeout: float) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Open the staged page in headless Chromium (node + playwright). Returns (summary, checks); the
    summary status is SKIPPED when node, the playwright package or its browser are missing."""
    node = shutil.which("node")
    if node is None:
        return {"status": "SKIPPED", "reason": "node is not on PATH"}, [_check("browser-verify", "skipped", "node missing")]
    config = {"url": page_path.resolve().as_uri(), "screen": str(stage / SCREEN_NAME), "debug": str(stage / DEBUG_NAME),
              "timeoutMs": int(timeout * 800)}
    env = {**os.environ, "FORGE_SCENE_VERIFY": json.dumps(config)}
    try:
        completed = subprocess.run([node, "-"], input=_VERIFY_SCRIPT, capture_output=True, encoding="utf-8",
                                   errors="replace", env=env, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        reason = f"timed out after {timeout:g} s"
        return {"status": "fail", "reason": reason}, [_check("browser-verify", "fail", reason, timeout)]
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    try:
        result = json.loads(lines[-1]) if lines else None
    except json.JSONDecodeError:
        result = None
    if not isinstance(result, dict):
        detail = (completed.stderr or completed.stdout or "no output").strip().splitlines() or ["no output"]
        reason = forge_core.ascii_text(detail[-1][:300])
        return {"status": "fail", "reason": reason}, [_check("browser-verify", "fail", reason)]
    if "skipped" in result:
        reason = forge_core.ascii_text(str(result["skipped"]))
        return {"status": "SKIPPED", "reason": reason}, [_check("browser-verify", "skipped", reason)]
    errors = [*result.get("pageErrors", []), *result.get("consoleErrors", [])]
    errors += [message for message in (result.get("failure"), result.get("error")) if message]
    assets = result.get("assets") or {}
    keyboard = result.get("keyboard") or []
    keyboard_ok = bool(keyboard) and any(item["free"] for item in keyboard) and all(
        (item["moved"] > 1e-6) == bool(item["free"]) for item in keyboard)
    routes = result.get("routes") or {}
    failing = [f"{item.get('target')}: {item.get('reason')}" for item in routes.get("results", []) if not item.get("ok")]
    failing += [f"spawn {item.get('id')} is blocked" for item in routes.get("spawns", []) if not item.get("valid")]
    failing += [f"portal {portal.get('id')}: arrival spawn {item.get('spawn')} "
                + ("is missing" if not item.get("found") else "lies inside the trigger")
                for portal in routes.get("portals", []) for item in portal.get("arrivals", [])
                if not item.get("found") or item.get("insideTrigger")]
    ysort = (result.get("ysort") or {}).get("status", "fail")
    checks = [
        _check("browser-page-errors", "pass" if not errors else "fail",
               [forge_core.ascii_text(str(error))[:300] for error in errors], []),
        _check("browser-network-requests", "pass" if not result.get("requests") else "fail", result.get("requests", []), []),
        _check("browser-assets-loaded", "pass" if assets and assets.get("loaded") == assets.get("total")
               and not assets.get("failed") else "fail", assets, None),
        _check("browser-keyboard-walk", "pass" if keyboard_ok else "fail", keyboard, None),
        _check("browser-routes", "pass" if routes.get("ok") else "fail", failing, []),
        _check("browser-y-sort", {"pass": "pass", "skipped": "skipped"}.get(ysort, "fail"), result.get("ysort"), None),
    ]
    status = "fail" if any(check["status"] == "fail" for check in checks) else "pass"
    checks.insert(0, _check("browser-verify", status, forge_core.ascii_text(str(result.get("browser", ""))), None))
    if isinstance(result.get("snapshot"), dict):
        forge_core.write_json(stage / SNAPSHOT_NAME, result["snapshot"])
    summary = {"status": status, "browser": result.get("browser"), "routesOk": bool(routes.get("ok")),
               "routes": len(routes.get("results", [])), "failing": failing, "ysort": ysort}
    return summary, checks


# --------------------------------------------------------------------------- command line

def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="build_scene_preview.py",
        description="Build preview.html, a single-file playable preview of a map_bundle.v2 scene: data-URI art, "
                    "the inlined map-runtime.mjs collision walker, Y-sort, a debug overlay and a window.__scene "
                    "snapshot with a route check. Writes a new --output-dir only after QA passes.",
        epilog="examples:\n"
               '  python "<skill-dir>/scripts/build_scene_preview.py" --bundle map/map_bundle.json '
               "--output-dir map/qa/preview\n"
               '  python "<skill-dir>/scripts/build_scene_preview.py" --bundle map/map_bundle.json '
               "--prop-pack assets/props/prop-pack.json --output-dir map/qa/preview-2 --verify --strict",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bundle", required=True, type=Path,
                        help="map_bundle.v2 JSON (a v1 bundle with world and collision also works).")
    parser.add_argument("--output-dir", required=True, type=Path,
                        help="New folder for preview.html and preview-qa.json; it must not exist.")
    parser.add_argument("--prop-pack", action="append", default=[], type=Path, metavar="MANIFEST",
                        help="prop-pack.json whose labels name the objects' prop (repeatable). Object art is "
                             "looked up in this order: object image, prop pack label, occluder source.")
    parser.add_argument("--spawn", help="Spawn id where the actor starts (default: the first spawn).")
    parser.add_argument("--speed", type=float,
                        help="Walking speed in world px per second (default: 12 x actorRadius, at least 60).")
    parser.add_argument("--actor-height", type=float,
                        help="Height of the debug actor marker in world px (default: 4.5 x actorRadius, at least 12).")
    parser.add_argument("--zoom", type=int,
                        help=f"Integer canvas zoom 1-{MAX_ZOOM} (default: the largest of 1-4 that keeps the world "
                             f"within {AUTO_ZOOM_SPAN} px).")
    parser.add_argument("--title", help="Page title (default: the bundle's name or title, else its file name).")
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES,
                        help=f"Fail when preview.html is larger (default {DEFAULT_MAX_BYTES}).")
    parser.add_argument("--verify", action="store_true",
                        help="Open the page in headless Chromium through node and the playwright npm package "
                             "(set NODE_PATH to its node_modules when it is not installed in this project): keyboard "
                             "walk, route check, Y-sort probe, no page errors or requests, screenshots and "
                             "scene-snapshot.json. Prints SKIPPED when node, playwright or Chromium are missing.")
    parser.add_argument("--verify-timeout", type=float, default=120.0,
                        help="Seconds allowed for --verify (default 120).")
    parser.add_argument("--strict", action="store_true",
                        help="Warnings and failed --verify checks become errors; nothing is published.")
    return parser


def _defaults(args: argparse.Namespace, actor_radius: float, size: tuple[int, int]) -> tuple[float, float, int]:
    speed = args.speed if args.speed is not None else max(60.0, 12.0 * actor_radius)
    height = args.actor_height if args.actor_height is not None else max(12.0, 4.5 * actor_radius)
    zoom = args.zoom if args.zoom is not None else max(1, min(4, AUTO_ZOOM_SPAN // max(size)))
    if not (math.isfinite(speed) and speed > 0):
        raise ValueError("--speed must be a positive number.")
    if not (math.isfinite(height) and height > 0):
        raise ValueError("--actor-height must be a positive number.")
    if not 1 <= zoom <= MAX_ZOOM:
        raise ValueError(f"--zoom must be an integer from 1 to {MAX_ZOOM}.")
    return speed, height, zoom


def build_preview(args: argparse.Namespace) -> dict[str, Any]:
    """Compile the scene, assemble and check the page, optionally verify it, publish; returns the summary."""
    if args.max_bytes < 1 or not (math.isfinite(args.verify_timeout) and args.verify_timeout > 0):
        raise ValueError("--max-bytes and --verify-timeout must be positive.")
    final = args.output_dir.parent.resolve() / args.output_dir.name
    if os.path.lexists(final):
        raise FileExistsError(f"Refusing to replace existing output: {args.output_dir}")
    bundle_path = args.bundle.resolve()
    if not bundle_path.is_file():
        raise FileNotFoundError(f"No such bundle: {args.bundle}")
    build = Build(bundle_path, final)
    try:
        bundle = _object(json.loads(build.read(bundle_path, "bundle").decode("utf-8-sig")), "bundle")
    except json.JSONDecodeError as error:
        raise ValueError(f"{bundle_path.name} is not valid JSON (line {error.lineno}).") from None
    schema = bundle.get("schema")
    if schema not in BUNDLE_SCHEMAS:
        raise ValueError(f"{bundle_path.name} is not a map bundle (schema {schema!r}; "
                         f"expected {' or '.join(BUNDLE_SCHEMAS)}).")
    world = _object(bundle.get("world"), "world")
    world_w = _number(world.get("width"), "world.width", positive=True)
    world_h = _number(world.get("height"), "world.height", positive=True)
    size = (math.ceil(world_w), math.ceil(world_h))
    collision = clean_collision(bundle.get("collision"))
    spawns = clean_spawns(bundle.get("spawns"))
    if not spawns:
        raise ValueError("The bundle has no spawns; the preview starts the actor at a spawn.")
    spawn_ids = [spawn["id"] for spawn in spawns]
    start = args.spawn or spawn_ids[0]
    if start not in spawn_ids:
        raise ValueError(f"--spawn {start!r} is not a spawn id ({', '.join(spawn_ids)}).")
    portals = clean_portals(bundle.get("portals"))
    for portal in portals:
        for spawn in portal.get("entranceByFrom", {}).values():
            if spawn not in spawn_ids:
                build.warn(f"portal {portal['id']}: entranceByFrom names spawn {spawn!r}, which is not in this map.")
    interactions = clean_interactions(bundle.get("interactions"))
    anchors = clean_anchors(bundle.get("anchors"))
    speed, actor_height, zoom = _defaults(args, collision["actorRadius"], size)
    packs = PropPacks(build, bundle, args.prop_pack)
    runtime_objects, drawn = compile_objects(bundle, build, packs)
    layers = compile_layers(bundle, build, size, bool(drawn))
    material_grid, material_info = compile_material_grid(bundle, build, (world_w, world_h))
    runtime, runtime_sha, runtime_version = read_runtime()
    title = args.title or next((bundle[key] for key in ("name", "title") if isinstance(bundle.get(key), str)
                                and bundle[key]), bundle_path.stem)
    scene = {
        "title": title, "source": {"file": bundle_path.name, "sha256": build.inputs[0]["sha256"], "schema": schema},
        "world": {"width": world_w, "height": world_h}, "zoom": zoom, "speed": speed, "start": start,
        "actor": {"height": actor_height}, "layers": layers, "objects": drawn,
        "bundle": {"world": {"width": world_w, "height": world_h}, "collision": collision, "objects": runtime_objects,
                   "portals": portals, "spawns": spawns, "interactions": interactions, "anchors": anchors},
        "materialGrid": material_grid,
    }
    subtitle = (f"{world_w:g} x {world_h:g} px | objects {len(drawn)} | exits {len(portals)} | "
                f"spawns {len(spawns)} | actor radius {collision['actorRadius']:g}")
    scene_json, images_json = script_json(scene), script_json(build.images)
    page = render_page(title, subtitle, runtime, scene_json, images_json)
    data = page.encode("ascii", "replace")
    checks = page_checks(page, data, runtime, (title, subtitle), scene_json, images_json, args.max_bytes)
    gates = [f"{check['id']} {check['value']}" for check in checks if check["status"] == "fail"]
    if gates:
        raise QAFailure("; ".join(forge_core.ascii_text(gate)[:300] for gate in gates))
    art = {source: sum(1 for item in drawn if item["art"] == source)
           for source in ("object.image", "prop-pack", "occluder.source")}
    missing_art = [item["id"] for item in drawn if item["image"] is None]
    checks.append(_check("object-art", "pass" if not missing_art else "warn", missing_art, []))

    with forge_core.staged_output(final) as stage:
        page_path = stage / PREVIEW_NAME
        page_path.write_bytes(data)
        verify: dict[str, Any] = {"status": "not-requested"}
        if args.verify:
            verify, verify_checks = run_verify(stage, page_path, args.verify_timeout)
            checks.extend(verify_checks)
            if verify["status"] == "SKIPPED":
                print(f"verify: SKIPPED ({verify['reason']})", file=sys.stderr)
        checks.append(_check("build-warnings", "pass" if not build.warnings else "warn", len(build.warnings), 0))
        statuses = {check["status"] for check in checks}
        status = "fail" if "fail" in statuses else "warn" if "warn" in statuses else "pass"
        if args.strict and status != "pass":
            failed = ", ".join(check["id"] for check in checks if check["status"] in ("fail", "warn"))
            first = f"; first warning: {build.warnings[0]}" if build.warnings else ""
            raise QAFailure(f"--strict: {failed} did not pass{first}")
        outputs = [_local_file_ref(stage / name, stage)
                   for name in (PREVIEW_NAME, SNAPSHOT_NAME, SCREEN_NAME, DEBUG_NAME) if (stage / name).is_file()]
        report = {
            "schema": REPORT_SCHEMA, "status": status,
            "method": "build_scene_preview: bundle files resolved relative to the bundle and checked against their "
                      "sha256 when given; tiles layers pre-rendered from tileset manifests; art embedded as data "
                      "URIs; map-runtime.mjs inlined verbatim; the page scanned for anything that loads from "
                      "outside it; size gate; optional headless-Chromium run (--verify).",
            "notProven": NOT_PROVEN, "checks": checks, "inputs": build.inputs, "outputs": outputs, "tool": TOOL,
            "preview": {
                "file": PREVIEW_NAME, "bytes": len(data), "sha256": forge_core.sha256_bytes(data),
                "runtime": {"file": RUNTIME_PATH.name, "version": runtime_version, "sha256": runtime_sha},
                "world": [world_w, world_h], "zoom": zoom, "speed": speed, "actorHeight": actor_height,
                "actorRadius": collision["actorRadius"], "ySquash": collision.get("ySquash", 1), "start": start,
                "layers": [{key: layer[key] for key in ("name", "source", "size") if key in layer} for layer in layers],
                "drawOrder": [item["id"] for item in drawn], "objectArt": {**art, "missing": missing_art},
                "counts": {"objects": len(drawn), "portals": len(portals), "spawns": len(spawns),
                           "interactions": len(interactions), "anchors": len(anchors)},
                "images": {"count": len(build.images), "bytes": build.image_bytes},
                "materialMap": material_info,
                "hashes": {"checked": build.hashes_checked, "unchecked": build.hashes_missing},
            },
            "warnings": build.warnings, "verify": verify,
        }
        forge_core.write_json(stage / REPORT_NAME, report)
    return {"status": status, "output_dir": str(final), "preview": str(final / PREVIEW_NAME),
            "metadata": str(final / REPORT_NAME), "bytes": len(data), "verify": verify["status"],
            "warnings": len(build.warnings)}


def main(argv: list[str] | None = None) -> int:
    forge_core.utf8_stdio()
    args = build_parser().parse_args(argv)
    try:
        summary = build_preview(args)
    except QAFailure as error:
        print(f"error: QA failed, nothing published: {forge_core.ascii_text(error)}", file=sys.stderr)
        return 1
    except (ValueError, OSError) as error:
        print(f"error: {forge_core.ascii_text(error)}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
