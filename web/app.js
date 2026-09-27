const $ = (id) => document.getElementById(id);

const SEVERITY_LABEL = { minor: "一般", major: "严重", critical: "紧急" };
const STATUS_LABEL = { active: "待处理", acknowledged: "已确认", resolved: "已销警" };

async function api(path, options) {
  const response = await fetch(path, options);
  return response.json();
}

async function post(path, body) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}

function renderCards(entries) {
  const cards = $("overview-cards");
  cards.innerHTML = "";
  for (const [label, value, tone] of entries) {
    const card = document.createElement("div");
    card.className = "card" + (tone ? " " + tone : "");
    const labelNode = document.createElement("div");
    labelNode.className = "label";
    labelNode.textContent = label;
    const valueNode = document.createElement("div");
    valueNode.className = "value";
    valueNode.textContent = String(value);
    card.appendChild(labelNode);
    card.appendChild(valueNode);
    cards.appendChild(card);
  }
}

function setOnline(online) {
  const node = $("conn-status");
  node.textContent = online ? "已连接" : "连接中断";
  node.classList.toggle("offline", !online);
}

function formatTime(iso) {
  if (!iso) return "—";
  return iso.replace("T", " ").slice(0, 19) + " UTC";
}

async function renderAlarms() {
  const status = $("alarm-filter-status").value;
  const path = "/api/alarms?limit=100" + (status ? "&status=" + status : "");
  const response = await api(path);
  const data = response.data || {};
  const incidents = data.incidents || [];
  const list = $("alarm-list");
  list.innerHTML = "";
  if (incidents.length === 0) {
    const empty = document.createElement("div");
    empty.className = "alarm-empty";
    empty.textContent = "暂无报警记录";
    list.appendChild(empty);
  }
  for (const incident of incidents) {
    list.appendChild(renderAlarmItem(incident));
  }
  const counts = data.by_severity || {};
  $("alarm-summary").textContent =
    "未关闭 " + (data.open || 0) +
    " 条（紧急 " + (counts.critical || 0) +
    " / 严重 " + (counts.major || 0) +
    " / 一般 " + (counts.minor || 0) + "）";
}

function renderAlarmItem(incident) {
  const item = document.createElement("div");
  item.className = "alarm-item sev-" + incident.severity + " " + incident.status;

  const head = document.createElement("div");
  head.className = "alarm-head";

  const title = document.createElement("span");
  title.className = "alarm-title";
  title.textContent = incident.title || incident.kind;

  const location = document.createElement("span");
  location.className = "alarm-location";
  location.textContent = incident.location;

  const severityBadge = badge(SEVERITY_LABEL[incident.severity] || incident.severity, "sev-" + incident.severity);
  const statusBadge = badge(STATUS_LABEL[incident.status] || incident.status, "status-" + incident.status);

  head.appendChild(title);
  head.appendChild(location);
  head.appendChild(severityBadge);
  head.appendChild(statusBadge);

  if (incident.occurrences > 1) {
    const repeat = badge("连锁 ×" + incident.occurrences, "status-active");
    head.appendChild(repeat);
  }
  if (incident.follows) {
    const follows = document.createElement("span");
    follows.className = "alarm-location";
    follows.textContent = "续接已确认报警 " + incident.follows.slice(0, 8);
    head.appendChild(follows);
  }

  if (incident.status !== "resolved") {
    const actions = document.createElement("div");
    actions.className = "alarm-actions";
    if (incident.status === "active") {
      const ack = document.createElement("button");
      ack.className = "ack";
      ack.textContent = "确认";
      ack.addEventListener("click", async () => {
        await post("/api/alarms/acknowledge", { id: incident.id, operator: "值班员" });
        refresh();
      });
      actions.appendChild(ack);
    }
    const resolve = document.createElement("button");
    resolve.className = "resolve";
    resolve.textContent = "销警";
    resolve.addEventListener("click", async () => {
      await post("/api/alarms/resolve", { id: incident.id, operator: "值班员" });
      refresh();
    });
    actions.appendChild(resolve);
    head.appendChild(actions);
  }

  const meta = document.createElement("div");
  meta.className = "alarm-meta";
  const parts = [
    "首次 " + formatTime(incident.first_seen),
    "最近 " + formatTime(incident.last_seen),
  ];
  if (incident.escalated_at) parts.push("升级于 " + formatTime(incident.escalated_at));
  if (incident.acknowledged_by) parts.push("确认人 " + incident.acknowledged_by);
  if (incident.resolved_by) parts.push("销警人 " + incident.resolved_by);
  meta.textContent = parts.join("　·　");

  item.appendChild(head);
  item.appendChild(meta);
  return item;
}

function badge(text, className) {
  const node = document.createElement("span");
  node.className = "alarm-badge " + className;
  node.textContent = text;
  return node;
}

