"""
Phase 2-3: coordinate input and area-of-interest (AOI) construction.

Coordinates are always WGS84 (EPSG:4326). For metric operations (buffering,
pixel sizing, slope calculation) we reproject to the UTM zone that contains
the point, auto-detected from longitude/latitude.
"""
from __future__ import annotations

from dataclasses import dataclass

from pyproj import Transformer


@dataclass
class AOI:
    lat: float
    lon: float
    radius_m: float
    utm_epsg: int
    bounds_wgs84: tuple[float, float, float, float]  # minx, miny, maxx, maxy
    bounds_utm: tuple[float, float, float, float]
    center_utm: tuple[float, float]


def utm_epsg_for(lat: float, lon: float) -> int:
    """WGS84 -> UTM EPSG code (326xx north / 327xx south) for a given point."""
    zone = int((lon + 180) / 6) % 60 + 1
    return (32600 if lat >= 0 else 32700) + zone


def validate_coordinates(lat: float, lon: float) -> None:
    if not (-90.0 <= lat <= 90.0):
        raise ValueError(f"Latitude must be between -90 and 90, got {lat}")
    if not (-180.0 <= lon <= 180.0):
        raise ValueError(f"Longitude must be between -180 and 180, got {lon}")


def build_aoi(lat: float, lon: float, radius_m: float) -> AOI:
    validate_coordinates(lat, lon)
    if radius_m <= 0:
        raise ValueError(f"Radius must be positive, got {radius_m}")

    utm_epsg = utm_epsg_for(lat, lon)
    to_utm = Transformer.from_crs("EPSG:4326", f"EPSG:{utm_epsg}", always_xy=True)
    to_wgs84 = Transformer.from_crs(f"EPSG:{utm_epsg}", "EPSG:4326", always_xy=True)

    cx, cy = to_utm.transform(lon, lat)
    minx, miny, maxx, maxy = cx - radius_m, cy - radius_m, cx + radius_m, cy + radius_m

    corners_lon, corners_lat = [], []
    for x, y in [(minx, miny), (minx, maxy), (maxx, miny), (maxx, maxy)]:
        lon_c, lat_c = to_wgs84.transform(x, y)
        corners_lon.append(lon_c)
        corners_lat.append(lat_c)

    bounds_wgs84 = (min(corners_lon), min(corners_lat), max(corners_lon), max(corners_lat))

    return AOI(
        lat=lat,
        lon=lon,
        radius_m=radius_m,
        utm_epsg=utm_epsg,
        bounds_wgs84=bounds_wgs84,
        bounds_utm=(minx, miny, maxx, maxy),
        center_utm=(cx, cy),
    )


def bounds_in_crs(aoi: AOI, dst_epsg: int) -> tuple[float, float, float, float]:
    """Reproject the AOI's WGS84 bounding box envelope into another CRS."""
    if dst_epsg == 4326:
        return aoi.bounds_wgs84
    if dst_epsg == aoi.utm_epsg:
        return aoi.bounds_utm

    minx, miny, maxx, maxy = aoi.bounds_wgs84
    transformer = Transformer.from_crs("EPSG:4326", f"EPSG:{dst_epsg}", always_xy=True)
    xs, ys = [], []
    for lon, lat in [(minx, miny), (minx, maxy), (maxx, miny), (maxx, maxy)]:
        x, y = transformer.transform(lon, lat)
        xs.append(x)
        ys.append(y)
    return (min(xs), min(ys), max(xs), max(ys))


def estimate_download_mb(radius_m: float, resolution_m: float = 10.0, n_bands: int = 8) -> float:
    """Rough size estimate for the cropped AOI rasters (not full satellite tiles)."""
    side_px = (2 * radius_m) / resolution_m
    pixels = side_px * side_px
    bytes_per_px = 2  # uint16
    total_bytes = pixels * bytes_per_px * n_bands
    return total_bytes / (1024 * 1024)


def large_area_warning(radius_m: float, threshold_m: float) -> str | None:
    if radius_m > threshold_m:
        est_mb = estimate_download_mb(radius_m)
        return (
            f"UYARI: radius={int(radius_m)}m, {threshold_m:.0f}m eşiğinin üzerinde. "
            f"İndirme/işleme daha uzun sürebilir. Kırpılmış Sentinel-2 verisi ~{est_mb:.1f} MB; "
            f"COG blok okuması nedeniyle ağ trafiği bunun birkaç katı olabilir. "
            f"WARNING: large area may take longer to download/process."
        )
    return None
