"use strict";

const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[ch]));
}

function humanBytes(n) {
  if (n == null) return "—";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let v = Number(n);
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v >= 100 || i === 0 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}

function humanDuration(sec) {
  if (sec == null) return "—";
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${sec % 60}s`;
  return `${sec}s`;
}

function usageColor(p) {
  if (p == null) return "#30363d";
  if (p < 50) return "#3fb950";
  if (p < 80) return "#d29922";
  return "#f85149";
}

// ---------------------------------------------------------------------------
// charts
// ---------------------------------------------------------------------------

Chart.defaults.color = "#8b949e";
Chart.defaults.borderColor = "#30363d";
Chart.defaults.font.family = "system-ui, -apple-system, sans-serif";

const cpuSpark = new Chart($("cpu-spark"), {
  type: "line",
  data: {
    labels: [],
    datasets: [{
      data: [],
      borderColor: "#58a6ff",
      backgroundColor: "rgba(88, 166, 255, 0.15)",
      fill: true,
      tension: 0.35,
      pointRadius: 0,
      borderWidth: 2,
    }],
  },
  options: {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    plugins: { legend: { display: false }, tooltip: { enabled: false } },
    scales: { x: { display: false }, y: { display: false, min: 0, max: 100 } },
  },
});

const coreChart = new Chart($("core-chart"), {
  type: "bar",
  data: {
    labels: [],
    datasets: [{
      data: [],
      backgroundColor: [],
      borderRadius: 3,
      maxBarThickness: 38,
    }],
  },
  options: {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    plugins: {
      legend: { display: false },
      tooltip: { callbacks: { label: (c) => `${c.parsed.y}%` } },
    },
    scales: {
      x: { grid: { display: false } },
      y: { min: 0, max: 100, ticks: { callback: (v) => v + "%" } },
    },
  },
});

const memChart = new Chart($("mem-chart"), {
  type: "bar",
  data: {
    labels: ["RAM"],
    datasets: [
      { label: "Used", data: [0], backgroundColor: "#58a6ff" },
      { label: "Cached", data: [0], backgroundColor: "#3fb950" },
      { label: "Buffers", data: [0], backgroundColor: "#8957e5" },
      { label: "Free", data: [0], backgroundColor: "#21262d" },
    ],
  },
  options: {
    indexAxis: "y",
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    plugins: {
      legend: { position: "bottom" },
      tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${humanBytes(c.parsed.x)}` } },
    },
    scales: {
      x: { stacked: true, ticks: { callback: (v) => humanBytes(v) } },
      y: { stacked: true, grid: { display: false } },
    },
  },
});

// ---------------------------------------------------------------------------
// rendering
// ---------------------------------------------------------------------------

const HISTORY_MAX = 120; // ~3 minutes at 1.5s pushes
const history = [];

function onSnapshot({ system, containers }) {
  if (system) updateSystem(system);
  if (containers) updateTable(containers);
}

function updateSystem(system) {
  const cpu = system.cpu.overall_percent;
  const ram = system.memory.percent;

  history.push({ cpu, ram });
  if (history.length > HISTORY_MAX) history.shift();

  $("cpu-overall").textContent = cpu.toFixed(1) + "%";
  $("cpu-overall").style.color = usageColor(cpu);
  $("ram-overall").textContent = ram.toFixed(1) + "%";
  $("ram-overall").style.color = usageColor(ram);
  $("uptime").textContent = "up " + humanDuration(system.uptime_seconds);
  $("load").textContent = "load " + system.cpu.load_avg.map((v) => v.toFixed(2)).join(" / ");
  $("cores-count").textContent = system.cpu.cores + " cores";

  const m = system.memory;
  $("ram-used-txt").textContent = humanBytes(m.used_bytes);
  $("ram-cached-txt").textContent = humanBytes(m.cached_bytes);
  $("ram-buffers-txt").textContent = humanBytes(m.buffers_bytes);
  $("ram-free-txt").textContent = humanBytes(m.free_bytes);
  $("ram-bar-used").style.flexGrow = m.used_bytes;
  $("ram-bar-cached").style.flexGrow = m.cached_bytes;
  $("ram-bar-buffers").style.flexGrow = m.buffers_bytes;
  $("ram-bar-free").style.flexGrow = m.free_bytes;

  cpuSpark.data.labels = history.map(() => "");
  cpuSpark.data.datasets[0].data = history.map((h) => h.cpu);
  cpuSpark.update("none");

  const cores = system.cpu.per_core_percent;
  coreChart.data.labels = cores.map((_, i) => "cpu" + i);
  coreChart.data.datasets[0].data = cores;
  coreChart.data.datasets[0].backgroundColor = cores.map(usageColor);
  coreChart.update("none");

  memChart.data.datasets[0].data = [m.used_bytes];
  memChart.data.datasets[1].data = [m.cached_bytes];
  memChart.data.datasets[2].data = [m.buffers_bytes];
  memChart.data.datasets[3].data = [m.free_bytes];
  memChart.update("none");

  $("swap-sub").textContent = `swap ${humanBytes(m.swap_used_bytes)} / ${humanBytes(m.swap_total_bytes)}`;
}

