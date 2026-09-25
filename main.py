"""
UNDERGROUND ANOMALY SCANNER - surface anomaly research from free satellite data.

Satellite data does NOT image the subsurface. This tool looks for indirect,
surface-level indicators (vegetation, SAR backscatter, micro-topography and
their persistence over time) and shows them in QGIS.

Usage:
    python main.py                                   (interactive menu)
    python main.py --lat 40.735 --lon 31.605 --radius 250
    python main.py --check-env
    python main.py --build-qgis                      (rebuild project from last run)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import env_check  # noqa: E402
from logger import init_logger, log, step, warnings_seen  # noqa: E402

TOTAL_STEPS = 7
GENERATED_SUFFIXES = {".tif", ".geojson", ".qgz", ".html", ".json"}


def load_config() -> dict:
    return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def ensure_dependencies() -> bool:
    missing = [m for m, ok in env_check.check_current_python_libraries().items() if not ok]
    if missing:
        names = " ".join(env_check.PIP_NAMES.get(m, m) for m in missing)
        print(f"Missing Python libraries: {', '.join(missing)}")
        print(f"Install only these (nothing else is reinstalled):\n    python -m pip install {names}")
        print("Run 'python main.py --check-env' for a full environment report.")
        return False
    return True


def clean_output(out_dir: Path) -> None:
    """Remove products of a previous run so stale layers never leak into the new project."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for f in out_dir.iterdir():
        if f.is_file() and f.suffix.lower() in GENERATED_SUFFIXES:
            f.unlink()


def write_aoi_geojson(aoi, path: Path) -> Path:
    from pyproj import Transformer

    to_wgs84 = Transformer.from_crs(f"EPSG:{aoi.utm_epsg}", "EPSG:4326", always_xy=True)
    minx, miny, maxx, maxy = aoi.bounds_utm
    ring = [to_wgs84.transform(x, y) for x, y in [(minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy), (minx, miny)]]
    fc = {"type": "FeatureCollection", "features": [{
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [[list(p) for p in ring]]},
        "properties": {"lat": aoi.lat, "lon": aoi.lon, "radius_m": aoi.radius_m, "utm_epsg": aoi.utm_epsg},
    }]}
    path.write_text(json.dumps(fc), encoding="utf-8")
    return path


def layer_metadata(aoi, grid, s2, s1, dem) -> dict:
    crs = f"EPSG:{grid.epsg}"
    res = f"{grid.resolution_m} m grid"
    layers = {}
    if s2:
        sc = s2["scene"]
        for name in s2["paths"]:
            layers[name] = {"source": "ESA Copernicus Sentinel-2 L2A via Microsoft Planetary Computer",
                            "product": sc["id"], "date": sc["datetime"][:10],
                            "resolution": f"native 10-20 m, {res}", "crs": crs}
        if s2.get("reference") and "ndvi_change" in s2["paths"]:
            for name in ("ndvi_reference", "ndvi_change"):
                layers[name]["product"] = f"{sc['id']} vs {s2['reference']['id']}"
        comp = s2.get("composite_scenes") or []
        for name in s2["paths"]:
            if comp and (name.endswith("_median") or name == "ndvi_temporal_std"):
                layers[name]["product"] = f"per-pixel median/std of {len(comp)} scenes: " + ", ".join(c["id"] for c in comp)
                layers[name]["date"] = f"{comp[0]['datetime'][:10]} .. {comp[-1]['datetime'][:10]}"
    if s1:
        ids = [s["id"] for s in s1["scenes"]]
        for name in s1["paths"]:
            layers[name] = {"source": f"ESA Copernicus Sentinel-1 ({s1['scenes'][-1]['collection']}) via Microsoft Planetary Computer",
                            "product": ids[-1] if name in ("s1_vv", "s1_vh", "s1_vv_vh_ratio") else ", ".join(ids),
                            "date": s1["scenes"][-1]["datetime"][:10] if name in ("s1_vv", "s1_vh", "s1_vv_vh_ratio")
                            else f"{s1['scenes'][0]['datetime'][:10]} .. {s1['scenes'][-1]['datetime'][:10]}",
                            "processing": s1["scenes"][-1].get("calibration"),
                            "resolution": f"native ~10-20 m, {res}", "crs": crs}
    if dem:
        for name in dem["paths"]:
            layers[name] = {"source": "Copernicus DEM GLO-30 via Microsoft Planetary Computer",
                            "product": ", ".join(t["id"] for t in dem["tiles"]), "date": "static (2011-2015 acquisitions)",
                            "resolution": f"native ~30 m, {res}", "crs": crs}
    return {"coordinate_wgs84": [aoi.lat, aoi.lon], "radius_m": aoi.radius_m, "aoi_bounds_wgs84": aoi.bounds_wgs84,
            "working_crs": crs, "layers": {k: {**v} for k, v in layers.items()}}


