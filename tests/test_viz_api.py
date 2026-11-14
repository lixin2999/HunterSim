"""可视化客户端端点测试（V3.1.0）。

覆盖挂在控制面 ``/viz`` 下的只读视图：

- 页面与静态资产白名单（路径穿越/未登记名字一律 404）；
- 画面降级链（V3.5.5：落到单帧轮询后自动回探 MJPEG，不得单向永久降级）；
- ``/viz/api/status`` 汇总运行态，且服务替身缺少 ``runtime_status`` 时能降级；
- 事件/传感器/单流帧（RGB 原样、深度 PNG、点云与 IMU JSON）与错误码；
- 日志快照过滤与 SSE 增量（``text/event-stream``）；
- ``/viz`` 端点不进 OpenAPI（平台契约零改动，V3.0.0 §16.1 单一控制面）。
"""

from __future__ import annotations

import asyncio
import json
import re
import struct
import sys
import time
from types import SimpleNamespace
from typing import Any, Iterator, Optional

import pytest
from fastapi.testclient import TestClient

from hunter_sim.platform_adapter.app import create_app
from hunter_sim.sensor_sim.frame_hub import Frame
from hunter_sim.visualization.logbus import LogBus


def _record(instance_id: str = "inst-1", status: str = "running") -> Any:
    """实例记录替身（``_require_instance`` 只判空，降级视图需 ``to_view``）。"""
    record = SimpleNamespace(instance_id=instance_id, status=status)
    record.to_view = lambda: {"instance_id": instance_id, "status": status}
    return record


def _frame(stream: str, payload: bytes, meta: Optional[dict[str, Any]] = None) -> Frame:
    return Frame(stream=stream, ts=time.time(), seq=7, payload=payload, meta=meta or {})


class _ReadOnlyService:
    """只实现查询方法的替身：验证可视化端点对缺失只读能力的降级。"""

    def __init__(self) -> None:
        self.record = _record()

    def get(self, instance_id: str) -> Any:
        return self.record if instance_id == self.record.instance_id else None

    def list(self, scene_id: Optional[str] = None) -> list[Any]:
        return [self.record]


class _FakeService(_ReadOnlyService):
    """带事件/传感器/ROS/运行态的真实数据替身。"""

    def __init__(self) -> None:
        super().__init__()
        self.frames: dict[str, Frame] = {}
        self.event_items: list[dict[str, Any]] = [
            {"event_type": "collision", "event_level": "critical", "time_stamp": 1.5, "description": "碰撞"},
            {"event_type": "collision", "event_level": "critical", "time_stamp": 2.5, "description": "再次碰撞"},
            {"event_type": "lane_invasion", "event_level": "info", "time_stamp": 3.5, "description": "压线"},
        ]
        self.sensor_body: dict[str, Any] = {
            "attached": True,
            "counts": {"rgb": 3},
            "streams": {"rgb": {"count": 3, "fps": 10.0, "frame": None}},
        }
        self.ros_body: dict[str, Any] = {
            "enabled": True,
            "topics": {"/carla/lidar_points": 5},
        }
        self.actor_items: list[dict[str, Any]] = [
            {
                "id": "lead-1",
                "actor_type": "vehicle",
                "behavior": "constant_speed",
                "source_behavior_type": "forward",
                "spawned": True,
                "ahead_m": 18.0,
                "left_m": 0.0,
                "visible": True,
            },
            {
                "id": "lead-2",
                "actor_type": "vehicle",
                "behavior": "lane_follow",
                "spawned": True,
                "ahead_m": -6.0,
                "left_m": 12.0,
                "visible": False,
            },
        ]
        self.runtime_calls = 0

    def events(self, instance_id: str) -> list[dict[str, Any]]:
        return list(self.event_items)

    def actors(self, instance_id: str) -> list[dict[str, Any]]:
        return [dict(item) for item in self.actor_items]

    def sensors(self, instance_id: str) -> dict[str, Any]:
        return dict(self.sensor_body)

    def ros(self, instance_id: str) -> dict[str, Any]:
        return dict(self.ros_body)

    def sensor_frame(self, instance_id: str, stream: str) -> Optional[Frame]:
        return self.frames.get(stream)

    def runtime_status(self) -> dict[str, Any]:
        self.runtime_calls += 1
        return {
            "instances": {"total": 1, "active": 1},
            "active_instance": {"instance_id": self.record.instance_id, "status": "running"},
            "carla": {"endpoint": "127.0.0.1:2000", "connected": True},
            "platform": {"enabled": False, "gateway_url": "", "kafka_bootstrap": "localhost:9092"},
            "sensor": self.sensor_body,
            # V3.5.5：推流名额/上限/帧率（面板用它把「MJPEG 不可用」从推测变成可读）
            "stream": {"clients": 3, "limit": 8, "fps": 20.0},
            "ros": self.ros_body,
        }


