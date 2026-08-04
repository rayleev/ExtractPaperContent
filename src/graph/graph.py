"""
PaperProcessingGraph — LangGraph StateGraph 定义。

将各节点串联为有向图，支持条件路由、checkpoint、错误隔离、分步执行。

Graph 流程:
  classify → filter → [extractable?] → download → [pdf?] → parse → extract_phase1 → extract_phase2
    → [needs_lookup?] → lookup → postprocess → geocode → evidence → validate → [flagged?] → targeted_llm_validate → END

分步执行:
  通过 PaperState.stop_after 控制，graph 在指定节点后提前终止。
  支持值: "classify" | "filter" | "download" | "parse" | "extract_phase1" | "extract_phase2"
         | "lookup" | "postprocess" | "geocode" | "evidence" | "validate" | "" (完整流程)

动态 phase:
  parse 节点输出 needs_lookup，决定是否添加 lookup 节点。
"""

from __future__ import annotations
import logging
import time
from typing import Optional

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver

from src.config import AppConfig
from src.clients.llm import LLMClient
from src.clients.mineru import MinerUClient
from src.clients.semantic_scholar import SemanticScholarClient
from src.core.geocoder import Geocoder
from src.graph.state import PaperState
from src.graph.nodes import (
    classify_node,
    filter_node,
    download_node,
    parse_node,
    extract_phase1_node,
    extract_phase2_node,
    lookup_node,
    postprocess_node,
    geocode_node,
    evidence_node,
    validate_node,
    targeted_llm_validate_node,
)

logger = logging.getLogger("paper_extractor")


# ── 节点计时包装 ──────────────────────────────────────────

def _timed(node_name: str, func):
    """包装节点函数，记录并打印执行耗时。"""
    def wrapper(state: PaperState) -> dict:
        pid = state.get("paper_id", "?")[:25]
        logger.info(f"  [{pid}] {node_name} START")
        start = time.time()
        result = func(state)
        elapsed = time.time() - start
        if elapsed > 1.0:
            logger.info(f"  [{pid}] {node_name} END ({elapsed:.1f}s)")
        else:
            logger.debug(f"  [{pid}] {node_name} END ({elapsed:.2f}s)")
        return result
    return wrapper


# ── 分步执行：stop_after 路由 ──────────────────────────────

def _should_stop(state: PaperState, node_name: str) -> bool:
    """检查是否应在当前节点后停止（分步执行）。"""
    return state.get("stop_after") == node_name


def _route_after_classify(state: PaperState) -> str:
    """classify 后：先检查是否失败，再检查 stop_after，否则进入 filter。"""
    # classify 节点 LLM 调用失败时返回 status="failed"，提前终止避免被 filter 误判为 skipped
    if state.get("status") == "failed":
        return "fail"
    if _should_stop(state, "classify"):
        return "done"
    return "continue"


def _route_after_filter(state: PaperState) -> str:
    """filter 后：先检查筛选条件，再检查 stop_after。"""
    if state.get("status") == "skipped":
        return "skip"
    if _should_stop(state, "filter"):
        return "done"
    return "download"


def _route_after_download(state: PaperState) -> str:
    """download 后：PDF 不可用时提前终止，否则进入 parse。"""
    if state.get("status") == "no_pdf":
        return "no_pdf"
    if _should_stop(state, "download"):
        return "done"
    return "parse"


def _route_after_parse(state: PaperState) -> str:
    """parse 后：先检查是否失败，再检查质量门控，最后检查 stop_after。"""
    if state.get("status") == "failed":
        return "fail"
    # 质量门控：parse 质量太弱时跳过，避免垃圾数据进入后续节点
    parse_quality = state.get("parse_quality", {})
    if parse_quality.get("overall") == "failed":
        pid = state.get("paper_id", "?")[:25]
        logger.warning(f"  [{pid}] Parse quality too weak ({parse_quality}), skipping extraction")
        return "parse_failed"
    if _should_stop(state, "parse"):
        return "done"
    return "continue"


def _route_after_phase1(state: PaperState) -> str:
    """extract_phase1 后：先检查是否失败，再检查 stop_after。"""
    if state.get("status") == "phase1_failed":
        return "skip"
    if _should_stop(state, "extract_phase1"):
        return "done"
    return "continue"


def _route_after_phase2(state: PaperState) -> str:
    """extract_phase2 后：根据 needs_lookup 决定是否进入 lookup，否则进入 postprocess。"""
    if _should_stop(state, "extract_phase2"):
        return "done"
    if state.get("needs_lookup"):
        return "lookup"
    return "continue"


def _route_after_lookup(state: PaperState) -> str:
    """lookup 后：检查 stop_after，否则进入 postprocess。"""
    if _should_stop(state, "lookup"):
        return "done"
    return "continue"


