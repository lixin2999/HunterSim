"""场景管理路由测试（PROMPT-API-001）。

通过向 app.state.scene_runners 注入 FakeRunner 驱动各路由成功/失败分支，
不依赖真实 CARLA / 场景运行服务。
"""

from __future__ import annotations

from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient

_VALID_CONFIG: dict[str, Any] = {
    "scene_id": "scene-001",
    "scene_name": "测试场景",
    "map_id": "Town05",
    "ego_vehicle": {"spawn_point": {"x": 1.0, "y": 2.0, "yaw_deg": 90.0}},
}


class _FakeWeather:
    def __init__(self) -> None:
        self.preset_called: Optional[str] = None
        self.custom_called: Optional[dict] = None

    def apply_preset(self, name: str, transition_seconds: float = 0.0) -> None:
        self.preset_called = name

    def apply_custom(self, params: dict, transition_seconds: float = 0.0) -> None:
        self.custom_called = params


class _FakeCoord:
    def __init__(self) -> None:
        self.calibration: Optional[tuple] = None

    def set_calibration(self, x0: float, y0: float, yaw0_rad: float) -> None:
        self.calibration = (x0, y0, yaw0_rad)


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
    def test_preset(self, client: TestClient, auth_headers: dict, runner: tuple) -> None:
        inst, fake = runner
        resp = client.post(
            f"/api/v1/sim/scenes/{inst}/weather",
            json={"preset": "rainy_night"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert fake.weather_manager.preset_called == "rainy_night"

    def test_custom(self, client: TestClient, auth_headers: dict, runner: tuple) -> None:
        inst, fake = runner
        resp = client.post(
            f"/api/v1/sim/scenes/{inst}/weather",
            json={"cloudiness": 60.0, "precipitation": 30.0},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert fake.weather_manager.custom_called == {"cloudiness": 60.0, "precipitation": 30.0}

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
    def test_calibrate(self, client: TestClient, auth_headers: dict, runner: tuple) -> None:
        inst, fake = runner
        resp = client.post(
            f"/api/v1/sim/scenes/{inst}/calibrate",
            json={"x0": 10.0, "y0": 20.0, "yaw0_deg": 45.0},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert fake.coordinate_transformer.calibration is not None
        x0, y0, yaw = fake.coordinate_transformer.calibration
        assert (x0, y0) == (10.0, 20.0)
        assert round(yaw, 4) == 0.7854

    def test_calibrate_no_transformer_503(
        self, client: TestClient, auth_headers: dict, app: Any
    ) -> None:
        inst = "inst-nocal"
        fake = _FakeRunner()
        fake.coordinate_transformer = None
        app.state.scene_runners = {inst: fake}
        resp = client.post(
            f"/api/v1/sim/scenes/{inst}/calibrate",
            json={"x0": 1.0, "y0": 1.0, "yaw0_deg": 0.0},
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