@pytest.fixture
def service() -> _FakeService:
    return _FakeService()


@pytest.fixture
def client(service: _FakeService) -> Iterator[TestClient]:
    with TestClient(create_app(service=service)) as test_client:
        yield test_client


class TestPageAndAssets:
    def test_viz_page_served(self, client: TestClient) -> None:
        response = client.get("/viz")

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "HunterSim 仿真可视化" in response.text

    def test_assets_are_whitelisted(self, client: TestClient) -> None:
        script = client.get("/viz/assets/app.js")
        style = client.get("/viz/assets/style.css")

        assert script.status_code == 200
        assert "javascript" in script.headers["content-type"]
        assert "refreshStatus" in script.text
        assert style.status_code == 200
        assert "text/css" in style.headers["content-type"]

    def test_unlisted_asset_is_404(self, client: TestClient) -> None:
        assert client.get("/viz/assets/secret.txt").status_code == 404
        assert client.get("/viz/assets/..%2F..%2Fpyproject.toml").status_code == 404

    def test_assets_require_revalidation(self, client: TestClient) -> None:
        """页面与资产必须每次重验证（V3.10.0）。

        回归现场：资产名不带指纹，浏览器做启发式缓存后，升级到新版仍用旧
        ``style.css`` 渲染，看起来就是「新布局没生效 / 排版错乱」。实际是缓存造成的
        假象，排查时极易误判为样式回归。
        """
        for path in ("/viz", "/viz/assets/app.js", "/viz/assets/style.css"):
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-cache"

    def test_terminal_edge_snapshot_is_kept(self, client: TestClient) -> None:
        """终态实例回显最后一份 EdgeLink 快照（V3.10.0）。

        回归现场：真 SIL 首帧控制超时 → 实例 failed → 运行时随场景销毁，
        ``/viz/api/status`` 的 ``edge_link`` 退回「未装配（脚本自车）」，面板遂把一次
        「车端没回 /cmd_vel」渲染成「脚本自车、无 Kafka 上行」，与本场失败原因自相矛盾。
        """
        script = client.get("/viz/assets/app.js").text

        assert "lastEdge: null" in script
        assert "state.lastEdge = edge;" in script
        assert "{ detached: true }" in script
        assert "SIL 已卸载" in script
        assert "本场结束前最后快照" in script
        # 切换实例必须丢弃上一场的快照，否则会把旧链路状态当成本场
        assert "state.lastEdge = null;" in script

    def test_layout_uses_single_page_scroll(self, client: TestClient) -> None:
        """整页只允许一条滚动条（V3.10.0）。

        回归现场：首版重排把右侧栏做成 ``position: sticky`` + ``max-height`` +
        ``overflow: auto``。整页滑动时这块“独立滚动区”会浮在下方面板之上，
        表现为“实例状态压住传感器/交通参与者、场景事件被挤成一条缝”，
        观感就是“页面割裂 + 内容错位”。侧栏只允许普通文档流。
        """
        css = client.get("/viz/assets/style.css").text
        blocks = re.findall(r"\.side-panel\s*\{([^}]*)\}", css)

        assert blocks, "style.css 里找不到 .side-panel 规则"
        for declarations in blocks:
            assert "position" not in declarations
            assert "overflow" not in declarations
            assert "max-height" not in declarations
        # 只保留数据表粘性表头这一处 sticky
        assert css.count("position: sticky") == 1

    def test_panel_columns_are_kept(self, client: TestClient) -> None:
        """左主栏 / 右栏的分工与右栏三段顺序（V3.10.0）。

        回归现场：重排过程中“场景事件”两次被挪错位置（先被浮层盖住、后又被搬到左栏底部），
        现场按“区域消失了”报障。右栏自上而下必须是 实例状态 → 场景事件 → 仿真日志。
        """
        html = client.get("/viz").text

        main_at = html.index('class="col col-main"')
        side_at = html.index('class="col col-side"')
        main_end = html.index("</main>")
        assert main_at < side_at < main_end
        # 宽表留在左主栏（8 列传感器 / 7 列参与者），否则会被右栏挤窄
        for panel in ("video-panel", "actor-panel", "sensor-panel", "edge-panel"):
            assert main_at < html.index(panel) < side_at
        # 右栏三段顺序 + 日志是右栏最后一块
        instance_at = html.index('class="panel side-panel"')
        event_at = html.index("event-panel")
        log_at = html.index("log-panel")
        assert side_at < instance_at < event_at < log_at < main_end
        assert event_at < html.index('id="event-list"') < log_at

    def test_columns_keep_ratio_and_unpinned_topbar(self, client: TestClient) -> None:
        """左右 2:1 比例、顶栏不冻结、右栏分工、自车状态两列（V3.10.0 / V3.10.2）。"""
        css = client.get("/viz/assets/style.css").text
        html = client.get("/viz").text

        # 左 2fr / 右 1fr，右栏有 320 px 下限，否则键值表会被折成一条窄条
        assert "grid-template-columns: minmax(0, 2fr) minmax(320px, 1fr)" in css
        # 标题栏随页面滚走：整份样式里只允许数据表表头这一处 sticky
        topbar = re.findall(r"\.topbar\s*\{([^}]*)\}", css)
        assert len(topbar) == 1, "style.css 里 .topbar 规则不止一处"
        assert "position" not in topbar[0]
        assert css.count("position: sticky") == 1
        # 右栏分工（V3.10.2）：事件区不瓜分剩余高度，日志 basis 归零后吃掉剩余
        assert re.search(r"\.col-side > \.event-panel\s*\{[^}]*flex: 0 0 auto", css)
        assert re.search(r"\.col-side > \.log-panel\s*\{[^}]*flex: 1 1 0", css)
        # 自车状态 7 行短值两列并排，实例状态卡整体高度砍掉近三成
        assert 'class="kv kv-tight" id="ego-info"' in html

    def test_event_height_cap_and_both_panels_scroll(self, client: TestClient) -> None:
        """事件区封顶到旧版约三倍，事件与日志各自内部滚动（V3.10.2）。

        回归现场（两头的错都犯过）：
        - 旧版把事件列表写死 ``max-height: 260px``，一场 24 条事件的场景只够看两三条；
        - V3.10.1 改成按窗口 3:1 瓜分，事件区被拉到 ~876 px，只有两三条事件时卡内
          下半部全是空白，现场按「扩大太多了」打回。
        现口径：事件按内容增长、列表封顶 460 px（≈旧版两三条时高度的三倍）后内部滚动；
        日志吃剩余高度，但 ``flex-basis`` 必须归零——否则日志会按 40 行内容把整页撑到
        3000+ px，也就永远不会滚动（开发时实测踩到）。
        """
        css = client.get("/viz/assets/style.css").text

        event_list = re.search(r"\.event-list\s*\{([^}]*list-style[^}]*)\}", css)
        assert event_list is not None
        assert "max-height: 460px" in event_list.group(1)
        assert "overflow: auto" in event_list.group(1)
        log_console = re.search(r"\.log-console\s*\{([^}]*)\}", css)
        assert log_console is not None
        assert "overflow: auto" in log_console.group(1)
        # 单列沿用同一封顶口径（不再另设一套高度），但日志给固定窗口
        narrow = css.split("@media (max-width: 1024px)")[-1]
        assert re.search(r"\.log-console\s*\{[^}]*height: 300px", narrow)

    def test_terminal_instance_stops_frame_polling(self, client: TestClient) -> None:
        """终态/未运行实例必须短路帧类轮询（V3.1.3 / V3.4.1）。

        回归背景：实例进入 succeeded/failed/canceled 后不再产生新帧，但
        ``/sensors/lidar``、``/sensors/imu`` 仍按 1.5s 被轮询并持续返回空内容，
        长驻运行会刷满服务端日志；pending（场景加载中）同样没有帧。
        """
        script = client.get("/viz/assets/app.js").text

        assert "function isTerminalInstance()" in script
        assert "function isRunningInstance()" in script
        # 预览（激光点云 / IMU / 深度）同时带终态与未运行短路
        assert "if (!state.instanceId || isTerminalInstance() || !isRunningInstance())" in script
        # 传感器帧统计与事件列表的定时轮询也必须走同一短路（不能直挂 setInterval）
        for name in ("refreshSensors", "refreshEvents"):
            assert (
                f"setInterval(gate(function () {{\n"
                f"      if (!isTerminalInstance()) {{\n"
                f"        {name}();"
            ) in script
        assert (
            "setInterval(gate(function () {\n"
            "      if (!isTerminalInstance() && isRunningInstance()) {\n"
            "        refreshPreviews();"
        ) in script
        # 终态后仍需一次收尾加载，否则传感器/事件面板停在旧值
        assert "state.terminalSynced = true" in script

    def test_upstream_backoff_gates_readonly_polling(self, client: TestClient) -> None:
        """控制面不可用时，只读轮询必须整体退避（V3.5.6）。

        回归现场：控制面原生崩溃到重启的 10~20s 里，本页 8 个独立轮询逐个撞
        已断开的端口，代理端刷屏式回 502/503，真正的报警被淹没。
        现在任一请求读到 50x 即按 ``Retry-After`` 暂停全部只读轮询。
        """
        script = client.get("/viz/assets/app.js").text

        assert "function isUpstreamDown(response)" in script
        assert "response.status === 502 || response.status === 503 || response.status === 504" in script
        # Retry-After 优先，无该头时回落到固定退避窗口
        assert 'response.headers.get("Retry-After")' in script
        assert "var UPSTREAM_BACKOFF_MS = " in script
        # 统一闸门：所有周期轮询都必须走 gate（共 7 处 setInterval）
        assert script.count("setInterval(gate(") == 7
        # 用户控制动作不能被静默，只能提示 + 退避
        assert "if (!response.ok) {" in script
        assert "控制失败：" in script
        # 单帧轮询在退避窗口内仍要续排下一轮，否则控制面恢复后画面永久停住
        assert "控制面不可用，已暂停轮询（自动恢复）" in script
        assert "// 退避窗口内不发请求，但要续排下一轮" in script

    def test_mjpeg_fallback_is_not_terminal(self, client: TestClient) -> None:
        """落到单帧轮询后必须能自己回到 MJPEG 图像流（V3.5.5）。

        回归背景：旧版 ``switchToScreenshot`` 是单向永久降级 —— 推流名额被僵尸
        连接吃满、或首帧 6s 看门狗超时一次，页面就长期停在 ~1.4 fps 的幻灯片上
        （``POLL_MS.screenshot=700ms``），越挂越卡且只能刷新页面才能恢复。
        """
        script = client.get("/viz/assets/app.js").text

        assert "function scheduleMjpegReprobe(" in script
        assert "async function reprobeMjpeg(" in script
        # 降级即计数并调度回探
        assert "state.mjpegFailures += 1" in script
        assert "pollScreenshot(instanceId);\n    scheduleMjpegReprobe(instanceId);" in script
        # 探测只读响应头、拿到即断（不打断当前画面，也不长占推流名额）
        assert "signal: controller ? controller.signal : undefined" in script
        assert 'contentType.indexOf("multipart") >= 0' in script
        # 递增退避（10s 起、上限 80s），探不中要接着排下一次
        assert "MJPEG_REPROBE_BASE_MS * Math.pow(2, step)" in script
        assert "MJPEG_REPROBE_MAX_MS" in script
        assert script.count("scheduleMjpegReprobe(instanceId);") >= 2
        # 首帧真正到手才算成功：复位退避并拆掉待触发的回探
        assert "state.mjpegFailures = 0" in script
        assert script.count("clearReprobe()") >= 3
        # 名额占用与当前画质档必须可读（卡顿可定性）
        assert '"推流客户端"' in script
        assert 'parts.push("q" + meta.quality)' in script

    def test_viz_is_not_in_openapi(self, client: TestClient) -> None:
        """可视化端点不进入平台 OpenAPI 契约（单一控制面口径）。"""
        paths = client.get("/openapi.json").json()["paths"]

        assert [path for path in paths if path.startswith("/viz")] == []


