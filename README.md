# Underground Anomaly Scanner — surface anomaly research from free satellite data

> **Satellite remote sensing data does not directly image underground objects. This software detects surface and subsurface-related anomalies indirectly. Confirming an underground structure requires ground-based geophysical methods such as GPR, electrical resistivity tomography, seismic methods, or other appropriate field measurements.**
>
> **TR:** Uydu uzaktan algılama verileri yeraltındaki nesneleri doğrudan görüntülemez. Bu yazılım yalnızca yüzeyde görülen ve yeraltıyla ilişkili olabilecek anomalileri dolaylı olarak tespit eder. Yeraltında bir yapının varlığını doğrulamak için GPR, elektrik özdirenç tomografisi (ERT), sismik yöntemler veya uygun diğer saha ölçümleri gerekir. **GPR uydu analizinin alternatifi değildir; gerektiğinde uygulanan bir saha doğrulama yöntemidir.**

You give a coordinate. The tool builds a study area around it and pulls free, official satellite data for it, cropped to the study area only. It computes explainable surface-anomaly layers and writes a ready-to-open QGIS project plus an HTML report.

```
Coordinate → AOI → free official satellite data → processing → anomaly analysis → QGIS project → HTML report
```

The tool never says "there is a tunnel/room/buried structure here". It only uses the terms *vegetation anomaly*, *SAR backscatter anomaly*, *terrain anomaly*, *temporal persistence* and *MULTI-SOURCE ANOMALY*.

## Quick start (Windows)

1. Install **QGIS** (free): https://qgis.org/download/ — the tool finds it automatically (Program Files, OSGeo4W, registry); no paths are hardcoded.
2. Check your machine. Nothing gets installed by this step:
   ```
   python main.py --check-env
   ```
   The report shows QGIS / QGIS-Python / SNAP / internet status and each library. For any missing ones it prints one `pip install` line.
3. Install **only the missing** libraries. A virtual environment is recommended so QGIS's own Python stays untouched:
   ```
   py -m venv .venv
   .venv\Scripts\python -m pip install -r requirements.txt
   ```
4. Run:
   ```
   .venv\Scripts\python main.py --lat 40.735 --lon 31.605 --radius 250 --open
   ```
   You can also run `python main.py` with no arguments to get the interactive menu:
   ```
   UNDERGROUND ANOMALY SCANNER
   [1] Start Analysis   [2] Open QGIS   [3] Exit
   ```

The main script runs in any Python 3.10+ that has the libraries. Only the QGIS-project step runs inside **QGIS's own Python** (`python-qgis*.bat`), started automatically as a subprocess.

| Option | Meaning |
|---|---|
| `--radius` | 100, 250 (default), 500, 1000, 2000 m. Larger areas take longer; the tool warns above 500 m. |
| `--open` | Open `output/project.qgz` in QGIS when finished |
| `--refresh` | Ignore cached scene selections and search the catalog again |
| `--s1-safe PATH` | Optional: a full Sentinel-1 SAFE product for ESA SNAP preprocessing (see below) |
| `--build-qgis` | Rebuild the QGIS project from the last run (e.g. after installing QGIS) |
| `--check-env` | Environment report only |

## Outputs (`output/`)

| File | Content |
|---|---|
| `project.qgz` | QGIS project, grouped and styled: Study area, Anomaly, Sentinel-1, Sentinel-2, Terrain, OpenStreetMap |
| `s2_rgb.tif`, `s2_false_color.tif` | Sentinel-2 true colour / false colour (NIR-R-G) |
| `ndvi.tif`, `ndwi.tif`, `nbr.tif`, `ndre.tif`, `ndmi.tif`, `ndvi_change.tif` | Spectral indices of the latest clear scene (NDRE = red-edge, sensitive to crop marks; NDMI = moisture), and the NDVI change vs. a scene ~1 year earlier |
| `*_median.tif`, `ndvi_temporal_std.tif` | **Multi-date median** of up to 10 clear scenes of the last year (cleanest optical layers), and NDVI variability over the year |
| `s1_vv_mean_db.tif`, `s1_vh_mean_db.tif` | **Multi-date mean** of up to 15 SAR dates (much less speckle) |
| `s1_vv_db.tif`, `s1_vh_db.tif`, `s1_vv_change_db.tif`, `s1_vv_temporal_std_db.tif` | Latest Sentinel-1 backscatter (dB), change and multi-date variability |
| `dem.tif`, `slope.tif`, `aspect.tif`, `hillshade.tif`, `lrm.tif` | Copernicus DEM derivatives; `lrm` = local relief model |
| `anomaly_*.tif` | Per-source anomaly scores (0–1) |
| `anomaly.tif` | Combined **anomaly consistency score** (0–1) |
| `anomaly_areas.geojson` | MULTI-SOURCE anomaly polygons with area, score and the agreeing indicators |
| `report.html` | Report: coordinate, radius, sources + licences, dates, cloud cover, scenes, DEM, anomaly areas, limitations |
| `metadata.json` | Per layer: source, product, date, resolution, CRS |
| `run.log` | Log of every step |