async function refresh() {
  let state;
  try {
    state = await api("/api/state");
  } catch (error) {
    setOnline(false);
    return;
  }
  setOnline(true);
  const data = state.data || {};
  const zone = data.zone || {};
  const slew = data.slew || {};
  const hoist = data.hoist || {};
  const wind = data.wind || {};
  const limit = data.limit || {};
  const cluster = data.cluster || {};
  const pair = cluster.nearest_pair || {};
  renderCards([
    ["塔吊数量", Object.keys(cluster.positions || {}).length],
    ["分区占用", (zone.occupants || []).length, zone.conflict ? "danger" : ""],
    ["冲突闩锁", zone.latch ? "锁定" : "释放", zone.latch ? "danger" : ""],
    ["回转状态", slew.moving ? "运转" : "停止", wind.active ? "warn" : ""],
    ["落钩状态", hoist.hook_lowered ? "已落钩" : "未落钩", hoist.hook_lowered ? "" : "warn"],
    ["制动状态", hoist.brake_engaged ? "抱闸" : "松闸"],
    ["风速", (wind.level || 0).toFixed(1) + " m/s", wind.active ? "danger" : ""],
    ["限位闩锁", limit.latch ? "锁定" : "释放", limit.latch ? "danger" : ""],
    ["最近间距", pair.distance ? pair.distance.toFixed(1) + " 米" : "—", pair.below_safe ? "danger" : ""],
    ["待处理报警", data.alarms || 0, (data.alarms || 0) > 0 ? "danger" : ""],
    ["审计记录", data.audit || 0],
  ]);
  $("crane-data").textContent = JSON.stringify(cluster, null, 2);
  $("zone-data").textContent = JSON.stringify(zone, null, 2);
  $("hoist-data").textContent = JSON.stringify(hoist, null, 2);
  $("alarm-data").textContent = JSON.stringify({ wind, limit }, null, 2);
  $("config-data").textContent = JSON.stringify(data.config || {}, null, 2);
  const audit = await api("/api/audit?limit=15");
  $("audit-data").textContent = JSON.stringify(audit.data || [], null, 2);
  $("overview-log").textContent = JSON.stringify((audit.data || []).slice(0, 5), null, 2);
  renderAlarms();
}

document.querySelectorAll("#nav button").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll("#nav button").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    document.querySelectorAll("main section").forEach((section) => section.classList.add("hidden"));
    $("page-" + button.dataset.page).classList.remove("hidden");
  });
});

$("survey-btn").addEventListener("click", async () => {
  await post("/api/cluster/survey", {
    cranes: { "crane-1": { x: 0, y: 0 }, "crane-2": { x: 82, y: 1 } },
  });
  refresh();
});

$("reset-crane-btn").addEventListener("click", async () => {
  await post("/api/crane/reset");
  refresh();
});

$("occupy-a-btn").addEventListener("click", async () => {
  await post("/api/zone/occupy", { crane_id: "crane-1" });
  refresh();
});

$("occupy-b-btn").addEventListener("click", async () => {
  await post("/api/zone/occupy", { crane_id: "crane-2" });
  refresh();
});

$("clear-conflict-btn").addEventListener("click", async () => {
  await post("/api/zone/clear");
  refresh();
});

$("slew-start-btn").addEventListener("click", async () => {
  await post("/api/slew/start");
  refresh();
});

$("lower-btn").addEventListener("click", async () => {
  await post("/api/crane/command", { source: "cab", action: "lower", axis: "hoist" });
  refresh();
});

$("feedback-btn").addEventListener("click", async () => {
  await post("/api/hoist/feedback", { speed: 1.2 });
  refresh();
});

$("release-btn").addEventListener("click", async () => {
  await post("/api/hoist/release");
  refresh();
});

$("trolley-btn").addEventListener("click", async () => {
  await post("/api/crane/command", { source: "cab", action: "trolley", axis: "trolley" });
  refresh();
});

$("hoist-stop-btn").addEventListener("click", async () => {
  await post("/api/hoist/stop");
  refresh();
});

$("wind-alarm-btn").addEventListener("click", async () => {
  await post("/api/wind/alarm", { level: 14 });
  refresh();
});

$("wind-clear-btn").addEventListener("click", async () => {
  await post("/api/wind/clear");
  refresh();
});

$("limit-check-btn").addEventListener("click", async () => {
  await post("/api/limit/check");
  refresh();
});

$("slew-fault-btn").addEventListener("click", async () => {
  await post("/api/slew/fault", { faulted: true });
  refresh();
});

$("slew-recover-btn").addEventListener("click", async () => {
  await post("/api/slew/fault", { faulted: false });
  refresh();
});

$("config-save-btn").addEventListener("click", async () => {
  await post("/api/config", { site_name: "华东三号工地", safe_separation: 15 });
  refresh();
});

$("alarm-filter-status").addEventListener("change", renderAlarms);

$("alarm-clear-site-btn").addEventListener("click", async () => {
  const response = await post("/api/alarms/clear", { location: "site", operator: "值班员" });
  if (response.ok && response.data.resolved === 0) {
    alert("现场没有待销的报警；如需按塔吊销警，请在台账页按部位操作。");
  }
  refresh();
});

setInterval(refresh, 3000);
refresh();
