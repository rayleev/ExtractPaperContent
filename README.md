# Paper Extractor — 科研文献结构化数据提取系统

基于 LangGraph 的农业科研文献结构化数据提取系统。从 PDF 论文中自动提取品种产量试验数据，支持论文分类、PDF 解析、全文理解、两阶段 LLM 提取、证据验证、地理编码、单位换算、规则验证。

**核心原则**：不让 LLM "猜" 数据。经纬度/海拔等可计算字段均由程序根据地名反查（`geo_source` 字段标记来源），LLM 只负责抄录论文中明确写出的数值。

## 架构概览

采用 LangGraph StateGraph 有向图架构，每篇论文独立走完整条流程。所有输出统一写入 PostgreSQL 数据库，支持大规模存储和查询。

```
论文 PDF / Semantic Scholar API
       │
       ▼
┌──────────────┐
│  search       │  搜索/导入论文（Semantic Scholar API）
└──────┬───────┘
       ▼
┌──────────────┐
│  classify     │  LLM 分类（仅用元数据，支持多作物配置）
└──────┬───────┘
       ▼
┌──────────────┐
│  filter       │  筛选：China + 可提取类别 + 目标作物
└──────┬───────┘  ──→ [不可提取] → END
       ▼
┌──────────────┐
│  download     │  下载 PDF（Semantic Scholar）
└──────┬───────┘  ──→ [无 PDF] → END
       ▼
┌──────────────┐
│  parse        │  全文理解（LLM 理解全文，识别品种共用关系）
└──────┬───────┘  ──→ [解析失败] → END
       ▼
┌──────────────┐
│  extract_p1   │  Phase 1: 论文级+试验级提取
└──────┬───────┘  ──→ [Phase1失败] → END
       ▼
┌──────────────┐
│  extract_p2   │  Phase 2: 品种级提取（逐试验，利用共用品种信息）
└──────┬───────┘
       ▼
┌──────────────┐
│  lookup       │  补充查找（处理 needs_lookup 的项，条件触发）
└──────┬───────┘
       ▼
┌──────────────┐
│  postprocess  │  Pydantic验证 + 产量换算 + 过滤 + 回填
└──────┬───────┘  ──→ [非中国] → END
       ▼
┌──────────────┐
│  geocode      │  地理编码（查找表 → 天地图 → 百度 → 省会兜底）
└──────┬───────┘
       ▼
┌──────────────┐
│  evidence     │  证据验证（批量验证重要字段的原文证据）
└──────┬───────┘
       ▼
┌──────────────┐
│  validate     │  规则验证（产量换算一致性、范围检查、增产率校验等）
└──────┬───────┘
       ▼  ──→ [无异常] → END
┌──────────────────┐
│  targeted_validate│  针对性 LLM 验证（仅验证规则标记为异常的记录）
└──────┬───────────┘
       ▼
      END → PostgreSQL DB + CSV 导出
```

**核心特性：**

- **全文理解**：parse 节点使用 LLM 理解全文，识别品种共用关系（CK 对照）
- **多作物支持**：config.yaml 配置目标作物，支持按作物拆分 study
- **共用品种识别**：自动识别跨试验共用的对照品种，统一名称
- **证据验证**：evidence 节点批量验证重要字段的原文证据
- **单位换算可配置**：config.yaml 配置单位换算表，支持扩展
- **规则验证编码**：validation_issues 使用编码（如 YIELD_001）便于查询
- **HTTP API**：FastAPI 提供 REST API 和监控面板
- **断点续跑**：LangGraph checkpoint，崩溃后从断点恢复
- **滑动窗口并发**：ThreadPoolExecutor 始终保持 N 个任务在跑
- **多实例部署**：通过 `INSTANCE_ID` 环境变量标识，多容器共享 PostgreSQL 协调任务

## 目录结构

