"""
数据库 schema 定义和迁移脚本。

用法：
    python migrations/schema.py                    # 执行迁移
    python migrations/schema.py --check            # 仅检查，不执行
    python migrations/schema.py --connection "..." # 指定连接字符串

变更历史：
    2024-08-06: 初始版本，包含所有核心表结构
"""

import argparse
import logging
import os
import sys

# 添加项目根目录到 path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("schema_migration")

# ── 默认连接字符串（可通过命令行覆盖）──────────────────────
DEFAULT_CONNECTION = "postgresql://postgres:Admin123!@10.33.105.145:5432/paper_extractor"

# ── 表结构定义 ────────────────────────────────────────────

TABLES = {
    "pe_core_papers": {
        "comment": "论文级数据。一篇论文一行，存储论文基本信息和分类结果。",
        "columns": {
            "paper_id": ("TEXT", "NOT NULL", "论文唯一标识（MD5 哈希）"),
            "doi": ("TEXT", "", "DOI"),
            "title": ("TEXT", "", "论文标题"),
            "publication_year": ("INTEGER", "", "发表年份"),
            "journal_name": ("TEXT", "", "期刊名称"),
            "crop_species": ("TEXT", "", "作物种类（如水稻、玉米）"),
            "category": ("TEXT", "", "论文分类（varietal_yield/management_yield/remote_sensing_yield）"),
            "data_file_link": ("TEXT", "", "数据文件链接"),
            "data_file_description": ("TEXT", "", "数据文件描述"),
            "data_file_version": ("TEXT", "", "数据文件版本"),
            "extracted_at": ("TEXT", "", "提取时间 ISO8601"),
            "parse_context": ("JSONB", "", "parse 节点输出（doc_context + extraction_hints）"),
        },
        "primary_key": ("paper_id",),
    },
    "pe_core_studies": {
        "comment": "试验级数据。一篇论文包含多个试验，每个试验一行。",
        "columns": {
            "paper_id": ("TEXT", "NOT NULL", "论文唯一标识"),
            "study_index": ("TEXT", "NOT NULL", "试验序号（格式：S_01, S_02...）"),
            "study_title": ("TEXT", "", "试验标题"),
            "study_description": ("TEXT", "", "试验描述"),
            "trial_year": ("TEXT", "", "试验年份"),
            "sowing_date": ("TEXT", "", "播种日期"),
            "harvest_date": ("TEXT", "", "收获日期"),
            "country": ("TEXT", "", "国家"),
            "site_administrative_region": ("TEXT", "", "试验地点（行政区划）"),
            "experimental_site_name": ("TEXT", "", "试验地点名称"),
            "latitude": ("DOUBLE PRECISION", "", "纬度"),
            "longitude": ("DOUBLE PRECISION", "", "经度"),
            "altitude": ("DOUBLE PRECISION", "", "海拔"),
            "geo_source": ("TEXT", "", "坐标来源"),
            "replication_number": ("INTEGER", "", "重复次数"),
            "plot_size": ("TEXT", "", "小区面积"),
            "planting_density": ("TEXT", "", "种植密度"),
            "experimental_design_description": ("TEXT", "", "试验设计描述"),
            "experimental_design_type": ("TEXT", "", "试验设计类型"),
            "growth_facility_description": ("TEXT", "", "栽培设施描述"),
            "cultural_practices": ("TEXT", "", "栽培措施"),
            "notes": ("TEXT", "", "备注"),
        },
        "primary_key": ("paper_id", "study_index"),
    },
    "pe_core_varieties": {
        "comment": "品种产量数据（主数据表）。一行一个品种-处理组合。",
        "columns": {
            "paper_id": ("TEXT", "NOT NULL", "论文唯一标识"),
            "study_index": ("TEXT", "NOT NULL", "试验序号"),
            "variety_index": ("TEXT", "NOT NULL", "品种序号（格式：V_01, V_02...，同一品种不同处理共享）"),
            "variety_name": ("TEXT", "", "品种名称"),
            "variety_code": ("TEXT", "", "品种编号"),
            "is_check_variety": ("INTEGER", "", "是否对照品种（1=是，0=否）"),
            "variety_source": ("TEXT", "", "品种来源"),
            "yield_raw_value": ("DOUBLE PRECISION", "", "原始产量值"),
            "yield_raw_unit": ("TEXT", "", "原始产量单位"),
            "yield_standard_value": ("DOUBLE PRECISION", "", "标准产量值（换算为 kg/ha）"),
            "yield_standard_unit": ("TEXT", "", "标准产量单位", "'kg/ha'"),
            "yield_value_type": ("TEXT", "", "产量值类型"),
            "significance_group": ("TEXT", "", "显著性分组"),
            "pct_over_check": ("DOUBLE PRECISION", "", "较对照增产率（%）"),
            "measurement_method": ("TEXT", "", "测定方法"),
            "source_location": ("TEXT", "", "数据来源位置"),
            "confidence_level": ("TEXT", "", "置信水平"),
            "treatment_name": ("TEXT", "NOT NULL", "处理名称（非管理类论文填 'Not management yield'）"),
            "n_raw_value": ("DOUBLE PRECISION", "", "纯氮施用量原始值"),
            "n_raw_unit": ("TEXT", "", "纯氮施用量单位"),
            "p_raw_value": ("DOUBLE PRECISION", "", "纯磷施用量原始值"),
            "p_raw_unit": ("TEXT", "", "纯磷施用量单位"),
            "k_raw_value": ("DOUBLE PRECISION", "", "纯钾施用量原始值"),
            "k_raw_unit": ("TEXT", "", "纯钾施用量单位"),
            "nutrient_source_location": ("TEXT", "", "施肥数据来源位置"),
            "n_standard_value": ("DOUBLE PRECISION", "", "纯氮施用量标准值（kg/ha）"),
            "p_standard_value": ("DOUBLE PRECISION", "", "纯磷施用量标准值（kg/ha）"),
            "k_standard_value": ("DOUBLE PRECISION", "", "纯钾施用量标准值（kg/ha）"),
        },
        "primary_key": ("paper_id", "study_index", "variety_index", "treatment_name"),
    },
    "varieties_flat": {
        "comment": "品种产量宽表（扁平化，用于交接导出）。包含论文、试验、品种全部字段。",
        "columns": {
            "paper_id": ("TEXT", "NOT NULL", "论文唯一标识"),
            "study_index": ("TEXT", "NOT NULL", "试验序号"),
            "variety_index": ("TEXT", "NOT NULL", "品种序号"),
            "doi": ("TEXT", "", "DOI"),
            "paper_title": ("TEXT", "", "论文标题"),
            "publication_year": ("INTEGER", "", "发表年份"),
            "journal_name": ("TEXT", "", "期刊名称"),
            "crop_species": ("TEXT", "", "作物种类"),
            "category": ("TEXT", "", "论文分类"),
            "study_title": ("TEXT", "", "试验标题"),
            "trial_year": ("TEXT", "", "试验年份"),
            "sowing_date": ("TEXT", "", "播种日期"),
            "harvest_date": ("TEXT", "", "收获日期"),
            "country": ("TEXT", "", "国家"),
            "site_administrative_region": ("TEXT", "", "试验地点"),
            "experimental_site_name": ("TEXT", "", "试验地点名称"),
            "latitude": ("DOUBLE PRECISION", "", "纬度"),
            "longitude": ("DOUBLE PRECISION", "", "经度"),
            "altitude": ("DOUBLE PRECISION", "", "海拔"),
            "geo_source": ("TEXT", "", "坐标来源"),
            "replication_number": ("INTEGER", "", "重复次数"),
            "plot_size": ("TEXT", "", "小区面积"),
            "planting_density": ("TEXT", "", "种植密度"),
            "experimental_design_description": ("TEXT", "", "试验设计描述"),
            "experimental_design_type": ("TEXT", "", "试验设计类型"),
            "growth_facility_description": ("TEXT", "", "栽培设施描述"),
            "cultural_practices": ("TEXT", "", "栽培措施"),
            "study_notes": ("TEXT", "", "试验备注"),
            "variety_name": ("TEXT", "", "品种名称"),
            "variety_code": ("TEXT", "", "品种编号"),
            "is_check_variety": ("INTEGER", "", "是否对照品种"),
            "variety_source": ("TEXT", "", "品种来源"),
            "yield_raw_value": ("DOUBLE PRECISION", "", "原始产量值"),
            "yield_raw_unit": ("TEXT", "", "原始产量单位"),
            "yield_standard_value": ("DOUBLE PRECISION", "", "标准产量值"),
            "yield_standard_unit": ("TEXT", "", "标准产量单位", "'kg/ha'"),
            "yield_value_type": ("TEXT", "", "产量值类型"),
            "significance_group": ("TEXT", "", "显著性分组"),
            "pct_over_check": ("DOUBLE PRECISION", "", "较对照增产率"),
            "measurement_method": ("TEXT", "", "测定方法"),
            "source_location": ("TEXT", "", "数据来源位置"),
            "confidence_level": ("TEXT", "", "置信水平"),
            "treatment_name": ("TEXT", "NOT NULL", "处理名称"),
            "n_raw_value": ("DOUBLE PRECISION", "", "纯氮施用量"),
            "n_raw_unit": ("TEXT", "", "纯氮单位"),
            "p_raw_value": ("DOUBLE PRECISION", "", "纯磷施用量"),
            "p_raw_unit": ("TEXT", "", "纯磷单位"),
            "k_raw_value": ("DOUBLE PRECISION", "", "纯钾施用量"),
            "k_raw_unit": ("TEXT", "", "纯钾单位"),
            "nutrient_source_location": ("TEXT", "", "施肥数据来源"),
            "n_standard_value": ("DOUBLE PRECISION", "", "纯氮标准值"),
            "p_standard_value": ("DOUBLE PRECISION", "", "纯磷标准值"),
            "k_standard_value": ("DOUBLE PRECISION", "", "纯钾标准值"),
            "extracted_at": ("TEXT", "", "提取时间"),
        },
        "primary_key": ("paper_id", "study_index", "variety_index", "treatment_name"),
    },
    "pe_aud_classification": {
        "comment": "论文分类结果。存储 LLM 分类的输出。",
        "columns": {
            "paper_id": ("TEXT", "NOT NULL", "论文唯一标识"),
            "category": ("TEXT", "", "分类结果"),
            "confidence": ("DOUBLE PRECISION", "", "分类置信度"),
            "reasoning": ("TEXT", "", "分类推理过程"),
            "key_signals": ("TEXT", "", "关键信号"),
            "crop_species": ("TEXT", "", "作物种类"),
            "paper_type": ("TEXT", "", "论文类型"),
            "has_yield_data": ("INTEGER", "", "是否有产量数据"),
            "research_country": ("TEXT", "", "研究国家"),
        },
        "primary_key": ("paper_id",),
    },
    "pe_aud_validation_issues": {
        "comment": "验证问题明细（扁平化）。存储规则验证发现的问题。",
        "columns": {
            "id": ("SERIAL", "NOT NULL", "自增主键"),
            "paper_id": ("TEXT", "NOT NULL", "论文唯一标识"),
            "issue_type": ("TEXT", "NOT NULL", "问题类型（issue/warning）"),
            "severity": ("TEXT", "NOT NULL", "严重程度"),
            "code": ("TEXT", "", "问题编码"),
            "message": ("TEXT", "", "问题描述"),
        },
        "primary_key": ("id",),
    },
    "pe_aud_evidence": {
        "comment": "证据验证明细。联合主键：(paper_id, study_index, variety_index, treatment_name, field_name)",
        "columns": {
            "paper_id": ("TEXT", "NOT NULL", "论文唯一标识"),
            "study_index": ("TEXT", "NOT NULL", "试验序号"),
            "variety_index": ("TEXT", "NOT NULL", "品种序号"),
            "field_name": ("TEXT", "NOT NULL", "字段名称"),
            "field_value": ("TEXT", "", "字段值"),
            "treatment_name": ("TEXT", "NOT NULL", "处理名称"),
            "source_location": ("TEXT", "", "来源位置"),
            "source_text": ("TEXT", "", "原文片段"),
            "confidence": ("TEXT", "", "置信度"),
            "verified": ("INTEGER", "", "是否已验证"),
            "reason": ("TEXT", "", "验证原因"),
            "created_at": ("TEXT", "", "创建时间"),
        },
        "primary_key": ("paper_id", "study_index", "variety_index", "treatment_name", "field_name"),
    },
    "pe_reg_paper_status": {
        "comment": "论文处理状态（兼任务协调注册表）。跟踪每篇论文的处理进度。",
        "columns": {
            "paper_id": ("TEXT", "NOT NULL", "论文唯一标识"),
            "title": ("TEXT", "", "论文标题"),
            "target_step": ("TEXT", "", "目标步骤"),
            "status": ("TEXT", "", "处理状态", "'pending'"),
            "claimed_by": ("TEXT", "", "处理者标识"),
            "duration_sec": ("DOUBLE PRECISION", "", "处理耗时（秒）"),
            "error_message": ("TEXT", "", "错误信息"),
            "run_id": ("TEXT", "", "运行批次 ID"),
            "updated_at": ("TEXT", "", "更新时间"),
            "ss_paper_id": ("TEXT", "", "搜索阶段论文 ID"),
            "doi": ("TEXT", "", "DOI"),
            "abstract": ("TEXT", "", "摘要"),
            "publication_year": ("TEXT", "", "发表年份"),
            "journal": ("TEXT", "", "期刊名称"),
            "year": ("TEXT", "", "年份"),
        },
        "primary_key": ("paper_id",),
    },
    "pe_log_pdf_missing": {
        "comment": "无法获取 PDF 的论文记录。",
        "columns": {
            "paper_id": ("TEXT", "NOT NULL", "论文唯一标识"),
            "title": ("TEXT", "", "论文标题"),
            "doi": ("TEXT", "", "DOI"),
            "reason": ("TEXT", "", "失败原因"),
            "attempted_at": ("TEXT", "", "尝试时间"),
        },
        "primary_key": ("paper_id",),
    },
    "pe_aud_paper_coverage": {
        "comment": "每篇论文字段覆盖率（统计）。",
        "columns": {
            "paper_id": ("TEXT", "NOT NULL", "论文唯一标识"),
            "run_id": ("TEXT", "", "运行批次 ID"),
            "total_fields": ("INTEGER", "", "总字段数"),
            "filled_fields": ("INTEGER", "", "已填字段数"),
            "coverage": ("DOUBLE PRECISION", "", "覆盖率"),
            "paper_level": ("DOUBLE PRECISION", "", "论文级覆盖率"),
            "study_level": ("DOUBLE PRECISION", "", "试验级覆盖率"),
            "variety_level": ("DOUBLE PRECISION", "", "品种级覆盖率"),
            "num_studies": ("INTEGER", "", "试验数量"),
            "num_varieties": ("INTEGER", "", "品种数量"),
            "generated_at": ("TEXT", "", "生成时间"),
        },
        "primary_key": ("paper_id",),
    },
    "pe_aud_field_coverage": {
        "comment": "每个字段全局命中率（统计）。",
        "columns": {
            "id": ("SERIAL", "NOT NULL", "自增主键"),
            "run_id": ("TEXT", "", "运行批次 ID"),
            "field_name": ("TEXT", "NOT NULL", "字段名称"),
            "level": ("TEXT", "NOT NULL", "字段级别"),
            "hit_count": ("INTEGER", "", "命中次数"),
            "miss_count": ("INTEGER", "", "未命中次数"),
            "total_count": ("INTEGER", "", "总次数"),
            "coverage": ("DOUBLE PRECISION", "", "命中率"),
            "generated_at": ("TEXT", "", "生成时间"),
        },
        "primary_key": ("id",),
    },
    "pe_aud_stats_summary": {
        "comment": "批次总体统计。",
        "columns": {
            "run_id": ("TEXT", "NOT NULL", "运行批次 ID"),
            "paper_count": ("INTEGER", "", "论文数量"),
            "average_coverage": ("DOUBLE PRECISION", "", "平均覆盖率"),
            "paper_level": ("DOUBLE PRECISION", "", "论文级覆盖率"),
            "study_level": ("DOUBLE PRECISION", "", "试验级覆盖率"),
            "variety_level": ("DOUBLE PRECISION", "", "品种级覆盖率"),
            "best_paper": ("TEXT", "", "最佳论文"),
            "worst_paper": ("TEXT", "", "最差论文"),
            "top_missing_fields": ("JSONB", "", "缺失最多的字段"),
            "total_studies": ("INTEGER", "", "试验总数"),
            "total_varieties": ("INTEGER", "", "品种总数"),
            "generated_at": ("TEXT", "", "生成时间"),
        },
        "primary_key": ("run_id",),
    },
}

