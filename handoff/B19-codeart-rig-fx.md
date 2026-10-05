# B19-codeart-rig-fx: skeletal code-art animation (FK, two-bone IK with ground, easing, vector and pixel routes) and code-drawn FX (six presets, hit-timed frames, fx.v1 runtime and verifier)

Branch `asf/B19-codeart-rig-fx` from `wip/asf-upgrade-20261005` @ 3f9252d. New files:

- Tools: [rig_animate.py](../skills/codeart2d/scripts/rig_animate.py), [fx_build.py](../skills/codeart2d/scripts/fx_build.py), [fx_verify.mjs](../skills/codeart2d/scripts/fx_verify.mjs), runtime template [fx-template.mjs](../skills/codeart2d/references/runtime/fx-template.mjs).
- Examples: [hero.rig.svg](../skills/codeart2d/examples/hero.rig.svg), [hero.anim.json](../skills/codeart2d/examples/hero.anim.json), [slash.fx.json](../skills/codeart2d/examples/slash.fx.json).
- References: [rig-animation.md](../skills/codeart2d/references/rig-animation.md), [fx-language.md](../skills/codeart2d/references/fx-language.md), [fx-runtime-contract.md](../skills/codeart2d/references/fx-runtime-contract.md).
- Tests: [test_codeart2d_rig.py](../tests/test_codeart2d_rig.py) (39), [test_codeart2d_fx.py](../tests/test_codeart2d_fx.py) (15), [test_fx_verify_js.py](../tests/test_fx_verify_js.py) (6, marker node), [tests/js/fx-verify.test.mjs](../tests/js/fx-verify.test.mjs) (15 node:test cases).

## Phase 3 integration status (branch asf/int-g-codeart)

Resolved in the codeart group fix pass (integration decisions cited as Dn):