```
extract4paperQC/
├── run.py                          # CLI 入口
├── config.yaml                     # 配置文件（本地运行，从 config.yaml.example 复制）
├── config.yaml.example             # 配置文件模板
├── .env.example                    # Docker 部署环境变量模板
├── requirements.txt                # Python 依赖
├── Dockerfile                      # Docker 构建
├── docker-compose.yml              # Docker Compose 多实例部署（3 实例）
├── woodpecker.yml                  # Woodpecker CI 流水线（build → deploy → 健康检查）
├── src/
│   ├── config.py                   # 配置加载 (AppConfig)，环境变量 > config.yaml > 默认值
│   ├── clients/
│   │   ├── mineru.py               # MinerU PDF 解析客户端
│   │   ├── llm.py                  # LLM API 客户端
│   │   └── semantic_scholar.py     # Semantic Scholar API 客户端
│   ├── core/
│   │   ├── loader.py               # 论文发现与元数据匹配
│   │   ├── chunker.py              # 文档层级树构建器
│   │   ├── geocoder.py             # 地理编码（4 级策略）
│   │   ├── constants.py            # 常量定义
│   │   └── models.py               # Pydantic 数据模型 + 产量换算
│   ├── graph/
│   │   ├── state.py                # PaperState 状态定义
│   │   ├── graph.py                # StateGraph + 条件路由
│   │   ├── batch.py                # BatchOrchestrator（并发+注册表+多实例领取）
│   │   ├── output.py               # PostgreSQL 输出（建表/写入/导出/数据字典）
│   │   ├── rules.py                # 规则验证引擎（纯代码）
│   │   ├── country_utils.py        # 国家判断工具
│   │   ├── postprocess_utils.py    # 后处理工具（过滤/回填）
│   │   └── nodes/                  # 节点函数
│   │       ├── search.py           # 搜索/导入论文（独立函数，非图节点）
│   │       ├── classify.py         # 分类节点
│   │       ├── filter.py           # 过滤节点
│   │       ├── download.py         # 下载节点
│   │       ├── parse.py            # 全文理解节点
│   │       ├── extract_phase1.py   # Phase 1 提取
│   │       ├── extract_phase2.py   # Phase 2 提取
│   │       ├── lookup.py           # 补充查找节点
│   │       ├── postprocess.py      # 后处理节点
│   │       ├── geocode.py          # 地理编码节点
│   │       ├── evidence.py         # 证据验证节点
│   │       └── validate.py         # 规则验证 + 针对性 LLM 验证
│   ├── prompts/                    # LLM Prompt 模板
│   │   ├── classify.txt            # 分类 prompt
│   │   ├── parse_understanding.txt # 全文理解 prompt
│   │   ├── parse_merge.txt         # 分段合并 prompt
│   │   ├── extract_paper.txt       # Phase 1 提取 prompt
│   │   ├── extract_study.txt       # Phase 2 提取 prompt
│   │   ├── extract_study_management.txt  # 管理型论文提取 prompt
│   │   ├── lookup.txt              # 补充查找 prompt
│   │   └── evidence.txt            # 证据验证 prompt
│   ├── api/                        # HTTP API
│   │   ├── main.py                 # FastAPI 应用
│   │   ├── routes.py               # API 路由
│   │   ├── schemas.py              # API 数据模型
│   │   └── static/dashboard.html   # 监控面板
│   └── output/
│       └── statistics.py           # 覆盖率统计
├── docs/                           # 论文数据（PDF/PDF/，meta/）
├── migrations/                     # 数据库迁移脚本
│   ├── add_parse_context.py        # 添加 parse_context JSONB 字段
│   ├── fix_schema.py               # 修复 schema
│   └── update_schema.py            # 更新 schema
└── tests/                          # 测试工具（本地使用）
    ├── import_by_id.py             # 通过 SS paper_id 导入
    ├── init_db.py                  # 数据库初始化
    ├── process_from_db.py          # 从数据库处理 pending 论文
    └── test_yield_convert.py       # 产量单位换算测试
```

## 安装

```bash
pip install -r requirements.txt
cp config.yaml.example config.yaml   # 然后填入实际配置
```

### 外部服务

| 服务 | 用途 | 配置 | 必须 |
|------|------|------|------|
| MinerU | PDF → Markdown 解析 | `config.yaml` → `mineru` | 是 |
| LLM API (OpenAI 兼容) | 分类 + 提取 | `config.yaml` → `llm` | 是 |
| Semantic Scholar API | 论文搜索 + PDF 下载 | `config.yaml` → `semantic_scholar`（缺省回退 `mineru`） | 否 |
| 天地图 API | 地理编码（推荐） | `config.yaml` → `geocoding.tianditu_tk` | 是 |
| 百度地图 API | 地理编码（备选） | `config.yaml` → `geocoding.baidu_api_key` | 否 |
| Open-Meteo | 海拔（无需 Key） | 公共服务 | 是（兜底用） |
| PostgreSQL | 数据存储 | `config.yaml` → `database` | 是 |

## 使用方法

### CLI

