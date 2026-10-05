# A3-codeart-core handoff

Module: `skills/codeart2d/scripts/codeart_core.py`, the library behind the codeart2d CLIs (B18 to B21).
Tests: [`tests/test_codeart2d_core.py`](../tests/test_codeart2d_core.py), with fixtures in `tests/fixtures/codeart/`.
Library source: [`codeart_core.py`](../skills/codeart2d/scripts/codeart_core.py).

## 1. CLIs

A3 ships no CLI. The CLIs in B18 to B21 import the library by path:
`sys.path.insert(0, str(Path(__file__).resolve().parent)); import codeart_core`.
Call `forge_core.require_modules(["numpy", "PIL"])` before that import. The module needs numpy and Pillow at import time.

Entry points, as single-line Python calls:

- Compile and lint SVG:
  - `svg = codeart_core.compile_svg(source, palette, variant="blue", id_prefix="f0_")`
  - `problems = codeart_core.lint_portable_svg(svg, profile="portable")`. Profile is `portable` or `pixel`. Each problem is a line `"<code>: <message>"`.
- Rasterize: `pixels, renderer = codeart_core.rasterize(svg, zoom=4, backend="auto")`.
  - Backends: `auto`, `resvg_py`, `resvg_js_cli` or `chrome`.
  - `renderer` holds `backend`, `name`, `version`, `engine` (resvg-py only), `zoom` and `size`.
- Doctor: `report = codeart_core.run_doctor()`. `report["status"]` is `pass` or `fail`. With no backend installed, `report["hint"]` holds the pip command. B18's `svg_render.py doctor` exits non-zero unless the status is `pass`.
- PixelSpec: `frame = codeart_core.render_pixelspec(spec, "rest", "blue")`. The frame argument is an index, a name, a frame object, or None for the base layers.
- Pixel finishing: `rgba, report = codeart_core.pixel_finish(slots, (64, 64), outline="selout", stamps=[(29, 21, "#1a1c2c")])`.
- QA:
  - `qa = codeart_core.qa_pixels(rgba, palette, outline_colour_or_list)`
  - `grid = codeart_core.detect_grid(rgba)`
- Review: `codeart_core.review_sheet(frames, palette=..., qa=..., anchor=(16, 31)).save("review.png")`.
- Output:
  - `codeart_core.save_png(rgba, path)`
  - `codeart_core.write_codeart_meta(path, generator=..., spec_sha256=..., renderer=..., palette=..., outputs=[...], qa=...)`

Contracts that B18 to B21 depend on:

- **Errors.**
  - Bad input raises `CodeArtError`, a ValueError subclass.
  - A missing or failed rasterizer raises `RasterError`, a RuntimeError subclass.
  - Messages are ASCII. CLIs print `error: <message>` and exit 1.
- **Backend choice.** `auto` takes the first installed backend: resvg_py, then resvg_js_cli, then chrome. A render error never falls through to another backend.
- **Chrome.**
  - Runs as `--headless` on a `file://` copy of the SVG, with a throwaway `--user-data-dir`.
  - `CHROME_PATH` overrides discovery.
  - On Windows, the version comes from the exe's version resource. Never run `chrome.exe --version` on Windows: it hands off to the user's running browser session and prints nothing useful.
- **Pixel arrays.** Every array returned is `(H, W, 4)` uint8 straight-alpha RGBA, with RGB zeroed where alpha is 0.
- **pixel_finish slots.** Back to front: `{"alpha": coverage at (H*ss, W*ss), "fill": hex | "ramp": {hi, mid, lo, dark, out} | [5 colours], "group": bone id, "name": ...}`.
  - `alpha` may be uint8, float, bool, or an RGBA render whose alpha is used. The default `ss` is 8.
  - For a rig, render each slot at `zoom=8` filled white with no stroke, and pass the alpha.
  - `report["border_contact_px"] > 0` means the exterior outline was clipped. B19's margin lint should fail on it.
