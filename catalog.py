"""
STAC catalog access (Microsoft Planetary Computer: free, no account, no API key).

The imagery itself is the official ESA Copernicus / USGS data, mirrored as
Cloud-Optimized GeoTIFFs. Asset URLs are signed anonymously by the
`planetary_computer` package (short-lived SAS token, no registration).

Scene selections are cached as JSON so a re-run for the same AOI works
offline and does not re-query the catalog.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from aoi import AOI


class DataSourceUnavailable(RuntimeError):
    pass


def open_catalog(endpoint: str):
    try:
        import planetary_computer
        import pystac_client
    except ImportError as exc:
        raise DataSourceUnavailable(
            f"Missing library ({exc.name}). Run: python -m pip install pystac-client planetary-computer"
        ) from exc
    try:
        return pystac_client.Client.open(endpoint, modifier=planetary_computer.sign_inplace)
    except Exception as exc:  # noqa: BLE001
        raise DataSourceUnavailable(
            f"Cannot reach STAC catalog {endpoint} ({type(exc).__name__}). "
            "Check your internet connection / firewall."
        ) from exc


def search_items(
    catalog,
    collection: str,
    aoi: AOI,
    lookback_days: int,
    query: dict[str, Any] | None = None,
    max_items: int = 200,
) -> list:
    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(days=lookback_days)
    try:
        search = catalog.search(
            collections=[collection],
            bbox=list(aoi.bounds_wgs84),
            datetime=f"{start:%Y-%m-%dT%H:%M:%SZ}/{end:%Y-%m-%dT%H:%M:%SZ}",
            query=query or {},
            max_items=max_items,
        )
        return list(search.items())
    except Exception as exc:  # noqa: BLE001
        raise DataSourceUnavailable(f"STAC search failed for {collection}: {type(exc).__name__}: {exc}") from exc


def fetch_item(catalog, collection: str, item_id: str):
    """Re-fetch one item by id (fresh signed URLs)."""
    items = list(catalog.search(collections=[collection], ids=[item_id]).items())
    if not items:
        raise DataSourceUnavailable(f"Item {item_id} not found in {collection}")
    return items[0]


def covers_aoi(item, aoi: AOI) -> bool:
    from shapely.geometry import box, shape

    return shape(item.geometry).contains(box(*aoi.bounds_wgs84))


def item_date(item) -> dt.datetime:
    return item.datetime or dt.datetime.fromisoformat(item.properties["start_datetime"].replace("Z", "+00:00"))


def load_selection(path: Path) -> dict | None:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def save_selection(path: Path, selection: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(selection, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def aoi_cache_dir(cache_root: Path, aoi: AOI) -> Path:
    return cache_root / f"lat{aoi.lat:.5f}_lon{aoi.lon:.5f}_r{int(aoi.radius_m)}"