const STATE_CLASS = {
  running: "ok",
  paused: "warn",
  restarting: "warn",
  created: "muted",
  dead: "bad",
};

function rowHtml(c) {
  const stateCls =
    c.state === "exited"
      ? c.exit_code === 0 ? "muted" : "bad"
      : STATE_CLASS[c.state] || "muted";
  const healthCls =
    c.health === "healthy" ? "ok" : c.health === "unhealthy" ? "bad" : "warn";
  let mem = "—";
  if (c.memory_usage_bytes != null) {
    mem = humanBytes(c.memory_usage_bytes)
      + (c.memory_limit_bytes ? " / " + humanBytes(c.memory_limit_bytes) : "")
      + (c.memory_percent != null ? ` (${c.memory_percent}%)` : "");
  }
  return `<tr>
    <td class="name">${esc(c.name)}</td>
    <td class="image">${esc(c.image || "—")}</td>
    <td><span class="badge ${stateCls}">${esc(c.state || "unknown")}</span></td>
    <td>${c.health ? `<span class="badge ${healthCls}">${esc(c.health)}</span>` : "—"}</td>
    <td class="dim">${esc(c.status)}</td>
    <td>${c.cpu_percent != null ? c.cpu_percent + "%" : "—"}</td>
    <td>${mem}</td>
  </tr>`;
}

function updateTable(payload) {
  const errEl = $("docker-error");
  if (payload.error) {
    errEl.hidden = false;
    errEl.textContent = "Docker: " + payload.error;
    $("container-count").textContent = "unavailable";
    return;
  }
  errEl.hidden = true;
  const list = payload.containers;
  $("container-count").textContent =
    list.length + (list.length === 1 ? " container" : " containers");
  $("containers-table").tbody.innerHTML = list.map(rowHtml).join("");
}

// ---------------------------------------------------------------------------
// live data: websocket with polling fallback
// ---------------------------------------------------------------------------

let wsFails = 0;
let pollTimer = null;

function setConn(ok) {
  const el = $("conn-status");
  el.classList.toggle("off", !ok);
  el.title = ok ? "live" : "reconnecting";
}

function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(async () => {
    try {
      const [s, c] = await Promise.all([
        fetch("/api/system").then((r) => r.json()),
        fetch("/api/containers").then((r) => (r.ok ? r.json() : { error: "HTTP " + r.status })),
      ]);
      onSnapshot({ system: s, containers: c });
    } catch {
      // keep polling; status dot already shows "reconnecting"
    }
  }, 3000);
}

function stopPolling() {
  clearInterval(pollTimer);
  pollTimer = null;
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => {
    wsFails = 0;
    stopPolling();
    setConn(true);
  };
  ws.onmessage = (e) => {
    try {
      onSnapshot(JSON.parse(e.data));
    } catch {
      // ignore malformed frames
    }
  };
  ws.onclose = () => {
    wsFails++;
    setConn(false);
    setTimeout(connect, 2000);
    if (wsFails >= 3) startPolling();
  };
  ws.onerror = () => ws.close();
}

connect();
