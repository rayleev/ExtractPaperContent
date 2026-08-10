"""
Pydantic 数据模型 — 论文结构化提取的三级层次定义。
Paper → Study → VarietyYield

用法:
  from src.core.models import ExtractionResult, PaperInfo, StudyInfo, VarietyYield
  
  # 从 LLM JSON 输出解析
  result = ExtractionResult.model_validate(llm_json_dict)
  
  # 生成 prompt 中的 JSON schema
  schema_str = ExtractionResult.to_prompt_schema()
  
  # 扁平化为 CSV 行
  rows = result.to_flat_csv_rows(paper_id="xxx")
"""

from __future__ import annotations
from enum import Enum
from typing import Optional, List
from pydantic import BaseModel, Field, model_validator, field_validator, ConfigDict
import json
import re


class ExperimentalDesignType(str, Enum):
    RCBD = "RCBD"
    SPLIT_PLOT = "Split-plot"
    CRD = "CRD"
    UNKNOWN = "Unknown"


class YieldValueType(str, Enum):
    PLOT_MEAN = "plot_mean"
    SINGLE_REPLICATE = "single_replicate"
    CONVERTED = "converted"


class ConfidenceLevel(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class PaperInfo(BaseModel):
    """论文级别元数据"""
    model_config = ConfigDict(extra="ignore")
    paper_doi: Optional[str] = Field(None, description="论文DOI")
    paper_title: Optional[str] = Field(None, description="[REQUIRED] 论文完整标题")
    publication_year: Optional[int] = Field(None, description="[REQUIRED] 发表年份，整数")
    journal_name: Optional[str] = Field(None, description="期刊/来源名称")
    crop_species: Optional[str] = Field(None, description="[REQUIRED] 作物物种，如 '水稻 / Rice'、'玉米 / Maize'")
    data_file_link: Optional[str] = Field(None, description="公共数据库中的数据文件链接（可选，中文论文通常无）")
    data_file_description: Optional[str] = Field(None, description="数据文件格式说明（可选）")
    data_file_version: Optional[str] = Field(None, description="数据集版本号（可选）")


class VarietyYield(BaseModel):
    """品种产量记录 — 每个品种在某个试验中的一条记录"""
    model_config = ConfigDict(extra="ignore")  # 忽略LLM多输出的字段（如yield_standard_value）
    variety_name: Optional[str] = Field(None, description="[REQUIRED] 品种/品系完整名称（含编号，如 '辽梗375'、'郑单958'、'川优6203'）")
    variety_code: Optional[str] = Field(None, description="品种审定编号（最可靠的跨文献实体解析键，如有）")
    is_check_variety: Optional[bool] = Field(None, description="[REQUIRED] 是否为对照(CK)品种，true/false")
    variety_source: Optional[str] = Field(None, description="育种单位/来源")
    yield_raw_value: Optional[float] = Field(None, description="[REQUIRED] 论文中的原始产量数值，如 612.5。只提取数值，不做换算")
    yield_raw_unit: Optional[str] = Field(None, description="[REQUIRED] 原始产量单位，如 kg/亩、kg/ha、t/ha。只提取原文单位")
    yield_standard_value: Optional[float] = Field(None, description="[PROGRAM] 由程序换算的kg/ha值，LLM不要填")
    yield_standard_unit: str = Field("kg/ha", description="[PROGRAM] 标准单位，固定为kg/ha，LLM不要填")
    yield_value_type: Optional[YieldValueType] = Field(None, description="[REQUIRED] 产量值的统计类型：<plot_mean|single_replicate|converted>")
    significance_group: Optional[str] = Field(None, description="显著性字母标记，如 a/b/ab")
    pct_over_check: Optional[float] = Field(None, description="相对对照品种的增产/减产百分比（如 8.3 表示增产8.3%）")
    measurement_method: Optional[str] = Field(None, description="产量测定与计产方法，如 '小区单打单收单计产，晒干扬净后称重'")
    source_location: Optional[str] = Field(None, description="[REQUIRED] 数据来源位置：论文中的具体表格或段落，如 '表5'、'Table 3'、'2.3节'")
    confidence_level: Optional[ConfidenceLevel] = Field(None, description="[REQUIRED] 本条记录的整体提取置信度：<high|medium|low>")
    treatment_name: Optional[str] = Field(None, description="[OPTIONAL] 处理名称（management_yield 类别或肥料试验时填写），如 'N0'/'N180'/'N240'/'CK'/'常规灌溉'/'高密度'；品种比较试验无处理时填 null")
    n_raw_value: Optional[float] = Field(None, description="[OPTIONAL] 该处理的纯氮(N)施用量数值，如 180。只抄录论文明确写出的纯养分量，复合肥只给总量不给养分含量时填 null")
    n_raw_unit: Optional[str] = Field(None, description="[OPTIONAL] 氮施用量原始单位，如 'kg/ha'、'kg/hm²'、'kg/亩'")
    p_raw_value: Optional[float] = Field(None, description="[OPTIONAL] 该处理的纯磷(P2O5)施用量数值。只抄录论文明确写出的纯养分量")
    p_raw_unit: Optional[str] = Field(None, description="[OPTIONAL] 磷施用量原始单位")
    k_raw_value: Optional[float] = Field(None, description="[OPTIONAL] 该处理的纯钾(K2O)施用量数值。只抄录论文明确写出的纯养分量")
    k_raw_unit: Optional[str] = Field(None, description="[OPTIONAL] 钾施用量原始单位")
    nutrient_source_location: Optional[str] = Field(None, description="[OPTIONAL] 氮磷钾施用量数据的来源位置，如 '表2'、'表3-N180行'、'材料方法'")
    n_standard_value: Optional[float] = Field(None, description="[PROGRAM] 由程序换算的 kg N/亩 值，LLM不要填")
    p_standard_value: Optional[float] = Field(None, description="[PROGRAM] 由程序换算的 kg P2O5/亩 值，LLM不要填")
    k_standard_value: Optional[float] = Field(None, description="[PROGRAM] 由程序换算的 kg K2O/亩 值，LLM不要填")
    n_standard_unit: Optional[str] = Field(None, description="[PROGRAM] 程序自动填充 'kg/亩'")
    p_standard_unit: Optional[str] = Field(None, description="[PROGRAM] 程序自动填充 'kg/亩'")
    k_standard_unit: Optional[str] = Field(None, description="[PROGRAM] 程序自动填充 'kg/亩'")

    @field_validator("pct_over_check", mode="before")
    @classmethod
    def _parse_pct_over_check(cls, v):
        """处理 pct_over_check 的非标准格式，如 '8.48(13.88)' 或 '-12.5(-8.14)'。"""
        if v is None:
            return v
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            # 提取第一个数值（含负号），忽略括号内的第二个数值
            m = re.match(r'^([+-]?\d+\.?\d*)', v.strip())
            if m:
                return float(m.group(1))
        return None


def _coerce_geo_float(value):
    """
    将 LLM 可能输出的经纬度/海拔值稳健地转换为 float。

    LLM 有时会按 prompt 示例输出带格式的字符串（如 '北纬30.5°'、'东经114.3°'、
    '30°N'、'约30.5'、'25m'、'1,234'），直接交给 Optional[float] 会导致 Pydantic
    校验失败、进而使整篇论文回退甚至崩溃。这里统一解析为 float，解析不了返回 None
    （交给 geocoder 后处理兜底），而不是抛异常。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)

    s = str(value).strip()
    if not s:
        return None

    # 方向判负：南纬/西经，或以 S/W 结尾（中国境内均为正，仅作通用兜底）
    negative = ("南纬" in s) or ("西经" in s) or re.search(r"[SsWw]\s*$", s) is not None

    # 去除中文方向词、度分秒符号、单位、近似词、千分位与空白
    cleaned = s
    for token in ("北纬", "南纬", "东经", "西经", "海拔", "约", "近似",
                  "°", "º", "度", "′", "″", "'", '"',
                  "N", "E", "S", "W", "n", "e", "s", "w",
                  "米", "m", "M", ",", "，", " ", "\u3000"):
        cleaned = cleaned.replace(token, "")

    m = re.search(r"\d+(?:\.\d+)?", cleaned)
    if not m:
        return None
    try:
        num = float(m.group())
    except ValueError:
        return None
    return -num if negative else num


class StudyInfo(BaseModel):
    """试验级别信息 — 一篇论文可包含多个试验"""
    model_config = ConfigDict(extra="ignore")
    study_title: Optional[str] = Field(None, description="[REQUIRED] 试验名称/标题")
    study_description: Optional[str] = Field(None, description="试验简述（1-2句话概括）")
    trial_year: Optional[str] = Field(None, description="[REQUIRED] 试验实施的年份或期间，如 '2022' 或 '2022-2025'")
    sowing_date: Optional[str] = Field(None, description="播种日期（ISO 8601 格式，如 2022-04-15）")
    harvest_date: Optional[str] = Field(None, description="收获日期（ISO 8601 格式，如 2022-10-08）")
    country: Optional[str] = Field(None, description="[REQUIRED] 试验所在国家（ISO 3166 代码），如 CN")
    site_administrative_region: Optional[str] = Field(None, description="[REQUIRED] 试验点行政区划（省/市/县），如 '四川省广汉市'")
    experimental_site_name: Optional[str] = Field(None, description="试验站/试验地点名称")
    latitude: Optional[float] = Field(None, description="[OPTIONAL] 纬度（仅当论文中明确写出时才填，否则留空由程序计算）")
    longitude: Optional[float] = Field(None, description="[OPTIONAL] 经度（仅当论文中明确写出时才填，否则留空由程序计算）")
    altitude: Optional[float] = Field(None, description="[OPTIONAL] 海拔/米（仅当论文中明确写出时才填，否则留空由程序计算）")
    geo_source: Optional[str] = Field(None, description="[DO NOT FILL] 坐标来源，系统自动标注: paper/lookup/baidu/province_fallback")
    replication_number: Optional[int] = Field(None, description="田间试验重复次数")
    plot_size: Optional[str] = Field(None, description="小区面积，如 '13.3 m²'")
    planting_density: Optional[str] = Field(None, description="种植密度，如 '22.5万穴/公顷'")
    experimental_design_description: Optional[str] = Field(None, description="[REQUIRED] 试验设计的文字描述")
    experimental_design_type: Optional[ExperimentalDesignType] = Field(None, description="试验设计类型：<RCBD|Split-plot|CRD|Unknown>")
    growth_facility_description: Optional[str] = Field(None, description="试验环境描述，如 '大田环境'、'温室'")
    cultural_practices: Optional[str] = Field(None, description="栽培管理措施描述")
    notes: Optional[str] = Field(None, description="[DO NOT FILL] 备注，系统自动生成的数据质量警告（如多站点标记），LLM无需填写")
    varieties: List[VarietyYield] = Field(default_factory=list, description="该试验中所有品种的产量记录")

    @field_validator("latitude", "longitude", "altitude", mode="before")
    @classmethod
    def _coerce_geo_fields(cls, v):
        """经纬度/海拔容错：把 '北纬30.5°' 之类的字符串解析为 float，解析不了置 None。"""
        return _coerce_geo_float(v)


class ExtractionResult(BaseModel):
    """完整提取结果 — 一篇论文的顶层容器"""
    paper: PaperInfo = Field(description="论文级别元数据")
    studies: List[StudyInfo] = Field(default_factory=list, description="论文中包含的所有试验")

    @model_validator(mode="before")
    @classmethod
    def normalize_paper_field(cls, data):
        """
        预处理：如果 LLM 没有把论文元数据包裹在 'paper' 键下，
        而是直接放在顶层，则自动提取并构造 paper 对象。
        """
        if isinstance(data, dict) and "paper" not in data:
            paper_keys = {
                "paper_doi", "paper_title", "publication_year",
                "journal_name", "crop_species",
                "data_file_link", "data_file_description", "data_file_version",
                "title", "doi", "year", "journal",
            }
            paper_dict = {}
            remaining = {}
            for k, v in data.items():
                if k in paper_keys:
                    key_map = {
                        "title": "paper_title",
                        "doi": "paper_doi",
                        "year": "publication_year",
                        "journal": "journal_name",
                    }
                    mapped_key = key_map.get(k, k)
                    paper_dict[mapped_key] = v
                else:
                    remaining[k] = v
            if paper_dict:
                remaining["paper"] = paper_dict
                return remaining
        return data

    @classmethod
    def to_prompt_schema(cls) -> str:
        """生成适合嵌入 LLM prompt 的 JSON 示例骨架。"""
        schema = cls.model_json_schema()
        example = _build_example_with_descriptions(schema)
        return json.dumps(example, ensure_ascii=False, indent=2)

    def to_flat_csv_rows(self, paper_id: str = "") -> list[dict]:
        """将层次结构扁平化为 CSV 行列表，每个品种一条记录。

        ID 层级结构:
          paper_id:  P20260715_001           (系统生成)
          study_id:  P20260715_001-S01       (paper_id + 试验序号)
          record_id: P20260715_001-S01-R001  (study_id + 品种序号)
        """
        rows = []
        for si, study in enumerate(self.studies):
            study_id = f"{paper_id}-S{si+1:02d}"
            for ri, variety in enumerate(study.varieties):
                record_id = f"{study_id}-R{ri+1:03d}"
                row = {
                    "record_id": record_id,
                    "paper_id": paper_id,
                    "paper_doi": self.paper.paper_doi or "",
                    "paper_title": self.paper.paper_title or "",
                    "publication_year": self.paper.publication_year or "",
                    "journal_name": self.paper.journal_name or "",
                    "crop_species": self.paper.crop_species or "",
                    "study_id": study_id,
                    "study_title": study.study_title or "",
                    "study_description": study.study_description or "",
                    "trial_year": study.trial_year or "",
                    "sowing_date": study.sowing_date or "",
                    "harvest_date": study.harvest_date or "",
                    "country": study.country or "",
                    "site_administrative_region": study.site_administrative_region or "",
                    "experimental_site_name": study.experimental_site_name or "",
                    "latitude": study.latitude or "",
                    "longitude": study.longitude or "",
                    "altitude": study.altitude or "",
                    "geo_source": study.geo_source or "",
                    "replication_number": study.replication_number or "",
                    "plot_size": study.plot_size or "",
                    "planting_density": study.planting_density or "",
                    "experimental_design_type": study.experimental_design_type.value if study.experimental_design_type else "",
                    "experimental_design_description": study.experimental_design_description or "",
                    "growth_facility_description": study.growth_facility_description or "",
                    "cultural_practices": study.cultural_practices or "",
                    "variety_name": variety.variety_name or "",
                    "variety_code": variety.variety_code or "",
                    "is_check_variety": variety.is_check_variety if variety.is_check_variety is not None else "",
                    "variety_source": variety.variety_source or "",
                    "yield_raw_value": variety.yield_raw_value if variety.yield_raw_value is not None else "",
                    "yield_raw_unit": variety.yield_raw_unit or "",
                    "yield_standard_value": variety.yield_standard_value if variety.yield_standard_value is not None else "",
                    "yield_standard_unit": variety.yield_standard_unit,
                    "yield_value_type": variety.yield_value_type.value if variety.yield_value_type else "",
                    "significance_group": variety.significance_group or "",
                    "pct_over_check": variety.pct_over_check if variety.pct_over_check is not None else "",
                    "measurement_method": variety.measurement_method or "",
                    "source_location": variety.source_location or "",
                    "confidence_level": variety.confidence_level.value if variety.confidence_level else "",
                    "treatment_name": variety.treatment_name or "",
                    "n_raw_value": variety.n_raw_value if variety.n_raw_value is not None else "",
                    "n_raw_unit": variety.n_raw_unit or "",
                    "p_raw_value": variety.p_raw_value if variety.p_raw_value is not None else "",
                    "p_raw_unit": variety.p_raw_unit or "",
                    "k_raw_value": variety.k_raw_value if variety.k_raw_value is not None else "",
                    "k_raw_unit": variety.k_raw_unit or "",
                    "nutrient_source_location": variety.nutrient_source_location or "",
                    "n_standard_value": variety.n_standard_value if variety.n_standard_value is not None else "",
                    "p_standard_value": variety.p_standard_value if variety.p_standard_value is not None else "",
                    "k_standard_value": variety.k_standard_value if variety.k_standard_value is not None else "",
                }
                rows.append(row)
        return rows

    def compute_standard_yields(self, config=None):
        """后处理：根据 yield_raw_value + yield_raw_unit 程序化换算 yield_standard_value。

        Args:
            config: AppConfig 实例，用于读取单位换算表。为 None 时使用默认换算表。
        """
        # 从 config 获取换算表，或使用默认值
        if config is not None:
            mass_to_kg = config.unit_conversion.mass_to_kg
            area_to_ha = config.unit_conversion.area_to_ha
        else:
            # 默认换算表（向后兼容）
            mass_to_kg = {
                "g": 0.001, "kg": 1.0, "t": 1000.0, "ton": 1000.0, "tonne": 1000.0,
                "mg": 1e-6, "Mg": 1000.0,
                "斤": 0.5, "公斤": 1.0, "lb": 0.453592,
            }
            area_to_ha = {
                "m2": 0.0001, "平方米": 0.0001,
                "ha": 1.0, "hm2": 1.0, "公顷": 1.0,
                "亩": 1.0 / 15.0, "mu": 1.0 / 15.0, "667m2": 1.0 / 15.0,
                "acre": 0.404686,
            }

        context_plot = {"plot", "小区"}
        context_plant = {"plant", "株", "pot", "盆", "ear", "穗", "hill", "穴", "棵"}

        for study in self.studies:
            for v in study.varieties:
                if v.yield_raw_value is not None and v.yield_raw_unit:
                    v.yield_standard_value = _convert_yield(
                        v.yield_raw_value, v.yield_raw_unit,
                        mass_to_kg, area_to_ha, context_plot, context_plant,
                        plot_size=study.plot_size or "",
                        planting_density=study.planting_density or "",
                    )
                    v.yield_standard_unit = "kg/ha"

    def compute_standard_nutrients(self, config=None):
        """后处理：根据 n/p/k_raw_value + n/p/k_raw_unit 程序化换算 n/p/k_standard_value。

        换算目标单位：kg/亩

        Args:
            config: AppConfig 实例，用于读取单位换算表。为 None 时使用默认换算表。
        """
        # 从 config 获取换算表，或使用默认值
        if config is not None:
            mass_to_kg = config.unit_conversion.mass_to_kg
            area_to_ha = config.unit_conversion.area_to_ha
        else:
            mass_to_kg = {
                "g": 0.001, "kg": 1.0, "t": 1000.0, "ton": 1000.0, "tonne": 1000.0,
                "mg": 1e-6, "Mg": 1000.0,
                "斤": 0.5, "公斤": 1.0, "lb": 0.453592,
            }
            area_to_ha = {
                "m2": 0.0001, "平方米": 0.0001,
                "ha": 1.0, "hm2": 1.0, "公顷": 1.0,
                "亩": 1.0 / 15.0, "mu": 1.0 / 15.0, "667m2": 1.0 / 15.0,
                "acre": 0.404686,
            }

        context_plot = {"plot", "小区"}
        context_plant = {"plant", "株", "pot", "盆", "ear", "穗", "hill", "穴", "棵"}

        for study in self.studies:
            for v in study.varieties:
                # 氮 N
                if v.n_raw_value is not None and v.n_raw_unit:
                    kg_per_ha = _convert_yield(
                        v.n_raw_value, v.n_raw_unit,
                        mass_to_kg, area_to_ha, context_plot, context_plant,
                        plot_size=study.plot_size or "",
                        planting_density=study.planting_density or "",
                    )
                    if kg_per_ha is not None:
                        v.n_standard_value = round(kg_per_ha / 15.0, 2)
                        v.n_standard_unit = "kg/亩"

                # 磷 P
                if v.p_raw_value is not None and v.p_raw_unit:
                    kg_per_ha = _convert_yield(
                        v.p_raw_value, v.p_raw_unit,
                        mass_to_kg, area_to_ha, context_plot, context_plant,
                        plot_size=study.plot_size or "",
                        planting_density=study.planting_density or "",
                    )
                    if kg_per_ha is not None:
                        v.p_standard_value = round(kg_per_ha / 15.0, 2)
                        v.p_standard_unit = "kg/亩"

                # 钾 K
                if v.k_raw_value is not None and v.k_raw_unit:
                    kg_per_ha = _convert_yield(
                        v.k_raw_value, v.k_raw_unit,
                        mass_to_kg, area_to_ha, context_plot, context_plant,
                        plot_size=study.plot_size or "",
                        planting_density=study.planting_density or "",
                    )
                    if kg_per_ha is not None:
                        v.k_standard_value = round(kg_per_ha / 15.0, 2)
                        v.k_standard_unit = "kg/亩"


def _normalize_unit(unit: str) -> str:
    """
    归一化单位字符串，统一为 mass/area 格式。

    处理: kg·ha⁻¹ → kg/ha,  kg·hm⁻² → kg/hm2,  g m⁻² → g/m2,
          kg per ha → kg/ha,  Unicode 上标 → ASCII,
          t·hm-2 → t/hm2,  kg·ha-1 → kg/ha（兼容普通减号）,
          kg/667m2 → kg/667m2, kg/20m2 → kg/20m2（特定面积）
    """
    u = unit.strip()
    # ·Y⁻¹ / 空格Y⁻¹ → /Y （如 kg·ha⁻¹ → kg/ha）
    u = re.sub(r'[·⋅\s]+(\S+)⁻¹$', r'/\1', u)
    # ·Y⁻² / 空格Y⁻² → /Y2 （如 kg·hm⁻² → kg/hm2, g·m⁻² → g/m2）
    u = re.sub(r'[·⋅\s]+(\S+)⁻²$', r'/\g<1>2', u)
    # 兼容普通减号: ·Y-1 → /Y, ·Y-2 → /Y2 （如 t·hm-2 → t/hm2, kg·ha-1 → kg/ha）
    u = re.sub(r'[·⋅\s]+(\S+)-1$', r'/\1', u)
    u = re.sub(r'[·⋅\s]+(\S+)-2$', r'/\g<1>2', u)
    # 兼容 g·株-1 格式（中文单位）
    u = re.sub(r'[·⋅\s]+(\S+)-1$', r'/\1', u)
    # "per" 关键词 → /
    u = re.sub(r'\s+per\s+', '/', u, flags=re.IGNORECASE)
    # 剩余 Unicode 上标转 ASCII
    u = u.replace("²", "2").replace("³", "3")
    # 去空格
    u = u.replace(" ", "")
    return u


def _match_mass(s: str, mass_to_kg: dict) -> Optional[float]:
    """匹配质量单位，返回 → kg 的换算系数。支持中文数字前缀（万）。"""
    wan = 1.0
    if s.startswith("万"):
        wan = 10000.0
        s = s[1:]
    # 精确匹配（区分 Mg 和 mg）
    if s in mass_to_kg:
        return mass_to_kg[s] * wan
    # 忽略大小写（处理 KG, T 等变体，但排除 Mg/mg 混淆）
    sl = s.lower()
    if sl == "mg":
        return 1e-6 * wan
    for k, v in mass_to_kg.items():
        if k.lower() == sl:
            return v * wan
    return None


def _match_area(s: str, area_to_ha: dict) -> Optional[float]:
    """匹配面积单位，返回 → ha 的换算系数。"""
    if s in area_to_ha:
        return area_to_ha[s]
    sl = s.lower()
    for k, v in area_to_ha.items():
        if k.lower() == sl:
            return v
    return None


def _parse_plot_size_m2(s: str) -> Optional[float]:
    """从自由文本中提取小区面积，统一为 m²。

    支持格式:
      - 单值: '13.3 m²', '0.002 ha', '0.2 亩', '20 平米', '20m^2'
      - 长×宽: '长 5m、宽 2.66m', '5m×2.66m', '5 x 2.66 米'
    """
    if not s:
        return None
    s = s.strip()

    # ── 尝试长×宽格式 ──
    m = re.search(r'(?:长\s*)?(\d+\.?\d*)\s*(?:m|米)?\s*[×xX*，、]\s*(?:宽\s*)?(\d+\.?\d*)\s*(?:m|米)?', s)
    if m:
        length, width = float(m.group(1)), float(m.group(2))
        if length > 0 and width > 0:
            # 如果数值很大（如 30×12），可能是 cm 为单位的间距，不是长×宽
            if length > 100 and width > 100:
                pass  # 跳过，当作密度间距处理
            else:
                return round(length * width, 2)

    # ── 尝试单值格式 ──
    m = re.search(r'(\d+\.?\d*)\s*(m²|m2|m\^2|平方米|平米|ha|hm²|hm2|公顷|亩)', s)
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2)
    if unit in ("m²", "m2", "m^2", "平方米", "平米"):
        return val
    if unit in ("ha", "hm²", "hm2", "公顷"):
        return val * 10000
    if unit == "亩":
        return val * 666.67
    return None


def _parse_density_per_ha(s: str) -> Optional[float]:
    """从自由文本中提取种植密度，统一为 穴(株)/ha。

    支持格式:
      - 间距: '30×12 cm', '30 x 12 cm', '30*12cm'
      - 行距株距: '行距 30cm、株距 12cm', '株行距 30×12cm'
      - 密度: '22.5万穴/公顷', '30万株/hm²', '15000株/亩', '基本苗 150 万株/公顷'
    """
    if not s:
        return None
    s = s.strip()

    # ── 1. 行距株距格式（带中文前缀）──
    m = re.search(r'行\s*距\s*(\d+\.?\d*)\s*(?:cm|厘米)?[、,，\s]*株\s*距\s*(\d+\.?\d*)\s*(?:cm|厘米)?', s)
    if m:
        row, plant = float(m.group(1)), float(m.group(2))
        if row > 0 and plant > 0:
            area_per_plant_m2 = (row / 100.0) * (plant / 100.0)
            return round(10000.0 / area_per_plant_m2, 2)

    # ── 2. 株行距/行距×株距格式（带前缀）──
    m = re.search(r'(?:株\s*行\s*距|行\s*距\s*[×xX*]\s*株\s*距)\s*(\d+\.?\d*)\s*[×xX*]\s*(\d+\.?\d*)\s*(?:cm|厘米)?', s)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        if a > 0 and b > 0:
            area_per_plant_m2 = (a / 100.0) * (b / 100.0)
            return round(10000.0 / area_per_plant_m2, 2)

    # ── 3. 纯间距格式: 30×12 cm, 30 x 12 cm, 30*12cm ──
    m = re.search(r'(\d+\.?\d*)\s*[×xX*]\s*(\d+\.?\d*)\s*(cm|厘米|cm²)?', s)
    if m:
        a, b = float(m.group(1)), float(m.group(2))
        if a > 0 and b > 0:
            area_per_plant_m2 = (a / 100.0) * (b / 100.0)
            return round(10000.0 / area_per_plant_m2, 2)

    # ── 4. 密度格式: 22.5万穴/公顷, 15000株/亩, 基本苗 150 万株/公顷 ──
    m = re.search(r'(\d+\.?\d*)\s*(万)?\s*(穴|株|plant|hill|棵|苗)\s*/?\s*(公顷|ha|hm²|hm2|亩)?', s, re.IGNORECASE)
    if m:
        val = float(m.group(1))
        if m.group(2):  # "万" 前缀
            val *= 10000
        area_unit = m.group(4) or "公顷"  # 默认按公顷
        if area_unit in ("公顷", "ha", "hm²", "hm2"):
            return val
        if area_unit == "亩":
            return val * 15  # 穴/亩 → 穴/ha
    return None


def _convert_yield(
    value: float,
    unit: str,
    mass_to_kg: dict,
    area_to_ha: dict,
    context_plot: set,
    context_plant: set,
    plot_size: str = "",
    planting_density: str = "",
) -> Optional[float]:
    """
    将产量值换算为 kg/ha（组合式解析）。

    三层策略:
      1. 组合式解析: 拆 mass/area，各自查系数 → value × mass_factor / area_factor
         覆盖: kg/ha, t/ha, g/m², Mg·ha⁻¹, kg/亩, 斤/亩, kg/667m² 等所有标准组合
      2. 上下文辅助: per-plot/per-plant 单位 + plot_size/planting_density → 换算
         覆盖: kg/plot + plot_size="13.3 m²", g/株 + planting_density="22.5万穴/公顷"
      3. 不可转换: 返回 None（保留 raw 值，后续可标记）
    """
    u = _normalize_unit(unit)

    # ── 拆分 mass/area ──
    if "/" not in u:
        return None
    mass_str, area_str = u.split("/", 1)
    if not mass_str or not area_str:
        return None

    mass_factor = _match_mass(mass_str, mass_to_kg)
    if mass_factor is None:
        return None

    # ── 第 1 层: 标准面积单位 → 直接换算 ──
    area_factor = _match_area(area_str, area_to_ha)
    if area_factor is not None:
        return round(value * mass_factor / area_factor, 2)

    # ── 第 2 层: 非标准面积 → 上下文辅助 ──
    area_lower = area_str.lower()

    # kg/plot, g/小区 → 需要 plot_size
    if area_lower in context_plot:
        plot_m2 = _parse_plot_size_m2(plot_size)
        if plot_m2 and plot_m2 > 0:
            # value [mass/plot] × mass_factor [kg/mass] × (10000 m²/ha / plot_m2) [plot/ha]
            return round(value * mass_factor * (10000.0 / plot_m2), 2)

    # g/株, kg/plant, g/pot → 需要 planting_density
    if area_lower in context_plant:
        density = _parse_density_per_ha(planting_density)
        if density and density > 0:
            # value [mass/plant] × mass_factor [kg/mass] × density [plant/ha]
            return round(value * mass_factor * density, 2)

    # ── 第 3 层: 不可转换 ──
    return None


def _build_example_with_descriptions(schema: dict) -> dict:
    """从 JSON Schema 构建带字段描述注释的示例字典。"""
    defs = schema.get("$defs", {})
    defs_with_root = dict(defs)
    defs_with_root["ExtractionResult"] = {
        "properties": schema.get("properties", {}),
    }

    def _example_for_ref(ref: str) -> dict:
        model_name = ref.split("/")[-1]
        model_schema = defs_with_root.get(model_name, {})
        props = model_schema.get("properties", {})
        result = {}
        for field_name, field_info in props.items():
            if field_name == "varieties":
                result[field_name] = [_example_for_ref("#/$defs/VarietyYield")]
            elif field_name == "studies":
                result[field_name] = [_example_for_ref("#/$defs/StudyInfo")]
            elif field_name == "paper":
                result[field_name] = _example_for_ref("#/$defs/PaperInfo")
            else:
                desc = field_info.get("description", "")
                # 跳过 DO NOT FILL 和 PROGRAM 字段
                if "[DO NOT FILL]" in desc or "[PROGRAM]" in desc:
                    continue
                any_of = field_info.get("anyOf", [])
                enum_ref = None
                for opt in any_of:
                    if "$ref" in opt:
                        enum_ref = opt["$ref"]
                        break
                if "$ref" in field_info:
                    enum_ref = field_info["$ref"]

                if enum_ref:
                    enum_name = enum_ref.split("/")[-1]
                    enum_def = defs.get(enum_name, {})
                    enum_vals = enum_def.get("enum", [])
                    result[field_name] = f"<{'|'.join(str(v) for v in enum_vals)}>"
                else:
                    result[field_name] = desc or None
        return result

    return _example_for_ref("#/$defs/ExtractionResult")
