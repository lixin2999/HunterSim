# HunterSim · HUNTER SE 车辆在环（VIL）仿真端

HunterSim 是面向 **HUNTER SE 自动驾驶底盘车** 的仿真端：按平台下发的场景驱动 CARLA 0.9.16 世界，用实车遥测驱动虚拟自车，把状态、事件、指标、传感器文件与实时画面回传给云端。

仿真端只做「执行」——用户、租户、场景库、批量任务、审计、OTA、远程操控等**治理能力全部归云端**，本仓库不重复实现。

- **车辆规格**：820×640×310 mm，整备质量 60 kg，轴距 0.46 m，后轮驱动 + 前轮阿克曼转向，最大速度 4.8 m/s。
- **当前版本**：`3.8.1`（破坏性变更清单见 [`release.md`](release.md) 的 V3.0.0 章节，补丁修复见 V3.0.1 / V3.0.2 / V3.0.3 / V3.0.4 / V3.1.1 / V3.1.2 / V3.1.3 / V3.2.1 / V3.2.2 / V3.2.3 / V3.2.4 / V3.2.5 / V3.2.6 / V3.3.3 / V3.3.4 / V3.3.5 / V3.4.1 / V3.4.2 / V3.4.3 / V3.4.4 章节，V3.4.0 新增出生点有效坐标替换与传感器按实际外形挂载，V3.4.1 新增环视主视区、启动期日志治理（409→204）与 Kafka TLS 证书自动发现，V3.4.2 修复场景车辆与参与者不运动、场景行为不生效、场景结束后 CARLA 卡死，并将环视流改为独立编码，V3.4.3 让主视区回归 CARLA 原生 1080p 画质（overview 不再套用 `ADAPTER_STREAM_MAX_WIDTH` 降采样）、深度图预览默认勾选、修复行人 AI 附着坐标错误并新增蓝图回退/出生点重试/脚本化兜底，自车状态取不到时改返回空值不再写零位姿，V3.1.0 新增一键启动与可视化客户端，V3.2.0 新增仿真节点接入，V3.2.1 修复 `.env` 模板留空项导致的首启失败，V3.2.2 落地 V2.0 节点级回传通道与下发 mTLS、`huntersim connect` 三步接入，并把接入开关默认全开、无凭据静默跳过、`.env.example` 收敛为极简模板，V3.2.3 修复 mTLS 启动 `ssl_cert_reqs` 参数类型错误，V3.2.4 修复 mTLS 就绪探测失败，V3.2.5 将 viz 改为探针面（:8080 HTTP）反向代理访问、免除浏览器导入证书，V3.2.6 修复 viz 代理向管理面建连时服务端证书 EKU 校验失败导致的 502，V3.3.0 VIL 消费端 Kafka 连接参数自动继承 CloudSettings、消除 .env 重复配置，V3.3.1 曾将平台下发「Carla 管理 API 连接超时」归因于 `.env` 漏配 `ADAPTER_HOST`（后由 V3.3.2 定论为现场 NAT 拓扑问题，配置本身当时已正确），V3.3.2 修复仿真节点 `endpoint_mismatch` 诊断在多网卡场景下误报、并将 NAT 导致平台学到错误回调地址写成可直接定位的排障线索，V3.4.4 修复自车状态取数误用 `get_rotation` 导致整场无数据、主视区改为 BGRA 直编 JPEG 快路径并给出慢编码告警、参与者映射兼容 `traffic_participants` 等别名键并输出可诊断汇总、场景收尾按阶段落日志以便定位 CARLA 原生崩溃，V3.5.0 对齐 HunterCore V1.26.0 场景语义：actor 三分类（vehicle/walker/cyclist/prop）、行为/触发条件按真实数值执行、平台事件每 tick 触发并输出 `TRIGGER_REACHED`、新增 `mapping.json` 与 `huntersim scene-check` 离线校验，V3.5.1 修复大地图冷启动 `load_map` 误用 10s 普通 RPC 超时导致实例 `failed`（地图加载单独放宽到 `CARLA_MAP_LOAD_TIMEOUT_SECONDS`，默认 180s），V3.5.2 修复 `relative_to_ego` 参与者出生点未按自车航向旋转导致「主车前看不到目标车」（环视相机回退）、`/viz` 新增「参与者」面板与 `/viz/api/instances/{id}/actors`（声明数/已生成/主视区 + 相对自车前向·左向），并修复 Kafka producer 超时组合非法致上报静默降级 no-op、viz 反代读取异常由 500 改为 502/504；V3.5.3 进一步修复 V3.5.2 的旋转基准取自「同步模式首 tick 前未同步的实时位姿」（仍表现为目标车落在数百米外、方位前后跳变）：参与者偏移改绕自车确定性出生点旋转、首个 tick 后补发 Traffic Manager 指令、不在行车道上的车辆出生点投影到车道中心、新增 `follow_condition_unmet` 跟车条件归因日志，并将 viz 反代长连接正常断开由逐条 WARNING 改为限频汇总；V3.5.4 让 `huntersim up` 具备崩溃自愈：子进程异常退出（典型为 CARLA 原生 fail-fast `0xC0000409` 带走 `carla-manager`）按退避自动重启且不再牵连其余服务，场景跑完后仿真端继续常驻等待平台下发下一个任务（`--no-restart` 保留旧行为），并在实例收尾补 `Execution plane releasing` / `Manager idle` 两条定位锚点；V3.5.5 针对主视区「幻灯片化」做了四件事：落到单帧轮询后按 10/20/40/80 s 递增退避**自动回探 MJPEG**（探测只读响应头、不打断当前画面）、推流名额改**租约制**（闲置 >30 s 自动回收、首帧异常路径立即归还）并在 `/health`·`/viz/api/status`·`/viz` 顶栏暴露 `stream.{clients,limit,fps}`、深度预览改**抽样先行 + 并发闸 + 上一帧缓存**（不再抢编码核）、环视编码**自适应降质** 85→78→72（不降分辨率、宽裕时升回、面板可读 `q` 档），并按仿真端机器配置（i7-14700KF / 32 GB / RTX 4070）把主视区默认帧率 10→**20 fps**（弱机 `ADAPTER_STREAM_FPS=10` + `overview_camera.fps=10.0` 回退）；V3.5.6 根治「大量 502」：查明 41 条 502 只是症状，真因是控制面两次**原生崩溃**（`0xC0000409` / `0xC00000FD`），由此消除两条「两个线程并发操作同一 `carla.Client` 连接」的路径——场景收尾期 `join` 上限 5→**20 s** 且 tick 线程仍持有世界时释放线程**不再碰 world/TM**（日志 `Execution plane release deferred`）、`get_ego_state()` 只读 tick 线程缓存不再退回跨线程直读 CARLA；代理把「上游暂时不在」由 502 改 **503 + `Retry-After`** 并加 2 s 熔断窗口，`/viz` 8 路轮询统一走退避闸门（不再刷屏）；`huntersim up` 把 NTSTATUS 退出码直接译成中文原因；V3.6.0 把原生崩溃从「事后自愈」改为**结构性消除**：新增 `engine/carla_mailbox.py`，每个 CARLA 端点（`host:rpc_port`）只有一个 owner 线程（tick 线程），所有 CARLA 客户端访问收敛为**单线程信箱**——owner 内联执行、非 owner 入队由 owner **每 tick `drain()` 串行代执行**、拿不到结果才降级到缓存（返回调用方哨兵而非 `None`），装配类长事务不允许排队（`CARLA engine … is busy` 直接失败重发，宁失败不崩溃）；同时把 `configure_logging` 真正接进 `carla-manager` / `probes` 入口（此前为死配置），日志统一带线程名、uvicorn 原生输出并入同一出口、`/viz` 轮询访问日志降噪。自证入口：`huntersim_carla_offowner_rpc_total` 长期为 **0**）；V3.6.1 补修场景结束后另一类原生崩溃并修正 `follow_condition_unmet` 偏移基准；V3.6.2 根治行人/动物横穿场景的一类原生栈溢出（`0xC00000FD`：对 off-navmesh 坐标调用 CARLA 原生 `controller.ai.walker.go_to_location` 触发 RecastNavigation 深层递归），场景行人默认改走**脚本化横穿**（`set_transform` 逐帧推进、不碰原生导航），原生 AI 导航改由 `SCENE_WALKER_AI_CONTROLLER=1` 显式开启；V3.6.3 修复脚本化横穿的同源位姿未同步缺陷——`scenario.tick()` 先于 `world.tick()`，首 tick 前 `get_transform()` 返回占位原点 `(0,0,0)` 会把行人从真实出生点瞬移到世界原点（表现为「动物横穿看不到 / 视区外几百米」），`_advance_scripted_walkers`/`_relative_gaps` 补「占位读数不采信」判据后行人保持在真实出生点横穿、`trigger_reached actual=0.000` 不再在 t=0 误触发；V3.7.0 对齐动物/行人穿行场景语义：面板/遥测/事件度量的自车加速度由「三维模长」改为**沿航向的带符号纵向分量**（制动转负，顺带修正 `emergency_brake` 此前因恒正永不触发），`sudden_cross`（鬼探头）由「起步即匀速横穿」改为**等待自车接近再快速窜入**，横穿终点改为**按地图车道探测落到对向路缘外**（避免行人 done 后杵在行车道中间把自车卡在低速）；V3.7.1 把面板/遥测/事件度量的纵向加速度由「`get_acceleration()` 瞬时投影」改为**前向投影速度的时间导数 + 一阶低通**（消除自车近静止时的 ±数 m/s² 抖动尖峰，使读数与「车几乎没动」不再矛盾、emergency_brake 不被抖动误触），并新增 SIL「卡低速」归因日志 `ego_speed_stuck`；V3.8.0 依据 `ego_speed_stuck` 实机定位到自车卡低速的真因是 **Traffic Manager 同步模式速度环油门/刹车极限环**（行人 33 m 外即已发生，非避让），对 **SIL 直道**自车改走**确定性纵向控制**（P + 巡航前馈，解除 TM；弯道/复杂路况仍交 TM），可用环境变量 `HUNTER_SIM_EGO_DRIVE`（`auto`|`tm`|`manual`）或场景参数 `ego_drive_mode` 强制；V3.8.1 封死场景结束 teardown 窗口的并发 CARLA 访问残留（新增 `CarlaMailbox.direct_call`，`owner released` 后多线程只读兜底改持端点锁串行，消除 `0xC0000409`），并把平台事件 `brake`/`emergency_stop` 动作在确定性控制下接入自车减速（`_apply_scene_event_actions`）。
- **运行环境**：Python 3.12 · CARLA 0.9.16 · Kafka 3.6 ·（可选）ROS2 Humble

