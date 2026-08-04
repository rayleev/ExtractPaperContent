"""
LLM API 客户端 — OpenAI 兼容接口，支持 JSON 响应解析和重试。
"""

import json
import re
import time
import threading
import logging
from typing import Optional

import requests

from src.config import LLMConfig

logger = logging.getLogger("paper_extractor")


class LLMClient:
    """OpenAI 兼容的 LLM 客户端，带 JSON 提取能力和简单限流。"""

    # 类级别限流：所有实例共享，确保跨线程请求间隔
    _last_call_time: float = 0
    _call_lock = threading.Lock()

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

    def _throttle(self):
        """请求间隔至少 0.5 秒，避免瞬间并发冲击 LLM 服务。"""
        with LLMClient._call_lock:
            now = time.time()
            elapsed = now - LLMClient._last_call_time
            if elapsed < 0.5:
                time.sleep(0.5 - elapsed)
            LLMClient._last_call_time = time.time()

    def call(self, prompt: str, max_tokens: int | None = None) -> Optional[str]:
        """调用 LLM，返回原始文本响应。"""
        self._throttle()
        url = f"{self.config.base_url}/chat/completions"
        payload = {
            "model": self.config.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens or self.config.max_tokens,
            "temperature": self.config.temperature,
        }
        prompt_chars = len(prompt)
        for attempt in range(1, self.config.max_retries + 1):
            try:
                t0 = time.time()
                resp = requests.post(
                    url, headers=self.headers, json=payload, timeout=self.config.timeout
                )
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                elapsed = time.time() - t0
                # 防御性检查：reasoning 模型可能只返回 reasoning 不返回 content
                if content is None:
                    reasoning = data["choices"][0]["message"].get("reasoning")
                    logger.warning(
                        f"  LLM attempt {attempt}/{self.config.max_retries}: content is None "
                        f"(reasoning model?). reasoning length={len(reasoning) if reasoning else 0}, "
                        f"finish_reason={data['choices'][0].get('finish_reason')}, "
                        f"usage={data.get('usage')}"
                    )
                    content = ""
                logger.debug(
                    f"  LLM ok: {prompt_chars} chars → {len(content)} chars, "
                    f"{elapsed:.1f}s, model={self.config.model}"
                )
                return content
            except Exception as e:
                elapsed = time.time() - t0
                logger.warning(
                    f"  LLM attempt {attempt}/{self.config.max_retries} failed "
                    f"({elapsed:.1f}s, prompt={prompt_chars} chars): {e}"
                )
                if 'data' in dir() and data:
                    logger.debug(f"  LLM response data: {str(data)[:500]}")
                if attempt < self.config.max_retries:
                    time.sleep(min(3 * (2 ** attempt), 60))
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
