"""
FastAPI 应用入口 — Paper Extractor HTTP 服务。

启动方式：
  uvicorn src.api.main:app --host 0.0.0.0 --port 8000
  或
  python run.py --serve
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.api.routes import router


# ── 日志配置（API/Docker 模式）──────────────────────────────
# CLI 模式由 run.py 的 setup_logging() 配置；
# --serve 模式走这里，确保 pipeline 后台线程的日志不丢失。

def _setup_api_logging():
    """配置 paper_extractor logger（幂等，避免重复添加 handler）。"""
    _logger = logging.getLogger("paper_extractor")
    if _logger.handlers:
        return _logger  # 已配置过（如 CLI 模式先调用了 setup_logging）

    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # stdout handler（Docker log collector 会捕获）
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(fmt)
    _logger.addHandler(stdout_handler)

    # 文件 handler（可选，写入 output/logs/api.log）
    try:
        log_dir = PROJECT_ROOT / "output" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_dir / "api.log", encoding="utf-8")
        file_handler.setFormatter(fmt)
        _logger.addHandler(file_handler)
    except Exception:
        pass  # 文件写入失败不影响服务启动

    _logger.setLevel(level)
    _logger.info(f"API logging initialized: level={level_name}")
    return _logger


logger = _setup_api_logging()


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

    @app.on_event("startup")
    def _init_db():
        """服务启动时一次性初始化数据库 schema（建表、索引、注释）。"""
        try:
            from src.config import load_config
            from src.graph.output import init_database
            config = load_config()
            conn = init_database(config.database.connection_string)
            conn.close()
            logger.info("Database schema initialized at startup")
        except Exception as e:
            logger.warning(f"Database schema init failed at startup (will retry on first pipeline run): {e}")

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
        from src.graph.output import get_connection, get_table_stats

        db_ok = False
        db_info = ""
        try:
            config = load_config()
            conn = get_connection(config.database.connection_string)
            stats = get_table_stats(conn)
            conn.close()
            db_ok = True
            db_info = f"{stats.get('pe_core_papers', 0)} papers, {stats.get('pe_core_varieties', 0)} varieties"
        except Exception as e:
            db_info = str(e)[:200]

        return {
            "service": "paper-extractor",
            "database": "ok" if db_ok else "error",
            "database_info": db_info,
        }

    @app.get("/dashboard", tags=["dashboard"], response_class=HTMLResponse)
    def dashboard():
        """监控仪表盘 — 实例健康、处理进度、数据库统计、任务触发。"""
        html_path = Path(__file__).resolve().parent / "static" / "dashboard.html"
        return html_path.read_text(encoding="utf-8")

    return app


# 模块级 app 实例（uvicorn 直接引用）
app = create_app()
