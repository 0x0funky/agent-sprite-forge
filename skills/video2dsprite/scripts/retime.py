#!/usr/bin/env python3
"""Retime selected source frames into a playback timeline (forge-frame-selection/v2).

Choose the played source frames with exactly one of:
  --selection FILE     a v1/v2 frame selection (for example from gait_loop select)
  --spans SPEC         comma list: a:b (end exclusive), a:b:step (negative step reverses),
                       k (one frame) or k*n (frame k held n times)
  --range A:B          [A, B) evenly sampled to --max-frames N (default: every frame)
  --map S/P@MS/E       three-key time map: source position S at 0 ms, P at MS ms (the
                       impact), E at the end; positions are 0-based frames, or seconds with
                       an 's' suffix. Needs --duration; samples at --output-fps.

Timing (integer ms that sum exactly): source timing by default, or --duration MS,
--ticks N|t1,t2,... (60 Hz rows; writes in/hit/end events), or --stride and --speed for
walks (cadence = 1000 x stride / speed per cycle). --impact-source and --hold-source put
impactMs/holdMs on the output frame nearest that source frame. Walks never ping-pong and
strikes never recover by playing frames backwards. A contact frame is suggested for gaits
(widest ground contact) and actions (largest reach). Output stays
'selected-needs-visual-review'.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import re
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any, Sequence

import numpy as np

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import forge_core  # noqa: E402  (this skill's vendored copy)
import gait_loop  # noqa: E402  (selection I/O and frame statistics)

RETIME_VERSION = "1"
GAIT_KINDS = ("gait", "walk", "run")
AMBIENT_KINDS = ("idle", "hover")
STRIKE_KINDS = ("attack", "cast")
OTHER_KINDS = ("hurt", "guard", "victory", "defeat", "fx", "other")
KINDS = GAIT_KINDS + AMBIENT_KINDS + STRIKE_KINDS + OTHER_KINDS
EVENT_NAMES = ("in", "tell", "hit", "active_end", "cancel", "chain", "impact", "hold", "end", "sfx",
               "step_l", "step_r")
TICK_HZ = 60
_MAP = re.compile(r"^\s*([0-9.]+s?)\s*/\s*([0-9.]+s?)\s*@\s*([0-9]+)\s*/\s*([0-9.]+s?)\s*$")
_EVENT = re.compile(r"^\s*([A-Za-z_]+|custom:\S+)\s*@\s*([0-9]+)(t?)\s*$")


# --------------------------------------------------------------------------- frame choice

def parse_spans(text: str, count: int) -> list[int]:
    """Exact source indices from a spans string (see the module help)."""
    indices: list[int] = []
    for item in str(text).split(","):
        item = item.strip()
        try:
            if not item:
                raise ValueError
            if "*" in item:
                frame, _, repeat = item.partition("*")
                times = int(repeat)
                if times < 1:
                    raise ValueError
                values = [int(frame)] * times
            elif ":" in item:
                parts = [int(part) for part in item.split(":")]
                if len(parts) not in (2, 3) or (len(parts) == 3 and parts[2] == 0):
                    raise ValueError
                values = list(range(*parts))
                if not values:
                    raise ValueError(f"span {item!r} is empty: the end is exclusive; use a:b:-1 to play backwards")
            else:
                values = [int(item)]
        except ValueError as error:
            raise ValueError(str(error) if str(error) else
                             f"--spans item {item!r} must be a:b, a:b:step, k or k*n") from None
        indices.extend(values)
    bad = [i for i in indices if not 0 <= i < count]
    if bad:
        raise ValueError(f"--spans frames {bad[:5]} are outside the {count} source frames")
    return indices


def sample_range(start: int, end: int, count: int | None) -> list[int]:
    """[start, end) evenly sampled to ``count`` frames, first and last frame included."""
    frames = end - start
    if count is None or count >= frames:
        return list(range(start, end))
    if count < 1:
        raise ValueError("--max-frames must be at least 1")
    if count == 1:
        return [start]
    return [start + gait_loop.round_half_up(Fraction(k * (frames - 1), count - 1)) for k in range(count)]


def _map_position(token: str, fps: Fraction) -> Fraction:
    """A --map key in source frames; an 's' suffix means seconds at the source fps."""
    try:
        return Fraction(token[:-1]) * fps if token.endswith("s") else Fraction(token)
    except ValueError:
        raise ValueError(f"--map key {token!r} must be a number of frames, or seconds with an s suffix") from None


def three_key_map(text: str, duration: int, output_fps: Fraction, fps: Fraction, count: int) -> tuple[list[int], int, dict]:
    """Source indices of a three-key time map and the output frame of the impact.

    Output frame k starts at frame_durations(duration, n) edge k; the impact frame is the
    one whose start is nearest MS, and it shows the source position P. Positions between
    the keys are linear in output frames, rounded half up to source frames.
    """
    match = _MAP.match(str(text))
    if not match:
        raise ValueError("--map must be START/PEAK@MS/END, e.g. 16/40@350/138 or 0.65s/1.65s@350/5.75s")
    start, peak, end = (_map_position(match.group(i), fps) for i in (1, 2, 4))
    impact_ms = int(match.group(3))
    if not start <= peak <= end:
        raise ValueError("--map keys must not go backwards: START <= PEAK <= END")
    if end > count - 1:
        raise ValueError(f"--map END is source frame {float(end):.2f}, past the last frame {count - 1}")
    if not 0 <= impact_ms < duration:
        raise ValueError("--map impact ms must lie inside [0, --duration)")
    n = gait_loop.round_half_up(Fraction(duration) * output_fps / 1000)
    if n < 2:
        raise ValueError("--map needs at least 2 output frames: raise --duration or --output-fps")
    k = min(n - 1, gait_loop.round_half_up(Fraction(impact_ms * n, duration)))
    positions = []
    for i in range(n):
        if i <= k:
            position = start + (peak - start) * Fraction(i, k) if k else peak
        else:
            position = peak + (end - peak) * Fraction(i - k, n - 1 - k)
        positions.append(min(count - 1, max(0, gait_loop.round_half_up(position))))
    return positions, k, {"start": float(start), "peak": float(peak), "end": float(end), "impactMs": impact_ms,
                          "outputFrames": n, "impactFrame": k}


# --------------------------------------------------------------------------- timing

def scaled_edges(weights: Sequence[int], total: int) -> list[int]:
    """Cumulative edges for ``total`` split in proportion to ``weights`` (half-up rounding,
    exact sum; equal weights give forge_core.frame_durations)."""
    cumulative = np.concatenate([[0], np.cumsum(weights)]).astype(object)
    whole = int(cumulative[-1])
    return [(2 * int(c) * total + whole) // (2 * whole) for c in cumulative]


def durations_from_edges(edges: Sequence[int], unit: str) -> list[int]:
    durations = [b - a for a, b in zip(edges, edges[1:])]
    if any(d < 1 for d in durations):
        raise ValueError(f"{len(durations)} frames cannot each get at least one {unit} from that total")
    return durations


def tick_durations(ticks: Sequence[int], hz: int) -> tuple[list[int], list[int]]:
    """Integer ms per frame from per-frame ticks on the ``hz`` grid, and the tick edges."""
    edges = np.concatenate([[0], np.cumsum(ticks)]).astype(int).tolist()
    ms = [(2 * t * 1000 + hz) // (2 * hz) for t in edges]
    return [b - a for a, b in zip(ms, ms[1:])], edges


def parse_ticks(text: str, frames: int, weights: Sequence[int]) -> list[int]:
    """--ticks N (split over the frames in proportion to their current durations) or one
    tick count per frame."""
    try:
        values = [int(v) for v in str(text).split(",")]
    except ValueError:
        raise ValueError("--ticks must be a total tick count or one integer per frame") from None
    if len(values) == 1 and frames > 1:
        if values[0] < frames:
            raise ValueError(f"--ticks {values[0]} cannot give each of {frames} frames a tick")
        return durations_from_edges(scaled_edges(weights, values[0]), "tick")
    if len(values) != frames or any(v < 1 for v in values):
        raise ValueError(f"--ticks lists {len(values)} values for {frames} frames; each frame needs >= 1 tick")
    return values


def frame_starts(durations: Sequence[int]) -> list[int]:
    return np.concatenate([[0], np.cumsum(durations)]).astype(int).tolist()


def nearest_position(indices: Sequence[int], source: int, flag: str) -> int:
    """Output position whose source frame is nearest ``source`` (earliest on ties)."""
    if not min(indices) <= source <= max(indices):
        raise ValueError(f"{flag} {source} lies outside the selected source frames {min(indices)}-{max(indices)}")
    return min(range(len(indices)), key=lambda position: (abs(indices[position] - source), position))


# --------------------------------------------------------------------------- policy rules

def check_policy(kind: str, policy: str, indices: Sequence[int], impact: int | None) -> list[str]:
    """Raise for the two loop rules; return the rules that were applied."""
    backwards = [p for p in range(len(indices) - 1) if indices[p + 1] < indices[p]]
    if kind in GAIT_KINDS:
        if policy == "pingpong":
            raise ValueError("walks never ping-pong: a mirrored gait slides its feet backwards (moonwalk); "
                             "ship a full cycle (gait_loop select) with --policy cycle")
        if backwards:
            p = backwards[0]
            raise ValueError(f"a walk cycle plays forward: output frames {p}-{p + 1} go from source "
                             f"{indices[p]} back to {indices[p + 1]}")
        return ["walks never ping-pong", "walk frames play forward"]
    if kind in STRIKE_KINDS:
        if policy == "pingpong":
            raise ValueError("recoveries never reverse: pingpong would replay the strike backwards as its "
                             "recovery; export a one-shot that recovers forward and return to idle in the engine")
        late = [p for p in backwards if impact is None or p >= impact]
        if late:
            p = late[0]
            raise ValueError(f"recoveries never reverse: output frames {p}-{p + 1} play source {indices[p]} back to "
                             f"{indices[p + 1]}" + (" after the impact" if impact is not None else "")
                             + "; take later source frames for the recovery")
        return ["recoveries never reverse"]
    return []


# --------------------------------------------------------------------------- events and contact

def parse_event(text: str, tick_mode: bool) -> dict:
    """NAME@MS, or NAME@Nt (ticks) in tick mode."""
    match = _EVENT.match(str(text))
    if not match:
        raise ValueError(f"--event must be NAME@MS or NAME@Nt, got {text!r}")
    name, value, ticks = match.group(1), int(match.group(2)), bool(match.group(3))
    if name not in EVENT_NAMES and not name.startswith("custom:"):
        raise ValueError(f"event name {name!r} must be one of {', '.join(EVENT_NAMES)} or custom:<name>")
    if ticks:
        if not tick_mode:
            raise ValueError(f"--event {text}: tick times need --ticks")
        return {"name": name, "tick": value}
    return {"name": name, "atMs": value}


def place_event(event: dict, starts: Sequence[int], tick_edges: Sequence[int] | None, hz: int) -> dict:
    """Resolve an event to atMs (snapped to the tick grid in tick mode), its tick and its frame."""
    total = starts[-1]
    if tick_edges is not None:
        tick = event["tick"] if "tick" in event else gait_loop.round_half_up(Fraction(event["atMs"] * hz, 1000))
        if not 0 <= tick <= tick_edges[-1]:
            raise ValueError(f"event {event['name']} at tick {tick} is outside 0..{tick_edges[-1]}")
        at_ms = (2 * tick * 1000 + hz) // (2 * hz)
        return {"name": event["name"], "atMs": int(at_ms), "frame": _frame_at(tick_edges, tick), "tick": int(tick)}
    at_ms = event["atMs"]
    if not 0 <= at_ms <= total:
        raise ValueError(f"event {event['name']} at {at_ms} ms is outside 0..{total} ms")
    return {"name": event["name"], "atMs": int(at_ms), "frame": _frame_at(starts, at_ms)}


def _frame_at(edges: Sequence[int], value: int) -> int:
    """The frame showing at ``value`` on a timeline of frame ``edges`` (the last frame at the end)."""
    return min(len(edges) - 2, bisect.bisect_right(edges, value) - 1)


def suggest_contact(clip: gait_loop.Clip, kind: str, indices: Sequence[int], starts: Sequence[int]) -> dict | None:
    """Gait: the widest ground contact (legs spread, the contact pose). Actions: the largest
    reach beyond the first selected pose (the strike or recoil extreme). None otherwise."""
    if kind in AMBIENT_KINDS or kind == "fx":
        return None
    distinct = list(dict.fromkeys(indices))
    masks = {i: clip.rgba(i)[..., 3] > forge_core.BODY_ALPHA_THRESHOLD for i in distinct}
    body = float(np.nanmedian(clip.heights[distinct])) if np.isfinite(clip.heights[distinct]).any() else 0.0
    scores = {}
    if kind in GAIT_KINDS:
        band = max(2, gait_loop.round_half_up(gait_loop.BAND_FRACTION * body))
        for i, mask in masks.items():
            if mask.any():
                ground = forge_core.ground_row(mask)
                columns = np.nonzero(mask[max(0, ground - band):ground].any(axis=0))[0]
                scores[i] = float(columns[-1] - columns[0] + 1)
        method = "widest ground contact: column span of the alpha band above each frame's ground line"
    else:
        first = forge_core.subject_bbox(masks[distinct[0]])
        if first is None:
            return None
        for i, mask in masks.items():
            box = forge_core.subject_bbox(mask)
            if box is not None:
                scores[i] = float(max(first[0] - box[0], box[2] - first[2], first[1] - box[1]))
        method = "largest reach: how far the body box extends left, right or up beyond the first selected frame"
    if not scores or max(scores.values()) <= 0:
        return None
    first_position = {index: position for position, index in reversed(list(enumerate(indices)))}
    best = max(scores, key=lambda i: (scores[i], -first_position[i]))
    position = first_position[best]
    return {"outputFrame": position, "sourceIndex": int(best), "atMs": int(starts[position]),
            "score": scores[best], "method": method}


# --------------------------------------------------------------------------- command

def choose_frames(args: argparse.Namespace, clip: gait_loop.Clip, fps: Fraction) -> dict:
    """The played source frames from exactly one of --selection, --spans, --range or --map."""
    count = clip.count
    chosen = {"selection": None, "weights": None, "impact": None, "map": None}
    if args.selection:
        selection = gait_loop.read_selection(args.selection)
        gait_loop.verify_selection(selection, args.selection, args.frames_dir, clip)
        chosen.update(selection=selection, indices=list(selection["indices"]), weights=list(selection["durations"]),
                      source=f"selection {Path(args.selection).name}")
    elif args.spans:
        chosen.update(indices=parse_spans(args.spans, count), source=f"spans {args.spans}")
    elif args.range:
        start, end = gait_loop.parse_interval(args.range)
        if end > count:
            raise ValueError(f"--range ends after the last of {count} frames")
        indices = sample_range(start, end, args.max_frames)
        chosen.update(indices=indices, source=f"range {start}:{end}"
                      + (f" sampled to {len(indices)}" if args.max_frames else ""))
    else:
        if args.duration is None:
            raise ValueError("--map needs --duration (ms)")
        if args.impact_source is not None:
            raise ValueError("--map already places the impact at its PEAK; drop --impact-source")
        if args.ticks or args.stride is not None or args.speed is not None:
            raise ValueError("--map times its frames with --duration; drop --ticks/--stride/--speed")
        output_fps = gait_loop.parse_fps(args.output_fps) if args.output_fps else fps
        indices, impact, info = three_key_map(args.map, args.duration, output_fps, fps, count)
        chosen.update(indices=indices, impact=impact, map=info, source=f"three-key map {args.map} over "
                      f"{args.duration} ms at {gait_loop.fps_text(output_fps)} fps")
    if args.max_frames is not None and not args.range:
        raise ValueError("--max-frames applies to --range")
    return chosen


def timing(args: argparse.Namespace, kind: str, chosen: dict, fps: Fraction) -> dict:
    """Integer durations from one of --ticks, --stride/--speed or --duration; else the
    selection's durations or source timing. Uneven input durations keep their proportions."""
    n = len(chosen["indices"])
    base = chosen["weights"] or [1] * n
    flags = [flag for flag, value in (("--duration", args.duration if chosen["map"] is None else None),
                                      ("--ticks", args.ticks),
                                      ("--stride/--speed", args.stride is not None or args.speed is not None))
             if value]
    if len(flags) > 1:
        raise ValueError(f"choose one timing: {', '.join(flags)}")
    result = {"ticks": None, "tick_edges": None, "cadence": None}
    if args.ticks:
        result["ticks"] = parse_ticks(args.ticks, n, base)
        result["durations"], result["tick_edges"] = tick_durations(result["ticks"], args.tick_hz)
    elif args.stride is not None or args.speed is not None:
        if kind not in GAIT_KINDS:
            raise ValueError("--stride/--speed set a walk cadence; use them with --kind walk, run or gait")
        if (args.stride is None or args.speed is None or not (math.isfinite(args.stride) and math.isfinite(args.speed))
                or args.stride <= 0 or args.speed <= 0):
            raise ValueError("cadence needs both --stride and --speed as positive numbers (world units, units/s)")
        selection = chosen["selection"]
        cycles = args.cycles or (selection["raw"].get("cycles") if selection else None) or 1
        if type(cycles) is not int or cycles < 1:
            raise ValueError("the gait cycle count must be a whole number >= 1 (--cycles)")
        exact = Fraction(str(args.stride)) * 1000 / Fraction(str(args.speed))
        total = gait_loop.round_half_up(exact * cycles)
        result["durations"] = durations_from_edges(scaled_edges(base, total), "ms")
        cadence_ms = Fraction(total, cycles)
        result["cadence"] = {"cadenceMs": int(cadence_ms) if cadence_ms.denominator == 1 else float(cadence_ms),
                             "strideWorldUnits": args.stride, "speedRef": args.speed, "cycles": cycles,
                             "exactCadenceMs": float(exact)}
    elif args.duration is not None:
        if args.duration < n:
            raise ValueError(f"--duration {args.duration} ms cannot give each of {n} frames 1 ms")
        result["durations"] = durations_from_edges(scaled_edges(base, args.duration), "ms")
    else:
        result["durations"] = list(chosen["weights"]) if chosen["weights"] else gait_loop.source_durations(n, fps)
    result["starts"] = frame_starts(result["durations"])
    return result


