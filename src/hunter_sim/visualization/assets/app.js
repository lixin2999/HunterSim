/*
 * HunterSim 仿真可视化客户端（纯浏览器脚本，无构建步骤）。
 *
 * 数据来源（全部同源，无跨域）：
 *   /viz/api/status                          顶栏状态灯 + 仿真端模块连通性
 *   /viz/api/instances/{id}/events          场景事件（碰撞/偏离/通过…）
 *   /viz/api/instances/{id}/actors          交通参与者（类型/平台行为标签/车速/相对自车方位）
 *   /viz/api/instances/{id}/sensors         传感器各流帧数/帧率 + ROS2 话题
 *   /viz/api/instances/{id}/sensors/{stream} 单流最新帧（深度 PNG / 点云 JSON / IMU JSON）
 *   /viz/api/logs、/viz/api/logs/stream     仿真日志（SSE 增量，断线降级为轮询）
 *   /api/v1/instances[/{id}]                实例列表/进度（平台既有契约）
 *   /api/v1/instances/{id}/stream.mjpg      实时画面（MJPEG，失败降级单帧轮询并定期回探）
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
    actors: 1000,
    preview: 1500,
    screenshot: 700,
    logFallback: 1500
  };
  var LOG_MAX_LINES = 1500;
  var VIDEO_WATCHDOG_MS = 6000;
  // 控制面不可用退避窗口（V3.5.6，毫秒）：控制面崩溃/重启的十几秒里，本页有
  // 8 个独立轮询在反复发请求，代理端逐个回 502/503，日志里就是一片“502 报警”。
  // 任一请求读到 502/503/504 就把所有只读轮询暂停一个窗口（默认按 Retry-After），
  // 窗口到点只放行一次探测；服务恢复后自动回到正常节奏。
  var UPSTREAM_BACKOFF_MS = 2000;
  // 降级后的回探节奏（V3.5.5）：旧版一旦落到单帧轮询就永不回头，画面长期停在
  // ~1.4 fps。现在先做不干扰画面的响应头探测，探到可用立即回到 MJPEG 流。
  var MJPEG_REPROBE_BASE_MS = 10000;
  var MJPEG_REPROBE_MAX_MS = 80000;
  var TERMINAL = ["succeeded", "failed", "canceled"];
  var STREAM_ORDER = ["overview", "rgb", "depth", "lidar", "imu"];

  var state = {
    instanceId: "",
    instanceStatus: "",
    instanceMessage: "",
    carlaStatus: "",
    // 终态实例是否已做过一次收尾加载（传感器/事件面板需要落最终值，之后轮询即停）
    terminalSynced: false,
    videoInstance: "",
    videoMode: "",
    videoTimer: 0,
    videoWatchdog: 0,
    videoObjectUrl: "",
    videoReprobe: 0,
    mjpegFailures: 0,
    depthOn: true,
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

  // V3.8.18：仿真经过秒（非 Unix epoch）格式化为 hh:mm:ss，供传感器表「仿真时刻」列。
  function simClock(seconds) {
    if (typeof seconds !== "number" || !isFinite(seconds) || seconds < 0) {
      return "—";
    }
    var total = Math.floor(seconds);
    var hours = Math.floor(total / 3600);
    var minutes = Math.floor((total % 3600) / 60);
    var rest = total % 60;
    var prefix = hours > 0 ? pad(hours) + ":" : "";
    return prefix + pad(minutes) + ":" + pad(rest);
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
      target.appendChild(el("dt", row[2] ? "kv-label " + row[2] : "", row[0]));
      var value = row[1];
      var empty = value === undefined || value === null || value === "";
      target.appendChild(el("dd", row[2] || "", empty ? "—" : value));
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
    if (meta.quality) {
      // 自适应降质后能直接从页面读出当前档（V3.5.5）
      parts.push("q" + meta.quality);
    }
    if (meta.points) {
      parts.push(meta.points + " 点");
    }
    if (meta.encoding) {
      parts.push(String(meta.encoding));
    }
    return parts.length ? parts.join(" · ") : "—";
  }

  // ── 控制面不可用退避（只读轮询统一闸门） ────────────────────────────

  var upstreamBackoffUntil = 0;

  function isUpstreamDown(response) {
    if (!response) {
      return false;
    }
    return response.status === 502 || response.status === 503 || response.status === 504;
  }

  function retryAfterMs(response) {
    var raw = response && response.headers ? response.headers.get("Retry-After") : null;
    var seconds = raw === null ? NaN : parseFloat(raw);
    return isFinite(seconds) && seconds > 0 ? seconds * 1000 : UPSTREAM_BACKOFF_MS;
  }

  function noteUpstreamUnavailable(response) {
    upstreamBackoffUntil = Date.now() + Math.max(500, retryAfterMs(response));
  }

  function clearUpstreamBackoff() {
    upstreamBackoffUntil = 0;
  }

  function inUpstreamBackoff() {
    return Date.now() < upstreamBackoffUntil;
  }

  function gate(task) {
    return function () {
      if (inUpstreamBackoff()) {
        return;
      }
      task();
    };
  }

  async function fetchJSON(url, options) {
    if (inUpstreamBackoff()) {
      return null;
    }
    try {
      var response = await fetch(url, Object.assign({ cache: "no-store" }, options || {}));
      if (isUpstreamDown(response)) {
        noteUpstreamUnavailable(response);
        return null;
      }
      if (!response.ok) {
        return null;
      }
      clearUpstreamBackoff();
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
    var edge = payload.edge_link || {};
    var sensor = payload.sensor || {};
    var log = payload.log || {};
    var instances = payload.instances || {};
    var stream = payload.stream || {};
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
    setBadge("badge-ros", ros.enabled ? "ROS2 已启用" : ros.reason === "unavailable" ? "ROS2 不可用" : "ROS2 未启用", ros.enabled ? "ok" : "");
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
      // 推流名额一眼可读：clients 贴近 limit 就是“MJPEG 不可用”的直接成因（V3.5.5）
      [
        "推流客户端",
        stream.clients === undefined
          ? "—"
          : stream.clients + " / " + (stream.limit > 0 ? stream.limit : "不限") +
            "（" + num(stream.fps, 0) + " fps）"
      ],
      ["ROS 话题", ros.enabled ? published.length + " 路有数据" : "未启用"],
      ["SIL 链路", edge.enabled ? "EdgeLink 已装配" : "未装配（脚本自车）"],
      ["日志缓冲", (log.records || 0) + " / " + (log.capacity || 0)]
    ]);
    renderEdgeLink(edge, carla, platform, active);
  }

  // ── SIL 链路卡片（V3.9.0：仿真端 → 车端 HunterEdge → 平台三方互联）──────
  //
  // 数据源是 /viz/api/status 的 edge_link 段（完整诊断，非平台契约的精简段）。
  // 未装配时后端只给 enabled/control_source，卡片必须明示「脚本自车」，
  // 避免把模拟 SIL 误读成真闭环。

  // 上行流展示顺序，与后端 _STREAM_FPS_FIELDS 一致
  var EDGE_STREAM_ORDER = ["rgb", "depth", "lidar", "imu", "ego_state"];

  function hzText(value) {
    return typeof value === "number" && isFinite(value) ? num(value, 1) + " Hz" : "—";
  }

  function edgeBadgeText(edge) {
    if (!edge.enabled) {
      return ["SIL 未装配", ""];
    }
    if (edge.watchdog_tripped) {
      return ["SIL 断流刹停", "err"];
    }
    if (edge.waiting_first_control) {
      return ["SIL 等待车端", "warn"];
    }
    if (typeof edge.control_hz === "number" && edge.control_hz > 0) {
      return ["SIL 闭环中", "ok"];
    }
    return ["SIL 已装配", "warn"];
  }

  function renderEdgeLink(edge, carla, platform, active) {
    var enabled = edge.enabled === true;
    var badge = edgeBadgeText(edge);
    setBadge("badge-edge", badge[0], badge[1]);

    var tripped = edge.watchdog_tripped === true;
    var watchdogMs = typeof edge.watchdog_ms === "number" ? edge.watchdog_ms : 500;
    var ageMs = edge.control_age_ms;
    var ageKnown = typeof ageMs === "number" && isFinite(ageMs);
    // boot 帧两种口径兼容：viz 完整 stats 用 boot_sent，平台精简段用 session_booted
    var booted = edge.boot_sent !== undefined ? edge.boot_sent : edge.session_booted;
    var received = typeof edge.control_received === "number" ? edge.control_received : null;

    var simText = carla.connected === true
      ? "CARLA 已连接"
      : carla.connected === false ? "CARLA 未连接" : "CARLA 未知";

    var edgeText;
    var edgeKind;
    if (!enabled) {
      edgeText = "未接入（当前为脚本自车）";
      edgeKind = "warn";
    } else if (tripped) {
      edgeText = "控制断流 → 零速刹停保持";
      edgeKind = "err";
    } else if (edge.waiting_first_control) {
      edgeText = booted ? "会话已建立，等待首帧 /cmd_vel" : "等待会话 boot 帧";
      edgeKind = "warn";
    } else if (received === 0) {
      edgeText = "尚未收到控制帧";
      edgeKind = "warn";
    } else {
      edgeText = "已回传 " + (received === null ? hzText(edge.control_hz) : received + " 帧");
      edgeKind = "";
    }

    var platText = platform.enabled ? "网关已接入" : "平台未启用";
    if (active) {
      platText += " · 实例 " + (active.status || "?");
    }

    fillKv($("edge-info"), [
      ["仿真端", simText, carla.connected === true ? "" : "err"],
      ["车端", edgeText, edgeKind],
      ["平台", platText, platform.enabled ? "" : "warn"],
      ["控制源", enabled ? "EdgeLink（车端接管）" : "脚本自车", enabled ? "" : "warn"],
      ["上行 Topic", edge.sensor_topic || "—"],
      ["下行 Topic", edge.control_topic || "—"],
      ["看门狗", tripped ? "已触发（零速刹停）" : "正常 · 阈值 " + watchdogMs + " ms", tripped ? "err" : ""],
      [
        "控制延迟",
        ageKnown ? num(ageMs, 0) + " ms" : "尚未收到控制帧",
        ageKnown && ageMs > watchdogMs ? "err" : ""
      ],
      ["控制频率", hzText(edge.control_hz)],
      [
        "最新指令",
        "v " + num(edge.last_linear_x, 2) + " m/s · ω " + num(edge.last_angular_z, 2) + " rad/s"
      ],
      [
        "上行错误",
        edge.send_errors === undefined ? "—" : String(edge.send_errors),
        edge.send_errors > 0 ? "warn" : ""
      ],
      ["链路时长", enabled ? humanDuration(edge.uptime_s) : "—"]
    ]);

    var body = $("edge-table").querySelector("tbody");
    body.textContent = "";
    if (!enabled) {
      body.appendChild(emptyRow(6, "未装配 EdgeLink（脚本自车，无 Kafka 上行）"));
      setNote("edge-note", "未装配 EdgeLink：当前为脚本自车，非真 SIL 闭环");
      return;
    }

    var hz = edge.sensor_hz || {};
    var nominal = edge.sensor_nominal_fps || {};
    var sent = edge.sensor_sent || {};
    var rateDropped = edge.sensor_rate_dropped || {};
    var sizeDropped = edge.sensor_size_dropped || {};
    var names = EDGE_STREAM_ORDER.filter(function (name) {
      return hz[name] !== undefined || nominal[name] !== undefined || sent[name] !== undefined;
    });
    // 信封里出现的新流也要上表，不能因为不在预设顺序里就漏显
    Object.keys(hz).concat(Object.keys(nominal), Object.keys(sent)).forEach(function (name) {
      if (names.indexOf(name) < 0) {
        names.push(name);
      }
    });
    names.forEach(function (name) {
      var actual = hz[name];
      var expect = nominal[name];
      var count = sent[name] || 0;
      var tr = el("tr");
      // 断流（曾发送但窗口内为 0）或明显低于标称（<80%）标黄：
      // 这正是「馈送速率失真」的直接判据，比只看累计帧数更早暴露问题。
      var stalled = count > 0 && !(typeof actual === "number" && actual > 0);
      var degraded = typeof expect === "number" && expect > 0 &&
        typeof actual === "number" && actual < expect * 0.8;
      if (stalled || degraded) {
        tr.className = "stale";
        tr.title = stalled ? "该流已断流（窗口内无上行）" : "实际速率低于标称 80%，检查带宽/限频丢弃";
      }
      tr.appendChild(el("td", "", name));
      tr.appendChild(el("td", "num", hzText(actual)));
      tr.appendChild(el("td", "num", typeof expect === "number" && expect > 0 ? num(expect, 1) : "不限"));
      tr.appendChild(el("td", "num", count));
      tr.appendChild(el("td", "num", rateDropped[name] || 0));
      tr.appendChild(el("td", "num", sizeDropped[name] || 0));
      body.appendChild(tr);
    });
    if (!names.length) {
      body.appendChild(emptyRow(6, "尚未上行任何传感器帧"));
    }
    setNote(
      "edge-note",
      (booted ? "会话已建立" : "会话未建立") +
        (edge.sim_id ? " · sim_id " + edge.sim_id : "") +
        " · 已运行 " + humanDuration(edge.uptime_s)
    );
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
    state.instanceMessage = record.message || "";
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

  // 终态实例（succeeded/failed/canceled）不会再产生新帧：帧类轮询必须短路，
  // 否则 /sensors/lidar 、/sensors/imu 会持续返回无内容并刷满服务端日志
  function isTerminalInstance() {
    return TERMINAL.indexOf(state.instanceStatus) >= 0;
  }

  // 只有 running 的实例才可能有传感器帧：pending（场景加载中）不启动拉流
  // 与预览轮询，避免启动前的无效请求（平台状态 pending → running）
  function isRunningInstance() {
    return state.instanceStatus === "running";
  }

  function selectInstance(instanceId) {
    if (instanceId === state.instanceId) {
      return;
    }
    state.instanceId = instanceId || "";
    state.instanceStatus = "";
    state.instanceMessage = "";
    state.carlaStatus = "";
    state.terminalSynced = false;
    var select = $("instance-select");
    if (state.instanceId) {
      select.value = state.instanceId;
    }
    $("event-list").textContent = "";
    setNote("event-summary", "—");
    $("sensor-table").querySelector("tbody").textContent = "";
    $("ros-table").querySelector("tbody").textContent = "";
    $("edge-table").querySelector("tbody").textContent = "";
    $("actor-table").querySelector("tbody").textContent = "";
    setNote("actor-summary", "—");
    fillKv($("instance-info"), []);
    fillKv($("ego-info"), []);
    fillKv($("imu-info"), []);
    updateControls();
    if (state.instanceId) {
      // 状态未知时先不拉流：等 refreshDetail 拿到 running 后再启动
      refreshDetail();
      refreshSensors();
      refreshEvents();
      refreshActors();
      setNote("video-empty", "等待实例启动…");
      setNote("stream-state", "未开始推流");
      setNote("video-meta", "等待运行");
    } else {
      stopVideo();
      setNote("sensor-note", "未选择实例");
      setNote("ros-note", "—");
      setNote("edge-note", "—");
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
        // 用户动作不进闸门（不能把“暂停”按钮静默掉），但上游不可用仍需退避，
        // 避免后面几十个轮询继续撞已断开的端口
        if (isUpstreamDown(response)) {
          noteUpstreamUnavailable(response);
        }
        setNote("stream-state", "控制失败：" + action + " → HTTP " + response.status);
      } else {
        clearUpstreamBackoff();
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
    // 平台状态 pending → running 时才启动拉流；running → 终态由下方停流
    if (isRunningInstance()) {
      if (!state.videoMode) {
        startVideo(state.instanceId);
      }
    } else if (!isTerminalInstance() && state.videoMode) {
      stopVideo();
      setNote("video-empty", "等待实例启动…");
      setNote("stream-state", "未开始推流");
      setNote("video-meta", "等待运行");
    }
    var progress =
      typeof payload.progress_percent === "number" ? payload.progress_percent.toFixed(1) + "%" : "—";
    // 失败/取消时必须把原因写在页面上：平台侧也是靠 message 判断故障类型
    var statusText = (payload.status || "?") + " / " + (payload.carla_status || "?");
    var statusKind = payload.status === "failed" ? "err" : payload.status === "canceled" ? "warn" : "";
    var message = String(payload.message || "").trim();
    var messageKind = payload.status === "failed" ? "err" : "warn";
    var sceneLabel = payload.scene_name
      ? payload.scene_name + "（" + (payload.scene_id || "—") + "）"
      : payload.scene_id || "—";
    var rows = [
      ["实例 ID", payload.instance_id],
      ["场景", sceneLabel],
      ["模式", payload.mode],
      ["车辆", payload.vehicle_id],
      ["平台状态", statusText, statusKind],
    ];
    // V3.8.18：评估结论与平台状态分行展示（succeeded ≠ 评估通过）。
    var evaluation = payload.evaluation || null;
    if (evaluation) {
      var evalText = evaluation.passed ? "通过" : "未通过";
      if (evaluation.grade) {
        evalText += "（等级 " + evaluation.grade + "）";
      }
      rows.push(["评估结论", evalText, evaluation.passed ? "" : "err"]);
      var criteria = evaluation.success_criteria || {};
      if (criteria.max_speed_deviation !== undefined) {
        var devText = num(criteria.max_speed_deviation, 2) + " m/s";
        if (criteria.max_speed_deviation_limit !== undefined && criteria.max_speed_deviation_limit !== null) {
          devText += " / 上限 " + num(criteria.max_speed_deviation_limit, 2);
        }
        devText += criteria.max_speed_deviation_ok ? " ✓" : " ✗";
        rows.push(["速度偏差", devText, criteria.max_speed_deviation_ok ? "" : "err"]);
      }
      if (criteria.min_safe_distance !== undefined && criteria.min_safe_distance !== null) {
        var distText = num(criteria.min_safe_distance, 2) + " m";
        if (criteria.min_safe_distance_required !== undefined && criteria.min_safe_distance_required !== null) {
          distText += " / 要求 ≥ " + num(criteria.min_safe_distance_required, 2);
        }
        distText += criteria.min_safe_distance_ok ? " ✓" : " ✗";
        rows.push(["最小安全距离", distText, criteria.min_safe_distance_ok ? "" : "err"]);
      }
      if (criteria.no_collision !== undefined) {
        rows.push(["无碰撞", criteria.no_collision ? "是 ✓" : "否 ✗", criteria.no_collision ? "" : "err"]);
      }
    } else if (isTerminalInstance()) {
      rows.push(["评估结论", "无评估结果", "warn"]);
    }
    if (message) {
      rows.push(["原因", message, messageKind]);
    }
    // V3.8.18：运行期录制态可见（旧版运行中一直显「未录制」，终态才出现文件大小）。
    var recordingText;
    if (payload.recording_active) {
      recordingText = "录制中 (" + formatBytes(payload.recording_bytes) + ")";
    } else if (payload.recording_path) {
      recordingText = formatBytes(payload.recording_bytes);
    } else {
      recordingText = "未录制";
    }
    rows.push(
      ["进度", progress],
      ["仿真时间", num(payload.current_time_s, 1) + " / " + num(payload.total_time_s, 1) + " s"],
      ["参与者", num(payload.actors_count, 0)],
      ["碰撞", num(payload.collisions, 0)],
      ["车道侵入", num(payload.lane_invasions, 0)],
      ["录制", recordingText],
      ["更新时间", payload.updated_at]
    );
    fillKv($("instance-info"), rows);

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

    // 实例刚进入终态：停掉拉流/截图轮询，只保留原因提示
    if (TERMINAL.indexOf(payload.status || "") >= 0 && state.videoMode) {
      stopVideo();
      setNote("video-empty", "实例已结束（" + payload.status + "）" + (message ? "：" + message : ""));
      setNote("stream-state", "无实时画面（实例已结束）");
      setNote("video-meta", "未推流");
    }
    // 终态后补一次收尾加载：传感器帧数/事件列表落到最终值，此后定时轮询由 isTerminalInstance 短路
    if (TERMINAL.indexOf(payload.status || "") >= 0 && !state.terminalSynced) {
      state.terminalSynced = true;
      refreshSensors();
      refreshEvents();
    }
  }

  // ── 实时画面（MJPEG → 单帧轮询降级） ─────────────────────────────────────

  function startVideo(instanceId) {
    stopVideo();
    state.videoInstance = instanceId;
    state.videoMode = "mjpeg";
    var img = $("video");
    var stage = img.parentElement;
    stage.classList.remove("live");
    if (TERMINAL.indexOf(state.instanceStatus) >= 0) {
      // 终态实例不会再有帧：直接说明原因，避免陷入轮询循环
      state.videoMode = "";
      var reason = state.instanceMessage ? "：" + state.instanceMessage : "";
      setNote("video-empty", "实例已结束（" + state.instanceStatus + "）" + reason);
      setNote("stream-state", "无实时画面（实例已结束）");
      setNote("video-meta", "未推流");
      return;
    }
    if (!isRunningInstance()) {
      // 场景加载中：不建立 MJPEG 连接（首帧未就绪时服务端只会回 204）
      state.videoMode = "";
      setNote("video-empty", "等待实例启动…");
      setNote("stream-state", "未开始推流");
      setNote("video-meta", "等待运行");
      return;
    }
    setNote("video-empty", "连接图像流…");
    setNote("video-meta", "MJPEG 连接中");
    setNote("stream-state", "图像流（MJPEG）");

    img.onload = function () {
      if (state.videoInstance !== instanceId) {
        return;
      }
      stage.classList.add("live");
      // 首帧真正到手才算成功：复位回探退避，并停掉可能在跑的单帧轮询
      state.mjpegFailures = 0;
      clearReprobe();
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
    img.src = instancePath("/stream.mjpg?stream=overview&t=" + Date.now(), instanceId);

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
    clearReprobe();
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
    state.mjpegFailures += 1;
    if (state.videoWatchdog) {
      clearTimeout(state.videoWatchdog);
      state.videoWatchdog = 0;
    }
    var img = $("video");
    img.onload = null;
    img.onerror = null;
    img.removeAttribute("src");
    setNote("stream-state", "单帧轮询（" + reason + "）· 稍后自动重试图像流");
    pollScreenshot(instanceId);
    scheduleMjpegReprobe(instanceId);
  }

  function clearReprobe() {
    if (state.videoReprobe) {
      clearTimeout(state.videoReprobe);
      state.videoReprobe = 0;
    }
  }

  function reprobeDelayMs() {
    var step = Math.max(0, state.mjpegFailures - 1);
    return Math.min(MJPEG_REPROBE_BASE_MS * Math.pow(2, step), MJPEG_REPROBE_MAX_MS);
  }

  function scheduleMjpegReprobe(instanceId) {
    clearReprobe();
    var delay = reprobeDelayMs();
    state.videoReprobe = window.setTimeout(function () {
      state.videoReprobe = 0;
      if (state.videoInstance !== instanceId || state.videoMode !== "screenshot") {
        return;
      }
      if (isTerminalInstance() || !isRunningInstance()) {
        return;
      }
      reprobeMjpeg(instanceId);
    }, delay);
  }

  // 只读响应头、拿到即断：不断画面当前的单帧轮询，也不长占推流名额
  // （服务端在首帧异常路径会立即归还名额，V3.5.5）
  async function reprobeMjpeg(instanceId) {
    var controller = window.AbortController ? new AbortController() : null;
    var usable = false;
    try {
      var response = await fetch(
        instancePath("/stream.mjpg?stream=overview&t=" + Date.now(), instanceId),
        { cache: "no-store", signal: controller ? controller.signal : undefined }
      );
      if (isUpstreamDown(response)) {
        noteUpstreamUnavailable(response);
      }
      var contentType = String(response.headers.get("content-type") || "");
      usable = response.ok && contentType.indexOf("multipart") >= 0;
    } catch (err) {
      usable = false;
    }
    if (controller) {
      try {
        controller.abort();
      } catch (err) {
        // 已结束的请求再 abort 会抛错，忽略即可
      }
    }
    if (state.videoInstance !== instanceId || state.videoMode !== "screenshot") {
      return;
    }
    if (usable) {
      setNote("stream-state", "图像流已可用，切回 MJPEG");
      startVideo(instanceId);
      return;
    }
    scheduleMjpegReprobe(instanceId);
  }

  async function pollScreenshot(instanceId) {
    if (state.videoInstance !== instanceId || state.videoMode !== "screenshot") {
      return;
    }
    if (inUpstreamBackoff()) {
      // 退避窗口内不发请求，但要续排下一轮（否则控制面恢复后画面永久停住）
      state.videoTimer = setTimeout(function () {
        pollScreenshot(instanceId);
      }, POLL_MS.screenshot);
      return;
    }
    var img = $("video");
    var stage = img.parentElement;
    try {
      var response = await fetch(
        instancePath("/screenshot?stream=overview&t=" + Date.now(), instanceId),
        {
          cache: "no-store"
        }
      );
      if (response.status === 204) {
        // 场景已 running 但首帧还没到：保持轮询并提示等待
        stage.classList.remove("live");
        setNote("video-empty", "等待首帧…");
        setNote("video-meta", "无画面");
      } else if (!response.ok) {
        stage.classList.remove("live");
        if (isUpstreamDown(response)) {
          noteUpstreamUnavailable(response);
          setNote("video-empty", "控制面不可用，已暂停轮询（自动恢复）");
        } else if (response.status === 409) {
          setNote("video-empty", "相机未挂载（场景未加载或实例已结束）");
        } else {
          setNote("video-empty", "截图请求失败（HTTP " + response.status + "）");
        }
        setNote("video-meta", "无画面");
      } else {
        clearUpstreamBackoff();
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
      // V3.8.18：帧率列展示「实际 / 标称」，丢帧数附后（定位 fps 低于标称的成因）。
      var fpsText = num(item.fps, 1);
      if (typeof item.nominal_fps === "number" && item.nominal_fps > 0) {
        fpsText += " / " + num(item.nominal_fps, 0);
      }
      if (item.dropped_frames > 0) {
        fpsText += "（丢 " + item.dropped_frames + "）";
      }
      row.appendChild(el("td", "num", fpsText));
      // V3.8.18：时钟列用墙钟 wall_ts（旧版用仿真秒 frame.ts 渲染出 1970 年）；
      // 仿真时刻单独一列（仿真经过秒，格式化为 mm:ss）。
      row.appendChild(el("td", "num", frame ? clock(frame.wall_ts || frame.ts) : "—"));
      row.appendChild(el("td", "num", frame ? simClock(frame.sim_ts !== undefined ? frame.sim_ts : frame.ts) : "—"));
      row.appendChild(el("td", "num", frame ? num(frame.age_s, 1) + " s" : "—"));
      row.appendChild(el("td", "num", frame ? formatBytes(frame.size) : "—"));
      row.appendChild(el("td", "", frame ? describeMeta(frame.meta) : "—"));
      body.appendChild(row);
    });
    if (!names.length) {
      body.appendChild(emptyRow(8, payload.attached ? "尚未收到任何传感器帧" : "当前实例无活动运行器"));
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
    // V3.8.18：ROS2 状态文案区分未启用/不可用/已启用（旧版笼统显示「桥接未启用」）。
    var rosNote;
    if (ros.enabled) {
      rosNote = "桥接已启用（rclpy 在线）";
    } else if (ros.reason === "unavailable") {
      rosNote = "桥接不可用（rclpy 未安装或初始化失败；真 SIL 走 Kafka 链路无需 rclpy）";
    } else {
      rosNote = "桥接未启用（sensors[].ros_enabled=false，默认关闭）";
    }
    setNote("ros-note", rosNote);
  }

  // ── 预览：深度图 / 点云 / IMU ────────────────────────────────────────────

  async function refreshPreviews() {
    if (!state.instanceId || isTerminalInstance() || !isRunningInstance()) {
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
      if (response.status === 204) {
        img.classList.remove("live");
        $("depth-empty").style.display = "";
        setNote("depth-empty", "等待深度帧…");
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

  // ── 交通参与者（场内到底有谁 / 期望行为 / 在自车哪个方位） ────────────────

  function relativeText(ahead, left) {
    if (typeof ahead !== "number" || !isFinite(ahead)) {
      return "—";
    }
    var side = typeof left === "number" && isFinite(left) ? left : 0;
    return (
      (ahead >= 0 ? "前 " : "后 ") +
      num(Math.abs(ahead), 1) +
      " / " +
      (side >= 0 ? "左 " : "右 ") +
      num(Math.abs(side), 1)
    );
  }

  async function refreshActors() {
    if (!state.instanceId) {
      return;
    }
    var target = state.instanceId;
    var payload = await fetchJSON(vizPath("/actors"));
    if (!payload || state.instanceId !== target) {
      return;
    }
    var items = payload.items || [];
    var body = $("actor-table").querySelector("tbody");
    body.textContent = "";
    items.forEach(function (item) {
      var row = el("tr");
      // 视区外的行标黄：一眼定位「车生成了但画面里看不到」
      if (!item.visible) {
        row.className = "stale";
      }
      row.appendChild(el("td", "", item.id || "—"));
      row.appendChild(el("td", "", item.actor_type || "—"));
      // 行为标签回显平台原值（如 sudden_cross）；旧载荷无该字段时退回内部行为
      row.appendChild(el("td", "", item.source_behavior_type || item.behavior || "—"));
      row.appendChild(el("td", "num", item.spawned ? num(item.speed_ms, 1) + " m/s" : "未生成"));
      row.appendChild(el("td", "num", relativeText(item.ahead_m, item.left_m)));
      row.appendChild(el("td", "num", num(item.distance_to_ego_m, 1) + " m"));
      row.appendChild(el("td", "", item.visible ? "在视区内" : "视区外"));
      row.title =
        "world=(" +
        num(item.x, 1) +
        ", " +
        num(item.y, 1) +
        ") yaw=" +
        num(item.yaw_deg, 1) +
        "° · blueprint=" +
        String(item.blueprint || "—") +
        " · 内部行为=" +
        String(item.behavior || "—");
      body.appendChild(row);
    });
    if (!items.length) {
      body.appendChild(
        emptyRow(7, "当前场景未声明交通参与者，或场景尚未开始 tick")
      );
    }
    var count = payload.count || 0;
    var visible = payload.visible || 0;
    setNote(
      "actor-summary",
      "声明 " + count + " · 已生成 " + (payload.spawned || 0) + " · 主视区 " + visible
    );
    if (count > 0 && visible === 0) {
      // 这是「主车前面看不到其他车」最直接的页面提示
      setNote(
        "actor-note",
        "有参与者但全部落在环视主视区外：画面里只能看到自车。请检查平台下发的相对偏移（x 前向 / y 左向，米）。"
      );
    } else {
      setNote(
        "actor-note",
        "环视相机挂在自车后缘、朝前 100° 视场；“视区外”的参与者画面里看不到。"
      );
    }
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
    // 深度图预览默认勾选：以 DOM 勾选态为准，避免刷新后状态与复选框不一致
    var depthToggle = $("toggle-depth");
    if (depthToggle) {
      state.depthOn = Boolean(depthToggle.checked);
      if (state.depthOn) {
        setNote("depth-empty", "等待深度帧…");
      }
    }
    updateControls();
    refreshStatus();
    refreshInstances().then(refreshDetail);
    startLogs();
    setInterval(gate(refreshStatus), POLL_MS.status);
    setInterval(gate(refreshInstances), POLL_MS.instances);
    setInterval(gate(refreshDetail), POLL_MS.detail);
    setInterval(gate(function () {
      if (!isTerminalInstance()) {
        refreshSensors();
      }
    }), POLL_MS.sensors);
    setInterval(gate(function () {
      if (!isTerminalInstance()) {
        refreshEvents();
      }
    }), POLL_MS.events);
    setInterval(gate(refreshActors), POLL_MS.actors);
    setInterval(gate(function () {
      if (!isTerminalInstance() && isRunningInstance()) {
        refreshPreviews();
      }
    }), POLL_MS.preview);
  }

  document.addEventListener("DOMContentLoaded", bootstrap);
})();
