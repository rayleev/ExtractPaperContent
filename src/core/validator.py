"""
置信度验证器 — 对提取结果进行质量评估和逻辑一致性检查。
"""

import json
import logging
from pathlib import Path
from typing import Optional

from src.config import AppConfig
from src.clients.llm import LLMClient

logger = logging.getLogger("paper_extractor")

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"


def load_validate_prompt() -> str:
    """加载验证 prompt 模板。"""
    path = PROMPT_DIR / "validate.txt"
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def validate_extraction(
    paper: dict,
    extraction: dict,
    config: AppConfig,
    llm: LLMClient,
) -> Optional[dict]:
    """对提取结果运行置信度验证。"""
    logger.info("  Running confidence validation...")
    prompt_template = load_validate_prompt()

    extraction_str = json.dumps(extraction, ensure_ascii=False, indent=2)[:8000]
    prompt = prompt_template.format(
        title=paper["title"],
        abstract=paper["abstract"][:1500],
        extraction_json=extraction_str,
    )
    result = llm.call_json(prompt, max_tokens=2000)
    return result
