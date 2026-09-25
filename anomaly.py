"""
Phase 10: explainable multi-source SURFACE anomaly analysis.

For every source we compute a local z-score
    z = (pixel - local_mean) / local_std     (moving square window)
and map it to a 0..1 score: score = clip(|z| / (2 * threshold), 0, 1)
(so |z| = threshold -> 0.5, |z| >= 2*threshold -> 1.0).

Sources:
  vegetation : NDVI (Sentinel-2)
  sar        : mean of |z| of VV and VH backscatter in dB (Sentinel-1)
  terrain    : local relief model = DEM - smoothed DEM (Copernicus DEM)
  temporal   : PERSISTENCE of the SAR / NDVI anomaly across acquisition dates
               (fraction of dates in which the pixel is anomalous)

The combined value is an "anomaly consistency score": how consistently several
independent surface indicators deviate from their surroundings. It is NOT a
probability that anything exists underground.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from pyproj import Transformer
from rasterio import features
from shapely.geometry import mapping, shape
from shapely.ops import transform as shapely_transform

from logger import log
from raster_utils import Grid, local_zscore, nan_reduce, save_geotiff

SOURCE_LABELS = {
    "vegetation": "vegetation anomaly (NDVI)",
    "sar": "SAR backscatter anomaly",
    "terrain": "terrain anomaly (local relief)",
    "temporal": "temporal persistence",
}
MIN_POLYGON_PIXELS = 4


def z_to_score(z: np.ndarray, threshold: float) -> np.ndarray:
    return np.clip(np.abs(z) / (2.0 * threshold), 0.0, 1.0).astype("float32")


def effective_window(grid: Grid, window_px: int) -> int:
    # A feature covering fraction f of the window caps |z| at sqrt((1-f)/f), so the window
    # must be much larger than the features of interest; clamp to the AOI size.
    w = min(window_px, max(5, min(grid.width, grid.height) - 1))
    return w if w % 2 == 1 else w - 1


def persistence(arrays: list[np.ndarray], window: int, threshold: float) -> np.ndarray | None:
    """Fraction of dates in which |local z| >= threshold (NaN where no valid date)."""
    valid_arrays = [a for a in arrays if a is not None and np.isfinite(a).any()]
    if len(valid_arrays) < 2:
        return None
    flags = []
    for a in valid_arrays:
        z = local_zscore(a, window)
        f = np.where(np.isfinite(z), (np.abs(z) >= threshold).astype("float32"), np.nan)
        flags.append(f)
    return nan_reduce(np.stack(flags))


def build_source_scores(s2: dict | None, s1: dict | None, terrain: dict | None,
                        grid: Grid, config: dict) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
    thr = config["anomaly_zscore_threshold"]
    win = effective_window(grid, config["local_stats_window_px"])
    scores: dict[str, np.ndarray] = {}
    flags: dict[str, np.ndarray] = {}

    if s2 is not None:
        z = local_zscore(s2["ndvi"], win)
        scores["vegetation"] = z_to_score(z, thr)
        flags["vegetation"] = np.abs(z) >= thr

    if s1 is not None:
        z_vv = np.abs(local_zscore(s1["vv"], win))
        z_vh = np.abs(local_zscore(s1["vh"], win))
        z = nan_reduce(np.stack([z_vv, z_vh]))
        scores["sar"] = z_to_score(z, thr)
        flags["sar"] = z >= thr

    if terrain is not None:
        z = local_zscore(terrain["lrm"], win)
        scores["terrain"] = z_to_score(z, thr)
        flags["terrain"] = np.abs(z) >= thr

    temporal_parts = []
    if s1 is not None and s1.get("vv_stack") is not None and len(s1["vv_stack"]) >= 2:
        p = persistence(list(s1["vv_stack"]), win, thr)
        if p is not None:
            temporal_parts.append(p)
    if s2 is not None and s2.get("ndvi_change") is not None:
        ndvi_ref = s2["ndvi"] - s2["ndvi_change"]
        p = persistence([s2["ndvi"], ndvi_ref], win, thr)
        if p is not None:
            temporal_parts.append(p)
    if temporal_parts:
        temporal = nan_reduce(np.stack(temporal_parts))
        scores["temporal"] = temporal
        flags["temporal"] = temporal >= 0.5

    for name in flags:
        flags[name] = flags[name] & np.isfinite(scores[name])
    log(f"Anomaly: local window {win} px ({win * grid.resolution_m:.0f} m), |z| threshold {thr}")
    return scores, flags


def combine(scores: dict[str, np.ndarray], weights: dict[str, float]) -> np.ndarray:
    """Weighted mean over the sources that have a valid value at each pixel."""
    num = None
    den = None
    for name, score in scores.items():
        w = weights.get(name, 0.0)
        valid = np.isfinite(score)
        contrib = np.where(valid, score * w, 0.0)
        weight = np.where(valid, w, 0.0)
        num = contrib if num is None else num + contrib
        den = weight if den is None else den + weight
    with np.errstate(invalid="ignore", divide="ignore"):
        combined = num / den
    combined[den == 0] = np.nan
    return combined.astype("float32")


def polygonize(mask: np.ndarray, combined: np.ndarray, flags: dict[str, np.ndarray], count: np.ndarray,
               grid: Grid) -> list[dict]:
    to_wgs84 = Transformer.from_crs(f"EPSG:{grid.epsg}", "EPSG:4326", always_xy=True)
    features_out = []
    for geom, value in features.shapes(mask.astype("uint8"), mask=mask, transform=grid.transform):
        if value != 1:
            continue
        poly = shape(geom)
        region = features.rasterize([(geom, 1)], out_shape=mask.shape, transform=grid.transform).astype(bool)
        n_px = int(region.sum())
        if n_px < MIN_POLYGON_PIXELS:
            continue
        sources = [SOURCE_LABELS[n] for n, f in flags.items() if f[region].mean() >= 0.5]
        centroid_lon, centroid_lat = to_wgs84.transform(poly.centroid.x, poly.centroid.y)
        features_out.append({
            "type": "Feature",
            "geometry": mapping(shapely_transform(to_wgs84.transform, poly)),
            "properties": {
                "id": len(features_out) + 1,
                "class": "MULTI-SOURCE ANOMALY",
                "area_m2": round(n_px * grid.resolution_m ** 2, 1),
                "mean_consistency_score": round(float(np.nanmean(combined[region])), 3),
                "max_consistency_score": round(float(np.nanmax(combined[region])), 3),
                "max_sources_agreeing": int(count[region].max()),
                "indicators": ", ".join(sources),
                "centroid_lat": round(centroid_lat, 6),
                "centroid_lon": round(centroid_lon, 6),
                "note": "indirect surface indicator, not a confirmed subsurface feature",
            },
        })
    features_out.sort(key=lambda f: -f["properties"]["mean_consistency_score"])
    for i, f in enumerate(features_out, 1):
        f["properties"]["id"] = i
    return features_out


def run(s2: dict | None, s1: dict | None, terrain: dict | None, grid: Grid, config: dict, out_dir: Path) -> dict:
    scores, flags = build_source_scores(s2, s1, terrain, grid, config)
    if not scores:
        raise RuntimeError("No data source available for anomaly analysis.")

    paths: dict[str, Path] = {}
    for name, score in scores.items():
        paths[f"anomaly_{name}"] = save_geotiff(out_dir / f"anomaly_{name}.tif", score, grid)

    combined = combine(scores, config["anomaly_weights"])
    count = np.sum(np.stack([f.astype("uint8") for f in flags.values()]), axis=0).astype("uint8")
    paths["anomaly"] = save_geotiff(out_dir / "anomaly.tif", combined, grid)
    paths["anomaly_source_count"] = save_geotiff(out_dir / "anomaly_source_count.tif", count, grid,
                                                 dtype="uint8", nodata=None)

    n_sources = len(scores)
    required = min(3, n_sources)
    multi_source_possible = n_sources >= 2
    mask = (count >= required) & (combined >= 0.5) if multi_source_possible else np.zeros_like(count, bool)
    polygons = polygonize(mask, combined, flags, count, grid) if mask.any() else []

    geojson_path = out_dir / "anomaly_areas.geojson"
    geojson_path.write_text(json.dumps({"type": "FeatureCollection", "features": polygons}, indent=1),
                            encoding="utf-8")
    paths["anomaly_areas"] = geojson_path

    valid = np.isfinite(combined)
    stats = {
        "sources_used": list(scores.keys()),
        "weights_used": {k: config["anomaly_weights"].get(k, 0.0) for k in scores},
        "sources_required_for_multi_source": required,
        "multi_source_possible": multi_source_possible,
        "flagged_pct_per_source": {k: round(100.0 * float(f[valid].mean()), 2) if valid.any() else 0.0
                                   for k, f in flags.items()},
        "multi_source_area_m2": round(float(mask.sum()) * grid.resolution_m ** 2, 1),
        "multi_source_pct": round(100.0 * float(mask[valid].mean()), 3) if valid.any() else 0.0,
        "score_mean": round(float(np.nanmean(combined)), 3) if valid.any() else None,
        "score_p95": round(float(np.nanpercentile(combined, 95)), 3) if valid.any() else None,
        "score_max": round(float(np.nanmax(combined)), 3) if valid.any() else None,
        "n_polygons": len(polygons),
    }
    if not multi_source_possible:
        log("Anomaly: only one data source available -> no MULTI-SOURCE anomaly can be declared.", "WARNING")
    log(f"Anomaly: sources={stats['sources_used']}, multi-source areas={len(polygons)}, "
        f"covering {stats['multi_source_pct']}% of AOI")
    return {"paths": paths, "stats": stats, "polygons": polygons}
