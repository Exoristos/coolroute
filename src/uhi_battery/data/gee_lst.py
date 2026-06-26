"""Google Earth Engine Land Surface Temperature puller.

Isolates every ``ee.`` call behind module-level functions so unit tests can
monkeypatch ``ee`` entirely without hitting the network.

Products:
- Landsat 8/9 Collection 2 Level-2 LST (band ``ST_B10``, °C), masked via
  ``QA_PIXEL`` cloud bits.
- MODIS MOD11A1 / MYD11A1 daily LST day + night (Kelvin × 0.02 → °C),
  masked via ``QC_Day`` band.

Credentials: service-account JSON key (from Settings) OR
``google.auth.default()`` fallback.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import ee

if TYPE_CHECKING:
    from uhi_battery.config import Settings


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def init_ee(settings: Settings) -> None:
    """Initialise Earth Engine with service-account or default credentials.

    Raises
    ------
    RuntimeError
        If neither a service account nor default credentials are available.
    """
    service_account = settings.gee_service_account
    key_path = settings.gee_private_key_path

    if service_account and key_path:
        # Service-account flow
        key_file = Path(key_path)
        if not key_file.exists():
            raise FileNotFoundError(f"GEE key file not found: {key_path}")
        # Load key to extract project id if not explicitly set
        try:
            key_data = json.loads(key_file.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError(f"Invalid GEE key file: {key_path}") from exc
        project_id = settings.gee_project_id or key_data.get("project_id", "")
        credentials = ee.ServiceAccountCredentials(service_account, str(key_file))
        ee.Initialize(credentials, project=project_id)
    else:
        # Interactive (user) auth flow — one-time browser consent, then cached.
        # Needs a registered Earth Engine project (https://code.earthengine.google.com/).
        project_id = settings.gee_project_id
        if not project_id:
            raise RuntimeError(
                "Earth Engine needs a project id. After registering at "
                "https://code.earthengine.google.com/ , set GEE_PROJECT_ID in .env "
                "(or use a service account: GEE_SERVICE_ACCOUNT + GEE_PRIVATE_KEY_PATH)."
            )
        try:
            ee.Authenticate()  # one-time browser flow; cached for later runs
            ee.Initialize(project=project_id)
        except ee.EEException as exc:
            raise RuntimeError(
                "Earth Engine interactive auth failed. Ensure you are registered "
                "at https://code.earthengine.google.com/ and GEE_PROJECT_ID is correct."
            ) from exc


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _ee_bbox(bbox: tuple[float, float, float, float]) -> ee.Geometry:
    """Build an ee.Geometry rectangle from (west, south, east, north)."""
    return ee.Geometry.Rectangle([bbox[0], bbox[1], bbox[2], bbox[3]])


def _mask_landsat_clouds(image: ee.Image) -> ee.Image:
    """Mask Landsat pixels using QA_PIXEL cloud bits (bits 3-4)."""
    qa = image.select("QA_PIXEL")
    cloud_mask = qa.bitwiseAnd(1 << 3).eq(0).And(qa.bitwiseAnd(1 << 4).eq(0))
    return image.updateMask(cloud_mask)


def _mask_modis_clouds(image: ee.Image) -> ee.Image:
    """Mask MODIS LST using QC_Day mandatory QA flags (bits 0-1)."""
    qc = image.select("QC_Day")
    # bits 0-1: 0 = good quality
    mask = qc.bitwiseAnd(3).eq(0)
    return image.updateMask(mask)


_KELVIN_TO_CELSIUS = 0.02
"""MODIS LST scale factor: DN × 0.02 → Kelvin."""

_CELSIUS_OFFSET = -273.15
"""Offset to convert Kelvin → Celsius."""


def _modis_lst_celsius(image: ee.Image, band: str = "LST_Day_1km") -> ee.Image:
    """Convert MODIS LST band from scaled-Kelvin to Celsius."""
    lst = image.select(band).multiply(_KELVIN_TO_CELSIUS).add(_CELSIUS_OFFSET)
    return image.addBands(lst.rename("LST_C"), None, True)


# ---------------------------------------------------------------------------
# Public pullers
# ---------------------------------------------------------------------------


def pull_landsat_lst(
    bbox: tuple[float, float, float, float],
    start: str,
    end: str,
    out_dir: str | Path = "data/raw/landsat",
) -> list[Path]:
    """Download Landsat 8/9 LST (Collection 2, ST_B10, °C) as GeoTIFFs.

    Masks cloudy pixels via QA_PIXEL.  Exports one GeoTIFF per scene to
    *out_dir* via ``ee.batch.Export.image.toDrive``.

    .. note::
        Programmatic GEE ``getDownloadURL`` is unreliable for large regions.
        This function returns a list of :class:`ee.batch.Task` objects that
        must be started / waited on outside this module.  For a fully
        synchronous path use the companion ``download_landsat_to_memory``.

    Returns
    -------
    list[Path]
        Paths to exported GeoTIFF files (may not exist until tasks complete).
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    region = _ee_bbox(bbox)
    collection = (
        ee.ImageCollection("LANDSAT/LC08/C02/T1_L2")
        .merge(ee.ImageCollection("LANDSAT/LC09/C02/T1_L2"))
        .filterBounds(region)
        .filterDate(start, end)
        .map(_mask_landsat_clouds)
    )

    # ST_B10 is the thermal band (Kelvin); convert to °C
    def _celsius(img: ee.Image) -> ee.Image:
        lst = img.select("ST_B10").multiply(0.00341802).add(149.0).subtract(273.15)
        return img.addBands(lst.rename("LST_C"), None, True)

    collection = collection.map(_celsius).select("LST_C")

    # Export each image to Drive; user must run tasks
    image_list = collection.toList(collection.size())
    n = image_list.length().getInfo()

    paths: list[Path] = []
    for i in range(n):
        img: ee.Image = ee.Image(image_list.get(i))
        img_date = img.date().format("YYYYMMdd_HHmmss").getInfo()
        fname = f"landsat_lst_{img_date}.tif"
        task = ee.batch.Export.image.toDrive(
            image=img.select("LST_C"),
            description=f"landsat_{img_date}",
            folder="uhi_battery_landsat",
            fileNamePrefix=fname.replace(".tif", ""),
            region=region,
            scale=30,
            crs="EPSG:4326",
            maxPixels=1e9,
        )
        task.start()
        paths.append(out_dir / fname)

    return paths


