"""
配置管理模块 — 从 config.yaml 加载并集中管理所有运行时配置。

所有环境相关的值（IP、URL、密钥、密码）默认为空，
必须通过 config.yaml 或环境变量提供。
环境变量优先级高于 config.yaml：
  LLM_API_KEY, MINERU_API_KEY, SS_API_KEY, DB_PASSWORD
"""

import os
import yaml
from pathlib import Path
from dataclasses import dataclass, field, fields, MISSING
from datetime import datetime


def _find_config_path() -> Path:
    """向上查找 config.yaml，优先使用项目根目录的配置文件。"""
    here = Path(__file__).resolve().parent.parent  # src/ 的上一级 = 项目根
    cfg = here / "config.yaml"
    if cfg.exists():
        return cfg
    raise FileNotFoundError(f"config.yaml not found at {here}")


def _env_or_yaml(env_key: str, yaml_dict: dict, yaml_key: str, default=""):
    """
    三级取值：环境变量 > config.yaml > 默认值。

    用于敏感配置（API Key、密码等），环境变量优先级最高。
    """
    env_val = os.environ.get(env_key)
    if env_val:
        return env_val
    return yaml_dict.get(yaml_key, default)


# ── 配置数据类 ──────────────────────────────────────────
# 默认值均为"通用安全值"（空字符串、标准端口等），
# 环境相关的具体值必须通过 config.yaml 或环境变量提供。

@dataclass
class MinerUConfig:
    base_url: str = ""              # 必填：MinerU 服务地址
    api_key: str = ""               # 必填：MinerU API Key
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
    base_url: str = ""              # 必填：LLM API 地址
    api_key: str = ""               # 必填：LLM API Key
    model: str = "DSv4-flash"
    max_tokens: int = 8192
    temperature: float = 0.1
    max_retries: int = 5
    timeout: int = 600


@dataclass
class ExtractionConfig:
    max_text_chars: int = 120000
    extractable_categories: list = field(
        default_factory=lambda: ["varietal_yield", "management_yield"])
    confidence_threshold: float = 0.5
    crops: list = field(default_factory=lambda: ["rice"])
    search_keywords: list = field(
        default_factory=lambda: ["水稻产量", "rice yield"])
    search_year_range: str = ""


@dataclass
class GeocodingConfig:
    enabled: bool = True
    baidu_api_key: str = ""
    use_tianditu: bool = True
    tianditu_tk: str = ""
    tianditu_delay: float = 0.2
    # 地理编码服务地址（可选覆盖，默认使用公共服务）
    tianditu_url: str = "https://api.tianditu.gov.cn/geocoder"
    baidu_url: str = "https://api.map.baidu.com/geocoding/v3/"
    elevation_url: str = "https://api.open-meteo.com/v1/elevation"


@dataclass
class ConcurrencyConfig:
    classify_workers: int = 5
    parse_workers: int = 8
    extract_workers: int = 3


@dataclass
class DatabaseConfig:
    host: str = ""                  # 必填：PostgreSQL 主机地址
    port: int = 5432
    dbname: str = "paper_extractor"
    user: str = "postgres"
    password: str = ""              # 必填：PostgreSQL 密码

    @property
    def connection_string(self) -> str:
        if not self.host:
            raise ValueError("database.host is required (set in config.yaml or via environment)")
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.dbname}"


@dataclass
class SemanticScholarConfig:
    base_url: str = ""              # 默认复用 mineru.base_url（同一服务）
    api_key: str = ""               # 默认复用 mineru.api_key（同一 API Key）
    max_retries: int = 5
    request_interval: float = 0.3


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
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    semantic_scholar: SemanticScholarConfig = field(default_factory=SemanticScholarConfig)

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

    @property
    def pdf_path(self) -> Path:
        """PDF 存储根目录（按年份子目录组织）。"""
        return self.papers_path / "PDF"

    @property
    def meta_path(self) -> Path:
        """搜索元数据 CSV 存储目录。"""
        return self.papers_path / "meta"

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


# ── 加载函数 ──────────────────────────────────────────────

def _get_default(cls, field_name: str):
    """从 dataclass 获取字段的默认值（避免在 load_config 中重复写默认值）。"""
    for f in fields(cls):
        if f.name == field_name:
            if f.default_factory is not MISSING:
                return f.default_factory()
            if f.default is not MISSING:
                return f.default
            return None
    return None


