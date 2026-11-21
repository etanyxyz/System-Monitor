const dataEl = document.getElementById("data");
const statusEl = document.getElementById("status");

function render(payload) {
  dataEl.textContent = JSON.stringify(payload, null, 2);
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => {
    statusEl.textContent = "live";
  };
  ws.onmessage = (event) => {
    try {
      render(JSON.parse(event.data));
    } catch {
      dataEl.textContent = event.data;
    }
  };
  ws.onclose = () => {
    statusEl.textContent = "disconnected — retrying in 2s";
    setTimeout(connect, 2000);
  };
  ws.onerror = () => ws.close();
}

connect();
