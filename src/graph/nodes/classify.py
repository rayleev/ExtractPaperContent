"""
分类节点 — LLM 判断论文类别（5 类）和研究国家。

分类结果决定论文是否进入后续提取流程：
  varietal_yield / management_yield → 提取
  其他 → 跳过
"""

from __future__ import annotations
import logging
from pathlib import Path

from src.config import AppConfig
from src.clients.llm import LLMClient
from src.graph.state import PaperState
from src.core.constants import CHINA_PROVINCE_KEYWORDS

logger = logging.getLogger("paper_extractor")

PROMPT_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"


def _correct_research_country(classification: dict, paper_meta: dict) -> dict:
    """
    修正 LLM 的 research_country 判断。
    如果 LLM 返回 Unknown，但标题/摘要包含中国地名，强制修正为 China。
    """
    country = classification.get("research_country", "")
    if country and country != "Unknown":
        return classification  # LLM 有明确判断，不修正

    # LLM 返回 Unknown 或空，检查标题/摘要中的中国地名
    text = (paper_meta.get("title", "") + " " + paper_meta.get("abstract", "")).lower()
    if any(province in text for province in CHINA_PROVINCE_KEYWORDS):
        classification["research_country"] = "China"
        classification["reasoning"] = classification.get("reasoning", "") + " [规则修正：标题/摘要包含中国地名]"

    return classification


def _load_classify_prompt() -> str:
    """加载分类 prompt 模板。"""
    path = PROMPT_DIR / "classify.txt"
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def classify_node(state: PaperState, config: AppConfig, llm: LLMClient) -> dict:
    """
    分类节点：LLM 判断论文类别。

    使用论文元数据（标题、摘要、关键词、期刊、语言）进行分类，
    不需要 PDF 解析。输出 5 类分类 + 研究国家判断。

    目标作物列表从 config.extraction.crops 读取，支持动态扩展。
    """
    paper_meta = state["paper_meta"]
    pid = state["paper_id"]
    prompt_template = _load_classify_prompt()

    # 从配置构建目标作物列表（如 "水稻/Rice, 玉米/Maize, 小麦/Wheat"）
    crop_list = ", ".join(config.extraction.crops) if config.extraction.crops else "水稻/Rice"

    # 构造 prompt，填充模板占位符
    prompt = prompt_template.format(
        title=paper_meta.get("title", ""),
        abstract=paper_meta.get("abstract", ""),
        keywords=paper_meta.get("keywords", ""),
        journal=paper_meta.get("journal", ""),
        crop_list=crop_list,
    )

    logger.info(f"  [{pid[:25]}] Classifying: {paper_meta.get('title', '')[:60]}")
    result = llm.call_json(prompt, max_tokens=config.llm.classify_max_tokens, node_name="classify")

    # LLM 调用失败 → 标记为 error，让流程在此终止并入库为 failed
    # （而非默认 unknown 进入 filter 被误判为 skipped，掩盖真实失败原因）
    if not result:
        logger.error(
            f"  [{pid[:25]}] Classify LLM call failed (no result returned) — "
            f"marking as error to avoid silent skip"
        )
        return {
            "classification": {"paper_id": pid, "category": "unknown"},
            "status": "failed",
            "node_status": {"classify": "failed"},
            "errors": [{
                "node": "classify",
                "error": "LLM 调用失败：分类节点未返回结果（可能是 API 限流/超时/JSON 解析失败），请检查 LLM 服务状态后重试",
            }],
        }

    classification = result
    classification["paper_id"] = pid

    # 修正 research_country 判断（方案 B：规则修正）
    classification = _correct_research_country(classification, paper_meta)

    logger.info(
        f"  [{pid[:25]}] Classification: category={classification.get('category')}, "
        f"country={classification.get('research_country', '')}, "
        f"confidence={classification.get('confidence', '')}, "
        f"crops={classification.get('crops', [])}"
    )

    return {
        "classification": classification,
        "status": "classified",
        "node_status": {"classify": "classified"},
    }
