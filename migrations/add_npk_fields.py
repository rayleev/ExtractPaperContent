"""
迁移脚本 — 为 pe_core_varieties 与 varieties_flat 添加氮磷钾(NPK)施用量字段

新增字段（两表各 10 列）：
  treatment_name   TEXT              处理名称（如 N0/N180/CK/滴灌）
  n_raw_value      DOUBLE PRECISION  纯氮施用量原始数值
  n_raw_unit       TEXT              氮施用量原始单位
  p_raw_value      DOUBLE PRECISION  纯磷(P2O5)施用量原始数值
  p_raw_unit       TEXT              磷施用量原始单位
  k_raw_value      DOUBLE PRECISION  纯钾(K2O)施用量原始数值
  k_raw_unit       TEXT              钾施用量原始单位
  n_standard_value DOUBLE PRECISION  程序换算的 kg N/ha（本期留空）
  p_standard_value DOUBLE PRECISION  程序换算的 kg P2O5/ha（本期留空）
  k_standard_value DOUBLE PRECISION  程序换算的 kg K2O/ha（本期留空）

用法:
  python -m migrations.add_npk_fields "postgresql://user:pass@host:5432/dbname"

或在 Python 中调用:
  from migrations.add_npk_fields import run_migration
  run_migration(connection_string)
"""

from __future__ import annotations
import sys
import logging

logger = logging.getLogger("paper_extractor")


MIGRATION_SQL = """
ALTER TABLE pe_core_varieties
    ADD COLUMN IF NOT EXISTS treatment_name   TEXT,
    ADD COLUMN IF NOT EXISTS n_raw_value      DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS n_raw_unit       TEXT,
    ADD COLUMN IF NOT EXISTS p_raw_value      DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS p_raw_unit       TEXT,
    ADD COLUMN IF NOT EXISTS k_raw_value      DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS k_raw_unit       TEXT,
    ADD COLUMN IF NOT EXISTS n_standard_value DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS p_standard_value DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS k_standard_value DOUBLE PRECISION;

ALTER TABLE varieties_flat
    ADD COLUMN IF NOT EXISTS treatment_name   TEXT,
    ADD COLUMN IF NOT EXISTS n_raw_value      DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS n_raw_unit       TEXT,
    ADD COLUMN IF NOT EXISTS p_raw_value      DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS p_raw_unit       TEXT,
    ADD COLUMN IF NOT EXISTS k_raw_value      DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS k_raw_unit       TEXT,
    ADD COLUMN IF NOT EXISTS n_standard_value DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS p_standard_value DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS k_standard_value DOUBLE PRECISION;
"""

VERIFY_SQL = """
SELECT table_name, column_name
FROM information_schema.columns
WHERE table_name IN ('pe_core_varieties', 'varieties_flat')
  AND column_name IN ('treatment_name','n_raw_value','n_raw_unit',
                      'p_raw_value','p_raw_unit','k_raw_value','k_raw_unit',
                      'n_standard_value','p_standard_value','k_standard_value')
ORDER BY table_name, column_name;
"""


def run_migration(connection_string: str) -> bool:
    """
    执行迁移：为 pe_core_varieties 与 varieties_flat 添加 NPK 字段。

    Args:
        connection_string: PostgreSQL 连接字符串

    Returns:
        迁移成功返回 True，失败返回 False
    """
    import psycopg2

    conn = None
    try:
        conn = psycopg2.connect(connection_string)
        with conn.cursor() as cur:
            logger.info("执行迁移: 为 pe_core_varieties 与 varieties_flat 添加 NPK 字段（10 列 × 2 表）")
            cur.execute(MIGRATION_SQL)
            conn.commit()

            cur.execute(VERIFY_SQL)
            rows = cur.fetchall()
            expected = 10
            by_table = {}
            for tbl, col in rows:
                by_table.setdefault(tbl, []).append(col)
            ok = all(len(cols) == expected for cols in by_table.values()) and len(by_table) == 2
            if ok:
                logger.info(
                    "迁移成功: pe_core_varieties 与 varieties_flat 各新增 "
                    f"{expected} 列: {sorted(by_table['pe_core_varieties'])}"
                )
                return True
            else:
                logger.error(f"迁移验证失败: 期望每表 {expected} 列，实际 {by_table}")
                return False

    except Exception as e:
        logger.error(f"迁移失败: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python -m migrations.add_npk_fields <connection_string>")
        print("示例: python -m migrations.add_npk_fields \"postgresql://user:pass@host:5432/dbname\"")
        sys.exit(1)

    connection_string = sys.argv[1]
    success = run_migration(connection_string)
    sys.exit(0 if success else 1)
