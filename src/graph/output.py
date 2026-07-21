"""
输出模块 — PostgreSQL 统一存储 + CSV 导出 + 统计报告。

所有提取结果、分类结果、验证报告统一写入 PostgreSQL 数据库，
支持 1500 万级论文的大规模存储、多实例并发写入和下游对接。

数据库表结构：
  papers             论文级数据（一篇一行）
  studies            试验级数据（一篇论文多行）
  varieties          品种产量数据（主数据表，一行一个品种）
  varieties_flat     品种产量宽表（交接用）
  classification     论文分类结果
  validation_issues  验证问题明细（扁平化）
  paper_status       论文处理状态（兼任务协调注册表）
  pdf_missing        无法获取 PDF 的论文记录

用法：
  conn = init_database("postgresql://user:pass@host:5432/dbname")
  insert_extraction(conn, result, paper_id)   # 逐篇追加
  insert_classification(conn, records)         # 批量写入分类
  insert_validation(conn, results)             # 批量写入验证
  export_table_csv(conn, "varieties", path)    # 导出任意表为 CSV
"""

from __future__ import annotations
import csv
import json
import logging
from pathlib import Path
from typing import List, Optional

import psycopg2
import psycopg2.extras

from src.core.models import ExtractionResult
from src.output.statistics import generate_statistics

logger = logging.getLogger("paper_extractor")


# ── 建表 SQL（PostgreSQL 语法）────────────────────────────

