FROM python:3.11-slim

# 系统依赖（psycopg2 需要 libpq）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 工作目录
WORKDIR /app

# 先复制依赖文件，利用 Docker 缓存层
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目代码
COPY src/ src/
COPY run.py .
COPY config.yaml.example .

# 创建数据目录
RUN mkdir -p docs/meta docs/PDF cache output/parsed output/runs

# 环境变量默认值
ENV PYTHONUNBUFFERED=1
ENV INSTANCE_ID=default

# 暴露 API 端口
EXPOSE 8000

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# 启动命令
CMD ["python", "run.py", "--serve", "--port", "8000"]