---

## 1. 架构

两个进程，职责互斥：

```
huntersim carla-manager  :8090   唯一业务入口（内网、无鉴权）
├── 控制面   platform_adapter/    创建/下发/暂停/恢复/停止/进度/结果 + 画面回传
├── VIL Bridge  vil_mapper/       收帧 → 10Hz 栅格降采样 → 时间对齐插值 → 位姿/速度/舵角/灯光 → 感知叠加
├── Scene Manager  scene_runner/ + engine/ + traffic_sim/   地图/参与者/天气/脚本/事件
├── Sensor Emulator  sensor_sim/  三型传感器挂载 → FrameHub + mcap 录制 → ROS2（可选）
└── Monitor & Recorder            MJPEG/截图 + 产物 + Prometheus 指标

huntersim probes  :8080          只读探针面（/health + /metrics）

huntersim up                     一键启动（V3.1.0：预检 → 两进程 → 打开可视化客户端；V3.5.4：子进程崩溃自愈）
├── --demo                       就绪后自动建一个 SIL 冒烟场景（V3.1.1，无平台下发也能看到画面）
├── --no-restart                 关闭异常退出自动重启（V3.5.4，留给外部监控/编排器接管）
└── 可视化客户端  visualization/  /viz 单页应用：画面(MJPEG) / 传感器(ROS) / 参与者 / 事件 / 日志（只读）
```

