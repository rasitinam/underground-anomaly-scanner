"""
Phase 7b: write output/map.html, an interactive map that opens in any web browser.

No GIS software needed: every layer is reprojected to WGS84, coloured with the same
ramps as the QGIS project and embedded as a PNG overlay on a Leaflet map (free, open
source). Basemap tiles and Leaflet itself are loaded from the internet when viewed.
"""
from __future__ import annotations

import base64
import json
import os
import platform
import subprocess
import warnings
import webbrowser
from html import escape
from pathlib import Path

import numpy as np
import rasterio
from rasterio.errors import NotGeoreferencedWarning
from rasterio.io import MemoryFile
from rasterio.warp import Resampling, calculate_default_transform, reproject

from logger import log
from qgis_project import LAYER_GROUPS

# Same colour ramps as qgis_builder.py (which runs inside QGIS's Python and cannot share this module).
RAMPS = {
    "ndvi": [(-0.2, "#a6611a"), (0.1, "#dfc27d"), (0.3, "#f5f5a0"), (0.5, "#80cd6b"), (0.8, "#1a7a2e")],
    "ndwi": [(-0.6, "#8c510a"), (-0.2, "#f6e8c3"), (0.0, "#c7eae5"), (0.4, "#2166ac")],
    "nbr": [(-0.3, "#7f3b08"), (0.0, "#fee0b6"), (0.3, "#b2df8a"), (0.7, "#1b7837")],
    "ndre": [(0.0, "#a6611a"), (0.15, "#dfc27d"), (0.25, "#f5f5a0"), (0.35, "#80cd6b"), (0.5, "#1a7a2e")],
    "ndmi": [(-0.3, "#8c510a"), (0.0, "#f6e8c3"), (0.2, "#80cdc1"), (0.5, "#01665e")],
    "anomaly": [(0.0, "#ffffff00"), (0.35, "#ffffb200"), (0.5, "#fecc5c"), (0.7, "#fd8d3c"), (1.0, "#bd0026")],
    "count": [(0, "#ffffff00"), (1, "#ffffb2"), (2, "#fecc5c"), (3, "#f03b20"), (4, "#7a0177")],
}
GRAY = [(0.0, "#000000"), (1.0, "#ffffff")]
LEAFLET = "https://unpkg.com/leaflet@1.9.4/dist/"
LEAFLET_CSS_SRI = "sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY="
LEAFLET_JS_SRI = "sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="


def _rgba(hex_code: str) -> tuple[int, int, int, int]:
    h = hex_code.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), int(h[6:8], 16) if len(h) == 8 else 255


def _apply_ramp(values: np.ndarray, stops: list[tuple[float, str]], discrete: bool = False) -> np.ndarray:
    """values (H, W) float -> (4, H, W) uint8 RGBA; NaN becomes fully transparent."""
    xs = np.array([s[0] for s in stops], dtype="float64")
    cols = np.array([_rgba(s[1]) for s in stops], dtype="float64")
    valid = np.isfinite(values)
    v = np.where(valid, values, xs[0])
    if discrete:
        idx = np.clip(np.searchsorted(xs, v, side="right") - 1, 0, len(xs) - 1)
        out = cols[idx].transpose(2, 0, 1)
    else:
        out = np.stack([np.interp(v, xs, cols[:, c]) for c in range(4)])
    out[3] = np.where(valid, out[3], 0)
    return np.round(out).astype("uint8")


def _style_stops(data: np.ndarray, style: str) -> tuple[list[tuple[float, str]], bool]:
    valid = data[np.isfinite(data)]
    if style in RAMPS:
        return RAMPS[style], style == "count"
    if valid.size == 0:
        return GRAY, False
    if style == "diverging":
        limit = max(abs(float(valid.min())), abs(float(valid.max())), 1e-6)
        std = float(valid.std())
        limit = min(limit, 3 * std) if std > 0 else limit
        return [(-limit, "#2166ac"), (0.0, "#f7f7f7"), (limit, "#b2182b")], False
    if style == "dem":
        lo, hi = float(valid.min()), float(valid.max())
        span = max(hi - lo, 1e-6)
        return [(lo, "#1a9641"), (lo + 0.33 * span, "#a6d96a"), (lo + 0.66 * span, "#fdae61"), (hi, "#a0522d")], False
    lo, hi = (float(p) for p in np.percentile(valid, [2, 98]))  # gray: cumulative-cut stretch like QGIS
    return [(lo, "#000000"), (max(hi, lo + 1e-6), "#ffffff")], False


