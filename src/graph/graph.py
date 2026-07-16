"""
PaperProcessingGraph — LangGraph StateGraph 定义。

将各节点串联为有向图，支持条件路由、checkpoint、错误隔离。

Graph 流程:
  classify → filter → [extractable?] → parse → extract_phase1 → extract_phase2
    → postprocess → geocode → validate → [flagged?] → targeted_llm_validate → END
"""

from __future__ import annotations
import logging
from typing import Optional

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver

from src.config import AppConfig
from src.clients.llm import LLMClient
from src.clients.mineru import MinerUClient
from src.core.geocoder import Geocoder
from src.graph.state import PaperState
from src.graph.nodes import (
    classify_node,
    filter_node,
    parse_node,
    extract_phase1_node,
    extract_phase2_node,
    postprocess_node,
    geocode_node,
    validate_node,
    targeted_llm_validate_node,
)

logger = logging.getLogger("paper_extractor")


def _make_classify_node(config: AppConfig, llm: LLMClient):
    def node(state: PaperState) -> dict:
        return classify_node(state, config, llm)
    return node


def _make_filter_node(config: AppConfig):
    def node(state: PaperState) -> dict:
        return filter_node(state, config)
    return node


def _make_parse_node(config: AppConfig, mineru_client: Optional[MinerUClient]):
    def node(state: PaperState) -> dict:
        return parse_node(state, config, mineru_client)
    return node


def _make_extract_phase1_node(config: AppConfig, llm: LLMClient):
    def node(state: PaperState) -> dict:
        return extract_phase1_node(state, config, llm)
    return node


def _make_extract_phase2_node(config: AppConfig, llm: LLMClient):
    def node(state: PaperState) -> dict:
        return extract_phase2_node(state, config, llm)
    return node


def _make_postprocess_node(config: AppConfig):
    def node(state: PaperState) -> dict:
        return postprocess_node(state, config)
    return node


def _make_geocode_node(config: AppConfig, geocoder: Geocoder):
    def node(state: PaperState) -> dict:
        return geocode_node(state, config, geocoder)
    return node


def _make_validate_node(config: AppConfig):
    def node(state: PaperState) -> dict:
        return validate_node(state, config)
    return node


def _make_targeted_llm_validate_node(config: AppConfig, llm: LLMClient):
    def node(state: PaperState) -> dict:
        return targeted_llm_validate_node(state, config, llm)
    return node


# ── 条件路由函数 ──────────────────────────────────────────

def _should_extract(state: PaperState) -> str:
    """filter 后的路由：是否可提取。"""
    if state.get("status") == "skipped":
        return "skip"
    return "extract"


def _should_parse(state: PaperState) -> str:
    """parse 后的路由：是否解析成功。"""
    if state.get("status") == "failed":
        return "fail"
    return "continue"


def _has_flagged_records(state: PaperState) -> str:
    """validate 后的路由：是否有需要 LLM 验证的记录。"""
    flagged = state.get("flagged_records", [])
    if flagged:
        return "llm_validate"
    return "done"


def _should_do_phase2(state: PaperState) -> str:
    """phase1 后的路由：是否有实验章节。"""
    if state.get("status") == "phase1_failed":
        return "skip"
    return "continue"


def _should_geocode(state: PaperState) -> str:
    """postprocess 后是否需要地理编码。"""
    if state.get("status") == "failed":
        return "fail"
    return "continue"


# ── 构建 Graph ──────────────────────────────────────────────

def build_paper_graph(
    config: AppConfig,
    llm: LLMClient,
    mineru_client: Optional[MinerUClient],
    geocoder: Geocoder,
    checkpoint_path: Optional[str] = None,
) -> StateGraph:
    """
    构建单篇论文的 StateGraph。

    Args:
        config: 应用配置
        llm: LLM 客户端
        mineru_client: MinerU 客户端（可为 None）
        geocoder: 地理编码器
        checkpoint_path: SQLite checkpoint 文件路径

    Returns:
        编译后的 LangGraph graph
    """
    graph = StateGraph(PaperState)

    # ── 注册节点 ──
    graph.add_node("classify", _make_classify_node(config, llm))
    graph.add_node("filter", _make_filter_node(config))
    graph.add_node("parse", _make_parse_node(config, mineru_client))
    graph.add_node("extract_phase1", _make_extract_phase1_node(config, llm))
    graph.add_node("extract_phase2", _make_extract_phase2_node(config, llm))
    graph.add_node("postprocess", _make_postprocess_node(config))
    graph.add_node("geocode", _make_geocode_node(config, geocoder))
    graph.add_node("validate", _make_validate_node(config))
    graph.add_node("targeted_validate", _make_targeted_llm_validate_node(config, llm))

    # ── 边定义 ──
    # START → classify
    graph.add_edge(START, "classify")

    # classify → filter
    graph.add_edge("classify", "filter")

    # filter → conditional
    graph.add_conditional_edges(
        "filter",
        _should_extract,
        {
            "extract": "parse",
            "skip": END,
        },
    )

    # parse → conditional
    graph.add_conditional_edges(
        "parse",
        _should_parse,
        {
            "continue": "extract_phase1",
            "fail": END,
        },
    )

    # extract_phase1 → conditional
    graph.add_conditional_edges(
        "extract_phase1",
        _should_do_phase2,
        {
            "continue": "extract_phase2",
            "skip": END,
        },
    )

    # extract_phase2 → postprocess
    graph.add_edge("extract_phase2", "postprocess")

    # postprocess → conditional
    graph.add_conditional_edges(
        "postprocess",
        _should_geocode,
        {
            "continue": "geocode",
            "fail": END,
        },
    )

    # geocode → validate
    graph.add_edge("geocode", "validate")

    # validate → conditional
    graph.add_conditional_edges(
        "validate",
        _has_flagged_records,
        {
            "llm_validate": "targeted_validate",
            "done": END,
        },
    )

    # targeted_validate → END
    graph.add_edge("targeted_validate", END)

    # ── Checkpoint ──
    checkpointer = None
    if checkpoint_path:
        import sqlite3
        conn = sqlite3.connect(checkpoint_path, check_same_thread=False)
        checkpointer = SqliteSaver(conn)

    # 编译
    compiled = graph.compile(checkpointer=checkpointer)
    return compiled
