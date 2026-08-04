"""
输出模块 — PostgreSQL 统一存储 + 统计报告。

所有提取结果、分类结果、验证报告、覆盖率统计统一写入 PostgreSQL 数据库，
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
  paper_coverage     每篇论文字段覆盖率（统计）
  field_coverage     每个字段全局命中率（统计）
  stats_summary      批次总体统计（统计）

用法：
  conn = init_database("postgresql://user:pass@host:5432/dbname")
  insert_extraction(conn, result, paper_id)   # 逐篇追加
  insert_classification(conn, records)         # 批量写入分类
  insert_validation(conn, results)             # 批量写入验证
  insert_statistics(conn, extractions)         # 批量写入覆盖率统计
"""

from __future__ import annotations
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import psycopg2
import psycopg2.extras

from src.core.models import ExtractionResult

logger = logging.getLogger("paper_extractor")


def extract_year_from_doi(doi: str) -> Optional[int]:
    """
    从 DOI 中提取发表年份。

    很多中文 DOI 格式为 10.xxxx/issn.xxxx.YYYYMMDD，
    其中 YYYY 部分即为发表年份。

    Args:
        doi: DOI 字符串

    Returns:
        年份整数，或 None（无法提取时）
    """
    if not doi:
        return None
    # 匹配 DOI 中的年份（如 20230217 → 2023）
    # 中文期刊 DOI 常见格式：10.16178/j.issn.0528-9017.20230217
    match = re.search(r'(\d{4})\d{4}', doi)
    if match:
        year = int(match.group(1))
        if 1990 <= year <= 2030:
            return year
    return None


# ── 建表 SQL（PostgreSQL 语法）────────────────────────────