Downloaded data is cached in `data/cache/` (per coordinate + radius). Re-running the same coordinate reuses the cache and also works offline.

## Data sources and licences

All data is free and official. Nothing needs an account or API key. Everything is fetched through the public STAC API of Microsoft Planetary Computer, which hosts the original ESA/EU products unchanged as Cloud-Optimized GeoTIFFs. Only the pixels inside the AOI are read; whole satellite tiles are never downloaded.

| Data | Origin | Access URL | Licence |
|---|---|---|---|
| Sentinel-2 L2A (10–20 m) | ESA / EU Copernicus | https://planetarycomputer.microsoft.com/dataset/sentinel-2-l2a | Copernicus Sentinel data terms (free, full, open). Attribution: "Contains modified Copernicus Sentinel data [year]" |
| Sentinel-1 RTC / GRD (C-band SAR) | ESA / EU Copernicus | https://planetarycomputer.microsoft.com/dataset/sentinel-1-rtc , https://planetarycomputer.microsoft.com/dataset/sentinel-1-grd | Copernicus Sentinel data terms; see the RTC dataset page for derived-product terms |
| Copernicus DEM GLO-30 | ESA / EU (DLR, Airbus) | https://planetarycomputer.microsoft.com/dataset/cop-dem-glo-30 | Copernicus DEM licence, free with attribution |
| OpenStreetMap basemap | OSM contributors | https://www.openstreetmap.org/copyright | ODbL; tile usage policy https://operations.osmfoundation.org/policies/tiles/ |
| Esri World Imagery basemap (QGIS only) | Esri | https://www.arcgis.com/home/item.html?id=10df2279f9684e4a9f6a7f08febac2a9 | **Visual reference only, never analysed, not open data**; Esri terms of use apply. Disable by setting `satellite_basemap_xyz_url` to `""` in `config.json` |
| STAC catalog | Microsoft Planetary Computer | https://planetarycomputer.microsoft.com/api/stac/v1 | Free, anonymous |

