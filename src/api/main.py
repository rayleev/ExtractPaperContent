"""
FastAPI 应用入口 — Paper Extractor HTTP 服务。

启动方式：
  uvicorn src.api.main:app --host 0.0.0.0 --port 8000
  或
  python run.py --serve
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.api.routes import router

logger = logging.getLogger("paper_extractor")


def create_app() -> FastAPI:
    """创建并配置 FastAPI 应用。"""

    app = FastAPI(
        title="Paper Extractor",
        description="科研文献结构化数据提取服务 — 论文搜索、PDF 下载、LLM 提取、PostgreSQL 落库",
        version="2.0.0",
    )

    # CORS（允许前端/DBeaver 等工具跨域访问）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 注册路由
    app.include_router(router)

    @app.get("/", tags=["health"])
    def root():
        """健康检查。"""
        return {
            "service": "paper-extractor",
            "version": "2.0.0",
            "status": "running",
        }

    @app.get("/health", tags=["health"])
    def health():
        """详细健康检查（含数据库连通性）。"""
        from src.config import load_config
        from src.graph.output import init_database, get_table_stats

        db_ok = False
        db_info = ""
        try:
            config = load_config()
            conn = init_database(config.database.connection_string)
            stats = get_table_stats(conn)
            conn.close()
            db_ok = True
            db_info = f"{stats.get('papers', 0)} papers, {stats.get('varieties', 0)} varieties"
        except Exception as e:
            db_info = str(e)[:200]

        return {
            "service": "paper-extractor",
            "database": "ok" if db_ok else "error",
            "database_info": db_info,
        }

    return app


# 模块级 app 实例（uvicorn 直接引用）
app = create_app()