_SCHEMA = """
-- 论文级数据
CREATE TABLE IF NOT EXISTS papers (
    paper_id        TEXT PRIMARY KEY,
    doi             TEXT,
    title           TEXT,
    publication_year INTEGER,
    journal_name    TEXT,
    crop_species    TEXT,
    language        TEXT,
    category        TEXT,
    data_file_link  TEXT,
    data_file_description TEXT,
    data_file_version TEXT,
    extracted_at    TEXT
);

-- 试验级数据
CREATE TABLE IF NOT EXISTS studies (
    paper_id        TEXT NOT NULL,
    study_index     INTEGER NOT NULL,
    study_title     TEXT,
    study_description TEXT,
    trial_year      TEXT,
    sowing_date     TEXT,
    harvest_date    TEXT,
    country         TEXT,
    site_administrative_region TEXT,
    experimental_site_name TEXT,
    latitude        DOUBLE PRECISION,
    longitude       DOUBLE PRECISION,
    altitude        DOUBLE PRECISION,
    geo_source      TEXT,
    replication_number INTEGER,
    plot_size       TEXT,
    planting_density TEXT,
    experimental_design_description TEXT,
    experimental_design_type TEXT,
    growth_facility_description TEXT,
    cultural_practices TEXT,
    notes           TEXT,
    PRIMARY KEY (paper_id, study_index)
);

-- 品种产量数据（主数据表）
CREATE TABLE IF NOT EXISTS varieties (
    paper_id        TEXT NOT NULL,
    study_index     INTEGER NOT NULL,
    variety_index   INTEGER NOT NULL,
    variety_name    TEXT,
    variety_code    TEXT,
    is_check_variety INTEGER,
    variety_source  TEXT,
    yield_raw_value DOUBLE PRECISION,
    yield_raw_unit  TEXT,
    yield_standard_value DOUBLE PRECISION,
    yield_standard_unit TEXT DEFAULT 'kg/ha',
    yield_value_type TEXT,
    significance_group TEXT,
    pct_over_check  DOUBLE PRECISION,
    measurement_method TEXT,
    source_location TEXT,
    confidence_level TEXT,
    PRIMARY KEY (paper_id, study_index, variety_index)
);

-- 品种产量宽表（扁平化，每行 = paper+study+variety 全部字段，用于交接导出）
CREATE TABLE IF NOT EXISTS varieties_flat (
    paper_id        TEXT NOT NULL,
    study_index     INTEGER NOT NULL,
    variety_index   INTEGER NOT NULL,
    doi             TEXT,
    paper_title     TEXT,
    publication_year INTEGER,
    journal_name    TEXT,
    crop_species    TEXT,
    language        TEXT,
    category        TEXT,
    study_title     TEXT,
    trial_year      TEXT,
    sowing_date     TEXT,
    harvest_date    TEXT,
    country         TEXT,
    site_administrative_region TEXT,
    experimental_site_name TEXT,
    latitude        DOUBLE PRECISION,
    longitude       DOUBLE PRECISION,
    altitude        DOUBLE PRECISION,
    geo_source      TEXT,
    replication_number INTEGER,
    plot_size       TEXT,
    planting_density TEXT,
    experimental_design_description TEXT,
    experimental_design_type TEXT,
    growth_facility_description TEXT,
    cultural_practices TEXT,
    study_notes     TEXT,
    variety_name    TEXT,
    variety_code    TEXT,
    is_check_variety INTEGER,
    variety_source  TEXT,
    yield_raw_value DOUBLE PRECISION,
    yield_raw_unit  TEXT,
    yield_standard_value DOUBLE PRECISION,
    yield_standard_unit TEXT DEFAULT 'kg/ha',
    yield_value_type TEXT,
    significance_group TEXT,
    pct_over_check  DOUBLE PRECISION,
    measurement_method TEXT,
    source_location TEXT,
    confidence_level TEXT,
    extracted_at    TEXT,
    PRIMARY KEY (paper_id, study_index, variety_index)
);

-- 论文分类结果
CREATE TABLE IF NOT EXISTS classification (
    paper_id        TEXT PRIMARY KEY,
    doi             TEXT,
    title           TEXT,
    language        TEXT,
    year            TEXT,
    journal         TEXT,
    category        TEXT,
    confidence      DOUBLE PRECISION,
    reasoning       TEXT,
    key_signals     TEXT,
    crop_species    TEXT,
    paper_type      TEXT,
    has_yield_data  INTEGER,
    research_country TEXT
);

-- 验证问题明细（扁平化）
CREATE TABLE IF NOT EXISTS validation_issues (
    id              SERIAL PRIMARY KEY,
    paper_id        TEXT NOT NULL,
    issue_type      TEXT NOT NULL,
    severity        TEXT NOT NULL,
    message         TEXT
);

-- 论文处理状态（兼任务协调注册表）
CREATE TABLE IF NOT EXISTS paper_status (
    paper_id        TEXT PRIMARY KEY,
    title           TEXT,
    target_step     TEXT,
    status          TEXT DEFAULT 'pending',
    claimed_by      TEXT,
    duration_sec    DOUBLE PRECISION,
    error_message   TEXT,
    run_id          TEXT,
    updated_at      TEXT
);

-- 无法获取 PDF 的论文记录
CREATE TABLE IF NOT EXISTS pdf_missing (
    paper_id        TEXT PRIMARY KEY,
    title           TEXT,
    doi             TEXT,
    reason          TEXT,
    attempted_at    TEXT
);

-- 字段文档表（数据字典）
CREATE TABLE IF NOT EXISTS _schema_doc (
    table_name      TEXT NOT NULL,
    column_name     TEXT NOT NULL,
    column_type     TEXT,
    description     TEXT,
    is_required     INTEGER DEFAULT 0,
    source          TEXT,
    PRIMARY KEY (table_name, column_name)
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_varieties_name ON varieties(variety_name);
CREATE INDEX IF NOT EXISTS idx_varieties_paper ON varieties(paper_id);
CREATE INDEX IF NOT EXISTS idx_studies_paper ON studies(paper_id);
CREATE INDEX IF NOT EXISTS idx_studies_region ON studies(site_administrative_region);
CREATE INDEX IF NOT EXISTS idx_studies_year ON studies(trial_year);
CREATE INDEX IF NOT EXISTS idx_classification_cat ON classification(category);
CREATE INDEX IF NOT EXISTS idx_validation_paper ON validation_issues(paper_id);
CREATE INDEX IF NOT EXISTS idx_validation_severity ON validation_issues(severity);
CREATE INDEX IF NOT EXISTS idx_paper_status ON paper_status(status);
CREATE INDEX IF NOT EXISTS idx_paper_status_claimed ON paper_status(claimed_by);
CREATE INDEX IF NOT EXISTS idx_pdf_missing ON pdf_missing(paper_id);
"""