- **Fixed stage order.** `report["stages"]` is `coverage, labels, cleanup, shading, inner_lines, outline`.
- **PixelSpec.**
  - Both `codeart2d.pixelspec.v1` and `codeart.pixelspec.v1` (the roadmap 4.5 spelling) are accepted.
  - `segments` are run-length rows such as `"13.6l13."`. Digits are counts, so segment rows cannot use digit palette characters.
  - `mirror_safe: false` keeps a layer's own orientation under `flip_x`; only its placement mirrors.
  - A palette colour with alpha 0 erases lower layers.
  - Overflow and an outline that would leave the canvas both raise.
- **Doctor report shape.** B18 writes this to `doctor.json`:
  - `{status, primary, cases[], backends: {name: {status, renderer | reason, results: [{case, zoom, status, method, ...}]}}, lint: [...], hint?}`.
  - `method` is one of `truth`, `palette`, `sha256`, `probes` or `lint`.
- **QA envelope.** `qa_pixels` returns raw metrics. A CLI that writes QA JSON must wrap them in the common qaEnvelope: `method`, `notProven`, and input/output sha256.
  - `rgba_sha256(rgba)` hashes the canonical pixel bytes, independent of PNG encoding.

## 2. SKILL.md routing rows

For `skills/codeart2d/SKILL.md` (Z), Dependencies section, paste-ready:

```text
Dependencies: numpy and Pillow for everything; PixelSpec needs nothing else. SVG
art also needs one rasterizer: resvg-py (python -m pip install "resvg-py>=0.5,<0.6",
preferred), the resvg-js CLI on PATH (npm i -g @resvg/resvg-js-cli), or Chrome/Edge
(set CHROME_PATH if it is not found). Never PyMuPDF, skia-python or cairosvg: they
silently ignore crispEdges, <style>, clipPath or gradients. Run the doctor once per
machine; it checks the backends against the raster corpus.
```

```text
SVG rules (portable profile, enforced by lint): root width/height equal an integer
viewBox (1 unit = 1 logical pixel); palette colours via class="c-NAME" (the compiler
writes literal-hex rules, variants become one override <style>); CSS var() only in
sources; bones rotate with transform="rotate(a cx cy)", never CSS transform or
transform-origin; no <text>, <image>, feTurbulence, feDisplacementMap or
mix-blend-mode; pixel art adds shape-rendering="crispEdges" and no gradients, masks,
filters or opacity. Style reused (<use>) content with classes, not #id selectors.
```

## 3. README tool-table rows

Requirements (the shared section generated by `tools/gen_readme_sections.py`):

| Package | Needed for |
|---|---|
| numpy, Pillow >= 10.1 | everything, including PixelSpec code art |
| resvg-py >= 0.5, < 0.6 (optional) | codeart2d SVG rasterizing; fallbacks are the resvg-js CLI or Chrome/Edge |

Tool table: `codeart2d/scripts/codeart_core.py` is a shared library, not a CLI. Its row belongs to the B18 CLIs that wrap it.

## 4. CHANGELOG entries

- **Added**
  - codeart2d core library `codeart_core.py`, covering:
    - palette parsing (JSON, `.hex`, `.gpl`, palette_v1, variants);
    - an SVG compiler: palette class rules with literal hex, variant override blocks, var() inlining, `<use>` expansion, id prefixing;
    - a portable/pixel SVG lint;
    - the rasterizer chain resvg-py, resvg-js CLI, then headless Chrome/Edge, with version capture and a pip hint;
    - a doctor conformance corpus (t01, t03, t06A, t07, t09, t10, t13, t16, t17, plus lint cases t06B, t06C, t13B);
    - a PixelSpec v1 renderer;
    - pixel finishing route D;
    - pixel QA metrics and grid detection;
    - review sheets;
    - the codeart-meta writer (`art_source: "code"`, disclosure "code-drawn, no image model").
