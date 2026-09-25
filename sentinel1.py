"""
Phase 8-9: Sentinel-1 C-band SAR (VV / VH backscatter), multi-date.

Processing levels, best first:
  1. ESA SNAP (if installed AND a full SAFE product is supplied with --s1-safe):
     Apply Orbit File -> Calibration (sigma0) -> Speckle Filter -> Terrain Correction.
  2. Planetary Computer `sentinel-1-rtc`: already radiometrically terrain corrected
     (gamma0), georeferenced COGs -- used if it is accessible anonymously.
  3. Planetary Computer `sentinel-1-grd` (fallback): GCP geocoding (NO terrain
     correction), approximate sigma0 calibration from the product's own calibration
     LUT, Lee speckle filter. Positions may be shifted in hilly terrain; treated
     as a relative indicator only.

Only scenes from the same relative orbit and pass direction are compared over
time, so viewing geometry is constant and calibration offsets largely cancel.
"""
from __future__ import annotations

import subprocess
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

import numpy as np
import rasterio
from rasterio.control import GroundControlPoint
from rasterio.enums import Resampling
from rasterio.warp import reproject
from rasterio.windows import Window
from scipy.ndimage import uniform_filter

import catalog
from aoi import AOI
from logger import log
from raster_utils import GDAL_REMOTE_ENV, Grid, cached_read_to_grid, nan_reduce, save_geotiff

POLARIZATIONS = ("vv", "vh")


# ---------------------------------------------------------------- helpers

def lee_filter(power: np.ndarray, size: int = 5) -> np.ndarray:
    """Classic Lee speckle filter on linear power (NaN-aware)."""
    valid = np.isfinite(power)
    x = np.where(valid, power, 0.0).astype("float64")
    w = np.maximum(uniform_filter(valid.astype("float64"), size), 1e-9)
    mean = uniform_filter(x, size) / w
    mean_sq = uniform_filter(x * x, size) / w
    var = np.maximum(mean_sq - mean * mean, 0.0)
    noise_var = np.nanmean(var[valid]) if valid.any() else 0.0
    weight = var / np.maximum(var + noise_var, 1e-12)
    out = mean + weight * (x - mean)
    out[~valid] = np.nan
    return out.astype("float32")


