"""
地理编码模块 — 根据 site_administrative_region 和 experimental_site_name
计算试验地点的 latitude, longitude, altitude。

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

        if config:
            geo_cfg = getattr(config, "geocoding", None)
            if geo_cfg:
                self._enabled = getattr(geo_cfg, "enabled", True)
                # 天地图配置
                self._use_tianditu = getattr(geo_cfg, "use_tianditu", True)
                self._tianditu_tk = getattr(geo_cfg, "tianditu_tk", "")
                self._tianditu_delay = getattr(geo_cfg, "tianditu_delay", 0.2)
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

        # 策略 2: 天地图地理编码（国内服务，中文解析质量高，推荐）
        if result is None:
            result = self._tianditu_geocode(region, site_name)

        # 策略 3: 百度地图 API
        if result is None:
            result = self._baidu_geocode(region, site_name)

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

    # ── 策略 2: 天地图地理编码 ─────────────────────────────

    def _tianditu_geocode(self, region: str, site_name: str) -> Optional[GeoResult]:
        """使用天地图地理编码接口（已验证：status='0', location.lon/lat/level）。"""
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
                resp = httpx.get(
                    "https://api.tianditu.gov.cn/geocoder",
                    params={"ds": ds, "tk": self._tianditu_tk},
                    timeout=8,
                )
                time.sleep(self._tianditu_delay)

                if resp.status_code == 200:
                    data = resp.json()
                    logger.info(f"    [TIANDITU_RAW] query='{query}' → {json.dumps(data, ensure_ascii=False)[:500]}")

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

                    return GeoResult(
                        latitude=lat_f,
                        longitude=lon_f,
                        altitude=None,          # 地理编码接口不返回海拔，由 _tianditu_altitude 补充
                        source="tianditu",
                        matched_name=query,
                    )
            except Exception as e:
                logger.warning(f"    Tianditu geocode error for '{query}': {e}")
                continue

        return None

    def _free_altitude(self, lat: float, lon: float) -> Optional[float]:
        """使用 Open-Meteo Elevation API 查询海拔（已验证：返回 {"elevation": [数值]}）。"""
        try:
            import httpx
        except ImportError:
            return None

        # 指数退避重试：最多 3 次，初始间隔 1s
        max_retries = 3
        base_delay = 1.0
        timeout = 20.0

        for attempt in range(1, max_retries + 1):
            try:
                resp = httpx.get(
                    "https://api.open-meteo.com/v1/elevation",
                    params={"latitude": lat, "longitude": lon},
                    timeout=timeout,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    logger.info(f"    [ALT_RAW] lat={lat},lon={lon} → {json.dumps(data, ensure_ascii=False)[:500]}")

                    elevation = data.get("elevation")
                    if isinstance(elevation, list) and len(elevation) > 0:
                        alt_f = float(elevation[0])
                        logger.info(f"    Open-Meteo altitude: ({lat:.4f},{lon:.4f}) → {alt_f:.1f}m")
                        return alt_f
                    if isinstance(elevation, (int, float)):
                        alt_f = float(elevation)
                        logger.info(f"    Open-Meteo altitude: ({lat:.4f},{lon:.4f}) → {alt_f:.1f}m")
                        return alt_f
                    # 字段缺失或格式不符，不重试，直接走兜底
                    logger.warning(f"    Open-Meteo altitude: unexpected response format for ({lat},{lon})")
                    return None

                # 非 200，记录后重试
                logger.warning(f"    Open-Meteo altitude attempt {attempt}/{max_retries} failed: HTTP {resp.status_code} for ({lat},{lon})")

            except Exception as e:
                logger.warning(f"    Open-Meteo altitude attempt {attempt}/{max_retries} error for ({lat},{lon}): {e}")

            # 最后一次不重试
            if attempt < max_retries:
                delay = base_delay * (2 ** (attempt - 1))
                time.sleep(delay)

        return None

    # ── 策略 3: 百度地图 ─────────────────────────────────

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
                # 补充海拔（优先级：geocode 结果 > 免费海拔 API > 省会海拔）
                if result.altitude is not None and study.get("altitude") is None:
                    study["altitude"] = result.altitude
                if study.get("altitude") is None:
                    alt = geocoder._free_altitude(result.latitude, result.longitude)
                    if alt is not None:
                        study["altitude"] = alt
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

if __name__ == "__main__":
    import sys

    # 快速自测：验证天地图地理编码 + Open-Meteo 海拔接口
    # 用法: python -m src.core.geocoder [tk]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    from src.config import AppConfig
    config = AppConfig()

    tk = sys.argv[1] if len(sys.argv) > 1 else ""
    if not tk:
        import yaml
        cfg_path = Path("config.yaml")
        if cfg_path.exists():
            with open(cfg_path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            tk = (raw.get("geocoding", {}) or {}).get("tianditu_tk", "")
    if tk:
        config.geocoding.tianditu_tk = tk
    else:
        print("[WARN] 未提供天地图 tk，跳过天地图地理编码测试")

    geocoder = Geocoder(config)

    print("\n=== 测试 Open-Meteo 海拔 ===")
    alt = geocoder._free_altitude(45.32073, 127.3898)
    print(f"Open-Meteo 海拔(45.32073, 127.3898) = {alt}")

    print("\n=== 测试天地图地理编码 ===")
    result = geocoder.geocode("海南省三亚市", "崖州湾国家实验室1号试验田")
    if result:
        print(f"天地图地理编码: lat={result.latitude}, lon={result.longitude}, source={result.source}")
    else:
        print("天地图地理编码: 失败")

    print("\n=== 测试完整流程 ===")
    study = {"site_administrative_region": "浙江省杭州市", "experimental_site_name": "西湖区"}
    geocode_extractions([{"extraction": {"studies": [study]}}], geocoder)
    print(f"完整流程: lat={study.get('latitude')}, lon={study.get('longitude')}, alt={study.get('altitude')}, source={study.get('geo_source')}")