```bash
# 完整流程（默认）
python run.py

# 分步执行（自动补全前置步骤）
python run.py --step search            # 仅搜索论文（写入 paper_status 表）
python run.py --step classify          # 仅分类
python run.py --step download          # 分类 + 下载 PDF
python run.py --step parse             # 分类 + 解析 PDF
python run.py --step extract           # 完整流程

# 处理特定论文（按 DOI 或标题关键词匹配）
python run.py --paper "水稻"

# 指定配置文件
python run.py --config /path/to/config.yaml

# 启动 HTTP API 服务
python run.py --serve --port 8000
python run.py --serve --host 0.0.0.0 --port 8000
```

### 数据库初始化与导入

```bash
python tests/init_db.py                # 建表 + 写入数据字典
python tests/import_by_id.py <ss_paper_id>   # 通过 SS paper_id 导入论文
python tests/process_from_db.py        # 从数据库 pending 论文继续处理
python tests/test_yield_convert.py     # 产量单位换算自测
```

### HTTP API

启动后可访问：

| 地址 | 说明 |
|------|------|
| http://localhost:8000/ | 根路径健康检查 |
| http://localhost:8000/health | 健康检查端点 |
| http://localhost:8000/dashboard | 监控仪表盘 |
| http://localhost:8000/docs | Swagger API 文档 |

REST API 端点（前缀 `/api`）：

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/run` | 触发 pipeline（后台线程，立即返回 job_id） |
| POST | `/api/import` | 按 SS paperId 批量导入论文 |
| POST | `/api/stop` | 停止当前 pipeline（完成当前分块后停止） |
| GET | `/api/status/{job_id}` | 查询任务状态 |
| GET | `/api/jobs` | 列出所有任务 |
| GET | `/api/stats` | 表统计信息 |
| GET | `/api/progress` | 处理进度 |
| GET | `/api/status` | 论文状态列表（分页） |
| GET | `/api/status/{paper_id}/detail` | 单篇论文状态详情 |

## 配置说明

`config.yaml` 中的关键配置：

```yaml
# 路径配置
paths:
  base_dir: "."
  papers_dir: "docs"                    # PDF 子目录 docs/PDF/，元数据 docs/meta/
  cache_dir: "cache"
  parsed_dir: "output/parsed"
  runs_dir: "output/runs"

# MinerU PDF 解析服务
mineru:
  base_url: "http://your-mineru-host"
  api_key: "your-key"
  lang_list: ["ch", "en"]
  poll_interval: 5
  poll_timeout: 600
  return_md: true
  formula_enable: true
  table_enable: true
  parse_method: "ocr"                   # auto / txt / ocr

# LLM 服务
llm:
  base_url: "http://your-llm-host/v1"
  api_key: "your-key"
  model: "DSv4-flash"
  max_tokens: 8192
  temperature: 0.1
  max_retries: 5
  timeout: 600

# Semantic Scholar API（可选；缺省时复用 mineru.base_url + mineru.api_key）
semantic_scholar:
  base_url: "http://your-mineru-host"
  api_key: "your-key"
  max_retries: 5
  request_interval: 0.3

# 提取参数
extraction:
  max_text_chars: 120000
  extractable_categories:
    - "varietal_yield"
    - "management_yield"
  confidence_threshold: 0.5
  crops:                                # 目标作物列表
    - "水稻/Rice"
  search_keywords:                      # 搜索关键词
    - "水稻产量"
    - "rice yield"
  search_year_range: ""                 # 如 "2020-2025"，空则不限

# 单位换算配置（可通过配置文件新增）
unit_conversion:
  mass_to_kg:
    g: 0.001
    kg: 1.0
    t: 1000.0
    斤: 0.5
    公斤: 1.0
  area_to_ha:
    m2: 0.0001
    ha: 1.0
    hm2: 1.0
    亩: 0.0666667

# 证据验证配置
evidence_validation:
  enabled: true
  fields:
    - field: crop_species
      required: true
    - field: variety_name
      required: true
    - field: yield_raw_value
      required: true
    - field: n_raw_value
      required: false
    - field: p_raw_value
      required: false
    - field: k_raw_value
      required: false

# parse 节点配置
parse:
  chunked_enabled: true                 # 分段理解策略（长论文 + 有章节标题）
  sliding_window_enabled: true          # 滑动窗口策略（长论文 + 无章节标题）
  full_text_threshold: 0.5              # 短论文阈值（< 上下文窗口 * 阈值 → 一次性给全文）
  context_window: 128000                # LLM 上下文窗口大小（token）
  sliding_window_size: 8000             # 滑动窗口大小（字符）
  sliding_window_step: 6400             # 滑动窗口步长（字符，20% 重叠）

