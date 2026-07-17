"""
验证节点 — 规则验证 + 针对性 LLM 验证。

规则验证（validate_node）：纯代码检查，不消耗 token。
  - 产量换算一致性
  - 产量范围检查（500-18000 kg/ha）
  - 增产率计算校验
  - 对照品种存在性
  - trial_year ≤ publication_year
  - 经纬度在中国范围内
  - 跨 study 产量一致性

针对性 LLM 验证（targeted_llm_validate_node）：仅对规则标记为可疑的记录做 LLM 核对。
"""

from __future__ import annotations
import logging

from src.config import AppConfig
from src.clients.llm import LLMClient
from src.graph.rules import validate_extraction
from src.graph.state import PaperState

logger = logging.getLogger("paper_extractor")


def validate_node(state: PaperState, config: AppConfig) -> dict:
    """
    规则验证节点：纯代码检查，不消耗 token。

    输出 validation_report 和 flagged_records（需要 LLM 验证的记录索引）。
    """
    extraction = state.get("extraction", {})
    paper_meta = state.get("paper_meta", {})

    report = validate_extraction(extraction, paper_meta)

    stats = report["stats"]
    logger.info(
        f"  [{state['paper_id'][:25]}] Validation: "
        f"{stats['issues_count']} issues, {stats['warnings_count']} warnings, "
        f"{stats['flagged_records']} flagged"
    )

    for issue in report["issues"]:
        logger.warning(f"  [{state['paper_id'][:25]}] ISSUE: {issue}")

    return {
        "validation_report": report,
        "flagged_records": report.get("flagged_variety_indices", []),
        "status": "validated",
    }


def targeted_llm_validate_node(
    state: PaperState,
    config: AppConfig,
    llm: LLMClient,
) -> dict:
    """
    针对性 LLM 验证节点：仅对规则标记为可疑的品种记录做 LLM 核对。

    只发送可疑记录的数据（不含论文全文），prompt 精简，token 消耗低。
    """
    flagged = state.get("flagged_records", [])
    if not flagged:
        return {"status": "validated_complete"}

    extraction = state.get("extraction", {})
    studies = extraction.get("studies", [])
    pid = state["paper_id"]

    verified_count = 0
    for si, vi in flagged:
        if si >= len(studies):
            continue
        study = studies[si]
        varieties = study.get("varieties", [])
        if vi >= len(varieties):
            continue

        v = varieties[vi]
        vname = v.get("variety_name", "?")

        # 构建精简验证 prompt（只发送可疑记录的字段）
        prompt = (
            f"请核对以下从论文表格中提取的品种产量数据是否正确：\n\n"
            f"品种名称: {vname}\n"
            f"产量: {v.get('yield_raw_value')} {v.get('yield_raw_unit')}\n"
            f"对照品种: {v.get('is_check_variety')}\n"
            f"增产率: {v.get('pct_over_check')}%\n"
            f"数据来源: {v.get('source_location')}\n\n"
            f"如果数据看起来合理，输出 {{\"verified\": true}}\n"
            f"如果有明显错误，输出 {{\"verified\": false, \"reason\": \"错误描述\"}}"
        )

        result = llm.call_json(prompt, max_tokens=200)
        if result and result.get("verified"):
            verified_count += 1
        elif result and not result.get("verified"):
            reason = result.get("reason", "")
            logger.info(f"  [{pid[:25]}] LLM flagged {vname}: {reason}")

    logger.info(f"  [{pid[:25]}] Targeted validation: {verified_count}/{len(flagged)} verified")

    return {"status": "validated_complete"}
