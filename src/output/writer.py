"""
输出写入器 — 生成分类 CSV/JSON、提取扁平化 CSV/JSON、置信度汇总 CSV。
"""

import csv
import json
import logging
from pathlib import Path
from datetime import datetime
from typing import List
from collections import Counter
from src.core.models import ExtractionResult

logger = logging.getLogger("paper_extractor")


def generate_outputs(
    classifications: List[dict],
    extractions: List[dict],
    classification_dir: Path,
    extraction_dir: Path,
    confidence_dir: Path,
):
    """生成全部最终输出文件，分别写入分类、提取、置信度目录。"""
    classification_dir.mkdir(parents=True, exist_ok=True)
    extraction_dir.mkdir(parents=True, exist_ok=True)
    confidence_dir.mkdir(parents=True, exist_ok=True)

    # --- 分类 CSV ---
    cls_csv = classification_dir / "classification.csv"
    cls_fields = [
        "paper_id", "doi", "title", "language", "year", "journal",
        "category", "confidence", "reasoning", "key_signals",
        "crop_species", "paper_type", "has_yield_data", "research_country",
    ]
    _write_csv(cls_csv, classifications, cls_fields)
    logger.info(f"Classification CSV: {cls_csv}")

    # --- 分类 JSON ---
    cls_json = classification_dir / "classification.json"
    _write_json(cls_json, classifications)

    # --- 提取扁平化（全量） ---
    flat_rows = _flatten_via_model(extractions)

    # --- paper.csv: 每篇论文一行 ---
    paper_rows = _split_paper_rows(flat_rows)
    if paper_rows:
        paper_csv = extraction_dir / "paper.csv"
        paper_fields = list(paper_rows[0].keys())
        _write_csv(paper_csv, paper_rows, paper_fields)
        logger.info(f"Paper CSV: {paper_csv} ({len(paper_rows)} rows)")

    # --- study.csv: 每个 study 一行 ---
    study_rows = _split_study_rows(flat_rows)
    if study_rows:
        study_csv = extraction_dir / "study.csv"
        study_fields = list(study_rows[0].keys())
        _write_csv(study_csv, study_rows, study_fields)
        logger.info(f"Study CSV: {study_csv} ({len(study_rows)} rows)")

    # --- variety.csv: 每个品种一行 ---
    variety_rows = _split_variety_rows(flat_rows)
    if variety_rows:
        variety_csv = extraction_dir / "variety.csv"
        variety_fields = list(variety_rows[0].keys())
        _write_csv(variety_csv, variety_rows, variety_fields)
        logger.info(f"Variety CSV: {variety_csv} ({len(variety_rows)} rows)")

    # --- full_flat.csv: 完整扁平化 ---
    if flat_rows:
        full_flat_csv = extraction_dir / "full_flat.csv"
        flat_fields = list(flat_rows[0].keys())
        _write_csv(full_flat_csv, flat_rows, flat_fields)
        logger.info(f"Full flat CSV: {full_flat_csv} ({len(flat_rows)} rows)")
    else:
        logger.warning("No extraction data to write to flat CSV")

    # --- 提取层级 JSON ---
    ext_json = extraction_dir / "extraction.json"
    _write_json(ext_json, extractions)

    # --- 置信度汇总 CSV ---
    conf_csv = confidence_dir / "confidence_summary.csv"
    conf_rows = build_confidence_summary(extractions)
    if conf_rows:
        conf_fields = list(conf_rows[0].keys())
        _write_csv(conf_csv, conf_rows, conf_fields)
        logger.info(f"Confidence summary CSV: {conf_csv} ({len(conf_rows)} rows)")

    # 打印汇总报告
    print_summary(classifications, extractions, flat_rows, extraction_dir)