**数据面契约不变**：

| 方向 | Topic | 内容 |
|------|-------|------|
| 消费 | `carla.{sim_id}.vil_state` | 平台转发的实车状态（位姿/速度/底盘/感知/期望控制） |
| 发布 | `carla.{sim_id}.sensor_data` | 6 类 type：`sim_status` / `sim_telemetry` / `sim_event` / `sim_metrics` / `sensor_file` / `artifact` |
| 死信 | `carla.{sim_id}.sensor_data.dlq` | 反序列化失败与坏消息 |

**画面出口**：`GET /api/v1/instances/{id}/stream.mjpg`（MJPEG，V3.5.5 起默认 20fps，受 `ADAPTER_STREAM_FPS` 与 `overview_camera.fps` 取小者约束）与 `/screenshot`（单帧 JPEG）；可视化客户端 `http://127.0.0.1:8090/viz` 直接复用这两个出口，不新增第二套业务面。画面降级（名额已满 / 首帧超时）后不再永久停在单帧轮询，会按计划回探并自动切回图像流。

**CARLA 访问线程模型（V3.6.0/V3.6.1）**：LibCarla 的 Python `Client / World / Actor` **不是线程安全的**，两个线程并发操作同一连接会直接把进程带进无 traceback 的原生退出（`0xC0000409` / `0xC00000FD` / `0xC0000005`）。因此本版本把访问收敛为**单线程信箱**（`engine/carla_mailbox.py`）：按端点 `host:rpc_port` 归一一个 **owner 线程**（即场景 tick 线程），owner 内联执行、其他线程入队由 owner **每 tick `drain()` 串行代执行**、等不到结果才降级读缓存，装配类长事务（`load_map` / `spawn` / 收尾）不允许排队而直接失败重试。V3.6.1 补丁修复：场景 cleanup 后在 `relinquish()` 前等待 LibCarla 内部传感器回调排空（避免 5s 后必现的 `0xC0000409`）并修正 `follow_condition_unmet` 偏移日志中的占位原点误读。V3.6.2 补丁修复另一类与并发无关的原生崩溃：行人/动物横穿场景把平台下发的任意坐标喂给 `controller.ai.walker.go_to_location`，off-mesh 时触发 RecastNavigation 深层递归→`0xC00000FD` 栈溢出；本版默认改走脚本化横穿（`set_transform` 逐帧推进、不碰原生导航），原生 AI 控制器改由 `SCENE_WALKER_AI_CONTROLLER=1` 显式开启。详见 [`release.md`](release.md) 的 V3.6.0 / V3.6.1 / V3.6.2 章节。

---

## 2. 模块职责

| 模块 | 职责 |
|------|------|
| `platform_adapter/` | 控制平面骨架：10 个实例端点 + `/health` + `/metrics`、单世界串行队列、SQLite 实例注册表（重启恢复/幂等）、看门狗、VIL 运行时装配、评估与产物回传、mcap 录制与上传；`sim_node` 负责 node 模式注册/心跳（IP 漂移自愈） |
| `vil_mapper/` | 遥测环形缓冲、`FrameAligner`（10Hz 栅格 + 最短弧插值/外推）、位姿外推、坐标标定、灯光与转向写入、感知叠加、Kafka 消费自愈与 DLQ |
| `scene_runner/` | 场景契约与校验、状态机、事件检测、内置场景（含传感器挂载）、ScenarioRunner 适配 |
| `sensor_sim/` | 传感型号剖面（`profiles.py`）、蓝图工厂、安装位、数据转换、`FrameHub` 帧汇聚、`McapRecorder` 录制、ROS2 桥接（可选） |
| `traffic_sim/` | 交通流、Actor 行为、行为树、行人、触发条件 |
| `engine/` | CARLA 原子能力：地图/天气/自车/蓝图/坐标/OpenDRIVE/控制器/服务端配置/本地进程自愈；`carla_mailbox` 提供每端点唯一 owner 线程的单线程信箱（V3.6.0） |
| `execution/` | 引擎端点库存与独占租约（`engine_pool`）、装配与释放（`plane`）、地图亲和选端、泄漏与僵尸租约回收 |
| `cloud/` | `SimDataProducer`（有界队列 + DLQ）、`sim_data_mapper`、`vil_state_parser`、`BagUploader` |
| `eval_service/` | 轨迹/舒适/安全/覆盖率四维评估与 S/A/B/C/D 评级 |
| `probes/` | 只读探针应用：`/health` 聚合管理面快照，`/metrics` 只导出本进程注册表 |
| `visualization/` | 只读可视化客户端：`/viz` 页面 + `/viz/api/*`（状态/事件/传感器/参与者/日志 SSE）、`logbus` 日志旁路、`launcher` 一键启动编排 |
| `common/` | pydantic 配置模型、异常体系、日志、Prometheus 指标 |
| `cli/` | `huntersim carla-manager` / `probes` / `up` / `doctor` / `version` |