- **Fixed**
  - Fox probe, roadmap 4.4: a Python complex (`61.13-0.00j`) or NaN leaking into SVG geometry is now rejected by `compile_svg` and flagged by lint. Chrome used to draw such frames wrong without any error.
  - Fox probe, roadmap 4.4: clipPath/gradient ids colliding between frames batched in one page. `compile_svg(id_prefix=...)` now namespaces ids and every reference to them.
  - Roadmap 4.7: pixel finishing now cleans up before it outlines. Outlining first reopened 18 to 37 outline gaps per frame. The prototype's 478 ms per frame is now about 5 ms per 64x64 frame, vectorised.
- **Changed / BREAKING:** none. This is a new skill library.

## 5. Schema change requests

These go in `shared/schemas/codeart.schema.json` (A0). `codeart_core` produces and reads exactly the fields below.

`additionalProperties` stays true everywhere. Replace `COMMON` with the `$id` A0 gives `common.schema.json`.

```json
{
  "$defs": {
    "hexColour": {"type": "string", "pattern": "^#?([0-9A-Fa-f]{3,4}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})$"},
    "pixelGrid": {"oneOf": [
      {"type": "array", "minItems": 1, "items": {"type": "string"}},
      {"type": "object", "required": ["rows"], "properties": {"rows": {"type": "array", "minItems": 1, "items": {"type": "string"}}}},
      {"type": "object", "required": ["segments"], "properties": {"segments": {"$ref": "#/$defs/pixelSegments"}}}
    ]},
    "pixelSegments": {"type": "array", "minItems": 1, "items": {"type": "string", "pattern": "^(\\d*\\D)*$"}},
    "pixelspecPalette": {"type": "object", "minProperties": 1, "propertyNames": {"pattern": "^[^. ]$"},
                         "additionalProperties": {"$ref": "#/$defs/hexColour"}},
    "pixelspec_v1": {
      "type": "object",
      "required": ["schema", "canvas", "palette", "layers"],
      "properties": {
        "schema": {"enum": ["codeart2d.pixelspec.v1", "codeart.pixelspec.v1"]},
        "name": {"type": "string"},
        "canvas": {"type": "array", "minItems": 2, "maxItems": 2, "items": {"type": "integer", "minimum": 1}},
        "anchor_px": {"type": "array", "minItems": 2, "maxItems": 2, "items": {"type": "number"}},
        "palette": {"$ref": "#/$defs/pixelspecPalette"},
        "variants": {"type": "object", "additionalProperties": {"type": "object", "additionalProperties": {"$ref": "#/$defs/hexColour"}}},
        "poses": {"type": "object", "additionalProperties": {"$ref": "#/$defs/pixelGrid"}},
        "layers": {"type": "array", "minItems": 1, "items": {
          "type": "object", "required": ["name"],
          "properties": {
            "name": {"type": "string", "minLength": 1}, "z": {"type": "number"},
            "origin": {"type": "array", "minItems": 2, "maxItems": 2, "items": {"type": "integer"}},
            "rows": {"oneOf": [{"type": "string"}, {"type": "array", "minItems": 1, "items": {"type": "string"}}]},
            "segments": {"$ref": "#/$defs/pixelSegments"},
            "mirror": {"type": "boolean"}, "mirror_safe": {"type": "boolean"}, "hidden": {"type": "boolean"}
          }}},
        "frames": {"type": "array", "items": {
          "type": "object",
          "properties": {
            "name": {"type": "string"}, "dx": {"type": "integer"}, "dy": {"type": "integer"}, "flip_x": {"type": "boolean"},
            "layers": {"type": "object", "additionalProperties": {
              "type": "object",
              "properties": {
                "rows": {"oneOf": [{"type": "string"}, {"type": "array", "minItems": 1, "items": {"type": "string"}}]},
                "segments": {"$ref": "#/$defs/pixelSegments"},
                "dx": {"type": "integer"}, "dy": {"type": "integer"}, "mirror": {"type": "boolean"}, "hidden": {"type": "boolean"}
              }}}
          }}},
        "outline": {"type": "object", "properties": {
          "mode": {"enum": ["none", "solid", "selout"]},
          "color": {"type": "string", "minLength": 1, "maxLength": 1},
          "map": {"type": "object", "additionalProperties": {"type": "string", "minLength": 1, "maxLength": 1}}}},
        "clips": {"type": "object"}
      }
    },
    "codeart_meta_v1": {
      "type": "object",
      "required": ["art_source", "generator", "spec_sha256", "renderer", "palette", "outputs", "qa"],
      "properties": {
        "schema": {"const": "codeart2d.codeart_meta.v1"},
        "art_source": {"const": "code"},
        "placeholder": {"type": "boolean"},
        "disclosure": {"type": "string"},
        "generator": {"type": "string", "minLength": 1},
        "spec_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "renderer": {"type": "object", "required": ["name", "version"], "properties": {
          "name": {"type": "string"}, "version": {"type": "string"},
          "backend": {"enum": ["resvg_py", "resvg_js_cli", "chrome"]}, "engine": {"type": "string"},
          "zoom": {"type": "number", "exclusiveMinimum": 0},
          "size": {"type": "array", "minItems": 2, "maxItems": 2, "items": {"type": "integer"}}}},
        "palette": {"type": "object", "required": ["colors"], "properties": {
          "colors": {"type": "object", "additionalProperties": {"type": "string", "pattern": "^#[0-9a-f]{6}([0-9a-f]{2})?$"}},
          "variants": {"type": "object", "additionalProperties": {"type": "object", "additionalProperties": {"type": "string", "pattern": "^#[0-9a-f]{6}([0-9a-f]{2})?$"}}}}},
        "outputs": {"type": "array", "items": {"$ref": "COMMON#/$defs/fileRef"}},
        "qa": {"type": "object"}
      }
    }
  }
}
```