def build_events(args: argparse.Namespace, timeline: dict, impact: int | None, hold: int | None) -> list[dict]:
    """hit (impact) and hold events; tick mode adds in and end (unless --event end@...);
    plus every --event. Sorted by time."""
    edges, starts = timeline["tick_edges"], timeline["starts"]
    events = []
    if edges is not None:
        events.append({"name": "in", "tick": 0})
    for name, position in (("hit", impact), ("hold", hold)):
        if position is not None:
            events.append({"name": name, "tick": edges[position]} if edges is not None
                          else {"name": name, "atMs": starts[position]})
    custom = [parse_event(text, edges is not None) for text in args.event]
    if edges is not None and not any(event["name"] == "end" for event in custom):
        events.append({"name": "end", "tick": edges[-1]})
    placed = [place_event(event, starts, edges, args.tick_hz) for event in events + custom]
    return sorted(placed, key=lambda event: (event["atMs"], event["name"]))


def retime(args: argparse.Namespace) -> dict:
    out = Path(args.output_dir)
    fps = gait_loop.parse_fps(args.fps)
    if args.tick_hz < 1:
        raise ValueError("--tick-hz must be at least 1")
    if args.cycles is not None and args.cycles < 1:
        raise ValueError("--cycles must be at least 1")
    clip = gait_loop.load_clip(Path(args.frames_dir), fps, 1, allow_blank=True)
    chosen = choose_frames(args, clip, fps)
    selection, indices = chosen["selection"], chosen["indices"]
    kind = args.kind or (selection["raw"].get("kind") if selection else None)
    if kind not in KINDS:
        raise ValueError(f"--kind is required (one of {', '.join(KINDS)})")
    timeline = timing(args, kind, chosen, fps)
    durations, starts, ticks = timeline["durations"], timeline["starts"], timeline["ticks"]
    total = starts[-1]
    impact = (nearest_position(indices, args.impact_source, "--impact-source") if args.impact_source is not None
              else chosen["impact"])
    hold = nearest_position(indices, args.hold_source, "--hold-source") if args.hold_source is not None else None
    default_policy = "cycle" if kind in GAIT_KINDS or kind in AMBIENT_KINDS else "oneshot"
    policy = args.policy or (selection["policy"] if selection and selection["policy"] else default_policy)
    rules = check_policy(kind, policy, indices, impact)
    events = build_events(args, timeline, impact, hold)
    contact = suggest_contact(clip, kind, indices, starts)

    extra: dict[str, Any] = {}
    if impact is not None:
        extra["impactMs"] = int(starts[impact])
    if hold is not None:
        extra["holdMs"] = int(starts[hold])
    if timeline["cadence"]:
        extra.update({key: timeline["cadence"][key] for key in ("cadenceMs", "strideWorldUnits", "speedRef", "cycles")})
    elif selection and selection["raw"].get("cycles"):
        extra["cycles"] = selection["raw"]["cycles"]
    if ticks is not None:
        extra.update({"ticks": ticks, "tickHz": args.tick_hz})
    if contact:
        extra["suggestedContact"] = contact
    method = f"retime: {chosen['source']}; {len(indices)} frames, {total} ms" + (
        f" on {args.tick_hz} Hz ticks" if ticks is not None else "")
    review = ("Check the retimed clip at speed: the impact and hold frames, the recovery and the contact frame are "
              "pixel suggestions, not approval.")
    with forge_core.staged_output(out) as stage:
        document = gait_loop.build_selection(clip, stage, indices, durations, policy=policy, method=method,
                                             review=review, kind=kind, events=events, extra=extra)
        gait_loop.write_selection_files(stage, {"selection.json": document})
        rows = [{"frame": p, "sourceIndex": int(i), "startMs": int(starts[p]), "durationMs": int(durations[p]),
                 **({"ticks": int(ticks[p]), "startTick": int(timeline["tick_edges"][p])} if ticks is not None else {})}
                for p, i in enumerate(indices)]
        checks = [{"id": "durations-sum", "status": "pass", "value": total, "threshold": total},
                  {"id": "integer-durations", "status": "pass", "value": min(durations), "threshold": 1},
                  {"id": "policy-rules", "status": "pass", "value": rules or ["none for this kind"]},
                  {"id": "visual-review", "status": "needs-visual-review", "value": None}]
        if args.impact_source is not None:
            checks.insert(2, {"id": "impact-nearest-source", "status": "pass",
                              "value": abs(indices[impact] - args.impact_source)})
        report = {"schema": "video2dsprite.retime_report.v1",
                  "tool": {"name": "retime", "version": RETIME_VERSION},
                  "frames": {"directory": gait_loop.directory_ref(clip.directory, stage), "count": clip.count,
                             "fps": gait_loop.fps_text(fps)},
                  "kind": kind, "policy": policy, "source": chosen["source"], "map": chosen["map"],
                  "cadence": timeline["cadence"], "timeline": rows, "events": events, "suggestedContact": contact,
                  "rules": rules,
                  "qa": gait_loop.qa_envelope("needs-visual-review", method, checks,
                                              gait_loop.frame_refs(clip, stage, indices),
                                              [gait_loop.file_ref(stage / "selection.json", stage)],
                                              tool={"name": "retime", "version": RETIME_VERSION})}
        gait_loop.write_selection_files(stage, {"retime-report.json": report})
    summary = {"output": str(out), "selection": str(out / "selection.json"), "metadata": str(out / "retime-report.json"),
               "frames": len(indices), "durationMs": total, "policy": policy}
    if impact is not None:
        summary["impactMs"] = int(starts[impact])
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--frames-dir", type=Path, required=True, help="RGBA PNG frames on one canvas")
    parser.add_argument("--fps", required=True, help="source frame rate: a number or N/D (24, 24000/1001)")
    parser.add_argument("--output-dir", type=Path, required=True, help="new directory; refused if it exists")
    parser.add_argument("--kind", choices=KINDS,
                        help="motion kind (default: the selection's kind): sets the default policy and the rules")
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--selection", type=Path, help="frame selection v1 or v2")
    choice.add_argument("--spans", help="a:b, a:b:step, k, k*n items, comma separated (0-based, end exclusive)")
    choice.add_argument("--range", help="0-based start:endExclusive, sampled with --max-frames")
    choice.add_argument("--map", help="three-key time map START/PEAK@MS/END (frames, or seconds with s)")
    parser.add_argument("--max-frames", type=int, help="--range: evenly sample this many frames")
    parser.add_argument("--duration", type=int, help="total output duration in ms")
    parser.add_argument("--output-fps", help="--map: output sample rate (default --fps)")
    parser.add_argument("--ticks", help=f"total ticks, or one tick count per frame, at --tick-hz (default {TICK_HZ})")
    parser.add_argument("--tick-hz", type=int, default=TICK_HZ, help=f"tick grid (default {TICK_HZ})")
    parser.add_argument("--stride", type=float, help="walks: world units travelled per cycle")
    parser.add_argument("--speed", type=float, help="walks: world units per second at this cadence")
    parser.add_argument("--cycles", type=int, help="gait cycles in the selection (default: the selection's, else 1)")
    parser.add_argument("--impact-source", type=int, help="source frame of the impact (hit) -> impactMs")
    parser.add_argument("--hold-source", type=int, help="source frame of the held pose -> holdMs")
    parser.add_argument("--event", action="append", default=[], metavar="NAME@MS",
                        help="extra event at ms, or NAME@Nt in ticks (repeatable), e.g. cancel@12t")
    parser.add_argument("--policy", choices=gait_loop.LOOP_POLICIES,
                        help="cycle | pingpong | oneshot (default: cycle for walks and idles, oneshot otherwise)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    forge_core.utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        summary = retime(args)
    except (ValueError, OSError) as error:
        print(forge_core.ascii_text(f"error: {error}"), file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
