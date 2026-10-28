# HunterSim 仿真服务镜像（V3.0.0，设计文档 §15.3 「仿真服务镜像」）
# 基于 Python 3.12 slim，安装 hunter-sim 包及所有依赖。
# 同一镜像两种启动形态（由 docker-compose / k8s 的 command 区分）：
#   huntersim carla-manager  :8090   业务入口（单进程，控制面 + VIL + 场景 + 传感器）
#   huntersim probes         :8080   只读探针面（/health + /metrics）
# CARLA 引擎镜像另见 docker/Dockerfile.carla（§15.3）。
# 使用：docker build -f docker/Dockerfile -t hunter-sim:3.0.0 .
#
# CARLA Python SDK（V2.3.0）：服务端要「真跑起一个场景」必须能 import carla。
# PyPI 自 0.9.14 起提供 manylinux wheel（cp310/cp311/cp312），默认随镜像安装，
# 版本必须与 CARLA server 严格一致；无 wheel 的平台可用 --build-arg INSTALL_CARLA=false 关闭
# （此时服务仍可启动，下发场景返回 503 并给出诊断原因）。

# 国内/慢速网络环境可通过 --build-arg 切换 PyPI 镜像源（默认官方源，不改变既有构建行为）
# 示例：--build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_TRUSTED_HOST=pypi.org

FROM python:3.12-slim AS base

# ── 构建阶段 ────────────────────────────────────────────────────────────────
FROM base AS builder

RUN pip install --no-cache-dir --retries 5 --timeout 120 build setuptools wheel

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
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_TRUSTED_HOST=pypi.org
COPY requirements.txt ./
RUN pip install --no-cache-dir --user -r requirements.txt \
        --retries 5 --timeout 120 \
        --index-url "${PIP_INDEX_URL}" --trusted-host "${PIP_TRUSTED_HOST}"

# 安装 CARLA Python SDK（与 CARLA server 版本一致，详见文件头说明）
ARG CARLA_VERSION=0.9.16
ARG INSTALL_CARLA=true
RUN if [ "$INSTALL_CARLA" = "true" ]; then \
        pip install --no-cache-dir --user "carla==${CARLA_VERSION}" \
            --retries 5 --timeout 120 \
            --index-url "${PIP_INDEX_URL}" --trusted-host "${PIP_TRUSTED_HOST}"; \
    else \
        echo "Skip CARLA SDK install (INSTALL_CARLA=${INSTALL_CARLA})"; \
    fi

# 安装 hunter-sim 包
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --user /wheels/*.whl

# V2.8.0：依赖全部装到 --user 站点，console scripts（huntersim）落在 ~/.local/bin；
# 不加进 PATH 则容器内 `docker compose exec sim-service huntersim doctor` 会 command not found。
ENV PATH="/home/hunter/.local/bin:${PATH}"

# 复制配置文件
COPY configs/ ./configs/

# 暴露控制面（8090）与只读探针面（8080）端口；未运行 `probes` 时 8080 空置
EXPOSE 8090 8080

# 健康检查（控制面 /health 为聚合健康；探针面见 docker-compose.yml 的 probes 服务）
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -sf http://localhost:8090/health || exit 1

# 启动命令：单一业务入口（V3.0.0 起取消 --workers，单世界串行）
ENTRYPOINT ["huntersim"]
CMD ["carla-manager"]