Notes:

- `codeart_meta_v1.palette` is always an object `{colors, variants}`. Appendix B left its type open.
- `renderer` carries the extra keys from `rasterize()`.
- `test_meta_validates_against_schema` checks `skills/codeart2d/references/schemas/codeart.schema.json` `#/$defs/codeart_meta_v1` as soon as A0's vendored copy exists.
- The PixelSpec schema spelling is unresolved. Roadmap 4.5 writes `codeart.pixelspec.v1`, while the namespace rule says `codeart2d.*`. The library accepts both; examples should use `codeart2d.pixelspec.v1`.

## 6. Shared-helper promotion requests

No promotions into `forge_*` are needed. Once `skills/codeart2d/scripts/forge_core.py` (A1) is vendored, replace these private stand-ins:

- `_local_sha256_file(path)` becomes `forge_core.sha256_file(path)`.
- `_local_write_json(path, data)` becomes `forge_core.write_json(path, data, no_clobber=True)`. The behaviour is identical: UTF-8, indent 2, `ensure_ascii=False`, trailing newline, refuses to overwrite.
- `save_png` stays in the codeart_core API (frozen in Appendix A). It may delegate to `forge_core.save_png` once A1 confirms the same contract: RGBA, RGB zeroed where alpha is 0, no metadata chunks.

Optional, for B04 or later: `detect_grid` and `qa_pixels` are free of image-model assumptions. B04's `pixel_reduce` could reuse them if they are ever moved into `forge_palette`.

## 7. Cross-module links that Z must add

- codeart2d `SKILL.md` and B18's `references/rasterizers.md`:
  - link `scripts/codeart_core.py` as the implementation of the SVG profile, the backends and the doctor;
  - the doctor corpus is `codeart_core.doctor_cases()`;
  - the test goldens live in `tests/fixtures/codeart/raster/goldens.json`.
