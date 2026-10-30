/*
 * HunterSim 仿真可视化客户端（纯浏览器脚本，无构建步骤）。
 *
 * 数据来源（全部同源，无跨域）：
 *   /viz/api/status                          顶栏状态灯 + 仿真端模块连通性
 *   /viz/api/instances/{id}/events          场景事件（碰撞/偏离/通过…）
 *   /viz/api/instances/{id}/sensors         传感器各流帧数/帧率 + ROS2 话题
 *   /viz/api/instances/{id}/sensors/{stream} 单流最新帧（深度 PNG / 点云 JSON / IMU JSON）
 *   /viz/api/logs、/viz/api/logs/stream     仿真日志（SSE 增量，断线降级为轮询）
 *   /api/v1/instances[/{id}]                实例列表/进度（平台既有契约）
 *   /api/v1/instances/{id}/stream.mjpg      实时画面（MJPEG，失败降级单帧轮询）
 *
 * 该脚本只读运行态，除暂停/恢复/停止三个既有控制端点外不触发任何副作用。
 */
(function () {
  "use strict";

  var POLL_MS = {
    status: 3000,
    instances: 4000,
    detail: 1500,
    sensors: 2000,
    events: 2000,
    preview: 1500,
    screenshot: 700,
    logFallback: 1500
  };
  var LOG_MAX_LINES = 1500;
  var VIDEO_WATCHDOG_MS = 6000;
  var TERMINAL = ["succeeded", "failed", "canceled"];
  var STREAM_ORDER = ["rgb", "depth", "lidar", "imu"];

  var state = {
    instanceId: "",
    instanceStatus: "",
    carlaStatus: "",
    videoInstance: "",
    videoMode: "",
    videoTimer: 0,
    videoWatchdog: 0,
    videoObjectUrl: "",
    depthOn: false,
    depthUrl: "",
    logSource: null,
    logPoller: 0,
    logMode: "",
    logPaused: false,
    logSeq: 0,
    filterTimer: 0
  };

  // ── 通用工具 ──────────────────────────────────────────────────────────────

  function $(id) {
    return document.getElementById(id);
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined && text !== null) {
      node.textContent = String(text);
    }
    return node;
  }

  function num(value, digits) {
    if (typeof value !== "number" || !isFinite(value)) {
      return "—";
    }
    return value.toFixed(digits === undefined ? 2 : digits);
  }

  function pad(value, width) {
    return String(value).padStart(width || 2, "0");
  }

  function clock(ts) {
    if (typeof ts !== "number" || !isFinite(ts) || ts <= 0) {
      return "—";
    }
    var date = new Date(ts * 1000);
    return (
      pad(date.getHours()) +
      ":" +
      pad(date.getMinutes()) +
      ":" +
      pad(date.getSeconds()) +
      "." +
      pad(date.getMilliseconds(), 3)
    );
  }

  function humanDuration(seconds) {
    var total = Math.max(0, Math.floor(typeof seconds === "number" ? seconds : 0));
    var hours = Math.floor(total / 3600);
    var minutes = Math.floor((total % 3600) / 60);
    var rest = total % 60;
    if (hours > 0) {
      return hours + "h" + pad(minutes) + "m";
    }
    if (minutes > 0) {
      return minutes + "m" + pad(rest) + "s";
    }
    return rest + "s";
  }

  function formatBytes(size) {
    if (typeof size !== "number" || !isFinite(size) || size < 0) {
      return "—";
    }
    if (size < 1024) {
      return size + " B";
    }
    if (size < 1024 * 1024) {
      return (size / 1024).toFixed(1) + " KB";
    }
    return (size / (1024 * 1024)).toFixed(2) + " MB";
  }

  function fillKv(target, rows) {
    target.textContent = "";
    rows.forEach(function (row) {
      target.appendChild(el("dt", "", row[0]));
      var value = row[1];
      var empty = value === undefined || value === null || value === "";
      target.appendChild(el("dd", "", empty ? "—" : value));
    });
  }

  function setBadge(id, text, kind) {
    var node = $(id);
    node.textContent = text;
    node.className = "badge" + (kind ? " " + kind : "");
  }

  function setNote(id, text) {
    $(id).textContent = text;
  }

  function debounce(fn, ms) {
    return function () {
      if (state.filterTimer) {
        clearTimeout(state.filterTimer);
      }
      state.filterTimer = setTimeout(fn, ms);
    };
  }

  function emptyRow(cols, text) {
    var row = el("tr");
    var cell = el("td", "muted", text);
    cell.colSpan = cols;
    row.appendChild(cell);
    return row;
  }

  function describeMeta(meta) {
    if (!meta) {
      return "—";
    }
    var parts = [];
    if (meta.content_type) {
      parts.push(String(meta.content_type).replace("image/", ""));
    }
    if (meta.width && meta.height) {
      parts.push(meta.width + "×" + meta.height);
    }
    if (meta.points) {
      parts.push(meta.points + " 点");
    }
    if (meta.encoding) {
      parts.push(String(meta.encoding));
    }
    return parts.length ? parts.join(" · ") : "—";
  }

  async function fetchJSON(url, options) {
    try {
      var response = await fetch(url, Object.assign({ cache: "no-store" }, options || {}));
      if (!response.ok) {
        return null;
      }
      return await response.json();
    } catch (err) {
      return null;
    }
  }

  function instancePath(suffix, instanceId) {
    return "/api/v1/instances/" + encodeURIComponent(instanceId || state.instanceId) + (suffix || "");
  }

  function vizPath(suffix, instanceId) {
    return "/viz/api/instances/" + encodeURIComponent(instanceId || state.instanceId) + (suffix || "");
  }

  // ── 顶栏：运行态与模块连通性 ──────────────────────────────────────────────

  async function refreshStatus() {
    var payload = await fetchJSON("/viz/api/status");
    if (!payload) {
      setBadge("badge-manager", "控制面 离线", "err");
      return;
    }
    var manager = payload.manager || {};
    var carla = payload.carla || {};
    var platform = payload.platform || {};
    var ros = payload.ros || {};
    var sensor = payload.sensor || {};
    var log = payload.log || {};
    var instances = payload.instances || {};
    var active = payload.active_instance;

    setNote(
      "version",
      "v" + (payload.version || "?") + " · " + (payload.env || "-") + " · 已运行 " + humanDuration(payload.uptime_s)
    );
    setBadge("badge-manager", "控制面 :" + (manager.port || "?"), "ok");
    if (carla.connected === true) {
      setBadge("badge-carla", "CARLA 已连接", "ok");
    } else if (carla.connected === false) {
      setBadge("badge-carla", "CARLA 未连接", "err");
    } else {
      setBadge("badge-carla", "CARLA 未知", "warn");
    }
    setBadge("badge-platform", platform.enabled ? "平台已接入" : "平台未启用", platform.enabled ? "ok" : "warn");
    setBadge("badge-ros", ros.enabled ? "ROS2 已启用" : "ROS2 未启用", ros.enabled ? "ok" : "");
    setBadge(
      "badge-scene",
      active ? "场景 " + (active.status || "?") : "场景空闲",
      active ? "ok" : ""
    );

    var published = Object.keys(ros.topics || {}).filter(function (key) {
      return ros.topics[key] > 0;
    });
    fillKv($("module-info"), [
      ["实例", (instances.total || 0) + " 个（活动 " + (instances.active || 0) + "）"],
      ["探针面", (payload.probes || {}).url || "—"],
      ["CARLA", carla.endpoint || "—"],
      ["平台网关", platform.enabled ? platform.gateway_url : "未启用"],
      ["Kafka", platform.kafka_bootstrap || "—"],
      ["传感器", sensor.attached ? "已挂载" : "未挂载"],
      ["ROS 话题", ros.enabled ? published.length + " 路有数据" : "未启用"],
      ["日志缓冲", (log.records || 0) + " / " + (log.capacity || 0)]
    ]);
  }

  // ── 实例选择与控制 ────────────────────────────────────────────────────────

  async function refreshInstances() {
    var payload = await fetchJSON("/api/v1/instances");
    var items = (payload && payload.items) || [];
    var select = $("instance-select");
    var keep = state.instanceId;

    select.textContent = "";
    if (!items.length) {
      select.appendChild(new Option("暂无实例", ""));
      select.disabled = true;
      selectInstance("");
      return;
    }
    select.disabled = false;
    items.forEach(function (item) {
      var label =
        (item.scene_name || item.scene_id || "未命名场景") +
        " · " +
        (item.status || "?") +
        " · " +
        String(item.instance_id || "").slice(0, 8);
      select.appendChild(new Option(label, item.instance_id));
    });

    var keepIndex = items.findIndex(function (item) {
      return item.instance_id === keep;
    });
    if (keepIndex < 0) {
      var target =
        items.find(function (item) {
          return item.status === "running";
        }) ||
        items.find(function (item) {
          return item.status === "pending";
        }) ||
        items[0];
      selectInstance(target.instance_id);
      return;
    }
    select.value = keep;
    applyRecord(items[keepIndex]);
  }

  function applyRecord(record) {
    if (!record) {
      return;
    }
    state.instanceStatus = record.status || "";
    state.carlaStatus = record.carla_status || "";
    updateControls();
  }

  function updateControls() {
    var terminal = TERMINAL.indexOf(state.instanceStatus) >= 0;
    var has = Boolean(state.instanceId) && !terminal;
    var paused = state.carlaStatus === "PAUSED";
    $("btn-pause").disabled = !has || paused;
    $("btn-resume").disabled = !has || !paused;
    $("btn-stop").disabled = !has;
  }

  function selectInstance(instanceId) {
    if (instanceId === state.instanceId) {
      return;
    }
    state.instanceId = instanceId || "";
    state.instanceStatus = "";
    state.carlaStatus = "";
    var select = $("instance-select");
    if (state.instanceId) {
      select.value = state.instanceId;
    }
    $("event-list").textContent = "";
    setNote("event-summary", "—");
    $("sensor-table").querySelector("tbody").textContent = "";
    $("ros-table").querySelector("tbody").textContent = "";
    fillKv($("instance-info"), []);
    fillKv($("ego-info"), []);
    fillKv($("imu-info"), []);
    updateControls();
    if (state.instanceId) {
      startVideo(state.instanceId);
      refreshDetail();
      refreshSensors();
      refreshEvents();
      refreshPreviews();
    } else {
      stopVideo();
      setNote("sensor-note", "未选择实例");
      setNote("ros-note", "—");
      setNote("video-meta", "未推流");
      setNote("video-empty", "暂无活动场景，等待平台下发实例…");
    }
  }

  async function postControl(action) {
    if (!state.instanceId) {
      return;
    }
    var target = state.instanceId;
    try {
      var response = await fetch(instancePath("/" + action), { method: "POST" });
      if (!response.ok) {
        setNote("stream-state", "控制失败：" + action + " → HTTP " + response.status);
      }
    } catch (err) {
      setNote("stream-state", "控制失败：" + action + " → 网络异常");
    }
    if (state.instanceId === target) {
      refreshDetail();
      refreshSensors();
    }
    refreshInstances();
    refreshStatus();
  }

  // ── 实例进度与自车状态 ────────────────────────────────────────────────────

  async function refreshDetail() {
    if (!state.instanceId) {
      fillKv($("instance-info"), [["实例", "无活动实例"]]);
      fillKv($("ego-info"), [["状态", "等待平台下发"]]);
      return;
    }
    var target = state.instanceId;
    var payload = await fetchJSON(instancePath());
    if (!payload || state.instanceId !== target) {
      return;
    }
    applyRecord(payload);
    var progress =
      typeof payload.progress_percent === "number" ? payload.progress_percent.toFixed(1) + "%" : "—";
    fillKv($("instance-info"), [
      ["实例 ID", payload.instance_id],
      ["场景", payload.scene_name || payload.scene_id],
      ["模式", payload.mode],
      ["车辆", payload.vehicle_id || "—"],
      ["状态", (payload.status || "?") + " / " + (payload.carla_status || "?")],
      ["进度", progress],
      ["仿真时间", num(payload.current_time_s, 1) + " / " + num(payload.total_time_s, 1) + " s"],
      ["碰撞", payload.collisions],
      ["车道侵入", payload.lane_invasions],
      ["录制", payload.recording_path ? formatBytes(payload.recording_bytes) : "未录制"],
      ["更新时间", payload.updated_at || "—"]
    ]);

    var ego = payload.ego_state || {};
    var heading = ego.heading === undefined ? ego.yaw : ego.heading;
    fillKv($("ego-info"), [
      ["位置 x", num(ego.x) + " m"],
      ["位置 y", num(ego.y) + " m"],
      ["位置 z", num(ego.z) + " m"],
      ["航向", num(heading, 1) + "°"],
      ["速度", num(ego.velocity) + " m/s"],
      ["转角", num(ego.steering, 3)],
      ["加速度", num(ego.acceleration)]
    ]);
  }

  // ── 实时画面（MJPEG → 单帧轮询降级） ─────────────────────────────────────

  function startVideo(instanceId) {
    stopVideo();
    state.videoInstance = instanceId;
    state.videoMode = "mjpeg";
    var img = $("video");
    var stage = img.parentElement;
    stage.classList.remove("live");
    setNote("video-empty", "连接图像流…");
    setNote("video-meta", "MJPEG 连接中");
    setNote("stream-state", "图像流（MJPEG）");

    img.onload = function () {
      if (state.videoInstance !== instanceId) {
        return;
      }
      stage.classList.add("live");
      setNote("stream-state", "图像流（MJPEG 实时）");
    };
    img.onerror = function () {
      if (state.videoInstance !== instanceId) {
        return;
      }
      if (state.videoMode === "mjpeg") {
        switchToScreenshot(instanceId, "MJPEG 不可用（相机未挂载或推流已达上限）");
      }
    };
    img.src = instancePath("/stream.mjpg?t=" + Date.now(), instanceId);

    state.videoWatchdog = window.setTimeout(function () {
      if (state.videoInstance !== instanceId || state.videoMode !== "mjpeg") {
        return;
      }
      if (!stage.classList.contains("live")) {
        switchToScreenshot(instanceId, "MJPEG 首帧超时，改为单帧轮询");
      }
    }, VIDEO_WATCHDOG_MS);
  }

  function stopVideo() {
    if (state.videoWatchdog) {
      clearTimeout(state.videoWatchdog);
      state.videoWatchdog = 0;
    }
    if (state.videoTimer) {
      clearTimeout(state.videoTimer);
      state.videoTimer = 0;
    }
    var img = $("video");
    img.onload = null;
    img.onerror = null;
    img.removeAttribute("src");
    img.parentElement.classList.remove("live");
    if (state.videoObjectUrl) {
      URL.revokeObjectURL(state.videoObjectUrl);
      state.videoObjectUrl = "";
    }
    state.videoInstance = "";
    state.videoMode = "";
  }

  function switchToScreenshot(instanceId, reason) {
    if (state.videoInstance !== instanceId || state.videoMode === "screenshot") {
      return;
    }
    state.videoMode = "screenshot";
    if (state.videoWatchdog) {
      clearTimeout(state.videoWatchdog);
      state.videoWatchdog = 0;
    }
    var img = $("video");
    img.onload = null;
    img.onerror = null;
    img.removeAttribute("src");
    setNote("stream-state", "单帧轮询（" + reason + "）");
    pollScreenshot(instanceId);
  }

  async function pollScreenshot(instanceId) {
    if (state.videoInstance !== instanceId || state.videoMode !== "screenshot") {
      return;
    }
    var img = $("video");
    var stage = img.parentElement;
    try {
      var response = await fetch(instancePath("/screenshot?t=" + Date.now(), instanceId), {
        cache: "no-store"
      });
      if (!response.ok) {
        stage.classList.remove("live");
        if (response.status === 409) {
          setNote("video-empty", "相机未挂载（场景未加载或实例已结束）");
        } else {
          setNote("video-empty", "截图请求失败（HTTP " + response.status + "）");
        }
        setNote("video-meta", "无画面");
      } else {
        var blob = await response.blob();
        var url = URL.createObjectURL(blob);
        img.src = url;
        stage.classList.add("live");
        if (state.videoObjectUrl) {
          URL.revokeObjectURL(state.videoObjectUrl);
        }
        state.videoObjectUrl = url;
        setNote("video-meta", "单帧 " + clock(Date.now() / 1000) + " · " + formatBytes(blob.size));
      }
    } catch (err) {
      setNote("video-meta", "截图网络异常");
    }
    if (state.videoInstance === instanceId) {
      state.videoTimer = setTimeout(function () {
        pollScreenshot(instanceId);
      }, POLL_MS.screenshot);
    }
  }

  // ── 传感器面板（各流帧 + ROS2 话题） ─────────────────────────────────────

  async function refreshSensors() {
    if (!state.instanceId) {
      return;
    }
    var target = state.instanceId;
    var payload = await fetchJSON(vizPath("/sensors"));
    if (!payload || state.instanceId !== target) {
      return;
    }
    var streams = payload.streams || {};
    var body = $("sensor-table").querySelector("tbody");
    body.textContent = "";

    var names = STREAM_ORDER.filter(function (name) {
      return streams[name];
    }).concat(
      Object.keys(streams).filter(function (name) {
        return STREAM_ORDER.indexOf(name) < 0;
      })
    );
    var total = 0;
    names.forEach(function (name) {
      var item = streams[name] || {};
      var frame = item.frame || null;
      total += item.count || 0;
      var row = el("tr");
      var stale = frame && typeof frame.age_s === "number" && frame.age_s > 2.0;
      if (stale) {
        row.className = "stale";
      }
      row.appendChild(el("td", "", name));
      row.appendChild(el("td", "num", item.count === undefined ? "—" : item.count));
      row.appendChild(el("td", "num", num(item.fps, 1)));
      row.appendChild(el("td", "num", frame ? clock(frame.ts) : "—"));
      row.appendChild(el("td", "num", frame ? num(frame.age_s, 1) + " s" : "—"));
      row.appendChild(el("td", "num", frame ? formatBytes(frame.size) : "—"));
      row.appendChild(el("td", "", frame ? describeMeta(frame.meta) : "—"));
      body.appendChild(row);
    });
    if (!names.length) {
      body.appendChild(emptyRow(7, payload.attached ? "尚未收到任何传感器帧" : "当前实例无活动运行器"));
    }
    setNote(
      "sensor-note",
      payload.attached ? "运行器已挂载 · 累计 " + total + " 帧" : "当前实例无活动运行器（未开始或已结束）"
    );

    var ros = payload.ros || {};
    var mappings = ros.mappings || [];
    var rosBody = $("ros-table").querySelector("tbody");
    rosBody.textContent = "";
    mappings.forEach(function (row) {
      var tr = el("tr");
      if (!row.published) {
        tr.className = "stale";
      }
      tr.appendChild(el("td", "", row.sim_topic));
      tr.appendChild(el("td", "", row.real_topic));
      tr.appendChild(el("td", "", row.msg_type));
      tr.appendChild(el("td", "num", num(row.frequency_hz, 1) + " Hz"));
      tr.appendChild(el("td", "num", row.published));
      rosBody.appendChild(tr);
    });
    if (!mappings.length) {
      rosBody.appendChild(emptyRow(5, "无话题映射"));
    }
    setNote("ros-note", ros.enabled ? "桥接已启用（rclpy 在线）" : "桥接未启用（rclpy 未安装或已关闭）");
  }

  // ── 预览：深度图 / 点云 / IMU ────────────────────────────────────────────

  async function refreshPreviews() {
    if (!state.instanceId) {
      return;
    }
    var target = state.instanceId;
    if (state.depthOn) {
      await refreshDepth(target);
    }
    if (state.instanceId !== target) {
      return;
    }
    await refreshLidar(target);
    if (state.instanceId !== target) {
      return;
    }
    await refreshImu(target);
  }

  async function refreshDepth(instanceId) {
    var img = $("depth-preview");
    try {
      var response = await fetch(vizPath("/sensors/depth?t=" + Date.now(), instanceId), {
        cache: "no-store"
      });
      if (state.instanceId !== instanceId) {
        return;
      }
      if (!response.ok) {
        img.classList.remove("live");
        $("depth-empty").style.display = "";
        setNote(
          "depth-empty",
          response.status === 409 ? "暂无深度帧（深度相机未挂载）" : "深度渲染不可用（需 numpy）"
        );
        return;
      }
      var blob = await response.blob();
      var url = URL.createObjectURL(blob);
      if (state.depthUrl) {
        URL.revokeObjectURL(state.depthUrl);
      }
      state.depthUrl = url;
      img.src = url;
      img.classList.add("live");
      $("depth-empty").style.display = "none";
    } catch (err) {
      setNote("depth-empty", "深度预览网络异常");
    }
  }

  async function refreshLidar(instanceId) {
    var payload = await fetchJSON(vizPath("/sensors/lidar", instanceId));
    if (!payload || state.instanceId !== instanceId) {
      return;
    }
    drawLidar(payload);
  }

  function drawLidar(payload) {
    var canvas = $("lidar-canvas");
    var context = canvas.getContext("2d");
    var width = canvas.width;
    var height = canvas.height;
    context.clearRect(0, 0, width, height);
    var points = payload.points || [];
    if (!points.length) {
      setNote("lidar-empty", payload.total ? "点云解析不可用（需 numpy）" : "无点云数据");
      return;
    }
    setNote(
      "lidar-empty",
      "共 " + payload.total + " 点 · 抽样 " + payload.sampled + "（stride=" + payload.stride + "）"
    );

    // 俯视投影：x 向前（画布向上），y 向左（画布向左）；量程取点云最大半径
    var range = 1;
    points.forEach(function (point) {
      var radius = Math.max(Math.abs(point[0] || 0), Math.abs(point[1] || 0));
      if (radius > range) {
        range = radius;
      }
    });
    var originX = width / 2;
    var originY = height - 8;
    var scale = (Math.min(width, height) / 2 - 8) / range;

    context.strokeStyle = "#1b2836";
    context.lineWidth = 1;
    var step = niceStep(range);
    for (var distance = step; distance <= range + 1e-6; distance += step) {
      context.beginPath();
      context.arc(originX, originY, distance * scale, Math.PI, 2 * Math.PI);
      context.stroke();
    }

    points.forEach(function (point) {
      var px = originX - (point[1] || 0) * scale;
      var py = originY - (point[0] || 0) * scale;
      if (px < 0 || px > width || py < 0 || py > height) {
        return;
      }
      var level = Math.max(0, Math.min(1, ((point[2] || 0) + 2) / 6));
      context.fillStyle =
        "rgba(" +
        Math.round(70 + 180 * level) +
        "," +
        Math.round(200 - 90 * level) +
        ",255,0.78)";
      context.fillRect(px, py, 1.6, 1.6);
    });

    context.fillStyle = "#3ea6ff";
    context.beginPath();
    context.arc(originX, originY, 3, 0, 2 * Math.PI);
    context.fill();
  }

  function niceStep(range) {
    var raw = range / 4;
    var power = Math.pow(10, Math.floor(Math.log10(Math.max(raw, 1e-6))));
    var scaled = raw / power;
    var factor = scaled <= 1 ? 1 : scaled <= 2 ? 2 : scaled <= 5 ? 5 : 10;
    return factor * power;
  }

  async function refreshImu(instanceId) {
    var payload = await fetchJSON(vizPath("/sensors/imu", instanceId));
    if (!payload || state.instanceId !== instanceId) {
      return;
    }
    var accel = payload.accel || [];
    var gyro = payload.gyro || [];
    if (!accel.length && !gyro.length) {
      fillKv($("imu-info"), [["数据", payload.raw || "无 IMU 帧"]]);
      return;
    }
    fillKv($("imu-info"), [
      ["加速度 x", num(accel[0], 3)],
      ["加速度 y", num(accel[1], 3)],
      ["加速度 z", num(accel[2], 3)],
      ["角速度 x", num(gyro[0], 3)],
      ["角速度 y", num(gyro[1], 3)],
      ["角速度 z", num(gyro[2], 3)]
    ]);
  }

  // ── 场景事件 ─────────────────────────────────────────────────────────────

  async function refreshEvents() {
    if (!state.instanceId) {
      return;
    }
    var target = state.instanceId;
    var payload = await fetchJSON(vizPath("/events?limit=200"));
    if (!payload || state.instanceId !== target) {
      return;
    }
    var counts = payload.counts || {};
    var summary = Object.keys(counts)
      .sort()
      .map(function (key) {
        return key + " " + counts[key];
      })
      .join(" · ");
    setNote("event-summary", "共 " + (payload.count || 0) + " 条" + (summary ? " · " + summary : ""));

    var list = $("event-list");
    list.textContent = "";
    var items = payload.items || [];
    if (!items.length) {
      list.appendChild(el("li", "event-item muted", "暂无事件（无碰撞/偏离/通过，或场景尚未运行）"));
      return;
    }
    items
      .slice()
      .reverse()
      .forEach(function (event) {
        var level = String(event.event_level || "info").toLowerCase();
        var kind = level === "warning" ? " warning" : level === "error" || level === "critical" ? " critical" : "";
        var item = el("li", "event-item" + kind);
        item.appendChild(el("span", "t", "t=" + num(event.time_stamp, 2) + "s"));
        item.appendChild(el("span", "k", String(event.event_type || "unknown")));
        item.appendChild(el("span", "d", String(event.description || "")));
        item.title = "event_id=" + String(event.event_id || "") + " · level=" + level;
        list.appendChild(item);
      });
  }

  // ── 仿真日志（SSE 增量 → 轮询降级） ─────────────────────────────────────

  function logQuery() {
    return (
      "level=" +
      encodeURIComponent($("log-level").value) +
      "&contains=" +
      encodeURIComponent($("log-filter").value.trim())
    );
  }

  function startLogs() {
    closeLogs();
    state.logMode = "sse";
    var url = "/viz/api/logs/stream?" + logQuery() + "&history=200";
    try {
      var source = new EventSource(url);
      state.logSource = source;
      source.onmessage = function (event) {
        if (state.logPaused) {
          return;
        }
        var record = null;
        try {
          record = JSON.parse(event.data);
        } catch (err) {
          return;
        }
        appendLog(record);
      };
      source.onerror = function () {
        degradeLogs("SSE 连接中断，已切换为轮询（每 1.5s）");
      };
    } catch (err) {
      degradeLogs("浏览器不支持 SSE，已切换为轮询（每 1.5s）");
    }
  }

  function closeLogs() {
    if (state.logSource) {
      state.logSource.close();
      state.logSource = null;
    }
    if (state.logPoller) {
      clearTimeout(state.logPoller);
      state.logPoller = 0;
    }
    state.logMode = "";
  }

  function degradeLogs(note) {
    if (state.logMode === "poll") {
      return;
    }
    if (state.logSource) {
      state.logSource.close();
      state.logSource = null;
    }
    state.logMode = "poll";
    appendLog({ seq: 0, ts: Date.now() / 1000, level: "WARNING", logger: "viz", message: note, thread: "" });
    pollLogs();
  }

  async function pollLogs() {
    if (state.logMode !== "poll") {
      return;
    }
    var payload = await fetchJSON("/viz/api/logs?limit=300&" + logQuery() + "&since_seq=" + state.logSeq);
    if (payload && !state.logPaused) {
      (payload.items || []).forEach(appendLog);
    }
    if (state.logMode === "poll") {
      state.logPoller = setTimeout(pollLogs, POLL_MS.logFallback);
    }
  }

  function appendLog(record) {
    if (!record) {
      return;
    }
    var consoleEl = $("log-console");
    var line = el("span", "log-line " + String(record.level || "INFO"));
    line.appendChild(el("span", "ts", clock(record.ts) + " "));
    line.appendChild(el("span", "lv", String(record.level || "?")));
    line.appendChild(el("span", "lg", String(record.logger || "-")));
    line.appendChild(el("span", "msg", String(record.message || "") + "\n"));
    consoleEl.appendChild(line);
    while (consoleEl.childElementCount > LOG_MAX_LINES) {
      consoleEl.removeChild(consoleEl.firstChild);
    }
    if (typeof record.seq === "number" && record.seq > state.logSeq) {
      state.logSeq = record.seq;
    }
    if ($("log-autoscroll").checked) {
      consoleEl.scrollTop = consoleEl.scrollHeight;
    }
  }

  function resetLogs() {
    $("log-console").textContent = "";
    state.logSeq = 0;
    startLogs();
  }

  // ── 事件绑定与启动 ───────────────────────────────────────────────────────

  function bindEvents() {
    $("instance-select").addEventListener("change", function (event) {
      selectInstance(event.target.value);
    });
    $("btn-pause").addEventListener("click", function () {
      postControl("pause");
    });
    $("btn-resume").addEventListener("click", function () {
      postControl("resume");
    });
    $("btn-stop").addEventListener("click", function () {
      postControl("stop");
    });
    $("toggle-depth").addEventListener("change", function (event) {
      state.depthOn = Boolean(event.target.checked);
      if (!state.depthOn) {
        $("depth-preview").classList.remove("live");
        $("depth-empty").style.display = "";
        setNote("depth-empty", "未开启（勾选上方「深度图预览」）");
        return;
      }
      $("depth-empty").style.display = "";
      setNote("depth-empty", "等待深度帧…");
      refreshPreviews();
    });
    $("log-level").addEventListener("change", resetLogs);
    $("log-filter").addEventListener(
      "input",
      debounce(function () {
        resetLogs();
      }, 400)
    );
    $("log-pause").addEventListener("click", function () {
      state.logPaused = !state.logPaused;
      setNote("log-pause", state.logPaused ? "继续" : "暂停");
      if (!state.logPaused) {
        resetLogs();
      }
    });
    $("log-clear").addEventListener("click", function () {
      $("log-console").textContent = "";
      if (state.logMode !== "poll") {
        resetLogs();
      }
    });
  }

  function bootstrap() {
    bindEvents();
    updateControls();
    refreshStatus();
    refreshInstances().then(refreshDetail);
    startLogs();
    setInterval(refreshStatus, POLL_MS.status);
    setInterval(refreshInstances, POLL_MS.instances);
    setInterval(refreshDetail, POLL_MS.detail);
    setInterval(refreshSensors, POLL_MS.sensors);
    setInterval(refreshEvents, POLL_MS.events);
    setInterval(refreshPreviews, POLL_MS.preview);
  }

  document.addEventListener("DOMContentLoaded", bootstrap);
})();
