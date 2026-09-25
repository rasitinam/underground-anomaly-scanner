"""Phase 11: simple, self-contained HTML report (no template engine)."""
from __future__ import annotations

import base64
import datetime as dt
import warnings
from html import escape
from pathlib import Path

import numpy as np
import rasterio
from rasterio.errors import NotGeoreferencedWarning
from rasterio.io import MemoryFile

DISCLAIMER_EN = (
    "Satellite remote sensing data does not directly image underground objects. This software detects "
    "surface and subsurface-related anomalies indirectly. Confirming an underground structure requires "
    "ground-based geophysical methods such as GPR, electrical resistivity tomography, seismic methods, "
    "or other appropriate field measurements."
)
DISCLAIMER_TR = (
    "Uydu uzaktan algılama verileri yeraltındaki nesneleri doğrudan görüntülemez. Bu yazılım yalnızca "
    "yüzeyde ortaya çıkan ve yeraltıyla ilişkili olabilecek anomalileri dolaylı olarak tespit eder. "
    "Yeraltında bir yapının varlığını doğrulamak için GPR (yere nüfuz eden radar), elektrik özdirenç "
    "tomografisi (ERT), sismik yöntemler veya uygun diğer saha ölçümleri gerekir. GPR, uydu analizinin "
    "alternatifi değil; gerektiğinde uygulanan bir saha doğrulama yöntemidir."
)

DATA_SOURCES = [
    ("Sentinel-2 L2A (ESA Copernicus)", "https://planetarycomputer.microsoft.com/dataset/sentinel-2-l2a",
     "Copernicus Sentinel data terms: free, full and open. Attribution: 'Contains modified Copernicus Sentinel data'."),
    ("Sentinel-1 GRD / RTC (ESA Copernicus)", "https://planetarycomputer.microsoft.com/dataset/sentinel-1-grd",
     "Copernicus Sentinel data terms: free, full and open. RTC derived-product terms: see "
     "https://planetarycomputer.microsoft.com/dataset/sentinel-1-rtc"),
    ("Copernicus DEM GLO-30 (ESA / EU)", "https://planetarycomputer.microsoft.com/dataset/cop-dem-glo-30",
     "Copernicus DEM licence, free with attribution: (c) DLR e.V. 2010-2014 and (c) Airbus Defence and "
     "Space GmbH 2014-2018, provided under COPERNICUS by the European Union and ESA."),
    ("OpenStreetMap (basemap)", "https://www.openstreetmap.org/copyright",
     "ODbL, (c) OpenStreetMap contributors. Tile usage policy: https://operations.osmfoundation.org/policies/tiles/"),
    ("Esri World Imagery (QGIS basemap only)", "https://www.arcgis.com/home/item.html?id=10df2279f9684e4a9f6a7f08febac2a9",
     "Visual reference only; NOT used in the analysis and not open data. Subject to Esri terms of use; "
     "remove it by emptying 'satellite_basemap_xyz_url' in config.json."),
    ("STAC access: Microsoft Planetary Computer", "https://planetarycomputer.microsoft.com/",
     "Free, anonymous access (no account or API key). Hosts the official data unchanged as COGs."),
]

LIMITATIONS_EN = [
    "All indicators are measured at or near the surface (optical: top of canopy/soil; C-band SAR: a few cm "
    "into dry soil at most; DEM: surface elevation).",
    "Sentinel-2 pixels are 10-20 m and the Copernicus DEM is 30 m: features smaller than ~2-3 pixels cannot be resolved.",
    "Anomalies are frequently caused by ordinary surface factors: field boundaries, crops, irrigation, soil moisture, "
    "roads, buildings, recent construction, shadows, clouds, and seasonal changes.",
    "Without ESA SNAP terrain correction, Sentinel-1 GRD positions can be shifted in hilly terrain; see the "
    "calibration note of each SAR scene.",
    "The weights (SAR 0.35, vegetation 0.30, terrain 0.20, temporal 0.15) are a transparent heuristic, NOT calibrated "
    "against ground truth. The score expresses the consistency of several surface anomalies, not the probability "
    "of an underground object.",
]
LIMITATIONS_TR = [
    "Tüm göstergeler yüzeyde ya da yüzeye çok yakın ölçülür (optik: bitki/toprak yüzeyi; C-bant SAR: kuru toprakta "
    "en fazla birkaç cm; DEM: yüzey yüksekliği).",
    "Sentinel-2 piksel boyutu 10-20 m, Copernicus DEM 30 m'dir; ~2-3 pikselden küçük özellikler ayırt edilemez.",
    "Anomaliler çoğunlukla sıradan yüzey etkenlerinden kaynaklanır: tarla sınırları, ekin, sulama, toprak nemi, yollar, "
    "binalar, yeni inşaat, gölge, bulut ve mevsimsel değişim.",
    "Ağırlıklar sezgiseldir ve saha verisiyle kalibre edilmemiştir. Skor, 'yeraltı nesnesi olasılığı' DEĞİL, birden fazla "
    "yüzey anomalisinin tutarlılık derecesidir (anomaly consistency score).",
    "Herhangi bir yorumdan önce saha gözlemi ve gerekiyorsa GPR / ERT / sismik gibi yersel jeofizik ölçümler yapılmalıdır.",
]


