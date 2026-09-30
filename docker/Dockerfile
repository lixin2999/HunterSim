# HunterSim 仿真服务镜像（设计文档 §15.3 「仿真服务镜像」）
# 基于 Python 3.12 slim，安装 hunter-sim 包及所有依赖
# 注：文档示例基于 python:3.8-slim，但本项目代码使用 3.10+ 语法与 pydantic v2，
#     为保持可构建性升级基础镜像；镜像结构（requirements + uvicorn 启动）与文档一致。
# CARLA 引擎镜像另见 docker/Dockerfile.carla（§15.3）。
# 使用：docker build -f docker/Dockerfile -t hunter-sim:2.0.0 .

FROM python:3.12-slim AS base

# ── 构建阶段 ────────────────────────────────────────────────────────────────
FROM base AS builder

RUN pip install --no-cache-dir build setuptools wheel

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN python -m build --wheel --no-isolation \
    && pip wheel --no-deps --wheel-dir /wheels dist/*.whl

# ── 运行阶段 ────────────────────────────────────────────────────────────────
FROM base AS runtime

# 系统依赖（Kafka 客户端、网络诊断工具）
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 非 root 用户
RUN useradd --create-home --shell /bin/bash hunter
USER hunter
WORKDIR /home/hunter/app

# 安装 Python 依赖
COPY requirements.txt ./
RUN pip install --no-cache-dir --user -r requirements.txt

# 安装 hunter-sim 包
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --user /wheels/*.whl

# 复制配置文件
COPY configs/ ./configs/

# 暴露 API 端口
EXPOSE 8080

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -sf http://localhost:8080/api/v1/sim/health/live || exit 1

# 启动命令
ENTRYPOINT ["python", "-m", "uvicorn"]
CMD ["hunter_sim.api.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8080", \
     "--workers", "1", \
     "--loop", "uvloop", \
     "--http", "httptools", \
     "--access-log", "--log-level", "info"]
