# 项目记忆 — Paper Extractor

> 基于代码库实际扫描生成，非编造。所有 `[未检测到]` 标注表示未在代码中找到对应证据。

## 技术栈

| 层级 | 技术 | 版本要求 | 证据来源 |
|------|------|---------|---------|
| 开发语言 | Python | 3.11+（Dockerfile 基镜像 `python:3.11-slim`） | [Dockerfile](Dockerfile#L1) |
| 工作流引擎 | LangGraph | `>=1.0` | [requirements.txt](requirements.txt#L21) |
| Checkpoint 持久化 | langgraph-checkpoint-sqlite | `>=3.0` | [requirements.txt](requirements.txt#L22) |
| LangChain 基础 | langchain-core | `>=0.3` | [requirements.txt](requirements.txt#L23) |
| 数据模型 | Pydantic | `>=2.0` | [requirements.txt](requirements.txt#L2) |
| 配置 | PyYAML | [未检测到版本约束] | [requirements.txt](requirements.txt#L3) |
| HTTP 客户端 | requests | [未检测到版本约束] | [requirements.txt](requirements.txt#L4) |
| 数据库 | PostgreSQL + psycopg2-binary | `>=2.9` | [requirements.txt](requirements.txt#L7) |
| API 服务 | FastAPI + Uvicorn | `>=0.100` / `>=0.23` | [requirements.txt](requirements.txt#L10-L11) |
| 表单解析 | python-multipart | `>=0.0.6` | [requirements.txt](requirements.txt#L12) |
| 外部服务 | MinerU / LLM(OpenAI 兼容) / Semantic Scholar / 天地图 / 百度 / Open-Meteo | HTTP 调用，无 SDK | [requirements.txt](requirements.txt#L14-L18) |
| 容器化 | Docker + docker-compose | Docker 24（CI 镜像） | [.woodpecker.yml](.woodpecker.yml#L26) |
| CI/CD | Woodpecker | [未检测到版本约束] | [.woodpecker.yml](.woodpecker.yml) |

**关键特征**：所有外部服务（MinerU/LLM/地理编码）均通过 `requests`/`httpx` HTTP 调用，**不引入任何外部 SDK**。

## 架构决策记录 (ADR)

### ADR-1: LangGraph StateGraph 有向图作为核心流程引擎
- **决策**：每篇论文独立走完整条流程，节点间通过 `PaperState` (TypedDict) 传递状态
- **位置**：[src/graph/graph.py](src/graph/graph.py)、[src/graph/state.py](src/graph/state.py#L12)
- **理由**：流程节点多（13 个）、有条件路由和动态节点（lookup），有向图天然适配
- **后果**：每篇论文独立 checkpoint，崩溃可恢复；但单篇流程串行，并发靠 BatchOrchestrator 滑动窗口

### ADR-2: 不让 LLM "猜" 可计算字段
- **决策**：经纬度/海拔/单位换算/`yield_standard_value` 等由程序计算，`geo_source` 标记来源
- **位置**：[src/core/models.py](src/core/models.py#L67)（`[PROGRAM]` 字段标记）
- **理由**：LLM 易在数值计算上出错；地名 → 经纬度有现成地理编码 API
- **后果**：模型字段分四级标记 `[REQUIRED]` / `[OPTIONAL]` / `[PROGRAM]` / `[DO NOT FILL]`

### ADR-3: 两阶段 LLM 提取（Phase 1 + Phase 2）
- **决策**：Phase 1 提取论文级 + 试验级；Phase 2 逐试验提取品种级（主要瓶颈）
- **位置**：[src/graph/nodes/extract_phase1.py](src/graph/nodes/extract_phase1.py)、[src/graph/nodes/extract_phase2.py](src/graph/nodes/extract_phase2.py)
- **理由**：单次 LLM 调用无法容纳整篇论文 + 全部品种细节；分阶段降低 token 压力
- **后果**：Phase 2 每试验 1 次大 LLM 调用（max_tokens=8192），是性能瓶颈

### ADR-4: SQLite Checkpoint + PostgreSQL 业务库
- **决策**：LangGraph 用 SQLite 做断点续跑（`cache/langgraph_checkpoint.db`），业务数据写 PostgreSQL
- **位置**：[docker-compose.yml](docker-compose.yml#L10-L11)（注释明确：多实例独立 cache 目录避免 SQLite 锁冲突）
- **理由**：SQLite 轻量、无需部署；业务数据需要并发查询与跨实例共享
- **后果**：多实例部署时**每个实例必须独立 cache 目录**（`cache-1/2/3`），否则锁冲突

### ADR-5: 多实例任务领取（INSTANCE_ID 锁定）
- **决策**：`BatchOrchestrator.claim_tasks` 把 `processing` 状态论文锁定到具体 `INSTANCE_ID`
- **位置**：[src/graph/batch.py](src/graph/batch.py)、[docker-compose.yml](docker-compose.yml#L60)（`INSTANCE_ID: instance_1/2/3`）
- **理由**：3 个实例共享 PostgreSQL，必须避免重复处理
- **后果**：修改 `claim_tasks` 不能丢掉实例锁定逻辑

### ADR-6: 稳定 paper_id（MD5 标题归一化）
- **决策**：`paper_id = P_{MD5(标题归一化)[:10]}`，同一标题跨运行 ID 不变
- **位置**：AGENTS.md 防重机制节
- **理由**：跨运行/跨实例去重需要稳定标识；DOI 不一定都有
- **后果**：防重机制不可破坏；强制重跑需从 `pe_reg_paper_status` 表删记录

### ADR-7: 数据库表名前缀分组
- **决策**：`pe_core_*`（核心）/ `pe_aud_*`（审计）/ `pe_reg_*`（注册表）/ `pe_log_*`（日志）
- **位置**：README 数据库结构节
- **理由**：单库多职责，前缀分组便于权限与运维管理

### ADR-8: Woodpecker CI 失败自动回滚
- **决策**：部署失败时回滚到旧镜像；健康检查最多 60s（20 次 × 3s）
- **位置**：[.woodpecker.yml](.woodpecker.yml#L84-L93)、[.woodpecker.yml](.woodpecker.yml#L115-L125)
- **理由**：3 实例同时更新，失败需快速回退

### ADR-9: 非 root 运行 Docker 容器
- **决策**：`USER 1000:1000`，部署目录 `/home/root01/deploy/paper-extractor/`
- **位置**：[Dockerfile](Dockerfile#L38)、[docker-compose.yml](docker-compose.yml#L64)
- **理由**：避免 bind mount 目录下创建 root 属主子目录导致权限问题
- **后果**：`output/runs` 必须在镜像内预创建并 chown

## 目录结构约定

```
extract4paperQC/
├── run.py                  # CLI 入口（argparse）
├── config.yaml.example     # 配置模板（config.yaml 不入仓，含密钥）
├── .env.example            # Docker 部署环境变量模板
├── requirements.txt        # 依赖清单（按 Core/Database/API/LangGraph 分组注释）
├── Dockerfile              # python:3.11-slim + 阿里云镜像加速
├── docker-compose.yml      # 3 实例 + 独立 cache 目录
├── .woodpecker.yml         # CI 流水线
├── src/
│   ├── config.py           # AppConfig（环境变量 > config.yaml > 默认值）
│   ├── clients/            # 外部服务客户端（mineru/llm/semantic_scholar）
│   ├── core/               # 核心模型与工具（models/geocoder/chunker/loader/constants）
│   ├── graph/
│   │   ├── state.py        # PaperState (TypedDict)
│   │   ├── graph.py        # StateGraph + 条件路由
│   │   ├── batch.py        # BatchOrchestrator（并发+注册表+多实例领取）
│   │   ├── output.py       # PostgreSQL 输出
│   │   ├── rules.py        # 规则验证引擎
│   │   └── nodes/          # 13 个节点函数（每个一个 .py）
│   ├── prompts/            # LLM Prompt 模板（.txt 文件，8 个）
│   ├── api/                # FastAPI HTTP 服务（main/routes/schemas/static）
│   └── output/
│       └── statistics.py   # 覆盖率统计
├── migrations/             # 数据库迁移脚本（add_parse_context/fix_schema/update_schema）
└── [tests/ 未检测到]       # AGENTS.md 引用但实际不存在
└── [docs/ 未检测到]        # config.yaml.example 引用 papers_dir: "docs" 但实际不存在
```

**约定**：
- 节点函数与文件一一对应（`parse.py` → `parse` 节点）
- Prompt 文件用下划线命名（`extract_study.txt`、`parse_understanding.txt`）
- 客户端与服务一一对应（`mineru.py` → MinerU 服务）
- 数据库迁移脚本独立成文件，不引入迁移框架

## 命名规范

### Python 代码（从 [models.py](src/core/models.py)、[state.py](src/graph/state.py)、[run.py](run.py) 推断）

| 类别 | 规范 | 示例 |
|------|------|------|
| 类名 | PascalCase | `PaperState`、`ExtractionResult`、`BatchOrchestrator`、`ExperimentalDesignType` |
| 函数/方法 | snake_case | `setup_logging`、`discover_papers`、`process_batch`、`claim_tasks` |
| 变量 | snake_case | `paper_id`、`stop_after`、`extract_workers` |
| 常量/枚举值 | UPPER_SNAKE | `RCBD`、`SPLIT_PLOT`、`PLOT_MEAN`、`HIGH` |
| 私有方法 | 单下划线前缀 | `_parse_pct_over_check`、`_timed()` |
| 模块文件 | snake_case | `extract_phase1.py`、`postprocess_utils.py` |
| Pydantic 字段 | snake_case + 类型后缀 | `paper_doi`、`publication_year`、`yield_raw_value` |
| TypedDict 字段 | snake_case + 分组注释 | `# ── parse 节点 ──` 分组 |

### 文档与配置

| 类别 | 规范 | 示例 |
|------|------|------|
| Prompt 文件 | snake_case `.txt` | `parse_understanding.txt`、`extract_study_management.txt` |
| 数据库表 | `pe_{group}_{name}` 前缀 | `pe_core_papers`、`pe_aud_validation_issues` |
| 验证问题编码 | `XXX_NNN` | `YIELD_001`、`GEO_002`、`CK_001` |
| 环境变量 | UPPER_SNAKE | `LLM_API_KEY`、`INSTANCE_ID`、`DB_HOST` |
| Docker 容器 | `extractor-{N}` | `extractor-1`、`extractor-2`、`extractor-3` |
| INSTANCE_ID | `instance_{N}` | `instance_1` |

### 代码注释风格
- 中文为主，技术术语保留英文（如 `paper_id`、`checkpoint`、`thread_id`）
- 模块级 docstring 用三引号 + 用法示例（见 [models.py](src/core/models.py#L1-L16)）
- 字段标记约定：`[REQUIRED]` / `[OPTIONAL]` / `[PROGRAM]` / `[DO NOT FILL]`
- 章节分隔用 `# ── xxx ──` 注释（见 [state.py](src/graph/state.py#L15)）

## 关键业务流程

### 主流程（有向图，13 节点）

```
search → classify → filter → download → parse → extract_phase1 → extract_phase2
  → [lookup] → postprocess → geocode → evidence → validate → [targeted_validate] → END
```

- **条件路由**：filter/parse/phase1/postprocess 失败时提前 END
- **动态节点**：`lookup` 仅在 parse 输出 `needs_lookup=True` 时触发
- **分步执行**：`--step` 通过 `PaperState.stop_after` 控制停止节点

### 地理编码优先级（4 级）

```
论文明文经纬度 → 内置机构查找表 → 天地图 API → 百度地图 API → 省会兜底
geo_source=paper  geo_source=lookup  geo_source=tianditu  geo_source=baidu  province_fallback
```

海拔（3 级）：geocode 结果 → Open-Meteo Elevation API → 省会海拔近似值

### 单位换算流程

LLM 提取 `yield_raw_value` + `yield_raw_unit` → 程序按 `config.yaml → unit_conversion` 换算为 `yield_standard_value`（kg/ha）
- 支持 Unicode 上标 / 普通减号 / 中间点 / 中文单位
- 非标准单位（`kg/plot`、`g/株`）结合 `plot_size` / `planting_density` 上下文换算

### 部署流程（Woodpecker CI）

```
build 镜像 → 写入 secrets 到 /home/root01/deploy/paper-extractor/{config.yaml,.env}
  → 停旧容器 → docker compose up -d --build → 健康检查（20×3s=60s）
  → 失败回滚到旧镜像
```

## 已知技术债务

**暂无**。

- 扫描 `TODO` / `FIXME` / `HACK` / `XXX` 注释：**无匹配**
- 代码中无明显技术债务标记

## 环境配置说明

### 本地运行
- **依赖**：`pip install -r requirements.txt`
- **配置**：`cp config.yaml.example config.yaml` 后填入实际值
- **必填配置**：MinerU 服务、LLM 服务、PostgreSQL、天地图 TK
- **可选**：百度地图 Key、Semantic Scholar 独立配置（缺省回退 MinerU）

### Docker 部署
- **镜像**：`python:3.11-slim`，阿里云镜像加速（Debian 源 + pip 源）
- **运行用户**：`uid 1000:1000`（非 root）
- **健康检查**：`curl -f http://localhost:8000/health`，30s 间隔
- **必填 secrets**：`CONFIG_YAML`、`ENV_FILE`（Woodpecker CI）
- **必填 .env**：`DB_PASSWORD`、`LLM_API_KEY_1/2/3`、`MINERU_API_KEY_1/2/3`
- **端口分配**：8004 / 8002 / 8003（避免与 paperrag 的 8000/8001 冲突，见 [.env.example](.env.example#L33) 注释）

### 配置优先级

```
环境变量 > config.yaml > dataclass 默认值
```

### 关键环境变量

| 变量 | 用途 | 必填 |
|------|------|------|
| `LLM_API_KEY` / `LLM_BASE_URL` | LLM 服务 | 是 |
| `MINERU_API_KEY` / `MINERU_BASE_URL` | MinerU PDF 解析 | 是 |
| `SS_API_KEY` / `SS_BASE_URL` | Semantic Scholar | 否（回退 MinerU） |
| `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` | PostgreSQL | 是 |
| `TIANDITU_TK` / `BAIDU_API_KEY` | 地理编码 | 是 / 否 |
| `INSTANCE_ID` | 多实例标识 | Docker 部署必填 |
| `LOG_LEVEL` | 日志级别 | 否（默认 INFO） |

## 踩坑记录

### 1. `tests/` 目录在 AGENTS.md / README.md 中被引用，但实际不存在
- **现象**：AGENTS.md 的 Commands 节、README.md 的"数据库初始化与导入"节均引用 `python tests/init_db.py`、`python tests/import_by_id.py`、`python tests/process_from_db.py`、`python tests/test_yield_convert.py`，但项目根目录下**没有 `tests/` 目录**
- **影响**：文档与代码不一致；新成员按文档运行命令会失败
- **建议**：补充 `tests/` 目录，或修订文档

### 2. `docs/` 目录在 config.yaml.example 中被引用，但实际不存在
- **现象**：[config.yaml.example](config.yaml.example#L14) 配置 `papers_dir: "docs"`，但项目根目录下**没有 `docs/` 目录**
- **缓解**：Dockerfile 在镜像内预创建 `docs/PDF`、`docs/meta`（[Dockerfile](Dockerfile#L29)）；docker-compose 通过 bind mount 挂载宿主机 `docs/PDF`、`docs/meta`
- **影响**：本地首次运行需手动创建 `docs/PDF/` 和 `docs/meta/` 子目录

### 3. 多实例 SQLite 锁冲突
- **现象**：3 个实例共享 cache 目录会导致 SQLite checkpoint 文件锁冲突
- **解决**：每个实例独立 cache 目录（`cache-1/2/3`），见 [docker-compose.yml](docker-compose.yml#L10-L11) 注释
- **教训**：修改部署配置时不能合并 cache 目录

### 4. bind mount 目录权限问题
- **现象**：容器以 `uid 1000` 运行，若宿主机 bind mount 目录属主为 root，容器无法写入
- **解决**：Dockerfile 在镜像内预创建 `output/runs` 并 `chown 1000:1000`，见 [Dockerfile](Dockerfile#L26-L30) 注释
- **教训**：新增 bind mount 目录时需同步调整 Dockerfile 的 chown

### 5. `extract_workers` 配置不一致
- **现象**：[config.yaml.example](config.yaml.example#L73) 中 `extract_workers: 3`，但 README.md 和 AGENTS.md 文档中标注"默认 5"
- **影响**：文档与示例配置不一致，易误解
- **建议**：统一为 5 或在文档说明可调

---

**文档生成时间**：2026-08-03
**扫描范围**：项目根目录 + src/ + migrations/ + 配置文件 + 部署文件
**未扫描**：`.git/`、`cache/`、`output/`、`docs/`（后两者不存在）
