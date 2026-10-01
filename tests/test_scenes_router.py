"""场景管理路由测试（PROMPT-API-001）。

通过向 app.state.scene_runners 注入 FakeRunner 驱动各路由成功/失败分支，
不依赖真实 CARLA / 场景运行服务。
"""

from __future__ import annotations

from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient

from hunter_sim.engine.weather_manager import WeatherProfile

_VALID_CONFIG: dict[str, Any] = {
    "scene_id": "scene-001",
    "scene_name": "测试场景",
    "map_id": "Town05",
    "ego_vehicle": {"spawn_point": {"x": 1.0, "y": 2.0, "yaw_deg": 90.0}},
}


class _FakeWeather:
    """与真实 WeatherManager 接口对齐的桩（set_preset/set_weather/start_transition/current_profile）。"""

    def __init__(self) -> None:
        self.preset_called: Optional[str] = None
        self.set_weather_called: Optional[Any] = None
        self.transition_target: Optional[Any] = None
        self.transition_seconds: Optional[float] = None

    def set_preset(self, preset_name: str) -> None:
        self.preset_called = preset_name

    def set_weather(self, profile: Any) -> None:
        self.set_weather_called = profile

    def start_transition(self, target: Any, duration_seconds: float = 3.0) -> None:
        self.transition_target = target
        self.transition_seconds = duration_seconds

    @property
    def current_profile(self) -> WeatherProfile:
        return WeatherProfile(preset_name="sunny_noon")


class _FakeCoord:
    def __init__(self) -> None:
        self.calibration: Optional[Any] = None

    def update_calibration(self, params: Any) -> None:
        self.calibration = params


class _FakeRunner:
    def __init__(self, load_raises: bool = False, screenshot: Optional[str] = None) -> None:
        self.loaded: list = []
        self.started = False
        self.stopped = False
        self.paused = False
        self.resumed = False
        self._load_raises = load_raises
        self.weather_manager = _FakeWeather()
        self.coordinate_transformer = _FakeCoord()
        self._screenshot = screenshot

    def load_scene(self, config: Any) -> None:
        if self._load_raises:
            raise RuntimeError("CARLA connection lost")
        self.loaded.append(config)

    def get_scene_state(self) -> dict:
        return {"scene_id": "scene-001", "status": "ready", "progress": 0.25}

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.resumed = True

    def get_latest_screenshot(self) -> Optional[str]:
        return self._screenshot


@pytest.fixture
def runner(app: Any) -> Any:
    """为已知实例注入 FakeRunner，测试后清理。"""
    inst = "inst-scene-1"
    fake = _FakeRunner()
    app.state.scene_runners = {inst: fake}
    yield inst, fake
    app.state.scene_runners = {}


