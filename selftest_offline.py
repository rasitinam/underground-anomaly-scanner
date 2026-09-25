"""
Offline self-test (no internet needed): seeds the cache with SYNTHETIC rasters
containing one planted surface feature, runs the real pipeline from cache and
checks that the feature is reported as a multi-source anomaly.

    python selftest_offline.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import catalog  # noqa: E402
import main  # noqa: E402
from aoi import build_aoi  # noqa: E402
from raster_utils import make_grid, save_geotiff  # noqa: E402

LAT, LON, RADIUS = 40.735, 31.605, 250


def seed_cache(cache_root: Path, config: dict) -> tuple[int, int, int, int]:
    rng = np.random.default_rng(42)
    aoi = build_aoi(LAT, LON, RADIUS)
    grid = make_grid(aoi, config["target_resolution_m"])
    h, w = grid.height, grid.width
    base = catalog.aoi_cache_dir(cache_root, aoi)
    r0, r1, c0, c1 = h // 2 - 3, h // 2 + 3, w // 2 - 4, w // 2 + 4  # ~60 x 80 m feature
    feature = np.zeros((h, w), bool)
    feature[r0:r1, c0:c1] = True

    s2_scenes = {"scene": {"id": "S2_SYNTH_NEW", "datetime": "2026-07-01T09:00:00", "cloud_cover_pct": 3.2,
                           "platform": "synthetic", "processing_baseline": "05.10", "collection": "sentinel-2-l2a"},
                 "reference": {"id": "S2_SYNTH_OLD", "datetime": "2025-07-03T09:00:00", "cloud_cover_pct": 5.0,
                               "platform": "synthetic", "processing_baseline": "05.10", "collection": "sentinel-2-l2a"},
                 "cloud_threshold_used": 10,
                 "composite_max_scenes": config["sentinel2_composite_max_scenes"],
                 "composite": [{"id": f"S2_SYNTH_C{i}", "datetime": f"2026-0{i + 3}-15T09:00:00", "cloud_cover_pct": 4.0,
                                "platform": "synthetic", "processing_baseline": "05.10",
                                "collection": "sentinel-2-l2a"} for i in range(5)]}
    for sc in [s2_scenes["scene"], s2_scenes["reference"], *s2_scenes["composite"]]:
        d = base / "sentinel2" / sc["id"]
        refl = {"B02": 0.05, "B03": 0.08, "B04": 0.07, "B05": 0.12, "B08": 0.35, "B8A": 0.36,
                "B11": 0.20, "B12": 0.12}
        for band, v in refl.items():
            arr = v + rng.normal(0, 0.01, (h, w))
            if band in ("B08", "B8A"):
                arr[feature] -= 0.15  # stressed vegetation over the feature
            save_geotiff(d / f"{band}.tif", ((arr * 10000) + 1000).astype("float32"), grid)
        scl = np.full((h, w), 4, "float32")
        if sc["id"] == "S2_SYNTH_C1":
            scl[:10, :] = 9  # a cloud: must be masked out of the median
        save_geotiff(d / "SCL.tif", scl, grid)
    catalog.save_selection(base / "sentinel2" / "selection.json", s2_scenes)

    s1_scenes = []
    for i, date in enumerate(["2025-09-01", "2026-01-05", "2026-05-10", "2026-09-01"]):
        sid = f"S1_SYNTH_{i}"
        s1_scenes.append({"id": sid, "collection": "sentinel-1-grd", "datetime": f"{date}T04:00:00",
                          "platform": "synthetic", "orbit_state": "ascending", "relative_orbit": 58,
                          "instrument_mode": "IW", "calibration": "synthetic test data"})
        for pol, level in (("vv", -11.0), ("vh", -18.0)):
            arr = level + rng.normal(0, 0.7, (h, w))
            arr[feature] += 4.0  # persistent brighter backscatter
            save_geotiff(base / "sentinel1" / sid / f"{pol}_db.tif", arr.astype("float32"), grid)
    catalog.save_selection(base / "sentinel1" / "selection.json",
                           {"scenes": s1_scenes, "max_scenes": config["sentinel1_max_scenes"]})

    y, x = np.mgrid[0:h, 0:w]
    dem = 750 + 0.05 * x * grid.resolution_m + rng.normal(0, 0.05, (h, w))
    dem[feature] += 1.5  # low mound
    save_geotiff(base / "dem" / "DEM_SYNTH.tif", dem.astype("float32"), grid)
    catalog.save_selection(base / "dem" / "selection.json", {"tiles": [{"id": "DEM_SYNTH", "collection": "cop-dem-glo-30"}]})
    return r0, r1, c0, c1


def run() -> int:
    config = main.load_config()
    tmp = Path(tempfile.mkdtemp(prefix="uas_selftest_"))
    config["cache_dir"] = str(tmp / "cache")
    config["output_dir"] = str(tmp / "output")
    config["connectivity_timeout_s"] = 2
    seed_cache(Path(config["cache_dir"]), config)

    code = main.run_analysis(LAT, LON, RADIUS, config)
    out = Path(config["output_dir"])
    expected = ["s2_rgb.tif", "s2_false_color.tif", "ndvi.tif", "ndwi.tif", "nbr.tif", "ndre.tif", "ndmi.tif",
                "ndvi_change.tif", "s2_rgb_median.tif", "ndvi_median.tif", "ndre_median.tif", "ndmi_median.tif",
                "ndvi_temporal_std.tif", "s1_vv_mean_db.tif", "s1_vh_mean_db.tif",
                "s1_vv_db.tif", "s1_vh_db.tif", "s1_vv_change_db.tif", "dem.tif", "slope.tif", "aspect.tif",
                "hillshade.tif", "lrm.tif", "anomaly.tif", "anomaly_sar.tif", "anomaly_vegetation.tif",
                "anomaly_terrain.tif", "anomaly_temporal.tif", "anomaly_areas.geojson", "aoi.geojson",
                "report.html", "run.log", "metadata.json", "qgis_layers.json"]
    missing = [f for f in expected if not (out / f).exists()]
    polygons = json.loads((out / "anomaly_areas.geojson").read_text())["features"]
    html = (out / "report.html").read_text(encoding="utf-8")

    checks = {
        "exit code 0": code == 0,
        "all outputs written": not missing,
        "planted feature found as multi-source anomaly": len(polygons) >= 1,
        "top area is near AOI centre": bool(polygons) and abs(polygons[0]["properties"]["centroid_lat"] - LAT) < 0.001
        and abs(polygons[0]["properties"]["centroid_lon"] - LON) < 0.001,
        "few false multi-source areas": len(polygons) <= 3,
        "report has EN+TR disclaimer": "does not directly image underground objects" in html
        and "doğrudan görüntülemez" in html,
        "no certainty language in report": not any(s in html.lower() for s in
                                                   ("underground object detected", "tunnel detected", "tünel var")),
    }
    print("\nSELF-TEST RESULTS")
    for name, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if missing:
        print(f"  missing: {missing}")
    for p in polygons:
        print(f"  area #{p['properties']['id']}: {p['properties']}")
    print(f"  outputs in: {out}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(run())
