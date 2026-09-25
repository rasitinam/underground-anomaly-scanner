"""
Shared raster helpers: common AOI grid, cached remote COG reads, GeoTIFF
writing and simple, explainable statistics (normalized difference, local
z-score, robust 0-1 normalization).

Every data source is warped onto ONE common grid (AOI UTM zone, fixed
resolution) so layers can be compared pixel by pixel.
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.vrt import WarpedVRT
from scipy.ndimage import uniform_filter

from aoi import AOI

GDAL_REMOTE_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.TIF,.tiff",
    "GDAL_HTTP_MAX_RETRY": "3",
    "GDAL_HTTP_RETRY_DELAY": "2",
}


@dataclass
class Grid:
    epsg: int
    transform: rasterio.Affine
    width: int
    height: int
    resolution_m: float


def make_grid(aoi: AOI, resolution_m: float) -> Grid:
    minx, miny, maxx, maxy = aoi.bounds_utm
    width = max(1, int(round((maxx - minx) / resolution_m)))
    height = max(1, int(round((maxy - miny) / resolution_m)))
    return Grid(
        epsg=aoi.utm_epsg,
        transform=from_origin(minx, maxy, resolution_m, resolution_m),
        width=width,
        height=height,
        resolution_m=resolution_m,
    )


def read_to_grid(
    href: str,
    grid: Grid,
    resampling: Resampling = Resampling.bilinear,
    band: int = 1,
) -> np.ndarray:
    """Read one band of a (remote) raster, warped/cropped to the grid. NaN = nodata."""
    with rasterio.Env(**GDAL_REMOTE_ENV):
        with rasterio.open(href) as src:
            src_nodata = src.nodata
            with WarpedVRT(
                src,
                crs=f"EPSG:{grid.epsg}",
                transform=grid.transform,
                width=grid.width,
                height=grid.height,
                resampling=resampling,
                src_nodata=src_nodata,
                nodata=np.nan,
                dtype="float32",
            ) as vrt:
                return vrt.read(band).astype("float32")


def cached_read_to_grid(
    href: str,
    grid: Grid,
    cache_path: Path,
    resampling: Resampling = Resampling.bilinear,
    band: int = 1,
) -> np.ndarray:
    """Read from local cache if present, otherwise fetch remotely and cache."""
    if cache_path.exists():
        with rasterio.open(cache_path) as src:
            return src.read(1).astype("float32")
    array = read_to_grid(href, grid, resampling=resampling, band=band)
    save_geotiff(cache_path, array, grid)
    return array


def save_geotiff(
    path: Path,
    array: np.ndarray,
    grid: Grid,
    dtype: str = "float32",
    nodata: float | None = np.nan,
    band_descriptions: list[str] | None = None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = array if array.ndim == 3 else array[np.newaxis, ...]
    profile = {
        "driver": "GTiff",
        "height": grid.height,
        "width": grid.width,
        "count": data.shape[0],
        "dtype": dtype,
        "crs": f"EPSG:{grid.epsg}",
        "transform": grid.transform,
        "compress": "deflate",
        "tiled": True,
        "blockxsize": 256,
        "blockysize": 256,
    }
    if grid.width < 256 or grid.height < 256:
        profile.pop("tiled")
        profile.pop("blockxsize")
        profile.pop("blockysize")
    if nodata is not None:
        profile["nodata"] = nodata
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data.astype(dtype))
        if band_descriptions:
            for i, desc in enumerate(band_descriptions, start=1):
                dst.set_band_description(i, desc)
    return path


def normalized_difference(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        result = (a - b) / (a + b)
    result[~np.isfinite(result)] = np.nan
    return result.astype("float32")


def local_zscore(array: np.ndarray, window_px: int) -> np.ndarray:
    """z = (pixel - local_mean) / local_std, NaN-aware, moving square window."""
    valid = np.isfinite(array)
    filled = np.where(valid, array, 0.0).astype("float64")
    weight = uniform_filter(valid.astype("float64"), size=window_px, mode="reflect")
    with np.errstate(divide="ignore", invalid="ignore"):
        mean = uniform_filter(filled, size=window_px, mode="reflect") / weight
        mean_sq = uniform_filter(filled * filled, size=window_px, mode="reflect") / weight
        std = np.sqrt(np.maximum(mean_sq - mean * mean, 0.0))
        z = (array - mean) / std
    z[~valid | ~np.isfinite(z) | (std < 1e-9)] = np.nan
    return z.astype("float32")


def nan_reduce(stack: np.ndarray, how: str = "mean") -> np.ndarray:
    """np.nanmean / np.nanstd along axis 0 without 'empty slice' warnings (all-NaN -> NaN)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        result = np.nanmean(stack, axis=0) if how == "mean" else np.nanstd(stack, axis=0)
    return result.astype("float32")


def robust_normalize(array: np.ndarray, low_pct: float = 2, high_pct: float = 98) -> np.ndarray:
    """Scale to 0..1 using percentiles (outlier-robust). NaN stays NaN."""
    valid = array[np.isfinite(array)]
    if valid.size == 0:
        return np.full_like(array, np.nan, dtype="float32")
    lo, hi = np.percentile(valid, [low_pct, high_pct])
    if hi - lo < 1e-12:
        return np.where(np.isfinite(array), 0.0, np.nan).astype("float32")
    return np.clip((array - lo) / (hi - lo), 0.0, 1.0).astype("float32")


def to_uint8_stretch(array: np.ndarray) -> np.ndarray:
    scaled = robust_normalize(array)
    return np.where(np.isfinite(scaled), np.round(scaled * 254) + 1, 0).astype("uint8")
