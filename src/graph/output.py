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
from datetime import datetime
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
    extracted_at    TEXT,
    parse_context   JSONB
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
    language        TEXT,
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
    code            TEXT,
    message         TEXT
);

-- 证据验证明细
CREATE TABLE IF NOT EXISTS evidence (
    id              SERIAL PRIMARY KEY,
    paper_id        TEXT NOT NULL,
    field_name      TEXT NOT NULL,
    field_value     TEXT,
    source_location TEXT,
    source_text     TEXT,
    confidence      TEXT,
    verified        INTEGER,
    reason          TEXT,
    created_at      TEXT
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
    updated_at      TEXT,
    -- 搜索阶段写入的论文元数据（供后续 classify/download/extract 使用）
    ss_paper_id     TEXT,
    doi             TEXT,
    abstract        TEXT,
    publication_year TEXT,
    journal         TEXT
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
CREATE INDEX IF NOT EXISTS idx_evidence_paper ON evidence(paper_id);
CREATE INDEX IF NOT EXISTS idx_evidence_field ON evidence(field_name);
CREATE INDEX IF NOT EXISTS idx_paper_status ON paper_status(status);
CREATE INDEX IF NOT EXISTS idx_paper_status_claimed ON paper_status(claimed_by);
CREATE INDEX IF NOT EXISTS idx_pdf_missing ON pdf_missing(paper_id);
"""


def get_connection(connection_string: str):
    """
    获取 PostgreSQL 连接（轻量级，不执行任何 DDL）。

    适用于 API 端点等高频场景。建表/注释等 schema 初始化
    由 init_database() 在服务启动时一次性完成。
    """
    conn = psycopg2.connect(connection_string)
    conn.autocommit = False
    return conn


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
        # ── 迁移：旧版 classification 表含 title/doi/year/journal 冗余列，检测并重建 ──
        cur.execute("""
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'classification' AND column_name = 'doi'
        """)
        if cur.fetchone():
            cur.execute("DROP TABLE classification")
            logger.info("Dropped legacy classification table (removed redundant doi/title/year/journal columns)")

        for raw_statement in _SCHEMA.strip().split(";"):
            # 去除前导注释行（-- 开头的行），保留实际 SQL
            lines = raw_statement.strip().splitlines()
            sql_lines = [l for l in lines if not l.strip().startswith("--")]
            statement = "\n".join(sql_lines).strip()
            if statement:
                cur.execute(statement)

        # 兼容已有数据库：为 paper_status 补充搜索元数据列
        for col, col_type in [
            ("ss_paper_id", "TEXT"), ("doi", "TEXT"),
            ("abstract", "TEXT"), ("year", "TEXT"), ("journal", "TEXT"),
        ]:
            cur.execute(
                f"ALTER TABLE paper_status ADD COLUMN IF NOT EXISTS {col} {col_type}"
            )

    conn.commit()
    _populate_schema_doc(conn)
    logger.info(f"Database initialized: {connection_string.split('@')[-1] if '@' in connection_string else connection_string}")
    return conn


# ── 表级注释 ──────────────────────────────────────────────

_TABLE_COMMENTS = {
    "papers": "论文级数据（一篇一行），doi/title/year/journal 来自搜索元数据",
    "studies": "试验级数据（一篇论文多行），一年×一站 = 一个试验",
    "varieties": "品种产量数据（主数据表），一行 = 一个品种×一个试验",
    "varieties_flat": "品种产量宽表（扁平化交接用），每行含 paper+study+variety 全部字段",
    "classification": "论文分类结果（5类），paper_id FK → paper_status",
    "validation_issues": "验证问题明细（扁平化），issue=严重/warning=警告",
    "evidence": "证据验证明细（字段来源追溯）",
    "paper_status": "论文处理状态（兼任务协调注册表+搜索元数据存储）",
    "pdf_missing": "无法获取 PDF 的论文记录",
    "_schema_doc": "字段文档表（数据字典），自动维护",
}


# ── 字段文档 ──────────────────────────────────────────────

_SCHEMA_DOCS = [
    # ── papers 表 ──
    ("papers", "paper_id", "TEXT", "论文唯一标识（P_ + MD5指纹前10位）", 1, "系统生成"),
    ("papers", "doi", "TEXT", "论文 DOI", 0, "搜索元数据"),
    ("papers", "title", "TEXT", "论文完整标题", 1, "搜索元数据"),
    ("papers", "publication_year", "INTEGER", "发表年份", 1, "搜索元数据"),
    ("papers", "journal_name", "TEXT", "期刊/来源名称", 0, "搜索元数据"),
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
    # ── varieties_flat 宽表（全部字段，与 CREATE TABLE 一一对应）──
    ("varieties_flat", "paper_id", "TEXT", "论文唯一标识", 1, "系统生成"),
    ("varieties_flat", "study_index", "INTEGER", "试验序号", 1, "系统生成"),
    ("varieties_flat", "variety_index", "INTEGER", "品种序号", 1, "系统生成"),
    ("varieties_flat", "doi", "TEXT", "论文DOI", 0, "搜索元数据"),
    ("varieties_flat", "paper_title", "TEXT", "论文标题", 1, "搜索元数据"),
    ("varieties_flat", "publication_year", "INTEGER", "发表年份", 1, "搜索元数据"),
    ("varieties_flat", "journal_name", "TEXT", "期刊名称", 0, "搜索元数据"),
    ("varieties_flat", "crop_species", "TEXT", "作物物种", 1, "LLM提取"),
    ("varieties_flat", "language", "TEXT", "论文语言", 0, "系统检测"),
    ("varieties_flat", "category", "TEXT", "论文分类", 0, "LLM分类"),
    ("varieties_flat", "study_title", "TEXT", "试验名称", 1, "LLM提取"),
    ("varieties_flat", "trial_year", "TEXT", "试验年份", 1, "LLM提取"),
    ("varieties_flat", "sowing_date", "TEXT", "播种日期（ISO 8601）", 0, "LLM提取"),
    ("varieties_flat", "harvest_date", "TEXT", "收获日期（ISO 8601）", 0, "LLM提取"),
    ("varieties_flat", "country", "TEXT", "试验所在国家（ISO 3166代码）", 1, "LLM提取"),
    ("varieties_flat", "site_administrative_region", "TEXT", "行政区划（省/市/县）", 1, "LLM提取"),
    ("varieties_flat", "experimental_site_name", "TEXT", "试验站名称", 0, "LLM提取"),
    ("varieties_flat", "latitude", "DOUBLE PRECISION", "纬度", 0, "LLM/地理编码"),
    ("varieties_flat", "longitude", "DOUBLE PRECISION", "经度", 0, "LLM/地理编码"),
    ("varieties_flat", "altitude", "DOUBLE PRECISION", "海拔/米", 0, "LLM/地理编码"),
    ("varieties_flat", "geo_source", "TEXT", "坐标来源: paper/lookup/nominatim/province_fallback", 0, "系统标注"),
    ("varieties_flat", "replication_number", "INTEGER", "田间试验重复次数", 0, "LLM提取"),
    ("varieties_flat", "plot_size", "TEXT", "小区面积", 0, "LLM提取"),
    ("varieties_flat", "planting_density", "TEXT", "种植密度", 0, "LLM提取"),
    ("varieties_flat", "experimental_design_description", "TEXT", "试验设计描述", 1, "LLM提取"),
    ("varieties_flat", "experimental_design_type", "TEXT", "试验设计类型: RCBD/Split-plot/CRD", 0, "LLM提取"),
    ("varieties_flat", "growth_facility_description", "TEXT", "试验环境描述（大田/温室）", 0, "LLM提取"),
    ("varieties_flat", "cultural_practices", "TEXT", "栽培管理措施描述", 0, "LLM提取"),
    ("varieties_flat", "study_notes", "TEXT", "数据质量备注", 0, "系统生成"),
    ("varieties_flat", "variety_name", "TEXT", "品种名称", 1, "LLM提取"),
    ("varieties_flat", "variety_code", "TEXT", "品种审定编号", 0, "LLM/系统回填"),
    ("varieties_flat", "is_check_variety", "INTEGER", "是否对照品种（1=是, 0=否）", 1, "LLM提取"),
    ("varieties_flat", "variety_source", "TEXT", "育种单位/来源", 0, "LLM提取"),
    ("varieties_flat", "yield_raw_value", "DOUBLE PRECISION", "原始产量数值", 1, "LLM提取"),
    ("varieties_flat", "yield_raw_unit", "TEXT", "原始产量单位", 1, "LLM提取"),
    ("varieties_flat", "yield_standard_value", "DOUBLE PRECISION", "换算后kg/ha值", 0, "程序计算"),
    ("varieties_flat", "yield_standard_unit", "TEXT", "标准单位，固定kg/ha", 0, "程序固定"),
    ("varieties_flat", "yield_value_type", "TEXT", "产量值类型: plot_mean/single_replicate/converted", 1, "LLM提取"),
    ("varieties_flat", "significance_group", "TEXT", "显著性字母标记（a/b/ab）", 0, "LLM提取"),
    ("varieties_flat", "pct_over_check", "DOUBLE PRECISION", "增产/减产百分比", 0, "LLM提取"),
    ("varieties_flat", "measurement_method", "TEXT", "产量测定与计产方法", 0, "LLM提取"),
    ("varieties_flat", "source_location", "TEXT", "数据来源位置（如'表5'）", 1, "LLM提取"),
    ("varieties_flat", "confidence_level", "TEXT", "提取置信度: high/medium/low", 1, "LLM评估"),
    ("varieties_flat", "extracted_at", "TEXT", "提取时间（ISO 8601）", 0, "系统生成"),
    # ── classification 表 ──
    ("classification", "paper_id", "TEXT", "论文唯一标识（FK → paper_status）", 1, "系统生成"),
    ("classification", "language", "TEXT", "论文语言", 0, "系统检测"),
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
    ("validation_issues", "code", "TEXT", "问题编码（如 YIELD_001）", 0, "规则引擎"),
    ("validation_issues", "message", "TEXT", "问题描述（含具体数值和上下文）", 1, "规则引擎"),
    # ── evidence 表 ──
    ("evidence", "id", "SERIAL", "自增主键", 1, "系统生成"),
    ("evidence", "paper_id", "TEXT", "关联论文ID", 1, "外键"),
    ("evidence", "field_name", "TEXT", "字段名（如 variety_name）", 1, "配置"),
    ("evidence", "field_value", "TEXT", "字段值", 0, "LLM提取"),
    ("evidence", "source_location", "TEXT", "来源位置（如'表3'）", 0, "LLM验证"),
    ("evidence", "source_text", "TEXT", "原文片段", 0, "LLM验证"),
    ("evidence", "confidence", "TEXT", "置信度: high/medium/low", 0, "LLM评估"),
    ("evidence", "verified", "INTEGER", "是否通过验证（1=是, 0=否）", 0, "LLM验证"),
    ("evidence", "reason", "TEXT", "判断原因", 0, "LLM生成"),
    ("evidence", "created_at", "TEXT", "创建时间（ISO 8601）", 1, "系统生成"),
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
    ("paper_status", "ss_paper_id", "TEXT", "Semantic Scholar paperId（用于 PDF 下载）", 0, "搜索阶段"),
    ("paper_status", "doi", "TEXT", "论文 DOI", 0, "搜索阶段"),
    ("paper_status", "abstract", "TEXT", "论文摘要（用于 LLM 分类）", 0, "搜索阶段"),
    ("paper_status", "publication_year", "TEXT", "发表年份", 0, "搜索阶段"),
    ("paper_status", "journal", "TEXT", "期刊名称", 0, "搜索阶段"),
    # ── pdf_missing 表 ──
    ("pdf_missing", "paper_id", "TEXT", "论文唯一标识", 1, "系统生成"),
    ("pdf_missing", "title", "TEXT", "论文标题", 0, "元数据"),
    ("pdf_missing", "doi", "TEXT", "论文DOI", 0, "元数据"),
    ("pdf_missing", "reason", "TEXT", "下载失败原因（404/timeout/error）", 1, "系统记录"),
    ("pdf_missing", "attempted_at", "TEXT", "尝试下载时间（ISO 8601）", 0, "系统生成"),
    # ── _schema_doc 字段文档表 ──
    ("_schema_doc", "table_name", "TEXT", "所属表名", 1, "系统维护"),
    ("_schema_doc", "column_name", "TEXT", "字段名", 1, "系统维护"),
    ("_schema_doc", "column_type", "TEXT", "字段类型（PG语法）", 0, "系统维护"),
    ("_schema_doc", "description", "TEXT", "字段中文说明", 0, "系统维护"),
    ("_schema_doc", "is_required", "INTEGER", "是否必填（1=是, 0=否）", 0, "系统维护"),
    ("_schema_doc", "source", "TEXT", "数据来源（搜索元数据/LLM提取/程序计算/系统生成等）", 0, "系统维护"),
]


def _populate_schema_doc(conn):
    """填充字段文档表 + 为所有表和字段添加 PG 原生 COMMENT（幂等操作）。"""
    with conn.cursor() as cur:
        # ── 字段文档表 ──
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

        # ── 表级注释 ──
        for table, comment in _TABLE_COMMENTS.items():
            cur.execute(f"COMMENT ON TABLE {table} IS %s", (comment,))

        # ── 字段级注释 ──
        for table, col, _col_type, desc, _required, source in _SCHEMA_DOCS:
            cur.execute(
                f"COMMENT ON COLUMN {table}.{col} IS %s",
                (f"{desc}（来源: {source}）",),
            )

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
    meta = result.get("paper_meta", {})
    cls = result.get("classification", {})  # language/category 在分类子字典中
    extracted_at = result.get("extracted_at") or datetime.now().isoformat()

    with conn.cursor() as cur:
        # ── 重跑整篇覆盖：先清空该论文的旧提取行（delete-then-insert）──
        # upsert 在"新提取行数 < 旧行数"时会残留旧行（如 management_yield 改为
        # 只提对照组后 variety 行数减少，高 variety_index 的旧处理行会留下成脏数据）。
        # 故每次写入前按 paper_id 清空 studies/varieties/varieties_flat，再整篇插入。
        # papers 表每个 paper_id 仅一行，直接 upsert 即可，无需删除。
        for _table in ("varieties", "varieties_flat", "studies"):
            cur.execute(f"DELETE FROM {_table} WHERE paper_id = %s", (paper_id,))

        # ── papers 表 ──
        # doi/title/year/journal 从搜索元数据直填（权威来源），crop_species 等从 LLM 取
        # parse_context 从 parse 节点输出（doc_context + extraction_hints）
        # lookup_results 从 lookup 节点输出（补充后的信息）
        # evidence_nodes 从 evidence 节点输出（验证后的证据）
        parse_context_data = {
            "doc_context": result.get("doc_context", {}),
            "extraction_hints": result.get("extraction_hints", []),
            "needs_lookup": result.get("needs_lookup", False),
            "lookup_results": result.get("lookup_results", []),
            "evidence_nodes": result.get("evidence_nodes", []),
        }
        cur.execute("""
            INSERT INTO papers
            (paper_id, doi, title, publication_year, journal_name, crop_species,
             language, category, data_file_link, data_file_description, data_file_version,
             extracted_at, parse_context)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (paper_id) DO UPDATE SET
                doi = EXCLUDED.doi, title = EXCLUDED.title,
                publication_year = EXCLUDED.publication_year, journal_name = EXCLUDED.journal_name,
                crop_species = EXCLUDED.crop_species, language = EXCLUDED.language,
                category = EXCLUDED.category, extracted_at = EXCLUDED.extracted_at,
                parse_context = EXCLUDED.parse_context
        """, (
            paper_id,
            meta.get("doi") or paper.get("paper_doi"),
            meta.get("title") or paper.get("paper_title"),
            int(meta["year"]) if meta.get("year", "").isdigit() else paper.get("publication_year"),
            meta.get("journal") or paper.get("journal_name"),
            paper.get("crop_species"),
            cls.get("language"),
            cls.get("category"),
            paper.get("data_file_link"),
            paper.get("data_file_description"),
            paper.get("data_file_version"),
            extracted_at,
            json.dumps(parse_context_data, ensure_ascii=False),
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
                    meta.get("doi") or paper.get("paper_doi"),
                    meta.get("title") or paper.get("paper_title"),
                    int(meta["year"]) if meta.get("year", "").isdigit() else paper.get("publication_year"),
                    meta.get("journal") or paper.get("journal_name"),
                    paper.get("crop_species"), cls.get("language"), cls.get("category"),
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
                    v.get("confidence_level"), extracted_at,
                ))

        # ── evidence 表 ──
        evidence_nodes = result.get("evidence_nodes", [])
        for ev in evidence_nodes:
            verified_int = 1 if ev.get("verified") else 0
            cur.execute("""
                INSERT INTO evidence
                (paper_id, field_name, field_value, source_location,
                 source_text, confidence, verified, reason, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                paper_id,
                ev.get("field", ""),
                str(ev.get("value", ""))[:500],
                ev.get("source_location", ""),
                ev.get("source_text", ""),
                ev.get("confidence", ""),
                verified_int,
                ev.get("reason", ""),
                extracted_at,
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
                (paper_id, language, category, confidence, reasoning,
                 key_signals, crop_species, paper_type,
                 has_yield_data, research_country)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (paper_id) DO UPDATE SET
                    category = EXCLUDED.category, confidence = EXCLUDED.confidence,
                    reasoning = EXCLUDED.reasoning, key_signals = EXCLUDED.key_signals,
                    crop_species = EXCLUDED.crop_species, research_country = EXCLUDED.research_country
            """, (
                cls.get("paper_id"),
                cls.get("language"),
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
                code = issue.get("code", "") if isinstance(issue, dict) else ""
                message = issue.get("message", issue) if isinstance(issue, dict) else issue
                cur.execute(
                    "INSERT INTO validation_issues (paper_id, issue_type, severity, code, message) VALUES (%s, %s, %s, %s, %s)",
                    (paper_id, "issue", "error", code, message))
                row_count += 1

            for warning in report.get("warnings", []):
                code = warning.get("code", "") if isinstance(warning, dict) else ""
                message = warning.get("message", warning) if isinstance(warning, dict) else warning
                cur.execute(
                    "INSERT INTO validation_issues (paper_id, issue_type, severity, code, message) VALUES (%s, %s, %s, %s, %s)",
                    (paper_id, "warning", "warning", code, message))
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


def delete_pdf_missing(conn, paper_id: str):
    """论文已成功处理或被剔除时，从 pdf_missing 移除。

    pdf_missing 表只保留"当前仍卡住"的论文（确实无法获取任何全文资源）。
    一旦论文靠 MD 兜底成功提取（completed）或因非中国等原因被剔除（skipped），
    即视为已解决，从该表移除。DELETE 幂等，论文不在表中时为无害空操作。
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM pdf_missing WHERE paper_id = %s", (paper_id,))
    conn.commit()


def insert_search_results(conn, papers: List[dict]) -> int:
    """
    将搜索结果批量写入 paper_status 表（幂等）。

    已存在的论文（无论状态）不会被覆盖，确保：
      - 多实例搜索同一关键词不会产生重复
      - 正在处理或已完成的论文不会被重置为 pending

    Args:
        papers: 论文字典列表，需包含 paper_id, title 等字段。

    Returns:
        新插入的记录数（不含已存在被跳过的）。
    """
    if not papers:
        return 0

    from datetime import datetime
    now = datetime.now().isoformat()
    inserted = 0

    with conn.cursor() as cur:
        for p in papers:
            pid = p.get("paper_id", "")
            if not pid:
                continue
            cur.execute("""
                INSERT INTO paper_status
                    (paper_id, title, status, updated_at,
                     ss_paper_id, doi, abstract, publication_year, journal)
                VALUES (%s, %s, 'pending', %s, %s, %s, %s, %s, %s)
                ON CONFLICT (paper_id) DO NOTHING
            """, (
                pid,
                p.get("title", ""),
                now,
                p.get("ss_paper_id", ""),
                p.get("doi", ""),
                p.get("abstract", ""),
                p.get("publication_year", ""),
                p.get("journal", ""),
            ))
            inserted += cur.rowcount

    conn.commit()
    logger.info(
        f"Search results → paper_status: {inserted} new, "
        f"{len(papers) - inserted} already existed"
    )
    return inserted


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
                       "classification", "validation_issues", "evidence", "paper_status", "pdf_missing"):
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            stats[table] = cur.fetchone()[0]
    return stats


def get_progress(conn) -> dict:
    """
    查询论文处理进度（按状态和实例分组）。

    适用于大规模数据（15M+），全部走 SQL 聚合，不加载明细到内存。

    Returns:
        {
            "total": 150000,
            "active_total": 149000,   # pending+processing+completed（剔除 failed/skipped）
            "by_status": {"pending": 1000, "processing": 30, "completed": 140000, ...},
            "by_instance": {"instance-1": {"processing": 10, "completed": 50000}, ...},
            "completion_pct": 93.9,   # completed / active_total
        }
    """
    result = {"total": 0, "active_total": 0, "by_status": {}, "by_instance": {}, "completion_pct": 0.0}

    with conn.cursor() as cur:
        # 按状态分组统计
        cur.execute("""
            SELECT status, COUNT(*) FROM paper_status GROUP BY status
        """)
        for status, count in cur.fetchall():
            result["by_status"][status or "unknown"] = count
            result["total"] += count

        # 按实例分组统计（仅 processing/completed，了解各实例负载）
        cur.execute("""
            SELECT claimed_by, status, COUNT(*)
            FROM paper_status
            WHERE claimed_by IS NOT NULL
            GROUP BY claimed_by, status
        """)
        for instance, status, count in cur.fetchall():
            if instance not in result["by_instance"]:
                result["by_instance"][instance] = {}
            result["by_instance"][instance][status] = count

    # 活跃论文 = pending + processing + completed（剔除 failed/skipped，进度条口径）
    completed = result["by_status"].get("completed", 0)
    processing = result["by_status"].get("processing", 0)
    pending = result["by_status"].get("pending", 0)
    result["active_total"] = completed + processing + pending

    # 完成百分比基于活跃论文（failed/skipped 不计入分母）
    if result["active_total"] > 0:
        result["completion_pct"] = round(completed * 100.0 / result["active_total"], 2)

    return result
