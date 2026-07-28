"""
地理编码模块 — 根据地名计算经纬度和海拔。

策略（按优先级）：
  1. 内置中国农科院/省级农科院/农大查找表（覆盖主要农业试验点）
  2. 天地图地理编码（国内服务，中文解析质量高，推荐）
  3. 百度地图 API（需 baidu_api_key，可选）
  4. 省会城市中心坐标兜底（确保有值可用）

海拔补充（优先级）：
  - geocode 结果中的海拔
  - Open-Meteo Elevation API（SRTM 数据，精度约 90m，无需 Key）
  - 省会城市海拔近似值

用法:
  from src.core.geocoder import Geocoder
  geo = Geocoder(config)
  result = geo.geocode("四川省成都市", "四川省农科院水稻研究所")
  # result.latitude, result.longitude, result.altitude
"""

from __future__ import annotations
import json
import logging
import re
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional, List

from src.core.constants import PROVINCE_CENTROIDS, AGRI_INSTITUTIONS

logger = logging.getLogger("paper_extractor")


@dataclass
class GeoResult:
    """地理编码结果"""
    latitude: float
    longitude: float
    altitude: Optional[float] = None
    source: str = ""  # "lookup" | "tianditu" | "baidu" | "province_fallback"
    matched_name: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class Geocoder:
    """地理编码器 — 多策略查找试验地点坐标和海拔。"""

    def __init__(self, config=None):
        self.config = config
        self._cache: dict = {}
        self._cache_file: Optional[Path] = None
        self._enabled = True
        # 默认服务地址（config 未提供时使用）
        self._tianditu_url = "https://api.tianditu.gov.cn/geocoder"
        self._baidu_url = "https://api.map.baidu.com/geocoding/v3/"
        self._elevation_url = "https://api.open-meteo.com/v1/elevation"

        if config:
            geo_cfg = getattr(config, "geocoding", None)
            if geo_cfg:
                self._enabled = getattr(geo_cfg, "enabled", True)
                self._use_tianditu = getattr(geo_cfg, "use_tianditu", True)
                self._tianditu_tk = getattr(geo_cfg, "tianditu_tk", "")
                self._tianditu_delay = getattr(geo_cfg, "tianditu_delay", 0.2)
                self._tianditu_url = getattr(geo_cfg, "tianditu_url", "https://api.tianditu.gov.cn/geocoder")
                self._baidu_url = getattr(geo_cfg, "baidu_url", "https://api.map.baidu.com/geocoding/v3/")
                self._elevation_url = getattr(geo_cfg, "elevation_url", "https://api.open-meteo.com/v1/elevation")
            if self._enabled:
                self._cache_file = config.cache_path / "geocoding_cache.json"
                self._load_cache()

    # ── 主入口 ───────────────────────────────────────────

    def geocode(self, region: Optional[str], site_name: Optional[str] = None) -> Optional[GeoResult]:
        if not self._enabled:
            return None
        if not region and not site_name:
            return None

        cache_key = f"{region or ''}||{site_name or ''}"
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            return GeoResult(**cached) if cached else None

        result = None

        # 策略 1: 内置科研机构查找表
        result = self._lookup_table(region, site_name)

        # 策略 2: 天地图地理编码
        if result is None:
            result = self._tianditu_geocode(region, site_name)

        # 策略 3: 百度地图 API
        if result is None:
            result = self._baidu_geocode(region, site_name)

        # 策略 4: 省会城市中心坐标兜底
        if result is None:
            result = self._province_fallback(region)

        if self._cache is not None:
            self._cache[cache_key] = result.to_dict() if result else None
            self._save_cache()

        return result

    # ── 策略 1: 查找表 ──────────────────────────────────

    def _lookup_table(self, region: str, site_name: str) -> Optional[GeoResult]:
        combined = f"{region or ''}{site_name or ''}"
        if not combined:
            return None

        best_match = None
        best_kw_len = 0

        for inst in AGRI_INSTITUTIONS:
            for kw in inst["keywords"]:
                if kw in combined and len(kw) > best_kw_len:
                    best_match = inst
                    best_kw_len = len(kw)

        if best_match:
            logger.info(f"    Geo lookup match: {best_match['name']}")
            return GeoResult(
                latitude=best_match["lat"],
                longitude=best_match["lon"],
                altitude=best_match.get("alt"),
                source="lookup",
                matched_name=best_match["name"],
            )
        return None

    # ── 策略 2: 天地图地理编码 ─────────────────────────────

    def _tianditu_geocode(self, region: str, site_name: str) -> Optional[GeoResult]:
        if not self._use_tianditu:
            return None
        if not self._tianditu_tk:
            return None

        try:
            import httpx
        except ImportError:
            return None

        queries = _build_tianditu_queries(region, site_name)

        for query in queries:
            ds = json.dumps({
                "keyWord": query,
                "level": "10",
                "mapBound": "",
                "queryType": "1",
                "start": "0",
                "count": "1",
            }, ensure_ascii=False)

            try:
                resp = httpx.get(self._tianditu_url, params={"ds": ds, "tk": self._tianditu_tk}, timeout=8)
                time.sleep(self._tianditu_delay)

                if resp.status_code == 200:
                    data = resp.json()
                    status = data.get("status")
                    if str(status) != "0":
                        continue

                    location = data.get("location", {})
                    lon = location.get("lon")
                    lat = location.get("lat")
                    if lon is None or lat is None:
                        continue

                    lon_f = float(lon)
                    lat_f = float(lat)
                    level = location.get("level", "")
                    logger.info(f"    Tianditu: '{query}' → ({lat_f:.4f}, {lon_f:.4f}) level={level}")

                    return GeoResult(latitude=lat_f, longitude=lon_f, altitude=None, source="tianditu", matched_name=query)
            except Exception as e:
                logger.warning(f"    Tianditu geocode error for '{query}': {e}")
                continue

        return None

    def _free_altitude(self, lat: float, lon: float) -> Optional[float]:
        """使用 Open-Meteo Elevation API 查询海拔。"""
        try:
            import httpx
        except ImportError:
            return None

        max_retries = 3
        base_delay = 1.0
        timeout = 20.0

        for attempt in range(1, max_retries + 1):
            try:
                resp = httpx.get(self._elevation_url, params={"latitude": lat, "longitude": lon}, timeout=timeout)
                if resp.status_code == 200:
                    data = resp.json()
                    elevation = data.get("elevation")
                    if isinstance(elevation, list) and len(elevation) > 0:
                        return float(elevation[0])
                    if isinstance(elevation, (int, float)):
                        return float(elevation)
                    return None
                logger.warning(f"    Open-Meteo altitude attempt {attempt}/{max_retries} failed: HTTP {resp.status_code}")
            except Exception as e:
                logger.warning(f"    Open-Meteo altitude attempt {attempt}/{max_retries} error: {e}")

            if attempt < max_retries:
                time.sleep(base_delay * (2 ** (attempt - 1)))

        return None

    # ── 策略 3: 百度地图 ─────────────────────────────────

    def _baidu_geocode(self, region: str, site_name: str) -> Optional[GeoResult]:
        api_key = ""
        if self.config:
            geo_cfg = getattr(self.config, "geocoding", None)
            if geo_cfg:
                api_key = getattr(geo_cfg, "baidu_api_key", "")
        if not api_key:
            return None

        try:
            import httpx
        except ImportError:
            return None

        address = region or site_name
        if not address:
            return None

        try:
            resp = httpx.get(self._baidu_url, params={"address": address, "output": "json", "ak": api_key}, timeout=8)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == 0:
                    result = data.get("result", {})
                    location = result.get("location", {})
                    lat = location.get("lat")
                    lon = location.get("lng")
                    if lat is not None and lon is not None:
                        logger.info(f"    Baidu: {address} → ({lat:.4f}, {lon:.4f})")
                        return GeoResult(latitude=lat, longitude=lon, altitude=None, source="baidu", matched_name=address)
        except Exception as e:
            logger.warning(f"    Baidu geocode error for '{address}': {e}")

        return None

    # ── 策略 4: 省会兜底 ─────────────────────────────────

    def _province_fallback(self, region: str) -> Optional[GeoResult]:
        if not region:
            return None
        for province, (lat, lon, alt) in PROVINCE_CENTROIDS.items():
            if province in region:
                logger.info(f"    Province fallback: {province}")
                return GeoResult(latitude=lat, longitude=lon, altitude=alt, source="province_fallback", matched_name=province)
        return None

    # ── 缓存管理 ─────────────────────────────────────────

    def _load_cache(self):
        if self._cache_file and self._cache_file.exists():
            try:
                with open(self._cache_file, "r", encoding="utf-8") as f:
                    self._cache = json.load(f)
                logger.info(f"Loaded {len(self._cache)} cached geocoding results")
            except Exception:
                self._cache = {}

    def _save_cache(self):
        if self._cache_file:
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._cache_file, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, ensure_ascii=False, indent=2)