def load_config(config_path: Path | None = None) -> AppConfig:
    """
    从 YAML 文件加载配置，返回 AppConfig 实例。

    优先级：环境变量 > config.yaml > dataclass 默认值
    """
    path = config_path or _find_config_path()
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    paths = raw.get("paths", {})
    base = Path(paths.get("base_dir", "."))

    # ── MinerU ──
    m = raw.get("mineru", {})
    mineru = MinerUConfig(
        base_url=m.get("base_url", _env_or_yaml("MINERU_BASE_URL", m, "base_url", MinerUConfig.base_url)),
        api_key=_env_or_yaml("MINERU_API_KEY", m, "api_key"),
        lang_list=m.get("lang_list", _get_default(MinerUConfig, "lang_list")),
        poll_interval=m.get("poll_interval", _get_default(MinerUConfig, "poll_interval")),
        poll_timeout=m.get("poll_timeout", _get_default(MinerUConfig, "poll_timeout")),
        return_md=m.get("return_md", _get_default(MinerUConfig, "return_md")),
        return_content_list=m.get("return_content_list", _get_default(MinerUConfig, "return_content_list")),
        return_middle_json=m.get("return_middle_json", _get_default(MinerUConfig, "return_middle_json")),
        formula_enable=m.get("formula_enable", _get_default(MinerUConfig, "formula_enable")),
        table_enable=m.get("table_enable", _get_default(MinerUConfig, "table_enable")),
        parse_method=m.get("parse_method", _get_default(MinerUConfig, "parse_method")),
    )

    # ── LLM ──
    l = raw.get("llm", {})
    llm = LLMConfig(
        base_url=l.get("base_url", _env_or_yaml("LLM_BASE_URL", l, "base_url", LLMConfig.base_url)),
        api_key=_env_or_yaml("LLM_API_KEY", l, "api_key"),
        model=l.get("model", _get_default(LLMConfig, "model")),
        max_tokens=l.get("max_tokens", _get_default(LLMConfig, "max_tokens")),
        temperature=l.get("temperature", _get_default(LLMConfig, "temperature")),
        max_retries=l.get("max_retries", _get_default(LLMConfig, "max_retries")),
        timeout=l.get("timeout", _get_default(LLMConfig, "timeout")),
    )

    # ── 提取参数 ──
    e = raw.get("extraction", {})
    extraction = ExtractionConfig(
        max_text_chars=e.get("max_text_chars", _get_default(ExtractionConfig, "max_text_chars")),
        extractable_categories=e.get("extractable_categories", _get_default(ExtractionConfig, "extractable_categories")),
        confidence_threshold=e.get("confidence_threshold", _get_default(ExtractionConfig, "confidence_threshold")),
        crops=e.get("crops", _get_default(ExtractionConfig, "crops")),
        search_keywords=e.get("search_keywords", _get_default(ExtractionConfig, "search_keywords")),
        search_year_range=e.get("search_year_range", _get_default(ExtractionConfig, "search_year_range")),
    )

    # ── 地理编码 ──
    g = raw.get("geocoding", {})
    geocoding = GeocodingConfig(
        enabled=g.get("enabled", _get_default(GeocodingConfig, "enabled")),
        baidu_api_key=_env_or_yaml("BAIDU_API_KEY", g, "baidu_api_key"),
        use_tianditu=g.get("use_tianditu", _get_default(GeocodingConfig, "use_tianditu")),
        tianditu_tk=_env_or_yaml("TIANDITU_TK", g, "tianditu_tk"),
        tianditu_delay=g.get("tianditu_delay", _get_default(GeocodingConfig, "tianditu_delay")),
        tianditu_url=g.get("tianditu_url", _get_default(GeocodingConfig, "tianditu_url")),
        baidu_url=g.get("baidu_url", _get_default(GeocodingConfig, "baidu_url")),
        elevation_url=g.get("elevation_url", _get_default(GeocodingConfig, "elevation_url")),
    )

    # ── 并发 ──
    c = raw.get("concurrency", {})
    concurrency = ConcurrencyConfig(
        classify_workers=c.get("classify_workers", _get_default(ConcurrencyConfig, "classify_workers")),
        parse_workers=c.get("parse_workers", _get_default(ConcurrencyConfig, "parse_workers")),
        extract_workers=c.get("extract_workers", _get_default(ConcurrencyConfig, "extract_workers")),
    )

    # ── 数据库 ──
    d = raw.get("database", {})
    database = DatabaseConfig(
        host=_env_or_yaml("DB_HOST", d, "host", DatabaseConfig.host),
        port=int(_env_or_yaml("DB_PORT", d, "port", DatabaseConfig.port)),
        dbname=d.get("dbname", _get_default(DatabaseConfig, "dbname")),
        user=_env_or_yaml("DB_USER", d, "user", DatabaseConfig.user),
        password=_env_or_yaml("DB_PASSWORD", d, "password"),
    )

    # ── Semantic Scholar（与 MinerU 共用同一服务和 API Key）──
    s = raw.get("semantic_scholar", {})
    semantic_scholar = SemanticScholarConfig(
        base_url=s.get("base_url", _env_or_yaml("SS_BASE_URL", s, "base_url", mineru.base_url)),
        api_key=_env_or_yaml("SS_API_KEY", s, "api_key", mineru.api_key),
        max_retries=s.get("max_retries", _get_default(SemanticScholarConfig, "max_retries")),
        request_interval=s.get("request_interval", _get_default(SemanticScholarConfig, "request_interval")),
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
        database=database,
        semantic_scholar=semantic_scholar,
    )