class TestStatus:
    def test_runtime_snapshot_is_merged(self, client: TestClient, service: _FakeService) -> None:
        body = client.get("/viz/api/status").json()

        assert service.runtime_calls >= 1
        assert isinstance(body["manager"]["port"], int)
        assert body["manager"]["port"] > 0
        assert body["carla"]["connected"] is True
        assert body["instances"] == {"total": 1, "active": 1}
        assert body["sensor"]["attached"] is True
        # 推流名额一眼可读：clients 贴近 limit 就是「MJPEG 不可用」的直接成因
        assert body["stream"] == {"clients": 3, "limit": 8, "fps": 20.0}
        assert body["log"]["capacity"] > 0
        assert isinstance(body["uptime_s"], (int, float))

    def test_missing_runtime_status_degrades(self) -> None:
        with TestClient(create_app(service=_ReadOnlyService())) as client:
            body = client.get("/viz/api/status").json()

        assert body["instances"] == {"total": 1, "active": 1}
        assert body["active_instance"]["instance_id"] == "inst-1"
        assert body["carla"]["connected"] is None
        # 服务替身没有 runtime_status 时给零值（面板显示“—”，不能因缺字段报错）
        assert body["stream"] == {"clients": 0, "limit": 0, "fps": 0.0}


class TestEvents:
    def test_counts_and_items(self, client: TestClient) -> None:
        body = client.get("/viz/api/instances/inst-1/events").json()

        assert body["count"] == 3
        assert body["counts"] == {"collision": 2, "lane_invasion": 1}
        assert [item["event_type"] for item in body["items"]][-1] == "lane_invasion"

    def test_limit_returns_tail(self, client: TestClient) -> None:
        body = client.get("/viz/api/instances/inst-1/events?limit=1").json()

        assert body["count"] == 3
        assert [item["description"] for item in body["items"]] == ["压线"]

    def test_unknown_instance_is_404(self, client: TestClient) -> None:
        assert client.get("/viz/api/instances/ghost/events").status_code == 404

    def test_service_without_events_returns_empty(self) -> None:
        with TestClient(create_app(service=_ReadOnlyService())) as client:
            body = client.get("/viz/api/instances/inst-1/events").json()

        assert body == {"instance_id": "inst-1", "count": 0, "counts": {}, "items": []}


