"""
配置管理模块 — 从 config.yaml 加载并集中管理所有运行时配置。
"""

import yaml
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime


def _find_config_path() -> Path:
    """向上查找 config.yaml，优先使用项目根目录的配置文件。"""
    here = Path(__file__).resolve().parent.parent  # src/ 的上一级 = 项目根
    cfg = here / "config.yaml"
    if cfg.exists():
        return cfg
    raise FileNotFoundError(f"config.yaml not found at {here}")


@dataclass
class MinerUConfig:
    base_url: str = "http://172.17.1.122"
    api_key: str = ""
    lang_list: list = field(default_factory=lambda: ["ch", "en"])
    poll_interval: int = 5
    poll_timeout: int = 600
    return_md: bool = True
    return_content_list: bool = False
    return_middle_json: bool = False
    formula_enable: bool = True
    table_enable: bool = True
    parse_method: str = "auto"


@dataclass
class LLMConfig:
    base_url: str = "http://135.98.25.8:8000/v1"
    api_key: str = ""
    model: str = "DSv4-flash"
    max_tokens: int = 8192
    temperature: float = 0.1
    max_retries: int = 3


@dataclass
class ExtractionConfig:
    max_text_chars: int = 120000
    extractable_categories: list = field(
        default_factory=lambda: ["varietal_yield", "management_yield"])
    confidence_threshold: float = 0.5
    crops: list = field(default_factory=lambda: ["rice"])


@dataclass
class GeocodingConfig:
    enabled: bool = True
    use_nominatim: bool = True
    nominatim_delay: float = 1.1  # Nominatim rate limit (seconds)
    baidu_api_key: str = ""       # 百度地图 API Key（空则跳过百度）


@dataclass
class ConcurrencyConfig:
    classify_workers: int = 5   # 分类并发数
    parse_workers: int = 8      # MinerU 解析并发数
    extract_workers: int = 3    # LLM 提取并发数


@dataclass
class AppConfig:
    base_dir: Path = field(default_factory=lambda: Path("."))
    papers_dir: Path = field(default_factory=lambda: Path("docs"))
    cache_dir: Path = field(default_factory=lambda: Path("cache"))
    parsed_dir: Path = field(default_factory=lambda: Path("output/parsed"))
    runs_dir: Path = field(default_factory=lambda: Path("output/runs"))
    mineru: MinerUConfig = field(default_factory=MinerUConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    extraction: ExtractionConfig = field(default_factory=ExtractionConfig)
    geocoding: GeocodingConfig = field(default_factory=GeocodingConfig)
    concurrency: ConcurrencyConfig = field(default_factory=ConcurrencyConfig)

    # 每次运行自动设置，用于区分批次输出
    run_id: str = field(default="", repr=False)

    def set_run_id(self, run_id: str | None = None):
        """设置当前运行的 ID（时间戳），后续输出路径均基于此。"""
        self.run_id = run_id or datetime.now().strftime("%Y%m%d_%H%M%S")

    @property
    def _run_path(self) -> Path:
        """当前运行的根目录。"""
        if not self.run_id:
            self.set_run_id()
        return self.base_dir / self.runs_dir / self.run_id

    # ── 共享路径（跨运行复用） ──────────────────────────

    @property
    def papers_path(self) -> Path:
        return self.base_dir / self.papers_dir

    @property
    def cache_path(self) -> Path:
        return self.base_dir / self.cache_dir

    @property
    def parsed_path(self) -> Path:
        return self.base_dir / self.parsed_dir

    # ── 每次运行独立路径 ────────────────────────────────

    @property
    def log_path(self) -> Path:
        return self._run_path / "logs"

    @property
    def classification_path(self) -> Path:
        return self._run_path / "results" / "classification"

    @property
    def extraction_path(self) -> Path:
        return self._run_path / "results" / "extraction"

    @property
    def validation_path(self) -> Path:
        return self._run_path / "results" / "validation"

    @property
    def statistics_path(self) -> Path:
        return self._run_path / "results" / "statistics"

    @property
    def db_path(self) -> Path:
        """SQLite 输出数据库路径（固定位置，跨运行共享）"""
        return self.base_dir / "output" / "paper_data.db"


def load_config(config_path: Path | None = None) -> AppConfig:
    """从 YAML 文件加载配置，返回 AppConfig 实例。"""
    path = config_path or _find_config_path()
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    paths = raw.get("paths", {})
    base = Path(paths.get("base_dir", "."))

    mineru_raw = raw.get("mineru", {})
    mineru = MinerUConfig(
        base_url=mineru_raw.get("base_url", MinerUConfig.base_url),
        api_key=mineru_raw.get("api_key", ""),
        lang_list=mineru_raw.get("lang_list", ["ch", "en"]),
        poll_interval=mineru_raw.get("poll_interval", 5),
        poll_timeout=mineru_raw.get("poll_timeout", 600),
        return_md=mineru_raw.get("return_md", True),
        return_content_list=mineru_raw.get("return_content_list", False),
        return_middle_json=mineru_raw.get("return_middle_json", False),
        formula_enable=mineru_raw.get("formula_enable", True),
        table_enable=mineru_raw.get("table_enable", True),
        parse_method=mineru_raw.get("parse_method", "auto"),
    )

    llm_raw = raw.get("llm", {})
    llm = LLMConfig(
        base_url=llm_raw.get("base_url", LLMConfig.base_url),
        api_key=llm_raw.get("api_key", ""),
        model=llm_raw.get("model", "DSv4-flash"),
        max_tokens=llm_raw.get("max_tokens", 8192),
        temperature=llm_raw.get("temperature", 0.1),
        max_retries=llm_raw.get("max_retries", 3),
    )

    ext_raw = raw.get("extraction", {})
    extraction = ExtractionConfig(
        max_text_chars=ext_raw.get("max_text_chars", 120000),
        extractable_categories=ext_raw.get("extractable_categories",
                                           ["varietal_yield", "management_yield"]),
        confidence_threshold=ext_raw.get("confidence_threshold", 0.5),
        crops=ext_raw.get("crops", ["rice"]),
    )

    geo_raw = raw.get("geocoding", {})
    geocoding = GeocodingConfig(
        enabled=geo_raw.get("enabled", True),
        use_nominatim=geo_raw.get("use_nominatim", True),
        nominatim_delay=geo_raw.get("nominatim_delay", 1.1),
        baidu_api_key=geo_raw.get("baidu_api_key", ""),
    )

    conc_raw = raw.get("concurrency", {})
    concurrency = ConcurrencyConfig(
        classify_workers=conc_raw.get("classify_workers", 5),
        parse_workers=conc_raw.get("parse_workers", 8),
        extract_workers=conc_raw.get("extract_workers", 3),
    )

    return AppConfig(
        base_dir=base,
        papers_dir=Path(paths.get("papers_dir", "docs")),
        cache_dir=Path(paths.get("cache_dir", "cache")),
        parsed_dir=Path(paths.get("parsed_dir", "output/parsed")),
        runs_dir=Path(paths.get("runs_dir", "output/runs")),
        mineru=mineru,
        llm=llm,
        extraction=extraction,
        geocoding=geocoding,
        concurrency=concurrency,
    )
