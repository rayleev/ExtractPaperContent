"""
数据库迁移脚本 — 将旧 schema 迁移到新 schema。

变更：
1. pe_core_varieties / varieties_flat: 新增 nutrient_source_location, treatment_name 列
2. pe_core_studies / pe_core_varieties / varieties_flat: study_index / variety_index 从 INTEGER 改为 TEXT
3. 主键从 (paper_id, study_index, variety_index) 改为 (paper_id, study_index, variety_index, treatment_name)
4. pe_aud_evidence: 去掉 serial id 主键，改为联合主键 (paper_id, study_index, variety_index, treatment_name, field_name)

使用方法：
    python migrations/migrate_to_new_schema.py
"""

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from migrations.schema import migrate_database

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("migration")


def main():
    conn_str = "postgresql://postgres:Admin123!@10.33.105.145:5432/paper_extractor"
    logger.info(f"Starting migration: {conn_str}")
    try:
        migrate_database(conn_str)
        logger.info("Migration completed successfully")
    except Exception as e:
        logger.error(f"Migration failed: {e}")
        raise


if __name__ == "__main__":
    main()
