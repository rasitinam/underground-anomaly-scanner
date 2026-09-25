"""
Phase 4-5: Sentinel-2 L2A scene selection, band reading and spectral indices.

Outputs (all on the common AOI grid):
  s2_rgb.tif, s2_false_color.tif, ndvi.tif, ndwi.tif, nbr.tif
  ndvi_reference.tif + ndvi_change.tif (older scene, for temporal comparison)
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from rasterio.enums import Resampling

import catalog
from aoi import AOI
from logger import log
from raster_utils import Grid, cached_read_to_grid, normalized_difference, save_geotiff, to_uint8_stretch

BANDS = {"blue": "B02", "green": "B03", "red": "B04", "nir": "B08", "swir1": "B11", "swir2": "B12"}
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


def read_scene_bands(scene: dict, grid: Grid, cache_dir: Path, stac, collection: str) -> dict[str, np.ndarray]:
    item = None
    bands: dict[str, np.ndarray] = {}
    for name, asset_key in list(BANDS.items()) + [("scl", "SCL")]:
        cache_path = cache_dir / scene["id"] / f"{asset_key}.tif"
        if not cache_path.exists() and item is None:
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


def compute_products(bands: dict[str, np.ndarray], grid: Grid, out_dir: Path) -> dict[str, Path]:
    b = bands
    rgb = np.stack([to_uint8_stretch(b["red"]), to_uint8_stretch(b["green"]), to_uint8_stretch(b["blue"])])
    false_color = np.stack([to_uint8_stretch(b["nir"]), to_uint8_stretch(b["red"]), to_uint8_stretch(b["green"])])
    return {
        "s2_rgb": save_geotiff(out_dir / "s2_rgb.tif", rgb, grid, dtype="uint8", nodata=0,
                               band_descriptions=["Red B04", "Green B03", "Blue B02"]),
        "s2_false_color": save_geotiff(out_dir / "s2_false_color.tif", false_color, grid, dtype="uint8", nodata=0,
                                       band_descriptions=["NIR B08", "Red B04", "Green B03"]),
        "ndvi": save_geotiff(out_dir / "ndvi.tif", normalized_difference(b["nir"], b["red"]), grid),
        "ndwi": save_geotiff(out_dir / "ndwi.tif", normalized_difference(b["green"], b["nir"]), grid),
        "nbr": save_geotiff(out_dir / "nbr.tif", normalized_difference(b["nir"], b["swir2"]), grid),
    }


def run(aoi: AOI, grid: Grid, config: dict, cache_root: Path, out_dir: Path, refresh: bool = False) -> dict:
    """Returns {'scene', 'reference', 'paths', 'ndvi', 'ndvi_change', 'cloud_threshold_used'}."""
    collection = config["stac_collections"]["sentinel2"]
    cache_dir = catalog.aoi_cache_dir(cache_root, aoi) / "sentinel2"
    selection_path = cache_dir / "selection.json"
    selection = None if refresh else catalog.load_selection(selection_path)
    stac = None

    if selection is None:
        stac = catalog.open_catalog(config["stac_endpoint"])
        items = catalog.search_items(
            stac, collection, aoi, config["sentinel2_lookback_days"],
            query={"eo:cloud_cover": {"lt": 100}},
        )
        log(f"Sentinel-2: {len(items)} scene(s) found in the last {config['sentinel2_lookback_days']} days")
        scene, reference, threshold = select_scenes(items, aoi, config["cloud_cover_fallback_pct"])
        selection = {"scene": scene, "reference": reference, "cloud_threshold_used": threshold}
        catalog.save_selection(selection_path, selection)
    else:
        log("Sentinel-2: using cached scene selection")

    scene = selection["scene"]
    log(f"Sentinel-2 selected: {scene['id']} ({scene['datetime'][:10]}, cloud {scene['cloud_cover_pct']}%)")
    if selection["cloud_threshold_used"] is None or (scene["cloud_cover_pct"] or 0) > 20:
        log("Sentinel-2: scene cloud cover is high; interpret optical results with care.", "WARNING")

    def get_stac():
        nonlocal stac
        if stac is None:
            try:
                stac = catalog.open_catalog(config["stac_endpoint"])
            except catalog.DataSourceUnavailable:
                return None
        return stac

    bands = read_scene_bands(scene, grid, cache_dir, get_stac(), collection)
    paths = compute_products(bands, grid, out_dir)
    ndvi = normalized_difference(bands["nir"], bands["red"])

    result = {"scene": scene, "reference": selection["reference"], "paths": paths, "ndvi": ndvi,
              "ndvi_change": None, "cloud_threshold_used": selection["cloud_threshold_used"]}

    reference = selection["reference"]
    if reference:
        try:
            ref_bands = read_scene_bands(reference, grid, cache_dir, get_stac(), collection)
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

    catalog.save_selection(selection_path, selection)
    return result
