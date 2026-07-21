"""
路由定义 — FastAPI 路由处理函数。

提供 HTTP API 触发 pipeline、查询进度、获取统计信息。
所有长时间运行的 pipeline 任务在后台线程中执行。
"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from fastapi import APIRouter, HTTPException

from src.api.schemas import (
    RunRequest, RunResponse, JobStatus,
    TableStats, PaperStatusResponse,
)

logger = logging.getLogger("paper_extractor")

router = APIRouter(prefix="/api", tags=["pipeline"])

# ── 任务追踪（内存存储，服务重启后清空）──────────────────

_jobs: Dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _get_job(job_id: str) -> Optional[dict]:
    with _jobs_lock:
        return _jobs.get(job_id)


def _update_job(job_id: str, **kwargs):
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(kwargs)


# ── Pipeline 后台执行器 ─────────────────────────────────

def _run_pipeline(job_id: str, request: RunRequest, config_override: dict = None):
    """
    在后台线程中执行完整的 pipeline 流程。

    Args:
        job_id: 任务标识符
        request: API 请求参数
        config_override: 可选的配置覆盖（如 keywords, year_range）
    """
    import sys
    # 确保项目根目录在 sys.path 中
    project_root = Path(__file__).resolve().parent.parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    from src.config import load_config
    from src.clients.llm import LLMClient
    from src.clients.mineru import MinerUClient
    from src.clients.semantic_scholar import SemanticScholarClient
    from src.core.geocoder import Geocoder
    from src.core.loader import discover_papers
    from src.graph.batch import BatchOrchestrator
    from src.graph.nodes.search import search_node
    from src.graph.state import PaperState

    _update_job(job_id, status="running", started_at=datetime.now().isoformat())

    try:
        # ── 加载配置 ──
        config = load_config()
        config.set_run_id()

        # 应用 API 请求中的覆盖参数
        if request.keywords:
            config.extraction.search_keywords = request.keywords
        if request.year_range:
            config.extraction.search_year_range = request.year_range

        # 步骤映射
        step_to_stop = {
            "search": "search",
            "classify": "classify",
            "download": "download",
            "parse": "parse",
            "extract": "",
            "all": "",
        }
        stop_after = step_to_stop.get(request.step, "")

        logger.info(f"[Job {job_id[:8]}] Starting pipeline: step={request.step}")

        # ── 初始化客户端 ──
        llm = LLMClient(config.llm)

        # Semantic Scholar 客户端
        ss_client = SemanticScholarClient(
            base_url=config.semantic_scholar.base_url,
            api_key=config.semantic_scholar.api_key,
            max_retries=config.semantic_scholar.max_retries,
            request_interval=config.semantic_scholar.request_interval,
        )

        # ── 搜索阶段（在 BatchOrchestrator 之前执行）──
        papers = []
        if request.step in ("search", "all"):
            _update_job(job_id, step="search")
            empty_state: PaperState = {"paper_id": "", "paper_meta": {}, "status": "pending", "errors": []}
            search_result = search_node(empty_state, config, ss_client)

            if search_result.get("status") == "search_empty":
                _update_job(job_id,
                    status="completed", step="search",
                    stats={"total": 0, "completed": 0, "failed": 0, "skipped": 0},
                    finished_at=datetime.now().isoformat(),
                    message="搜索未返回任何结果",
                )
                return

            # 将搜索结果转换为 paper dict 格式（兼容 discover_papers 的输出）
            import hashlib, re
            for sp in search_result.get("search_results", []):
                paper_id_raw = sp.get("paperId", "")
                title = sp.get("title", "")
                # 生成稳定 paper_id（与 loader.py 逻辑一致）
                normalized = re.sub(r'\s+', '', title).lower()
                fingerprint = hashlib.md5(normalized.encode("utf-8")).hexdigest()[:10]
                paper = {
                    "paper_id": f"P_{fingerprint}",
                    "ss_paper_id": paper_id_raw,
                    "doi": sp.get("doi", ""),
                    "title": title,
                    "abstract": sp.get("abstract", ""),
                    "keywords": sp.get("keywords", ""),
                    "year": str(sp.get("publicationYear", "")),
                    "journal": sp.get("journal", ""),
                    "language": "en",  # SS 论文默认英文
                }
                papers.append(paper)

            logger.info(f"[Job {job_id[:8]}] Search: {len(papers)} papers found")

            if request.step == "search":
                # 仅搜索，到此结束
                _update_job(job_id,
                    status="completed", step="search",
                    stats={"total": len(papers), "completed": len(papers), "failed": 0, "skipped": 0},
                    finished_at=datetime.now().isoformat(),
                    message=f"搜索完成: {len(papers)} 篇论文",
                )
                return
        else:
            # 非搜索步骤：从本地文件系统发现论文
            papers = discover_papers(config)

        # ── 论文过滤 ──
        if request.paper_filter:
            keyword = request.paper_filter.lower()
            papers = [
                p for p in papers
                if keyword in (p.get("doi", "") + p.get("title", "")).lower()
            ]
            logger.info(f"[Job {job_id[:8]}] Filtered to {len(papers)} papers matching '{request.paper_filter}'")

        if not papers:
            _update_job(job_id,
                status="completed",
                stats={"total": 0, "completed": 0, "failed": 0, "skipped": 0},
                finished_at=datetime.now().isoformat(),
                message="没有找到匹配的论文",
            )
            return

        # ── 初始化 MinerU / Geocoder ──
        mineru_client = None
        has_pdf = any(p.get("pdf_path") and not p.get("md_path") for p in papers)
        if has_pdf:
            mineru_client = MinerUClient(config.mineru)

        geocoder = Geocoder(config) if config.geocoding.enabled else None

        # ── 运行 BatchOrchestrator ──
        _update_job(job_id, step="extract")
        orchestrator = BatchOrchestrator(
            config=config,
            llm=llm,
            geocoder=geocoder,
            mineru_client=mineru_client,
            max_concurrent=config.concurrency.extract_workers,
            stop_after=stop_after,
        )

        stats = orchestrator.process_batch(papers=papers)

        _update_job(job_id,
            status="completed",
            step=request.step,
            stats=stats,
            finished_at=datetime.now().isoformat(),
            message=f"完成: {stats['completed']} ok, {stats['failed']} failed, {stats['skipped']} skipped",
        )

    except Exception as e:
        logger.error(f"[Job {job_id[:8]}] Pipeline failed: {e}", exc_info=True)
        _update_job(job_id,
            status="failed",
            error=str(e)[:500],
            finished_at=datetime.now().isoformat(),
        )


# ── 路由定义 ──────────────────────────────────────────────

@router.post("/run", response_model=RunResponse)
def trigger_run(request: RunRequest):
    """
    触发 pipeline 运行。

    在后台线程中执行，立即返回 job_id。
    通过 GET /api/status/{job_id} 查询进度。
    """
    job_id = str(uuid.uuid4())

    with _jobs_lock:
        _jobs[job_id] = {
            "job_id": job_id,
            "status": "accepted",
            "step": request.step,
            "stats": None,
            "started_at": None,
            "finished_at": None,
            "error": None,
            "message": "",
        }

    # 启动后台线程
    thread = threading.Thread(
        target=_run_pipeline,
        args=(job_id, request),
        daemon=True,
    )
    thread.start()

    return RunResponse(
        job_id=job_id,
        status="accepted",
        message=f"Pipeline '{request.step}' started in background",
    )


@router.get("/status/{job_id}", response_model=JobStatus)
def get_job_status(job_id: str):
    """查询指定任务的运行状态和进度。"""
    job = _get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return JobStatus(**job)


@router.get("/jobs", response_model=list[JobStatus])
def list_jobs():
    """列出所有任务（当前进程内的，服务重启后清空）。"""
    with _jobs_lock:
        return [JobStatus(**j) for j in _jobs.values()]


@router.get("/stats", response_model=TableStats)
def get_stats():
    """获取 PostgreSQL 各表的行数统计。"""
    from src.config import load_config
    from src.graph.output import init_database, get_table_stats

    config = load_config()
    try:
        conn = init_database(config.database.connection_string)
        stats = get_table_stats(conn)
        conn.close()
        return TableStats(**stats)
    except Exception as e:
        logger.error(f"Stats query failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


@router.get("/status", response_model=list[PaperStatusResponse])
def get_paper_statuses(
    status: Optional[str] = None,
    limit: int = 100,
):
    """
    查询论文处理状态列表。

    可按状态过滤（pending/processing/completed/failed/skipped），
    默认返回最新 100 条。
    """
    from src.config import load_config
    from src.graph.output import init_database

    config = load_config()
    try:
        conn = init_database(config.database.connection_string)
        with conn.cursor() as cur:
            if status:
                cur.execute(
                    "SELECT paper_id, title, target_step, status, duration_sec, error_message, updated_at "
                    "FROM paper_status WHERE status = %s ORDER BY updated_at DESC LIMIT %s",
                    (status, limit),
                )
            else:
                cur.execute(
                    "SELECT paper_id, title, target_step, status, duration_sec, error_message, updated_at "
                    "FROM paper_status ORDER BY updated_at DESC LIMIT %s",
                    (limit,),
                )
            rows = cur.fetchall()
        conn.close()

        return [
            PaperStatusResponse(
                paper_id=r[0], title=r[1], target_step=r[2],
                status=r[3], duration_sec=r[4], error_message=r[5],
                updated_at=r[6],
            )
            for r in rows
        ]
    except Exception as e:
        logger.error(f"Paper status query failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


@router.get("/status/{paper_id}/detail", response_model=PaperStatusResponse)
def get_paper_status(paper_id: str):
    """查询单篇论文的处理状态。"""
    from src.config import load_config
    from src.graph.output import init_database

    config = load_config()
    try:
        conn = init_database(config.database.connection_string)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT paper_id, title, target_step, status, duration_sec, error_message, updated_at "
                "FROM paper_status WHERE paper_id = %s",
                (paper_id,),
            )
            row = cur.fetchone()
        conn.close()

        if not row:
            raise HTTPException(status_code=404, detail=f"Paper '{paper_id}' not found")

        return PaperStatusResponse(
            paper_id=row[0], title=row[1], target_step=row[2],
            status=row[3], duration_sec=row[4], error_message=row[5],
            updated_at=row[6],
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Paper status query failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}")
