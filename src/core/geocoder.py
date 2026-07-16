"""
地理编码模块 — 根据 site_administrative_region 和 experimental_site_name
计算试验地点的 latitude, longitude, altitude。

策略（按优先级）：
  1. 内置中国农科院/省级农科院/农大查找表（覆盖主要农业试验点）
  2. OSM Nominatim 在线地理编码（免费、无需 API Key）
  3. 省会城市中心坐标兜底（确保有值可用）

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

logger = logging.getLogger("paper_extractor")


@dataclass
class GeoResult:
    """地理编码结果"""
    latitude: float
    longitude: float
    altitude: Optional[float] = None
    source: str = ""  # "lookup" | "nominatim" | "province_fallback"
    matched_name: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ── 省会城市中心坐标（最后兜底）────────────────────────────
PROVINCE_CENTROIDS = {
    "北京": (39.9042, 116.4074, 48.5),
    "天津": (39.0842, 117.2010, 3.0),
    "河北": (38.0428, 114.5149, 80.0),
    "山西": (37.8706, 112.5489, 800.0),
    "内蒙古": (40.8183, 111.6708, 1050.0),
    "辽宁": (41.8057, 123.4315, 49.0),
    "吉林": (43.8868, 125.3245, 220.0),
    "黑龙江": (45.7430, 126.6610, 150.0),
    "上海": (31.2304, 121.4737, 4.0),
    "江苏": (32.0603, 118.7969, 20.0),
    "浙江": (30.2741, 120.1551, 10.0),
    "安徽": (31.8612, 117.2830, 30.0),
    "福建": (26.0745, 119.2965, 10.0),
    "江西": (28.6765, 115.8924, 30.0),
    "山东": (36.6512, 117.1201, 50.0),
    "河南": (34.7466, 113.6254, 110.0),
    "湖北": (30.5928, 114.3055, 25.0),
    "湖南": (28.2280, 112.9388, 50.0),
    "广东": (23.1291, 113.2644, 10.0),
    "广西": (22.8170, 108.3665, 75.0),
    "海南": (20.0174, 110.3492, 15.0),
    "重庆": (29.5630, 106.5516, 240.0),
    "四川": (30.5728, 104.0668, 500.0),
    "贵州": (26.6470, 106.6302, 1100.0),
    "云南": (25.0389, 102.7183, 1900.0),
    "西藏": (29.6500, 91.1000, 3650.0),
    "陕西": (34.2658, 108.9541, 405.0),
    "甘肃": (36.0611, 103.8343, 1520.0),
    "青海": (36.6171, 101.7782, 2275.0),
    "宁夏": (38.4872, 106.2309, 1100.0),
    "新疆": (43.8256, 87.6168, 800.0),
}

# ── 主要农业科研机构查找表 ─────────────────────────────
AGRI_INSTITUTIONS = [
    # 国家级
    {"keywords": ["中国农业科学院", "中国农科院"], "lat": 39.9042, "lon": 116.3267, "alt": 48.5, "name": "中国农业科学院"},
    {"keywords": ["中国水稻研究所"], "lat": 30.2893, "lon": 120.1022, "alt": 10.0, "name": "中国水稻研究所(杭州)"},
    {"keywords": ["中国热带农业科学院"], "lat": 19.9925, "lon": 110.3490, "alt": 15.0, "name": "中国热带农业科学院"},
    # 省级农科院 (3 variants: 省农科院, 省农业科学院, 省+农业科学院)
    {"keywords": ["辽宁省农科院", "辽宁省农业科学院", "辽宁农业科学院"], "lat": 41.8106, "lon": 123.4293, "alt": 49.0, "name": "辽宁省农业科学院"},
    {"keywords": ["吉林省农科院", "吉林省农业科学院", "吉林农业科学院"], "lat": 43.8868, "lon": 125.3245, "alt": 220.0, "name": "吉林省农业科学院"},
    {"keywords": ["黑龙江省农科院", "黑龙江省农业科学院", "黑龙江农业科学院"], "lat": 45.7420, "lon": 126.6590, "alt": 150.0, "name": "黑龙江省农业科学院"},
    {"keywords": ["江苏省农科院", "江苏省农业科学院", "江苏农业科学院"], "lat": 32.0490, "lon": 118.8210, "alt": 20.0, "name": "江苏省农业科学院"},
    {"keywords": ["浙江省农科院", "浙江省农业科学院", "浙江农业科学院"], "lat": 30.2820, "lon": 120.1710, "alt": 10.0, "name": "浙江省农业科学院"},
    {"keywords": ["安徽省农科院", "安徽省农业科学院", "安徽农业科学院"], "lat": 31.8550, "lon": 117.2780, "alt": 30.0, "name": "安徽省农业科学院"},
    {"keywords": ["福建省农科院", "福建省农业科学院", "福建农业科学院"], "lat": 26.0800, "lon": 119.3020, "alt": 10.0, "name": "福建省农业科学院"},
    {"keywords": ["江西省农科院", "江西省农业科学院", "江西农业科学院"], "lat": 28.6700, "lon": 115.9300, "alt": 30.0, "name": "江西省农业科学院"},
    {"keywords": ["山东省农科院", "山东省农业科学院", "山东农业科学院"], "lat": 36.6580, "lon": 117.1280, "alt": 50.0, "name": "山东省农业科学院"},
    {"keywords": ["河南省农科院", "河南省农业科学院", "河南农业科学院"], "lat": 34.7540, "lon": 113.6330, "alt": 110.0, "name": "河南省农业科学院"},
    {"keywords": ["湖北省农科院", "湖北省农业科学院", "湖北农业科学院"], "lat": 30.5850, "lon": 114.3120, "alt": 25.0, "name": "湖北省农业科学院"},
    {"keywords": ["湖南省农科院", "湖南省农业科学院", "湖南农业科学院"], "lat": 28.2200, "lon": 112.9460, "alt": 50.0, "name": "湖南省农业科学院"},
    {"keywords": ["广东省农科院", "广东省农业科学院", "广东农业科学院"], "lat": 23.1350, "lon": 113.2700, "alt": 10.0, "name": "广东省农业科学院"},
    {"keywords": ["广西农科院", "广西农业科学院", "广西壮族自治区农业科学院"], "lat": 22.8100, "lon": 108.3600, "alt": 75.0, "name": "广西农业科学院"},
    {"keywords": ["四川省农科院", "四川省农业科学院", "四川农业科学院"], "lat": 30.6350, "lon": 104.0760, "alt": 500.0, "name": "四川省农业科学院"},
    {"keywords": ["贵州省农科院", "贵州省农业科学院", "贵州农业科学院"], "lat": 26.6540, "lon": 106.6250, "alt": 1100.0, "name": "贵州省农业科学院"},
    {"keywords": ["云南省农科院", "云南省农业科学院", "云南农业科学院"], "lat": 25.0450, "lon": 102.7120, "alt": 1900.0, "name": "云南省农业科学院"},
    {"keywords": ["陕西省农科院", "陕西省农业科学院", "陕西农业科学院"], "lat": 34.2600, "lon": 108.9490, "alt": 405.0, "name": "陕西省农业科学院"},
    {"keywords": ["甘肃省农科院", "甘肃省农业科学院", "甘肃农业科学院"], "lat": 36.0550, "lon": 103.8300, "alt": 1520.0, "name": "甘肃省农业科学院"},
    {"keywords": ["河北省农科院", "河北省农业科学院", "河北农业科学院"], "lat": 38.0370, "lon": 114.5090, "alt": 80.0, "name": "河北省农业科学院"},
    {"keywords": ["山西省农科院", "山西省农业科学院", "山西农业科学院"], "lat": 37.8650, "lon": 112.5440, "alt": 800.0, "name": "山西省农业科学院"},
    {"keywords": ["内蒙古农科院", "内蒙古农业科学院", "内蒙古自治区农业科学院"], "lat": 40.8120, "lon": 111.6660, "alt": 1050.0, "name": "内蒙古农业科学院"},
    # 主要农业大学
    {"keywords": ["中国农业大学"], "lat": 39.9870, "lon": 116.3540, "alt": 48.5, "name": "中国农业大学"},
    {"keywords": ["南京农业大学"], "lat": 32.0540, "lon": 118.8380, "alt": 20.0, "name": "南京农业大学"},
    {"keywords": ["华中农业大学"], "lat": 30.4760, "lon": 114.3560, "alt": 25.0, "name": "华中农业大学"},
    {"keywords": ["华南农业大学"], "lat": 23.1570, "lon": 113.3550, "alt": 10.0, "name": "华南农业大学"},
    {"keywords": ["四川农业大学"], "lat": 30.6950, "lon": 103.8600, "alt": 580.0, "name": "四川农业大学"},
    {"keywords": ["湖南农业大学"], "lat": 28.1830, "lon": 113.0830, "alt": 50.0, "name": "湖南农业大学"},
    {"keywords": ["沈阳农业大学"], "lat": 41.8260, "lon": 123.4580, "alt": 49.0, "name": "沈阳农业大学"},
    {"keywords": ["东北农业大学"], "lat": 45.7260, "lon": 126.7230, "alt": 150.0, "name": "东北农业大学"},
    {"keywords": ["西北农林科技大学"], "lat": 34.2890, "lon": 108.0720, "alt": 530.0, "name": "西北农林科技大学"},
    {"keywords": ["扬州大学"], "lat": 32.3940, "lon": 119.4150, "alt": 10.0, "name": "扬州大学"},
    {"keywords": ["河南农业大学"], "lat": 34.7580, "lon": 113.6720, "alt": 110.0, "name": "河南农业大学"},
    {"keywords": ["山东农业大学"], "lat": 36.1950, "lon": 117.1420, "alt": 130.0, "name": "山东农业大学"},
    {"keywords": ["安徽农业大学"], "lat": 31.8440, "lon": 117.2660, "alt": 30.0, "name": "安徽农业大学"},
    {"keywords": ["福建农林大学"], "lat": 26.0850, "lon": 119.2450, "alt": 10.0, "name": "福建农林大学"},
    {"keywords": ["江西农业大学"], "lat": 28.7560, "lon": 115.8380, "alt": 30.0, "name": "江西农业大学"},
    # 市级农科院
    {"keywords": ["武汉市农科院", "武汉市农业科学院"], "lat": 30.5700, "lon": 114.2900, "alt": 25.0, "name": "武汉市农业科学院"},
    {"keywords": ["成都市农科院", "成都市农林科学院"], "lat": 30.6450, "lon": 104.0600, "alt": 500.0, "name": "成都市农林科学院"},
    {"keywords": ["广州市农科院", "广州市农业科学院"], "lat": 23.1200, "lon": 113.2500, "alt": 10.0, "name": "广州市农业科学院"},
    {"keywords": ["南京市农科院", "南京市农业科学研究所"], "lat": 32.0500, "lon": 118.7900, "alt": 20.0, "name": "南京市农业科学研究所"},
    {"keywords": ["长沙市农科院", "长沙市农业科学院"], "lat": 28.2000, "lon": 112.9600, "alt": 50.0, "name": "长沙市农业科学院"},
]


class Geocoder:
    """地理编码器 — 多策略查找试验地点坐标和海拔。"""

    def __init__(self, config=None):
        self.config = config
        self._cache: dict = {}
        self._cache_file: Optional[Path] = None
        self._enabled = True
        self._use_nominatim = True

        if config:
            geo_cfg = getattr(config, "geocoding", None)
            if geo_cfg:
                self._enabled = getattr(geo_cfg, "enabled", True)
                self._use_nominatim = getattr(geo_cfg, "use_nominatim", True)
            if self._enabled:
                self._cache_file = config.cache_path / "geocoding_cache.json"
                self._load_cache()

    # ── 主入口 ───────────────────────────────────────────

    def geocode(
        self,
        region: Optional[str],
        site_name: Optional[str] = None,
    ) -> Optional[GeoResult]:
        """
        根据行政区划和试验站名称获取地理坐标。
        返回 GeoResult 或 None（全部策略均失败时）。
        """
        if not self._enabled:
            return None
        if not region and not site_name:
            return None

        # 构造缓存键
        cache_key = f"{region or ''}||{site_name or ''}"
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            return GeoResult(**cached) if cached else None

        result = None

        # 策略 1: 内置科研机构查找表
        result = self._lookup_table(region, site_name)

        # 策略 2: 百度地图 API
        if result is None:
            result = self._baidu_geocode(region, site_name)

        # 策略 3: Nominatim 在线查询
        if result is None:
            result = self._nominatim_geocode(region, site_name)

        # 策略 4: 省会城市中心坐标兜底
        if result is None:
            result = self._province_fallback(region)

        # 缓存结果
        if self._cache is not None:
            self._cache[cache_key] = result.to_dict() if result else None
            self._save_cache()

        return result

    # ── 策略 1: 查找表 ──────────────────────────────────

    def _lookup_table(self, region: str, site_name: str) -> Optional[GeoResult]:
        """在内置查找表中匹配关键词。"""
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

    # ── 策略 2: 百度地图 ─────────────────────────────────

    def _baidu_geocode(self, region: str, site_name: str) -> Optional[GeoResult]:
        """使用百度地图地理编码 API。需要 API Key。"""
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
            resp = httpx.get(
                "https://api.map.baidu.com/geocoding/v3/",
                params={
                    "address": address,
                    "output": "json",
                    "ak": api_key,
                },
                timeout=8,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == 0:
                    result = data.get("result", {})
                    location = result.get("location", {})
                    lat = location.get("lat")
                    lon = location.get("lng")
                    if lat is not None and lon is not None:
                        logger.info(
                            f"    Baidu: {address} → ({lat:.4f}, {lon:.4f})"
                        )
                        return GeoResult(
                            latitude=lat,
                            longitude=lon,
                            altitude=None,
                            source="baidu",
                            matched_name=address,
                        )
        except Exception as e:
            logger.warning(f"    Baidu geocode error for '{address}': {e}")

        return None

    # ── 策略 3: Nominatim ────────────────────────────────

    def _nominatim_geocode(self, region: str, site_name: str) -> Optional[GeoResult]:
        """使用 OSM Nominatim 免费地理编码 API。"""
        if not self._use_nominatim:
            return None

        try:
            import httpx
        except ImportError:
            logger.warning("    httpx not available, skipping Nominatim geocoding")
            return None

        queries = _build_queries(region, site_name)

        for query in queries:
            try:
                resp = httpx.get(
                    "https://nominatim.openstreetmap.org/search",
                    params={
                        "q": query,
                        "format": "jsonv2",
                        "limit": 1,
                        "countrycodes": "cn",
                    },
                    headers={"User-Agent": "PaperExtractor/1.0 (research)"},
                    timeout=8,
                )
                time.sleep(1.1)  # Nominatim: 1 req/s rate limit

                if resp.status_code == 200:
                    data = resp.json()
                    if data and isinstance(data, list) and len(data) > 0:
                        item = data[0]
                        lat = float(item["lat"])
                        lon = float(item["lon"])
                        alt = _extract_altitude(item)
                        display = item.get("display_name", "")
                        logger.info(
                            f"    Nominatim: {query} → ({lat:.4f}, {lon:.4f})"
                        )
                        return GeoResult(
                            latitude=lat,
                            longitude=lon,
                            altitude=alt,
                            source="nominatim",
                            matched_name=display[:80],
                        )
            except Exception as e:
                logger.warning(f"    Nominatim error for '{query}': {e}")
                continue

        return None

    # ── 策略 3: 省会兜底 ─────────────────────────────────

    def _province_fallback(self, region: str) -> Optional[GeoResult]:
        """从 region 中提取省份名，返回省会中心坐标。"""
        if not region:
            return None
        for province, (lat, lon, alt) in PROVINCE_CENTROIDS.items():
            if province in region:
                logger.info(f"    Province fallback: {province}")
                return GeoResult(
                    latitude=lat,
                    longitude=lon,
                    altitude=alt,
                    source="province_fallback",
                    matched_name=province,
                )
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


# ── 工具函数 ──────────────────────────────────────────────

def _build_queries(region: str, site_name: str) -> List[str]:
    """构建 Nominatim 查询列表（从精确到模糊）。"""
    queries = []
    clean_region = _clean_region(region) if region else ""
    clean_site = (site_name or "").strip()

    # 优先用短的地区名（快速匹配，避免长查询超时）
    if clean_region:
        queries.append(clean_region)
    if clean_region and clean_site:
        queries.append(f"{clean_region} {clean_site}")
    if clean_site and clean_site != clean_region:
        queries.append(f"中国 {clean_site}")

    return queries


def _clean_region(region: str) -> str:
    """清理行政区划名称，去掉冗余后缀。"""
    if not region:
        return ""
    cleaned = region.strip()
    for suffix in ["地区", "自治州", "自治县", "自治区"]:
        if cleaned.endswith(suffix) and len(cleaned) > 4:
            cleaned = cleaned[: -len(suffix)]
    return cleaned


def _extract_altitude(nominatim_result: dict) -> Optional[float]:
    """尝试从 Nominatim 结果中提取海拔。"""
    extra = nominatim_result.get("extratags", {})
    if isinstance(extra, dict):
        for key in ("ele", "altitude", "ele:local"):
            val = extra.get(key)
            if val:
                try:
                    return float(str(val).replace("m", "").replace(",", ".").strip())
                except ValueError:
                    continue
    return None


# ── Pipeline 集成函数 ────────────────────────────────────

def _supplement_altitude_from_province(study: dict, region: str, site: str):
    """
    从省份查找表补充海拔。

    当 geocoding 成功（如百度返回了经纬度）但海拔为空时，
    尝试从 region 或 site 中匹配省份名称，用省会海拔作为近似值。
    """
    combined = f"{region or ''}{site or ''}"
    for province, (_, _, alt) in PROVINCE_CENTROIDS.items():
        if province in combined:
            study["altitude"] = alt
            return


def geocode_extractions(extractions: List[dict], geocoder: Geocoder) -> List[dict]:
    """
    遍历提取结果，为每个 study 填充 latitude, longitude, altitude。
    原地修改 extractions 列表中的 extraction dict。
    """
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
                # LLM 从论文中提取了坐标
                study["geo_source"] = "paper"
                skipped += 1
                continue

            region = study.get("site_administrative_region", "")
            site = study.get("experimental_site_name", "")

            result = geocoder.geocode(region, site)
            if result:
                study["latitude"] = result.latitude
                study["longitude"] = result.longitude
                study["geo_source"] = result.source
                if result.altitude is not None and study.get("altitude") is None:
                    study["altitude"] = result.altitude
                # 补充海拔：如果 geocoding 成功但海拔为空，从省份查找表补充
                if study.get("altitude") is None:
                    _supplement_altitude_from_province(study, region, site)
                geocoded += 1
            else:
                study["geo_source"] = "unknown"
                failed += 1
                logger.warning(
                    f"    Geocoding failed: region='{region}', site='{site}'"
                )

    logger.info(
        f"Geocoding complete: {geocoded} geocoded, "
        f"{skipped} already filled, {failed} failed "
        f"(total {total_studies} studies)"
    )
    return extractions
