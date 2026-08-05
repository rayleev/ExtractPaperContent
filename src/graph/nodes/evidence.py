"""
EvidenceNode — 证据验证节点。

对重要字段进行验证，从 extraction_hints 中获取候选证据，
用 LLM 批量去原文验证，输出可靠的 EvidenceNode。

位置：geocode 之后，validate 之前
"""

from __future__ import annotations
import logging
import json
from pathlib import Path
from typing import Optional

from src.config import AppConfig
from src.clients.llm import LLMClient
from src.graph.state import PaperState

logger = logging.getLogger("paper_extractor")

PROMPT_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"


def _get_field_value(extraction: dict, field_name: str):
    """从 extraction 中获取字段值"""
    # paper 级字段
    paper = extraction.get("paper", {})
    if field_name in paper:
        return paper[field_name]

    # study 级字段（取第一个 study 的值）
    studies = extraction.get("studies", [])
    if studies:
        if field_name in studies[0]:
            return studies[0][field_name]

    # variety 级字段（取第一个 variety 的值）
    if studies:
        varieties = studies[0].get("varieties", [])
        if varieties and field_name in varieties[0]:
            return varieties[0][field_name]

    return None


def _find_evidence_from_hints(field_name: str, value, extraction_hints: list):
    """从 extraction_hints 中获取候选证据（按字段名匹配，值做包含检查）"""
    if value is None:
        return None
    value_str = str(value)
    for hint in extraction_hints:
        if hint.get("field") != field_name:
            continue
        hint_value = str(hint.get("value", ""))
        # 精确匹配 或 包含匹配（处理 yield_raw_value=8383.5 vs hint="8383.5 kg/hm²" 的情况）
        if hint_value == value_str or value_str in hint_value or hint_value in value_str:
            return {
                "location": "unknown",
                "text": hint.get("context", ""),
                "crop": hint.get("crop", ""),
            }
    return None


def evidence_node(
    state: PaperState,
    config: AppConfig,
    llm: LLMClient,
) -> dict:
    """
    EvidenceNode：批量验证重要字段的证据。

    输入：
      - extraction（最终提取结果）
      - extraction_hints（parse 输出的候选证据）
      - parsed_text（论文全文）

    输出：
      - evidence_nodes: 验证后的证据列表
    """
    if not config.evidence_validation.enabled:
        return {"evidence_nodes": [], "status": "evidence_skipped"}

    extraction = state.get("extraction", {})
    extraction_hints = state.get("extraction_hints", [])
    parsed_text = state.get("parsed_text", "")
    pid = state["paper_id"]

    # 调试日志
    logger.debug(
        f"  [{pid[:25]}] evidence_node: parsed_text type={type(parsed_text).__name__}, "
        f"len={len(parsed_text) if parsed_text else 'None/empty'}, "
        f"extraction keys={list(extraction.keys()) if extraction else 'None'}"
    )

    # 收集要验证的字段
    field_configs = config.evidence_validation.fields
    if not field_configs:
        return {"evidence_nodes": [], "status": "evidence_skipped"}

    # 构建字段和候选证据列表
    field_values = []
    candidates = []
    for field_cfg in field_configs:
        field_name = field_cfg.field
        value = _get_field_value(extraction, field_name)
        candidate = _find_evidence_from_hints(field_name, value, extraction_hints)

        field_values.append({
            "field": field_name,
            "value": value,
            "required": field_cfg.required,
            "description": field_cfg.description,
        })
        candidates.append(candidate or {"location": "unknown", "text": ""})

    # 批量 LLM 验证
    prompt_template = (PROMPT_DIR / "evidence.txt").read_text(encoding="utf-8")

    # 防御性检查：parsed_text 为 None 时转为空字符串
    if parsed_text is None:
        logger.warning(f"  [{pid[:25]}] evidence_node: parsed_text is None, using empty string")
        parsed_text = ""

    prompt = prompt_template.format(
        fields_json=json.dumps(field_values, ensure_ascii=False, indent=2),
        candidates_json=json.dumps(candidates, ensure_ascii=False, indent=2),
        parsed_text=parsed_text[:8000],  # 截断以节省 token
    )

    result = llm.call_json(prompt, max_tokens=config.llm.evidence_max_tokens)
    evidence_nodes = result.get("evidence_nodes", []) if result else []

    logger.info(f"  [{pid[:25]}] Evidence: {len(evidence_nodes)} fields verified")

    return {
        "evidence_nodes": evidence_nodes,
        "status": "evidence_collected",
    }
