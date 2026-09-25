"""
Runs INSIDE QGIS's own Python (PyQGIS), headless. Do not import project modules here.

Usage (done automatically by qgis_project.py):
    <qgis python> qgis_builder.py output/qgis_layers.json

Reads a JSON layer spec and writes a styled, grouped .qgz project.
"""
from __future__ import annotations

import json
import os
import sys

from qgis.core import (
    QgsApplication,
    QgsColorRampShader,
    QgsContrastEnhancement,
    QgsCoordinateReferenceSystem,
    QgsFillSymbol,
    QgsPalLayerSettings,
    QgsProject,
    QgsRasterLayer,
    QgsRasterMinMaxOrigin,
    QgsRasterShader,
    QgsRectangle,
    QgsReferencedRectangle,
    QgsSingleBandPseudoColorRenderer,
    QgsTextFormat,
    QgsVectorLayer,
    QgsVectorLayerSimpleLabeling,
)
from qgis.PyQt.QtGui import QColor

RAMPS = {
    "ndvi": [(-0.2, "#a6611a"), (0.1, "#dfc27d"), (0.3, "#f5f5a0"), (0.5, "#80cd6b"), (0.8, "#1a7a2e")],
    "ndwi": [(-0.6, "#8c510a"), (-0.2, "#f6e8c3"), (0.0, "#c7eae5"), (0.4, "#2166ac")],
    "nbr": [(-0.3, "#7f3b08"), (0.0, "#fee0b6"), (0.3, "#b2df8a"), (0.7, "#1b7837")],
    "anomaly": [(0.0, "#ffffff00"), (0.35, "#ffffb200"), (0.5, "#fecc5c"), (0.7, "#fd8d3c"), (1.0, "#bd0026")],
    "count": [(0, "#ffffff00"), (1, "#ffffb2"), (2, "#fecc5c"), (3, "#f03b20"), (4, "#7a0177")],
}


def color(hex_code: str) -> QColor:
    h = hex_code.lstrip("#")
    if len(h) == 8:
        return QColor(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), int(h[6:8], 16))
    return QColor("#" + h)


def pseudocolor(layer: QgsRasterLayer, stops: list[tuple[float, str]], discrete: bool = False) -> None:
    ramp = QgsColorRampShader()
    ramp.setColorRampType(QgsColorRampShader.Discrete if discrete else QgsColorRampShader.Interpolated)
    ramp.setColorRampItemList([QgsColorRampShader.ColorRampItem(v, color(c), str(v)) for v, c in stops])
    shader = QgsRasterShader()
    shader.setRasterShaderFunction(ramp)
    renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
    renderer.setClassificationMin(stops[0][0])
    renderer.setClassificationMax(stops[-1][0])
    layer.setRenderer(renderer)


def symmetric_stops(layer: QgsRasterLayer) -> list[tuple[float, str]]:
    stats = layer.dataProvider().bandStatistics(1)
    limit = max(abs(stats.minimumValue), abs(stats.maximumValue), 1e-6)
    limit = min(limit, 3 * stats.stdDev) if stats.stdDev > 0 else limit
    return [(-limit, "#2166ac"), (0.0, "#f7f7f7"), (limit, "#b2182b")]


def gray_stretch(layer: QgsRasterLayer) -> None:
    layer.setContrastEnhancement(
        QgsContrastEnhancement.StretchToMinimumMaximum, QgsRasterMinMaxOrigin.CumulativeCut
    )


def dem_style(layer: QgsRasterLayer) -> None:
    stats = layer.dataProvider().bandStatistics(1)
    lo, hi = stats.minimumValue, stats.maximumValue
    span = max(hi - lo, 1e-6)
    stops = [(lo, "#1a9641"), (lo + 0.33 * span, "#a6d96a"), (lo + 0.66 * span, "#fdae61"), (hi, "#a0522d")]
    pseudocolor(layer, stops)


def style_raster(layer: QgsRasterLayer, style: str) -> None:
    if style == "rgb":
        return  # 3-band uint8 -> QGIS default multiband color renderer
    if style in RAMPS:
        pseudocolor(layer, RAMPS[style], discrete=(style == "count"))
    elif style == "diverging":
        pseudocolor(layer, symmetric_stops(layer))
    elif style == "dem":
        dem_style(layer)
    else:
        gray_stretch(layer)
    if style in ("anomaly", "count"):
        layer.renderer().setOpacity(0.75)


def style_vector(layer: QgsVectorLayer, style: str) -> None:
    if layer.renderer() is None:  # layer without features/geometry type
        return
    if style == "aoi":
        symbol = QgsFillSymbol.createSimple(
            {"color": "0,0,0,0", "outline_color": "0,0,0,255", "outline_width": "0.6", "outline_style": "dash"}
        )
        layer.renderer().setSymbol(symbol)
    elif style == "anomaly_polygons":
        symbol = QgsFillSymbol.createSimple(
            {"color": "227,26,28,40", "outline_color": "227,26,28,255", "outline_width": "0.8"}
        )
        layer.renderer().setSymbol(symbol)
        settings = QgsPalLayerSettings()
        settings.fieldName = "id"
        fmt = QgsTextFormat()
        fmt.setSize(10)
        settings.setFormat(fmt)
        layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
        layer.setLabelsEnabled(True)


def add_layer(project: QgsProject, group, spec: dict) -> None:
    kind = spec.get("kind", "raster")
    if kind == "xyz":
        uri = f"type=xyz&url={spec['url']}&zmax=19&zmin=0"
        layer = QgsRasterLayer(uri, spec["name"], "wms")
    elif kind == "vector":
        layer = QgsVectorLayer(spec["path"], spec["name"], "ogr")
    else:
        layer = QgsRasterLayer(spec["path"], spec["name"], "gdal")
    if not layer.isValid():
        print(f"WARNING: layer not valid, skipped: {spec['name']} ({spec.get('path') or spec.get('url')})")
        return
    if kind == "raster":
        style_raster(layer, spec.get("style", "gray"))
    elif kind == "vector":
        style_vector(layer, spec.get("style", ""))
    project.addMapLayer(layer, False)
    node = group.addLayer(layer)
    node.setItemVisibilityChecked(bool(spec.get("visible", False)))
    node.setExpanded(False)
    print(f"added: {spec['name']}")


def main() -> int:
    with open(sys.argv[1], encoding="utf-8") as f:
        spec = json.load(f)

    prefix = os.environ.get("QGIS_PREFIX_PATH")
    if prefix:
        QgsApplication.setPrefixPath(prefix, True)
    app = QgsApplication([], False)
    app.initQgis()
    try:
        project = QgsProject.instance()
        project.clear()
        crs = QgsCoordinateReferenceSystem(f"EPSG:{spec['crs_epsg']}")
        project.setCrs(crs)
        project.setTitle(spec.get("title", "Surface anomaly research"))
        root = project.layerTreeRoot()
        for group_spec in spec["groups"]:
            group = root.addGroup(group_spec["name"])
            for layer_spec in group_spec["layers"]:
                add_layer(project, group, layer_spec)
        minx, miny, maxx, maxy = spec["extent"]
        try:
            project.viewSettings().setDefaultViewExtent(QgsReferencedRectangle(QgsRectangle(minx, miny, maxx, maxy), crs))
        except Exception as exc:  # noqa: BLE001 - older QGIS
            print(f"note: could not set default extent ({exc})")
        ok = project.write(spec["output"])
        print("PROJECT_WRITTEN" if ok else "PROJECT_WRITE_FAILED")
        return 0 if ok else 1
    finally:
        app.exitQgis()


if __name__ == "__main__":
    sys.exit(main())