- `references/svg-profile.md` (B18): each lint code (`viewbox`, `css-transform`, `transform-origin`, `var`, `text`, `image`, `feTurbulence`, `feDisplacementMap`, `mix-blend-mode`, `foreignObject`, `number`, plus pixel-profile codes `crisp-edges`, `gradient`, `mask`, `filter`, `opacity`) should be listed with its fix.
- `references/pixelspec.md` (B18): document the fields in section 5 and the RLE `segments` syntax. Use `tests/fixtures/codeart/slime.pixelspec.json` as the known-good example; it is the design prototype output.
- CONTRIBUTING (Z): to regenerate the raster goldens, run `codeart_core.rasterize(case.svg, zoom, backend)` for each render case of `doctor_cases()`. Record the backend versions in `goldens.json`. Pinned resvg hashes also live in `codeart_core._DOCTOR_PINNED` and must be updated together.
- `pytest.ini` (A0) must register the `perf` and `resvg` markers used here. Until then pytest prints three `PytestUnknownMarkWarning`s.

## 8. Known limitations and what is not proven

- **Bit-exact golden renders.**
  - Claimed only for resvg-py 0.5.0 (resvg 0.48.1) on Windows (win32), the configuration that produced the pinned hashes.
  - Other OS or versions fall back to probe pixels within 8/255, and the test uses MAE <= 1.0 against the stored Chrome 154 references.
  - Measured MAE, resvg-py against Chrome: t09 0.27, t10 0.29, t13 0.48.
  - resvg determinism across OS is unverified; CI on Linux and macOS will tell.
- **Backends verified end to end** (`run_doctor()` all pass, 8.5 s) only on Windows 11:
  - resvg-py 0.5.0;
  - resvg-js-cli 2.6.2-beta.1, the design prototype's local install;
  - Chrome 154.0.8037.57.
  - Untested: Chrome/Edge discovery and `--version` parsing on macOS and Linux, the root `--no-sandbox` path, and Edge or Chromium as the browser.
- **Chrome/resvg-js unit tests use fakes.** They monkeypatch subprocess and do not launch a browser, so CI without Chrome still covers command construction.
- **`compile_svg` CSS support:**
  - flat rule lists only; `@`-rules are rejected;
  - a `;` inside a CSS value, such as a data URI, is not supported;
  - `<use>` copies drop their ids, so reused content styled by a `#id` selector raises instead of compiling differently.
- **`pixel_finish` shading** is run-length form shading with 4 ramp bands. Quality is programmer-art level.
  - The gates were measured on one synthetic 64 px biped: 0 partial alpha, 0 off-palette, 0 gaps, 9 L-corners (limit 10), about 5 ms per frame.
  - Untested on real rigs; that is B19.
  - Orphan counts rise with shading (about 62 per frame), because diagonal band edges are single pixels.
- **`detect_grid`** finds integer periods 2-24 with per-axis phases. Fractional periods, such as the earlier 3.46 px case, are deferred to `pixelize.py`.
  - The no-grid acceptance (score < 0.05) uses synthetic painted crops: measured about 0.002.
  - The real meadow crop is A0's fixture and was not available in this worktree.
- **`review_sheet`** text uses Pillow's default font, so glyphs differ between Pillow/FreeType builds while sheet sizes do not.
- **`save_png`** gives identical bytes for a given Pillow/zlib build only. Cross-version tests compare pixel sha256 instead.
- **Not runnable in this worktree:**
  - `tools/vendor_sync.py --check` (A0's file is absent);
  - the `test_skill_packages.py` codeart2d case, which fails until Z adds `SKILL.md`, as expected.
- **No CLI** in A3, so the cp1252 `--help` gate does not apply. The module imports and runs under `PYTHONIOENCODING=cp1252`.
- **Fixtures.**
  - The SVGs in `tests/fixtures/codeart/raster/` are byte-identical to the design raster-test corpus. t06A and t13 are its portable subsets.
  - The three `*.chrome.png` references are synthetic renders made by `rasterize(..., "chrome")`.
  - `slime.pixelspec.json` is the code-generated design prototype, normalised to LF. All 12 of its frames render pixel-identical to the prototype PNGs.
  - None of these files is owner or image-model art.