_SCHEMA = """
-- 论文级数据
CREATE TABLE IF NOT EXISTS pe_core_papers (
    paper_id        TEXT PRIMARY KEY,
    doi             TEXT,
    title           TEXT,
    publication_year INTEGER,
    journal_name    TEXT,
    crop_species    TEXT,
    category        TEXT,
    data_file_link  TEXT,
    data_file_description TEXT,
    data_file_version TEXT,
    extracted_at    TEXT,
    parse_context   JSONB
);

-- 试验级数据
CREATE TABLE IF NOT EXISTS pe_core_studies (
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
CREATE TABLE IF NOT EXISTS pe_core_varieties (
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
    treatment_name  TEXT,
    n_raw_value     DOUBLE PRECISION,
    n_raw_unit      TEXT,
    p_raw_value     DOUBLE PRECISION,
    p_raw_unit      TEXT,
    k_raw_value     DOUBLE PRECISION,
    k_raw_unit      TEXT,
    n_standard_value DOUBLE PRECISION,
    p_standard_value DOUBLE PRECISION,
    k_standard_value DOUBLE PRECISION,
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
    treatment_name  TEXT,
    n_raw_value     DOUBLE PRECISION,
    n_raw_unit      TEXT,
    p_raw_value     DOUBLE PRECISION,
    p_raw_unit      TEXT,
    k_raw_value     DOUBLE PRECISION,
    k_raw_unit      TEXT,
    n_standard_value DOUBLE PRECISION,
    p_standard_value DOUBLE PRECISION,
    k_standard_value DOUBLE PRECISION,
    extracted_at    TEXT,
    PRIMARY KEY (paper_id, study_index, variety_index)
);

-- 论文分类结果
CREATE TABLE IF NOT EXISTS pe_aud_classification (
    paper_id        TEXT PRIMARY KEY,
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
CREATE TABLE IF NOT EXISTS pe_aud_validation_issues (
    id              SERIAL PRIMARY KEY,
    paper_id        TEXT NOT NULL,
    issue_type      TEXT NOT NULL,
    severity        TEXT NOT NULL,
    code            TEXT,
    message         TEXT
);

-- 证据验证明细
CREATE TABLE IF NOT EXISTS pe_aud_evidence (
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
CREATE TABLE IF NOT EXISTS pe_reg_paper_status (
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
CREATE TABLE IF NOT EXISTS pe_log_pdf_missing (
    paper_id        TEXT PRIMARY KEY,
    title           TEXT,
    doi             TEXT,
    reason          TEXT,
    attempted_at    TEXT
);

-- 每篇论文字段覆盖率（统计）
CREATE TABLE IF NOT EXISTS pe_aud_paper_coverage (
    paper_id        TEXT PRIMARY KEY,
    run_id          TEXT,
    total_fields    INTEGER,
    filled_fields   INTEGER,
    coverage        DOUBLE PRECISION,
    paper_level     DOUBLE PRECISION,
    study_level     DOUBLE PRECISION,
    variety_level   DOUBLE PRECISION,
    num_studies     INTEGER,
    num_varieties   INTEGER,
    generated_at    TEXT
);

-- 每个字段全局命中率（统计）
CREATE TABLE IF NOT EXISTS pe_aud_field_coverage (
    id              SERIAL PRIMARY KEY,
    run_id          TEXT,
    field_name      TEXT NOT NULL,
    level           TEXT NOT NULL,
    hit_count       INTEGER,
    miss_count      INTEGER,
    total_count     INTEGER,
    coverage        DOUBLE PRECISION,
    generated_at    TEXT
);

-- 批次总体统计（统计）
CREATE TABLE IF NOT EXISTS pe_aud_stats_summary (
    run_id          TEXT PRIMARY KEY,
    paper_count     INTEGER,
    average_coverage DOUBLE PRECISION,
    paper_level     DOUBLE PRECISION,
    study_level     DOUBLE PRECISION,
    variety_level   DOUBLE PRECISION,
    best_paper      TEXT,
    worst_paper     TEXT,
    top_missing_fields JSONB,
    total_studies   INTEGER,
    total_varieties INTEGER,
    generated_at    TEXT
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_pe_core_varieties_name ON pe_core_varieties(variety_name);
CREATE INDEX IF NOT EXISTS idx_pe_core_varieties_paper ON pe_core_varieties(paper_id);
CREATE INDEX IF NOT EXISTS idx_pe_core_studies_paper ON pe_core_studies(paper_id);
CREATE INDEX IF NOT EXISTS idx_pe_core_studies_region ON pe_core_studies(site_administrative_region);
CREATE INDEX IF NOT EXISTS idx_pe_core_studies_year ON pe_core_studies(trial_year);
CREATE INDEX IF NOT EXISTS idx_pe_aud_classification_cat ON pe_aud_classification(category);
CREATE INDEX IF NOT EXISTS idx_pe_aud_validation_paper ON pe_aud_validation_issues(paper_id);
CREATE INDEX IF NOT EXISTS idx_pe_aud_validation_severity ON pe_aud_validation_issues(severity);
CREATE INDEX IF NOT EXISTS idx_pe_aud_evidence_paper ON pe_aud_evidence(paper_id);
CREATE INDEX IF NOT EXISTS idx_pe_aud_evidence_field ON pe_aud_evidence(field_name);
CREATE INDEX IF NOT EXISTS idx_pe_reg_paper_status ON pe_reg_paper_status(status);
CREATE INDEX IF NOT EXISTS idx_pe_reg_paper_status_claimed ON pe_reg_paper_status(claimed_by);
CREATE INDEX IF NOT EXISTS idx_pe_log_pdf_missing ON pe_log_pdf_missing(paper_id);
CREATE INDEX IF NOT EXISTS idx_pe_aud_paper_coverage_run ON pe_aud_paper_coverage(run_id);
CREATE INDEX IF NOT EXISTS idx_pe_aud_field_coverage_run ON pe_aud_field_coverage(run_id);
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
            WHERE table_name = 'pe_aud_classification' AND column_name = 'doi'
        """)
        if cur.fetchone():
            cur.execute("DROP TABLE pe_aud_classification")
            logger.info("Dropped legacy pe_aud_classification table (removed redundant doi/title/year/journal columns)")

        for raw_statement in _SCHEMA.strip().split(";"):
            # 去除前导注释行（-- 开头的行），保留实际 SQL
            lines = raw_statement.strip().splitlines()
            sql_lines = [l for l in lines if not l.strip().startswith("--")]
            statement = "\n".join(sql_lines).strip()
            if statement:
                cur.execute(statement)

        # 兼容已有数据库：为 pe_reg_paper_status 补充搜索元数据列
        for col, col_type in [
            ("ss_paper_id", "TEXT"), ("doi", "TEXT"),
            ("abstract", "TEXT"), ("year", "TEXT"), ("journal", "TEXT"),
        ]:
            cur.execute(
                f"ALTER TABLE pe_reg_paper_status ADD COLUMN IF NOT EXISTS {col} {col_type}"
            )

    conn.commit()
    logger.info(f"Database initialized: {connection_string.split('@')[-1] if '@' in connection_string else connection_string}")
    return conn


