"use strict";

const $ = (id) => document.getElementById(id);

// Optional auth: when MONITOR_TOKEN is set server-side, open the page as
// http://host:1010/?token=... and the token is attached to every fetch and
// the WebSocket URL (JS can't set headers on WebSocket handshakes).
const urlToken = new URLSearchParams(location.search).get("token");
function withToken(path) {
  if (!urlToken) return path;
  return `${path}${path.includes("?") ? "&" : "?"}token=${encodeURIComponent(urlToken)}`;
}

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

function timeLabel(t) {
  return new Date(t).toLocaleTimeString([], { hour12: false });
}

// ---------------------------------------------------------------------------
// charts
// ---------------------------------------------------------------------------

Chart.defaults.color = "#8b949e";
Chart.defaults.borderColor = "#30363d";
Chart.defaults.font.family = "system-ui, -apple-system, sans-serif";

function sparkline(id, color) {
  return new Chart($(id), {
    type: "line",
    data: {
      labels: [],
      datasets: [{
        data: [],
        borderColor: color,
        backgroundColor: color + "26", // ~15% alpha
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
}

const cpuSpark = sparkline("cpu-spark", "#58a6ff");
const ramSpark = sparkline("ram-spark", "#8957e5");

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

const sysTimeline = new Chart($("sys-timeline"), {
  type: "line",
  data: {
    labels: [],
    datasets: [
      {
        label: "CPU",
        data: [],
        borderColor: "#58a6ff",
        backgroundColor: "rgba(88, 166, 255, 0.12)",
        fill: true,
        tension: 0.3,
        pointRadius: 0,
        borderWidth: 2,
      },
      {
        label: "RAM",
        data: [],
        borderColor: "#8957e5",
        backgroundColor: "rgba(137, 87, 229, 0.10)",
        fill: true,
        tension: 0.3,
        pointRadius: 0,
        borderWidth: 2,
      },
    ],
  },
  options: {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { position: "top", labels: { boxWidth: 12 } },
      tooltip: { callbacks: { label: (c) => ` ${c.dataset.label}: ${c.parsed.y}%` } },
    },
    scales: {
      x: { grid: { display: false }, ticks: { maxTicksLimit: 8 } },
      y: { min: 0, max: 100, ticks: { callback: (v) => v + "%" } },
    },
  },
});

const contTimeline = new Chart($("cont-timeline"), {
  type: "line",
  data: { labels: [], datasets: [] },
  options: {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { position: "bottom", labels: { boxWidth: 12 } },
      tooltip: { callbacks: { label: (c) => ` ${c.dataset.label}: ${c.parsed.y}%` } },
    },
    scales: {
      x: { grid: { display: false }, ticks: { maxTicksLimit: 8 } },
      y: { min: 0, max: 100, ticks: { callback: (v) => v + "%" } },
    },
  },
});

// ---------------------------------------------------------------------------
// history + rendering
// ---------------------------------------------------------------------------

const HISTORY_MAX = 2400; // ~1 hour at 1.5 s pushes
const history = [];
let rangeSec = 300; // selected timeline window (seconds)

const CONTAINER_PALETTE = [
  "#58a6ff", "#3fb950", "#d29922", "#f85149", "#8957e5",
  "#39c5cf", "#ff7b72", "#db61a2", "#7ee787", "#a5a6ff",
  "#f0883e", "#56d364",
];
const containerColors = new Map();

function colorFor(name) {
  if (!containerColors.has(name)) {
    containerColors.set(
      name,
      CONTAINER_PALETTE[containerColors.size % CONTAINER_PALETTE.length]
    );
  }
  return containerColors.get(name);
}

function onSnapshot({ system, containers }) {
  if (system) updateSystem(system);
  if (containers) {
    updateTable(containers);
    if (!containers.error && history.length > 0) {
      // attach this sample's per-container CPU to the latest history entry
      const entry = history[history.length - 1];
      entry.cont = {};
      for (const c of containers.containers) {
        if (c.cpu_percent != null) entry.cont[c.name] = c.cpu_percent;
      }
    }
  }
  if (system) renderTimeline();
}

function updateSystem(system) {
  const cpu = system.cpu.overall_percent;
  const ram = system.memory.percent;

  history.push({
    t: (system.timestamp || Date.now() / 1000) * 1000,
    cpu,
    ram,
    cores: system.cpu.per_core_percent,
  });
  if (history.length > HISTORY_MAX) history.shift();

  $("cpu-overall").textContent = cpu.toFixed(1) + "%";
  $("cpu-overall").style.color = usageColor(cpu);
  $("ram-overall").textContent = ram.toFixed(1) + "%";
  $("ram-overall").style.color = usageColor(ram);
  $("uptime").textContent = "up " + humanDuration(system.uptime_seconds);
  $("load").textContent = "load " + system.cpu.load_avg.map((v) => v.toFixed(2)).join(" / ");
  $("cores-count").textContent = system.cpu.cores + " cores";

  const m = system.memory;
  $("swap-sub").textContent = `swap ${humanBytes(m.swap_used_bytes)} / ${humanBytes(m.swap_total_bytes)}`;

  const labels = history.map(() => "");
  cpuSpark.data.labels = labels;
  cpuSpark.data.datasets[0].data = history.map((h) => h.cpu);
  cpuSpark.update("none");
  ramSpark.data.labels = labels;
  ramSpark.data.datasets[0].data = history.map((h) => h.ram);
  ramSpark.update("none");

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

// keep at most `max` points (uniform stride, always include the newest)
function downsample(pts, max) {
  if (pts.length <= max) return pts;
  const step = Math.ceil(pts.length / max);
  const out = [];
  for (let i = 0; i < pts.length; i += step) out.push(pts[i]);
  if (out[out.length - 1] !== pts[pts.length - 1]) out.push(pts[pts.length - 1]);
  return out;
}

function renderTimeline() {
  if (history.length === 0) return;
  const cutoff = history[history.length - 1].t - rangeSec * 1000;
  const pts = history.filter((h) => h.t >= cutoff);

  const shown = downsample(pts, 240);
  sysTimeline.data.labels = shown.map((h) => timeLabel(h.t));
  sysTimeline.data.datasets[0].data = shown.map((h) => h.cpu);
  sysTimeline.data.datasets[1].data = shown.map((h) => h.ram);
  sysTimeline.update("none");

  const cpts = pts.filter((h) => h.cont);
  const cshown = downsample(cpts, 120);
  const names = new Set();
  for (const h of cpts) for (const n of Object.keys(h.cont)) names.add(n);
  contTimeline.data.labels = cshown.map((h) => timeLabel(h.t));
  contTimeline.data.datasets = [...names].map((name) => ({
    label: name,
    data: cshown.map((h) => (h.cont && h.cont[name] != null ? h.cont[name] : null)),
    borderColor: colorFor(name),
    backgroundColor: colorFor(name),
    pointRadius: 0,
    borderWidth: 1.5,
    tension: 0.3,
    spanGaps: true,
  }));
  contTimeline.update("none");
}

// timeline range picker
for (const btn of document.querySelectorAll(".range-btn")) {
  btn.addEventListener("click", () => {
    for (const b of document.querySelectorAll(".range-btn")) b.classList.remove("active");
    btn.classList.add("active");
    rangeSec = Number(btn.dataset.range);
    renderTimeline();
  });
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
        fetch(withToken("/api/system")).then((r) => r.json()),
        fetch(withToken("/api/containers")).then((r) => (r.ok ? r.json() : { error: "HTTP " + r.status })),
      ]);
      onSnapshot({ system: s, containers: c });
    } catch (e) {
      console.error("poll error:", e);
    }
  }, 3000);
}

function stopPolling() {
  clearInterval(pollTimer);
  pollTimer = null;
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  let wsUrl = `${proto}://${location.host}/ws`;
  if (urlToken) wsUrl += `?token=${encodeURIComponent(urlToken)}`;
  const ws = new WebSocket(wsUrl);
  ws.onopen = () => {
    wsFails = 0;
    stopPolling();
    setConn(true);
  };
  ws.onmessage = (e) => {
    try {
      onSnapshot(JSON.parse(e.data));
    } catch (err) {
      console.error("snapshot render error:", err);
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