def _flatten_via_model(extractions: List[dict]) -> List[dict]:
    """使用 ExtractionResult Pydantic 模型做扁平化，带旧方法兜底。"""
    flat = []
    for ext in extractions:
        data = ext.get("extraction")
        if not data:
            continue
        try:
            result = ExtractionResult.model_validate(data)
            rows = result.to_flat_csv_rows(paper_id=ext.get("paper_id", ""))
            # 补充外层置信度信息
            conf = ext.get("confidence") or {}
            for row in rows:
                row["overall_confidence"] = conf.get("overall_confidence", "unknown")
                row["extraction_confidence"] = conf.get("overall_score", "")
            flat.extend(rows)
        except Exception as e:
            logger.warning(f"  Model flatten failed for {ext.get('paper_id', '?')}: {e}, using legacy")
            flat.extend(flatten_extractions([ext]))
    return flat


def flatten_extractions(extractions: List[dict]) -> List[dict]:
    """将层级提取结果扁平化为每个品种一行（层级 ID）。"""
    flat = []
    for ext in extractions:
        data = ext.get("extraction")
        if not data:
            continue
        paper = data.get("paper", {})
        paper_id = ext["paper_id"]
        conf = ext.get("confidence") or {}
        overall_conf = conf.get("overall_confidence", "unknown")

        for si, study in enumerate(data.get("studies", [])):
            study_id = f"{paper_id}-S{si+1:02d}"
            for ri, variety in enumerate(study.get("varieties", [])):
                record_id = f"{study_id}-R{ri+1:03d}"
                row = {
                    "record_id": record_id,
                    "paper_id": paper_id,
                    "paper_doi": paper.get("paper_doi", ext.get("doi", "")),
                    "paper_title": paper.get("paper_title", ext.get("title", "")),
                    "publication_year": paper.get("publication_year", ""),
                    "journal_name": paper.get("journal_name", ""),
                    "crop_species": paper.get("crop_species", ""),
                    "study_id": study_id,
                    "study_title": study.get("study_title", ""),
                    "study_description": study.get("study_description", ""),
                    "trial_year": study.get("trial_year", ""),
                    "sowing_date": study.get("sowing_date", ""),
                    "harvest_date": study.get("harvest_date", ""),
                    "country": study.get("country", ""),
                    "site_administrative_region": study.get("site_administrative_region", ""),
                    "experimental_site_name": study.get("experimental_site_name", ""),
                    "latitude": study.get("latitude", ""),
                    "longitude": study.get("longitude", ""),
                    "altitude": study.get("altitude", ""),
                    "replication_number": study.get("replication_number", ""),
                    "plot_size": study.get("plot_size", ""),
                    "planting_density": study.get("planting_density", ""),
                    "experimental_design_type": study.get("experimental_design_type", ""),
                    "experimental_design_description": study.get("experimental_design_description", ""),
                    "growth_facility_description": study.get("growth_facility_description", ""),
                    "cultural_practices": study.get("cultural_practices", ""),
                    "variety_name": variety.get("variety_name", ""),
                    "variety_code": variety.get("variety_code", ""),
                    "is_check_variety": variety.get("is_check_variety", ""),
                    "variety_source": variety.get("variety_source", ""),
                    "yield_raw_value": variety.get("yield_raw_value", ""),
                    "yield_raw_unit": variety.get("yield_raw_unit", ""),
                    "yield_standard_value": variety.get("yield_standard_value", ""),
                    "yield_standard_unit": variety.get("yield_standard_unit", ""),
                    "yield_value_type": variety.get("yield_value_type", ""),
                    "significance_group": variety.get("significance_group", ""),
                    "pct_over_check": variety.get("pct_over_check", ""),
                    "measurement_method": variety.get("measurement_method", ""),
                    "overall_confidence": overall_conf,
                    "extraction_confidence": conf.get("overall_score", ""),
                }
                flat.append(row)
    return flat


def _split_paper_rows(flat_rows: List[dict]) -> List[dict]:
    """从扁平行中提取论文级别字段，每篇论文去重保留一行。"""
    paper_fields = [
        "paper_id", "paper_doi", "paper_title", "publication_year",
        "journal_name", "crop_species",
    ]
    seen = set()
    rows = []
    for r in flat_rows:
        pid = r.get("paper_id", "")
        if pid in seen:
            continue
        seen.add(pid)
        rows.append({k: r.get(k, "") for k in paper_fields})
    return rows


