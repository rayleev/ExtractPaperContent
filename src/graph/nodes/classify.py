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

logger = logging.getLogger("paper_extractor")

PROMPT_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"


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
    prompt_template = _load_classify_prompt()

    # 从配置构建目标作物列表（如 "水稻/Rice, 玉米/Maize, 小麦/Wheat"）
    crop_list = ", ".join(config.extraction.crops) if config.extraction.crops else "水稻/Rice"

    # 构造 prompt，填充模板占位符
    prompt = prompt_template.format(
        title=paper_meta.get("title", ""),
        abstract=paper_meta.get("abstract", ""),
        keywords=paper_meta.get("keywords", ""),
        journal=paper_meta.get("journal", ""),
        language="中文" if paper_meta.get("language") == "zh" else "English",
        crop_list=crop_list,
    )

    result = llm.call_json(prompt, max_tokens=1000)
    classification = result or {"category": "unknown", "language": "zh"}
    classification["paper_id"] = state["paper_id"]

    return {
        "classification": classification,
        "status": "classified",
    }