class TestSceneLoad:
    def test_load_valid(self, client: TestClient, auth_headers: dict, runner: tuple) -> None:
        inst, fake = runner
        resp = client.post(
            "/api/v1/sim/scenes/load",
            json={"instance_id": inst, "scene_config": _VALID_CONFIG},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["scene_id"] == "scene-001"
        assert len(fake.loaded) == 1

    def test_load_invalid_config_422(
        self, client: TestClient, auth_headers: dict, runner: tuple
    ) -> None:
        inst, _ = runner
        resp = client.post(
            "/api/v1/sim/scenes/load",
            json={"instance_id": inst, "scene_config": {"scene_id": "x"}},
            headers=auth_headers,
        )
        assert resp.status_code == 422

    def test_load_runner_error_500(
        self, client: TestClient, auth_headers: dict, app: Any
    ) -> None:
        inst = "inst-err"
        app.state.scene_runners = {inst: _FakeRunner(load_raises=True)}
        resp = client.post(
            "/api/v1/sim/scenes/load",
            json={"instance_id": inst, "scene_config": _VALID_CONFIG},
            headers=auth_headers,
        )
        assert resp.status_code == 500
        app.state.scene_runners = {}

    def test_missing_runner_404(self, client: TestClient, auth_headers: dict) -> None:
        resp = client.get(
            "/api/v1/sim/scenes/unknown-inst/status", headers=auth_headers
        )
        assert resp.status_code == 404


class TestSceneControls:
    def test_status(self, client: TestClient, auth_headers: dict, runner: tuple) -> None:
        inst, _ = runner
        resp = client.get(f"/api/v1/sim/scenes/{inst}/status", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "ready"

    def test_start_stop_pause_resume(
        self, client: TestClient, auth_headers: dict, runner: tuple
    ) -> None:
        inst, fake = runner
        assert client.post(f"/api/v1/sim/scenes/{inst}/start", headers=auth_headers).status_code == 200
        assert client.post(f"/api/v1/sim/scenes/{inst}/stop", headers=auth_headers).status_code == 200
        assert client.post(f"/api/v1/sim/scenes/{inst}/pause", headers=auth_headers).status_code == 200
        assert client.post(f"/api/v1/sim/scenes/{inst}/resume", headers=auth_headers).status_code == 200
        assert fake.started and fake.stopped and fake.paused and fake.resumed


class TestWeatherControl:
    def test_preset_with_transition(self, client: TestClient, auth_headers: dict, runner: tuple) -> None:
        """预设天气 + 过渡时长 > 0 → start_transition（真实 WeatherManager 接口）。"""
        inst, fake = runner
        resp = client.post(
            f"/api/v1/sim/scenes/{inst}/weather",
            json={"preset": "heavy_rain", "transition_seconds": 5.0},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["transitioning"] is True
        assert fake.weather_manager.transition_target is not None
        assert fake.weather_manager.transition_target.preset_name == "heavy_rain"
        assert fake.weather_manager.transition_seconds == 5.0

    def test_preset_immediate(self, client: TestClient, auth_headers: dict, runner: tuple) -> None:
        """过渡时长 0 → set_preset 立即生效。"""
        inst, fake = runner
        resp = client.post(
            f"/api/v1/sim/scenes/{inst}/weather",
            json={"preset": "night", "transition_seconds": 0.0},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert fake.weather_manager.preset_called == "night"

    def test_unknown_preset_422(self, client: TestClient, auth_headers: dict, runner: tuple) -> None:
        """非 PRESET_ENVIRONMENTS 白名单预设 → 422。"""
        inst, fake = runner
        resp = client.post(
            f"/api/v1/sim/scenes/{inst}/weather",
            json={"preset": "rainy_night"},
            headers=auth_headers,
        )
        assert resp.status_code == 422
        assert fake.weather_manager.transition_target is None

    def test_custom(self, client: TestClient, auth_headers: dict, runner: tuple) -> None:
        """自定义参数 0-100 刻度直通（审查项 C）。"""
        inst, fake = runner
        resp = client.post(
            f"/api/v1/sim/scenes/{inst}/weather",
            json={"cloudiness": 60.0, "precipitation": 30.0},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        target = fake.weather_manager.transition_target
        assert target is not None
        assert target.cloudiness == 60.0
        assert target.precipitation == 30.0
        assert target.preset_name == ""

    def test_custom_road_wetness_mapping(
        self, client: TestClient, auth_headers: dict, runner: tuple
    ) -> None:
        """API 字段 road_wetness → 内部 precipitation_deposits 映射。"""
        inst, fake = runner
        resp = client.post(
            f"/api/v1/sim/scenes/{inst}/weather",
            json={"road_wetness": 80.0, "transition_seconds": 0.0},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        profile = fake.weather_manager.set_weather_called
        assert profile is not None
        assert profile.precipitation_deposits == 80.0

    def test_no_params_400(self, client: TestClient, auth_headers: dict, runner: tuple) -> None:
        inst, _ = runner
        resp = client.post(
            f"/api/v1/sim/scenes/{inst}/weather", json={}, headers=auth_headers
        )
        assert resp.status_code == 400

    def test_weather_manager_missing_503(
        self, client: TestClient, auth_headers: dict, app: Any
    ) -> None:
        inst = "inst-noweather"
        fake = _FakeRunner()
        fake.weather_manager = None
        app.state.scene_runners = {inst: fake}
        resp = client.post(
            f"/api/v1/sim/scenes/{inst}/weather",
            json={"preset": "sunny_noon"},
            headers=auth_headers,
        )
        assert resp.status_code == 503
        app.state.scene_runners = {}


class TestCalibration:
    def test_calibrate_origin_odom(self, client: TestClient, auth_headers: dict, runner: tuple) -> None:
        """odom 为默认原点时 x0/y0 直接等于地图坐标（文档 §12.3）。"""
        inst, fake = runner
        resp = client.post(
            f"/api/v1/sim/scenes/{inst}/calibrate",
            json={"map_x": 100.5, "map_y": 50.2, "map_heading": 90.0},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        params = fake.coordinate_transformer.calibration
        assert params is not None
        assert params.x0 == 100.5
        assert params.y0 == 50.2
        # CARLA 左手系约定：yaw0 = -radians(map_heading)
        assert round(params.yaw0, 4) == -1.5708
        data = resp.json()["data"]
        assert data["map_heading"] == 90.0
        assert round(data["yaw0_rad"], 4) == -1.5708

    def test_calibrate_with_odom_offset(
        self, client: TestClient, auth_headers: dict, runner: tuple
    ) -> None:
        """非零 odom 位姿时推导偏移量。"""
        inst, fake = runner
        resp = client.post(
            f"/api/v1/sim/scenes/{inst}/calibrate",
            json={
                "map_x": 100.0, "map_y": 50.0, "map_heading": 0.0,
                "odom_x": 10.0, "odom_y": 5.0, "odom_heading": 0.0,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        params = fake.coordinate_transformer.calibration
        # yaw0=0 → x0 = 100-10, y0 = 50+5（Y 轴翻转补偿）
        assert params.x0 == 90.0
        assert params.y0 == 55.0
        assert params.yaw0 == 0.0

    def test_calibrate_no_transformer_503(
        self, client: TestClient, auth_headers: dict, app: Any
    ) -> None:
        inst = "inst-nocal"
        fake = _FakeRunner()
        fake.coordinate_transformer = None
        app.state.scene_runners = {inst: fake}
        resp = client.post(
            f"/api/v1/sim/scenes/{inst}/calibrate",
            json={"map_x": 1.0, "map_y": 1.0, "map_heading": 0.0},
            headers=auth_headers,
        )
        assert resp.status_code == 503
        app.state.scene_runners = {}


class TestScreenshot:
    def test_screenshot_present(self, client: TestClient, auth_headers: dict, app: Any) -> None:
        inst = "inst-shot"
        app.state.scene_runners = {inst: _FakeRunner(screenshot="BASE64PNG")}
        resp = client.get(f"/api/v1/sim/scenes/{inst}/screenshot", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["data"]["image_base64"] == "BASE64PNG"
        app.state.scene_runners = {}

    def test_screenshot_absent_404(
        self, client: TestClient, auth_headers: dict, runner: tuple
    ) -> None:
        inst, _ = runner
        resp = client.get(f"/api/v1/sim/scenes/{inst}/screenshot", headers=auth_headers)
        assert resp.status_code == 404