def _split_study_rows(flat_rows: List[dict]) -> List[dict]:
    """从扁平行中提取 study 级别字段，每个 study 去重保留一行。"""
    study_fields = [
        "paper_id", "study_id", "study_title", "study_description",
        "trial_year", "sowing_date", "harvest_date",
        "country", "site_administrative_region", "experimental_site_name",
        "latitude", "longitude", "altitude",
        "replication_number", "plot_size", "planting_density",
        "experimental_design_type", "experimental_design_description",
        "growth_facility_description", "cultural_practices",
    ]
    seen = set()
    rows = []
    for r in flat_rows:
        key = (r.get("paper_id", ""), r.get("study_id", ""))
        if key in seen:
            continue
        seen.add(key)
        rows.append({k: r.get(k, "") for k in study_fields})
    return rows


def _split_variety_rows(flat_rows: List[dict]) -> List[dict]:
    """从扁平行中提取品种级别字段，保留 record_id + study_id 关联。"""
    variety_fields = [
        "record_id", "paper_id", "study_id",
        "variety_name", "variety_code", "is_check_variety", "variety_source",
        "yield_raw_value", "yield_raw_unit",
        "yield_standard_value", "yield_standard_unit", "yield_value_type",
        "significance_group", "pct_over_check", "measurement_method",
        "overall_confidence", "extraction_confidence",
    ]
    return [{k: r.get(k, "") for k in variety_fields} for r in flat_rows]


def build_confidence_summary(extractions: List[dict]) -> List[dict]:
    """构建置信度汇总：每篇论文一行。"""
    rows = []
    for ext in extractions:
        conf = ext.get("confidence") or {}
        data = ext.get("extraction") or {}
        num_studies = len(data.get("studies", []))
        num_varieties = sum(
            len(s.get("varieties", [])) for s in data.get("studies", [])
        )
        issues = conf.get("issues", [])
        logic = conf.get("logic_checks", {})

        rows.append({
            "paper_id": ext["paper_id"],
            "doi": ext.get("doi", ""),
            "title": ext.get("title", "")[:80],
            "category": ext.get("category", ""),
            "num_studies": num_studies,
            "num_varieties": num_varieties,
            "overall_confidence": conf.get("overall_confidence", "N/A"),
            "overall_score": conf.get("overall_score", ""),
            "num_issues": len(issues),
            "issues": "; ".join(issues) if issues else "",
            "yield_conversion_ok": logic.get("yield_conversion_consistent", ""),
            "pct_check_ok": logic.get("pct_over_check_consistent", ""),
            "year_ok": logic.get("year_consistent", ""),
            "location_ok": logic.get("location_reasonable", ""),
        })
    return rows


def print_summary(classifications, extractions, flat_rows, output_dir):
    """打印可读汇总报告。"""
    print("\n" + "=" * 70)
    print("  EXTRACTION PIPELINE SUMMARY")
    print("=" * 70)

    cats = Counter(r.get("category", "unknown") for r in classifications)
    print(f"\n  Total papers: {len(classifications)}")
    print("  Classification distribution:")
    for cat, count in sorted(cats.items(), key=lambda x: -x[1]):
        print(f"    {cat}: {count}")

    extracted_cats = Counter(e.get("category", "") for e in extractions)
    print(f"\n  Extracted papers: {len(extractions)}")
    for cat, count in extracted_cats.items():
        print(f"    {cat}: {count}")

    print(f"  Total variety records: {len(flat_rows)}")

    if flat_rows:
        confs = Counter(r.get("overall_confidence", "N/A") for r in flat_rows)
        print("\n  Confidence distribution (by record):")
        for c, n in sorted(confs.items(), key=lambda x: -x[1]):
            print(f"    {c}: {n}")

    print(f"\n  Output files in: {output_dir}")
    print("=" * 70 + "\n")


# ── 工具函数 ──────────────────────────────────────────────

def _write_csv(path: Path, rows: list, fields: list):
    """写入 CSV 文件。"""
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            row = dict(r)
            if isinstance(row.get("key_signals"), list):
                row["key_signals"] = "; ".join(row["key_signals"])
            writer.writerow(row)


def _write_json(path: Path, data):
    """写入 JSON 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
