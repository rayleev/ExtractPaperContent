"""数据库迁移脚本 - 修复服务器表结构以匹配代码

变更：
- 删除 _schema_doc 表（不再使用）
- 新增 papers.parse_context (JSONB)
- 新增 validation_issues.code (TEXT)
- 新增 paper_status.publication_year (TEXT)
- 删除 paper_status.year (多余字段)

保留所有现有数据。
"""
import sys
sys.path.insert(0, '.')

from src.config import load_config
from src.graph.output import get_connection

def migrate():
    config = load_config()
    conn = get_connection(config.database.connection_string)

    try:
        with conn.cursor() as cur:
            print("开始数据库迁移...")

            # ── 1. 删除 _schema_doc 表 ──
            print("1. 删除 _schema_doc 表...")
            cur.execute("DROP TABLE IF EXISTS _schema_doc CASCADE;")
            print("   [OK] _schema_doc 表已删除")

            # ── 2. 新增 papers.parse_context 字段 ──
            print("2. 添加 papers.parse_context 字段...")
            cur.execute("""
                ALTER TABLE papers
                ADD COLUMN IF NOT EXISTS parse_context JSONB;
            """)
            print("   [OK] papers.parse_context 字段已添加")

            # ── 3. 新增 validation_issues.code 字段 ──
            print("3. 添加 validation_issues.code 字段...")
            cur.execute("""
                ALTER TABLE validation_issues
                ADD COLUMN IF NOT EXISTS code TEXT;
            """)
            print("   [OK] validation_issues.code 字段已添加")

            # ── 4. 新增 paper_status.publication_year 字段 ──
            print("4. 添加 paper_status.publication_year 字段...")
            cur.execute("""
                ALTER TABLE paper_status
                ADD COLUMN IF NOT EXISTS publication_year TEXT;
            """)
            print("   [OK] paper_status.publication_year 字段已添加")

            # ── 5. 删除 paper_status.year 字段（如果存在）──
            print("5. 删除 paper_status.year 字段...")
            cur.execute("""
                ALTER TABLE paper_status
                DROP COLUMN IF EXISTS year;
            """)
            print("   [OK] paper_status.year 字段已删除")

            conn.commit()
            print("\n数据库迁移完成！")

            # ── 验证 ──
            print("\n验证表结构：")
            cur.execute("""
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                ORDER BY table_name;
            """)
            tables = [row[0] for row in cur.fetchall()]
            for t in tables:
                print(f"   - {t}")

            # 验证数据保留
            print("\n数据保留情况：")
            for table in ['papers', 'studies', 'varieties', 'classification', 'validation_issues', 'paper_status']:
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                count = cur.fetchone()[0]
                print(f"   - {table}: {count} 行")

    except Exception as e:
        print(f"\n迁移失败: {e}")
        conn.rollback()
        raise
    finally:
        conn.close()

if __name__ == "__main__":
    migrate()