def run_analysis(lat: float, lon: float, radius: float, config: dict, refresh: bool = False,
                 s1_safe: Path | None = None, open_qgis: bool = False) -> int:
    import numpy as np  # noqa: F401  - fail early with a clear message if deps are broken

    import anomaly
    import dem as dem_mod
    import qgis_project
    import report
    import sentinel1
    import sentinel2
    from aoi import build_aoi, large_area_warning
    from raster_utils import make_grid

    out_dir = ROOT / config["output_dir"]
    cache_root = ROOT / config["cache_dir"]
    clean_output(out_dir)
    init_logger(out_dir)

    aoi = build_aoi(lat, lon, radius)
    grid = make_grid(aoi, config["target_resolution_m"])
    log(f"AOI: lat={lat}, lon={lon}, radius={radius:.0f} m, UTM EPSG:{aoi.utm_epsg}, grid {grid.width}x{grid.height} px")
    warning = large_area_warning(radius, config["large_radius_warning_threshold_m"])
    if warning:
        log(warning, "WARNING")
    aoi_path = write_aoi_geojson(aoi, out_dir / "aoi.geojson")

    online, detail = env_check.check_internet(config["connectivity_check_url"], config["connectivity_timeout_s"])
    if not online:
        log(f"No connection to the data catalog ({detail}). Only previously cached data can be used. "
            "Check your internet connection / proxy / firewall.", "WARNING")

    qgis_installs = env_check.find_qgis_installations()
    qgis = next((q for q in qgis_installs if q.qgis_python), qgis_installs[0] if qgis_installs else None)
    snap = env_check.find_snap_installation()

    s2 = s1 = terrain = an = None

    step(1, TOTAL_STEPS, "Sentinel-2: searching, downloading (AOI crop only) and computing RGB / False Color / NDVI / NDWI / NBR...")
    try:
        s2 = sentinel2.run(aoi, grid, config, cache_root, out_dir, refresh)
    except Exception as exc:  # noqa: BLE001
        log(f"Sentinel-2 unavailable ({type(exc).__name__}: {exc}); continuing with Sentinel-1 + DEM.", "WARNING")

    step(2, TOTAL_STEPS, "Sentinel-1: searching, downloading and processing SAR (VV / VH, multi-date)...")
    try:
        s1 = sentinel1.run(aoi, grid, config, cache_root, out_dir, refresh,
                           snap_gpt=snap.gpt_executable if snap else None, s1_safe=s1_safe)
    except Exception as exc:  # noqa: BLE001
        log(f"Sentinel-1 unavailable ({type(exc).__name__}: {exc}); continuing without SAR.", "WARNING")

    step(3, TOTAL_STEPS, "Copernicus DEM: elevation, slope, aspect, hillshade, local relief...")
    try:
        terrain = dem_mod.run(aoi, grid, config, cache_root, out_dir, refresh)
    except Exception as exc:  # noqa: BLE001
        log(f"DEM unavailable ({type(exc).__name__}: {exc}); continuing without terrain.", "WARNING")

    step(4, TOTAL_STEPS, "Detecting surface anomalies (local z-score, multi-source consistency)...")
    try:
        an = anomaly.run(s2, s1, terrain, grid, config, out_dir)
    except Exception as exc:  # noqa: BLE001
        log(f"Anomaly analysis failed: {type(exc).__name__}: {exc}", "ERROR")

    metadata = layer_metadata(aoi, grid, s2, s1, terrain)
    if an:
        metadata["anomaly"] = an["stats"]
    (out_dir / "metadata.json").write_text(json.dumps(metadata, indent=2, default=str), encoding="utf-8")

    step(5, TOTAL_STEPS, "Creating QGIS project...")
    basemaps = [
        {"name": config.get("satellite_basemap_name", "Satellite basemap"),
         "url": config.get("satellite_basemap_xyz_url", ""), "visible": True},
        {"name": "OpenStreetMap", "url": config["osm_xyz_url"], "visible": not config.get("satellite_basemap_xyz_url")},
    ]
    spec = qgis_project.build_spec(out_dir, aoi_path, aoi.utm_epsg, aoi.bounds_utm, basemaps,
                                   f"Surface anomaly research {lat:.5f}, {lon:.5f} r={radius:.0f} m")
    project = qgis_project.write_project(spec, out_dir / "qgis_layers.json", qgis.qgis_python if qgis else None)

    step(6, TOTAL_STEPS, "Writing HTML report...")
    report_path = report.build_report(
        {"aoi": aoi, "grid": grid, "s2": s2, "s1": s1, "dem": terrain, "anomaly": an,
         "warnings": list(warnings_seen)},
        out_dir / "report.html",
    )

    step(7, TOTAL_STEPS, "Done.")
    available = [n for n, v in (("Sentinel-2", s2), ("Sentinel-1", s1), ("DEM", terrain)) if v]
    print()
    print("Analysis complete." if available else "Analysis finished WITHOUT any satellite data (see run.log).")
    print(f"Data sources used: {', '.join(available) or 'none'}")
    print(f"QGIS project:\n    {project if project else 'NOT created (QGIS not found or build failed, see run.log)'}")
    if an:
        print(f"Multi-source anomaly map:\n    {an['paths']['anomaly']}")
        print(f"Multi-source anomaly areas: {an['stats']['n_polygons']} (surface indicators only, not detections)")
    print(f"Report:\n    {report_path}")
    print(f"Log:\n    {out_dir / 'run.log'}")

    if open_qgis and project:
        qgis_project.open_in_qgis(project, qgis.qgis_bin if qgis else None)
    return 0 if available else 2


