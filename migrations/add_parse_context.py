"""
迁移脚本 — 添加 papers.parse_context JSONB 字段

用法:
  python -m migrations.add_parse_context "postgresql://user:pass@host:5432/dbname"

或在 Python 中调用:
  from migrations.add_parse_context import run_migration
  run_migration(connection_string)
"""

from __future__ import annotations
import sys
import logging

logger = logging.getLogger("paper_extractor")


MIGRATION_SQL = """
ALTER TABLE papers ADD COLUMN IF NOT EXISTS parse_context JSONB;
"""

VERIFY_SQL = """
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'papers' AND column_name = 'parse_context';
"""


def run_migration(connection_string: str) -> bool:
    """
    执行迁移：添加 parse_context JSONB 字段。

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
            # 执行迁移
            logger.info("执行迁移: ALTER TABLE papers ADD COLUMN parse_context JSONB")
            cur.execute(MIGRATION_SQL)
            conn.commit()

            # 验证迁移
            cur.execute(VERIFY_SQL)
            row = cur.fetchone()
            if row:
                logger.info(f"迁移成功: papers.parse_context ({row[1]})")
                return True
            else:
                logger.error("迁移失败: 未找到 parse_context 列")
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
        print("用法: python -m migrations.add_parse_context <connection_string>")
        print("示例: python -m migrations.add_parse_context \"postgresql://user:pass@host:5432/dbname\"")
        sys.exit(1)

    connection_string = sys.argv[1]
    success = run_migration(connection_string)
    sys.exit(0 if success else 1)