def init_database(connection_string: str):
    """
    初始化 PostgreSQL 数据库，创建所有表、索引和字段文档。

    Args:
        connection_string: PG 连接字符串，如 "postgresql://user:pass@host:5432/dbname"

    Returns:
        psycopg2 连接对象。调用方负责关闭连接。
    """
    conn = psycopg2.connect(connection_string)
    conn.autocommit = False

    # 执行建表（逐条执行，PG 不支持 executescript）
    # 注意：split(";") 后每条语句可能包含前导注释行，需要去除
    with conn.cursor() as cur:
        for raw_statement in _SCHEMA.strip().split(";"):
            # 去除前导注释行（-- 开头的行），保留实际 SQL
            lines = raw_statement.strip().splitlines()
            sql_lines = [l for l in lines if not l.strip().startswith("--")]
            statement = "\n".join(sql_lines).strip()
            if statement:
                cur.execute(statement)

    conn.commit()
    _populate_schema_doc(conn)
    logger.info(f"Database initialized: {connection_string.split('@')[-1] if '@' in connection_string else connection_string}")
    return conn


# ── 字段文档 ──────────────────────────────────────────────

_SCHEMA_DOCS = [
    # ── papers 表 ──
    ("papers", "paper_id", "TEXT", "论文唯一标识（P_ + MD5指纹前10位）", 1, "系统生成"),
    ("papers", "doi", "TEXT", "论文 DOI", 0, "元数据CSV"),
    ("papers", "title", "TEXT", "论文完整标题", 1, "LLM提取"),
    ("papers", "publication_year", "INTEGER", "发表年份", 1, "LLM提取"),
    ("papers", "journal_name", "TEXT", "期刊/来源名称", 0, "LLM提取"),
    ("papers", "crop_species", "TEXT", "作物物种，如'水稻/Rice'", 1, "LLM提取"),
    ("papers", "language", "TEXT", "论文语言（zh/en）", 0, "系统检测"),
    ("papers", "category", "TEXT", "论文分类（varietal_yield/management_yield等）", 0, "LLM分类"),
    ("papers", "data_file_link", "TEXT", "公共数据库中的数据文件链接", 0, "LLM提取"),
    ("papers", "data_file_description", "TEXT", "数据文件格式说明", 0, "LLM提取"),
    ("papers", "data_file_version", "TEXT", "数据集版本号", 0, "LLM提取"),
    ("papers", "extracted_at", "TEXT", "提取时间（ISO 8601）", 0, "系统生成"),
    # ── studies 表 ──
    ("studies", "paper_id", "TEXT", "关联论文ID", 1, "外键"),
    ("studies", "study_index", "INTEGER", "试验在论文中的序号（0起始）", 1, "系统生成"),
    ("studies", "study_title", "TEXT", "试验名称/标题", 1, "LLM提取"),
    ("studies", "study_description", "TEXT", "试验简述（1-2句话）", 0, "LLM提取"),
    ("studies", "trial_year", "TEXT", "试验年份或期间，如'2022'或'2022-2025'", 1, "LLM提取"),
    ("studies", "sowing_date", "TEXT", "播种日期（ISO 8601）", 0, "LLM提取"),
    ("studies", "harvest_date", "TEXT", "收获日期（ISO 8601）", 0, "LLM提取"),
    ("studies", "country", "TEXT", "试验所在国家（ISO 3166代码），如CN", 1, "LLM提取"),
    ("studies", "site_administrative_region", "TEXT", "试验点行政区划（省/市/县）", 1, "LLM提取"),
    ("studies", "experimental_site_name", "TEXT", "试验站/试验地点名称", 0, "LLM提取"),
    ("studies", "latitude", "DOUBLE PRECISION", "纬度（仅论文明确写出时）", 0, "LLM/地理编码"),
    ("studies", "longitude", "DOUBLE PRECISION", "经度（仅论文明确写出时）", 0, "LLM/地理编码"),
    ("studies", "altitude", "DOUBLE PRECISION", "海拔/米（仅论文明确写出时）", 0, "LLM/地理编码"),
    ("studies", "geo_source", "TEXT", "坐标来源: paper/lookup/baidu/nominatim/province_fallback", 0, "系统标注"),
    ("studies", "replication_number", "INTEGER", "田间试验重复次数", 0, "LLM提取"),
    ("studies", "plot_size", "TEXT", "小区面积，如'13.3 m²'", 0, "LLM提取"),
    ("studies", "planting_density", "TEXT", "种植密度，如'22.5万穴/公顷'", 0, "LLM提取"),
    ("studies", "experimental_design_description", "TEXT", "试验设计的文字描述", 1, "LLM提取"),
    ("studies", "experimental_design_type", "TEXT", "试验设计类型: RCBD/Split-plot/CRD/Unknown", 0, "LLM提取"),
    ("studies", "growth_facility_description", "TEXT", "试验环境描述，如'大田环境'、'温室'", 0, "LLM提取"),
    ("studies", "cultural_practices", "TEXT", "栽培管理措施描述", 0, "LLM提取"),
    ("studies", "notes", "TEXT", "系统自动生成的数据质量备注（如多站点标记）", 0, "系统生成"),
    # ── varieties 表 ──
    ("varieties", "paper_id", "TEXT", "关联论文ID", 1, "外键"),
    ("varieties", "study_index", "INTEGER", "关联试验序号", 1, "外键"),
    ("varieties", "variety_index", "INTEGER", "品种在试验中的序号（0起始）", 1, "系统生成"),
    ("varieties", "variety_name", "TEXT", "品种/品系完整名称（含编号）", 1, "LLM提取"),
    ("varieties", "variety_code", "TEXT", "品种审定编号（跨文献实体解析键）", 0, "LLM/系统回填"),
    ("varieties", "is_check_variety", "INTEGER", "是否为对照(CK)品种（1=是, 0=否）", 1, "LLM提取"),
    ("varieties", "variety_source", "TEXT", "育种单位/来源", 0, "LLM提取"),
    ("varieties", "yield_raw_value", "DOUBLE PRECISION", "论文中的原始产量数值", 1, "LLM提取"),
    ("varieties", "yield_raw_unit", "TEXT", "原始产量单位，如kg/亩、kg/ha", 1, "LLM提取"),
    ("varieties", "yield_standard_value", "DOUBLE PRECISION", "程序换算的kg/ha值", 0, "程序计算"),
    ("varieties", "yield_standard_unit", "TEXT", "标准单位，固定kg/ha", 0, "程序固定"),
    ("varieties", "yield_value_type", "TEXT", "产量值统计类型: plot_mean/single_replicate/converted", 1, "LLM提取"),
    ("varieties", "significance_group", "TEXT", "显著性字母标记，如a/b/ab", 0, "LLM提取"),
    ("varieties", "pct_over_check", "DOUBLE PRECISION", "相对对照品种的增产/减产百分比", 0, "LLM提取"),
    ("varieties", "measurement_method", "TEXT", "产量测定与计产方法", 0, "LLM提取"),
    ("varieties", "source_location", "TEXT", "数据来源位置：论文中的表格或段落，如'表5'", 1, "LLM提取"),
    ("varieties", "confidence_level", "TEXT", "提取置信度: high/medium/low", 1, "LLM评估"),
    # ── varieties_flat 宽表 ──
    ("varieties_flat", "paper_id", "TEXT", "论文唯一标识", 1, "系统生成"),
    ("varieties_flat", "study_index", "INTEGER", "试验序号", 1, "系统生成"),
    ("varieties_flat", "variety_index", "INTEGER", "品种序号", 1, "系统生成"),
    ("varieties_flat", "doi", "TEXT", "论文DOI", 0, "元数据"),
    ("varieties_flat", "paper_title", "TEXT", "论文标题", 1, "LLM提取"),
    ("varieties_flat", "publication_year", "INTEGER", "发表年份", 1, "LLM提取"),
    ("varieties_flat", "journal_name", "TEXT", "期刊名称", 0, "LLM提取"),
    ("varieties_flat", "crop_species", "TEXT", "作物物种", 1, "LLM提取"),
    ("varieties_flat", "language", "TEXT", "论文语言", 0, "系统检测"),
    ("varieties_flat", "category", "TEXT", "论文分类", 0, "LLM分类"),
    ("varieties_flat", "study_title", "TEXT", "试验名称", 1, "LLM提取"),
    ("varieties_flat", "trial_year", "TEXT", "试验年份", 1, "LLM提取"),
    ("varieties_flat", "site_administrative_region", "TEXT", "行政区划", 1, "LLM提取"),
    ("varieties_flat", "experimental_site_name", "TEXT", "试验站名称", 0, "LLM提取"),
    ("varieties_flat", "latitude", "DOUBLE PRECISION", "纬度", 0, "LLM/地理编码"),
    ("varieties_flat", "longitude", "DOUBLE PRECISION", "经度", 0, "LLM/地理编码"),
    ("varieties_flat", "variety_name", "TEXT", "品种名称", 1, "LLM提取"),
    ("varieties_flat", "variety_code", "TEXT", "品种审定编号", 0, "LLM/系统回填"),
    ("varieties_flat", "is_check_variety", "INTEGER", "是否对照品种", 1, "LLM提取"),
    ("varieties_flat", "yield_raw_value", "DOUBLE PRECISION", "原始产量数值", 1, "LLM提取"),
    ("varieties_flat", "yield_raw_unit", "TEXT", "原始产量单位", 1, "LLM提取"),
    ("varieties_flat", "yield_standard_value", "DOUBLE PRECISION", "换算后kg/ha值", 0, "程序计算"),
    ("varieties_flat", "pct_over_check", "DOUBLE PRECISION", "增产/减产百分比", 0, "LLM提取"),
    ("varieties_flat", "source_location", "TEXT", "数据来源位置", 1, "LLM提取"),
    ("varieties_flat", "confidence_level", "TEXT", "提取置信度", 1, "LLM评估"),
    ("varieties_flat", "extracted_at", "TEXT", "提取时间", 0, "系统生成"),
    # ── classification 表 ──
    ("classification", "paper_id", "TEXT", "论文唯一标识", 1, "系统生成"),
    ("classification", "doi", "TEXT", "论文DOI", 0, "元数据"),
    ("classification", "title", "TEXT", "论文标题", 1, "元数据"),
    ("classification", "language", "TEXT", "论文语言", 0, "系统检测"),
    ("classification", "year", "TEXT", "发表年份", 0, "元数据"),
    ("classification", "journal", "TEXT", "期刊名称", 0, "元数据"),
    ("classification", "category", "TEXT", "分类结果（5类之一）", 1, "LLM分类"),
    ("classification", "confidence", "DOUBLE PRECISION", "分类置信度（0-1）", 0, "LLM评估"),
    ("classification", "reasoning", "TEXT", "分类判断依据", 0, "LLM生成"),
    ("classification", "key_signals", "TEXT", "支持分类的关键信号（JSON数组）", 0, "LLM生成"),
    ("classification", "crop_species", "TEXT", "作物物种", 0, "LLM识别"),
    ("classification", "paper_type", "TEXT", "论文类型（期刊/学位/综述/会议）", 0, "LLM识别"),
    ("classification", "has_yield_data", "INTEGER", "是否包含产量数据（1=是, 0=否）", 0, "LLM判断"),
    ("classification", "research_country", "TEXT", "研究国家（China/Unknown等）", 0, "LLM判断"),
    # ── validation_issues 表 ──
    ("validation_issues", "id", "SERIAL", "自增主键", 1, "系统生成"),
    ("validation_issues", "paper_id", "TEXT", "关联论文ID", 1, "外键"),
    ("validation_issues", "issue_type", "TEXT", "问题类型: issue（严重）/ warning（警告）", 1, "规则引擎"),
    ("validation_issues", "severity", "TEXT", "严重级别: error / warning", 1, "规则引擎"),
    ("validation_issues", "message", "TEXT", "问题描述（含具体数值和上下文）", 1, "规则引擎"),
    # ── paper_status 表 ──
    ("paper_status", "paper_id", "TEXT", "论文唯一标识", 1, "系统生成"),
    ("paper_status", "title", "TEXT", "论文标题（截取前80字符）", 0, "元数据"),
    ("paper_status", "target_step", "TEXT", "本次运行的目标步骤（classify/parse/extract）", 1, "用户指定"),
    ("paper_status", "status", "TEXT", "处理状态: pending/processing/completed/failed/skipped", 1, "系统记录"),
    ("paper_status", "claimed_by", "TEXT", "领取该任务的实例ID", 0, "系统记录"),
    ("paper_status", "duration_sec", "DOUBLE PRECISION", "处理耗时（秒）", 0, "系统记录"),
    ("paper_status", "error_message", "TEXT", "错误信息（失败时）", 0, "系统记录"),
    ("paper_status", "run_id", "TEXT", "本次运行ID（时间戳）", 0, "系统生成"),
    ("paper_status", "updated_at", "TEXT", "最后更新时间（ISO 8601）", 0, "系统生成"),
    # ── pdf_missing 表 ──
    ("pdf_missing", "paper_id", "TEXT", "论文唯一标识", 1, "系统生成"),
    ("pdf_missing", "title", "TEXT", "论文标题", 0, "元数据"),
    ("pdf_missing", "doi", "TEXT", "论文DOI", 0, "元数据"),
    ("pdf_missing", "reason", "TEXT", "下载失败原因（404/timeout/error）", 1, "系统记录"),
    ("pdf_missing", "attempted_at", "TEXT", "尝试下载时间（ISO 8601）", 0, "系统生成"),
]