def pull_modis_lst(
    bbox: tuple[float, float, float, float],
    start: str,
    end: str,
    out_dir: str | Path = "data/raw/modis",
) -> tuple[list[Path], list[Path]]:
    """Download MODIS daily LST day + night (MOD11A1 + MYD11A1) to GeoTIFFs.

    Returns
    -------
    tuple[list[Path], list[Path]]
        (day_paths, night_paths) — one GeoTIFF per day per band.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    region = _ee_bbox(bbox)

    day_paths: list[Path] = []
    night_paths: list[Path] = []

    for product, prefix in [("MODIS/061/MOD11A1", "mod11a1"), ("MODIS/061/MYD11A1", "myd11a1")]:
        col = (
            ee.ImageCollection(product)
            .filterBounds(region)
            .filterDate(start, end)
            .map(_mask_modis_clouds)
            .map(lambda img: _modis_lst_celsius(img, "LST_Day_1km"))
            .map(lambda img: _modis_lst_celsius(img, "LST_Night_1km"))
        )

        for band, band_label in [("LST_Day_1km", "day"), ("LST_Night_1km", "night")]:
            band_col = col.map(
                lambda img, b=band: _modis_lst_celsius(img, b).select("LST_C")
            )

            img_list = band_col.toList(band_col.size())
            n = img_list.length().getInfo()

            for i in range(n):
                img: ee.Image = ee.Image(img_list.get(i))
                img_date = img.date().format("YYYYMMdd").getInfo()
                fname = f"{prefix}_{band_label}_{img_date}.tif"
                task = ee.batch.Export.image.toDrive(
                    image=img,
                    description=f"{prefix}_{band_label}_{img_date}",
                    folder="uhi_battery_modis",
                    fileNamePrefix=fname.replace(".tif", ""),
                    region=region,
                    scale=1000,
                    crs="EPSG:4326",
                    maxPixels=1e9,
                )
                task.start()
                path = out_dir / fname
                if band_label == "day":
                    day_paths.append(path)
                else:
                    night_paths.append(path)

    return day_paths, night_paths