# ── 写入函数 ──────────────────────────────────────────────

def insert_extraction(conn, result: dict, paper_id: str):
    """
    将一篇论文的提取结果写入数据库（pe_core_papers + pe_core_studies + pe_core_varieties + varieties_flat）。

    使用 ON CONFLICT DO UPDATE 实现幂等写入。
    线程安全：调用方需保证 conn 的线程安全（或使用锁）。
    """
    extraction = result.get("extraction", {})
    if not extraction:
        return

    paper = extraction.get("paper", {})
    studies = extraction.get("studies", [])
    meta = result.get("paper_meta", {})
    cls = result.get("classification", {})  # category 在分类子字典中
    extracted_at = result.get("extracted_at") or datetime.now().isoformat()

    with conn.cursor() as cur:
        # ── 重跑整篇覆盖：先清空该论文的旧提取行（delete-then-insert）──
        # upsert 在"新提取行数 < 旧行数"时会残留旧行（如 management_yield 改为
        # 只提对照组后 variety 行数减少，高 variety_index 的旧处理行会留下成脏数据）。
        # 故每次写入前按 paper_id 清空 pe_core_studies/pe_core_varieties/varieties_flat，再整篇插入。
        # pe_core_papers 表每个 paper_id 仅一行，直接 upsert 即可，无需删除。
        # pe_aud_evidence 同样按 paper_id 清空，避免重跑时累积重复证据行。
        for _table in ("pe_core_varieties", "varieties_flat", "pe_core_studies", "pe_aud_evidence"):
            cur.execute(f"DELETE FROM {_table} WHERE paper_id = %s", (paper_id,))

        # ── pe_core_papers 表 ──
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
            INSERT INTO pe_core_papers
            (paper_id, doi, title, publication_year, journal_name, crop_species,
             category, data_file_link, data_file_description, data_file_version,
             extracted_at, parse_context)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (paper_id) DO UPDATE SET
                doi = EXCLUDED.doi, title = EXCLUDED.title,
                publication_year = EXCLUDED.publication_year, journal_name = EXCLUDED.journal_name,
                crop_species = EXCLUDED.crop_species,
                category = EXCLUDED.category, extracted_at = EXCLUDED.extracted_at,
                parse_context = EXCLUDED.parse_context
        """, (
            paper_id,
            meta.get("doi") or paper.get("paper_doi"),
            meta.get("title") or paper.get("paper_title"),
            int(meta["year"]) if meta.get("year", "").isdigit() else paper.get("publication_year") or extract_year_from_doi(meta.get("doi") or paper.get("paper_doi")),
            meta.get("journal") or paper.get("journal_name"),
            paper.get("crop_species"),
            cls.get("category"),
            paper.get("data_file_link"),
            paper.get("data_file_description"),
            paper.get("data_file_version"),
            extracted_at,
            json.dumps(parse_context_data, ensure_ascii=False),
        ))

        # ── pe_core_studies 表 ──
        for si, study in enumerate(studies):
            cur.execute("""
                INSERT INTO pe_core_studies
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

            # ── pe_core_varieties + varieties_flat ──
            varieties = study.get("varieties", [])
            for vi, v in enumerate(varieties):
                is_ck = v.get("is_check_variety")
                is_ck_int = 1 if is_ck else (0 if is_ck is not None else None)

                cur.execute("""
                    INSERT INTO pe_core_varieties
                    (paper_id, study_index, variety_index, variety_name, variety_code,
                     is_check_variety, variety_source, yield_raw_value, yield_raw_unit,
                     yield_standard_value, yield_standard_unit, yield_value_type,
                     significance_group, pct_over_check, measurement_method,
                     source_location, confidence_level,
                     treatment_name, n_raw_value, n_raw_unit, p_raw_value, p_raw_unit,
                     k_raw_value, k_raw_unit, n_standard_value, p_standard_value, k_standard_value)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (paper_id, study_index, variety_index) DO UPDATE SET
                        variety_name = EXCLUDED.variety_name, variety_code = EXCLUDED.variety_code,
                        yield_raw_value = EXCLUDED.yield_raw_value,
                        yield_standard_value = EXCLUDED.yield_standard_value,
                        pct_over_check = EXCLUDED.pct_over_check,
                        treatment_name = EXCLUDED.treatment_name,
                        n_raw_value = EXCLUDED.n_raw_value, p_raw_value = EXCLUDED.p_raw_value,
                        k_raw_value = EXCLUDED.k_raw_value
                """, (
                    paper_id, si, vi,
                    v.get("variety_name"), v.get("variety_code"), is_ck_int,
                    v.get("variety_source"), v.get("yield_raw_value"),
                    v.get("yield_raw_unit"), v.get("yield_standard_value"),
                    v.get("yield_standard_unit", "kg/ha"), v.get("yield_value_type"),
                    v.get("significance_group"), v.get("pct_over_check"),
                    v.get("measurement_method"), v.get("source_location"),
                    v.get("confidence_level"),
                    v.get("treatment_name"),
                    v.get("n_raw_value"), v.get("n_raw_unit"),
                    v.get("p_raw_value"), v.get("p_raw_unit"),
                    v.get("k_raw_value"), v.get("k_raw_unit"),
                    v.get("n_standard_value"), v.get("p_standard_value"),
                    v.get("k_standard_value"),
                ))

                # 宽表
                cur.execute("""
                    INSERT INTO varieties_flat
                    (paper_id, study_index, variety_index,
                     doi, paper_title, publication_year, journal_name, crop_species,
                     category,
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
                     confidence_level,
                     treatment_name, n_raw_value, n_raw_unit, p_raw_value, p_raw_unit,
                     k_raw_value, k_raw_unit, n_standard_value, p_standard_value, k_standard_value,
                     extracted_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (paper_id, study_index, variety_index) DO UPDATE SET
                        variety_name = EXCLUDED.variety_name,
                        yield_standard_value = EXCLUDED.yield_standard_value,
                        extracted_at = EXCLUDED.extracted_at,
                        treatment_name = EXCLUDED.treatment_name,
                        n_raw_value = EXCLUDED.n_raw_value, p_raw_value = EXCLUDED.p_raw_value,
                        k_raw_value = EXCLUDED.k_raw_value
                """, (
                    paper_id, si, vi,
                    meta.get("doi") or paper.get("paper_doi"),
                    meta.get("title") or paper.get("paper_title"),
                    int(meta["year"]) if meta.get("year", "").isdigit() else paper.get("publication_year"),
                    meta.get("journal") or paper.get("journal_name"),
                    paper.get("crop_species"), cls.get("category"),
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
                    v.get("confidence_level"),
                    v.get("treatment_name"),
                    v.get("n_raw_value"), v.get("n_raw_unit"),
                    v.get("p_raw_value"), v.get("p_raw_unit"),
                    v.get("k_raw_value"), v.get("k_raw_unit"),
                    v.get("n_standard_value"), v.get("p_standard_value"),
                    v.get("k_standard_value"),
                    extracted_at,
                ))

        # ── pe_aud_evidence 表 ──
        evidence_nodes = result.get("evidence_nodes", [])
        for ev in evidence_nodes:
            verified_int = 1 if ev.get("verified") else 0
            cur.execute("""
                INSERT INTO pe_aud_evidence
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
                INSERT INTO pe_aud_classification
                (paper_id, category, confidence, reasoning,
                 key_signals, crop_species, paper_type,
                 has_yield_data, research_country)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (paper_id) DO UPDATE SET
                    category = EXCLUDED.category, confidence = EXCLUDED.confidence,
                    reasoning = EXCLUDED.reasoning, key_signals = EXCLUDED.key_signals,
                    crop_species = EXCLUDED.crop_species, research_country = EXCLUDED.research_country
            """, (
                cls.get("paper_id"),
                cls.get("category"), cls.get("confidence"), cls.get("reasoning"),
                key_signals, cls.get("crop_species"), cls.get("paper_type"),
                has_yield_int, cls.get("research_country"),
            ))

    conn.commit()
    logger.info(f"Classification: {len(records)} records written to DB")