def _populate_schema_doc(conn):
    """填充字段文档表（幂等操作，ON CONFLICT DO UPDATE）。"""
    with conn.cursor() as cur:
        for table, col, col_type, desc, required, source in _SCHEMA_DOCS:
            cur.execute("""
                INSERT INTO _schema_doc (table_name, column_name, column_type, description, is_required, source)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (table_name, column_name) DO UPDATE SET
                    column_type = EXCLUDED.column_type,
                    description = EXCLUDED.description,
                    is_required = EXCLUDED.is_required,
                    source = EXCLUDED.source
            """, (table, col, col_type, desc, required, source))
    conn.commit()


# ── 写入函数 ──────────────────────────────────────────────

def insert_extraction(conn, result: dict, paper_id: str):
    """
    将一篇论文的提取结果写入数据库（papers + studies + varieties + varieties_flat）。

    使用 ON CONFLICT DO UPDATE 实现幂等写入。
    线程安全：调用方需保证 conn 的线程安全（或使用锁）。
    """
    extraction = result.get("extraction", {})
    if not extraction:
        return

    paper = extraction.get("paper", {})
    studies = extraction.get("studies", [])

    with conn.cursor() as cur:
        # ── papers 表 ──
        cur.execute("""
            INSERT INTO papers
            (paper_id, doi, title, publication_year, journal_name, crop_species,
             language, category, data_file_link, data_file_description, data_file_version, extracted_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (paper_id) DO UPDATE SET
                doi = EXCLUDED.doi, title = EXCLUDED.title,
                publication_year = EXCLUDED.publication_year, journal_name = EXCLUDED.journal_name,
                crop_species = EXCLUDED.crop_species, language = EXCLUDED.language,
                category = EXCLUDED.category, extracted_at = EXCLUDED.extracted_at
        """, (
            paper_id,
            paper.get("paper_doi"),
            paper.get("paper_title"),
            paper.get("publication_year"),
            paper.get("journal_name"),
            paper.get("crop_species"),
            result.get("language"),
            result.get("category"),
            paper.get("data_file_link"),
            paper.get("data_file_description"),
            paper.get("data_file_version"),
            result.get("extracted_at"),
        ))

        # ── studies 表 ──
        for si, study in enumerate(studies):
            cur.execute("""
                INSERT INTO studies
                (paper_id, study_index, study_title, study_description, trial_year,
                 sowing_date, harvest_date, country, site_administrative_region,
                 experimental_site_name, latitude, longitude, altitude, geo_source,
                 replication_number, plot_size, planting_density,
                 experimental_design_description, experimental_design_type,
                 growth_facility_description, cultural_practices, notes)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (paper_id, study_index) DO UPDATE SET
                    study_title = EXCLUDED.study_title, trial_year = EXCLUDED.trial_year,
                    site_administrative_region = EXCLUDED.site_administrative_region,
                    latitude = EXCLUDED.latitude, longitude = EXCLUDED.longitude,
                    notes = EXCLUDED.notes
            """, (
                paper_id, si,
                study.get("study_title"), study.get("study_description"),
                study.get("trial_year"), study.get("sowing_date"), study.get("harvest_date"),
                study.get("country"), study.get("site_administrative_region"),
                study.get("experimental_site_name"),
                study.get("latitude"), study.get("longitude"), study.get("altitude"),
                study.get("geo_source"), study.get("replication_number"),
                study.get("plot_size"), study.get("planting_density"),
                study.get("experimental_design_description"),
                study.get("experimental_design_type"),
                study.get("growth_facility_description"),
                study.get("cultural_practices"), study.get("notes"),
            ))

            # ── varieties + varieties_flat ──
            varieties = study.get("varieties", [])
            for vi, v in enumerate(varieties):
                is_ck = v.get("is_check_variety")
                is_ck_int = 1 if is_ck else (0 if is_ck is not None else None)

                cur.execute("""
                    INSERT INTO varieties
                    (paper_id, study_index, variety_index, variety_name, variety_code,
                     is_check_variety, variety_source, yield_raw_value, yield_raw_unit,
                     yield_standard_value, yield_standard_unit, yield_value_type,
                     significance_group, pct_over_check, measurement_method,
                     source_location, confidence_level)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (paper_id, study_index, variety_index) DO UPDATE SET
                        variety_name = EXCLUDED.variety_name, variety_code = EXCLUDED.variety_code,
                        yield_raw_value = EXCLUDED.yield_raw_value,
                        yield_standard_value = EXCLUDED.yield_standard_value,
                        pct_over_check = EXCLUDED.pct_over_check
                """, (
                    paper_id, si, vi,
                    v.get("variety_name"), v.get("variety_code"), is_ck_int,
                    v.get("variety_source"), v.get("yield_raw_value"),
                    v.get("yield_raw_unit"), v.get("yield_standard_value"),
                    v.get("yield_standard_unit", "kg/ha"), v.get("yield_value_type"),
                    v.get("significance_group"), v.get("pct_over_check"),
                    v.get("measurement_method"), v.get("source_location"),
                    v.get("confidence_level"),
                ))

                # 宽表
                cur.execute("""
                    INSERT INTO varieties_flat
                    (paper_id, study_index, variety_index,
                     doi, paper_title, publication_year, journal_name, crop_species,
                     language, category,
                     study_title, trial_year, sowing_date, harvest_date, country,
                     site_administrative_region, experimental_site_name,
                     latitude, longitude, altitude, geo_source,
                     replication_number, plot_size, planting_density,
                     experimental_design_description, experimental_design_type,
                     growth_facility_description, cultural_practices, study_notes,
                     variety_name, variety_code, is_check_variety, variety_source,
                     yield_raw_value, yield_raw_unit, yield_standard_value,
                     yield_standard_unit, yield_value_type, significance_group,
                     pct_over_check, measurement_method, source_location,
                     confidence_level, extracted_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (paper_id, study_index, variety_index) DO UPDATE SET
                        variety_name = EXCLUDED.variety_name,
                        yield_standard_value = EXCLUDED.yield_standard_value,
                        extracted_at = EXCLUDED.extracted_at
                """, (
                    paper_id, si, vi,
                    paper.get("paper_doi"), paper.get("paper_title"),
                    paper.get("publication_year"), paper.get("journal_name"),
                    paper.get("crop_species"), result.get("language"), result.get("category"),
                    study.get("study_title"), study.get("trial_year"),
                    study.get("sowing_date"), study.get("harvest_date"),
                    study.get("country"), study.get("site_administrative_region"),
                    study.get("experimental_site_name"),
                    study.get("latitude"), study.get("longitude"), study.get("altitude"),
                    study.get("geo_source"), study.get("replication_number"),
                    study.get("plot_size"), study.get("planting_density"),
                    study.get("experimental_design_description"),
                    study.get("experimental_design_type"),
                    study.get("growth_facility_description"),
                    study.get("cultural_practices"), study.get("notes"),
                    v.get("variety_name"), v.get("variety_code"), is_ck_int,
                    v.get("variety_source"), v.get("yield_raw_value"),
                    v.get("yield_raw_unit"), v.get("yield_standard_value"),
                    v.get("yield_standard_unit", "kg/ha"), v.get("yield_value_type"),
                    v.get("significance_group"), v.get("pct_over_check"),
                    v.get("measurement_method"), v.get("source_location"),
                    v.get("confidence_level"), result.get("extracted_at"),
                ))

    conn.commit()