def _route_after_postprocess(state: PaperState) -> str:
    """postprocess 后：先检查失败/跳过（如国家复核非中国），再检查 stop_after。"""
    if state.get("status") == "failed":
        return "fail"
    if state.get("status") == "skipped":
        return "skip"
    if _should_stop(state, "postprocess"):
        return "done"
    return "continue"


def _route_after_geocode(state: PaperState) -> str:
    """geocode 后：检查 stop_after，否则进入 evidence。"""
    if _should_stop(state, "geocode"):
        return "done"
    return "continue"


def _route_after_evidence(state: PaperState) -> str:
    """evidence 后：检查 stop_after，否则进入 validate。"""
    if _should_stop(state, "evidence"):
        return "done"
    return "continue"


def _route_after_validate(state: PaperState) -> str:
    """validate 后：检查 flagged 记录 + stop_after。"""
    if _should_stop(state, "validate"):
        return "done"
    flagged = state.get("flagged_records", [])
    if flagged:
        return "llm_validate"
    return "done"


# ── 构建 Graph ──────────────────────────────────────────────

def build_paper_graph(
    config: AppConfig,
    llm: LLMClient,
    mineru_client: Optional[MinerUClient],
    geocoder: Geocoder,
    ss_client: Optional[SemanticScholarClient] = None,
    checkpoint_path: Optional[str] = None,
) -> StateGraph:
    """
    构建单篇论文的 StateGraph。

    每个节点都包装了计时器，日志中会显示各节点执行耗时。
    支持通过 PaperState.stop_after 字段控制分步执行。
    """
    graph = StateGraph(PaperState)

    # ── 注册节点（全部带计时包装）──
    graph.add_node("classify", _timed("classify",
        lambda s: classify_node(s, config, llm)))
    graph.add_node("filter", _timed("filter",
        lambda s: filter_node(s, config)))
    graph.add_node("download", _timed("download",
        lambda s: download_node(s, config, ss_client)))
    graph.add_node("parse", _timed("parse",
        lambda s: parse_node(s, config, mineru_client, llm)))
    graph.add_node("extract_phase1", _timed("extract_phase1",
        lambda s: extract_phase1_node(s, config, llm)))
    graph.add_node("extract_phase2", _timed("extract_phase2",
        lambda s: extract_phase2_node(s, config, llm)))
    graph.add_node("lookup", _timed("lookup",
        lambda s: lookup_node(s, config, llm)))
    graph.add_node("postprocess", _timed("postprocess",
        lambda s: postprocess_node(s, config)))
    graph.add_node("geocode", _timed("geocode",
        lambda s: geocode_node(s, config, geocoder)))
    graph.add_node("evidence", _timed("evidence",
        lambda s: evidence_node(s, config, llm)))
    graph.add_node("validate", _timed("validate",
        lambda s: validate_node(s, config)))
    graph.add_node("targeted_validate", _timed("targeted_validate",
        lambda s: targeted_llm_validate_node(s, config, llm)))

    # ── 边定义（全部支持 stop_after 分步停止）──
    graph.add_edge(START, "classify")

    graph.add_conditional_edges("classify", _route_after_classify,
        {"continue": "filter", "fail": END, "done": END})

    graph.add_conditional_edges("filter", _route_after_filter,
        {"download": "download", "skip": END, "done": END})

    graph.add_conditional_edges("download", _route_after_download,
        {"parse": "parse", "no_pdf": END, "done": END})

    graph.add_conditional_edges("parse", _route_after_parse,
        {"continue": "extract_phase1", "fail": END, "parse_failed": END, "done": END})

    graph.add_conditional_edges("extract_phase1", _route_after_phase1,
        {"continue": "extract_phase2", "skip": END, "done": END})

    graph.add_conditional_edges("extract_phase2", _route_after_phase2,
        {"continue": "postprocess", "lookup": "lookup", "done": END})

    graph.add_conditional_edges("lookup", _route_after_lookup,
        {"continue": "postprocess", "done": END})

    graph.add_conditional_edges("postprocess", _route_after_postprocess,
        {"continue": "geocode", "fail": END, "skip": END, "done": END})

    graph.add_conditional_edges("geocode", _route_after_geocode,
        {"continue": "evidence", "done": END})

    graph.add_conditional_edges("evidence", _route_after_evidence,
        {"continue": "validate", "done": END})

    graph.add_conditional_edges("validate", _route_after_validate,
        {"llm_validate": "targeted_validate", "done": END})

    graph.add_edge("targeted_validate", END)

    # ── Checkpoint ──
    checkpointer = None
    if checkpoint_path:
        import sqlite3
        conn = sqlite3.connect(checkpoint_path, check_same_thread=False)
        checkpointer = SqliteSaver(conn)

    compiled = graph.compile(checkpointer=checkpointer)
    return compiled