# ── 索引定义 ──────────────────────────────────────────────

INDEXES = [
    ("idx_pe_core_varieties_name", "pe_core_varieties(variety_name)"),
    ("idx_pe_core_varieties_paper", "pe_core_varieties(paper_id)"),
    ("idx_pe_core_studies_paper", "pe_core_studies(paper_id)"),
    ("idx_pe_core_studies_region", "pe_core_studies(site_administrative_region)"),
    ("idx_pe_core_studies_year", "pe_core_studies(trial_year)"),
    ("idx_pe_aud_classification_cat", "pe_aud_classification(category)"),
    ("idx_pe_aud_validation_paper", "pe_aud_validation_issues(paper_id)"),
    ("idx_pe_aud_validation_severity", "pe_aud_validation_issues(severity)"),
    ("idx_pe_aud_evidence_paper", "pe_aud_evidence(paper_id)"),
    ("idx_pe_aud_evidence_field", "pe_aud_evidence(field_name)"),
    ("idx_pe_aud_evidence_study", "pe_aud_evidence(paper_id, study_index)"),
    ("idx_pe_aud_evidence_treatment", "pe_aud_evidence(paper_id, study_index, treatment_name)"),
    ("idx_pe_reg_paper_status", "pe_reg_paper_status(status)"),
    ("idx_pe_reg_paper_status_claimed", "pe_reg_paper_status(claimed_by)"),
    ("idx_pe_log_pdf_missing", "pe_log_pdf_missing(paper_id)"),
    ("idx_pe_aud_paper_coverage_run", "pe_aud_paper_coverage(run_id)"),
    ("idx_pe_aud_field_coverage_run", "pe_aud_field_coverage(run_id)"),
]