def rebuild_qgis(config: dict) -> int:
    import qgis_project

    out_dir = ROOT / config["output_dir"]
    spec_path = out_dir / "qgis_layers.json"
    if not spec_path.exists():
        print("No previous run found (output/qgis_layers.json missing). Run an analysis first.")
        return 1
    installs = env_check.find_qgis_installations()
    qgis = next((q for q in installs if q.qgis_python), None)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    return 0 if qgis_project.write_project(spec, spec_path, qgis.qgis_python if qgis else None) else 1


def open_last_project(config: dict) -> None:
    import qgis_project

    project = ROOT / config["output_dir"] / "project.qgz"
    if not project.exists():
        print("No project yet: output/project.qgz does not exist. Run an analysis first.")
        return
    installs = env_check.find_qgis_installations()
    qgis_project.open_in_qgis(project, installs[0].qgis_bin if installs else None)


def ask_float(prompt: str, low: float, high: float) -> float:
    while True:
        raw = input(prompt).strip().replace(",", ".")
        try:
            value = float(raw)
            if low <= value <= high:
                return value
        except ValueError:
            pass
        print(f"  Please enter a number between {low} and {high}.")


def ask_radius(config: dict) -> float:
    options = config["allowed_radius_options_m"]
    default = config["default_radius_m"]
    while True:
        raw = input(f"Radius in metres {options} [default {default}]: ").strip()
        if not raw:
            return float(default)
        if raw.isdigit() and int(raw) in options:
            return float(raw)
        print(f"  Choose one of {options}.")


def interactive(config: dict) -> int:
    while True:
        print()
        print("=" * 52)
        print(" UNDERGROUND ANOMALY SCANNER")
        print(" (surface anomaly research - indirect indicators)")
        print("=" * 52)
        print("[1] Start Analysis")
        print("[2] Open QGIS")
        print("[3] Exit")
        choice = input("> ").strip()
        if choice == "1":
            lat = ask_float("Latitude  (WGS84, e.g. 40.735): ", -90, 90)
            lon = ask_float("Longitude (WGS84, e.g. 31.605): ", -180, 180)
            radius = ask_radius(config)
            run_analysis(lat, lon, radius, config)
        elif choice == "2":
            open_last_project(config)
        elif choice == "3":
            return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Surface anomaly research from free satellite data (QGIS output).")
    parser.add_argument("--lat", type=float, help="Latitude, WGS84 decimal degrees")
    parser.add_argument("--lon", type=float, help="Longitude, WGS84 decimal degrees")
    parser.add_argument("--radius", type=float, help="Radius in metres (100, 250, 500, 1000, 2000)")
    parser.add_argument("--check-env", action="store_true", help="Print environment report and exit")
    parser.add_argument("--build-qgis", action="store_true", help="Rebuild output/project.qgz from the last run")
    parser.add_argument("--open", action="store_true", help="Open the project in QGIS when finished")
    parser.add_argument("--refresh", action="store_true", help="Ignore cached scene selections and search again")
    parser.add_argument("--s1-safe", type=Path, help="Optional Sentinel-1 SAFE product (.zip/.SAFE) for ESA SNAP processing")
    args = parser.parse_args()
    config = load_config()

    if args.check_env:
        report = env_check.build_environment_report(config["connectivity_check_url"], config["connectivity_timeout_s"])
        print(env_check.format_report(report))
        env_check.save_report_json(report, ROOT / config["output_dir"] / "environment.json")
        return 0
    if not ensure_dependencies():
        return 1
    if args.build_qgis:
        return rebuild_qgis(config)
    if args.lat is None and args.lon is None:
        return interactive(config)
    if args.lat is None or args.lon is None:
        parser.error("--lat and --lon must be given together")

    radius = args.radius if args.radius is not None else float(config["default_radius_m"])
    if int(radius) not in config["allowed_radius_options_m"]:
        parser.error(f"--radius must be one of {config['allowed_radius_options_m']}")
    try:
        from aoi import validate_coordinates

        validate_coordinates(args.lat, args.lon)
    except ValueError as exc:
        parser.error(str(exc))
    return run_analysis(args.lat, args.lon, radius, config, refresh=args.refresh, s1_safe=args.s1_safe,
                        open_qgis=args.open)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)
