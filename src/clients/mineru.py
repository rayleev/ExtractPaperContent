"""
MinerU PDF 解析客户端 — 异步提交 PDF，轮询状态，获取 Markdown 结果。
"""

import json
import time
import logging
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

import requests

from src.config import MinerUConfig

logger = logging.getLogger("paper_extractor")


class MinerUClient:
    """MinerU v3.x 异步解析客户端。"""

    def __init__(self, config: MinerUConfig):
        self.config = config
        self.headers = {"X-API-Key": config.api_key}
        self.session = requests.Session()
        self.session.headers.update(self.headers)

    def submit_pdf(self, pdf_path: Path) -> Optional[str]:
        """提交 PDF 解析任务，返回 task_id。"""
        url = f"{self.config.base_url}/graph/v1/tasks"
        try:
            with open(pdf_path, "rb") as f:
                files = {"files": (pdf_path.name, f, "application/pdf")}
                data = [
                    ("return_md", str(self.config.return_md).lower()),
                    ("return_content_list", str(self.config.return_content_list).lower()),
                    ("return_middle_json", str(self.config.return_middle_json).lower()),
                    ("formula_enable", str(self.config.formula_enable).lower()),
                    ("table_enable", str(self.config.table_enable).lower()),
                    ("parse_method", self.config.parse_method),
                ]
                for lang in self.config.lang_list:
                    data.append(("lang_list", lang))
                resp = self.session.post(url, files=files, data=data, timeout=120)
            resp.raise_for_status()
            result = resp.json()
            task_id = result.get("task_id")
            logger.info(f"  MinerU task submitted: {pdf_path.name} -> task_id={task_id}")
            return task_id
        except Exception as e:
            logger.error(f"  MinerU submit failed for {pdf_path.name}: {e}")
            try:
                logger.error(f"  Response: {resp.text[:500]}")
            except Exception:
                pass
            return None

    def poll_task(self, task_id: str) -> Tuple[str, Optional[str]]:
        """
        轮询任务状态。

        Returns:
            (status, result_url) 元组。status 为 completed / failed / timeout；
            result_url 为任务完成时后端返回的直连结果地址（可能为 None）。
        """
        url = f"{self.config.base_url}/graph/v1/tasks/{task_id}"
        start = time.time()
        last_log = 0.0
        poll_count = 0
        while time.time() - start < self.config.poll_timeout:
            try:
                resp = self.session.get(url, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                status = data.get("status", "unknown")
                poll_count += 1
                elapsed = time.time() - start
                if status == "failed":
                    logger.error(
                        f"  Task {task_id[:8]}... failed: {data.get('error', 'unknown error')}"
                    )
                    return status, None
                if status == "completed":
                    logger.info(f"  Task {task_id[:8]}... completed in {elapsed:.0f}s ({poll_count} polls)")
                    return status, data.get("result_url")
                if elapsed - last_log >= 30.0:
                    logger.info(f"  Task {task_id[:8]}... still {status} ({elapsed:.0f}s elapsed, {poll_count} polls)")
                    last_log = elapsed
                logger.debug(f"  Task {task_id[:8]}... status: {status}")
            except Exception as e:
                logger.warning(f"  Poll error for {task_id[:8]}...: {e}")
            time.sleep(self.config.poll_interval)
        logger.error(f"  Task {task_id[:8]}... timeout after {self.config.poll_timeout}s ({poll_count} polls)")
        return "timeout", None

    def get_result(
        self, task_id: str, result_url: Optional[str] = None
    ) -> Optional[str]:
        """
        获取解析后的 Markdown 文本。

        优先通过 result_url 直连后端（urllib + Accept-Encoding: identity），
        规避代理层 gzip 解码失败的问题；直连失败时回退到代理 URL。
        """
        # ── 优先：直连后端 result_url ──
        if result_url:
            try:
                req = urllib.request.Request(result_url)
                req.add_header("X-API-Key", self.config.api_key)
                req.add_header("Accept-Encoding", "identity")
                with urllib.request.urlopen(req, timeout=120) as resp:
                    raw = resp.read()
                    data = json.loads(raw.decode("utf-8"))
                    md = self._extract_md(data, task_id)
                    if md:
                        return md
            except Exception as e:
                logger.warning(
                    f"  Direct result_url failed for {task_id[:8]}..., "
                    f"falling back to proxy: {e}"
                )

        # ── 回退：通过代理（带重试）──
        proxy_url = f"{self.config.base_url}/graph/v1/tasks/{task_id}/result"
        for attempt in range(1, 4):
            try:
                resp = self.session.get(proxy_url, timeout=60)
                resp.raise_for_status()
                data = resp.json()
                md = self._extract_md(data, task_id)
                if md:
                    return md
                return None
            except Exception as e:
                logger.warning(
                    f"  Proxy result attempt {attempt}/3 failed "
                    f"for {task_id[:8]}...: {e}"
                )
                if attempt < 3:
                    time.sleep(5)
        logger.error(f"  Get result exhausted retries for {task_id[:8]}...")
        return None

    @staticmethod
    def _extract_md(data: dict, task_id: str) -> Optional[str]:
        """从结果 JSON 中提取 md_content。"""
        results = data.get("results", {})
        for _filename, file_data in results.items():
            md = file_data.get("md_content", "")
            if md:
                return md
        logger.warning(f"  No markdown content for task {task_id[:8]}...")
        return None

    def parse_pdf(self, pdf_path: Path) -> Optional[str]:
        """完整流程：提交 -> 轮询 -> 获取结果。"""
        task_id = self.submit_pdf(pdf_path)
        if not task_id:
            return None
        status, result_url = self.poll_task(task_id)
        if status != "completed":
            logger.error(f"  Task {task_id[:8]}... ended with status: {status}")
            return None
        return self.get_result(task_id, result_url=result_url)
