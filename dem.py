"""
Phase 6: Copernicus DEM GLO-30 -> elevation, slope, aspect, hillshade,
local relief model (LRM).

Terrain derivatives use Horn's (1981) 3x3 method, the same algorithm as
`gdaldem`, implemented with numpy so no extra GDAL command-line tools are
needed. The native DEM resolution is ~30 m; it is resampled (cubic) onto
the common 10 m grid, which does NOT add real detail.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from rasterio.enums import Resampling
from scipy.ndimage import gaussian_filter

import catalog
from aoi import AOI
from logger import log
from raster_utils import Grid, cached_read_to_grid, save_geotiff

NATIVE_RESOLUTION_M = 30


def horn_gradients(dem: np.ndarray, cell_size: float) -> tuple[np.ndarray, np.ndarray]:
    p = np.pad(dem, 1, mode="edge")
    a, b, c = p[:-2, :-2], p[:-2, 1:-1], p[:-2, 2:]
    d, f = p[1:-1, :-2], p[1:-1, 2:]
    g, h, i = p[2:, :-2], p[2:, 1:-1], p[2:, 2:]
    dz_dx = ((c + 2 * f + i) - (a + 2 * d + g)) / (8 * cell_size)
    dz_dy = ((g + 2 * h + i) - (a + 2 * b + c)) / (8 * cell_size)  # positive = downhill to the north
    return dz_dx, dz_dy


def slope_degrees(dem: np.ndarray, cell_size: float) -> np.ndarray:
    dz_dx, dz_dy = horn_gradients(dem, cell_size)
    return np.degrees(np.arctan(np.hypot(dz_dx, dz_dy))).astype("float32")


def _compass_aspect_rad(dz_dx: np.ndarray, dz_dy: np.ndarray) -> np.ndarray:
    # Downslope direction as (east, north) = (-dz_dx, dz_dy); compass azimuth = atan2(east, north).
    return np.mod(np.arctan2(-dz_dx, dz_dy), 2 * np.pi)


def aspect_degrees(dem: np.ndarray, cell_size: float) -> np.ndarray:
    """Compass direction the slope faces (0 = north, 90 = east), NaN on flat cells."""
    dz_dx, dz_dy = horn_gradients(dem, cell_size)
    aspect = np.degrees(_compass_aspect_rad(dz_dx, dz_dy))
    aspect[(np.abs(dz_dx) < 1e-9) & (np.abs(dz_dy) < 1e-9)] = np.nan
    return aspect.astype("float32")


def hillshade(dem: np.ndarray, cell_size: float, azimuth: float = 315.0, altitude: float = 45.0) -> np.ndarray:
    dz_dx, dz_dy = horn_gradients(dem, cell_size)
    slope = np.arctan(np.hypot(dz_dx, dz_dy))
    aspect = _compass_aspect_rad(dz_dx, dz_dy)
    zenith = np.radians(90.0 - altitude)
    shade = np.cos(zenith) * np.cos(slope) + np.sin(zenith) * np.sin(slope) * np.cos(np.radians(azimuth) - aspect)
    return np.clip(shade * 255.0, 0, 255).astype("float32")


def local_relief_model(dem: np.ndarray, sigma_px: float) -> np.ndarray:
    """DEM minus a smoothed DEM: highlights small mounds/depressions relative to the surroundings."""
    filled = np.where(np.isfinite(dem), dem, np.nanmean(dem))
    return (dem - gaussian_filter(filled, sigma=sigma_px)).astype("float32")


def _read_mosaic(items_meta: list[dict], grid: Grid, cache_dir: Path, stac, collection: str) -> np.ndarray:
    mosaic = np.full((grid.height, grid.width), np.nan, dtype="float32")
    for meta in items_meta:
        cache_path = cache_dir / f"{meta['id']}.tif"
        href = ""
        if not cache_path.exists():
            if stac is None:
                raise catalog.DataSourceUnavailable("DEM not cached and catalog unavailable (offline).")
            href = catalog.fetch_item(stac, collection, meta["id"]).assets["data"].href
        tile = cached_read_to_grid(href, grid, cache_path, resampling=Resampling.cubic)
        mosaic = np.where(np.isfinite(mosaic), mosaic, tile)
    return mosaic


def run(aoi: AOI, grid: Grid, config: dict, cache_root: Path, out_dir: Path, refresh: bool = False) -> dict:
    collection = config["stac_collections"]["dem"]
    cache_dir = catalog.aoi_cache_dir(cache_root, aoi) / "dem"
    selection_path = cache_dir / "selection.json"
    selection = None if refresh else catalog.load_selection(selection_path)
    stac = None

    if selection is None:
        stac = catalog.open_catalog(config["stac_endpoint"])
        # The DEM is a static product: search without a date window.
        items = list(stac.search(collections=[collection], bbox=list(aoi.bounds_wgs84)).items())
        if not items:
            raise catalog.DataSourceUnavailable("No Copernicus DEM tile intersects this AOI.")
        selection = {"tiles": [{"id": it.id, "collection": it.collection_id} for it in items]}
        catalog.save_selection(selection_path, selection)
    else:
        log("DEM: using cached tile selection")

    if stac is None and not all((cache_dir / f"{t['id']}.tif").exists() for t in selection["tiles"]):
        stac = catalog.open_catalog(config["stac_endpoint"])

    dem = _read_mosaic(selection["tiles"], grid, cache_dir, stac, collection)
    if not np.isfinite(dem).any():
        raise catalog.DataSourceUnavailable("DEM read returned no valid pixels for this AOI.")

    cell = grid.resolution_m
    lrm_sigma_px = max(2.0, 100.0 / cell)  # ~100 m smoothing scale
    products = {
        "dem": dem,
        "slope": slope_degrees(dem, cell),
        "aspect": aspect_degrees(dem, cell),
        "hillshade": hillshade(dem, cell),
        "lrm": local_relief_model(dem, lrm_sigma_px),
    }
    paths = {name: save_geotiff(out_dir / f"{name}.tif", arr, grid) for name, arr in products.items()}
    log(f"DEM: elevation {np.nanmin(dem):.1f} - {np.nanmax(dem):.1f} m, tiles: "
        + ", ".join(t["id"] for t in selection["tiles"]))
    return {"tiles": selection["tiles"], "paths": paths, **products}