def _to_wgs84(path: Path, nearest: bool) -> tuple[np.ndarray, list[list[float]]]:
    """Reproject all bands to EPSG:4326. Returns (bands, H, W) float32 with NaN nodata and Leaflet bounds."""
    with rasterio.open(path) as src:
        transform, width, height = calculate_default_transform(src.crs, "EPSG:4326", src.width, src.height, *src.bounds)
        out = np.full((src.count, height, width), np.nan, dtype="float32")
        for b in range(1, src.count + 1):
            band = src.read(b).astype("float32")
            if src.nodata is not None:
                band[band == src.nodata] = np.nan
            reproject(band, out[b - 1], src_transform=src.transform, src_crs=src.crs, src_nodata=np.nan,
                      dst_transform=transform, dst_crs="EPSG:4326", dst_nodata=np.nan,
                      resampling=Resampling.nearest if nearest else Resampling.bilinear)
    west, north = transform.c, transform.f
    east, south = west + transform.a * width, north + transform.e * height
    return out, [[south, west], [north, east]]


def _png_data_uri(rgba: np.ndarray) -> str:
    _, h, w = rgba.shape
    with warnings.catch_warnings(), MemoryFile() as mem:
        warnings.simplefilter("ignore", NotGeoreferencedWarning)
        with mem.open(driver="PNG", width=w, height=h, count=4, dtype="uint8") as dst:
            dst.write(rgba)
        return "data:image/png;base64," + base64.b64encode(mem.read()).decode("ascii")


def _raster_layer(path: Path, style: str) -> dict:
    bands, bounds = _to_wgs84(path, nearest=(style == "count"))
    if style == "rgb":
        rgb = np.nan_to_num(bands[:3], nan=0).clip(0, 255)
        alpha = np.where(np.isfinite(bands[0]) & (rgb.sum(axis=0) > 0), 255, 0)
        return {"image": _png_data_uri(np.concatenate([rgb, alpha[None]]).astype("uint8")), "bounds": bounds}
    stops, discrete = _style_stops(bands[0], style)
    legend = {"stops": [[round(float(v), 4), c] for v, c in stops], "discrete": discrete}
    return {"image": _png_data_uri(_apply_ramp(bands[0], stops, discrete)), "bounds": bounds, "legend": legend}


def build_map(out_dir: Path, aoi_geojson: Path, basemaps: list[dict], title: str) -> Path | None:
    """basemaps: [{"name", "url", "visible"}]. Returns output/map.html, or None if it could not be written."""
    try:
        groups = []
        for group_name, layers in LAYER_GROUPS:
            entries = []
            for filename, name, style, visible in layers:
                path = out_dir / filename
                if not path.exists():
                    continue
                if path.suffix == ".geojson":
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if data.get("features"):
                        entries.append({"name": name, "kind": "vector", "style": style, "visible": visible,
                                        "geojson": data})
                    continue
                try:
                    entries.append({"name": name, "kind": "raster", "style": style, "visible": visible,
                                    **_raster_layer(path, style)})
                except Exception as exc:  # noqa: BLE001
                    log(f"Web map: skipped {filename} ({type(exc).__name__}: {exc})", "WARNING")
            if entries:
                groups.append({"name": group_name, "layers": entries})
        aoi = json.loads(aoi_geojson.read_text(encoding="utf-8"))
        config = {"title": title, "aoi": aoi, "groups": groups,
                  "basemaps": [b for b in basemaps if b.get("url")]}
        out_path = out_dir / "map.html"
        out_path.write_text(_HTML.replace("__TITLE__", escape(title))
                            .replace("__LEAFLET__", LEAFLET)
                            .replace("__CSS_SRI__", LEAFLET_CSS_SRI)
                            .replace("__JS_SRI__", LEAFLET_JS_SRI)
                            .replace("__CONFIG__", json.dumps(config).replace("</", "<\\/")),
                            encoding="utf-8")
        log(f"Interactive web map written: {out_path}")
        return out_path
    except Exception as exc:  # noqa: BLE001
        log(f"Web map could not be created: {type(exc).__name__}: {exc}", "ERROR")
        return None