def create_table_sql(table_name: str, table_def: dict) -> str:
    """生成 CREATE TABLE 语句"""
    columns = []
    for col_name, col_def in table_def["columns"].items():
        col_type = col_def[0]
        col_null = col_def[1] if len(col_def) > 1 else ""
        col_default = f"DEFAULT {col_def[3]}" if len(col_def) > 3 and col_def[3] else ""
        columns.append(f"    {col_name:30s} {col_type:20s} {col_null:10s} {col_default}")

    pk_cols = ", ".join(table_def["primary_key"])
    columns.append(f"    PRIMARY KEY ({pk_cols})")

    return f"CREATE TABLE IF NOT EXISTS {table_name} (\n" + ",\n".join(columns) + "\n);"


def migrate_database(connection_string: str, dry_run: bool = False):
    """
    执行数据库迁移。

    Args:
        connection_string: PostgreSQL 连接字符串
        dry_run: 如果为 True，只打印 SQL 不执行
    """
    conn = psycopg2.connect(connection_string)
    conn.autocommit = False

    try:
        with conn.cursor() as cur:
            # 1. 创建/更新表结构
            for table_name, table_def in TABLES.items():
                create_sql = create_table_sql(table_name, table_def)
                if dry_run:
                    print(f"-- {table_name}: {table_def['comment']}")
                    print(create_sql)
                else:
                    cur.execute(create_sql)

                # 添加表注释
                if table_def.get("comment"):
                    cur.execute(
                        f"COMMENT ON TABLE {table_name} IS %s",
                        (table_def["comment"],)
                    )

                # 添加列注释
                for col_name, col_def in table_def["columns"].items():
                    if len(col_def) > 2 and col_def[2]:
                        cur.execute(
                            f"COMMENT ON COLUMN {table_name}.{col_name} IS %s",
                            (col_def[2],)
                        )

            # 2. 创建索引
            for idx_name, idx_def in INDEXES:
                table_name = idx_def.split("(")[0]
                cur.execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {idx_def}")

            # 3. 迁移：确保 treatment_name 非空
            for table_name in ("pe_core_varieties", "varieties_flat"):
                cur.execute(f"""
                    UPDATE {table_name} SET treatment_name = 'Not management yield'
                    WHERE treatment_name IS NULL
                """)

            # 4. 迁移：确保 pe_aud_evidence 的 study_index/variety_index/treatment_name 非空
            cur.execute("""
                UPDATE pe_aud_evidence SET study_index = '' WHERE study_index IS NULL
            """)
            cur.execute("""
                UPDATE pe_aud_evidence SET variety_index = '' WHERE variety_index IS NULL
            """)
            cur.execute("""
                UPDATE pe_aud_evidence SET treatment_name = '' WHERE treatment_name IS NULL
            """)

        if not dry_run:
            conn.commit()
            logger.info("Database migration completed successfully")
        else:
            logger.info("Dry run completed")

    except Exception as e:
        conn.rollback()
        logger.error(f"Migration failed: {e}")
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Database schema migration")
    parser.add_argument("--check", action="store_true", help="Dry run (print SQL only)")
    parser.add_argument("--connection", default=DEFAULT_CONNECTION, help="PostgreSQL connection string")
    args = parser.parse_args()

    migrate_database(args.connection, dry_run=args.check)


if __name__ == "__main__":
    main()
