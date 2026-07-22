"""
Semantic Scholar 代理 API 客户端 — 提供论文批量搜索和 PDF 下载功能。

两个核心端点：
1. GET /graph/v2/paper/search/bulk — 关键词批量搜索论文，支持游标分页。
2. GET /graph/v1/paper/{paper_id}/resources/pdf — 按 paperId 或 DOI 下载论文 PDF。
"""

import time
import threading
import logging
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger("paper_extractor")

# 搜索时默认请求的字段列表
DEFAULT_SEARCH_FIELDS = (
    "paperId,title,doi,pmid,pmcid,abstract,authors,keywords,"
    "publicationYear,journal,metadata,resources"
)


class SemanticScholarClient:
    """Semantic Scholar 代理 API 客户端，支持自动分页搜索与 PDF 下载。"""

    # 类级别限流：所有实例共享，确保跨线程请求间隔
    _last_call_time: float = 0
    _call_lock = threading.Lock()

    def __init__(
        self,
        base_url: str,
        api_key: str,
        max_retries: int = 5,
        request_interval: float = 0.3,
    ):
        """
        初始化客户端。

        Args:
            base_url: API 基础地址，例如 ``http://172.17.1.122``。
            api_key: 用于 ``X-API-Key`` 请求头认证的密钥。
            max_retries: 单次请求最大重试次数，默认 5。
            request_interval: 两次请求之间的最小间隔秒数，默认 0.3s。
        """
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.max_retries = max_retries
        self.request_interval = request_interval
        self.headers = {"X-API-Key": api_key}
        self.session = requests.Session()
        self.session.headers.update(self.headers)

    # ------------------------------------------------------------------
    # 内部工具方法
    # ------------------------------------------------------------------

    def _throttle(self) -> None:
        """请求限流，确保两次请求之间至少间隔 ``request_interval`` 秒。"""
        with SemanticScholarClient._call_lock:
            now = time.time()
            elapsed = now - SemanticScholarClient._last_call_time
            if elapsed < self.request_interval:
                time.sleep(self.request_interval - elapsed)
            SemanticScholarClient._last_call_time = time.time()

    def _backoff(self, attempt: int) -> None:
        """指数退避等待，公式为 ``min(3 * 2^attempt, 60)`` 秒。"""
        wait = min(3 * (2 ** attempt), 60)
        logger.debug(f"  SemanticScholar backoff: sleeping {wait}s (attempt {attempt})")
        time.sleep(wait)

    def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        params: Optional[dict] = None,
        stream: bool = False,
        timeout: int = 60,
    ) -> Optional[requests.Response]:
        """
        带重试和指数退避的通用请求方法。

        Args:
            method: HTTP 方法，如 ``"GET"``。
            url: 完整请求 URL。
            params: URL 查询参数。
            stream: 是否以流式方式接收响应体。
            timeout: 单次请求超时秒数。

        Returns:
            成功时返回 ``requests.Response``；所有重试均失败后返回 ``None``。
        """
        for attempt in range(1, self.max_retries + 1):
            try:
                self._throttle()
                resp = self.session.request(
                    method, url, params=params, stream=stream, timeout=timeout
                )
                resp.raise_for_status()
                return resp
            except requests.exceptions.HTTPError as e:
                status = e.response.status_code if e.response is not None else "?"
                # 404 属于业务语义（无 PDF），不应重试
                if status == 404:
                    logger.debug(f"  SemanticScholar 404 for {url}")
                    return None
                logger.warning(
                    f"  SemanticScholar request attempt {attempt}/{self.max_retries} "
                    f"failed (HTTP {status}): {e}"
                )
            except Exception as e:
                logger.warning(
                    f"  SemanticScholar request attempt {attempt}/{self.max_retries} "
                    f"failed: {e}"
                )
            if attempt < self.max_retries:
                self._backoff(attempt)
        logger.error(f"  SemanticScholar request exhausted all retries: {url}")
        return None

    # ------------------------------------------------------------------
    # 公共方法
    # ------------------------------------------------------------------

    def search_all(
        self,
        query: str,
        fields: str = DEFAULT_SEARCH_FIELDS,
        year: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[dict]:
        """
        按关键词批量搜索论文，自动翻页直到游标为空或达到 limit，返回结果列表。

        Args:
            query: 搜索关键词。
            fields: 逗号分隔的返回字段列表，默认包含常用元数据字段。
            year: 可选的出版年份范围，格式如 ``"2020-2025"``。
            limit: 最多返回的论文数量。None 表示不限制（拉取全部）。

        Returns:
            论文记录列表；若请求全部失败则返回空列表。
        """
        url = f"{self.base_url}/graph/v1/paper/search"
        all_results: list[dict] = []
        cursor: Optional[str] = None

        while True:
            params: dict = {"query": query, "fields": fields}
            if year:
                params["year"] = year
            if cursor:
                params["cursor"] = cursor

            resp = self._request_with_retry("GET", url, params=params, timeout=120)
            if resp is None:
                logger.error(
                    f"  SemanticScholar search failed at page (collected {len(all_results)} results)"
                )
                break

            try:
                body = resp.json()
            except Exception as e:
                logger.error(f"  SemanticScholar search response parse error: {e}")
                break

            data = body.get("data")
            if data:
                all_results.extend(data)
                # 达到 limit 后提前停止翻页
                if limit and len(all_results) >= limit:
                    all_results = all_results[:limit]
                    break

            # v1 接口使用 hasNext + nextCursor 翻页
            if not body.get("hasNext"):
                break
            cursor = body.get("nextCursor")
            if not cursor:
                break

        logger.info(
            f"  SemanticScholar search \"{query}\": {len(all_results)} papers collected"
        )
        return all_results

    def download_pdf(self, paper_id: str, save_path: Path) -> bool:
        """
        下载指定论文的 PDF 文件并保存到本地。

        支持两种 ``paper_id`` 格式：
        - 原始 paperId，如 ``"649def34f8be52c8b6588e147f55b36d1908e1b5"``
        - DOI 格式，如 ``"doi:10.1038/nrn3241"``

        Args:
            paper_id: 论文标识符（paperId 或 ``doi:xxx``）。
            save_path: PDF 文件的本地保存路径。

        Returns:
            下载成功返回 ``True``；论文无 PDF 或发生错误返回 ``False``。
        """
        url = f"{self.base_url}/graph/v1/paper/{paper_id}/resources/pdf"
        resp = self._request_with_retry("GET", url, stream=True, timeout=300)

        if resp is None:
            logger.warning(f"  SemanticScholar PDF unavailable for paper {paper_id}")
            return False

        try:
            save_path = Path(save_path)
            save_path.parent.mkdir(parents=True, exist_ok=True)
            with open(save_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            logger.info(f"  SemanticScholar PDF saved: {save_path}")
            return True
        except Exception as e:
            logger.error(f"  SemanticScholar PDF write failed for {paper_id}: {e}")
            # 清理不完整的文件
            try:
                if save_path.exists():
                    save_path.unlink()
            except OSError:
                pass
            return False
