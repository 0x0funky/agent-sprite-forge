# SVG rasterizers and the doctor

PixelSpec art needs no rasterizer. SVG art needs one of these backends, tried in this
order by `--backend auto`:

| Backend | Role | Install | Measured (design raster tests, Windows) |
|---|---|---|---|
| `resvg_py` (resvg-py 0.5.0, resvg 0.48.1 inside) | primary | `python -m pip install "resvg-py>=0.5,<0.6"` (abi3 wheels for Windows, Linux x86_64/aarch64, macOS) | 0.14-0.6 ms per small sprite, 2 ms with gradients, 19 ms with filters, 30.7 ms for a 256 px character; exact on crispEdges, `<style>`, `rotate(a cx cy)` and `<use>` |
| `resvg_js_cli` (@resvg/resvg-js-cli 2.6.2, older resvg) | fallback without the wheel | `npm i -g @resvg/resvg-js-cli` (must be on PATH) | about 0.6 s per call (process start); no `transform-origin` and no pixelated images, both outside the profile |
| `chrome` (Chrome, Edge or Chromium, headless) | reference and last resort | set `CHROME_PATH` when it is not found | exact on every truth case; the only one that resolves `var()` and CSS transforms; about 0.8 s per frame from the command line |

Never use PyMuPDF, skia-python or cairosvg: in the raster tests they ignored crispEdges,
`<style>` rules, clipPath or gradients without reporting an error (PyMuPDF painted
gradients black; skia ignored bare `href`; cairosvg did not load on Windows).

Rules the library follows ([codeart_core.py](../scripts/codeart_core.py)):

- A render error never falls through to another backend, so one output never mixes
  renderers. Pick one with `--backend` when you need a specific one.
- `codeart-meta.json` records the backend, name, version and, for resvg-py, the bundled
  resvg engine version, plus the zoom and output size.
- Chrome renders a `file://` copy of the SVG with `--headless`, a throwaway profile, a
  transparent background and `--force-device-scale-factor` for the zoom. On Windows its
  version is read from the executable's version resource: running `chrome.exe --version`
  there hands off to an open browser session and prints nothing useful. As root on Linux
  (CI containers) it adds `--no-sandbox`.
- With no backend at all, rendering fails with the pip command above instead of a
  traceback; `svg_render.py lint` and the PixelSpec tools keep working.

## Doctor

```text
python "<skill-dir>/scripts/svg_render.py" doctor --report out/doctor.json
python "<skill-dir>/scripts/svg_render.py" doctor --backend resvg_py
```

The doctor renders the conformance corpus on every available backend (or only the
`--backend` ones; the first available one is the primary):

| Case | Checks | Method |
|---|---|---|
| t01 | integer rects with crispEdges, at 1x and 4x | every pixel equals the truth image |
| t03 | crispEdges circle, polygon, curve and line | only the 4 palette colours, no partial alpha |
| t06A | bone rotated with `rotate(90 10 10)` | every pixel equals the truth image |
| t07 | `<use>` of a group (with `href` and `xlink:href`) and of a `<symbol>` | every pixel equals the truth image |
| t09 | linear, radial and repeating gradients | pinned resvg-py hash, otherwise probe pixels |
| t10 | clipPath, luminance mask and pattern | pinned resvg-py hash, otherwise probe pixels |
| t13 | group opacity composited once | pinned resvg-py hash, otherwise probe pixels |
| t16 | palette as class rules with literal hex | every pixel equals the truth image |
| t17 | palette variant as a later override `<style>` | every pixel equals the truth image |
| t06B, t06C, t13B | CSS transform, `transform-origin`, `mix-blend-mode` | the portable lint must reject them |

The status is `pass` only when the primary backend passes every case and the lint
rejects every lint case; otherwise the doctor exits 1 and names the failing cases. A
deviating fallback backend is reported as a warning. With no backend the status is
`fail` and the message is the pip command. `--report` writes the full report even when
the doctor fails: `schema: codeart2d.doctor_report.v1`, the case results per backend
plus a QA envelope (`method`, `notProven`, `checks`, `tool`) and the platform. It never
replaces an existing file.

Golden hashes for t09, t10 and t13 are pinned for resvg-py 0.5.0 on Windows (win32),
the only configuration measured. On other versions or systems those cases fall back to
probe pixels read from the Chrome references, within 8/255 per channel. Measured
anti-aliasing differences between resvg-py and Chrome (premultiplied MAE): t09 0.27,
t10 0.29, t13 0.48.

Run the doctor once per machine and again after upgrading a backend. The corpus has
passed end to end only on Windows 11 (resvg-py 0.5.0, resvg-js-cli 2.6.2, Chrome 154);
macOS, Linux and Edge discovery are untested.
