# HunterSim 一键入口（V3.1.0）
#
# 面向“装好依赖 → 预检 → 起服务 → 验证”的日常闭环；Windows(cmd/PowerShell)
# 与 Linux/macOS 通用：所有配方只调用 python / docker，不依赖 sh 专有语法。
#
#   make up-local   # 一键启动：预检 + 控制面(8090) + 探针面(8080) + 打开可视化客户端
#   make doctor     # 部署前自检（配置/端口/磁盘/GPU/CARLA/实例库/录制目录/mcap/Kafka/平台）
#   make manager    # 前台启动唯一业务入口（控制面 8090，可视化 /viz）
#   make probes     # 前台启动只读探针面（/health + /metrics，8080）
#   make test       # 单元测试 + 覆盖率门禁
#   make typecheck  # mypy strict
#   make up         # docker compose 起全套（Kafka + sim-service + probes）
#
# 变量：PYTHON 指向目标解释器；Windows 虚拟环境默认 .venv\Scripts\python.exe，
#       Linux/macOS 为 .venv/bin/python。COMPOSE_FILE 指向 compose 编排文件。

PYTHON ?= .venv/Scripts/python.exe
PIP ?= $(PYTHON) -m pip
COMPOSE_FILE ?= docker/docker-compose.yml
IMAGE_TAG ?= hunter-sim:3.1.0
UV_SYNC ?= uv sync

.PHONY: help install sync dev-install doctor manager probes up-local up-local-headless viz test coverage typecheck lint \
        image up up-carla down logs clean

help:
	@echo HunterSim targets: install sync dev-install doctor manager probes up-local viz test coverage typecheck image up up-carla down logs

# ── 依赖 ────────────────────────────────────────────────────────────────────
install:
	$(PIP) install -e .

dev-install:
	$(PIP) install -e ".[dev]"

sync:
	$(UV_SYNC) --extra dev

# ── 预检与运行 ──────────────────────────────────────────────────────────────
doctor:
	$(PYTHON) -m hunter_sim.cli.main doctor

manager:
	$(PYTHON) -m hunter_sim.cli.main carla-manager

probes:
	$(PYTHON) -m hunter_sim.cli.main probes

up-local:
	$(PYTHON) -m hunter_sim.cli.main up

up-local-headless:
	$(PYTHON) -m hunter_sim.cli.main up --no-browser

viz:
	@echo 可视化客户端：http://127.0.0.1:8090/viz（需先 make up-local / make manager）

# ── 质量门禁（与 CI 同口径：mypy strict + 覆盖率 >=80%）──────────────────────
test:
	$(PYTHON) -m pytest -q

coverage:
	$(PYTHON) -m pytest -q --cov=src/hunter_sim --cov-report=term-missing

typecheck:
	$(PYTHON) -m mypy src/hunter_sim

lint:
	$(PYTHON) -m flake8 src tests

# ── 容器 ────────────────────────────────────────────────────────────────────
image:
	docker build -f docker/Dockerfile -t $(IMAGE_TAG) .

up:
	docker compose -f $(COMPOSE_FILE) up -d

up-carla:
	docker compose -f $(COMPOSE_FILE) --profile carla up -d

down:
	docker compose -f $(COMPOSE_FILE) down

logs:
	docker compose -f $(COMPOSE_FILE) logs -f sim-service

clean:
	$(PYTHON) -c "import shutil, pathlib; [shutil.rmtree(str(p), ignore_errors=True) for p in [pathlib.Path('.pytest_cache'), pathlib.Path('.mypy_cache'), pathlib.Path('.ruff_cache'), pathlib.Path('htmlcov')]]"
