"""
下载节点 — 从 Semantic Scholar 获取论文全文（PDF 或 Markdown）。

仅对通过分类筛选的论文下载。下载前先用 /resources 接口检测资源类型，
按 "PDF 优先、MD 兜底" 策略获取全文：

  1. PDF 本地缓存命中 → 直接使用（不发 API 请求，保持原有快路径）
  2. 调 GET /graph/v1/paper/{id}/resources 判断 pdf / md 可用性
  3. pdf.exists → 下载 PDF（后续走 MinerU 解析，质量高）
  4. 否则 md.exists → 下载 MD 到 output/parsed/{paper_id}.md 并设置 md_path
     （parse_node 优先级 1 直读，跳过 MinerU）
  5. 两者都无 → no_pdf，记录 pdf_missing
  6. resources 接口本身失败 → 降级回"直接下载 PDF"的旧逻辑兜底

PDF 按年份组织：{base_dir}/docs/PDF/{year}/{paper_id}.pdf
下载失败时不中断 pipeline，设置 pdf_missing 标记并路由到 END。
"""

from __future__ import annotations
import logging
from pathlib import Path

from src.config import AppConfig
from src.graph.state import PaperState

logger = logging.getLogger("paper_extractor")


def _is_valid_pdf(path: Path) -> bool:
    """校验 PDF 有效性：文件存在、非 0 字节、文件头为 %PDF。

    用于识别中断下载残留的空文件/损坏文件，避免把无效 PDF 当作已下载复用。
    """
    try:
        if path.stat().st_size == 0:
            return False
        with open(path, "rb") as f:
            return f.read(16).startswith(b"%PDF")
    except OSError:
        return False


def _is_valid_md(path: Path) -> bool:
    """校验 MD 有效性：文件存在且非 0 字节。"""
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


def _try_download_pdf(ss_client, download_id: str, save_path: Path, pid_log: str) -> bool:
    """下载 PDF 并校验有效性，返回是否成功。"""
    try:
        success = ss_client.download_pdf(download_id, save_path)
    except Exception as e:
        logger.error(f"  [{pid_log}] PDF download exception: {e}")
        success = False

    if success and not _is_valid_pdf(save_path):
        logger.warning(
            f"  [{pid_log}] Downloaded PDF invalid "
            f"(size={save_path.stat().st_size if save_path.exists() else 0}B), treating as failed"
        )
        success = False
    return success


def _no_pdf(state: PaperState, paper_meta: dict, paper_id: str, download_id: str, reason: str) -> dict:
    """构建"无可用全文"的返回状态（记录 pdf_missing，路由到 END）。"""
    msg = f"{reason} for {paper_id} (id={download_id})"
    logger.warning(f"  [{paper_id[:25]}] {msg}")
    return {
        "paper_meta": paper_meta,
        "pdf_missing": True,
        "status": "no_pdf",
        "node_status": {"download": "no_pdf"},
        "errors": state.get("errors", []) + [{
            "node": "download",
            "error": msg,
        }],
    }