# 地理编码
geocoding:
  enabled: true
  use_tianditu: true
  tianditu_tk: "your-key"
  tianditu_delay: 0.2                   # 天地图请求间隔（秒）
  baidu_api_key: "your-key"             # 可选

# 并发控制
concurrency:
  extract_workers: 2                    # 并发论文数（同时跑的论文数）

# PostgreSQL 数据库
database:
  host: "postgres"                      # Docker 容器内为容器名，本地为 localhost
  port: 5432
  dbname: "paper_extractor"
  user: "postgres"
  password: "Admin123!"

# 日志配置
logging:
  level: "INFO"
  format: "%(asctime)s [%(levelname)s] %(message)s"
```

### 配置优先级

环境变量 > `config.yaml` > dataclass 默认值。支持的环境变量：

| 环境变量 | 用途 |
|---|---|
| `LLM_API_KEY` / `LLM_BASE_URL` | LLM 服务密钥/地址 |
| `MINERU_API_KEY` / `MINERU_BASE_URL` | MinerU 服务密钥/地址 |
| `SS_API_KEY` / `SS_BASE_URL` | Semantic Scholar 服务密钥/地址 |
| `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` | PostgreSQL 连接 |
| `TIANDITU_TK` / `BAIDU_API_KEY` | 地理编码密钥 |
| `INSTANCE_ID` | 多实例标识（用于任务领取 `claim_tasks`） |
| `LOG_LEVEL` | 日志级别 |

## 数据库结构

所有数据统一存储在 PostgreSQL 数据库。表名使用前缀分组：`pe_core_*`（核心数据）、`pe_aud_*`（审计/验证）、`pe_reg_*`（注册表）、`pe_log_*`（日志）。

### 表结构

| 表 | 用途 |
|---|---|
| `pe_core_papers` | 论文级元数据（含 `parse_context` JSONB） |
| `pe_core_studies` | 试验级信息（一年×一站 = 一个试验） |
| `pe_core_varieties` | 品种产量数据（主数据表，一行 = 一个品种×一个试验） |
| `varieties_flat` | 宽表（paper+study+variety 全字段，交接用） |
| `pe_aud_classification` | 论文分类结果（5 类） |
| `pe_aud_validation_issues` | 验证问题明细（issue=严重 / warning=警告） |
| `pe_aud_evidence` | 证据验证明细（字段来源追溯） |
| `pe_reg_paper_status` | 论文处理状态（兼任务协调注册表 + 搜索元数据存储） |
| `pe_log_pdf_missing` | 无法获取 PDF 的论文记录 |
| `_schema_doc` | 字段数据字典（DB 内查询） |

### 验证问题编码

| 编码 | 严重度 | 说明 |
|------|--------|------|
| `CK_001` | warning | 缺少对照品种 |
| `YEAR_001` | issue | trial_year > publication_year |
| `GEO_001` | warning | 经纬度超出中国范围 |
| `GEO_002` | warning | 经纬度非数值，已忽略范围检查 |
| `MULTI_SITE_001` | warning | experimental_site_name 包含多个地点 |
| `YIELD_001` | issue | 产量换算不一致 |
| `YIELD_002` | warning | 产量异常（超出合理范围） |
| `YIELD_003` | warning | 增产率偏差 |
| `YIELD_004` | issue | yield_raw_unit 为 %（增产比例，非实际产量） |
| `SOURCE_001` | warning | source_location 为空 |
| `CONSISTENCY_001` | warning | 跨 study 产量波动 >50% |
| `NUTRIENT_001` | warning | treatment_name 存在但 N/P/K raw 全空（该处理声称是处理但未抄到任何养分量） |
| `TREATMENT_001` | warning | management_yield 论文缺少 treatment_name |

## 论文分类标准

| 类别 | 说明 | 是否提取 |
|------|------|----------|
| `varietal_yield` | 品种型产量 — 核心关注基因型表现 | 是 |
| `management_yield` | 管理/环境型产量 — 核心关注栽培措施 | 是 |
| `remote_sensing_yield` | 遥感/区域宏观产量 | 否 |
| `mechanistic_yield` | 机制型产量 — 分子/生理机制 | 否 |
| `irrelevant` | 无关 | 否 |

## 单位换算

LLM 提取原始产量值和单位，程序自动换算为 kg/ha。换算表在 `config.yaml` 中配置，支持扩展。

### 支持的单位格式

| 格式 | 示例 | 说明 |
|------|------|------|
| Unicode 上标 | `kg·ha⁻¹`, `kg·hm⁻²` | 标准格式 |
| 普通减号 | `t·hm-2`, `kg·ha-1` | 兼容格式 |
| 中间点 | `kg·hm-2`, `t·ha-1` | 兼容格式 |
| 特定面积 | `kg/667m2`, `kg/20m2` | 特定面积 |
| 中文单位 | `g·株-1`, `公斤/亩` | 中文单位 |

### 上下文辅助换算

非标准面积单位（如 `kg/plot`、`g/株`）会结合 `plot_size` / `planting_density` 上下文换算：
- `kg/plot` + `plot_size="13.3 m²"` → 按 10000 m²/ha 比例换算
- `g/株` + `planting_density="22.5万穴/公顷"` → 按密度换算

### 地理编码策略（4 级优先）

```
论文中明确写出的经纬度 → 内置机构查找表 → 天地图 API → 百度地图 API → 省会兜底
     (geo_source=paper)   (geo_source=lookup)  (geo_source=tianditu)  (geo_source=baidu)  (province_fallback)