def to_db(power: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        db = 10.0 * np.log10(power)
    db[~np.isfinite(db)] = np.nan
    return db.astype("float32")


def read_gcp_window_to_grid(href: str, grid: Grid, margin_px: int = 200, n_local_gcps: int = 16) -> np.ndarray:
    """Crop + geocode a GCP-referenced raster (GRD) without reading the full scene."""
    from pyproj import Transformer

    with rasterio.Env(**GDAL_REMOTE_ENV), rasterio.open(href) as src:
        gcps, gcp_crs = src.gcps
        if not gcps:
            raise ValueError("Raster has no GCPs")
        to_gcp_crs = Transformer.from_crs(f"EPSG:{grid.epsg}", gcp_crs, always_xy=True)
        minx = grid.transform.c
        maxy = grid.transform.f
        maxx = minx + grid.width * grid.resolution_m
        miny = maxy - grid.height * grid.resolution_m
        corners = [to_gcp_crs.transform(x, y) for x, y in [(minx, miny), (minx, maxy), (maxx, miny), (maxx, maxy)]]
        cx = np.mean([c[0] for c in corners])
        cy = np.mean([c[1] for c in corners])

        pts = np.array([[g.x, g.y, g.col, g.row] for g in gcps])
        nearest = np.argsort(np.hypot(pts[:, 0] - cx, pts[:, 1] - cy))[:max(n_local_gcps, 6)]
        local = pts[nearest]
        design = np.column_stack([local[:, 0], local[:, 1], np.ones(len(local))])
        col_coef, *_ = np.linalg.lstsq(design, local[:, 2], rcond=None)
        row_coef, *_ = np.linalg.lstsq(design, local[:, 3], rcond=None)
        cols = [col_coef @ [x, y, 1] for x, y in corners]
        rows = [row_coef @ [x, y, 1] for x, y in corners]

        col_off = int(max(0, np.floor(min(cols)) - margin_px))
        row_off = int(max(0, np.floor(min(rows)) - margin_px))
        col_end = int(min(src.width, np.ceil(max(cols)) + margin_px))
        row_end = int(min(src.height, np.ceil(max(rows)) + margin_px))
        if col_end <= col_off or row_end <= row_off:
            raise ValueError("AOI is outside this SAR scene")
        window = Window(col_off, row_off, col_end - col_off, row_end - row_off)
        data = src.read(1, window=window).astype("float32")
        data[data == 0] = np.nan

    shifted = [
        GroundControlPoint(row=r - row_off, col=c - col_off, x=x, y=y)
        for x, y, c, r in local
    ]
    dst = np.full((grid.height, grid.width), np.nan, dtype="float32")
    reproject(
        data, dst, gcps=shifted, src_crs=gcp_crs, src_nodata=np.nan,
        dst_transform=grid.transform, dst_crs=f"EPSG:{grid.epsg}", dst_nodata=np.nan,
        resampling=Resampling.bilinear,
    )
    return dst


def grd_calibration_constant(item, pol: str) -> float | None:
    """Median sigma0 calibration gain A from the product's calibration LUT (sigma0 = DN^2 / A^2)."""
    import requests

    asset = item.assets.get(f"schema-calibration-{pol}")
    if asset is None:
        return None
    try:
        xml_text = requests.get(asset.href, timeout=30).text
        root = ET.fromstring(xml_text)
        values = []
        for node in root.iter("sigmaNought"):
            values.extend(float(v) for v in node.text.split())
        return float(np.median(values)) if values else None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- scene selection

def _summary(item) -> dict:
    p = item.properties
    return {
        "id": item.id,
        "collection": item.collection_id,
        "datetime": catalog.item_date(item).isoformat(),
        "platform": p.get("platform"),
        "orbit_state": p.get("sat:orbit_state"),
        "relative_orbit": p.get("sat:relative_orbit"),
        "instrument_mode": p.get("sar:instrument_mode"),
        "polarizations": p.get("sar:polarizations"),
    }


def select_scene_stack(items: list, aoi: AOI, max_scenes: int) -> list[dict]:
    covering = [it for it in items if catalog.covers_aoi(it, aoi)
                and {"VV", "VH"} <= set(it.properties.get("sar:polarizations", []))]
    if not covering:
        raise catalog.DataSourceUnavailable("No dual-pol (VV+VH) Sentinel-1 scene fully covers the AOI.")
    groups: dict[tuple, list] = defaultdict(list)
    for it in covering:
        groups[(it.properties.get("sat:relative_orbit"), it.properties.get("sat:orbit_state"))].append(it)
    # Track containing the most recent acquisition; ties -> more scenes.
    best = max(groups.values(), key=lambda g: (max(catalog.item_date(i) for i in g), len(g)))
    best.sort(key=catalog.item_date)
    if len(best) > max_scenes:
        idx = np.unique(np.round(np.linspace(0, len(best) - 1, max_scenes)).astype(int))
        best = [best[i] for i in idx]
    return [_summary(it) for it in best]


# ---------------------------------------------------------------- reading

def read_scene(scene: dict, grid: Grid, cache_dir: Path, get_stac) -> dict[str, np.ndarray]:
    """Returns calibrated, speckle-filtered backscatter in dB for VV and VH."""
    out: dict[str, np.ndarray] = {}
    item = None
    for pol in POLARIZATIONS:
        cache_path = cache_dir / scene["id"] / f"{pol}_db.tif"
        if cache_path.exists():
            with rasterio.open(cache_path) as src:
                out[pol] = src.read(1)
            continue
        if item is None:
            stac = get_stac()
            if stac is None:
                raise catalog.DataSourceUnavailable("SAR scene not cached and catalog unavailable (offline).")
            item = catalog.fetch_item(stac, scene["collection"], scene["id"])
        href = item.assets[pol].href
        if scene["collection"] == "sentinel-1-rtc":
            power = cached_read_to_grid(href, grid, cache_dir / scene["id"] / f"{pol}_raw.tif")
            scene["calibration"] = "gamma0 RTC (terrain corrected, Planetary Computer)"
        else:
            dn = read_gcp_window_to_grid(href, grid)
            gain = grd_calibration_constant(item, pol)
            if gain:
                power = dn * dn / (gain * gain)
                scene["calibration"] = "approx. sigma0 (median LUT gain), GCP geocoded, NOT terrain corrected"
            else:
                power = dn * dn
                scene["calibration"] = "uncalibrated DN^2 (relative only), GCP geocoded, NOT terrain corrected"
        db = to_db(lee_filter(power))
        save_geotiff(cache_path, db, grid)
        out[pol] = db
    return out


# ---------------------------------------------------------------- SNAP (optional)

SNAP_GRAPH = """<graph id="S1_preprocessing">
  <version>1.0</version>
  <node id="Read"><operator>Read</operator><parameters><file>{safe}</file></parameters></node>
  <node id="Orbit"><operator>Apply-Orbit-File</operator><sources><sourceProduct refid="Read"/></sources>
    <parameters><continueOnFail>true</continueOnFail></parameters></node>
  <node id="Calib"><operator>Calibration</operator><sources><sourceProduct refid="Orbit"/></sources>
    <parameters><outputSigmaBand>true</outputSigmaBand><selectedPolarisations>VV,VH</selectedPolarisations></parameters></node>
  <node id="Speckle"><operator>Speckle-Filter</operator><sources><sourceProduct refid="Calib"/></sources>
    <parameters><filter>Lee</filter><filterSizeX>5</filterSizeX><filterSizeY>5</filterSizeY></parameters></node>
  <node id="TC"><operator>Terrain-Correction</operator><sources><sourceProduct refid="Speckle"/></sources>
    <parameters><demName>Copernicus 30m Global DEM</demName><pixelSpacingInMeter>10.0</pixelSpacingInMeter>
    <mapProjection>EPSG:{epsg}</mapProjection><geoRegion>{wkt}</geoRegion></parameters></node>
  <node id="dB"><operator>LinearToFromdB</operator><sources><sourceProduct refid="TC"/></sources></node>
  <node id="Write"><operator>Write</operator><sources><sourceProduct refid="dB"/></sources>
    <parameters><file>{out}</file><formatName>GeoTIFF</formatName></parameters></node>
</graph>
"""


def run_snap(gpt: str, safe_path: Path, aoi: AOI, work_dir: Path) -> Path:
    minx, miny, maxx, maxy = aoi.bounds_wgs84
    wkt = f"POLYGON(({minx} {miny},{maxx} {miny},{maxx} {maxy},{minx} {maxy},{minx} {miny}))"
    out_tif = work_dir / "snap_s1_sigma0_db.tif"
    graph = work_dir / "snap_s1_graph.xml"
    work_dir.mkdir(parents=True, exist_ok=True)
    graph.write_text(SNAP_GRAPH.format(safe=safe_path, epsg=aoi.utm_epsg, wkt=wkt, out=out_tif), encoding="utf-8")
    log(f"SNAP: running {graph.name} (this can take several minutes)...")
    proc = subprocess.run([gpt, str(graph)], capture_output=True, text=True, timeout=3600)
    if proc.returncode != 0 or not out_tif.exists():
        raise RuntimeError(f"SNAP gpt failed: {proc.stderr[-800:]}")
    return out_tif


def read_snap_output(tif: Path, grid: Grid) -> dict[str, np.ndarray]:
    out: dict[str, np.ndarray] = {}
    with rasterio.open(tif) as src:
        names = [(d or "").lower() for d in src.descriptions]
    for pol in POLARIZATIONS:
        band = next((i + 1 for i, n in enumerate(names) if pol in n), None)
        if band is None:
            band = 1 if pol == "vv" else 2
        out[pol] = cached_read_to_grid(str(tif), grid, tif.parent / f"snap_{pol}_grid.tif", band=band)
    return out


# ---------------------------------------------------------------- main entry

def run(aoi: AOI, grid: Grid, config: dict, cache_root: Path, out_dir: Path,
        refresh: bool = False, snap_gpt: str | None = None, s1_safe: Path | None = None) -> dict:
    cache_dir = catalog.aoi_cache_dir(cache_root, aoi) / "sentinel1"
    selection_path = cache_dir / "selection.json"
    selection = None if refresh else catalog.load_selection(selection_path)
    stac_holder: dict = {}

    def get_stac():
        if "stac" not in stac_holder:
            try:
                stac_holder["stac"] = catalog.open_catalog(config["stac_endpoint"])
            except catalog.DataSourceUnavailable:
                stac_holder["stac"] = None
        return stac_holder["stac"]

    scenes: list[dict] = []
    stack: list[dict[str, np.ndarray]] = []

    if selection is not None:
        log("Sentinel-1: using cached scene selection")
        scenes = selection["scenes"]
        stack = [read_scene(s, grid, cache_dir, get_stac) for s in scenes]
    else:
        stac = get_stac()
        if stac is None:
            raise catalog.DataSourceUnavailable("Sentinel-1 catalog unreachable (offline?).")
        last_error: Exception | None = None
        for collection in config.get("sentinel1_collections", ["sentinel-1-rtc", "sentinel-1-grd"]):
            try:
                items = catalog.search_items(stac, collection, aoi, config["sentinel1_lookback_days"])
                scenes = select_scene_stack(items, aoi, config["sentinel1_max_scenes"])
                stack = [read_scene(s, grid, cache_dir, get_stac) for s in scenes]
                log(f"Sentinel-1: using collection '{collection}' ({len(scenes)} scene(s), "
                    f"relative orbit {scenes[0]['relative_orbit']}, {scenes[0]['orbit_state']})")
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                scenes, stack = [], []
                log(f"Sentinel-1: collection '{collection}' not usable ({type(exc).__name__}: {exc})", "WARNING")
        if not stack:
            raise catalog.DataSourceUnavailable(f"No usable Sentinel-1 data: {last_error}")
        catalog.save_selection(selection_path, {"scenes": scenes})

    if snap_gpt and s1_safe:
        try:
            snap_tif = run_snap(snap_gpt, s1_safe, aoi, cache_dir / "snap")
            stack[-1] = read_snap_output(snap_tif, grid)
            scenes[-1] = {**scenes[-1], "calibration": "sigma0 via ESA SNAP (orbit, calibration, Lee, terrain correction)",
                          "snap_input": str(s1_safe)}
            log("Sentinel-1: latest scene replaced by SNAP-preprocessed product")
        except Exception as exc:  # noqa: BLE001
            log(f"SNAP processing failed, keeping fallback SAR processing: {exc}", "WARNING")
    elif snap_gpt:
        log("SNAP detected. For full preprocessing pass a downloaded SAFE product with --s1-safe "
            "(free account at https://dataspace.copernicus.eu).")

    latest = stack[-1]
    vv_stack = np.stack([s["vv"] for s in stack])
    paths = {
        "s1_vv": save_geotiff(out_dir / "s1_vv_db.tif", latest["vv"], grid),
        "s1_vh": save_geotiff(out_dir / "s1_vh_db.tif", latest["vh"], grid),
        "s1_vv_vh_ratio": save_geotiff(out_dir / "s1_vv_minus_vh_db.tif", latest["vv"] - latest["vh"], grid),
    }
    change = None
    temporal_std = None
    if len(stack) >= 2:
        change = (latest["vv"] - stack[0]["vv"]).astype("float32")
        temporal_std = nan_reduce(vv_stack, how="std")
        paths["s1_vv_change"] = save_geotiff(out_dir / "s1_vv_change_db.tif", change, grid)
        paths["s1_vv_temporal_std"] = save_geotiff(out_dir / "s1_vv_temporal_std_db.tif", temporal_std, grid)
    else:
        log("Sentinel-1: only one scene available; temporal SAR comparison skipped.", "WARNING")

    for s in scenes:
        log(f"Sentinel-1 scene: {s['id']} ({s['datetime'][:10]}) - {s.get('calibration', '')}")
    return {"scenes": scenes, "paths": paths, "vv": latest["vv"], "vh": latest["vh"],
            "vv_stack": vv_stack, "vv_change": change, "vv_temporal_std": temporal_std}
