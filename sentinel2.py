"""
Phase 4-5: Sentinel-2 L2A scene selection, band reading and spectral indices.

Outputs (all on the common AOI grid):
  latest clear scene : s2_rgb, s2_false_color, ndvi, ndwi, nbr, ndre, ndmi
  multi-date median  : s2_rgb_median, s2_false_color_median, ndvi_median, ... (cloud-masked
                       per-pixel median of up to N clear scenes of the last year: less noise,
                       no single-day effects; it does NOT add spatial detail beyond 10 m)
  ndvi_reference + ndvi_change (scene ~1 year older, same season)
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import numpy as np
from rasterio.enums import Resampling

import catalog
from aoi import AOI
from logger import log
from raster_utils import Grid, cached_read_to_grid, nan_reduce, normalized_difference, save_geotiff, to_uint8_stretch

BANDS = {"blue": "B02", "green": "B03", "red": "B04", "rededge": "B05", "nir": "B08", "nir08": "B8A",
         "swir1": "B11", "swir2": "B12"}
MIN_COMPOSITE_SCENES = 3
# SCL classes treated as invalid: 0 nodata, 1 saturated, 3 cloud shadow, 8/9 cloud, 10 cirrus, 11 snow
SCL_INVALID = {0, 1, 3, 8, 9, 10, 11}
MIN_REFERENCE_GAP_DAYS = 60


def _scene_summary(item) -> dict:
    return {
        "id": item.id,
        "datetime": catalog.item_date(item).isoformat(),
        "cloud_cover_pct": item.properties.get("eo:cloud_cover"),
        "platform": item.properties.get("platform"),
        "mgrs_tile": item.properties.get("s2:mgrs_tile"),
        "processing_baseline": item.properties.get("s2:processing_baseline"),
        "collection": item.collection_id,
    }


def select_scenes(items: list, aoi: AOI, cloud_thresholds: list[float]) -> tuple[dict, dict | None, float | None]:
    """Best (most recent, lowest-cloud) scene with cloud-cover fallback + an older reference scene."""
    covering = [it for it in items if catalog.covers_aoi(it, aoi)] or items
    if not covering:
        raise catalog.DataSourceUnavailable("No Sentinel-2 scenes found for this AOI.")

    chosen, used_threshold = None, None
    for threshold in cloud_thresholds:
        candidates = [it for it in covering if (it.properties.get("eo:cloud_cover") or 100) <= threshold]
        if candidates:
            chosen = max(candidates, key=catalog.item_date)
            used_threshold = threshold
            break
    if chosen is None:
        chosen = min(covering, key=lambda it: it.properties.get("eo:cloud_cover") or 100)
        log(
            f"Sentinel-2: no scene <= {cloud_thresholds[-1]}% cloud; using least cloudy "
            f"({chosen.properties.get('eo:cloud_cover'):.1f}%). Results may be affected by clouds.",
            "WARNING",
        )

    chosen_date = catalog.item_date(chosen)
    max_cloud = used_threshold if used_threshold is not None else cloud_thresholds[-1]
    older = [
        it
        for it in covering
        if (chosen_date - catalog.item_date(it)).days >= MIN_REFERENCE_GAP_DAYS
        and (it.properties.get("eo:cloud_cover") or 100) <= max_cloud
    ]
    reference = None
    if older:
        # Prefer ~1 year earlier (same season) to limit phenology differences.
        reference = min(older, key=lambda it: abs((chosen_date - catalog.item_date(it)).days - 365))

    return _scene_summary(chosen), (_scene_summary(reference) if reference else None), used_threshold


def select_composite_scenes(items: list, aoi: AOI, max_cloud: float, max_scenes: int,
                            window_days: int = 365) -> list[dict]:
    """Up to max_scenes clear scenes of the last year, one per date, spread evenly in time."""
    now = dt.datetime.now(dt.timezone.utc)
    by_date: dict[str, object] = {}
    for it in items:
        cloud = it.properties.get("eo:cloud_cover")
        if cloud is None or cloud > max_cloud or not catalog.covers_aoi(it, aoi):
            continue
        if (now - catalog.item_date(it)).days > window_days:
            continue
        day = catalog.item_date(it).strftime("%Y-%m-%d")
        if day not in by_date or cloud < by_date[day].properties["eo:cloud_cover"]:
            by_date[day] = it
    chosen = sorted(by_date.values(), key=catalog.item_date)
    if len(chosen) > max_scenes:
        idx = np.unique(np.round(np.linspace(0, len(chosen) - 1, max_scenes)).astype(int))
        chosen = [chosen[i] for i in idx]
    return [_scene_summary(it) for it in chosen]


def _reflectance(dn: np.ndarray, processing_baseline: str | None) -> np.ndarray:
    offset = 0.0
    try:
        if processing_baseline and float(processing_baseline) >= 4.0:
            offset = 1000.0  # BOA_ADD_OFFSET introduced with baseline 04.00 (Jan 2022)
    except ValueError:
        pass
    refl = (dn - offset) / 10000.0
    refl[dn == 0] = np.nan
    return refl.astype("float32")


def read_scene_bands(scene: dict, grid: Grid, cache_dir: Path, get_stac, collection: str) -> dict[str, np.ndarray]:
    item = None
    bands: dict[str, np.ndarray] = {}
    for name, asset_key in list(BANDS.items()) + [("scl", "SCL")]:
        cache_path = cache_dir / scene["id"] / f"{asset_key}.tif"
        if not cache_path.exists() and item is None:
            stac = get_stac()
            if stac is None:
                raise catalog.DataSourceUnavailable("Band not cached and catalog unavailable (offline).")
            item = catalog.fetch_item(stac, collection, scene["id"])
        href = item.assets[asset_key].href if item is not None else ""
        resampling = Resampling.nearest if asset_key == "SCL" else Resampling.bilinear
        bands[name] = cached_read_to_grid(href, grid, cache_path, resampling=resampling)

    scl = bands.pop("scl")
    invalid = np.isin(np.nan_to_num(scl, nan=0).astype("int16"), list(SCL_INVALID))
    for name in list(bands):
        refl = _reflectance(bands[name], scene.get("processing_baseline"))
        refl[invalid] = np.nan
        bands[name] = refl
    valid_pct = 100.0 * (1 - invalid.mean())
    log(f"Sentinel-2 {scene['id']}: {valid_pct:.1f}% of AOI pixels are cloud/shadow-free")
    scene["aoi_valid_pct"] = round(valid_pct, 1)
    return bands


def compute_products(bands: dict[str, np.ndarray], grid: Grid, out_dir: Path, suffix: str = "") -> dict[str, Path]:
    b = bands
    rgb = np.stack([to_uint8_stretch(b["red"]), to_uint8_stretch(b["green"]), to_uint8_stretch(b["blue"])])
    false_color = np.stack([to_uint8_stretch(b["nir"]), to_uint8_stretch(b["red"]), to_uint8_stretch(b["green"])])
    products = {
        "s2_rgb": (rgb, "uint8", ["Red B04", "Green B03", "Blue B02"]),
        "s2_false_color": (false_color, "uint8", ["NIR B08", "Red B04", "Green B03"]),
        "ndvi": (normalized_difference(b["nir"], b["red"]), "float32", None),
        "ndwi": (normalized_difference(b["green"], b["nir"]), "float32", None),
        "nbr": (normalized_difference(b["nir"], b["swir2"]), "float32", None),
        # Red-edge index: sensitive to subtle vegetation stress (crop marks).
        "ndre": (normalized_difference(b["nir08"], b["rededge"]), "float32", None),
        # Moisture index: soil/vegetation water content differences.
        "ndmi": (normalized_difference(b["nir08"], b["swir1"]), "float32", None),
    }
    paths = {}
    for name, (array, dtype, descriptions) in products.items():
        key = f"{name}{suffix}"
        paths[key] = save_geotiff(out_dir / f"{key}.tif", array, grid, dtype=dtype,
                                  nodata=0 if dtype == "uint8" else np.nan, band_descriptions=descriptions)
    return paths


def run(aoi: AOI, grid: Grid, config: dict, cache_root: Path, out_dir: Path, refresh: bool = False) -> dict:
    """Returns {'scene', 'reference', 'paths', 'ndvi', 'ndvi_change', 'cloud_threshold_used'}."""
    collection = config["stac_collections"]["sentinel2"]
    cache_dir = catalog.aoi_cache_dir(cache_root, aoi) / "sentinel2"
    selection_path = cache_dir / "selection.json"
    selection = None if refresh else catalog.load_selection(selection_path)
    stac = None

    if selection is not None and selection.get("composite_max_scenes") != config["sentinel2_composite_max_scenes"]:
        selection = None  # cache from an older version / different settings

    if selection is None:
        stac = catalog.open_catalog(config["stac_endpoint"])
        max_cloud = config["cloud_cover_fallback_pct"][-1]
        items = catalog.search_items(
            stac, collection, aoi, config["sentinel2_lookback_days"],
            cql2_filter={"op": "<=", "args": [{"property": "eo:cloud_cover"}, max_cloud]},
        )
        if not items:
            items = catalog.search_items(stac, collection, aoi, config["sentinel2_lookback_days"])
        log(f"Sentinel-2: {len(items)} scene(s) found in the last {config['sentinel2_lookback_days']} days")
        scene, reference, threshold = select_scenes(items, aoi, config["cloud_cover_fallback_pct"])
        composite = select_composite_scenes(items, aoi, config["sentinel2_composite_max_cloud_pct"],
                                            config["sentinel2_composite_max_scenes"])
        selection = {"scene": scene, "reference": reference, "cloud_threshold_used": threshold,
                     "composite": composite, "composite_max_scenes": config["sentinel2_composite_max_scenes"]}
        catalog.save_selection(selection_path, selection)
    else:
        log("Sentinel-2: using cached scene selection")

    scene = selection["scene"]
    log(f"Sentinel-2 selected: {scene['id']} ({scene['datetime'][:10]}, cloud {scene['cloud_cover_pct'] or 0:.1f}%)")
    if selection["cloud_threshold_used"] is None or (scene["cloud_cover_pct"] or 0) > 20:
        log("Sentinel-2: scene cloud cover is high; interpret optical results with care.", "WARNING")

    tried = stac is not None

    def get_stac():
        nonlocal stac, tried
        if not tried:
            tried = True
            try:
                stac = catalog.open_catalog(config["stac_endpoint"])
            except catalog.DataSourceUnavailable:
                stac = None
        return stac

    bands = read_scene_bands(scene, grid, cache_dir, get_stac, collection)
    paths = compute_products(bands, grid, out_dir)
    ndvi = normalized_difference(bands["nir"], bands["red"])

    result = {"scene": scene, "reference": selection["reference"], "paths": paths, "ndvi": ndvi,
              "ndvi_change": None, "cloud_threshold_used": selection["cloud_threshold_used"]}

    reference = selection["reference"]
    if reference:
        try:
            ref_bands = read_scene_bands(reference, grid, cache_dir, get_stac, collection)
            ndvi_ref = normalized_difference(ref_bands["nir"], ref_bands["red"])
            change = ndvi - ndvi_ref
            paths["ndvi_reference"] = save_geotiff(out_dir / "ndvi_reference.tif", ndvi_ref, grid)
            paths["ndvi_change"] = save_geotiff(out_dir / "ndvi_change.tif", change, grid)
            result["ndvi_change"] = change
            log(f"Sentinel-2 reference scene: {reference['id']} ({reference['datetime'][:10]})")
        except Exception as exc:  # noqa: BLE001
            log(f"Sentinel-2 reference scene unavailable ({exc}); skipping temporal NDVI comparison.", "WARNING")
    else:
        log("Sentinel-2: no suitable older scene for temporal comparison.", "WARNING")

    result.update(build_composite(selection.get("composite") or [], grid, cache_dir, get_stac, collection, out_dir))
    paths.update(result.pop("composite_paths", {}))

    catalog.save_selection(selection_path, selection)
    return result


def build_composite(scenes: list[dict], grid: Grid, cache_dir: Path, get_stac, collection: str,
                    out_dir: Path) -> dict:
    """Per-pixel median of several cloud-masked scenes (noise and single-day effects removed)."""
    if len(scenes) < MIN_COMPOSITE_SCENES:
        log(f"Sentinel-2: only {len(scenes)} clear scene(s) for a multi-date composite; skipped.", "WARNING")
        return {"composite_scenes": [], "ndvi_stack": None, "ndvi_median": None}

    log(f"Sentinel-2: building median composite from {len(scenes)} scenes "
        f"({scenes[0]['datetime'][:10]} .. {scenes[-1]['datetime'][:10]}); first run downloads each scene's AOI crop...")
    per_scene: list[dict[str, np.ndarray]] = []
    used: list[dict] = []
    for sc in scenes:
        try:
            per_scene.append(read_scene_bands(sc, grid, cache_dir, get_stac, collection))
            used.append(sc)
        except Exception as exc:  # noqa: BLE001
            log(f"Sentinel-2 composite: scene {sc['id']} skipped ({type(exc).__name__}: {exc})", "WARNING")
    if len(per_scene) < MIN_COMPOSITE_SCENES:
        log("Sentinel-2: too few readable scenes for a composite; skipped.", "WARNING")
        return {"composite_scenes": [], "ndvi_stack": None, "ndvi_median": None}

    median = {name: nan_reduce(np.stack([s[name] for s in per_scene]), how="median") for name in BANDS}
    paths = compute_products(median, grid, out_dir, suffix="_median")
    ndvi_stack = [normalized_difference(s["nir"], s["red"]) for s in per_scene]
    ndvi_std = nan_reduce(np.stack(ndvi_stack), how="std")
    paths["ndvi_temporal_std"] = save_geotiff(out_dir / "ndvi_temporal_std.tif", ndvi_std, grid)
    return {
        "composite_scenes": used,
        "composite_paths": paths,
        "ndvi_stack": ndvi_stack,
        "ndvi_median": normalized_difference(median["nir"], median["red"]),
    }