def insert_classification(conn, records: List[dict]):
    """批量写入分类结果（幂等，ON CONFLICT DO UPDATE）。"""
    if not records:
        return

    with conn.cursor() as cur:
        for cls in records:
            key_signals = cls.get("key_signals")
            if isinstance(key_signals, list):
                key_signals = json.dumps(key_signals, ensure_ascii=False)

            has_yield = cls.get("has_yield_data")
            has_yield_int = 1 if has_yield else (0 if has_yield is not None else None)

            cur.execute("""
                INSERT INTO classification
                (paper_id, doi, title, language, year, journal, category,
                 confidence, reasoning, key_signals, crop_species, paper_type,
                 has_yield_data, research_country)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (paper_id) DO UPDATE SET
                    category = EXCLUDED.category, confidence = EXCLUDED.confidence,
                    reasoning = EXCLUDED.reasoning, key_signals = EXCLUDED.key_signals,
                    crop_species = EXCLUDED.crop_species, research_country = EXCLUDED.research_country
            """, (
                cls.get("paper_id"), cls.get("doi"), cls.get("title"),
                cls.get("language"), cls.get("year"), cls.get("journal"),
                cls.get("category"), cls.get("confidence"), cls.get("reasoning"),
                key_signals, cls.get("crop_species"), cls.get("paper_type"),
                has_yield_int, cls.get("research_country"),
            ))

    conn.commit()
    logger.info(f"Classification: {len(records)} records written to DB")


