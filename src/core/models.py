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
                }
                rows.append(row)
        return rows

    def compute_standard_yields(self):
        """后处理：根据 yield_raw_value + yield_raw_unit 程序化换算 yield_standard_value。"""
        for study in self.studies:
            for v in study.varieties:
                if v.yield_raw_value is not None and v.yield_raw_unit:
                    v.yield_standard_value = _convert_yield(v.yield_raw_value, v.yield_raw_unit)
                    v.yield_standard_unit = "kg/ha"


def _convert_yield(value: float, unit: str) -> Optional[float]:
    """
    将产量值换算为 kg/ha。

    策略：将单位拆解为"质量/面积"两部分，分别换算后计算结果。
    支持的质量单位: kg, t(吨), g, 斤(=0.5kg)
    支持的面积单位: ha, hm²(=ha), 亩(mu, =1/15 ha), m², 667m²(≈1亩)
    无法可靠换算时返回 None（而非静默保留错误值）。
    """
    u = unit.strip().lower().replace(" ", "")

    # ── 精确匹配常见完整单位 ──
    exact_map = {
        # kg/ha 系列（1 ha = 1 hm²）
        "kg/ha": 1.0, "kg/hm²": 1.0, "kg/hm2": 1.0,
        "kg·hm⁻²": 1.0, "kg·hm-2": 1.0,
        # t/ha 系列
        "t/ha": 1000.0, "t/hm²": 1000.0, "t/hm2": 1000.0,
        "t·hm⁻²": 1000.0, "t·hm-2": 1000.0,
        "ton/ha": 1000.0, "tonne/ha": 1000.0,
        # g/ha 系列
        "g/ha": 0.001, "g/hm²": 0.001,
        # kg/亩 系列（1 ha = 15 亩 → 1 kg/亩 = 15 kg/ha）
        "kg/亩": 15.0, "kg/mu": 15.0,
        # 斤/亩（1 斤 = 0.5 kg → 0.5 × 15 = 7.5 kg/ha）
        "斤/亩": 7.5,
        # kg/667m²（≈ 1 亩 → 同 kg/亩）
        "kg/667m²": 15.0, "kg/667m2": 15.0,
    }

    if u in exact_map:
        return round(value * exact_map[u], 2)

    # ── 模糊匹配：包含关键词 ──
    # 亩/mu → × 15
    if "亩" in u or "/mu" in u:
        if "kg" in u or "公斤" in u:
            return round(value * 15, 2)
        if "斤" in u:
            return round(value * 7.5, 2)
        if u.startswith("t") or "吨" in u:
            return round(value * 15000, 2)  # t/亩 → t/ha × 15

    # hm² 或公顷 → 等同于 ha
    if "hm" in u or "公顷" in u:
        if "kg" in u or "公斤" in u:
            return round(value, 2)
        if u.startswith("t") or "吨" in u:
            return round(value * 1000, 2)
        if u.startswith("g"):
            return round(value * 0.001, 2)

    # 无法可靠换算的单位 — 返回 None 而非静默保留
    # 常见的不可换算单位: g/株, g/plant, kg/plot, g/pot 等
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