---

## 3. 目录结构

```
HunterSim/
├── src/hunter_sim/
│   ├── platform_adapter/   # 控制面：app / service / executor / vil / store / mapper / models / config / sim_node
│   ├── vil_mapper/         # VIL：frame_aligner / vil_sync_service / vil_data_consumer / telemetry_buffer / pose_extrapolator / calibration_manager / state_visualizer / perception_overlay
│   ├── scene_runner/       # 场景：scene_config / validator / state_manager / event_detector / builtin_scenario
│   ├── sensor_sim/         # 传感器：profiles / sensor_factory / sensor_mount / data_converters / frame_hub / mcap_recorder / carla_ros_bridge
│   ├── traffic_sim/        # 交通流、行为树、行人、触发条件
│   ├── engine/             # CARLA 原子能力 + 环境检查 + 进程自愈 supervisor
│   ├── execution/          # engine_pool（租约/亲和）+ plane（装配/释放/快照）
│   ├── cloud/              # sim_data_producer / sim_data_mapper / vil_state_parser / bag_uploader / connector
│   ├── eval_service/       # 评估引擎与报告
│   ├── probes/             # 只读探针面
│   ├── visualization/      # 可视化客户端：logbus / sensor_view / routes / launcher + assets(index.html/app.js/style.css)
│   ├── cli/                # 运维 CLI
│   └── common/             # 配置 / 异常 / 日志 / 指标
├── configs/                # weather_profiles / sensor_configs / scene_templates / traffic_config
├── docker/                 # Dockerfile / Dockerfile.carla / compose（主 + Windows）/ prometheus / alerts / grafana
├── k8s/                    # 单 Pod 双容器（sim-service + probes）+ CARLA GPU Pod + PVC
├── tests/                  # 单元 + 集成（覆盖率门禁 ≥80%）
├── docs/                   # 单机部署指南
├── Makefile                # doctor / up-local / manager / probes / test / coverage / typecheck / lint / image / up
├── pyproject.toml          # 依赖与工具配置（pytest / mypy / coverage / console script）
└── .env.example            # 全部环境变量与默认值（逐项中文注释）
```

---

## 4. 快速开始

### 4.1 安装

```bash
python -m venv .venv
.venv\Scripts\activate                        # Windows（Linux: source .venv/bin/activate）
pip install -e ".[dev]"
pip install carla==0.9.16                     # 不在 pyproject 依赖内，需与 server 严格同版本
```

### 4.2 预检

```bash
huntersim doctor                              # 配置/端口/磁盘/GPU/CARLA/实例库/录制目录/mcap/Kafka
huntersim doctor --json --strict               # 脚本消费 / 上线门禁（warn 提为 fail）
huntersim scene-check --input tests/fixtures/scenes/semantics_sample_v1_26.json \
  --json-out build/mapping_report.json         # 离线校验平台 scene_config 语义映射（不连 CARLA）
huntersim scene-check --input <平台样本>.json --strict   # unresolved_param / condition_unknown 即非零退出
```

未装 CARLA SDK 时服务仍可启动，但下发场景返回 `503`；未装 `mcap` 时录制降级为 no-op。

### 4.3 启动

```bash
copy .env.example .env                        # Windows（Linux: cp）
# V3.2.2 起为极简模板：接入开关代码默认已开、无凭据静默跳过，本机拷完模板可不改直接 up
# （源码直跑先改 CARLA_HOST=127.0.0.1；接入平台时再补 node_id/服务令牌/平台地址/ADAPTER_HOST，详见 §5）

huntersim up                                  # 一键：预检 → 控制面 :8090 → 探针面 :8080 → 打开 /viz
huntersim up --no-browser                     # 不起浏览器，只打印可视化地址
huntersim up --skip-doctor --no-probes        # 跳过预检 / 不拉探针面
huntersim up --no-restart                     # 子进程异常退出即退出 up，不自动重启（V3.5.4）
```

> **仿真端常驻（V3.5.4）**：`up` 默认会在子进程异常退出时按 3/5/10/30s 退避自动重启（上限 4 次），
> 其余服务不受牵连，重启后继续等待平台下发场景；场景正常跑完不会导致 `up` 退出。

需要分进程排障时仍可手工起：

```bash
huntersim carla-manager                       # 控制面 :8090（唯一业务入口）
huntersim probes                              # 只读探针面 :8080（另开终端）
```

交互式文档：<http://127.0.0.1:8090/docs>；可视化客户端：<http://127.0.0.1:8090/viz>。

> **8090 无鉴权**，只允许内网/本机访问，禁止加 Ingress/NodePort 或映射公网。CARLA 的 2000–2002 同理。

### 4.4 最小闭环