Also possible, but not used in v1: Copernicus Data Space Ecosystem (https://dataspace.copernicus.eu, free account needed for downloads; use it to get SAFE files for SNAP) and Landsat 8/9 (USGS, public domain; 30 m, so coarser than Sentinel-2).

## Processing

- **AOI**: a WGS84 coordinate. The matching UTM zone is picked automatically, and the AOI is a square of ±radius. Every source is warped onto **one common 10 m UTM grid**, so layers can be compared pixel by pixel.
- **Sentinel-2**: scenes from the last ~15 months. Cloud-cover fallback **≤10% → ≤20% → ≤30%**, then the least cloudy scene with a warning. The newest scene passing the rule is used. SCL cloud, shadow and snow pixels are masked. The processing-baseline 04.00 offset is handled. A reference scene about 1 year older (same season) is used for temporal comparison. In addition, up to 10 clear scenes (≤20% cloud) of the last year, one per date, are cloud-masked and combined into a **per-pixel median**. This removes noise and single-day effects, but it cannot add detail below the 10 m pixel. The anomaly analysis uses the median NDVI, and the temporal persistence uses every date.
- **Sentinel-1**: up to 15 dual-pol scenes from the **same relative orbit and pass direction**, so viewing geometry is constant. Processing uses the best level available:
  1. **ESA SNAP** (if installed and `--s1-safe` is given): Apply Orbit File → Calibration (σ⁰) → Lee Speckle Filter → Terrain Correction (Copernicus 30 m DEM) → dB.
  2. **`sentinel-1-rtc`** (radiometrically terrain corrected γ⁰), if accessible anonymously.
  3. **`sentinel-1-grd`** fallback: windowed read and GCP geocoding (only the AOI window is read), approximate σ⁰ from the product's calibration LUT, Lee filter, dB. **No terrain correction**: positions may shift in hilly terrain, so treat SAR anomalies as relative indicators. The processing level of every scene is written to the report.

  All dates are then averaged in linear power (multi-temporal speckle reduction), and the SAR anomaly uses this mean.
- **Display**: QGIS shows rasters with bilinear resampling, so 10 m pixels are not drawn as blocks. This is smoother on screen but not more detailed.
- **DEM**: elevation, slope, aspect and hillshade use Horn's method (same as `gdaldem`), done in numpy. The local relief model is DEM − Gaussian-smoothed DEM (~100 m scale) and highlights small mounds and depressions. Native resolution is ~30 m; resampling to the 10 m grid adds no detail.

## Anomaly method (simple and explainable)

For each source: `z = (pixel − local_mean) / local_std` in a moving window. The default is 31 px ≈ 310 m; it must be much larger than the features of interest, because a feature filling a fraction *f* of the window caps |z| at √((1−f)/f). The z value then maps to a score, `score = clip(|z| / (2·threshold), 0, 1)` with threshold |z| = 2.

| Source | Input |
|---|---|
| vegetation | NDVI |
| SAR | mean \|z\| of VV and VH (dB) |
| terrain | local relief model |
| temporal | **persistence**: the fraction of acquisition dates in which the pixel is anomalous (SAR stack, both NDVI dates) |

```
anomaly_consistency_score = 0.35·SAR + 0.30·vegetation + 0.20·terrain + 0.15·temporal
```
If a source is missing, the weights are renormalized over the sources that are available. A pixel is a **MULTI-SOURCE ANOMALY** when at least 3 independent sources exceed the threshold (or all of them, if only 2 are available) and the score is ≥ 0.5. Areas smaller than 4 pixels are dropped. One source alone never produces a multi-source anomaly.

**Important:** the weights are a transparent heuristic. They are **not** calibrated against ground truth and do **not** express "underground detection accuracy". The score is the *confidence of anomaly consistency*: how consistently several independent surface indicators differ from their surroundings. It is **not** the probability of an underground object.

## Limitations

- Every indicator is measured at or very near the surface. Optical data sees canopy and soil. C-band SAR reaches at most a few cm into dry soil. The DEM is surface elevation.
- Resolution: Sentinel-2 is 10–20 m and the DEM is 30 m. Features smaller than ~2–3 pixels cannot be resolved.
- Most anomalies have ordinary causes: field boundaries, crop types, irrigation, soil moisture, roads, buildings, construction, shadows, clouds and seasons. Always check against the RGB image, the basemap and a field visit.
- The GRD fallback is not terrain corrected (see above).
- **Next step for any area of interest:** a field survey, then ground-based geophysics (GPR, ERT, seismic, magnetometry) where appropriate.

## Offline self-test

```
python selftest_offline.py
```
This seeds the cache with synthetic rasters that contain one planted surface feature. It then runs the full pipeline from the cache, with no internet, and checks that exactly that feature is reported. A pure-noise control gives no anomaly areas.

## Files

```
main.py            CLI / interactive menu, orchestration, error isolation per data source
config.json        radius options, cloud thresholds, weights, window size, endpoints
env_check.py       QGIS / QGIS-Python / SNAP / library / internet detection (Phase 1)
aoi.py             coordinate validation, UTM zone, AOI (Phase 2-3)
catalog.py         STAC search + cached scene selection
raster_utils.py    common grid, cached windowed reads, GeoTIFF writing, z-score helpers
sentinel2.py       Phase 4-5     dem.py        Phase 6
sentinel1.py       Phase 8-9     anomaly.py    Phase 10
qgis_project.py    Phase 7 (launcher)   qgis_builder.py  runs inside QGIS Python
report.py          Phase 11 HTML report
selftest_offline.py  offline end-to-end test with synthetic data
```
