"""
论文发现与元数据加载 — 扫描 docs/ 目录，加载 PDF 和元数据 CSV。
"""

import csv
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import List

from src.config import AppConfig

logger = logging.getLogger("paper_extractor")


def get_paper_key(paper: dict) -> str:
    """生成一致的论文缓存键。优先使用系统生成的 paper_id，其次 DOI。"""
    pid = paper.get("paper_id", "").strip()
    if pid:
        return pid
    doi = paper.get("doi", "").strip()
    if doi and doi.upper() != "NULL":
        return doi
    raw_id = paper.get("id", "").strip()
    if raw_id:
        return raw_id
    return paper.get("pdf_name", "")


def discover_papers(config: AppConfig) -> List[dict]:
    """
    扫描 papers_dir 目录下的 PDF 和 MD 文件，以及 CSV 元数据。

    规则:
      - 支持 .pdf 和 .md 文件，同名 MD 优先于 PDF
      - 元数据 CSV 也放在 papers_dir 下（可选）
      - 有 CSV 时按 CSV 字段匹配；无 CSV 时从文件名提取
      - 每篇论文自动分配 paper_id: P{YYYYMMDDHHmmss}_{NNN}
    """
    papers_path = config.papers_path

    # 1. 扫描 PDF 和 MD，按 stem 分组
    file_groups: dict = {}  # stem -> {"pdf": Path, "md": Path}
    for ext in ("*.pdf", "*.md"):
        for f in sorted(papers_path.glob(ext)):
            stem = f.stem
            if stem not in file_groups:
                file_groups[stem] = {}
            if f.suffix.lower() == ".pdf":
                file_groups[stem]["pdf"] = f
            elif f.suffix.lower() == ".md":
                file_groups[stem]["md"] = f

    if not file_groups:
        logger.warning(f"No PDF/MD files found in {papers_path}")
        return []

    # 2. 扫描 CSV 元数据
    csv_files = [f for f in sorted(papers_path.glob("*.csv"))
                 if not f.name.endswith(("_report.csv", "_port.csv"))]
    metadata = _load_metadata(csv_files) if csv_files else {}

    # 3. 逐个文件组匹配元数据
    papers = []
    for stem in sorted(file_groups):
        group = file_groups[stem]
        md_path = group.get("md")
        pdf_path = group.get("pdf")

        # 选择用于匹配的主文件（MD 优先）
        primary = md_path or pdf_path
        matched_row = _match_file_to_csv(primary, metadata)

        if matched_row:
            paper = _build_paper_dict(matched_row, primary, md_path, pdf_path)
        else:
            paper = _extract_metadata_from_filename(primary)
            if md_path:
                paper["md_path"] = str(md_path)
            if pdf_path:
                paper["pdf_path"] = str(pdf_path)
                paper["pdf_name"] = pdf_path.name

        papers.append(paper)

    # 4. 分配稳定 paper_id（基于标题 MD5 指纹，跨运行不变）
    import hashlib
    import re
    for paper in papers:
        title = paper.get("title", "") or paper.get("pdf_name", "")
        # 归一化：去空格、转小写，确保同一标题生成相同指纹
        normalized = re.sub(r'\s+', '', title).lower()
        fingerprint = hashlib.md5(normalized.encode("utf-8")).hexdigest()[:10]
        paper["paper_id"] = f"P_{fingerprint}"

    md_count = sum(1 for p in papers if p.get("md_path"))
    pdf_only = sum(1 for p in papers if p.get("pdf_path") and not p.get("md_path"))
    logger.info(
        f"Discovered {len(papers)} papers ({md_count} with MD, {pdf_only} PDF-only)"
    )
    return papers


# ── 文件名解析 ────────────────────────────────────────────

def _extract_metadata_from_filename(file_path: Path) -> dict:
    """从文件名提取论文元数据。

    支持格式: {id}_{year}_{title}.pdf 或 {id}_{year}_{title}.md
    """
    stem = file_path.stem

    parts = stem.split("_", 2)
    if len(parts) >= 3:
        raw_id, year_str, title = parts[0], parts[1], parts[2]
    elif len(parts) == 2:
        raw_id, year_str = parts
        title = stem
    else:
        raw_id = stem
        year_str = ""
        title = stem

    year = year_str if year_str.isdigit() and len(year_str) == 4 else ""
    has_chinese = any('\u4e00' <= c <= '\u9fff' for c in title)
    language = "zh" if has_chinese else "en"

    result = {
        "id": raw_id,
        "doi": "",
        "title": title,
        "abstract": "",
        "keywords": "",
        "journal": "",
        "year": year,
        "language": language,
        "pdf_name": file_path.name,
    }
    # 根据扩展名设置路径
    if file_path.suffix.lower() == ".md":
        result["md_path"] = str(file_path)
    else:
        result["pdf_path"] = str(file_path)
    return result