```bash
BASE=http://127.0.0.1:8090

# 1) 创建实例（立即返回，世界在后台加载）
curl -X POST $BASE/api/v1/instances -H "Content-Type: application/json" -d '{
  "scene_id": "smoke-001",
  "sim_id": "sim-local-001",
  "mode": "sil",
  "scene_config": {
    "map": {"map_id": "Town03", "spawn_point": {"x": 0, "y": 0, "z": 0.3, "yaw": 0}},
    "ego_vehicle": {"model": "hunter_se"},
    "duration": 30
  }
}'

> **Windows 下带中文 `scene_name` 请看这里**：`cmd` / PowerShell 默认按控制台代码页（GBK）把 `-d` 的字符串交给 `curl`，非 ASCII 字符会在提交端就变成 `?`，落库后页面显示 `?????`（服务端不丢字，无法还原）。
> 三种安全写法：① 直接用 `huntersim up --demo`（内部已显式 UTF-8）；② 把请求体存为 **UTF-8 文件**后 `curl.exe --data-binary @body.json`；③ PowerShell 先 `[Console]::OutputEncoding = [Text.Encoding]::UTF8`，再 `Invoke-RestMethod -ContentType 'application/json; charset=utf-8'`。

# 2) 轮询（pending → running → succeeded/failed/canceled）
curl $BASE/api/v1/instances/<ID>

# 3) 结果（非终态也返回 200 + 当前状态）
curl $BASE/api/v1/instances/<ID>/result

# 4) 实时画面
curl -o frame.jpg $BASE/api/v1/instances/<ID>/screenshot
# 浏览器：<img src="http://127.0.0.1:8090/api/v1/instances/<ID>/stream.mjpg">
# 可视化看板：http://127.0.0.1:8090/viz （画面 + 传感器 + 参与者 + 事件 + 日志）
```

### 4.5 容器化

```bash
copy .env.example .env
docker compose -f docker/docker-compose.yml up -d --build
# 服务：kafka · sim-service(:8090) · probes(:8080) · prometheus(:9090) · alertmanager(:9093) · grafana(:3000)
# Linux + NVIDIA Container Toolkit 可加 --profile carla 起容器内 CARLA 引擎
```

Windows 单机（CARLA 也在 Docker 里）：`docker compose -f docker/docker-compose.windows.yml up -d`（`carla-server` + `sim-service` + `kafka`）。

Kubernetes：`kubectl apply -f k8s/`（单 Pod 双容器，`replicas: 1`，引擎自愈由 carla-engine Pod liveness 承担）。

### 4.6 作为「仿真节点」接入平台（node 模式）

平台 `SIM_DISPATCH_MODE=node` 时，场景库下发不再依赖固定 IP：仿真机注册后，平台从
入站请求头自学习本机地址，再按该地址直推本机 CARLA 管理 API（`:8090`）。IP 漂移由下一次
心跳自愈，平台侧无需改配置。完整契约与排障见 HunterCore
[`docs/HunterSim接入清单.md`](../HunterCore/docs/HunterSim接入清单.md)。

```bash
# 平台管理员：签发节点服务令牌（同一 node_id 必须固定同一 client_id；建议同值）
curl -sS -X POST http://<平台地址>/api/v1/user/service-token \
  -H 'Content-Type: application/json' -H "Authorization: Bearer <管理员 JWT>" \
  -d '{"client_id":"sim-node-01"}'
# → data.access_token 填入 HUNTER_CORE_SIM_NODE_SERVICE_TOKEN
```

```dotenv
# 仿真机 .env（启用后控制面启动即注册，之后每 20s 心跳）
HUNTER_CORE_SIM_NODE_ENABLED=true
HUNTER_CORE_SIM_NODE_ID=sim-node-01
HUNTER_CORE_SIM_NODE_SERVICE_TOKEN=<服务令牌>
HUNTER_CORE_SIM_NODE_BASE_URL=http://<平台 web-portal>   # 空则复用 HUNTER_CORE_GATEWAY_URL
HUNTER_CORE_SIM_NODE_CAPACITY=1
HUNTER_CORE_SIM_NODE_MAPS=Town01,Town03                  # 留空 = 不做地图过滤
ADAPTER_HOST=0.0.0.0                                     # 必须允许平台回访 :8090
```

要点：

- **默认已开、无凭据静默跳过**（V3.2.2）：`HUNTER_CORE_SIM_NODE_ENABLED` 代码默认已翻转为 `true`，`.env.example` 不再预置开关。未配 `HUNTER_CORE_SIM_NODE_ID` / `_SERVICE_TOKEN` 与平台地址时不发起注册/心跳，`huntersim doctor` 记 **warn**（不拦住 `huntersim up`）；接入平台时补齐这几项即可，彻底关闭可显式置 `false`。
- **地址只能由平台自学习**：注册体只声明 `manager_port`/`protocol`/`maps`/`capacity`；
  在请求体里塞 IP 会被平台 422 拒绝（设计如此）。
- 节点走 **web-portal 80/443**，不要直连网关 `8080`：直连时平台学到的是网关容器地址，
  会登记出「在线但永远打不通」的节点（`huntersim doctor` 会针对 8080 给出 warn）。
- 平台主机需能入向访问本机 `:8090`（Windows 防火墙放行平台地址），WiFi 不得启用 AP 隔离。
- 平台日志应出现 `sim_node_dispatch_target` 且 `base_url` 指向本机；本机 `/health` 的
  `sim_node` 段可看到 `endpoint_ip`/`endpoint_source`/租约与最近一次错误。
- 失败语义：`3001`（未注册/租约过期）由客户端自动回到注册分支；`401/403` 需检查服务令牌
  是否过期/被撤销、角色是否在平台 `SIM_NODE_ROLES` 内。

---

## 5. 配置

优先级：**环境变量 > `.env` > 默认值**。全部由 `common/models.py` 的 pydantic `BaseSettings` 聚合；完整清单见 [`.env.example`](.env.example)。

