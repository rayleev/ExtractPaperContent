"""
BatchOrchestrator — 管理大规模论文批量处理。

功能：
  - 多论文并发处理（ThreadPoolExecutor）
  - SQLite checkpoint 断点续跑（LangGraph 内部，独立于输出库）
  - 逐篇写入 PostgreSQL 输出数据库（实时持久化）
  - 批次完成后生成验证报告 + 覆盖率统计 + CSV 导出
  - 步骤级注册表，支持分步执行和自动补全
  - 多实例支持（通过 INSTANCE_ID 环境变量标识）
"""

from __future__ import annotations
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from src.config import AppConfig
from src.clients.llm import LLMClient
from src.clients.mineru import MinerUClient
from src.core.geocoder import Geocoder
from src.graph.state import PaperState
from src.graph.graph import build_paper_graph
from src.graph.output import (
    init_database,
    insert_extraction,
    insert_classification,
    insert_validation,
    insert_pdf_missing,
    claim_tasks,
    export_table_csv,
    export_delivery_csv,
    get_table_stats,
    update_paper_status,
)

logger = logging.getLogger("paper_extractor")


class BatchOrchestrator:
    """批量论文处理编排器。"""

    def __init__(
        self,
        config: AppConfig,
        llm: LLMClient,
        geocoder: Geocoder,
        mineru_client: Optional[MinerUClient] = None,
        ss_client=None,
        max_concurrent: int = 10,
        stop_after: str = "",
    ):
        self.config = config
        self.llm = llm
        self.geocoder = geocoder
        self.mineru_client = mineru_client
        self.ss_client = ss_client
        self.max_concurrent = max_concurrent
        self.stop_after = stop_after  # 分步执行：在此节点后停止（空 = 完整流程）

        # Checkpoint 路径（LangGraph 断点续跑）
        self.checkpoint_path = str(config.cache_path / "langgraph_checkpoint.db")

        # 步骤优先级（数值越大 = 完成度越高）
        self._STEP_ORDER = {"classify": 1, "parse": 2, "extract": 3}

        # 输出目录
        self.output_dir = config.extraction_path
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # PostgreSQL 输出数据库（所有结果统一存储，含注册表功能）
        self._db_lock = threading.Lock()
        self.db_conn = init_database(config.database.connection_string)

        # 多实例标识（用于任务领取 claim_tasks）
        self.instance_id = os.environ.get("INSTANCE_ID", "default")

        # 统计
        self.stats = {
            "total": 0,
            "completed": 0,
            "failed": 0,
            "skipped": 0,
            "started_at": None,
        }

        # 收集完成的结果，用于批次报告
        self._completed_results: List[dict] = []

    def process_batch(
        self,
        papers: List[dict],
        classifications: Optional[List[dict]] = None,
    ) -> dict:
        """
        批量处理论文列表。

        每篇论文独立运行完整的 graph，通过 ThreadPoolExecutor 并发。
        完成后生成验证报告、覆盖率统计和 CSV 导出。
        """
        self.stats["total"] = len(papers)
        self.stats["started_at"] = datetime.now().isoformat()

        logger.info(
            f"BatchOrchestrator starting: {len(papers)} papers, "
            f"max_concurrent={self.max_concurrent}"
        )

        # 构建分类查找表
        cls_lookup = {}
        if classifications:
            for cls in classifications:
                cls_lookup[cls.get("paper_id", "")] = cls

        # 过滤已完成的论文（从 DB 的 paper_status 表查询注册表状态）
        target_step = self.stop_after if self.stop_after in self._STEP_ORDER else "extract"
        target_order = self._STEP_ORDER.get(target_step, 3)

        completed_registry = self._load_registry_from_db(
            paper_ids=[p.get("paper_id", "") for p in papers if p.get("paper_id")]
        )

        to_process = []
        for paper in papers:
            pid = paper.get("paper_id", "")
            reg_step = completed_registry.get(pid, "")
            reg_order = self._STEP_ORDER.get(reg_step, 0)

            if reg_order >= target_order:
                self.stats["skipped"] += 1
                logger.info(f"  SKIP ({reg_step} done): {pid} — {paper.get('title', '')[:50]}")
            else:
                to_process.append(paper)

        skipped_by_registry = len(papers) - len(to_process)
        if skipped_by_registry:
            logger.info(
                f"Registry: {skipped_by_registry} papers already at '{target_step}' or beyond, "
                f"{len(to_process)} to process"
            )

        # ── 原子领取（多实例防重）──
        # 通过 PG 行锁（FOR UPDATE SKIP LOCKED）确保每篇论文只被一个实例处理，
        # 即使多个实例同时启动、同时看到论文未完成，也只有一个能抢到。
        claimed_ids = self._claim_papers_batch(to_process)
        if len(claimed_ids) < len(to_process):
            lost = len(to_process) - len(claimed_ids)
            self.stats["skipped"] += lost
            logger.info(
                f"Claim: {len(claimed_ids)} papers claimed, "
                f"{lost} already locked/completed by other instances"
            )
        claimed_papers = [p for p in to_process if p.get("paper_id", "") in claimed_ids]

        # 并发处理（仅处理本实例成功领取的论文）
        import time as _time
        with ThreadPoolExecutor(max_workers=self.max_concurrent) as executor:
            futures = {}
            submit_times = {}
            for paper in claimed_papers:
                pid = paper.get("paper_id", "")
                future = executor.submit(
                    self._process_one_paper,
                    paper,
                    cls_lookup.get(pid),
                    completed_registry,
                )
                futures[future] = paper
                submit_times[future] = _time.time()

            for future in as_completed(futures):
                paper = futures[future]
                duration = _time.time() - submit_times.get(future, _time.time())
                try:
                    result = future.result()
                    self._handle_paper_result(result, paper, target_step, duration)
                except Exception as e:
                    pid = paper.get("paper_id", "")
                    title = paper.get("title", "")[:80]
                    logger.error(f"  Paper {pid[:25]} failed: {e}", exc_info=True)
                    self.stats["failed"] += 1
                    with self._db_lock:
                        update_paper_status(
                            self.db_conn, pid, title, target_step,
                            "error", duration, str(e)[:500], self.config.run_id,
                        )
                    self._log_progress()

        self.stats["finished_at"] = datetime.now().isoformat()

        # ── 批次完成后：写入分类、验证报告，生成统计，导出 CSV ──
        if target_step == "extract" and self._completed_results:
            self._generate_outputs(classifications)
        elif target_step == "classify" and self._completed_results:
            # 仅分类步骤：写入分类表
            cls_records = [r.get("classification") for r in self._completed_results if r.get("classification")]
            if cls_records:
                insert_classification(self.db_conn, cls_records)
                export_table_csv(self.db_conn, "classification",
                                 self.config.classification_path / "classification.csv")

        # 打印 DB 统计
        try:
            table_stats = get_table_stats(self.db_conn)
            logger.info(f"Database stats: {table_stats}")
        except Exception:
            pass

        logger.info(
            f"Batch complete: {self.stats['completed']} ok, "
            f"{self.stats['failed']} failed, {self.stats['skipped']} skipped "
            f"(total {self.stats['total']})"
        )

        # 关闭 DB 连接
        self.db_conn.close()

        return self.stats

    def _generate_outputs(self, classifications: Optional[List[dict]]):
        """批次完成后生成所有输出文件。"""
        logger.info("Generating outputs...")

        # 1. 分类结果写入 DB + 导出 CSV
        if classifications:
            insert_classification(self.db_conn, classifications)
            export_table_csv(self.db_conn, "classification",
                             self.config.classification_path / "classification.csv")

        # 2. 验证报告写入 DB + 导出 CSV
        insert_validation(self.db_conn, self._completed_results)
        export_table_csv(self.db_conn, "validation_issues",
                         self.config.validation_path / "validation_issues.csv")

        # 3. 提取结果导出 CSV（规范化表 + 交接用宽表）
        export_table_csv(self.db_conn, "varieties",
                         self.config.extraction_path / "varieties.csv")
        export_table_csv(self.db_conn, "studies",
                         self.config.extraction_path / "studies.csv")
        export_table_csv(self.db_conn, "papers",
                         self.config.extraction_path / "papers.csv")
        export_delivery_csv(self.db_conn,
                            self.config.extraction_path / "varieties_flat.csv")

        # 4. 覆盖率统计
        from src.output.statistics import generate_statistics
        generate_statistics(self._completed_results, self.config.statistics_path)

        logger.info(
            f"Output DB: {self.config.database.host}:{self.config.database.port}"
            f"/{self.config.database.dbname}"
        )

    def _process_one_paper(
        self,
        paper: dict,
        classification: Optional[dict],
        completed_registry: dict = None,
    ) -> dict:
        """处理单篇论文（在线程内执行）。"""
        pid = paper.get("paper_id", "")

        # 构建初始状态
        initial_state: PaperState = {
            "paper_id": pid,
            "paper_meta": paper,
            "stop_after": self.stop_after,
            "status": "pending",
            "errors": [],
        }

        # 如果有已存在的分类结果，注入初始状态
        if classification:
            initial_state["classification"] = classification
            category = classification.get("category", "")
            extractable_cats = self.config.extraction.extractable_categories
            initial_state["is_extractable"] = category in extractable_cats
            if not initial_state["is_extractable"]:
                initial_state["status"] = "skipped"
                return initial_state
            initial_state["status"] = "filtered"

        # 清除 checkpoint：仅对注册表中没有记录的新论文清除
        if pid not in completed_registry:
            self._clear_checkpoint(pid)

        # 构建 graph（每篇论文独立实例，共享 checkpoint）
        graph = build_paper_graph(
            config=self.config,
            llm=self.llm,
            mineru_client=self.mineru_client,
            geocoder=self.geocoder,
            ss_client=self.ss_client,
            checkpoint_path=self.checkpoint_path,
        )

        # 运行 graph（每篇论文独立的 thread_id，实现断点续跑）
        thread_config = {
            "configurable": {
                "thread_id": pid,
            }
        }

        try:
            for event in graph.stream(
                initial_state,
                config=thread_config,
                stream_mode="updates",
            ):
                for node_name, node_output in event.items():
                    if isinstance(node_output, dict):
                        initial_state.update(node_output)

        except Exception as e:
            logger.error(f"  [{pid[:25]}] Graph execution failed: {e}", exc_info=True)
            initial_state["status"] = "failed"
            initial_state["errors"] = initial_state.get("errors", []) + [
                {"node": "graph", "error": str(e), "timestamp": datetime.now().isoformat()}
            ]

        return initial_state

    def _log_progress(self):
        """记录处理进度。"""
        total = self.stats["total"]
        done = self.stats["completed"] + self.stats["failed"] + self.stats["skipped"]
        pct = done * 100 // max(total, 1)
        logger.info(
            f"  Progress: {done}/{total} ({pct}%) — "
            f"{self.stats['completed']} ok, {self.stats['failed']} fail, "
            f"{self.stats['skipped']} skip"
        )

    def _handle_paper_result(self, result: dict, paper: dict, target_step: str, duration: float):
        """处理单篇论文的执行结果，更新统计和数据库（process_batch / process_from_db 共用）。"""
        pid = paper.get("paper_id", "")
        title = paper.get("title", "")[:80]
        status = result.get("status", "unknown")

        full_run_statuses = ("validated", "geocoded", "validated_complete")
        is_completed = (
            status in full_run_statuses
            or (self.stop_after and status not in ("failed", "skipped"))
        )

        if is_completed:
            self.stats["completed"] += 1
            self._completed_results.append(result)

            if target_step == "extract":
                with self._db_lock:
                    insert_extraction(self.db_conn, result, pid)

            with self._db_lock:
                update_paper_status(
                    self.db_conn, pid, title, target_step,
                    "completed", duration, run_id=self.config.run_id,
                )
        elif status == "skipped":
            self.stats["skipped"] += 1
            with self._db_lock:
                update_paper_status(
                    self.db_conn, pid, title, target_step,
                    "skipped", duration, run_id=self.config.run_id,
                )
        else:
            self.stats["failed"] += 1
            error_msg = str(result.get("errors", [{}])[-1].get("error", status))[:500]
            with self._db_lock:
                update_paper_status(
                    self.db_conn, pid, title, target_step,
                    "failed", duration, error_msg, self.config.run_id,
                )

        self._log_progress()

    # ── DB 驱动的大规模处理 ──────────────────────────────────

    def process_from_db(self, chunk_size: int = 100) -> dict:
        """
        从 paper_status 表分块拉取 pending 论文并处理。

        适用于大规模场景（15M+ 论文）：
          - 不依赖内存中的论文列表，每次只从 DB 拉取 chunk_size 篇
          - 通过 FOR UPDATE SKIP LOCKED 原子领取，多实例安全
          - 循环处理直到没有更多 pending 论文

        搜索结果需先通过 insert_search_results() 写入 paper_status 表。

        Args:
            chunk_size: 每次从 DB 领取的论文数量（默认 100）。

        Returns:
            处理统计字典。
        """
        import time as _time

        self.stats["started_at"] = datetime.now().isoformat()
        target_step = self.stop_after if self.stop_after in self._STEP_ORDER else "extract"

        logger.info(
            f"process_from_db starting: chunk_size={chunk_size}, "
            f"target_step={target_step}, instance={self.instance_id}"
        )

        total_claimed = 0
        chunk_num = 0

        while True:
            chunk_num += 1

            # ── 原子领取一块 pending 论文（含元数据）──
            with self._db_lock:
                with self.db_conn.cursor() as cur:
                    cur.execute("""
                        SELECT paper_id, title, ss_paper_id, doi, abstract, year, journal
                        FROM paper_status
                        WHERE status = 'pending'
                        ORDER BY paper_id
                        LIMIT %s
                        FOR UPDATE SKIP LOCKED
                    """, (chunk_size,))
                    rows = cur.fetchall()

                    if not rows:
                        break

                    chunk_ids = [r[0] for r in rows]
                    cur.execute("""
                        UPDATE paper_status
                        SET status = 'processing', claimed_by = %s, updated_at = %s
                        WHERE paper_id = ANY(%s)
                    """, (self.instance_id, datetime.now().isoformat(), chunk_ids))

                self.db_conn.commit()

            # ── 转换为 paper dict ──
            papers = []
            for r in rows:
                papers.append({
                    "paper_id": r[0],
                    "title": r[1] or "",
                    "ss_paper_id": r[2] or "",
                    "doi": r[3] or "",
                    "abstract": r[4] or "",
                    "year": r[5] or "",
                    "journal": r[6] or "",
                })

            total_claimed += len(papers)
            self.stats["total"] = total_claimed
            logger.info(
                f"Chunk {chunk_num}: claimed {len(papers)} papers "
                f"(total claimed: {total_claimed})"
            )

            # ── 并发处理本块 ──
            with ThreadPoolExecutor(max_workers=self.max_concurrent) as executor:
                futures = {}
                submit_times = {}
                for paper in papers:
                    future = executor.submit(
                        self._process_one_paper, paper, None, {},
                    )
                    futures[future] = paper
                    submit_times[future] = _time.time()

                for future in as_completed(futures):
                    paper = futures[future]
                    duration = _time.time() - submit_times.get(future, _time.time())
                    try:
                        result = future.result()
                        self._handle_paper_result(result, paper, target_step, duration)
                    except Exception as e:
                        pid = paper.get("paper_id", "")
                        title = paper.get("title", "")[:80]
                        logger.error(f"  Paper {pid[:25]} failed: {e}", exc_info=True)
                        self.stats["failed"] += 1
                        with self._db_lock:
                            update_paper_status(
                                self.db_conn, pid, title, target_step,
                                "error", duration, str(e)[:500], self.config.run_id,
                            )
                        self._log_progress()

            logger.info(
                f"Chunk {chunk_num} done: "
                f"{self.stats['completed']} ok, {self.stats['failed']} fail, "
                f"{self.stats['skipped']} skip (total: {total_claimed})"
            )

        self.stats["total"] = total_claimed
        self.stats["finished_at"] = datetime.now().isoformat()

        # 批次完成后生成输出
        if target_step == "extract" and self._completed_results:
            self._generate_outputs(None)

        try:
            table_stats = get_table_stats(self.db_conn)
            logger.info(f"Database stats: {table_stats}")
        except Exception:
            pass

        logger.info(
            f"process_from_db complete: {self.stats['completed']} ok, "
            f"{self.stats['failed']} failed, {self.stats['skipped']} skipped "
            f"(total {total_claimed})"
        )

        self.db_conn.close()
        return self.stats

    # ── 多实例任务领取 ────────────────────────────────────────

    def _claim_papers_batch(self, papers: List[dict]) -> set:
        """
        批量原子领取论文（多实例安全）。

        通过 PostgreSQL 行锁（FOR UPDATE SKIP LOCKED）确保每篇论文
        在同一时刻只被一个实例处理：
          1. 为新论文或上次失败的论文创建/重置 pending 状态
          2. 用行锁原子领取，已被其他实例锁住的行自动跳过

        Args:
            papers: 待领取的论文列表（已通过注册表过滤）

        Returns:
            成功领取的 paper_id 集合
        """
        if not papers:
            return set()

        paper_ids = [p.get("paper_id", "") for p in papers if p.get("paper_id")]
        now = datetime.now().isoformat()

        with self._db_lock:
            with self.db_conn.cursor() as cur:
                # 1) 为新论文创建 pending 行；将上次失败/出错的论文重置为 pending（允许重试）
                for paper in papers:
                    pid = paper.get("paper_id", "")
                    title = paper.get("title", "")[:500]
                    if not pid:
                        continue
                    cur.execute("""
                        INSERT INTO paper_status (paper_id, title, status, updated_at)
                        VALUES (%s, %s, 'pending', %s)
                        ON CONFLICT (paper_id) DO UPDATE SET
                            status = 'pending',
                            claimed_by = NULL,
                            updated_at = %s
                        WHERE paper_status.status IN ('failed', 'error')
                    """, (pid, title, now, now))

                # 2) 行锁领取：SKIP LOCKED 自动跳过被其他实例锁住的行
                cur.execute("""
                    SELECT paper_id FROM paper_status
                    WHERE paper_id = ANY(%s) AND status = 'pending'
                    FOR UPDATE SKIP LOCKED
                """, (paper_ids,))
                claimed = {row[0] for row in cur.fetchall()}

                # 3) 标记为 processing，记录领取者
                if claimed:
                    cur.execute("""
                        UPDATE paper_status
                        SET status = 'processing', claimed_by = %s, updated_at = %s
                        WHERE paper_id = ANY(%s)
                    """, (self.instance_id, now, list(claimed)))

            self.db_conn.commit()

        logger.info(
            f"Claimed {len(claimed)}/{len(paper_ids)} papers "
            f"(instance={self.instance_id})"
        )
        return claimed

    # ── 注册表管理（基于 PostgreSQL paper_status 表）────────────

    def _load_registry_from_db(self, paper_ids: List[str] = None) -> dict:
        """
        从 paper_status 表查询已完成论文注册表。

        Args:
            paper_ids: 仅查询这些论文的状态（批次级查询，避免全表加载）。
                       为 None 时查询全部（仅适用于小规模场景）。

        返回 {paper_id: target_step}，仅包含 status='completed' 的记录。
        """
        try:
            with self.db_conn.cursor() as cur:
                if paper_ids:
                    cur.execute(
                        "SELECT paper_id, target_step FROM paper_status "
                        "WHERE status = 'completed' AND paper_id = ANY(%s)",
                        (paper_ids,),
                    )
                else:
                    cur.execute(
                        "SELECT paper_id, target_step FROM paper_status "
                        "WHERE status = 'completed'"
                    )
                registry = {row[0]: row[1] for row in cur.fetchall()}
            logger.info(
                f"Registry: {len(registry)} papers already completed "
                f"(checked {len(paper_ids) if paper_ids else 'all'} candidates)"
            )
            return registry
        except Exception as e:
            logger.warning(f"Registry load failed: {e}")
            return {}

    def _clear_checkpoint(self, paper_id: str):
        """清除指定论文的 LangGraph checkpoint，防止旧状态干扰重跑。"""
        import sqlite3
        db_path = Path(self.checkpoint_path)
        if not db_path.exists():
            return
        try:
            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM checkpoints WHERE thread_id = ?", (paper_id,)
            )
            if cursor.rowcount > 0:
                logger.debug(f"  Cleared {cursor.rowcount} checkpoint(s) for {paper_id}")
            conn.commit()
            conn.close()
        except Exception as e:
            logger.debug(f"  Checkpoint clear skipped for {paper_id}: {e}")