```

**海拔（3 级优先）**：geocode 结果 → Open-Meteo Elevation API（指数退避重试 3 次） → 省会海拔近似值。

缓存键：`region||site`，持久化到 `cache/geocoding_cache.json`。

## Docker 部署

### 单实例

```bash
docker build -t paper-extractor:latest .
docker run -d --name extractor \
  -p 8000:8000 \
  -v $(pwd)/config.yaml:/app/config.yaml:ro \
  -v $(pwd)/docs:/app/docs \
  -v $(pwd)/output:/app/output \
  -v $(pwd)/cache:/app/cache \
  --env-file .env \
  paper-extractor:latest
```

### 多实例（docker-compose）

`docker-compose.yml` 默认启动 3 个 extractor 实例，共享 PostgreSQL 与挂载目录，各自独立 cache 目录避免 SQLite 锁冲突：

```bash
cp .env.example .env             # 填入 DB_PASSWORD / LLM_API_KEY_1/2/3 等
docker compose up -d --build
docker compose logs -f
docker compose down
```

端口映射（宿主机:容器）：
- `extractor-1` → `8004:8000`
- `extractor-2` → `8002:8000`
- `extractor-3` → `8003:8000`

每个实例通过 `INSTANCE_ID` 环境变量标识，`BatchOrchestrator.claim_tasks` 实现 multi-instance 任务领取（processing 状态论文锁定到具体实例，避免重复处理）。

### Woodpecker CI

`woodpecker.yml` 定义 `build → deploy` 流水线：
- 构建镜像 → 写入 secrets 注入的 `config.yaml` / `.env` → 启动 docker compose → 健康检查（最多 60s）→ 失败回滚到旧镜像。

所需 Woodpecker secrets：`CONFIG_YAML`、`ENV_FILE`。

## Prompt 调优

Prompt 模板在 `src/prompts/` 下，修改后清除 checkpoint 重新运行：

| 文件 | 用途 |
|------|------|
| `classify.txt` | 论文分类 |
| `parse_understanding.txt` | 全文理解 |
| `parse_merge.txt` | 分段合并 |
| `extract_paper.txt` | Phase 1 提取 |
| `extract_study.txt` | Phase 2 提取 |
| `extract_study_management.txt` | 管理型论文提取 |
| `lookup.txt` | 补充查找 |
| `evidence.txt` | 证据验证 |

```bash
# 清除 checkpoint（从头开始）
del cache\langgraph_checkpoint.db
del cache\geocoding_cache.json
```

### 防重机制

- **稳定 paper_id**：`P_{MD5(标题归一化)[:10]}`，同一标题跨运行 ID 不变
- **注册表**：`pe_reg_paper_status` 表记录每篇论文完成的最高步骤
- 强制重跑：从 `pe_reg_paper_status` 表删除对应记录

## 开发说明

### 添加新节点

1. 在 `src/graph/nodes/` 新建 `mynode.py`，函数签名 `(state: PaperState, config, ...) -> dict`
2. 在 `src/graph/state.py` 的 `PaperState` 添加输出字段
3. 在 `src/graph/graph.py` 注册节点 + 条件路由函数
4. 在 `src/graph/nodes/__init__.py` 导出

## License

Internal use only.