| 前缀 | 组 | 关键项（默认值） |
|------|------|------------------|
| `HUNTER_SIM_` | 全局 | `env`(dev) · `log_level`(INFO) · `log_json`(false) · `log_file`(空) · `log_max_bytes` · `log_backup_count` |
| `CARLA_` | CARLA 连接 | `host` · `rpc_port`(2000) · `stream_port`(2001) · `fixed_delta_seconds`(0.02 = 50Hz) · `timeout_seconds`(10) · `supervisor_*`（本地进程自愈，仅托管回环端点） |
| `KAFKA_` | 数据总线 | `bootstrap_servers` · `telemetry_topic`(`carla.{sim_id}.vil_state`) · `sim_id` · `legacy_telemetry_topic` · `enable_self_heal`(true) · `rebuild_after_errors`(10) · `dlq_topic` · `security_protocol`(PLAINTEXT) · `sasl_mechanism`(SCRAM-SHA-512) · `username`/`password` · `ca_cert_path`/`client_cert_path`/`client_key_path`（V3.1.2 消费侧 SASL/TLS） |
| `VIL_` | 虚实映射 | `target_hz`(10.0) · `extrapolation_ms`(150) · `max_data_latency_ms`(500) · `warning_latency_ms`(50) · `buffer_max_frames`(10) · `ordering_guard_enabled`(true) · `telemetry_frequency_hz`(10.0) · `auto_buffer_depth`(true) · `overlay_enabled`(true) · `lights_enabled`(true) · `clock_offset_auto`(false) |
| `RESOURCE_` | 生命周期 | `instance_max_lifetime_seconds`(7200) · `health_check_interval_seconds`(10) |
| `EXECUTION_` | 执行面 | `engine_endpoints`(空=单引擎) · `max_engines`(8) · `restore_scene_on_assemble`(true) · `lease_heartbeat_enabled`(true) · `lease_ttl_seconds`(120) |
| `ADAPTER_` | 控制面 | `host`(127.0.0.1) · `port`(8090) · `db_path` · `artifacts_dir` · `artifact_bucket` · `artifact_retention_hours`(72) · `recordings_dir` · `recording_retention_hours`(24) · `record_enabled`(true) · `record_min_free_gb`(0.5) · `upload_on_failure`(false) · `stream_fps`(20.0，V3.5.5 由 10.0 上调；主视区实际帧率取它与 `overview_camera.fps` 的较小值) · `stream_max_width`(640，V3.4.3 起仅约束 `rgb` 数据流，`overview` 主视区固定原生分辨率不降采样) · `stream_max_clients`(8；V3.5.5 起为**租约**，闲置 >30 s 的连接在下一次申请时自动回收) · `screenshot_quality`(75；`overview` 编码质量不低于它，默认档 85，V3.5.5 自适应 85→78→72) · `watchdog_factor`(3.0) · `watchdog_grace_seconds`(120) · `progress_interval_seconds`(0.5) · `default_duration_seconds`(60) · `max_scene_seconds`(7200) · `busy_timeout_seconds`(5.0) |
| `PROBE_` | 探针面 | `host`(127.0.0.1) · `port`(8080) · `adapter_url`(`http://127.0.0.1:8090`) |
| `VIZ_` | 可视化客户端 | `enabled`(true) · `auto_open_browser`(true) · `log_buffer_size`(2000) · `log_history_limit`(500) · `event_limit`(200) · `lidar_max_points`(4000) · `depth_max_width`(480) |
| `HUNTER_CORE_` | 云端接入 | `enabled`(**true**，V3.2.2 翻转) · `kafka_bootstrap` · SASL 凭据 · `sensor_data_topic_template` · `sensor_data_dlq_template` · `gateway_url` · `jwt_token` · `rosbag_bucket` · `upload_queue_max_size`(32) · `upload_max_retries`(2) · `upload_retry_backoff_s`(1.0) |
| `HUNTER_CORE_SIM_NODE_` | 仿真节点 | `enabled`(**true**，V3.2.2 翻转；无凭据静默跳过) · `node_id` · `service_token` · `base_url`(空=复用 gateway_url) · `result_enabled`(**true**，V2.0 节点级回传) · `heartbeat_interval_seconds`(20，≤30) · `request_timeout_seconds`(5) · `capacity`(1) · `maps`(空=不过滤) · `carla_version` · `protocol`(http) · `manager_port`(空=ADAPTER_PORT) · `labels` · `runtime_hint` |
| `ADAPTER_SSL_` | 下发 mTLS | `certfile`/`keyfile`（成对则 carla-manager 启用 TLS） · `ca_certfile`（信任平台 CA 校验回访证书） · `client_auth_required`(false，置 true 强制双向 TLS) · `allowed_client_cn`（仅展示提示）（V3.2.2） |

> V3.0.0 起已移除 `API_*` / `STATE_*` / `PLATFORM_*` / `GUI_*` 前缀与 `EXECUTION_ENGINE_ENDPOINTS` 的双引擎示例（受支持形态 = 单世界串行，端点库存留空即单引擎）。

> 上表括号里是**代码字段默认值**（V3.2.2 起两个接入开关默认已翻转为 `true`）；`.env.example` 已收敛为极简模板、不再逐条预置。未配服务令牌/`node_id`/Kafka 凭据时，`huntersim doctor` 对 `sim_node` 只记 **warn**（不再 `fail`），遥测/上传/注册心跳/节点级回传静默降级为 no-op，本机**拷完模板一行不改就能直接 `huntersim up`**。接入平台时才需补：`CARLA_HOST=127.0.0.1`（源码直跑）、`HUNTER_CORE_SIM_NODE_ID`/`_SERVICE_TOKEN`、平台地址（`HUNTER_CORE_SIM_NODE_BASE_URL` 或 `HUNTER_CORE_GATEWAY_URL`）、node 模式的 `ADAPTER_HOST=0.0.0.0`。纯本机联调想让 `doctor` 完全干净：显式把两个开关置 `false`（转 `skip`）。

