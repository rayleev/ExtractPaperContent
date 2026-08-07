"""
BatchOrchestrator — 管理大规模论文批量处理。

功能：
  - 多论文并发处理（ThreadPoolExecutor）
  - SQLite checkpoint 断点续跑（LangGraph 内部，独立于输出库）
  - 逐篇写入 PostgreSQL 输出数据库（实时持久化）
  - 批次完成后生成验证报告 + 覆盖率统计（写入 DB）
  - 步骤级注册表，支持分步执行和自动补全
  - 多实例支持（通过 INSTANCE_ID 环境变量标识）
"""

from __future__ import annotations
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED

from src.config import AppConfig
from src.clients.llm import LLMClient
from src.clients.mineru import MinerUClient
from src.core.geocoder import Geocoder
from src.graph.state import PaperState
from src.graph.graph import build_paper_graph
from src.graph.output import (
    get_connection,
    insert_extraction,
    insert_classification,
    insert_validation,
    insert_pdf_missing,
    delete_pdf_missing,
    claim_tasks,
    insert_statistics,
    get_table_stats,
    update_paper_status,
)

logger = logging.getLogger("paper_extractor")


class BatchOrchestrator:
    """批量论文处理编排器。"""

    # processing 状态论文的回收超时（秒）：实例崩溃后其领取的论文超过该时长
    # 未被更新，视为可被其他实例重新领取（防止论文永久卡在 processing）。
    CLAIM_STALE_SECONDS = 3600
    # 处理心跳间隔（秒）：一篇论文 processing 期间周期性刷新 updated_at，
    # 使超时回收能区分"论文本身很耗时（仍在处理）"与"实例已死"，避免耗时论文被误回收重复处理。
    HEARTBEAT_INTERVAL = 60

    def __init__(
        self,
        config: AppConfig,
        llm: LLMClient,
        geocoder: Geocoder,
        mineru_client: Optional[MinerUClient] = None,
        ss_client=None,
        max_concurrent: int = 10,
        stop_after: str = "",
        stop_event: Optional[threading.Event] = None,
    ):
        self.config = config
        self.llm = llm
        self.geocoder = geocoder
        self.mineru_client = mineru_client
        self.ss_client = ss_client
        self.max_concurrent = max_concurrent
        self.stop_after = stop_after  # 分步执行：在此节点后停止（空 = 完整流程）
        self.stop_event = stop_event  # 外部停止信号（/api/stop 设置）

        # Checkpoint 路径（LangGraph 断点续跑）
        self.checkpoint_path = str(config.cache_path / "langgraph_checkpoint.db")

        # 步骤优先级（数值越大 = 完成度越高）
        self._STEP_ORDER = {"classify": 1, "parse": 2, "extract": 3}

        # PostgreSQL 输出数据库（所有结果统一存储，含注册表功能）
        # 批次级连接：在 process_batch / process_from_db 期间复用，避免频繁创建/销毁
        self._db_connection_string = config.database.connection_string
        self._db_lock = threading.Lock()
        self._batch_conn = None  # 批次级长连接，由 _get_batch_connection 懒创建

        # 多实例标识（用于任务领取 claim_tasks）
        self.instance_id = os.environ.get("INSTANCE_ID", "default")

        # 实例级心跳守护线程的停止信号
        self._heartbeat_stop = threading.Event()
        # 超时回收节流：记录上次执行回收的时间戳
        self._last_reclaim_time = 0.0

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
        完成后生成验证报告、覆盖率统计（写入 DB）。
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

        # 实例级心跳：批处理期间刷新本实例名下所有 processing 论文的租约，
        # 避免排队等待（worker 未启动）的论文因长时间无心跳被其他实例误回收。
        heartbeat_thread = self._start_heartbeat() if claimed_papers else None

        # 并发处理（仅处理本实例成功领取的论文）
        try:
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
                    submit_times[future] = time.time()

                for future in as_completed(futures):
                    paper = futures[future]
                    duration = time.time() - submit_times.get(future, time.time())
                    try:
                        result = future.result()
                        self._handle_paper_result(result, paper, target_step, duration)
                    except Exception as e:
                        pid = paper.get("paper_id", "")
                        title = paper.get("title", "")[:80]
                        logger.error(f"  Paper {pid[:25]} failed: {e}", exc_info=True)
                        self.stats["failed"] += 1
                        # 复用批次级连接处理异常状态更新
                        conn = self._get_batch_connection()
                        try:
                            update_paper_status(
                                conn, pid, title, target_step,
                                "error", duration, str(e)[:500], self.config.run_id,
                            )
                        except Exception:
                            conn.rollback()
                        self._log_progress()

            self.stats["finished_at"] = datetime.now().isoformat()

            # ── 批次完成后：写入验证报告、生成统计 ──
            # 复用批次级连接进行汇总操作
            # 注：分类结果已在 _handle_paper_result 中实时入库，无需重写
            conn = self._get_batch_connection()
            if target_step == "extract" and self._completed_results:
                self._generate_outputs(conn, classifications)

            # 打印 DB 统计
            try:
                table_stats = get_table_stats(conn)
                logger.info(f"Database stats: {table_stats}")
            except Exception:
                pass
        finally:
            # 批次结束：停止心跳守护线程，关闭长连接
            self._stop_heartbeat(heartbeat_thread)
            self._close_batch_connection()

        logger.info(
            f"Batch complete: {self.stats['completed']} ok, "
            f"{self.stats['failed']} failed, {self.stats['skipped']} skipped "
            f"(total {self.stats['total']})"
        )

        return self.stats

    def _generate_outputs(self, conn, classifications: Optional[List[dict]]):
        """批次完成后生成所有输出（复用传入的批次级连接，全部写入 DB）。"""
        logger.info("Generating outputs...")

        # 1. 分类结果写入 DB
        if classifications:
            insert_classification(conn, classifications)

        # 2. 验证报告写入 DB
        insert_validation(conn, self._completed_results)

        # 3. 覆盖率统计写入 DB
        insert_statistics(conn, self._completed_results, run_id=self.config.run_id)

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
                initial_state["errors"] = [{"node": "filter", "error": f"category '{category}' not in extractable list"}]
                return initial_state
            initial_state["status"] = "filtered"

        # 清除 checkpoint：仅对注册表中没有记录的新论文清除
        if pid not in completed_registry:
            self._clear_checkpoint(pid)

        # 构建 graph（每篇论文独立实例，共享 checkpoint）
        graph, sqlite_conn = build_paper_graph(
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
        finally:
            # 关闭 checkpoint SQLite 连接，避免资源泄漏
            if sqlite_conn is not None:
                try:
                    sqlite_conn.close()
                except Exception:
                    pass

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

    def _get_batch_connection(self):
        """获取批次级长连接（懒创建，整个批次期间复用）。

        _handle_paper_result 在主线程的 as_completed 循环中顺序调用，
        因此单连接即可满足需求，无需线程本地存储。
        """
        if self._batch_conn is None or self._batch_conn.closed:
            self._batch_conn = get_connection(self._db_connection_string)
        return self._batch_conn

    def _close_batch_connection(self):
        """关闭批次级长连接（批次结束时调用）。"""
        if self._batch_conn is not None:
            try:
                if not self._batch_conn.closed:
                    self._batch_conn.close()
            except Exception as e:
                logger.warning(f"Failed to close batch connection: {e}")
            finally:
                self._batch_conn = None
    def _handle_paper_result(self, result: dict, paper: dict, target_step: str, duration: float):
        """处理单篇论文的执行结果，更新统计和数据库（process_batch / process_from_db 共用）。"""
        pid = paper.get("paper_id", "")
        title = paper.get("title", "")[:80]
        status = result.get("status", "unknown")

        # 调试日志：打印完整 state 和 errors
        logger.debug(
            f"  [{pid[:25]}] _handle_paper_result: status={status}, "
            f"errors={result.get('errors', 'N/A')}, "
            f"keys={list(result.keys())}"
        )

        # 复用批次级长连接，避免每篇论文创建/销毁连接的开销
        conn = self._get_batch_connection()

        try:
            # 分类结果实时入库（无论论文最终 completed / skipped / failed）
            cls = result.get("classification")
            if cls:
                insert_classification(conn, [cls])

            # PDF 缺失记录入库
            if result.get("pdf_missing"):
                meta = result.get("paper_meta", {})
                reason = ""
                errs = result.get("errors", [])
                if errs:
                    reason = str(errs[-1].get("error", ""))[:500]
                insert_pdf_missing(
                    conn, pid,
                    title=meta.get("title", "")[:500],
                    doi=meta.get("doi", ""),
                    reason=reason,
                )

            full_run_statuses = ("validated", "geocoded", "validated_complete")
            is_completed = (
                status in full_run_statuses
                or (self.stop_after and status not in ("failed", "skipped"))
            )

            if is_completed:
                self.stats["completed"] += 1
                self._completed_results.append(result)

                if target_step == "extract":
                    insert_extraction(conn, result, pid)

                # 获取 node_status 和 last_node
                node_status = result.get("node_status", {})
                last_node = list(node_status.keys())[-1] if node_status else ""

                update_paper_status(
                    conn, pid, title, target_step,
                    "completed", duration, run_id=self.config.run_id,
                    last_node=last_node, node_status=node_status,
                )
                # 已成功处理（含 MD 兜底），从 pdf_missing 移除，该表只留当前卡住的
                delete_pdf_missing(conn, pid)
            elif status == "skipped":
                self.stats["skipped"] += 1
                skip_reason = ""
                errs = result.get("errors", [])
                if errs:
                    skip_reason = str(errs[-1].get("error", ""))[:500]
                
                # 补充提取超时和验证超时信息
                extraction_errors = result.get("extraction_errors", [])
                validation_errors = result.get("validation_errors", [])
                if extraction_errors:
                    skip_reason += f" | 提取超时: {len(extraction_errors)}个节点"
                if validation_errors:
                    skip_reason += f" | 验证超时: {len(validation_errors)}个节点"
                skip_reason = skip_reason[:500]
                
                # 获取 node_status 和 last_node
                node_status = result.get("node_status", {})
                last_node = list(node_status.keys())[-1] if node_status else ""
                
                update_paper_status(
                    conn, pid, title, target_step,
                    "skipped", duration, error_message=skip_reason,
                    run_id=self.config.run_id,
                    last_node=last_node, node_status=node_status,
                )
                # 已被剔除（如非中国论文），视为已解决，从 pdf_missing 移除
                delete_pdf_missing(conn, pid)
            else:
                self.stats["failed"] += 1
                errors = result.get("errors", [])
                error_msg = str(errors[-1].get("error", status))[:500] if errors else status
                
                # 补充提取超时和验证超时信息
                extraction_errors = result.get("extraction_errors", [])
                validation_errors = result.get("validation_errors", [])
                if extraction_errors:
                    error_msg += f" | 提取超时: {len(extraction_errors)}个节点"
                if validation_errors:
                    error_msg += f" | 验证超时: {len(validation_errors)}个节点"
                error_msg = error_msg[:500]
                
                # 获取 node_status 和 last_node
                node_status = result.get("node_status", {})
                last_node = list(node_status.keys())[-1] if node_status else ""
                
                update_paper_status(
                    conn, pid, title, target_step,
                    "failed", duration, error_msg, self.config.run_id,
                    last_node=last_node, node_status=node_status,
                )
        except Exception:
            # 回滚当前论文的未提交操作，保持连接可用供后续论文复用
            logger.error(
                f"  [{pid[:25]}] _handle_paper_result exception: "
                f"status={status}, errors={result.get('errors', 'N/A')}",
                exc_info=True,
            )
            conn.rollback()
            raise

        self._log_progress()

    # ── DB 驱动的大规模处理 ──────────────────────────────────

    def process_from_db(self, chunk_size: int = 100) -> dict:
        """
        从 paper_status 表持续领取 pending 论文并以滑动窗口并发处理。

        适用于大规模场景（15M+ 论文）：
          - 不依赖内存中的论文列表，每次从 DB 领取
          - 通过 FOR UPDATE SKIP LOCKED 原子领取，多实例安全
          - 滑动窗口：始终维持约 max_concurrent 篇论文在跑，一篇完成立即补领，
            避免一次性大批量领取导致其余论文长期空等

        搜索结果需先通过 insert_search_results() 写入 paper_status 表。

        Args:
            chunk_size: 保留参数（兼容旧调用），窗口大小由 concurrency.extract_workers 决定。

        Returns:
            处理统计字典。
        """
        self.stats["started_at"] = datetime.now().isoformat()
        target_step = self.stop_after if self.stop_after in self._STEP_ORDER else "extract"

        logger.info(
            f"process_from_db starting: window={self.max_concurrent}, "
            f"target_step={target_step}, instance={self.instance_id}"
        )

        total_claimed = 0
        heartbeat_thread = None

        try:
            # 实例级心跳：批处理全程刷新本实例名下所有 processing 论文的租约，
            # 覆盖"已领取但尚在 executor 队列排队、worker 未启动"的论文，防止被误回收。
            heartbeat_thread = self._start_heartbeat()

            # 复用批次级连接（领取是短事务，commit 后即释放行锁）
            claim_conn = self._get_batch_connection()

            # 初始填满窗口
            papers = self._claim_pending_papers(claim_conn, self.max_concurrent)
            total_claimed += len(papers)
            self.stats["total"] = total_claimed

            with ThreadPoolExecutor(max_workers=self.max_concurrent) as executor:
                future_to_paper = {}
                submit_times = {}
                for paper in papers:
                    f = executor.submit(self._process_one_paper, paper, None, {})
                    future_to_paper[f] = paper
                    submit_times[f] = time.time()
                pending = set(future_to_paper)

                while pending:
                    # 外部停止信号：等待所有在途任务完成，不再补领
                    if self.stop_event and self.stop_event.is_set():
                        logger.info(f"Stop signal detected — draining {len(pending)} in-flight papers")
                        done, pending = wait(pending)
                    else:
                        done, pending = wait(pending, return_when=FIRST_COMPLETED)

                    for future in done:
                        paper = future_to_paper.pop(future)
                        duration = time.time() - submit_times.pop(future, time.time())
                        try:
                            result = future.result()
                            self._handle_paper_result(result, paper, target_step, duration)
                        except Exception as e:
                            pid = paper.get("paper_id", "")
                            title = paper.get("title", "")[:80]
                            logger.error(f"  Paper {pid[:25]} failed: {e}", exc_info=True)
                            self.stats["failed"] += 1
                            # 复用批次级连接处理异常状态更新
                            conn = self._get_batch_connection()
                            try:
                                update_paper_status(
                                    conn, pid, title, target_step,
                                    "error", duration, str(e)[:500], self.config.run_id,
                                )
                            except Exception:
                                conn.rollback()
                            self._log_progress()

                    # 补领：维持滑动窗口 ~max_concurrent
                    if not (self.stop_event and self.stop_event.is_set()):
                        refill_limit = self.max_concurrent - len(pending)
                        if refill_limit > 0:
                            new_papers = self._claim_pending_papers(claim_conn, refill_limit)
                            total_claimed += len(new_papers)
                            self.stats["total"] = total_claimed
                            for paper in new_papers:
                                f = executor.submit(self._process_one_paper, paper, None, {})
                                future_to_paper[f] = paper
                                submit_times[f] = time.time()
                                pending.add(f)

                self.stats["total"] = total_claimed
                self.stats["finished_at"] = datetime.now().isoformat()

            # 批次完成后生成输出（复用批次级连接）
            if target_step == "extract" and self._completed_results:
                conn = self._get_batch_connection()
                self._generate_outputs(conn, None)

            try:
                conn = self._get_batch_connection()
                table_stats = get_table_stats(conn)
                logger.info(f"Database stats: {table_stats}")
            except Exception:
                pass
        finally:
            # 批次结束：停止心跳守护线程，关闭长连接
            self._stop_heartbeat(heartbeat_thread)
            self._close_batch_connection()

        logger.info(
            f"process_from_db complete: {self.stats['completed']} ok, "
            f"{self.stats['failed']} failed, {self.stats['skipped']} skipped "
            f"(total {total_claimed})"
        )

        return self.stats

    # ── 多实例任务领取 ────────────────────────────────────────

    def _claim_pending_papers(self, conn, limit: int) -> List[dict]:
        """原子领取至多 limit 篇 pending 论文（含元数据），多实例安全。

        周期性回收超时未心跳的 processing 论文（实例崩溃遗留）为 pending 后，
        用 FOR UPDATE SKIP LOCKED 原子领取，避免多实例重复处理。
        回收操作按时间节流（HEARTBEAT_INTERVAL），避免每个领取都触发全表扫描。
        """
        now_ts = time.time()
        do_reclaim = (now_ts - self._last_reclaim_time) >= self.HEARTBEAT_INTERVAL
        if do_reclaim:
            self._last_reclaim_time = now_ts
        try:
            with conn.cursor() as cur:
                # 超时回收：实例崩溃后遗留的 processing 论文（超过回收阈值未发心跳）
                # 重置为 pending，交由下方 FOR UPDATE SKIP LOCKED 原子领取，
                # 保证同一时刻仍只有单个实例能抢到；仍在处理（有心跳、updated_at 新鲜）的
                # 论文不会被误抢，从而避免重复处理。
                if do_reclaim:
                    now = datetime.now().isoformat()
                    stale_cutoff = (datetime.now() - timedelta(seconds=self.CLAIM_STALE_SECONDS)).isoformat()
                    cur.execute("""
                        UPDATE pe_reg_paper_status
                        SET status = 'pending', claimed_by = NULL, updated_at = %s
                        WHERE status = 'processing' AND updated_at < %s
                    """, (now, stale_cutoff))

                cur.execute("""
                    SELECT paper_id, title, ss_paper_id, doi, abstract, publication_year, journal
                    FROM pe_reg_paper_status
                    WHERE status = 'pending'
                    ORDER BY paper_id
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                """, (limit,))
                rows = cur.fetchall()
                if not rows:
                    return []

                ids = [r[0] for r in rows]
                cur.execute("""
                    UPDATE pe_reg_paper_status
                    SET status = 'processing', claimed_by = %s, updated_at = %s
                    WHERE paper_id = ANY(%s)
                """, (self.instance_id, datetime.now().isoformat(), ids))
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        return [
            {
                "paper_id": r[0],
                "title": r[1] or "",
                "ss_paper_id": r[2] or "",
                "doi": r[3] or "",
                "abstract": r[4] or "",
                "publication_year": r[5] or "",
                "journal": r[6] or "",
            }
            for r in rows
        ]

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
        # 回收超时阈值：仅当论文状态为 processing 且超过该时长未被更新时才重置，
        # 避免误抢其他实例"正在处理中"的论文（同时不破坏原有锁定语义）。
        stale_cutoff = (datetime.now() - timedelta(seconds=self.CLAIM_STALE_SECONDS)).isoformat()

        # 复用批次级连接（领取是短事务，提交后即释放行锁，不会阻塞其他实例）
        conn = self._get_batch_connection()
        try:
            with conn.cursor() as cur:
                # 1) 为新论文创建 pending 行；将上次失败/出错的论文重置为 pending（允许重试）；
                #    超过回收超时的 processing 论文（实例崩溃遗留）同样重置为 pending，交由后续
                #    FOR UPDATE SKIP LOCKED 原子领取，保证同一时刻仍只有单个实例能抢到。
                for paper in papers:
                    pid = paper.get("paper_id", "")
                    title = paper.get("title", "")[:500]
                    if not pid:
                        continue
                    cur.execute("""
                        INSERT INTO pe_reg_paper_status (paper_id, title, status, updated_at)
                        VALUES (%s, %s, 'pending', %s)
                        ON CONFLICT (paper_id) DO UPDATE SET
                            status = 'pending',
                            claimed_by = NULL,
                            updated_at = %s
                        WHERE pe_reg_paper_status.status IN ('failed', 'error')
                           OR (pe_reg_paper_status.status = 'processing'
                               AND pe_reg_paper_status.updated_at < %s)
                    """, (pid, title, now, now, stale_cutoff))

                # 2) 行锁领取：SKIP LOCKED 自动跳过被其他实例锁住的行
                cur.execute("""
                    SELECT paper_id FROM pe_reg_paper_status
                    WHERE paper_id = ANY(%s) AND status = 'pending'
                    FOR UPDATE SKIP LOCKED
                """, (paper_ids,))
                claimed = {row[0] for row in cur.fetchall()}

                # 3) 标记为 processing，记录领取者
                if claimed:
                    cur.execute("""
                        UPDATE pe_reg_paper_status
                        SET status = 'processing', claimed_by = %s, updated_at = %s
                        WHERE paper_id = ANY(%s)
                    """, (self.instance_id, now, list(claimed)))

            conn.commit()
        except Exception:
            conn.rollback()
            raise

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
            # 复用批次级连接
            conn = self._get_batch_connection()
            with conn.cursor() as cur:
                if paper_ids:
                    cur.execute(
                        "SELECT paper_id, target_step FROM pe_reg_paper_status "
                        "WHERE status = 'completed' AND paper_id = ANY(%s)",
                        (paper_ids,),
                    )
                else:
                    cur.execute(
                        "SELECT paper_id, target_step FROM pe_reg_paper_status "
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
        conn = None
        try:
            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM checkpoints WHERE thread_id = ?", (paper_id,)
            )
            if cursor.rowcount > 0:
                logger.debug(f"  Cleared {cursor.rowcount} checkpoint(s) for {paper_id}")
            conn.commit()
        except Exception as e:
            logger.debug(f"  Checkpoint clear skipped for {paper_id}: {e}")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _bump_claimed_heartbeats(self):
        """实例级心跳：刷新本实例名下所有仍在 processing 的论文的 updated_at。

        覆盖"已领取但尚在 executor 队列排队、worker 尚未启动"的论文——它们与正在处理的
        论文同属本实例的租约。只要实例活着，这些论文就持续续约，不会被 _claim_papers_batch
        或 process_from_db 的超时回收误判为"实例已死"而重置为 pending（从而避免重复处理）。
        实例崩溃后心跳停止，其名下论文在超过回收阈值后被其他实例正常回收。

        使用独立短连接（不共享主线程连接，psycopg2 连接非线程安全）。
        """
        try:
            conn = get_connection(self._db_connection_string)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE pe_reg_paper_status SET updated_at = %s "
                        "WHERE claimed_by = %s AND status = 'processing'",
                        (datetime.now().isoformat(), self.instance_id),
                    )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.debug(f"  Instance heartbeat failed: {e}")

    def _start_heartbeat(self) -> threading.Thread:
        """启动实例级心跳守护线程，批处理期间每隔 HEARTBEAT_INTERVAL 刷新一次。

        返回线程句柄，供 finally 中 join 停止。
        """
        def _loop():
            while not self._heartbeat_stop.is_set():
                if self._heartbeat_stop.wait(self.HEARTBEAT_INTERVAL):
                    break
                self._bump_claimed_heartbeats()

        t = threading.Thread(target=_loop, daemon=True, name="claim-heartbeat")
        t.start()
        return t

    def _stop_heartbeat(self, thread: Optional[threading.Thread]):
        """停止心跳守护线程并等待其退出。"""
        self._heartbeat_stop.set()
        if thread is not None:
            try:
                thread.join(timeout=5)
            except Exception:
                pass
