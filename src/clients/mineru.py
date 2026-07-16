"""
MinerU PDF 解析客户端 — 异步提交 PDF，轮询状态，获取 Markdown 结果。
"""

import time
import logging
from pathlib import Path
from typing import Optional

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

    def poll_task(self, task_id: str) -> str:
        """轮询任务状态，返回 completed / failed / timeout。"""
        url = f"{self.config.base_url}/graph/v1/tasks/{task_id}"
        start = time.time()
        while time.time() - start < self.config.poll_timeout:
            try:
                resp = self.session.get(url, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                status = data.get("status", "unknown")
                if status == "failed":
                    logger.error(
                        f"  Task {task_id[:8]}... failed: {data.get('error', 'unknown error')}"
                    )
                    return status
                if status == "completed":
                    return status
                logger.debug(f"  Task {task_id[:8]}... status: {status}")
            except Exception as e:
                logger.warning(f"  Poll error for {task_id[:8]}...: {e}")
            time.sleep(self.config.poll_interval)
        return "timeout"

    def get_result(self, task_id: str) -> Optional[str]:
        """获取解析后的 Markdown 文本。"""
        url = f"{self.config.base_url}/graph/v1/tasks/{task_id}/result"
        try:
            resp = self.session.get(url, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", {})
            for filename, file_data in results.items():
                md = file_data.get("md_content", "")
                if md:
                    return md
            logger.warning(f"  No markdown content for task {task_id[:8]}...")
            return None
        except Exception as e:
            logger.error(f"  Get result failed for {task_id[:8]}...: {e}")
            return None

    def parse_pdf(self, pdf_path: Path) -> Optional[str]:
        """完整流程：提交 -> 轮询 -> 获取结果。"""
        task_id = self.submit_pdf(pdf_path)
        if not task_id:
            return None
        status = self.poll_task(task_id)
        if status != "completed":
            logger.error(f"  Task {task_id[:8]}... ended with status: {status}")
            return None
        return self.get_result(task_id)