def insert_validation(conn, results: List[dict]):
    """将验证报告扁平化写入 validation_issues 表。"""
    if not results:
        return

    with conn.cursor() as cur:
        cur.execute("DELETE FROM validation_issues")

        row_count = 0
        for record in results:
            paper_id = record.get("paper_id", "")
            report = record.get("validation_report", {})

            for issue in report.get("issues", []):
                cur.execute(
                    "INSERT INTO validation_issues (paper_id, issue_type, severity, message) VALUES (%s, %s, %s, %s)",
                    (paper_id, "issue", "error", issue))
                row_count += 1

            for warning in report.get("warnings", []):
                cur.execute(
                    "INSERT INTO validation_issues (paper_id, issue_type, severity, message) VALUES (%s, %s, %s, %s)",
                    (paper_id, "warning", "warning", warning))
                row_count += 1

    conn.commit()
    logger.info(f"Validation: {row_count} issues/warnings written to DB")


def update_paper_status(
    conn,
    paper_id: str,
    title: str = "",
    target_step: str = "",
    status: str = "",
    duration_sec: float = 0.0,
    error_message: str = "",
    run_id: str = "",
):
    """记录或更新一篇论文的处理状态（幂等）。"""
    from datetime import datetime
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO paper_status
            (paper_id, title, target_step, status, duration_sec, error_message, run_id, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (paper_id) DO UPDATE SET
                title = EXCLUDED.title, target_step = EXCLUDED.target_step,
                status = EXCLUDED.status, duration_sec = EXCLUDED.duration_sec,
                error_message = EXCLUDED.error_message, run_id = EXCLUDED.run_id,
                updated_at = EXCLUDED.updated_at
        """, (
            paper_id, title, target_step, status,
            round(duration_sec, 2), error_message, run_id,
            datetime.now().isoformat(),
        ))
    conn.commit()


def insert_pdf_missing(conn, paper_id: str, title: str = "", doi: str = "", reason: str = ""):
    """记录无法获取 PDF 的论文。"""
    from datetime import datetime
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO pdf_missing (paper_id, title, doi, reason, attempted_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (paper_id) DO UPDATE SET
                reason = EXCLUDED.reason, attempted_at = EXCLUDED.attempted_at
        """, (paper_id, title, doi, reason, datetime.now().isoformat()))
    conn.commit()