class TestActors:
    def test_actor_items_with_visibility_summary(self, client: TestClient) -> None:
        body = client.get("/viz/api/instances/inst-1/actors").json()

        assert body["count"] == 2
        assert body["spawned"] == 2
        # 主视区计数用于面板一眼定位「车生成了但画面里看不到」
        assert body["visible"] == 1
        assert [item["id"] for item in body["items"]] == ["lead-1", "lead-2"]
        assert body["items"][1]["visible"] is False

    def test_actor_items_pass_through_platform_behavior_label(self, client: TestClient) -> None:
        """V3.8.7：平台行为标签原样透传（面板回显 sudden_cross 类原值）。"""
        body = client.get("/viz/api/instances/inst-1/actors").json()

        assert body["items"][0]["source_behavior_type"] == "forward"
        # 旧载荷无该字段时不造假：面板自行退回内部行为
        assert "source_behavior_type" not in body["items"][1]

    def test_service_without_actors_returns_empty(self) -> None:
        with TestClient(create_app(service=_ReadOnlyService())) as client:
            body = client.get("/viz/api/instances/inst-1/actors").json()

        assert body == {
            "instance_id": "inst-1",
            "count": 0,
            "spawned": 0,
            "visible": 0,
            "items": [],
        }


class TestSensors:
    def test_streams_and_ros_merged(self, client: TestClient) -> None:
        body = client.get("/viz/api/instances/inst-1/sensors").json()

        assert body["attached"] is True
        assert body["streams"]["rgb"]["count"] == 3
        assert body["ros"]["enabled"] is True
        assert body["ros"]["topics"] == {"/carla/lidar_points": 5}

    def test_rgb_frame_passthrough(self, client: TestClient, service: _FakeService) -> None:
        service.frames["rgb"] = _frame(
            "rgb", b"\xff\xd8fake-jpeg", {"content_type": "image/jpeg", "width": 100, "height": 50}
        )

        response = client.get("/viz/api/instances/inst-1/sensors/rgb")

        assert response.status_code == 200
        assert response.content == b"\xff\xd8fake-jpeg"
        assert response.headers["content-type"] == "image/jpeg"

    def test_overview_frame_passthrough(self, client: TestClient, service: _FakeService) -> None:
        service.frames["overview"] = _frame(
            "overview",
            b"\xff\xd8overview-jpeg",
            {"content_type": "image/jpeg", "width": 960, "height": 540},
        )

        response = client.get("/viz/api/instances/inst-1/sensors/overview")

        assert response.status_code == 200
        assert response.content == b"\xff\xd8overview-jpeg"
        assert response.headers["content-type"] == "image/jpeg"

    def test_overview_falls_back_to_rgb(self, client: TestClient, service: _FakeService) -> None:
        service.frames["rgb"] = _frame(
            "rgb", b"\xff\xd8front-jpeg", {"content_type": "image/jpeg"}
        )

        response = client.get("/viz/api/instances/inst-1/sensors/overview")

        assert response.status_code == 200
        assert response.content == b"\xff\xd8front-jpeg"

    def test_missing_frame_is_204(self, client: TestClient) -> None:
        """无帧时返回 204（启动早期不产生 409 错误日志）。"""
        response = client.get("/viz/api/instances/inst-1/sensors/imu")

        assert response.status_code == 204
        assert response.content == b""

    def test_unknown_stream_is_404(self, client: TestClient) -> None:
        assert client.get("/viz/api/instances/inst-1/sensors/radar").status_code == 404

    def test_lidar_frame_is_sampled_json(self, client: TestClient, service: _FakeService) -> None:
        pytest.importorskip("numpy")
        service.frames["lidar"] = _frame(
            "lidar", struct.pack("<6f", 1.0, 2.0, 0.0, 3.0, 4.0, 0.5), {"points": 2}
        )

        body = client.get("/viz/api/instances/inst-1/sensors/lidar").json()

        assert body["total"] == 2
        assert body["sampled"] == 2
        assert body["points"][0] == [1.0, 2.0, 0.0]

    def test_imu_frame_is_parsed(self, client: TestClient, service: _FakeService) -> None:
        service.frames["imu"] = _frame("imu", b"((1.0, 2.0, 3.0), (0.1, 0.2, 0.3))")

        body = client.get("/viz/api/instances/inst-1/sensors/imu").json()

        assert body["accel"] == [1.0, 2.0, 3.0]
        assert body["gyro"] == [0.1, 0.2, 0.3]

    def test_unparsable_imu_falls_back_to_raw(self, client: TestClient, service: _FakeService) -> None:
        service.frames["imu"] = _frame("imu", b"not-a-tuple")

        body = client.get("/viz/api/instances/inst-1/sensors/imu").json()

        assert body["raw"] == "not-a-tuple"

    def test_depth_frame_renders_png(self, client: TestClient, service: _FakeService) -> None:
        pytest.importorskip("numpy")
        payload = struct.pack("<4f", 1.0, 2.0, 3.0, 4.0)
        service.frames["depth"] = _frame("depth", payload, {"width": 2, "height": 2})

        response = client.get("/viz/api/instances/inst-1/sensors/depth")

        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.content.startswith(b"\x89PNG")

    def test_depth_without_numpy_is_503(
        self, client: TestClient, service: _FakeService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service.frames["depth"] = _frame("depth", struct.pack("<4f", 1.0, 2.0, 3.0, 4.0), {"width": 2, "height": 2})
        monkeypatch.setitem(sys.modules, "numpy", None)  # 强制 numpy 不可用

        response = client.get("/viz/api/instances/inst-1/sensors/depth")

        assert response.status_code == 503
        assert "sensor_render_unavailable" in response.json()["detail"]


class TestLogs:
    def _bus(self, client: TestClient) -> LogBus:
        bus = client.app.state.log_bus
        assert isinstance(bus, LogBus)
        return bus

    def test_snapshot_filters_by_level_and_keyword(self, client: TestClient) -> None:
        bus = self._bus(client)
        bus.publish("DEBUG", "hunter_sim.demo", "viz-demo trace")
        bus.publish("INFO", "hunter_sim.demo", "viz-demo ready")
        bus.publish("ERROR", "hunter_sim.demo", "viz-demo boom")

        body = client.get("/viz/api/logs?level=INFO&contains=viz-demo&limit=50").json()

        assert [item["level"] for item in body["items"]] == ["INFO", "ERROR"]
        assert body["min_level"] == "INFO"
        assert body["counts"]["total"] >= 3

    def test_since_seq_resumes(self, client: TestClient) -> None:
        bus = self._bus(client)
        cursor = bus.publish("INFO", "hunter_sim.demo", "viz-demo first").seq
        bus.publish("INFO", "hunter_sim.demo", "viz-demo second")

        body = client.get(f"/viz/api/logs?level=INFO&contains=viz-demo&since_seq={cursor}").json()

        assert [item["message"] for item in body["items"]] == ["viz-demo second"]
        assert body["next_since"] == cursor + 1

    def test_limit_must_be_positive(self, client: TestClient) -> None:
        assert client.get("/viz/api/logs?limit=0").status_code == 422


class TestLogStream:
    """SSE 端点：直接驱动异步生成器，避免测试等待 keep-alive。"""

    class _Request:
        def __init__(self, app: Any) -> None:
            self.app = app

        async def is_disconnected(self) -> bool:
            return False

    def _endpoint(self, app: Any) -> Any:
        for route in app.routes:
            if getattr(route, "path", "") == "/viz/api/logs/stream":
                return route.endpoint
        raise AssertionError("log stream route not registered")

    async def test_stream_replays_history_then_pushes(self) -> None:
        service = _FakeService()
        app = create_app(service=service)
        bus = LogBus(max_records=10)
        app.state.adapter_service = service
        app.state.log_bus = bus
        app.state.started_at = time.time()
        request = self._Request(app)
        endpoint = self._endpoint(app)

        response = await endpoint(request=request, level="INFO", contains="", history=10)
        bus.publish("ERROR", "hunter_sim.demo", "viz-stream boom")

        assert response.media_type == "text/event-stream"
        iterator = response.body_iterator
        try:
            assert await asyncio.wait_for(iterator.__anext__(), 1.0) == b": connected\n\n"
            chunk = await asyncio.wait_for(iterator.__anext__(), 1.0)
        finally:
            await iterator.aclose()

        record = json.loads(chunk.decode("utf-8").removeprefix("data: "))
        assert record["message"] == "viz-stream boom"
        assert record["level"] == "ERROR"
