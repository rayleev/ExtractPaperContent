"""
LLM API 客户端 — OpenAI 兼容接口，支持 JSON 响应解析和重试。
"""

import json
import re
import time
import logging
from typing import Optional

import requests

from src.config import LLMConfig

logger = logging.getLogger("paper_extractor")


class LLMClient:
    """OpenAI 兼容的 LLM 客户端，带 JSON 提取能力。"""

    def __init__(self, config: LLMConfig):
        self.config = config
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.api_key}",
        }

    @staticmethod
    def strip_code_fences(text: str) -> str:
        """移除 LLM 输出中的 Markdown 代码围栏。"""
        text = text.strip()
        m = re.match(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        if m:
            return m.group(1).strip()
        return text

    def call(self, prompt: str, max_tokens: int | None = None) -> Optional[str]:
        """调用 LLM，返回原始文本响应。"""
        url = f"{self.config.base_url}/chat/completions"
        payload = {
            "model": self.config.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens or self.config.max_tokens,
            "temperature": self.config.temperature,
        }
        for attempt in range(1, self.config.max_retries + 1):
            try:
                resp = requests.post(
                    url, headers=self.headers, json=payload, timeout=300
                )
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"]
            except Exception as e:
                logger.warning(
                    f"  LLM call attempt {attempt}/{self.config.max_retries} failed: {e}"
                )
                if attempt < self.config.max_retries:
                    time.sleep(3 * attempt)
        return None

    def call_json(self, prompt: str, max_tokens: int | None = None) -> Optional[dict]:
        """调用 LLM 并解析 JSON 响应。"""
        raw = self.call(prompt, max_tokens)
        if not raw:
            return None
        cleaned = self.strip_code_fences(raw)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as e:
            logger.warning(f"  JSON parse failed: {e}")
            # 尝试从文本中提取 JSON 对象
            m = re.search(r"\{[\s\S]*\}", cleaned)
            if m:
                try:
                    return json.loads(m.group())
                except json.JSONDecodeError:
                    pass
            logger.error("  Could not extract JSON from LLM response")
            return None
