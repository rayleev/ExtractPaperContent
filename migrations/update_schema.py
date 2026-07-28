"""数据库迁移脚本 - 更新表结构以匹配代码

新增：
- evidence 表（证据验证）
- validation_issues.code 字段（问题编码）
- papers.parse_context 字段（parse 节点输出 JSONB）

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

            # ── 1. 新增 evidence 表 ──
            print("1. 创建 evidence 表...")
            cur.execute("""
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
            """)
            print("   [OK] evidence 表已创建")

            # ── 2. 新增 validation_issues.code 字段 ──
            print("2. 添加 validation_issues.code 字段...")
            cur.execute("""
                ALTER TABLE validation_issues
                ADD COLUMN IF NOT EXISTS code TEXT;
            """)
            print("   [OK] validation_issues.code 字段已添加")

            # ── 3. 新增 papers.parse_context 字段 ──
            print("3. 添加 papers.parse_context 字段...")
            cur.execute("""
                ALTER TABLE papers
                ADD COLUMN IF NOT EXISTS parse_context JSONB;
            """)
            print("   [OK] papers.parse_context 字段已添加")

            # ── 4. 新增索引 ──
            print("4. 创建索引...")
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_evidence_paper ON evidence(paper_id);
                CREATE INDEX IF NOT EXISTS idx_evidence_field ON evidence(field_name);
            """)
            print("   [OK] 索引已创建")

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