- **D32, line endings.** `tests/js/fx-verify.test.mjs` normalises CRLF to LF before it builds the mutants, so the node suite passes 15/15 on a `core.autocrlf=true` checkout (the one STD failure every earlier stage carried).
- **D33, malformed animations.** `ik` must be an object (a list or a string was an AttributeError), IK `bones` must be strings (lists were unhashable), `entry_reference` must be a clip name string. `transitions[].to` must name a clip of the animation and `entry_frame` must fit that clip (it was published unchecked; the codeart review's non-blocking note).
- **D27.** `main()` in rig_animate and fx_build runs through `forge_core.run_cli` (in-process callers too): anything unexpected is one `error: internal error (Type: message)` line, exit 1. The codeart reviewer's `fuzz_b19.py` (49 animation and 49 FX mutations, main() called in-process) reports 0 problems; `test_fuzzed_animations_never_escape_main` and `test_fuzzed_specs_never_escape_main` keep it that way.
- **D11.** `--clips-schema` defaults to `v2` in both tools, so events reach the compiled `events_ms` and `sampling`, `pixel_art` and `art_source` reach the compiled clips (verified with the integrated B02 builder); `v1` stays for older builders.
- **D28.** Animation and FX specs are read through `forge_core.read_json(strict=True)`: a UTF-8 BOM is accepted; NaN, infinity and duplicate keys are refused.
- **D29.** `TOOL_VERSION` is `forge_core.FORGE_PACKAGE_VERSION` (`0.4.0`) in rig-report, fx-report and codeart-meta envelopes.
- **D30.** `output_ref` / `input_ref` are `forge_core.file_ref`.
- **Codeart review, non-blocking.** `--help` works on a machine without numpy (the modules record the import error and `main()` prints the pip command after argparse); `fx_build` checks that `routes` is a list of names.
- **Schemas.** Section 5's requests are applied (S1, f3d7eb2); the tests validate rig_anim_v1, rig_report_v1, fx_v1, fx_report_v1 and the presets against the real vendored schemas, and the in-memory `CODEART_ADDITIONS` / `FX_ADDITIONS` are gone.

## 1. CLIs

Run from the user's project root; outputs go to a new folder in the project. In Claude Code `<skill-dir>` is `${CLAUDE_SKILL_DIR}`.

    python "<skill-dir>/scripts/rig_animate.py" --rig hero.rig.svg --anim hero.anim.json --output-dir out/hero-v1 --route pixel --build-clips --strict-qc
    python "<skill-dir>/scripts/rig_animate.py" --anim hero.anim.json --output-dir out/hero-hd --route vector --zoom 4 --build-clips
    python "<skill-dir>/scripts/rig_animate.py" --anim hero.anim.json --output-dir out/hero-godot --clips walk,idle --godot-world-height 1.8
    python "<skill-dir>/scripts/fx_build.py" --spec slash.fx.json --output-dir out/fx-slash-v1 --route pixel --build-clips --export-runtime --strict-qc
    python "<skill-dir>/scripts/fx_build.py" --spec slash.fx.json --output-dir out/fx-hd --route vector --zoom 4 --effects slash,impact
    node "<skill-dir>/scripts/fx_verify.mjs" out/fx-slash-v1/fx-runtime.mjs --report out/fx-slash-v1/fx-verify-2.json

- Both Python CLIs: argparse, `utf8_stdio()` first, `--output-dir` refused when it exists, work staged with `forge_core.staged_output` and published only after QA; `--strict-qc` publishes nothing when a check fails. Errors: `error: ...` on stderr, exit 1, no traceback (D27, `forge_core.run_cli`); argument errors are argparse usage errors, exit 2. `--help` works before numpy is installed. Success: one ASCII JSON line with `output`, `metadata` (codeart-meta.json), `report` (rig-report.json or fx-report.json), the clips or effects, the stored frame count, the route (and `runtime` for fx_build).
- `--help` is ASCII and works under cp1252 and cp950 (tested with `assert_cli_help`).
- `fx_verify.mjs` is a Node 18+ CLI with no packages: `--help` prints ASCII usage; one JSON line `{status, module, report, effects, failed, warned}`; exit 1 when a check fails (also `error: fx.v1 verification failed: <ids>` on stderr); `--report` writes a common qaEnvelope and refuses an existing file (it writes the report also when checks fail, because the report is the evidence).
- Integration e2e (plan Appendix I, code art): `rig_animate.py --anim examples/hero.anim.json --output-dir <tmp>/hero --build-clips --strict-qc`, then export_engine on `<tmp>/hero/compiled-clips/animation-clips.json`; `fx_build.py --spec examples/slash.fx.json --output-dir <tmp>/fx --export-runtime --strict-qc` (it runs fx_verify.mjs itself when node is on PATH; `fx-verify.json` and the `runtime_verify` check record the result).

## 2. SKILL.md routing rows

codeart2d SKILL.md (Z):

| Need | Route |
|---|---|
| A small character (visible height up to 48 px, 49-64 px with consent) that walks, idles or attacks with exact palette and planted feet | Write a rig SVG and a `codeart2d.rig_anim.v1` animation (references/rig-animation.md), run `scripts/rig_animate.py ... --build-clips --strict-qc`, look at `review/*.png`, then hand `compiled-clips/` to export_engine. Never `generate2dsprite.py process`. |
| Slash, sparks, impact ring, hit flash, dust or projectile loop | Write a `codeart2d.fx.v1` spec (references/fx-language.md), run `scripts/fx_build.py ... --build-clips --export-runtime`; baked frames for pixel games, `fx-runtime.mjs` for gameplay-timed canvas FX (spawn at `hitAt - impactMs`, references/fx-runtime-contract.md). |
| Check an fx.v1 runtime (generated or hand-written) | `node scripts/fx_verify.mjs <module.mjs> --report <file>` |

generate2dsprite SKILL.md routing table, added in this release (roadmap P0-3 note, plan B19 doc notes):

| Need | Route |
|---|---|
| Skeletal code-art characters: several clips from one rig with IK-planted feet, no image model | codeart2d `rig_animate.py` (code-drawn; disclose it), then `build_animation_clips.py` with the produced clips.json |
| Game FX (slash, sparks, rings, flashes, dust, projectiles) | codeart2d first: `fx_build.py`; `fx.v1` runtime for gameplay-timed FX. Realistic fire, smoke or water: the video route |

Disclosure line for both tools (codeart_core already writes it into codeart-meta.json): "Code-drawn, no image model."

## 3. README tool-table rows

| Tool | What it does | Verified by |
|---|---|---|
| `codeart2d/scripts/rig_animate.py` | Poses an SVG rig with FK, two-bone IK and a ground constraint, eases keyframes (incl. cubic-bezier and spring), renders vector or exact-palette pixel frames, writes clips.json with events, stride and entry frame, a contact report, review sheets and optional Godot Sprite3D contracts | `tests/test_codeart2d_rig.py`: hero walk 8 + idle 4 on both routes (0 partial alpha, 0 off-palette, 0 outline gaps, at most 10 L-corners, seam 0.8-1.25, 0 px planted-foot drift in rig geometry and in pixels, build_animation_clips exit 0), one test per lint |
| `codeart2d/scripts/fx_build.py` | Bakes six FX presets with an exact palette, cuts a frame at the impact (hit event), drops empty tail frames, writes clips.json and the fx.v1 runtime | `tests/test_codeart2d_fx.py`: every preset builds, exact palette, the slash arc ends on the hit frame, tail merge, seeded particles |
| `codeart2d/scripts/fx_verify.mjs` | Checks an fx.v1 runtime: no randomness or clocks, no forbidden APIs, finite arguments, balanced state, nothing drawn outside the lifetime or box, deterministic, visible at impact, readability | `tests/js/fx-verify.test.mjs` (template passes; 11 mutants fail their own checks) and `tests/test_fx_verify_js.py` (generated slash passes, Math.random mutant fails, JS geometry equals the Python bake to 1e-9) |

## 4. CHANGELOG entries

- Added: `codeart2d/scripts/rig_animate.py`: SVG rigs (bones `<g data-pivot>`, slots with `data-z`, swap variants), FK (`world = parent * T(d) * T(p) * R * S * T(-p)`), two-bone IK with pole, targets or a stance/swing gait, ground constraint with exact rotated-shape extents and per-route outline allowance, clamp reports, easings `linear/sine/in/out/step/cubic-bezier(a,b,c,d)/spring(k,d)`, loop (t = i/n) and one-shot (t = i/(n-1)) sampling, vector route at integer zoom and pixel route D through `codeart_core.pixel_finish`, deduplicated frames, clips.json (events, stride, entry_frame, states, art_source code), rig-report.json (contacts, drift, clamps, seam reports, per-frame QA), codeart-meta.json with a QA envelope over every frame, review sheets with onion skins and a world-travel strip, optional `godot_sprite3d.v1` contracts, `--build-clips` (B19-T1).
- Added: rig lints that stop or fail a run: NaN, infinity and complex numbers (rig and animation), duplicate ids (clip id collisions) plus per-frame id prefixes, IK clamp, ground row, margins (B19-T2).
- Added: `codeart2d/scripts/fx_build.py`: presets slash, sparks, ring, flash, dust and projectile; ramps (brightest first), seeded particles from a 32-bit hash shared with the runtime, frames cut at `impactMs` with a `hit` event, mid-frame sampling, empty tail frames dropped and merged, exact-palette pixel route and vector route, fx-report.json, codeart-meta.json (B19-T3).
- Added: `--export-runtime` writes `fx-runtime.mjs` (the fx.v1 module: `render`, `spawnAt = hitAt - impactMs`, `shapes`, `effects`, `canvas`, `origin`) from `references/runtime/fx-template.mjs`; `scripts/fx_verify.mjs` checks fx.v1 modules (B19-T4).
- Added: references `rig-animation.md`, `fx-language.md`, `fx-runtime-contract.md` and examples `hero.rig.svg`, `hero.anim.json`, `slash.fx.json` (B19-T5).
- Changed: none (new tools). BREAKING: none.
- Fixed (roadmap findings, prototype to tool): planted-foot slide 2.43 px and 1.03 px sink of the design prototype rig are now 0 px (roadmap 4.8: IK plus ground constraint; measured on rig geometry and on the hero's boot pixels); Python complex values and NaN leaking into SVG geometry (fox probe `61.13-0.00j`, roadmap 4.4) are refused; clipPath id collisions between frames batched in one page (fox probe) cannot happen (unique ids enforced, per-frame prefixes); fully transparent FX tail frames that build_animation_clips refused (roadmap 4.11) are dropped with their time merged; prototype slot fills read only from `fill` attributes now follow the CSS cascade (classes, inheritance, `currentColor`).

## 5. Schema change requests

**Status: applied** by S1 (f3d7eb2); nothing is pending. The record of the requests follows.

All in `shared/schemas/codeart.schema.json` (A0), then `tools/vendor_sync.py --write` (vendored into `skills/codeart2d/references/schemas/`). Purely additive: new `$defs`, new optional properties, and a conditional `allOf` on FX primitives that only constrains the six preset types. A0's existing `codeart.rig_anim_v1.valid.json` and `codeart.fx_v1.valid.json` (placeholder primitive types `arc`, `particles`) still validate; `tests/test_codeart2d_rig.py` and `tests/test_codeart2d_fx.py` apply exactly this JSON in memory (`CODEART_ADDITIONS`, `FX_ADDITIONS`) and validate the examples, rig-report.json, fx-report.json and A0's fixtures against it.

Keys are JSON pointers into `codeart.schema.json`; "(add)" merges the members into that object, the `allOf` array is new.

```json
{
  "/$defs (add these defs)": {
    "ikGait": {
      "description": "Planted-foot gait for a loop clip with stride_world_units: the foot is planted for the stance fraction of the cycle and travels stride * stance against the body, so it stays still in the world; during the swing it lifts by lift px and rolls by roll degrees.",
      "type": "object",
      "properties": {
        "phase": {"type": "number"},
        "stance": {"type": "number", "minimum": 0.05, "maximum": 0.95},
        "lift": {"type": "number", "minimum": 0},
        "roll": {"type": "number"},
        "x": {"type": "number"}
      }
    },
    "ikChain": {
      "description": "Two-bone IK: bones [root, middle]; end is a child bone of middle (its pivot is the effector) or a rest point [x, y]; pole is the direction the middle joint bends toward; exactly one of target (canvas pixels) or gait; angle is the end bone's world angle; ground (default true) keeps the end bone's slots on the rig ground line.",
      "type": "object",
      "required": ["bones"],
      "properties": {
        "bones": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 2, "maxItems": 2},
        "end": {"anyOf": [{"type": "string", "minLength": 1}, {"$ref": "common.schema.json#/$defs/point2"}]},
        "pole": {"$ref": "common.schema.json#/$defs/point2"},
        "target": {
          "type": "array",
          "minItems": 1,
          "items": {
            "type": "array",
            "prefixItems": [{"type": "number", "minimum": 0, "maximum": 1}, {"$ref": "common.schema.json#/$defs/point2"}],
            "minItems": 2,
            "maxItems": 2
          }
        },
        "angle": {
          "type": "array",
          "minItems": 1,
          "items": {
            "type": "array",
            "prefixItems": [{"type": "number", "minimum": 0, "maximum": 1}, {"type": "number"}],
            "minItems": 2,
            "maxItems": 2
          }
        },
        "gait": {"$ref": "#/$defs/ikGait"},
        "ground": {"type": "boolean"},
        "ease": {"$ref": "#/$defs/easing"}
      },
      "oneOf": [{"required": ["target"]}, {"required": ["gait"]}]
    },
    "rig_report_v1": {
      "description": "rig-report.json written by rig_animate.py: per-clip frame records (QA metrics, ground edge, margin, IK contacts), seam reports, planted-foot drift, entry frame, stride and events, all in output pixels; qa is the envelope of codeart-meta.json.",
      "type": "object",
      "required": ["schema", "route", "frame_size", "anchor_px", "clips", "qa"],
      "properties": {
        "schema": {"const": "codeart2d.rig_report.v1"},
        "route": {"enum": ["pixel", "vector"]},
        "frame_size": {"$ref": "common.schema.json#/$defs/size2"},
        "anchor_px": {"$ref": "common.schema.json#/$defs/point2"},
        "ground_px": {"type": "number"},
        "zoom": {"type": "integer", "minimum": 1},
        "clips": {
          "type": "object",
          "minProperties": 1,
          "additionalProperties": {
            "type": "object",
            "required": ["frames", "loop", "duration_ms", "entry_frame", "contact"],
            "properties": {
              "frames": {
                "type": "array",
                "minItems": 1,
                "items": {
                  "type": "object",
                  "required": ["id", "t", "file", "stored_as", "qa", "ground"],
                  "properties": {"file": {"$ref": "common.schema.json#/$defs/relPath"}}
                }
              },
              "loop": {"type": "boolean"},
              "duration_ms": {"$ref": "common.schema.json#/$defs/durationsMs"},
              "seam": {"anyOf": [{"$ref": "common.schema.json#/$defs/seamReport"}, {"type": "null"}]},
              "entry_frame": {"type": "integer", "minimum": 0},
              "contact": {"type": "object"},
              "events": {"type": "array", "items": {"$ref": "sprite.schema.json#/$defs/clipEvent"}}
            }
          }
        },
        "ik_clamps": {"type": "array"},
        "ground_failures": {"type": "array"},
        "margin_failures": {"type": "array"},
        "qa": {"$ref": "common.schema.json#/$defs/qaEnvelope"}
      }
    },
    "fx_slash": {
      "type": "object",
      "required": ["type"],
      "properties": {
        "type": {"const": "slash"},
        "ramp": {"type": "string", "minLength": 1},
        "colors": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
        "center": {"$ref": "common.schema.json#/$defs/point2"},
        "radius": {"type": "number", "minimum": 0},
        "from": {"type": "number"},
        "to": {"type": "number"},
        "width": {"type": "number", "minimum": 0},
        "squash": {"type": "number", "minimum": 0},
        "trail": {"type": "number", "minimum": 0, "maximum": 1},
        "startMs": {"anyOf": [{"const": "impact"}, {"type": "number", "minimum": 0}]},
        "endMs": {"anyOf": [{"const": "impact"}, {"type": "number", "minimum": 0}]},
        "fadeMs": {"type": "number", "minimum": 0}
      },
      "additionalProperties": false
    },
    "fx_sparks": {
      "type": "object",
      "required": ["type"],
      "properties": {
        "type": {"const": "sparks"},
        "ramp": {"type": "string", "minLength": 1},
        "colors": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
        "origin": {"$ref": "common.schema.json#/$defs/point2"},
        "atMs": {"anyOf": [{"const": "impact"}, {"type": "number", "minimum": 0}]},
        "count": {"type": "integer", "minimum": 1, "maximum": 256},
        "angle": {"type": "number"},
        "spread": {"type": "number", "minimum": 0},
        "speed": {"type": "number", "minimum": 0},
        "lifeMs": {"type": "number", "minimum": 0},
        "length": {"type": "number", "minimum": 0},
        "width": {"type": "number", "minimum": 0},
        "gravity": {"type": "number"}
      },
      "additionalProperties": false
    },
    "fx_ring": {
      "type": "object",
      "required": ["type"],
      "properties": {
        "type": {"const": "ring"},
        "ramp": {"type": "string", "minLength": 1},
        "colors": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
        "origin": {"$ref": "common.schema.json#/$defs/point2"},
        "atMs": {"anyOf": [{"const": "impact"}, {"type": "number", "minimum": 0}]},
        "lifeMs": {"type": "number", "minimum": 0},
        "radius": {"$ref": "common.schema.json#/$defs/point2"},
        "width": {"$ref": "common.schema.json#/$defs/point2"},
        "squash": {"type": "number", "minimum": 0}
      },
      "additionalProperties": false
    },
    "fx_flash": {
      "type": "object",
      "required": ["type"],
      "properties": {
        "type": {"const": "flash"},
        "ramp": {"type": "string", "minLength": 1},
        "colors": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
        "origin": {"$ref": "common.schema.json#/$defs/point2"},
        "atMs": {"anyOf": [{"const": "impact"}, {"type": "number", "minimum": 0}]},
        "lifeMs": {"type": "number", "minimum": 0},
        "radius": {"type": "number", "minimum": 0},
        "rays": {"type": "integer", "minimum": 1, "maximum": 16},
        "rayLength": {"type": "number", "minimum": 0},
        "rotation": {"type": "number"}
      },
      "additionalProperties": false
    },
    "fx_dust": {
      "type": "object",
      "required": ["type"],
      "properties": {
        "type": {"const": "dust"},
        "ramp": {"type": "string", "minLength": 1},
        "colors": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
        "origin": {"$ref": "common.schema.json#/$defs/point2"},
        "atMs": {"anyOf": [{"const": "impact"}, {"type": "number", "minimum": 0}]},
        "count": {"type": "integer", "minimum": 1, "maximum": 256},
        "spread": {"type": "number", "minimum": 0},
        "rise": {"type": "number", "minimum": 0},
        "drift": {"type": "number"},
        "radius": {"$ref": "common.schema.json#/$defs/point2"},
        "lifeMs": {"type": "number", "minimum": 0}
      },
      "additionalProperties": false
    },
    "fx_projectile": {
      "type": "object",
      "required": ["type"],
      "properties": {
        "type": {"const": "projectile"},
        "ramp": {"type": "string", "minLength": 1},
        "colors": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
        "origin": {"$ref": "common.schema.json#/$defs/point2"},
        "angle": {"type": "number"},
        "radius": {"type": "number", "minimum": 0},
        "trail": {"type": "number", "minimum": 0},
        "orbiters": {"type": "integer", "minimum": 0, "maximum": 16},
        "periodMs": {"type": "number", "minimum": 0}
      },
      "additionalProperties": false
    },
    "fx_report_v1": {
      "description": "fx-report.json written by fx_build.py: per effect the frames (start, duration and sample ms, QA metrics, margin), durations, hit frame, dropped tail frames, events and loop seam; slash arc progress per frame; the build and runtime records; qa is the envelope of codeart-meta.json.",
      "type": "object",
      "required": ["schema", "route", "canvas", "origin", "effects", "qa"],
      "properties": {
        "schema": {"const": "codeart2d.fx_report.v1"},
        "route": {"enum": ["pixel", "vector"]},
        "canvas": {"$ref": "common.schema.json#/$defs/size2"},
        "origin": {"$ref": "common.schema.json#/$defs/point2"},
        "effects": {
          "type": "object",
          "minProperties": 1,
          "additionalProperties": {
            "type": "object",
            "required": ["frames", "duration_ms", "hit_frame", "events"],
            "properties": {
              "frames": {
                "type": "array",
                "minItems": 1,
                "items": {
                  "type": "object",
                  "required": ["id", "start_ms", "duration_ms", "sample_ms", "file", "qa"],
                  "properties": {"file": {"$ref": "common.schema.json#/$defs/relPath"}}
                }
              },
              "duration_ms": {"$ref": "common.schema.json#/$defs/durationsMs"},
              "hit_frame": {"anyOf": [{"type": "integer", "minimum": 0}, {"type": "null"}]},
              "events": {"type": "array", "items": {"$ref": "sprite.schema.json#/$defs/clipEvent"}},
              "seam": {"anyOf": [{"$ref": "common.schema.json#/$defs/seamReport"}, {"type": "null"}]}
            }
          }
        },
        "qa": {"$ref": "common.schema.json#/$defs/qaEnvelope"}
      }
    }
  },
  "/$defs/rigClip/properties (add)": {
    "ik": {"type": "object", "additionalProperties": {"$ref": "#/$defs/ikChain"}},
    "grounded": {"type": "boolean"},
    "entry_frame": {"type": "integer", "minimum": 0},
    "transitions": {"type": "array", "items": {"$ref": "sprite.schema.json#/$defs/clipTransition"}},
    "role": {"enum": ["player", "enemy", "npc", "fx", "prop"]}
  },
  "/$defs/rig_anim_v1/properties (add)": {
    "states": {"type": "object", "additionalProperties": {"type": "string", "minLength": 1}},
    "entry_reference": {"type": "string", "minLength": 1},
    "pixel": {
      "type": "object",
      "properties": {
        "ramps": {
          "type": "object",
          "additionalProperties": {
            "anyOf": [
              {
                "type": "array",
                "items": {"$ref": "common.schema.json#/$defs/hexColor"},
                "minItems": 5,
                "maxItems": 5
              },
              {
                "type": "object",
                "required": ["mid"],
                "properties": {
                  "hi": {"$ref": "common.schema.json#/$defs/hexColor"},
                  "mid": {"$ref": "common.schema.json#/$defs/hexColor"},
                  "lo": {"$ref": "common.schema.json#/$defs/hexColor"},
                  "dark": {"$ref": "common.schema.json#/$defs/hexColor"},
                  "out": {"$ref": "common.schema.json#/$defs/hexColor"}
                }
              }
            ]
          }
        },
        "light": {"anyOf": [{"$ref": "common.schema.json#/$defs/point2"}, {"type": "null"}]},
        "inner_lines": {"type": "boolean"},
        "outline": {"type": "string", "minLength": 1},
        "outline_mode": {"enum": ["solid", "selout", "none"]}
      }
    }
  },
  "/$defs/fx_v1/properties (add)": {
    "canvas": {"$ref": "common.schema.json#/$defs/size2"},
    "origin": {"$ref": "common.schema.json#/$defs/point2"}
  },
  "/$defs/fx_v1/properties/effects/items/properties (add)": {
    "frameMs": {"type": "integer", "minimum": 1},
    "loop": {"type": "boolean"},
    "outline": {"type": "string", "minLength": 1},
    "events": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["atMs", "name"],
        "properties": {"atMs": {"type": "integer", "minimum": 0}, "name": {"$ref": "common.schema.json#/$defs/eventName"}}
      }
    }
  },
  "/$defs/fx_v1/properties/effects/items/properties/primitives/items/allOf (add)": [
    {
      "if": {"properties": {"type": {"const": "slash"}}, "required": ["type"]},
      "then": {"$ref": "#/$defs/fx_slash"}
    },
    {
      "if": {"properties": {"type": {"const": "sparks"}}, "required": ["type"]},
      "then": {"$ref": "#/$defs/fx_sparks"}
    },
    {
      "if": {"properties": {"type": {"const": "ring"}}, "required": ["type"]},
      "then": {"$ref": "#/$defs/fx_ring"}
    },
    {
      "if": {"properties": {"type": {"const": "flash"}}, "required": ["type"]},
      "then": {"$ref": "#/$defs/fx_flash"}
    },
    {
      "if": {"properties": {"type": {"const": "dust"}}, "required": ["type"]},
      "then": {"$ref": "#/$defs/fx_dust"}
    },
    {
      "if": {"properties": {"type": {"const": "projectile"}}, "required": ["type"]},
      "then": {"$ref": "#/$defs/fx_projectile"}
    }
  ]
}
```

Reasons and consumers:

- `ikChain`, `ikGait`, `rigClip.ik/grounded/entry_frame/transitions/role`, `rig_anim_v1.states/entry_reference/pixel`: optional animation fields rig_animate.py reads (producer: Claude; consumer: B19). Undocumented today because `additionalProperties` is open.
- `rig_report_v1`, `fx_report_v1`: new document types written by rig_animate.py and fx_build.py (`schema` ids `codeart2d.rig_report.v1`, `codeart2d.fx_report.v1`); consumers: the agent, Z docs, integration e2e.
- `fx_v1.canvas/origin`, effect `frameMs/loop/outline/events`, `fx_slash` ... `fx_projectile`: the fields fx_build.py reads. The preset defs are closed (`additionalProperties: false`) because fx_build.py refuses unknown preset parameters; other primitive types stay open for hand-written runtimes.
- `fx-verify.json` needs no new def: it is a common `qaEnvelope` (plus `effects` and `findings`), tested with `assert_valid_contract(..., "common", "qaEnvelope")`.

Optional fields these producers add to existing contracts (open objects, listed so they do not drift):

- clips.json (`sprite/clips_input`): per clip `loop_policy`, `entry_frame`, `stride_world_units`, `stride_px_per_frame`, `events`, `transitions`, `role`; top level `sampling`, `pixel_art`, `art_source: "code"`, `placeholder: false`, `body_height_px`, `states`. All already defined in A0's schema; the file validates as is. The schema id is `generate2dsprite.animation_clips.v2` by default (D11; `--clips-schema v1` writes `.v1`).
- codeart-meta.json (`codeart/codeart_meta_v1`): top-level `route`, and `rig` plus `anim` (rig_animate) or `spec` (fx_build) as fileRefs; `renderer` adds `route`, and for the pixel route `coverage_ss`, `coverage`, `finish`.
- `godot/<clip>.sprite3d.json` follows `generate2dsprite.godot_sprite3d.v1` as built by `generate2dsprite.py` (no schema exists in `sprite.schema.json`) and adds `durations_ms` and `loop`; `frame_size` may be non-square. B09 owns engine exports; if it adds a `godot_sprite3d_v1` def, these two keys should be in it.

## 6. Shared-helper promotion requests

fx_build.py imports helpers from its sibling rig_animate.py (same skill, allowed), because B19 may not create a third module. Candidates to move at integration:

- Into codeart_core (A3 canonical, codeart2d only):
  - `Geometry`, `path_geometry(d)`, `shape_geometry(element)` (rig_animate.py:330, 506, 576): exact extents of SVG shapes under an affine map (rects with rounded corners, ellipses, arcs, cubic and quadratic Beziers). Useful for svg_render anchors and B20/B21 layout bounds. Tests: `test_shape_extent_is_exact_for_rotated_curves`.
  - `resolve_paint(chain, rules)` and its `_stylesheet` (rig_animate.py:685): computed fill, stroke, stroke width, opacity and visibility with the CSS cascade. Test: `test_paint_cascade_classes_inheritance_and_inline_style`.
  - `parse_ease`, `cubic_bezier`, `spring` (rig_animate.py:195, 114, 154): CSS-exact cubic-bezier and pinned-end spring; B07 retime could share them. Tests: `test_cubic_bezier_matches_a_dense_reference`, `test_spring_ends_exactly_and_damping_controls_overshoot`.
  - `run_clips_builder(builder, manifest, output)` (rig_animate.py:1682): the sibling build_animation_clips call; B18's render_pixelspec `--build-clips` needs the same.
  - `hash01`, `stream`, `triangle` (fx_build.py:68, 78, 218) if other code-art generators need seeded noise identical to the fx.v1 runtime.
- Into forge_core (A1 canonical):
  - `qa_envelope(checks, *, method, not_proven, inputs, outputs, tool)` and `check(id, passed, value, threshold, *, warn=False)` (rig_animate.py:1633, 1642): every QA writer builds these by hand today.
  - `output_ref(path, base)` and `input_ref(path, base)` (rig_animate.py:1618, 1624): fileRefs that never store an absolute path (an input on another drive records its file name), the rule A1's `portable_path` leaves to callers.
  - `margin_px(alpha)` and `ground_edge(alpha)` (rig_animate.py:1661, 1730), `dedupe_frames(rendered)` (rig_animate.py:1670).

No promotion is needed for the JS side: fx_verify.mjs is self-contained by design.

## 7. Cross-module links that Z must add

- codeart2d SKILL.md: link references/rig-animation.md, references/fx-language.md and references/fx-runtime-contract.md; the hello-sprite quickstart (B18) can point to the hero example for "make it walk".
- generate2dsprite character-animation.md (B02): the cutout/hybrid paragraph should point to codeart2d rig-animation.md in plain text (B02-T6 already plans this).
- generate2dsprite processing.md (B01): add a "Code-art frames" note: never run `process` on code art; frames are 8-bit RGBA on one shared canvas, empty tail frames are dropped with their time merged, repeated poses are reused by name (roadmap P1-1).
- build_animation_clips (B02): (a) done in Phase 3 (D11): both tools write `animation_clips.v2` by default, so events reach `events_ms`; the builder keeps accepting v1 manifests with v2 optional fields for `--clips-schema v1`. (b) The builder records `source_manifest.path` as an absolute path into the stage folder, so `compiled-clips/animation-clips.json` is not byte-deterministic and points at a folder that no longer exists after publishing; a manifest-relative path (or the file name) would fix both. B19's byte-determinism tests therefore run without `--build-clips`.
- export_engine (B09): reads `compiled-clips/animation-clips.json`; `body_height_px`, `sampling` and `art_source` are in clips.json for it; godot Sprite3D keys as in section 5.
- Integration e2e (Appendix I): the code-art pipeline commands are in section 1.
- svg-profile.md (B18): list the rig-specific refusals (bone transforms, transformed groups holding bones, `#id` and descendant selectors, nested svg) next to the portable profile, or link rig-animation.md.

## 8. Known limitations and what is not proven

- **Measured on one rig.** The hero example (64x64, about 48 px visible) and small synthetic rigs. The 10 L-corner gate, the 0.8-1.25 seam band and the IK margins were not tried on other body plans (quadrupeds, long weapons, 49-64 px characters). L-corners on the hero: at most 8 per frame.
- **Seam ratio is a heuristic.** Seam over median step depends on where the wrap falls: a perfectly smooth loop whose speed varies (a sampled sine) can exceed 1.25, and a loop with held pairs falls below 0.8. Rig clips keep it as a gate, as the plan asks; FX loops report it as a warning only (deviation, see below). The hero walk and idle and the example orb pass; the default projectile preset on a 48 px canvas with 8 frames warns (1.53).
- **Drift.** The gate is rig geometry (ankle x + stride * t per planted run, and ankle y), as in the probe and the prototype. Pixel-level zero drift holds only when the per-frame travel is a whole number of pixels and the planted foot stays flat (shown for the hero: identical boot pixels across 5 planted frames).
- **IK scope.** Two bones; the middle bone must be the root's direct child and the end bone the middle's; no scale on the chain or above it; a horizontal ground line (no slopes); the gait has no heel-toe roll during stance. Vector and pixel routes plant the ankle 0.5 px apart (half stroke versus 1 px outline), so a leg that is exactly straight in one route clamps in the other; the hero's legs are 19 px for that reason (the prototype's legs were shorter and never planted).
- **Flattening.** Slots leave their groups: clip-path, mask or filter on a bone with child bones is refused; group opacity is multiplied per slot (exact only when slots of a group do not overlap); `#id` and descendant selectors are refused.
- **Vector route.** Anti-aliased frames: partial alpha and blended edge colours are expected and not palette-checked.
- **FX.** Six presets, no free-form shapes; the runtime draws anti-aliased canvas paths, not pixel-exact frames (use the baked frames for pixel games). fx_verify.mjs records calls instead of rasterizing: readability (full-screen flash, reach, stroke width) is judged on paint bounding boxes, not pixels, and frame time on devices is not measured.
- **Builder.** The integrated B02 v2 builder is run by the hero and FX tests (v2 manifests by default). The integrated builder records manifest-relative paths (B02 uses manifest_path), so compiled-clips/ no longer points at the removed stage; its byte-identity across runs is not tested here.
- **Godot.** Sprite3D contracts were not imported into Godot.
- **Platforms.** Windows 11 only: Python 3.13.2, numpy 2.5.3, Pillow 12.3.0, resvg-py 0.5.0 (resvg 0.48.1), Node 22.15.0. Not run on Linux, macOS, Python 3.10 or Pillow 10.1. Frame PNG bytes are deterministic per Pillow/zlib build (tested by running twice).
- **Timing.** Hero, both clips: about 3 s (pixel route, 216 slot coverage renders through resvg-py, cached) and 2.3 s (vector, zoom 4) including the builder; the FX example about 4 s. No perf test was specified for these tools.
- **Deviations from the plan, with reasons.**
  - FX loop seam is a warning, not a gate: the plan gates seam only for the rig clips, and the experiments above show the ratio misfires on smooth pixel loops.
  - New document types rig-report.json and fx-report.json carry their own schema ids (section 5) instead of being folded into codeart-meta.json, which keeps per-frame detail out of the meta file.
  - The vector route defaults to zoom 4 (the plan names no default; zoom 1 at 64 px is blurry, and re-rendering at size beats downscaling).
  - The template's demo data is the normalised example slash, so the template stays runnable and verifiable on its own.