# ── Pipeline 集成函数 ────────────────────────────────────

def _build_tianditu_queries(region: str, site_name: str) -> List[str]:
    """构建天地图查询列表（从精确到模糊）。"""
    queries = []
    region = (region or "").strip()
    site = (site_name or "").strip()

    if region and site:
        queries.append(f"{region} {site}")
    if region:
        queries.append(region)
    if site and site != region:
        queries.append(f"中国 {site}")

    return queries


def _supplement_altitude_from_province(study: dict, region: str, site: str):
    """从省份查找表补充海拔。"""
    combined = f"{region or ''}{site or ''}"
    for province, (_, _, alt) in PROVINCE_CENTROIDS.items():
        if province in combined:
            study["altitude"] = alt
            return


def geocode_extractions(extractions: List[dict], geocoder: Geocoder) -> List[dict]:
    """遍历提取结果，为每个 study 填充 latitude, longitude, altitude。"""
    logger.info(f"Running geocoding on {len(extractions)} extraction results...")

    total_studies = 0
    geocoded = 0
    skipped = 0
    failed = 0

    for ext in extractions:
        data = ext.get("extraction")
        if not data:
            continue

        studies = data.get("studies", [])
        for study in studies:
            total_studies += 1
            lat = study.get("latitude")
            lon = study.get("longitude")

            if lat is not None and lon is not None:
                study["geo_source"] = "paper"
                skipped += 1
                if study.get("altitude") is None:
                    region = study.get("site_administrative_region", "") or ""
                    site = study.get("experimental_site_name", "") or ""
                    alt = geocoder._free_altitude(lat, lon)
                    if alt is not None:
                        study["altitude"] = alt
                    else:
                        _supplement_altitude_from_province(study, region, site)
                continue

            region = study.get("site_administrative_region", "") or ""
            site = study.get("experimental_site_name", "") or ""

            result = geocoder.geocode(region, site)
            if result:
                study["latitude"] = result.latitude
                study["longitude"] = result.longitude
                study["geo_source"] = result.source
                if result.altitude is not None and study.get("altitude") is None:
                    study["altitude"] = result.altitude
                if study.get("altitude") is None:
                    alt = geocoder._free_altitude(result.latitude, result.longitude)
                    if alt is not None:
                        study["altitude"] = alt
                if study.get("altitude") is None:
                    _supplement_altitude_from_province(study, region, site)
                geocoded += 1
                logger.debug(f"  [{study.get('study_title', '')[:40]}] Geocoded: {region or site} → ({result.latitude:.4f}, {result.longitude:.4f}) src={result.source}")
            else:
                study["geo_source"] = "unknown"
                failed += 1
                logger.warning(f"    Geocode FAILED: region='{region}', site='{site}'")

    logger.info(f"Geocoding complete: {geocoded} geocoded, {skipped} already filled, {failed} failed (total {total_studies} studies)")
    return extractions