def download_node(state: PaperState, config: AppConfig, ss_client) -> dict:
    """
    下载论文全文（PDF 优先，MD 兜底）。

    先用 /resources 接口检测资源类型，再按策略下载：
      - PDF 可用 → 下载 PDF（走 MinerU 解析）
      - 仅 MD 可用 → 下载 MD 并设置 md_path（parse 直读，跳过 MinerU）
      - 都不可用 → no_pdf

    Args:
        state: 当前论文状态。
        config: 全局配置。
        ss_client: SemanticScholarClient 实例。

    Returns:
        更新后的状态字段字典。
    """
    paper_id = state["paper_id"]
    paper_meta = state.get("paper_meta", {})
    pid_log = paper_id[:25]

    # ── 解析 paper identifier（paperId 或 DOI）──
    s2_paper_id = (
        paper_meta.get("paperId")
        or paper_meta.get("ss_paper_id")
        or ""
    )
    doi = paper_meta.get("doi", "")

    # 用于 API 调用的标识符
    download_id = s2_paper_id
    if not download_id and doi:
        download_id = f"doi:{doi}"

    if not download_id:
        return _no_pdf(
            state, paper_meta, paper_id, "",
            "No paperId or DOI available, cannot download",
        )

    # ── 确定保存路径 ──
    year = (
        paper_meta.get("publicationYear")
        or paper_meta.get("publication_year")
        or paper_meta.get("year")
        or ""
    )
    year_str = str(year) if year else "unknown"
    pdf_save_path = config.pdf_path / year_str / f"{paper_id}.pdf"
    md_save_path = config.parsed_path / f"{paper_id}.md"

    # ── 步骤 1: PDF 本地缓存（保持原有快路径，不发 API 请求）──
    if pdf_save_path.exists():
        if _is_valid_pdf(pdf_save_path):
            logger.debug(f"  [{pid_log}] PDF already exists: {pdf_save_path}")
            paper_meta["pdf_path"] = str(pdf_save_path)
            return {"paper_meta": paper_meta, "node_status": {"download": "downloaded"}}
        # 无效缓存（0字节/非PDF，多为中断下载残留）→ 删除后重新下载
        logger.warning(
            f"  [{pid_log}] Cached PDF invalid "
            f"(size={pdf_save_path.stat().st_size}B), re-downloading: {pdf_save_path}"
        )
        try:
            pdf_save_path.unlink()
        except OSError:
            pass

    # ── 步骤 2: 检测资源类型 ──
    resources = ss_client.get_resources(download_id)

    if not resources:
        # resources 接口不可用 → 降级回旧逻辑：直接尝试下载 PDF
        logger.warning(
            f"  [{pid_log}] resources check unavailable, "
            f"falling back to direct PDF download"
        )
        if _try_download_pdf(ss_client, download_id, pdf_save_path, pid_log):
            logger.info(f"  [{pid_log}] PDF downloaded (fallback): {pdf_save_path}")
            paper_meta["pdf_path"] = str(pdf_save_path)
            return {"paper_meta": paper_meta, "status": state.get("status", "downloaded"), "node_status": {"download": "downloaded"}}
        return _no_pdf(
            state, paper_meta, paper_id, download_id,
            "resources unavailable and PDF download failed",
        )

    pdf_info = resources.get("pdf", {})
    md_info = resources.get("md", {})

    # ── 步骤 3: PDF 优先（MinerU 解析质量高）──
    if pdf_info.get("exists"):
        if _try_download_pdf(ss_client, download_id, pdf_save_path, pid_log):
            logger.info(f"  [{pid_log}] PDF downloaded: {pdf_save_path}")
            paper_meta["pdf_path"] = str(pdf_save_path)
            return {"paper_meta": paper_meta, "status": state.get("status", "downloaded"), "node_status": {"download": "downloaded"}}
        logger.warning(f"  [{pid_log}] PDF exists but download failed, trying MD fallback")

    # ── 步骤 4: MD 兜底（parse_node 优先级 1 直读，跳过 MinerU）──
    if md_info.get("exists"):
        # 本地缓存命中则直接复用
        if md_save_path.exists() and _is_valid_md(md_save_path):
            logger.debug(f"  [{pid_log}] MD already exists: {md_save_path}")
            paper_meta["md_path"] = str(md_save_path)
            return {"paper_meta": paper_meta, "node_status": {"download": "downloaded"}}

        try:
            success = ss_client.download_md(
                download_id, md_save_path,
                download_url=md_info.get("downloadUrl"),
            )
        except Exception as e:
            logger.error(f"  [{pid_log}] MD download exception: {e}")
            success = False

        if success and _is_valid_md(md_save_path):
            logger.info(f"  [{pid_log}] MD downloaded (skip MinerU): {md_save_path}")
            paper_meta["md_path"] = str(md_save_path)
            return {"paper_meta": paper_meta, "status": state.get("status", "downloaded"), "node_status": {"download": "downloaded"}}
        logger.warning(f"  [{pid_log}] MD download failed")

    # ── 步骤 5: 无可用资源 ──
    return _no_pdf(
        state, paper_meta, paper_id, download_id,
        "No PDF or MD resource available",
    )
