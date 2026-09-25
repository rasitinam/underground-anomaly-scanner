"""
Phase 7: build output/project.qgz by running qgis_builder.py with QGIS's own
Python (auto-detected, never hardcoded), and open it in QGIS.
"""
from __future__ import annotations

import json
import os
import platform
import subprocess
from pathlib import Path

from env_check import run_interpreter
from logger import log

BUILDER = Path(__file__).with_name("qgis_builder.py")

# (file name in output/, layer name, style, visible)
LAYER_GROUPS: list[tuple[str, list[tuple[str, str, str, bool]]]] = [
    ("Anomaly (indirect surface indicators)", [
        ("anomaly_areas.geojson", "MULTI-SOURCE anomaly areas", "anomaly_polygons", True),
        ("anomaly.tif", "Anomaly consistency score (0-1)", "anomaly", True),
        ("anomaly_source_count.tif", "Number of agreeing sources", "count", False),
        ("anomaly_sar.tif", "SAR backscatter anomaly", "anomaly", False),
        ("anomaly_vegetation.tif", "Vegetation anomaly (NDVI)", "anomaly", False),
        ("anomaly_terrain.tif", "Terrain anomaly (local relief)", "anomaly", False),
        ("anomaly_temporal.tif", "Temporal persistence", "anomaly", False),
    ]),
    ("Sentinel-1 SAR", [
        ("s1_vv_db.tif", "Sentinel-1 VV (dB)", "gray", False),
        ("s1_vh_db.tif", "Sentinel-1 VH (dB)", "gray", False),
        ("s1_vv_minus_vh_db.tif", "Sentinel-1 VV-VH (dB)", "gray", False),
        ("s1_vv_change_db.tif", "SAR change VV (latest - earliest, dB)", "diverging", False),
        ("s1_vv_temporal_std_db.tif", "SAR temporal variability VV (dB)", "gray", False),
    ]),
    ("Sentinel-2 optical", [
        ("s2_rgb.tif", "Sentinel-2 RGB", "rgb", True),
        ("s2_false_color.tif", "Sentinel-2 False Color (NIR-R-G)", "rgb", False),
        ("ndvi.tif", "NDVI", "ndvi", False),
        ("ndwi.tif", "NDWI", "ndwi", False),
        ("nbr.tif", "NBR", "nbr", False),
        ("ndvi_change.tif", "NDVI change (latest - reference)", "diverging", False),
    ]),
    ("Terrain (Copernicus DEM)", [
        ("hillshade.tif", "Hillshade", "gray", True),
        ("dem.tif", "DEM elevation (m)", "dem", False),
        ("slope.tif", "Slope (deg)", "gray", False),
        ("aspect.tif", "Aspect (deg)", "gray", False),
        ("lrm.tif", "Local relief model (m)", "diverging", False),
    ]),
]


def build_spec(out_dir: Path, aoi_geojson: Path, utm_epsg: int, extent_utm: tuple, osm_url: str,
               title: str) -> dict:
    groups = []
    for group_name, layers in LAYER_GROUPS:
        entries = []
        for filename, name, style, visible in layers:
            path = out_dir / filename
            if path.exists():
                kind = "vector" if path.suffix == ".geojson" else "raster"
                entries.append({"kind": kind, "path": str(path.resolve()), "name": name,
                                "style": style, "visible": visible})
        if entries:
            groups.append({"name": group_name, "layers": entries})
    groups[0:0] = [{"name": "Study area", "layers": [
        {"kind": "vector", "path": str(aoi_geojson.resolve()), "name": "AOI", "style": "aoi", "visible": True}]}]
    groups.append({"name": "Basemap", "layers": [
        {"kind": "xyz", "url": osm_url, "name": "OpenStreetMap", "visible": True}]})
    return {
        "output": str((out_dir / "project.qgz").resolve()),
        "crs_epsg": utm_epsg,
        "extent": list(extent_utm),
        "title": title,
        "groups": groups,
    }


def _clean_env() -> dict:
    env = dict(os.environ)
    for var in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
        env.pop(var, None)
    return env


def write_project(spec: dict, spec_path: Path, qgis_python: str | None) -> Path | None:
    spec_path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    if not qgis_python:
        log("QGIS Python not found -> project.qgz not created. Layers are in output/ as GeoTIFF/GeoJSON; "
            "after installing QGIS run: python main.py --build-qgis", "WARNING")
        return None
    log(f"Building QGIS project with {qgis_python}")
    try:
        proc = run_interpreter(qgis_python, [str(BUILDER), str(spec_path)], timeout=300, env=_clean_env())
    except Exception as exc:  # noqa: BLE001
        log(f"QGIS project build failed to start: {exc}", "ERROR")
        return None
    for line in (proc.stdout or "").splitlines():
        if line.startswith("WARNING"):
            log(line, "WARNING")
    project = Path(spec["output"])
    if proc.returncode != 0 or "PROJECT_WRITTEN" not in (proc.stdout or "") or not project.exists():
        log(f"QGIS project build failed (exit {proc.returncode}): {(proc.stderr or proc.stdout)[-1500:]}", "ERROR")
        return None
    log(f"QGIS project written: {project}")
    return project


def open_in_qgis(project: Path, qgis_bin: str | None) -> bool:
    try:
        if platform.system() == "Windows":
            os.startfile(str(project))  # type: ignore[attr-defined]  # .qgz association -> QGIS
            return True
        if qgis_bin:
            subprocess.Popen([qgis_bin, str(project)])
            return True
        if platform.system() == "Darwin":
            subprocess.Popen(["open", str(project)])
            return True
        subprocess.Popen(["xdg-open", str(project)])
        return True
    except Exception as exc:  # noqa: BLE001
        log(f"Could not open QGIS automatically ({exc}). Open {project} manually.", "WARNING")
        return False
