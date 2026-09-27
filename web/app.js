const $ = (id) => document.getElementById(id);

const SEVERITY_LABEL = { minor: "一般", major: "严重", critical: "紧急" };
const SEVERITY_TONE = { minor: "sev-minor", major: "sev-major", critical: "sev-critical" };
const STATUS_LABEL = { open: "待确认", acknowledged: "已确认", resolved: "已销项" };

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

function formatTime(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleTimeString("zh-CN", { hour12: false });
}

function formatDeadline(iso) {
  if (!iso) return "已锁定/顶格";
  const seconds = Math.max(0, Math.round((new Date(iso) - new Date()) / 1000));
  if (seconds === 0) return "已超时，待升级";
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return minutes > 0 ? `${minutes}分${rest}秒后升级` : `${rest}秒后升级`;
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
    ["活动报警", (data.alerts && data.alerts.counts ? data.alerts.counts.open : 0),
      (data.alerts && data.alerts.counts && data.alerts.counts.by_severity.critical) ? "danger" : "warn"],
    ["审计记录", data.audit || 0],
  ]);
  $("crane-data").textContent = JSON.stringify(cluster, null, 2);
  $("zone-data").textContent = JSON.stringify(zone, null, 2);
  $("hoist-data").textContent = JSON.stringify(hoist, null, 2);
  $("alarm-data").textContent = JSON.stringify({ wind, limit }, null, 2);
  $("config-data").textContent = JSON.stringify(data.config || {}, null, 2);
  renderAlertCardsFromState(data.alerts || { counts: {} });
  loadAlerts();
  const audit = await api("/api/audit?limit=15");
  $("audit-data").textContent = JSON.stringify(audit.data || [], null, 2);
  $("overview-log").textContent = JSON.stringify((audit.data || []).slice(0, 5), null, 2);
}

function renderAlertCardsFromState(alerts) {
  const counts = alerts.counts || { open: 0, unacknowledged: 0, by_severity: {} };
  const bySeverity = counts.by_severity || {};
  renderAlertCards([
    ["活动报警", counts.open || 0, (bySeverity.critical ? "danger" : "warn")],
    ["待确认", counts.unacknowledged || 0, (counts.unacknowledged ? "warn" : "")],
    ["紧急", bySeverity.critical || 0, (bySeverity.critical ? "danger" : "")],
    ["严重", bySeverity.major || 0, (bySeverity.major ? "warn" : "")],
    ["一般", bySeverity.minor || 0],
  ]);
}

async function loadAlerts() {
  const query = $("alerts-show-resolved").checked ? "?resolved=1" : "";
  let payload;
  try {
    payload = await api("/api/alerts" + query);
  } catch (error) {
    return;
  }
  renderAlertTable(
    (payload.data && payload.data.open) || [],
    $("alerts-show-resolved").checked ? (payload.data.resolved || []) : []
  );
}

function renderAlertCards(entries) {
  const cards = $("alert-cards");
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

function renderAlertTable(openAlerts, resolvedAlerts) {
  const tbody = $("alerts-tbody");
  tbody.innerHTML = "";
  const rows = [];
  for (const alarm of openAlerts) {
    rows.push(alertRow(alarm, false));
  }
  for (const alarm of resolvedAlerts) {
    rows.push(alertRow(alarm, true));
  }
  for (const row of rows) {
    tbody.appendChild(row);
  }
  $("alerts-empty").classList.toggle("hidden", rows.length > 0);
}

function alertRow(alarm, isResolved) {
  const row = document.createElement("tr");
  row.className = SEVERITY_TONE[alarm.severity] || "";
  if (isResolved) row.classList.add("resolved-row");

  const levelCell = document.createElement("td");
  levelCell.textContent = SEVERITY_LABEL[alarm.severity] || alarm.severity;
  const locationCell = document.createElement("td");
  locationCell.textContent = alarm.location;
  const messageCell = document.createElement("td");
  const summary = document.createElement("div");
  summary.className = "alert-message";
  summary.textContent = alarm.message || "";
  if (alarm.root_id) {
    const linked = document.createElement("span");
    linked.className = "alert-link";
    linked.textContent = " ↳ 接在已确认报警之后";
    summary.appendChild(linked);
  }
  messageCell.appendChild(summary);
  if (alarm.repeats > 1) {
    const repeats = document.createElement("span");
    repeats.className = "repeat-badge";
    repeats.textContent = `连锁 ×${alarm.repeats}`;
    messageCell.appendChild(repeats);
  }
  const repeatsCell = document.createElement("td");
  repeatsCell.textContent = alarm.repeats;
  const statusCell = document.createElement("td");
  statusCell.textContent = STATUS_LABEL[alarm.status] || alarm.status;
  if (alarm.acknowledged_by) {
    const by = document.createElement("div");
    by.className = "alert-operator";
    by.textContent = alarm.acknowledged_by;
    statusCell.appendChild(by);
  }
  const timeCell = document.createElement("td");
  timeCell.className = "time-cell";
  timeCell.textContent = `${formatTime(alarm.first_seen)} / ${formatTime(alarm.last_seen)}`;
  const deadlineCell = document.createElement("td");
  deadlineCell.className = "deadline-cell";
  if (alarm.status === "open") {
    deadlineCell.textContent = alarm.escalations
      ? `已升级${alarm.escalations}级 · ${formatDeadline(alarm.escalate_after)}`
      : formatDeadline(alarm.escalate_after);
  } else if (alarm.status === "acknowledged") {
    deadlineCell.textContent = "已确认，不再升级";
  } else {
    deadlineCell.textContent = alarm.resolved_by ? `${formatTime(alarm.resolved_at)} 销项` : "已销项";
  }
  const actionCell = document.createElement("td");
  actionCell.className = "row-actions";
  if (alarm.status === "open") {
    const ackButton = document.createElement("button");
    ackButton.textContent = "确认";
    ackButton.addEventListener("click", async () => {
      await post("/api/alerts/ack", { id: alarm.id });
      refresh();
    });
    actionCell.appendChild(ackButton);
  }
  if (alarm.status !== "resolved") {
    const resolveButton = document.createElement("button");
    resolveButton.textContent = "销项";
    resolveButton.className = "resolve-btn";
    resolveButton.addEventListener("click", async () => {
      await post("/api/alerts/resolve", { id: alarm.id });
      refresh();
    });
    actionCell.appendChild(resolveButton);
  }
  row.append(levelCell, locationCell, messageCell, repeatsCell, statusCell, timeCell, deadlineCell, actionCell);
  return row;
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

$("alert-raise-btn").addEventListener("click", async () => {
  const location = $("alert-location").value.trim();
  if (!location) {
    $("alert-location").focus();
    return;
  }
  await post("/api/alerts/raise", {
    location,
    severity: $("alert-severity").value,
    message: $("alert-message").value.trim(),
    source: "manual",
  });
  $("alert-message").value = "";
  refresh();
});

$("alerts-resolve-all-btn").addEventListener("click", async () => {
  await post("/api/alerts/resolve-all");
  refresh();
});

$("alerts-show-resolved").addEventListener("change", loadAlerts);

setInterval(refresh, 3000);
setInterval(loadAlerts, 1000);
refresh();