def claim_tasks(conn, instance_id: str, limit: int = 10) -> List[str]:
    """
    原子领取待处理任务（多实例安全）。

    使用 SELECT FOR UPDATE SKIP LOCKED 确保同一论文不会被多个实例同时领取。

    Returns:
        领取到的 paper_id 列表
    """
    from datetime import datetime
    with conn.cursor() as cur:
        # 先查询待处理的论文（加行锁，跳过被其他实例锁住的行）
        cur.execute("""
            SELECT paper_id FROM paper_status
            WHERE status = 'pending'
            ORDER BY paper_id
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        """, (limit,))
        paper_ids = [row[0] for row in cur.fetchall()]

        if paper_ids:
            # 标记为 processing 并记录领取者
            cur.execute("""
                UPDATE paper_status
                SET status = 'processing', claimed_by = %s, updated_at = %s
                WHERE paper_id = ANY(%s)
            """, (instance_id, datetime.now().isoformat(), paper_ids))

    conn.commit()
    return paper_ids


# ── 导出函数 ──────────────────────────────────────────────

def export_table_csv(conn, table_name: str, csv_path: Path):
    """将指定表导出为 CSV 文件。"""
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM {table_name}")
        columns = [desc[0] for desc in cur.description]
        rows = cur.fetchall()

    if not rows:
        logger.info(f"Table '{table_name}' is empty, skipping CSV export")
        return

    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        writer.writerows(rows)

    logger.info(f"Exported {table_name} → {csv_path} ({len(rows)} rows)")


def export_delivery_csv(conn, csv_path: Path):
    """导出交接用宽表 CSV（varieties_flat）。"""
    export_table_csv(conn, "varieties_flat", csv_path)


def get_table_stats(conn) -> dict:
    """获取各表的行数统计。"""
    stats = {}
    with conn.cursor() as cur:
        for table in ("papers", "studies", "varieties", "varieties_flat",
                       "classification", "validation_issues", "paper_status", "pdf_missing"):
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            stats[table] = cur.fetchone()[0]
    return stats