def open_in_browser(path: Path) -> bool:
    try:
        if platform.system() == "Windows":
            os.startfile(str(path))  # type: ignore[attr-defined]  # .html association -> default browser
        elif not webbrowser.open(path.resolve().as_uri()):
            subprocess.Popen(["xdg-open", str(path)])
        return True
    except Exception as exc:  # noqa: BLE001
        log(f"Could not open the browser automatically ({exc}). Open {path} manually.", "WARNING")
        return False


_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<link rel="stylesheet" href="__LEAFLET__leaflet.css" integrity="__CSS_SRI__" crossorigin="">
<script src="__LEAFLET__leaflet.js" integrity="__JS_SRI__" crossorigin=""></script>
<style>
  html, body { height: 100%; margin: 0; font: 13px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif; }
  #map { position: absolute; inset: 0 0 0 320px; }
  #panel { position: absolute; top: 0; bottom: 0; left: 0; width: 320px; overflow-y: auto; box-sizing: border-box;
           padding: 12px 14px; background: #fafafa; border-right: 1px solid #ddd; }
  h1 { font-size: 14px; margin: 0 0 4px; }
  .note { color: #8a4b00; background: #fff4e0; border: 1px solid #f0d9b0; padding: 6px 8px; border-radius: 4px; margin: 8px 0; }
  details { margin: 6px 0; } summary { font-weight: 600; cursor: pointer; padding: 3px 0; }
  label { display: flex; gap: 6px; align-items: flex-start; padding: 2px 0 2px 4px; cursor: pointer; }
  label input { margin-top: 2px; }
  .row { display: flex; align-items: center; gap: 8px; margin: 8px 0; }
  .row input[type=range] { flex: 1; }
  #legend { margin-top: 8px; } #legend .bar { height: 12px; border: 1px solid #ccc; border-radius: 2px; }
  #legend .ticks { display: flex; justify-content: space-between; color: #555; font-size: 11px; }
  #coords { font-family: ui-monospace, Consolas, monospace; font-size: 12px; color: #333; }
  .leaflet-popup-content table { border-collapse: collapse; } .leaflet-popup-content td { padding: 1px 6px 1px 0; vertical-align: top; }
  @media (max-width: 700px) { #panel { width: 100%; bottom: auto; height: 42%; border-right: 0; border-bottom: 1px solid #ddd; }
                              #map { inset: 42% 0 0 0; } }
</style>
</head>
<body>
<div id="panel">
  <h1>__TITLE__</h1>
  <div class="note">Indirect surface indicators only, not detections. Confirming anything underground needs field
    geophysics (GPR / ERT / seismic). / Yalnızca dolaylı yüzey göstergeleri; doğrulama için saha ölçümü gerekir.</div>
  <div class="row"><span>Basemap</span><select id="basemap"></select></div>
  <div class="row"><span>Opacity</span><input id="opacity" type="range" min="0" max="100" value="80"><span id="opv">80%</span></div>
  <div id="layers"></div>
  <div id="legend"></div>
  <p id="coords">Click the map to read a coordinate.</p>
</div>
<div id="map"></div>
<script>
const CFG = __CONFIG__;
const map = L.map('map', { zoomControl: true });
map.createPane('rasters').style.zIndex = 350;  // below vector polygons (overlayPane = 400)
L.control.scale({ imperial: false }).addTo(map);

const baseSel = document.getElementById('basemap');
const baseLayers = CFG.basemaps.map((b, i) => {
  const layer = L.tileLayer(b.url, { maxZoom: 20, maxNativeZoom: 19,
    attribution: b.url.includes('openstreetmap') ? '&copy; OpenStreetMap contributors' : 'Esri World Imagery (visual reference only)' });
  const opt = document.createElement('option'); opt.value = i; opt.textContent = b.name; baseSel.appendChild(opt);
  return layer;
});
const noneOpt = document.createElement('option'); noneOpt.value = -1; noneOpt.textContent = 'None'; baseSel.appendChild(noneOpt);
let activeBase = null;
function setBase(i) {
  if (activeBase) map.removeLayer(activeBase);
  activeBase = i >= 0 ? baseLayers[i].addTo(map) : null;
  if (activeBase) activeBase.bringToBack();
}
const firstVisible = Math.max(0, CFG.basemaps.findIndex(b => b.visible));
baseSel.value = CFG.basemaps.length ? firstVisible : -1; setBase(+baseSel.value);
baseSel.onchange = () => setBase(+baseSel.value);

const aoi = L.geoJSON(CFG.aoi, { style: { color: '#000', weight: 2, dashArray: '6 4', fill: false }, interactive: false }).addTo(map);
// Re-fit whenever the map gets its real size (until the user pans/zooms), otherwise a map laid out
// while hidden or still 0 px wide starts at the maximum zoom level.
let userMoved = false;
['pointerdown', 'wheel', 'keydown'].forEach(ev => map.getContainer().addEventListener(ev, () => { userMoved = true; }));
const fit = () => { map.invalidateSize(); if (!userMoved) map.fitBounds(aoi.getBounds(), { padding: [20, 20], animate: false }); };
fit(); window.addEventListener('load', fit);
new ResizeObserver(fit).observe(map.getContainer());
const p = CFG.aoi.features[0].properties;
L.circleMarker([p.lat, p.lon], { radius: 4, color: '#000', weight: 1, fillColor: '#fff', fillOpacity: 1 })
  .bindTooltip('Input coordinate ' + p.lat.toFixed(6) + ', ' + p.lon.toFixed(6)).addTo(map);

let opacity = 0.8;
const rasters = [];
const order = [];  // most recently switched-on raster decides the legend
const legendEl = document.getElementById('legend');

function popupHtml(props) {
  return '<table>' + Object.entries(props).map(([k, v]) =>
    '<tr><td><b>' + k + '</b></td><td>' + String(v).replace(/</g, '&lt;') + '</td></tr>').join('') + '</table>';
}
function makeLayer(spec) {
  if (spec.kind === 'vector') {
    return L.geoJSON(spec.geojson, {
      style: { color: '#e31a1c', weight: 2, fillColor: '#e31a1c', fillOpacity: 0.15 },
      onEachFeature: (f, l) => l.bindPopup(popupHtml(f.properties)) });
  }
  const layer = L.imageOverlay(spec.image, spec.bounds, { opacity, interactive: false, pane: 'rasters' });
  rasters.push(layer);
  return layer;
}
function fmt(v) { return Math.abs(v) >= 100 ? v.toFixed(0) : Math.abs(v) >= 1 ? v.toFixed(1) : v.toFixed(2); }
function showLegend() {
  const spec = order[order.length - 1];
  if (!spec || !spec.legend) { legendEl.innerHTML = ''; return; }
  const s = spec.legend.stops, lo = s[0][0], hi = s[s.length - 1][0], span = (hi - lo) || 1;
  const grad = s.map(([v, c]) => c + ' ' + ((v - lo) / span * 100).toFixed(1) + '%').join(', ');
  legendEl.innerHTML = '<b>' + spec.name + '</b><div class="bar" style="background: linear-gradient(90deg, ' + grad +
    '), repeating-conic-gradient(#ddd 0 25%, #fff 0 50%) 0 0 / 8px 8px"></div><div class="ticks"><span>' + fmt(lo) +
    '</span><span>' + fmt((lo + hi) / 2) + '</span><span>' + fmt(hi) + '</span></div>';
}

const layersEl = document.getElementById('layers');
CFG.groups.forEach((g, gi) => {
  const det = document.createElement('details'); det.open = gi === 0;
  det.innerHTML = '<summary>' + g.name + '</summary>';
  g.layers.forEach(spec => {
    const layer = makeLayer(spec);
    const lab = document.createElement('label');
    const cb = document.createElement('input'); cb.type = 'checkbox';
    lab.appendChild(cb); lab.appendChild(document.createTextNode(spec.name));
    cb.onchange = () => {
      const i = order.indexOf(spec); if (i >= 0) order.splice(i, 1);
      if (cb.checked) { layer.addTo(map); if (spec.kind === 'raster') order.push(spec); }
      else map.removeLayer(layer);
      showLegend();
    };
    if (spec.visible) { cb.checked = true; cb.onchange(); }
    det.appendChild(lab);
  });
  layersEl.appendChild(det);
});

const opEl = document.getElementById('opacity');
opEl.oninput = () => { opacity = opEl.value / 100; document.getElementById('opv').textContent = opEl.value + '%';
                       rasters.forEach(r => r.setOpacity(opacity)); };
map.on('click', e => { document.getElementById('coords').textContent =
  'Clicked: ' + e.latlng.lat.toFixed(6) + ', ' + e.latlng.lng.toFixed(6); });
</script>
</body>
</html>
"""
