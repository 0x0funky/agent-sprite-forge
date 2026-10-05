# Portable SVG profile

Code-art SVG must render the same in resvg-py, the resvg-js CLI and Chrome/Edge, so the
art is defined by its text, not by whichever renderer happened to run. The rules below
are what those renderers agree on in the design raster tests (cases t01 to t17). The
compiler and lint in [codeart_core.py](../scripts/codeart_core.py) enforce them, and
[svg_render.py](../scripts/svg_render.py) runs both before it rasterizes anything.
Example: [potion-icons.svg](../examples/potion-icons.svg) with
[palette.json](../examples/palette.json).

## Rules

| Rule | Why (raster test) |
|---|---|
| Root `width`/`height` equal an integer `viewBox`; 1 unit = 1 logical pixel. Scale with `--zoom`, never by changing the root size | t01: every renderer is exact on integer rects at 1x and 4x |
| Pixel art sets `shape-rendering="crispEdges"` on the root (`svg_render.py render --crisp` adds it when missing) | t03: resvg and the browsers draw crisp shapes without anti-aliased pixels |
| Colours come from palette classes: `class="c-NAME"` plus a literal-hex rule `.c-NAME{fill:#rrggbb}`; the compiler writes missing rules from the palette | t16: resvg-py, resvg-js and the browsers apply `<style>` class rules exactly |
| A palette variant is one extra `<style>` appended at the end that re-declares only the changed classes | t17: the later rule wins in every supported renderer |
| CSS variables only in source files; the compiler inlines them | t04/t05: only browsers resolve `var()`; the others paint black |
| Bones rotate with the `transform` attribute, e.g. `rotate(a cx cy)`; never CSS `transform` or `transform-origin` | t06: CSS transforms are ignored outside browsers; `transform-origin` moves parts off the canvas in resvg-js |
| `<use>` is expanded by the compiler (both `href` and `xlink:href` work) | t07: some renderers ignore a bare `href` |
| No `<text>`: convert lettering to paths, or add bitmap text later | t12/t14: every renderer picks different fonts; resvg-js spends 139-294 ms loading system fonts |
| Gradients, clipPath, mask and pattern are fine for vector art | t09/t10: resvg and Chrome agree within MAE 0.29 |
| Blur, drop-shadow, morphology and glow filters are fine; `feTurbulence`, `feDisplacementMap` and `mix-blend-mode` are not; bake noise in code | t11: resvg 0.48 keeps only 125-240 of 2,704 displaced pixels; turbulence differs per renderer |
| No embedded raster `<image>`; composite raster art in Python | t08: pixelated image scaling differs between renderers |
| Numbers are finite decimals; never a Python complex or NaN | fox probe: `61.13-0.00j` from `x ** 0.65` made Chrome silently draw a broken tail |
| Frames batched into one HTML page get unique ids (`compile_svg(..., id_prefix="f3_")`) | fox probe: a shared clipPath id made frame 2 use frame 0's clip |

Root metadata: `data-anchor="x y"` is the default root for `svg_render.py render` (the
`--anchor` option wins); `data-codeart`, `data-facing` and `data-palette` are reserved
labels that the tools keep but do not interpret.

Rig conventions, used by the skeletal animation tool (rig_animate.py, planned for the
codeart2d P1 release): bones are `<g id="..." data-pivot="x y">`, poses change only their
`transform` attribute, and slots carry `data-z` so draw order does not follow the bone
hierarchy.

## Palette classes and variants

```xml
<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">
  <rect class="c-outline" x="2" y="2" width="12" height="12"/>
  <rect class="c-liquid" x="4" y="4" width="8" height="8"/>
</svg>
```

```json
{"colors": {"outline": "#2b1d33", "liquid": "#d93a4b"},
 "variants": {"health": {}, "mana": {"liquid": "#3b6fe0"}}}
```

`compile_svg(svg, palette, "mana")` inserts `<style>.c-outline{fill:#2b1d33}.c-liquid{fill:#d93a4b}</style>`
at the top and appends `<style data-codeart-variant="mana">.c-liquid{fill:#3b6fe0}</style>`
at the end. Class names are `c-` plus the palette name (letters, digits, `_`, `-`). A
`c-` class with no palette colour and no rule is an error. For strokes, write a normal
rule with a literal hex value, or `var(--name)`, which the compiler fills from the palette.
Palette files are `.json` (`{"colors": {...}, "variants": {...}}`, a flat `{name: colour}`
mapping or a `palette_v1` list), `.hex` or `.gpl`.