def insert_validation(conn, results: List[dict]):
    """将验证报告扁平化写入 pe_aud_validation_issues 表（按 paper_id 覆盖）。"""
    if not results:
        return

    # 收集本批次涉及的 paper_id，仅按 paper_id 删除旧记录，避免误清其他论文的验证数据
    paper_ids = [r.get("paper_id", "") for r in results if r.get("paper_id")]
    if not paper_ids:
        return

    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM pe_aud_validation_issues WHERE paper_id = ANY(%s)",
            (paper_ids,),
        )

        row_count = 0
        for record in results:
            paper_id = record.get("paper_id", "")
            report = record.get("validation_report", {})

            for issue in report.get("issues", []):
                code = issue.get("code", "") if isinstance(issue, dict) else ""
                message = issue.get("message", issue) if isinstance(issue, dict) else issue
                cur.execute(
                    "INSERT INTO pe_aud_validation_issues (paper_id, issue_type, severity, code, message) VALUES (%s, %s, %s, %s, %s)",
                    (paper_id, "issue", "error", code, message))
                row_count += 1

            for warning in report.get("warnings", []):
                code = warning.get("code", "") if isinstance(warning, dict) else ""
                message = warning.get("message", warning) if isinstance(warning, dict) else warning
                cur.execute(
                    "INSERT INTO pe_aud_validation_issues (paper_id, issue_type, severity, code, message) VALUES (%s, %s, %s, %s, %s)",
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
            INSERT INTO pe_reg_paper_status
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
            INSERT INTO pe_log_pdf_missing (paper_id, title, doi, reason, attempted_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (paper_id) DO UPDATE SET
                reason = EXCLUDED.reason, attempted_at = EXCLUDED.attempted_at
        """, (paper_id, title, doi, reason, datetime.now().isoformat()))
    conn.commit()


def delete_pdf_missing(conn, paper_id: str):
    """论文已成功处理或被剔除时，从 pe_log_pdf_missing 移除。

    pe_log_pdf_missing 表只保留"当前仍卡住"的论文（确实无法获取任何全文资源）。
    一旦论文靠 MD 兜底成功提取（completed）或因非中国等原因被剔除（skipped），
    即视为已解决，从该表移除。DELETE 幂等，论文不在表中时为无害空操作。
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM pe_log_pdf_missing WHERE paper_id = %s", (paper_id,))
    conn.commit()


def insert_search_results(conn, papers: List[dict]) -> int:
    """
    将搜索结果批量写入 pe_reg_paper_status 表（幂等）。

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
                INSERT INTO pe_reg_paper_status
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
            SELECT paper_id FROM pe_reg_paper_status
            WHERE status = 'pending'
            ORDER BY paper_id
            LIMIT %s
            FOR UPDATE SKIP LOCKED
        """, (limit,))
        paper_ids = [row[0] for row in cur.fetchall()]

        if paper_ids:
            # 标记为 processing 并记录领取者
            cur.execute("""
                UPDATE pe_reg_paper_status
                SET status = 'processing', claimed_by = %s, updated_at = %s
                WHERE paper_id = ANY(%s)
            """, (instance_id, datetime.now().isoformat(), paper_ids))

    conn.commit()
    return paper_ids


# ── 统计写入 ──────────────────────────────────────────────

def insert_statistics(conn, extractions: List[dict], run_id: str = ""):
    """
    计算覆盖率统计并写入数据库（pe_aud_paper_coverage + pe_aud_field_coverage + pe_aud_stats_summary）。

    按 run_id 分批覆盖：写入前删除该 run_id 下的旧统计记录。

    Args:
        extractions: 提取结果列表（每个元素包含 paper_id + extraction dict）
        run_id: 批次运行 ID
    """
    from src.output.statistics import (
        compute_paper_coverage,
        compute_field_coverage,
        compute_summary,
        _get_all_fields,
    )

    if not extractions:
        logger.warning("No extraction data to compute statistics")
        return

    logger.info(f"Computing statistics for {len(extractions)} papers...")

    all_fields = _get_all_fields()
    paper_cov = compute_paper_coverage(extractions, all_fields)
    field_cov = compute_field_coverage(extractions, all_fields)
    summary = compute_summary(paper_cov, field_cov, all_fields)
    generated_at = datetime.now().isoformat()

    try:
        with conn.cursor() as cur:
            # 清理同 run_id 旧记录
            if run_id:
                cur.execute("DELETE FROM pe_aud_paper_coverage WHERE run_id = %s", (run_id,))
                cur.execute("DELETE FROM pe_aud_field_coverage WHERE run_id = %s", (run_id,))
                cur.execute("DELETE FROM pe_aud_stats_summary WHERE run_id = %s", (run_id,))

            # 1. 每篇论文覆盖率
            for p in paper_cov:
                cur.execute("""
                    INSERT INTO pe_aud_paper_coverage
                    (paper_id, run_id, total_fields, filled_fields, coverage,
                     paper_level, study_level, variety_level,
                     num_studies, num_varieties, generated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (paper_id) DO UPDATE SET
                        run_id = EXCLUDED.run_id,
                        total_fields = EXCLUDED.total_fields,
                        filled_fields = EXCLUDED.filled_fields,
                        coverage = EXCLUDED.coverage,
                        paper_level = EXCLUDED.paper_level,
                        study_level = EXCLUDED.study_level,
                        variety_level = EXCLUDED.variety_level,
                        num_studies = EXCLUDED.num_studies,
                        num_varieties = EXCLUDED.num_varieties,
                        generated_at = EXCLUDED.generated_at
                """, (
                    p.get("paper_id", ""), run_id,
                    p.get("total_fields", 0), p.get("filled_fields", 0),
                    p.get("coverage", 0.0),
                    p.get("paper_level", 0.0), p.get("study_level", 0.0),
                    p.get("variety_level", 0.0),
                    p.get("num_studies", 0), p.get("num_varieties", 0),
                    generated_at,
                ))

            # 2. 每字段命中率
            for f in field_cov:
                cur.execute("""
                    INSERT INTO pe_aud_field_coverage
                    (run_id, field_name, level, hit_count, miss_count,
                     total_count, coverage, generated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """, (
                    run_id,
                    f.get("field_name", ""), f.get("level", ""),
                    f.get("hit_count", 0), f.get("miss_count", 0),
                    f.get("total_count", 0), f.get("coverage", 0.0),
                    generated_at,
                ))

            # 3. 批次汇总
            top_missing = summary.get("top_missing_fields", [])
            cur.execute("""
                INSERT INTO pe_aud_stats_summary
                (run_id, paper_count, average_coverage, paper_level, study_level,
                 variety_level, best_paper, worst_paper, top_missing_fields,
                 total_studies, total_varieties, generated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (run_id) DO UPDATE SET
                    paper_count = EXCLUDED.paper_count,
                    average_coverage = EXCLUDED.average_coverage,
                    paper_level = EXCLUDED.paper_level,
                    study_level = EXCLUDED.study_level,
                    variety_level = EXCLUDED.variety_level,
                    best_paper = EXCLUDED.best_paper,
                    worst_paper = EXCLUDED.worst_paper,
                    top_missing_fields = EXCLUDED.top_missing_fields,
                    total_studies = EXCLUDED.total_studies,
                    total_varieties = EXCLUDED.total_varieties,
                    generated_at = EXCLUDED.generated_at
            """, (
                run_id,
                summary.get("paper_count", 0),
                summary.get("average_coverage", 0.0),
                summary.get("paper_level", 0.0),
                summary.get("study_level", 0.0),
                summary.get("variety_level", 0.0),
                summary.get("best_paper", ""),
                summary.get("worst_paper", ""),
                json.dumps(top_missing, ensure_ascii=False),
                summary.get("total_studies", 0),
                summary.get("total_varieties", 0),
                generated_at,
            ))

        conn.commit()
        logger.info(
            f"Statistics → DB: {len(paper_cov)} paper_coverage, "
            f"{len(field_cov)} field_coverage, 1 summary (run_id={run_id})"
        )
    except Exception as e:
        logger.error(
            f"insert_statistics failed: {e}, "
            f"extractions count={len(extractions)}, run_id={run_id}",
            exc_info=True,
        )
        raise


def get_table_stats(conn) -> dict:
    """获取各表的行数统计。"""
    stats = {}
    with conn.cursor() as cur:
        for table in ("pe_core_papers", "pe_core_studies", "pe_core_varieties", "varieties_flat",
                       "pe_aud_classification", "pe_aud_validation_issues", "pe_aud_evidence", "pe_reg_paper_status", "pe_log_pdf_missing"):
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
            SELECT status, COUNT(*) FROM pe_reg_paper_status GROUP BY status
        """)
        for status, count in cur.fetchall():
            result["by_status"][status or "unknown"] = count
            result["total"] += count

        # 按实例分组统计（仅 processing/completed，了解各实例负载）
        cur.execute("""
            SELECT claimed_by, status, COUNT(*)
            FROM pe_reg_paper_status
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