def _png_data_uri(rgb: np.ndarray) -> str:
    """rgb: (3, H, W) uint8 -> base64 PNG data URI."""
    _, h, w = rgb.shape
    with warnings.catch_warnings(), MemoryFile() as mem:
        warnings.simplefilter("ignore", NotGeoreferencedWarning)
        with mem.open(driver="PNG", width=w, height=h, count=3, dtype="uint8") as dst:
            dst.write(rgb)
        return "data:image/png;base64," + base64.b64encode(mem.read()).decode("ascii")


def _quicklook_rgb(path: Path) -> str | None:
    if not path.exists():
        return None
    with rasterio.open(path) as src:
        return _png_data_uri(src.read()[:3])


def _quicklook_score(path: Path) -> str | None:
    if not path.exists():
        return None
    with rasterio.open(path) as src:
        s = src.read(1)
    s = np.nan_to_num(s, nan=0.0).clip(0, 1)
    r = (255 * np.clip(s * 2, 0, 1)).astype("uint8")
    g = (255 * np.clip(2 - s * 2, 0, 1) * (s > 0)).astype("uint8")
    b = (40 * (1 - s)).astype("uint8")
    return _png_data_uri(np.stack([r, g, b]))


def _table(headers: list[str], rows: list[list]) -> str:
    head = "".join(f"<th>{escape(str(h))}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{escape('' if c is None else str(c))}</td>" for c in r) + "</tr>"
                   for r in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def build_report(ctx: dict, out_path: Path) -> Path:
    aoi = ctx["aoi"]
    s2, s1, dem, an = ctx.get("s2"), ctx.get("s1"), ctx.get("dem"), ctx.get("anomaly")
    out_dir = out_path.parent
    parts: list[str] = []

    parts.append(f"<div class='warn'><b>EN:</b> {escape(DISCLAIMER_EN)}<br><br><b>TR:</b> {escape(DISCLAIMER_TR)}</div>")

    parts.append("<h2>Study area / Çalışma alanı</h2>")
    parts.append(_table(["Item", "Value"], [
        ["Coordinate (WGS84)", f"{aoi.lat:.6f}, {aoi.lon:.6f}"],
        ["Radius", f"{aoi.radius_m:.0f} m (square AOI {2 * aoi.radius_m:.0f} x {2 * aoi.radius_m:.0f} m)"],
        ["Working CRS", f"EPSG:{aoi.utm_epsg} (UTM, auto-selected)"],
        ["Grid resolution", f"{ctx['grid'].resolution_m} m ({ctx['grid'].width} x {ctx['grid'].height} px)"],
        ["Run time (local)", dt.datetime.now().strftime("%Y-%m-%d %H:%M")],
    ]))

    parts.append("<h2>Data sources / Veri kaynakları</h2>")
    parts.append(_table(["Source", "URL", "License / terms"], [list(r) for r in DATA_SOURCES]))

    parts.append("<h2>Sentinel-2 scenes</h2>")
    if s2:
        rows = []
        roles = [("selected", s2["scene"]), ("reference (temporal)", s2.get("reference"))]
        roles += [("median composite", c) for c in s2.get("composite_scenes") or []]
        for role, sc in roles:
            if sc:
                rows.append([role, sc["id"], sc["datetime"][:10], sc.get("cloud_cover_pct"),
                             sc.get("aoi_valid_pct"), sc.get("platform")])
        parts.append(_table(["Role", "Product", "Acquisition date", "Scene cloud %", "AOI clear %", "Platform"], rows))
        thr = s2.get("cloud_threshold_used")
        parts.append(f"<p>Cloud-cover rule used: {'<= ' + str(thr) + '%' if thr else 'fallback: least cloudy scene (above 30%)'}</p>")
    else:
        parts.append("<p class='na'>Sentinel-2 unavailable in this run.</p>")

    parts.append("<h2>Sentinel-1 scenes</h2>")
    if s1:
        parts.append(_table(["Product", "Acquisition date", "Orbit", "Rel. orbit", "Mode", "Processing / calibration"],
                            [[s["id"], s["datetime"][:10], s.get("orbit_state"), s.get("relative_orbit"),
                              s.get("instrument_mode"), s.get("calibration")] for s in s1["scenes"]]))
    else:
        parts.append("<p class='na'>Sentinel-1 unavailable in this run.</p>")

    parts.append("<h2>DEM</h2>")
    if dem:
        parts.append(f"<p>Copernicus DEM GLO-30 (native ~30 m, resampled to the analysis grid). Tiles: "
                     f"{escape(', '.join(t['id'] for t in dem['tiles']))}. Elevation range: "
                     f"{np.nanmin(dem['dem']):.1f} - {np.nanmax(dem['dem']):.1f} m.</p>")
    else:
        parts.append("<p class='na'>DEM unavailable in this run.</p>")

    parts.append("<h2>Anomaly analysis / Anomali analizi</h2>")
    if an:
        st = an["stats"]
        parts.append("<p>Method: local z-score <code>z = (pixel - local_mean) / local_std</code> per source, "
                     "mapped to 0-1, then weighted mean over available sources. A pixel is part of a "
                     "<b>MULTI-SOURCE ANOMALY</b> when at least "
                     f"{st['sources_required_for_multi_source']} independent sources agree and the "
                     "consistency score is &ge; 0.5.</p>")
        parts.append(_table(["Metric", "Value"], [
            ["Sources used", ", ".join(st["sources_used"])],
            ["Weights used", ", ".join(f"{k}={v}" for k, v in st["weights_used"].items())],
            ["Flagged pixels per source (%)", ", ".join(f"{k}: {v}" for k, v in st["flagged_pct_per_source"].items())],
            ["Anomaly consistency score (mean / p95 / max)", f"{st['score_mean']} / {st['score_p95']} / {st['score_max']}"],
            ["Multi-source anomaly area", f"{st['multi_source_area_m2']} m² ({st['multi_source_pct']}% of AOI)"],
            ["Number of multi-source anomaly areas", st["n_polygons"]],
        ]))
        if an["polygons"]:
            parts.append("<h3>Detected anomaly areas (surface indicators only)</h3>")
            parts.append(_table(
                ["#", "Class", "Centroid lat", "Centroid lon", "Area m²", "Mean score", "Max score", "Indicators"],
                [[p["properties"][k] for k in ("id", "class", "centroid_lat", "centroid_lon", "area_m2",
                                                 "mean_consistency_score", "max_consistency_score", "indicators")]
                 for p in an["polygons"][:50]]))
        else:
            parts.append("<p>No multi-source anomaly area passed the thresholds. This does not mean there is "
                         "nothing underground; it only means the surface indicators do not agree.</p>")
    else:
        parts.append("<p class='na'>Anomaly analysis could not be run.</p>")

    rgb_path = out_dir / "s2_rgb_median.tif"
    if not rgb_path.exists():
        rgb_path = out_dir / "s2_rgb.tif"
    imgs = [(f"Sentinel-2 RGB ({'multi-date median' if 'median' in rgb_path.name else 'latest scene'}, 10 m)",
             _quicklook_rgb(rgb_path)),
            ("Anomaly consistency score", _quicklook_score(out_dir / "anomaly.tif"))]
    imgs = [(t, u) for t, u in imgs if u]
    if imgs:
        parts.append("<h2>Quicklooks</h2><div class='imgs'>" + "".join(
            f"<figure><img src='{u}' alt='{escape(t)}'><figcaption>{escape(t)}</figcaption></figure>" for t, u in imgs)
            + "</div>")

    parts.append("<h2>Limitations / Sınırlamalar</h2><ul>"
                 + "".join(f"<li>{escape(x)}</li>" for x in LIMITATIONS_EN) + "</ul><ul>"
                 + "".join(f"<li>{escape(x)}</li>" for x in LIMITATIONS_TR) + "</ul>")

    if ctx.get("warnings"):
        parts.append("<h2>Processing warnings</h2><ul>" + "".join(f"<li>{escape(w)}</li>" for w in ctx["warnings"]) + "</ul>")

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Surface anomaly report</title>
<style>
body {{ font-family: Segoe UI, Arial, sans-serif; max-width: 1100px; margin: 24px auto; padding: 0 16px; color: #222; }}
h1 {{ margin-bottom: 4px; }} h2 {{ border-bottom: 1px solid #ccc; padding-bottom: 4px; margin-top: 28px; }}
table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
th, td {{ border: 1px solid #ddd; padding: 5px 8px; text-align: left; vertical-align: top; word-break: break-word; }}
th {{ background: #f2f2f2; }}
.warn {{ background: #fff4e5; border-left: 5px solid #e67e22; padding: 12px 16px; font-size: 14px; }}
.na {{ color: #a33; }}
.imgs {{ display: flex; flex-wrap: wrap; gap: 16px; }}
figure {{ margin: 0; }} img {{ width: 320px; image-rendering: pixelated; border: 1px solid #ccc; }}
</style></head><body>
<h1>Surface anomaly research report</h1>
<p>Yüzey anomali araştırma raporu &mdash; indirect indicators only / yalnızca dolaylı göstergeler</p>
{''.join(parts)}
</body></html>"""
    out_path.write_text(html, encoding="utf-8")
    return out_path