The compiler also expands `<use>` (copies drop their ids, so style reused content with
classes; a `#id` selector on reused content is refused) and refuses NaN, infinity and
complex numbers.

## Lint codes

`svg_render.py lint` prints one `<code>: <message>` line per problem and exits 1 when
there is any. The `portable` profile:

| Code | Problem | Fix |
|---|---|---|
| `xml` | not well-formed XML, a DOCTYPE/ENTITY, or a root that is not `<svg>` | write plain SVG with `xmlns="http://www.w3.org/2000/svg"` |
| `viewbox` | `viewBox` missing, not four numbers, not integers, or not equal to the root `width`/`height` | `width="W" height="H" viewBox="0 0 W H"` with integers |
| `css-transform` | CSS `transform` or `transform-box` | `transform="rotate(a cx cy)"`, `translate(...)` or `scale(...)` attributes |
| `transform-origin` | `transform-origin` attribute or property | pivot with `rotate(a cx cy)`, or translate, rotate, translate back |
| `var` | `var()` left in the SVG | keep variables in sources and render through `svg_render.py` (or `lint --compile`) |
| `text` | `<text>`, `<tspan>`, `<textPath>` | convert to paths |
| `image` | embedded `<image>` | composite raster art in Python |
| `feTurbulence` | noise filter | bake the noise in code |
| `feDisplacementMap` | displacement filter | bake the displacement in code |
| `mix-blend-mode` | blend modes | blend layers in Python |
| `foreignObject` | HTML inside SVG | browsers only; draw it as SVG shapes |
| `number` | NaN, infinity, a complex number or a stray character in geometry | write finite decimals, e.g. `round(float(v.real), 3)` |
| `reference` | an `href` (or `xlink:href`) that is not `#id` of an element in the same document: a file, a URL or a missing id | copy the shape into the SVG and reference it as `#id`; render refuses such a `<use>` too |

The `pixel` profile (used by `render --crisp`) adds:

| Code | Problem | Fix |
|---|---|---|
| `crisp-edges` | the root lacks `shape-rendering="crispEdges"` | add it (render `--crisp` does) |
| `gradient` | `linearGradient` / `radialGradient` | flat palette colours; shade with extra palette entries |
| `mask` | masks make partial alpha | cut shapes with paths instead |
| `filter` | filters make partial alpha and new colours | bake glows and shadows as shapes |
| `opacity` | `opacity`, `fill-opacity` or `stroke-opacity` below 1 | use an opaque palette colour |

```text
python "<skill-dir>/scripts/svg_render.py" lint --svg art/potion.svg --profile portable
python "<skill-dir>/scripts/svg_render.py" lint --svg art/potion.svg --compile --palette art/palette.json --variant mana
```

`--compile` lints what render rasterizes (palette applied, `var()` inlined, `<use>`
expanded) and reports compile errors as `compile:` lines.

## Two render routes

| | Vector (default) | Crisp (`--crisp`) |
|---|---|---|
| For | flat vector characters, props, UI, icons at any size | pixel art at its native size |
| Lint profile | portable | pixel |
| Render | anti-aliased at `--zoom` (any positive scale that gives whole pixels) | crispEdges at 1x; `--zoom N` adds `<name>@Nx.png` by integer nearest upscaling |
| QA gates | lint only; partial alpha and blended colours are expected | 0 partial-alpha and 0 off-palette pixels (`--strict-qc` publishes nothing otherwise) |

```text
python "<skill-dir>/scripts/svg_render.py" render --svg art/potion-icons.svg --palette art/palette.json --output-dir out/potions-v1 --zoom 4
python "<skill-dir>/scripts/svg_render.py" render --svg art/potion-icons.svg --palette art/palette.json --output-dir out/potions-px --crisp --zoom 4 --strict-qc
```

Each variant gets a folder holding the compiled SVG that was rendered
(`<variant>/<name>.svg`) and the PNG(s); `codeart-meta.json` records the renderer name,
version and engine, every output with its sha256, and the QA envelope. Without
`--palette`, the palette is the `#rrggbb` colours the SVG writes (named colours such as
`white` then count as off-palette in crisp QA). A lint problem always stops the render
before any file is written.

Draw crisp art on whole or half units and check it at 1x: shapes are sampled at pixel
centres, so a circle of radius 9.5 and one of radius 10 differ by a ring of pixels.