# ── CSV 元数据 ────────────────────────────────────────────

def _load_metadata(csv_files: list) -> dict:
    """从 CSV 文件加载论文元数据，返回 {匹配键: row_dict}。"""
    metadata = {}
    for csv_file in csv_files:
        for encoding in ["utf-8-sig", "utf-8", "gbk", "gb18030", "latin-1"]:
            try:
                with open(csv_file, "r", encoding=encoding) as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        # 建多个索引以支持多种匹配方式
                        for field in ("doi", "id", "paper_id", "filename", "pdf_file_path"):
                            val = row.get(field, "").strip()
                            if val and val.upper() != "NULL":
                                metadata[val] = row
                        title = row.get("title", "").strip()
                        if title:
                            metadata[title] = row
                logger.info(f"Loaded metadata from {csv_file.name}: {len(metadata)} entries")
                break
            except (UnicodeDecodeError, UnicodeError):
                continue
            except Exception as e:
                logger.warning(f"  Failed to read CSV {csv_file}: {e}")
                break
    return metadata


def _match_file_to_csv(file_path: Path, metadata: dict) -> dict | None:
    """尝试将文件匹配到 CSV 元数据行。"""
    if not metadata:
        return None

    stem = file_path.stem
    filename = file_path.name

    # 策略 1: 直接按文件名匹配
    if filename in metadata:
        return metadata[filename]

    # 策略 2: 从文件名提取的 id 匹配
    parts = stem.split("_", 2)
    if len(parts) >= 1:
        file_id = parts[0]
        if file_id in metadata:
            return metadata[file_id]

    # 策略 3: 从文件名提取的 title 匹配
    if len(parts) >= 3:
        file_title = parts[2]
        if file_title in metadata:
            return metadata[file_title]

    # 策略 4: 遍历匹配 filename / pdf_file_path 字段
    for key, row in metadata.items():
        for field in ("filename", "pdf_file_path"):
            val = row.get(field, "")
            if val and (stem in val):
                return row

    return None


# ── 构建论文字典 ──────────────────────────────────────────

def _build_paper_dict(row: dict, primary_path: Path, md_path=None, pdf_path=None) -> dict:
    """从 CSV 行和文件路径构建标准化的论文字典。"""
    title = row.get("title", "")

    # 解析 keywords
    keywords_raw = row.get("keywords", "")
    if keywords_raw and keywords_raw != "[]":
        try:
            kw_list = json.loads(keywords_raw)
            keywords = ", ".join(kw_list) if isinstance(kw_list, list) else str(kw_list)
        except (json.JSONDecodeError, TypeError):
            keywords = keywords_raw
    else:
        keywords = ""

    # 解析 journal
    journal_raw = row.get("journal", "")
    if journal_raw and journal_raw != "null":
        try:
            jdata = json.loads(journal_raw)
            journal = jdata.get("journal", "") if isinstance(jdata, dict) else str(jdata)
        except (json.JSONDecodeError, TypeError):
            journal = journal_raw
    else:
        journal = ""

    # 年份: 兼容 publication_year 和 year 两种字段名
    year = row.get("publication_year", "") or row.get("year", "")

    # 判断语言: 优先 CSV 字段，其次自动检测
    language = row.get("language", "").strip()
    if not language:
        has_chinese = any('\u4e00' <= c <= '\u9fff' for c in title)
        language = "zh" if has_chinese else "en"

    result = {
        "id": row.get("id", "") or row.get("paper_id", ""),
        "doi": row.get("doi", ""),
        "title": title,
        "abstract": row.get("abstract", ""),
        "keywords": keywords,
        "journal": journal,
        "year": year,
        "language": language,
        "pdf_name": (pdf_path or md_path or primary_path).name,
    }
    if md_path:
        result["md_path"] = str(md_path)
    if pdf_path:
        result["pdf_path"] = str(pdf_path)
    return result
