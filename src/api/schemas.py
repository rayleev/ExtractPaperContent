"""
请求/响应模型 — FastAPI Pydantic schemas。

定义 HTTP API 的请求体和响应体结构。
"""

from __future__ import annotations
from typing import Optional, List
from pydantic import BaseModel, Field


class RunRequest(BaseModel):
    """触发 pipeline 运行的请求体。"""
    step: str = Field(
        default="all",
        description="运行步骤: search/classify/download/parse/extract/all",
    )
    keywords: Optional[List[str]] = Field(
        default=None,
        description="搜索关键词列表（覆盖 config.yaml 默认值）",
    )
    year_range: Optional[str] = Field(
        default=None,
        description="年份范围过滤，如 '2020-2025'（覆盖 config.yaml 默认值）",
    )
    paper_filter: Optional[str] = Field(
        default=None,
        description="按 DOI 或标题关键词过滤（仅处理匹配的论文）",
    )
    limit: Optional[int] = Field(
        default=None,
        description="搜索阶段最多返回的论文数量（小批次测试用，如 20）",
    )


class RunResponse(BaseModel):
    """pipeline 运行触发后的响应。"""
    job_id: str = Field(description="任务 ID，用于查询进度")
    status: str = Field(description="任务状态: accepted/running")
    message: str = Field(default="", description="附加信息")


class JobStatus(BaseModel):
    """单个任务的进度状态。"""
    job_id: str
    status: str = Field(description="pending/running/completed/failed/stopped")
    step: str = Field(default="", description="当前运行的步骤")
    stats: Optional[dict] = Field(
        default=None,
        description="进度统计: {total, completed, failed, skipped}",
    )
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None
    message: str = Field(default="", description="附加信息（如停止原因）")


class StopResponse(BaseModel):
    """停止 pipeline 的响应。"""
    status: str = Field(description="stopped / no_running_jobs")
    message: str = Field(default="", description="附加信息")
    reset_count: int = Field(default=0, description="从 processing 重置为 pending 的论文数")


class TableStats(BaseModel):
    """数据库表统计信息。"""
    papers: int = 0
    studies: int = 0
    varieties: int = 0
    varieties_flat: int = 0
    classification: int = 0
    validation_issues: int = 0
    paper_status: int = 0
    pdf_missing: int = 0


class PaperStatusResponse(BaseModel):
    """单篇论文的处理状态。"""
    paper_id: str
    title: Optional[str] = None
    target_step: Optional[str] = None
    status: Optional[str] = None
    duration_sec: Optional[float] = None
    error_message: Optional[str] = None
    updated_at: Optional[str] = None


class ProgressResponse(BaseModel):
    """全局处理进度（按状态和实例分组）。"""
    total: int = Field(description="论文总数")
    by_status: dict = Field(
        default_factory=dict,
        description="按状态分组: {pending, processing, completed, failed, skipped}",
    )
    by_instance: dict = Field(
        default_factory=dict,
        description="按实例分组: {instance-1: {processing: N, completed: M}, ...}",
    )
    completion_pct: float = Field(description="完成百分比")
