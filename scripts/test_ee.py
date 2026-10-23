"""One-time Earth Engine auth + tiny pull test.

Runs interactively: ee.Authenticate() opens a browser for one-time consent,
then we query a small MODIS window over the pilot bbox to confirm end-to-end.
"""
from __future__ import annotations

import ee

from uhi_battery.config import Settings


def main() -> None:
    s = Settings()
    print(f"GEE_PROJECT_ID = {s.gee_project_id!r}")
    print("Initializing Earth Engine (a browser may open for one-time consent)...")
    # init_ee handles service-account OR interactive (ee.Authenticate + Initialize)
    from uhi_battery.data.gee_lst import init_ee

    init_ee(s)
    print("EE initialized OK.")

    w, south, e, north = s.pilot_bbox
    aoi = ee.Geometry.Rectangle([w, south, e, north], proj="EPSG:4326", geodesic=False)

    col = (
        ee.ImageCollection("MODIS/061/MOD11A1")
        .filterBounds(aoi)
        .filterDate("2024-07-01", "2024-07-08")
        .select("LST_Day_1km")
    )
    n = int(col.size().getInfo())
    print(f"MODIS MOD11A1 scenes (2024-07-01..08) over pilot bbox: {n}")
    assert n > 0, "No MODIS scenes returned — check bbox/project/auth."

    img = ee.Image(col.first())
    date = ee.Date(img.get("system:time_start")).format("YYYY-MM-dd").getInfo()
    reduced = img.reduceRegion(ee.Reducer.mean(), aoi, scale=1000)
    mean_raw = float(reduced.get("LST_Day_1km").getInfo())
    mean_c = mean_raw * 0.02 - 273.15
    print(f"First scene date: {date}")
    print(f"Mean LST_Day_1km = {mean_raw:.0f} raw  ->  {mean_c:.1f} C")
    print("EE TEST PASSED.")


if __name__ == "__main__":
    main()
