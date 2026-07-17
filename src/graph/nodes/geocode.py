"""
地理编码节点 — 填充经纬度和海拔。

策略（5 级优先）：
  1. 论文中明确写出的经纬度（geo_source=paper）
  2. 内置机构查找表（geo_source=lookup）
  3. 百度地图 API（geo_source=baidu）
  4. Nominatim/OpenStreetMap（geo_source=nominatim）
  5. 省会兜底（geo_source=province_fallback）
"""

from __future__ import annotations
import logging

from src.config import AppConfig
from src.core.geocoder import Geocoder, _supplement_altitude_from_province
from src.graph.state import PaperState

logger = logging.getLogger("paper_extractor")


def geocode_node(state: PaperState, config: AppConfig, geocoder: Geocoder) -> dict:
    """
    地理编码节点：根据地名填充经纬度和海拔。

    已有经纬度的 study 跳过（geo_source=paper）。
    """
    extraction = state.get("extraction", {})
    studies = extraction.get("studies", [])

    geocoded_count = 0
    for study in studies:
        lat = study.get("latitude")
        lon = study.get("longitude")

        # 论文中已有的经纬度，直接标记来源
        if lat is not None and lon is not None:
            study["geo_source"] = "paper"
            continue

        region = study.get("site_administrative_region", "") or ""
        site = study.get("experimental_site_name", "") or ""

        if not region and not site:
            study["geo_source"] = "unknown"
            continue

        result = geocoder.geocode(region, site)
        if result:
            study["latitude"] = result.latitude
            study["longitude"] = result.longitude
            study["geo_source"] = result.source
            # 补充海拔（优先用 geocode 结果，否则用省会兜底）
            if result.altitude is not None and study.get("altitude") is None:
                study["altitude"] = result.altitude
            if study.get("altitude") is None:
                _supplement_altitude_from_province(study, region, site)
            geocoded_count += 1
        else:
            study["geo_source"] = "unknown"

    return {
        "extraction": extraction,
        "geocoded": True,
        "status": "geocoded",
    }
