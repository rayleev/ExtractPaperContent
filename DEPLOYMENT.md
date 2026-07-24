# 部署与运维手册

extract4paperQC 多实例 Docker 部署的手动操作流程。所有命令均可直接复制执行。

## 环境信息

| 项 | 值 |
|----|----|
| 服务器 | `10.33.105.145`，SSH 用户 `root01`（密钥免密，非 root） |
| 项目路径 | `/home/root01/extract4paperQC` |
| 三实例 | extractor-1 → `:8004`，extractor-2 → `:8002`，extractor-3 → `:8003` |
| Gitea | `http://172.17.1.130:3000/phenomics/ExtractPaperContent.git` |
| Dashboard | `http://10.33.105.145:8004/dashboard`（任一端口的 /dashboard 均可） |
| Git 身份 | `admin <admin@phenomics.local>`（机器无全局 config，按次传入） |

代码是 `COPY` 进镜像的（非 bind mount）；只有 `config.yaml`、`docs/PDF`、`docs/meta`、`output/parsed`、`output/logs`、`cache` 是 bind mount。所以**改代码必须重建镜像**才生效。

---

## 一、标准部署流程（提交代码后正式上线）

### 1. 本地提交 + 推送（Windows）

```bash
cd /d D:\workspace\local_project\extract4paperQC

git status                      # 确认改动
git --no-pager diff             # 复核 diff

git add <具体文件>               # 按文件名加，避免 git add -A 误带敏感文件
git -c user.name=admin -c user.email=admin@phenomics.local commit -m "fix: 说明"
git push origin master
```

### 2. 服务器拉取

```bash
ssh root01@10.33.105.145
cd /home/root01/extract4paperQC
git pull --ff-only
git --no-pager log --oneline -1   # 确认 HEAD 是刚推的 commit
```

### 3. 停容器 + 删旧镜像 + 清构建缓存

> ⚠️ BuildKit 的 `--no-cache` 有时仍不刷新 `COPY` 层，**必须先 `rmi` + `builder prune`** 再 build，否则可能拿到旧代码。

```bash
docker compose down
docker rmi paper-extractor:latest
docker builder prune -f
```

### 4. 重建镜像（无缓存）

```bash
docker compose build --no-cache
```

耗时几分钟（pip 装依赖）。可加 `tail` 看尾部输出。

### 5. 启动三实例

```bash
docker compose up -d
```

### 6. 验证

```bash
# 健康检查（三实例都应 database: ok）
for p in 8004 8002 8003; do echo -n "port $p: "; curl -s http://localhost:$p/health; echo; done

# 确认新代码确实进了镜像（替换为你这次改动的特征字符串）
docker run --rm --entrypoint grep paper-extractor:latest -n '特征代码' /app/src/路径/文件.py
```

---

## 二、热修快速验证流程（不提交，先在单容器验证）

适合改完代码想先确认效果、再决定是否正式提交部署的场景。

```bash
# 1. 本地把改好的文件 scp 到服务器的挂载目录（output/logs → /app/output/logs）
scp src/clients/semantic_scholar.py root01@10.33.105.145:/home/root01/extract4paperQC/output/logs/_fixed.py

# 2. docker cp 进单个容器 + 重启（代码非 bind mount，必须 cp 进容器）
ssh root01@10.33.105.145 "docker cp /home/root01/extract4paperQC/output/logs/_fixed.py extractor-1:/app/src/clients/semantic_scholar.py && docker restart extractor-1"

# 3. 重置目标论文为 pending（见"常用运维操作"）
# 4. 仅在 extractor-1 触发 process 验证
ssh root01@10.33.105.145 "curl -s -X POST http://localhost:8004/api/run -H 'Content-Type: application/json' -d '{\"step\":\"process\"}'"

# 5. 查 paper_status / 计数确认效果
```

> 注意：`docker cp` 的改动只在当前容器生命周期内有效，`docker compose up -d --force-recreate` 或 `down/up` 后会丢失。验证通过后再走"标准部署流程"正式落地三实例。

---

## 三、常用运维操作

### 查健康 / 进度