---

## 6. 传感器剖面

实车型号参数固化在 [`src/hunter_sim/sensor_sim/profiles.py`](src/hunter_sim/sensor_sim/profiles.py)，与 [`configs/sensor_configs.json`](configs/sensor_configs.json) 双向对应（单测 `test_sensor_profiles.py` 逐字段断言一致）。

| 剖面 | 覆盖传感器 | 关键参数 |
|------|------------|----------|
| `helios_16p` | RS-Helios-16P LiDAR | 16 线 / 150 m / 10 Hz / 垂直 FOV ±15° / 噪声 σ=0.02 m / 131072 pts·s⁻¹ |
| `d435` | Intel D435 RGB + Depth | RGB 1280×720 / HFOV 69° / 30 fps；Depth 1280×720 / HFOV 87° / 0.2–10 m |
| `ch10x` | CH10X IMU | 100 Hz / accel σ=0.01 m/s² / gyro σ=0.001 rad/s |
| `hunter_se_full`（默认） | 上述三件套全挂 | 另含 collision / lane_invasion / obstacle 事件传感器 |

场景按剖面挂载：

```json
{
  "sensors": [
    {"profile": "hunter_se_full", "enabled": true, "ros_enabled": false, "record": true}
  ]
}
```

`ros_enabled=true` 时额外投递 ROS2 话题（需 `rclpy`，缺失自动降级为不出 ROS；桥接器装配后会调用 `initialize()` 建立发布者）；`record=true` 时写入 `ADAPTER_RECORDINGS_DIR` 下的 `.mcap`。录制还受 `ADAPTER_RECORD_ENABLED`（总开关）与 `ADAPTER_RECORD_MIN_FREE_GB`（磁盘余量闸门）约束，运行期每 5s 复查、跌破阈值主动停录；`rgb`/`depth`/`lidar`/`imu` 四路均进入 `FrameHub`（`depth` 为 `32FC1` 米制字节）。

传感器回调只做内存拷贝并入有界队列（上限 32 帧），编码/落盘/ROS 发布由 `hunter-sensor-encoder` 线程串行执行，避免把 CARLA `world.tick()` 线程拖出 50 Hz 预算；队满时丢帧并计入 `huntersim_sensor_frames_dropped_total{stream}`。环视主视区（`overview`）走独立的 `hunter-overview-encoder` 线程与小容量队列（仅保留最新帧），不与数据传感器争用编码队列，数据侧高负载时画面仍保持最新。V3.4.3 起 `overview` 固定按相机原生分辨率（默认 1920×1080）编码、不套用 `ADAPTER_STREAM_MAX_WIDTH`，启动日志会打印 `Overview camera attached: ... size=1920x1080 (native encode, jpeg 85→78→72 adaptive, no downscale)`；`ADAPTER_STREAM_MAX_WIDTH` 只影响 `rgb` 数据流。V3.4.4 起环视编码走 `encode_bgra_jpeg()` 零拷贝快路径（直接从 CARLA 的 BGRA 缓冲区编码，省掉 numpy 侧两次整帧拷贝），单帧编码超 120 ms 时限频告警 `Overview encode slow: ... ms/frame`。V3.5.5 起该告警不再只是信息：编码线程逐帧计时，**连续 8 帧超预算**则沿 85→78→72 逐档降质（只降质量不降分辨率，日志 `Overview encode adapted: jpeg quality 85→78`），**连续 150 帧宽裕**则升回一档；触底仍超预算则限频提示改 `configs/sensor_configs.json` 的 `overview_camera` 分辨率/帧率（需重建传感器，不自动做），当前档随帧 meta 下发并在面板显示为 `q85`/`q78`。同一版本的深度预览不再抢编码核：先按 `VIZ_DEPTH_MAX_WIDTH`（默认 480）跨步抽样再参与逐像素运算（1280×720 → 240×427，运算量约 1/9），并发闸非阻塞使编码不排队（已有在算则复用上一帧），同一帧重复请求零重算。mcap 写入连续失败 10 次即熔断（`RecordStats.write_errors` 上报），已写入部分仍会 `finish()` 落盘。

---

## 7. 监控

控制面 `:8090/metrics` 与探针面 `:8080/metrics` 各自导出标准 Prometheus 文本格式；探针 `/health` 通过 `PROBE_ADAPTER_URL` 向控制面聚合 `carla_connected` / `active_instance` / `vil_state` / `sensor` / `cloud`，上游不可达时返回 **200 + `status=degraded`**（避免容器 liveness 反复杀进程）。

V3.0.0 新增指标：

| 指标 | 类型 | 说明 |
|------|------|------|
| `huntersim_vil_frames_downsampled_total{mode}` | Counter | 栅格推进计数（`interpolated`/`extrapolated`/`nearest`），总和应稳定 10/s |
| `huntersim_vil_align_skew_ms` | Histogram | 栅格对齐误差，P95 < 60 ms 视为健康 |
| `huntersim_sensor_frames_total{stream}` | Counter | 传感器帧计数（`rgb`/`depth`/`lidar`/`imu`） |
| `huntersim_sensor_frames_dropped_total{stream}` | Counter | 编码队列满导致的丢帧计数（持续增长 → 磁盘/CPU 跟不上采集） |
| `huntersim_sensor_record_bytes_total` | Counter | mcap 累计写入字节（磁盘容量告警依据） |
| `huntersim_recording_events_total{event}` | Counter | 录制生命周期事件（`started`/`skipped_disabled`/`skipped_low_disk`/`stopped_low_disk`） |
| `huntersim_cloud_upload_total{result}` | Counter | bag/mcap 上传终态（自动重试耗尽才计 `failed`） |
| `huntersim_stream_clients` | Gauge | MJPEG 在线客户端数 |
| `huntersim_probe_upstream_reachable` | Gauge | 探针到控制面的可达性 |

