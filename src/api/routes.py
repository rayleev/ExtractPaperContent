"""
路由定义 — FastAPI 路由处理函数。

提供 HTTP API 触发 pipeline、查询进度、获取统计信息。
所有长时间运行的 pipeline 任务在后台线程中执行。
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from fastapi import APIRouter, HTTPException

from src.api.schemas import (
    RunRequest, RunResponse, JobStatus, StopResponse,
    TableStats, PaperStatusResponse, ProgressResponse,
    ImportRequest, ImportResponse,
)

logger = logging.getLogger("paper_extractor")

router = APIRouter(prefix="/api", tags=["pipeline"])

# ── 任务追踪（内存存储，服务重启后清空）──────────────────

_jobs: Dict[str, dict] = {}
_jobs_lock = threading.Lock()

# 全局停止标志：/api/stop 设置，BatchOrchestrator 在分块循环中检查
_stop_event = threading.Event()


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
            "process": "",  # 仅处理库中 pending 论文，不触发搜索
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

        # ── 搜索阶段：结果直接写入 paper_status 表 ──
        if request.step in ("search", "all"):
            _update_job(job_id, step="search")

            # 轻量 DB 连接（搜索入库需要，DDL 已在 startup 完成）
            from src.graph.output import get_connection
            search_conn = get_connection(config.database.connection_string)
            try:
                empty_state: PaperState = {"paper_id": "", "paper_meta": {}, "status": "pending", "errors": []}
                search_result = search_node(empty_state, config, ss_client, db_conn=search_conn, limit=request.limit)
            finally:
                search_conn.close()

            if search_result.get("status") == "search_empty":
                _update_job(job_id,
                    status="completed", step="search",
                    stats={"total": 0, "completed": 0, "failed": 0, "skipped": 0},
                    finished_at=datetime.now().isoformat(),
                    message="搜索未返回任何结果",
                )
                return

            search_total = search_result.get("search_total", 0)
            search_new = search_result.get("search_new", 0)
            logger.info(
                f"[Job {job_id[:8]}] Search: {search_total} papers found, "
                f"{search_new} new in DB"
            )

            if request.step == "search":
                _update_job(job_id,
                    status="completed", step="search",
                    stats={"total": search_total, "completed": search_new, "failed": 0, "skipped": search_total - search_new},
                    finished_at=datetime.now().isoformat(),
                    message=f"搜索完成: {search_total} 篇论文, 新入库 {search_new} 篇",
                )
                return

        # ── 处理阶段 ──
        mineru_client = MinerUClient(config.mineru)
        geocoder = Geocoder(config) if config.geocoding.enabled else None

        _update_job(job_id, step=request.step)
        orchestrator = BatchOrchestrator(
            config=config,
            llm=llm,
            geocoder=geocoder,
            mineru_client=mineru_client,
            ss_client=ss_client,
            max_concurrent=config.concurrency.extract_workers,
            stop_after=stop_after,
            stop_event=_stop_event,
        )

        if request.step in ("search", "all", "process"):
            # DB 驱动：从 paper_status 分块拉取 pending 论文处理
            stats = orchestrator.process_from_db(chunk_size=100)
        else:
            # 本地文件驱动：从文件系统发现论文
            papers = discover_papers(config)

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

            # 按需初始化 MinerU（本地 PDF 需要解析）
            has_pdf = any(p.get("pdf_path") and not p.get("md_path") for p in papers)
            if has_pdf:
                orchestrator.mineru_client = MinerUClient(config.mineru)

            stats = orchestrator.process_batch(papers=papers)

        # 检查是否被用户停止
        if _stop_event.is_set():
            _update_job(job_id,
                status="stopped",
                step=request.step,
                stats=stats,
                finished_at=datetime.now().isoformat(),
                message="用户手动停止",
            )
            return

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


def _run_import(job_id: str, ss_paper_ids: list):
    """
    在后台线程中执行按 SS paperId 的批量导入。

    拉取元数据 → 写入 paper_status 表（status='pending'，幂等去重）。
    导入完成后论文即可通过 process 步骤处理。

    Args:
        job_id: 任务标识符。
        ss_paper_ids: SS paperId 列表。
    """
    import sys
    project_root = Path(__file__).resolve().parent.parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    from src.config import load_config
    from src.clients.semantic_scholar import SemanticScholarClient
    from src.graph.nodes.search import import_papers_by_ids
    from src.graph.output import get_connection

    _update_job(job_id, status="running", started_at=datetime.now().isoformat())

    try:
        config = load_config()
        ss_client = SemanticScholarClient(
            base_url=config.semantic_scholar.base_url,
            api_key=config.semantic_scholar.api_key,
            max_retries=config.semantic_scholar.max_retries,
            request_interval=config.semantic_scholar.request_interval,
        )

        conn = get_connection(config.database.connection_string)
        try:
            result = import_papers_by_ids(ss_paper_ids, config, ss_client, conn)
        finally:
            conn.close()

        # failed_ids 可能很大，写入 job stats 时只保留前 100 个作样本
        stats = {k: v for k, v in result.items() if k != "failed_ids"}
        stats["failed_ids_sample"] = result.get("failed_ids", [])[:100]

        _update_job(job_id,
            status="completed",
            step="import",
            stats=stats,
            finished_at=datetime.now().isoformat(),
            message=(
                f"导入完成: 新入库 {result['new']} 篇, "
                f"已存在 {result['existed']} 篇, 未查到 {result['failed']} 个 ID"
            ),
        )

    except Exception as e:
        logger.error(f"[Job {job_id[:8]}] Import failed: {e}", exc_info=True)
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

    # 新任务启动前清除停止标志（防止上次 stop 影响新任务）
    _stop_event.clear()

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


@router.post("/import", response_model=ImportResponse)
def trigger_import(request: ImportRequest):
    """
    按 SS paperId 批量导入论文。

    在后台线程中拉取元数据并写入 paper_status 表（status='pending'），
    立即返回 job_id。通过 GET /api/status/{job_id} 查询进度。
    导入完成的论文可通过 process 步骤处理。
    """
    job_id = str(uuid.uuid4())

    with _jobs_lock:
        _jobs[job_id] = {
            "job_id": job_id,
            "status": "accepted",
            "step": "import",
            "stats": None,
            "started_at": None,
            "finished_at": None,
            "error": None,
            "message": "",
        }

    thread = threading.Thread(
        target=_run_import,
        args=(job_id, request.ss_paper_ids),
        daemon=True,
    )
    thread.start()

    return ImportResponse(
        job_id=job_id,
        status="accepted",
        message=f"Import of {len(request.ss_paper_ids)} IDs started in background",
    )


@router.post("/stop", response_model=StopResponse)
def stop_pipeline():
    """
    停止当前正在运行的 pipeline。

    设置全局停止标志，BatchOrchestrator 在完成当前分块后停止领取新论文。
    同时将 processing 状态的论文重置为 pending，以便后续重新处理。
    """
    _stop_event.set()
    logger.info("Stop signal received — pipeline will halt after current chunk")

    # 将本实例正在处理的论文重置为 pending（其他实例自行重置）
    reset_count = 0
    try:
        from src.config import load_config
        from src.graph.output import get_connection

        config = load_config()
        instance_id = os.environ.get("INSTANCE_ID", "default")
        conn = get_connection(config.database.connection_string)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE pe_reg_paper_status SET status = 'pending', claimed_by = NULL, updated_at = %s "
                    "WHERE status = 'processing' AND claimed_by = %s",
                    (datetime.now().isoformat(), instance_id),
                )
                reset_count = cur.rowcount
            conn.commit()
        finally:
            conn.close()
        logger.info(f"Reset {reset_count} processing papers to pending (instance={instance_id})")
    except Exception as e:
        logger.error(f"Failed to reset processing papers: {e}")

    # 将内存中 running 状态的任务标记为 stopped
    with _jobs_lock:
        for job in _jobs.values():
            if job["status"] in ("running", "accepted"):
                job["status"] = "stopped"
                job["finished_at"] = datetime.now().isoformat()
                job["message"] = "用户手动停止"

    return StopResponse(
        status="stopped",
        message=f"停止信号已发送，当前分块处理完后停止。已重置 {reset_count} 篇 processing 论文为 pending。",
        reset_count=reset_count,
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
    from src.graph.output import get_connection, get_table_stats

    config = load_config()
    conn = None
    try:
        conn = get_connection(config.database.connection_string)
        stats = get_table_stats(conn)
        return TableStats(**stats)
    except Exception as e:
        logger.error(f"Stats query failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}")
    finally:
        if conn is not None:
            conn.close()


@router.get("/progress", response_model=ProgressResponse)
def get_progress():
    """
    查询全局处理进度。

    按状态分组（pending/processing/completed/failed/skipped），
    按实例分组（各实例的领取和完成数量），以及总体完成百分比。
    适用于大规模数据（15M+），全部走 SQL 聚合。
    """
    from src.config import load_config
    from src.graph.output import get_connection, get_progress as _get_progress

    config = load_config()
    conn = None
    try:
        conn = get_connection(config.database.connection_string)
        progress = _get_progress(conn)
        return ProgressResponse(**progress)
    except Exception as e:
        logger.error(f"Progress query failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}")
    finally:
        if conn is not None:
            conn.close()


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
    from src.graph.output import get_connection

    config = load_config()
    conn = None
    try:
        conn = get_connection(config.database.connection_string)
        with conn.cursor() as cur:
            if status:
                cur.execute(
                    "SELECT paper_id, title, target_step, status, duration_sec, error_message, updated_at "
                    "FROM pe_reg_paper_status WHERE status = %s ORDER BY updated_at DESC LIMIT %s",
                    (status, limit),
                )
            else:
                cur.execute(
                    "SELECT paper_id, title, target_step, status, duration_sec, error_message, updated_at "
                    "FROM pe_reg_paper_status ORDER BY updated_at DESC LIMIT %s",
                    (limit,),
                )
            rows = cur.fetchall()

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
    finally:
        if conn is not None:
            conn.close()


@router.get("/status/{paper_id}/detail", response_model=PaperStatusResponse)
def get_paper_status(paper_id: str):
    """查询单篇论文的处理状态。"""
    from src.config import load_config
    from src.graph.output import get_connection

    config = load_config()
    conn = None
    try:
        conn = get_connection(config.database.connection_string)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT paper_id, title, target_step, status, duration_sec, error_message, updated_at "
                "FROM pe_reg_paper_status WHERE paper_id = %s",
                (paper_id,),
            )
            row = cur.fetchone()

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
    finally:
        if conn is not None:
            conn.close()