```bash
# 健康（含论文数、品种数）
curl -s http://localhost:8004/health

# 当前任务（stats 运行中为 null，完成后才填充）
for p in 8004 8002 8003; do echo -n "port $p: "; curl -s http://localhost:$p/api/jobs; echo; done
```

### 查 paper_status 状态分布（跟踪进度的可靠方式）

把下面脚本存为 `.py`，scp 到 `output/logs/` 后 `docker exec extractor-1 python /app/output/logs/xxx.py`：

```python
import sys; sys.path.insert(0, "/app")
from src.config import load_config
from src.graph.output import get_connection
cfg = load_config(); conn = get_connection(cfg.database.connection_string); cur = conn.cursor()
cur.execute("SELECT status, COUNT(*) FROM paper_status GROUP BY status ORDER BY COUNT(*) DESC")
for r in cur.fetchall():
    print(" ", r[0], r[1])
conn.close()
```

### 重置论文为 pending 重跑

`process_from_db` 只领取 `pending`（`FOR UPDATE SKIP LOCKED`），改 pending 即可重跑。

```sql
-- 按条件重置（示例：重置某批 failed）
UPDATE paper_status
SET status = 'pending', error_message = NULL, claimed_by = NULL, updated_at = NOW()
WHERE status = 'failed' AND paper_id IN (SELECT paper_id FROM pdf_missing);
```

> ⚠️ 重跑**已完成**的论文前先建备份表（`CREATE TABLE xxx_bak AS SELECT ...`），因为 insert_extraction 是 upsert 不删旧行，新提取行数变少会残留脏数据。仅重跑 failed/skipped 无此问题。

### 触发 process（处理库中 pending 论文，不搜索）

```bash
# 单实例
curl -s -X POST http://localhost:8004/api/run -H 'Content-Type: application/json' -d '{"step":"process"}'

# 三实例并行（FOR UPDATE SKIP LOCKED 保证不重复领取）
for p in 8004 8002 8003; do curl -s -X POST http://localhost:$p/api/run -H 'Content-Type: application/json' -d '{"step":"process"}'; echo; done
```

### 清理 pdf_missing（该表只保留"当前仍卡住"的论文）

代码已实现：论文 `completed`/`skipped` 时自动从 pdf_missing 移除。对历史已解决记录的一次性清理：

```sql
DELETE FROM pdf_missing
WHERE paper_id IN (
    SELECT paper_id FROM paper_status
    WHERE status IN ('completed', 'skipped')
);
```

清理后 pdf_missing 只剩确实无任何全文资源（md/pdf 全无）的 failed 论文。

### 按 ID 批量导入（CSV 清单）

Dashboard 的 "Import by Paper ID" 区上传 CSV（自动识别 `article_id` 列、去重，只发 1 号实例）。或调接口：

```bash
curl -s -X POST http://localhost:8004/api/import -H 'Content-Type: application/json' -d '{"ss_paper_ids":["id1","id2"]}'
```

导入只拉元数据并标记 `pending`，不处理；之后自行决定何时触发 process。

---

## 四、注意事项 / 陷阱

- **BuildKit 缓存**：`--no-cache` 不一定刷新 `COPY` 层，重建前务必 `docker rmi` + `docker builder prune -f`。
- **容器重启中断后台 pipeline**：jobs 清空，数据不丢（ON CONFLICT + 事务回滚），但 `processing` 论文会卡住，需手动重置为 `pending`。
- **ssh heredoc 单引号陷阱**：含单引号的脚本（如 SQL 字面量）不能用 ssh heredoc，须本地 Write + scp 到挂载目录再 `docker exec`；或 SQL 用 `%s` 占位 + 双引号 params。
- **ssh 远程命令含 `$变量`**：须用单引号包裹远程命令，防止被本地 shell 提前展开。
- **/api/jobs 的 stats 运行中为 null**：完成后才填充；跟踪进度靠查 paper_status 分布。
- **重跑已完成论文先备份**：insert_extraction 是 upsert 不删旧行。
- **三实例同时 step=all 会重复搜索**同一关键词（冗余 API 调用）；搜索类操作建议单实例，处理类操作（process）可三实例并行。