**502 / 503 口径（V3.5.6）**：`/viz` 反向代理在控制面重启或连接被中断时返回 **503 + `Retry-After: 2`**（`detail=control plane unavailable (restarting)`，即「上游暂时不在」，调用方据此退避重试）；`502`（`detail=upstream error`）现在只可能来自代理最后兜底分支捕获的真未知异常（旧版同场景会直接冒 500），读超时仍为 `504`（`upstream timeout`）。代理另带 2 s 熔断窗口，窗口内快速失败不再逐个撞已断开的端口（日志只留 1 条 WARNING + 1 条关窗汇总 INFO）。按 `502` 关键字匹配的平台侧告警规则需改为 `50[234]` 或直接改用 `/health`。

V3.6.0 新增指标（CARLA 单线程信箱）：

| 指标 | 类型 | 说明 |
|------|------|------|
| `huntersim_carla_mailbox_tasks_total{engine,op,result}` | Counter | 信箱任务终态：`ok`（owner 内联或排队后执行成功）/ `queued`（已入队等待）/ `timeout`（等不到结果降级）/ `dropped`（owner 已退出被丢弃）。`timeout` 持续增长说明 tick 线程长时间不消费信箱 |
| `huntersim_carla_offowner_rpc_total{engine,op}` | Counter | **非 owner 线程直读 CARLA 的违规计数，长期为 `0` 是本版正确性断言**。非 0 即说明还有一条路径没收敛进信箱（正是原生崩溃的前置条件），排查入口 `CARLA RPC off owner thread` 日志行 |

排查开关：`CARLA_MAILBOX_STRICT=1`（违规直接抛 `CarlaThreadViolation` 而不降级，适用于测试/回归环境）、`HUNTER_SIM_LOG_ACCESS_POLLING=1`（恢复 `/viz` 轮询访问日志逐条输出）。

---

## 8. 开发与测试

```bash
make test          # pytest -q（含覆盖率门禁 fail_under=80）
make coverage      # pytest -q --cov=src/hunter_sim --cov-report=term-missing
make typecheck     # mypy strict
make lint          # flake8 src tests
make doctor        # 部署预检
```

当前门禁基线：

- **测试**：`1556 passed`（单元 + 集成，V3.6.0 新增 68 项信箱/日志回归守卫），全仓行覆盖 **82.7%**（门禁 ≥80%）。
- **类型**：`mypy src/hunter_sim`（strict）**91 个源文件 0 错误**。
- **Lint**：`flake8 src tests` **0 告警**。
- **死代码**：`tests/test_no_orphan_exports.py` 断言 `__init__.py` 导出的符号在包内有生产引用（本期接入的 `StateVisualizer`/`FrameHub`/`McapRecorder`/`FrameAligner`/`CarlaRosBridge` 均有回归守卫）。
- CARLA / ROS2 / mcap 未安装的机器上，测试通过 `sys.modules` 桩注入（`tests/mocks/carla_mocks.py`）无引擎运行。

---

## 9. 容量边界

| 项 | 口径 |
|------|------|
| 控制面副本数 | **1**（单世界串行；`replicas: 1`，重启后非终态实例置 `failed`，由平台重新创建） |
| 状态库 | SQLite（WAL + 单写者），需 RWO 卷；丢失会失去幂等与历史查询 |
| 水平扩展 | **不受支持**（多副本需外置共享状态与分布式租约） |
| 录制磁盘 | 三传感器约 **1.5–3 GB/分钟**（LiDAR 为主），默认保留 24 小时；`doctor` 校验剩余 ≥ 5 GiB |
| MJPEG 带宽 | 默认 **20fps**（V3.5.5，取 `ADAPTER_STREAM_FPS` 与 `overview_camera.fps` 较小值）；`rgb` 数据流 `screenshot_quality=75` + `stream_max_width=640`；`overview` 主视区为原生 1920×1080、JPEG 默认 85（自适应可降至 78/72）、不降采样，单帧约 200–300 KB → 单客户端约 4–6 MB/s，按客户端数线性增长（`stream_max_clients=8` 封顶）；卡顿/带宽不足时的处理顺序：先看面板「推流客户端 n / 8」与 `q` 档 → 降 `overview_camera` 分辨率/帧率（而不是依赖 `ADAPTER_STREAM_FPS`）→ 关深度预览勾选项 |
| 并行 | 需要并行时部署多个控制面进程/端口并在平台侧分片（不共享注册表） |

---

## 10. 文档索引

| 文件 | 内容 |
|------|------|
| `README.md` | 项目总览与快速开始（本文件） |
| [`user_manual.md`](user_manual.md) | 控制面接口使用手册：实例/场景/VIL/画面/录制、状态与结果、FAQ |
| [`deployment_and_operations.md`](deployment_and_operations.md) | 部署、平台接入、监控指标、故障排查、受支持形态 |
| [`docs/单机部署指南.md`](docs/%E5%8D%95%E6%9C%BA%E9%83%A8%E7%BD%B2%E6%8C%87%E5%8D%97.md) | Windows 单机安装、启动、联调、冒烟验证、故障速查 |
| [`release.md`](release.md) | 版本发布说明（V3.0.0 含破坏性变更清单） |
| [`HunterSim.ai-rules.md`](HunterSim.ai-rules.md) | 工程约束与开发规范 |

---

## 11. 许可

本项目基于 [LICENSE](LICENSE) 发布。
