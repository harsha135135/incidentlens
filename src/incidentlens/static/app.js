// IncidentLens browser code. Every value from the server is written with textContent, never as HTML.
"use strict";

const TERMINAL = ["COMPLETED", "CANCELLED", "FAILED"];
let csrfToken = null;

function notice(message) {
  const box = document.getElementById("notice");
  if (!box) return;
  box.hidden = !message;
  box.textContent = message || "";
}

async function api(method, path, body, extraHeaders) {
  const headers = Object.assign({}, extraHeaders || {});
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && method !== "HEAD" && path !== "/api/v1/session") {
    if (!csrfToken) {
      const session = await api("GET", "/api/v1/session");
      csrfToken = session.data.csrf_token;
    }
    headers["X-CSRF-Token"] = csrfToken;
  }
  const response = await fetch(path, { method, headers, credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body) });
  if (response.status === 401 && !path.startsWith("/api/v1/session")) {
    window.location.assign("/login");
    throw new Error("Your session has expired.");
  }
  const text = await response.text();
  const data = text ? JSON.parse(text) : null;
  if (!response.ok) {
    const message = data && data.error ? data.error.message : `Request failed (${response.status})`;
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return { status: response.status, data };
}

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

// ---- Sign in ----
function initLogin() {
  const form = document.getElementById("login-form");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = document.getElementById("login-button");
    button.disabled = true;
    notice("Signing in…");
    try {
      await api("POST", "/api/v1/session", { secret: document.getElementById("secret").value });
      window.location.assign("/");
    } catch (error) {
      notice(error.message);
      button.disabled = false;
    }
  });
}

// ---- Start ----
function newKey() {
  return "ui-" + crypto.randomUUID();
}

function windowFromForm() {
  const mode = document.querySelector("input[name=window-mode]:checked").value;
  if (mode === "relative") {
    const minutes = Number(document.getElementById("relative").value);
    const end = new Date(Date.now() - 10 * 1000); // resolved once, here; the server stores absolute times
    return { start: new Date(end.getTime() - minutes * 60 * 1000).toISOString(), end: end.toISOString() };
  }
  const start = document.getElementById("start").value;
  const end = document.getElementById("end").value;
  if (!start || !end) throw new Error("Enter both a start and an end time.");
  return { start: start + "Z", end: end + "Z" };
}

async function showAvailability() {
  const box = document.getElementById("availability");
  try {
    const { data } = await api("GET", "/api/v1/services");
    const states = data.services[0].capabilities;
    box.textContent = "";
    const list = element("ul", undefined, "states");
    Object.entries(states).forEach(([name, state]) => {
      const item = element("li", `${name.replace(/_/g, " ")}: ${state}`, `state state-${state}`);
      list.appendChild(item);
    });
    box.appendChild(list);
  } catch (error) {
    box.textContent = "Telemetry availability is unknown: " + error.message;
  }
}

function initStart() {
  showAvailability();
  const form = document.getElementById("start-form");
  let key = newKey(); // kept for network retries; replaced when the request changes
  form.addEventListener("input", () => { key = newKey(); });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = document.getElementById("investigate");
    const symptom = document.getElementById("symptom").value.trim();
    if (!symptom) { notice("Describe the symptom first."); return; }
    let window_;
    try { window_ = windowFromForm(); } catch (error) { notice(error.message); return; }
    button.disabled = true;
    notice("Starting…");
    try {
      const { data } = await api("POST", "/api/v1/investigations", {
        service_id: document.getElementById("service").value, symptom, window: window_, profile: "standard",
      }, { "Idempotency-Key": key });
      window.location.assign(`/investigations/${data.id}`);
    } catch (error) {
      notice(error.message);
      button.disabled = false;
    }
  });
}

// ---- Progress ----
function describe(event) {
  const p = event.payload || {};
  switch (event.type) {
    case "queued": return "Queued";
    case "running": return `Started (attempt ${p.generation})`;
    case "action_admitted": return p.kind === "tool" ? `Collecting ${p.tool.replace(/_/g, " ")}` : "Asking the model for a decision";
    case "action_finished":
      if (p.kind === "tool") return `${p.tool.replace(/_/g, " ")}: ${p.status === "SUCCEEDED" ? `saved as ${p.handle} (${p.coverage})` : p.status}`;
      return `Model decision: ${p.decision || p.status}`;
    case "report_rejected": return "Report rejected; one repair requested";
    case "recovering": return `Recovering after an interruption (${p.reason})`;
    case "cancel_requested": return "Cancellation requested";
    case "cancelled": return "Cancelled";
    case "completed": return `Completed (${p.outcome}${p.source === "fallback" ? ", without a diagnosis" : ""})`;
    case "failed": return `Failed: ${p.reason}`;
    default: return event.type;
  }
}

function initProgress() {
  const section = document.getElementById("progress");
  const jobId = section.dataset.job;
  const timeline = document.getElementById("timeline");
  let afterSeq = 0;
  let delay = 2000;
  let startedAt = null;

  async function poll() {
    if (document.visibilityState !== "visible") { setTimeout(poll, delay); return; }
    try {
      const job = (await api("GET", `/api/v1/investigations/${jobId}`)).data;
      document.getElementById("status").textContent = job.status + (job.final_reason ? ` (${job.final_reason})` : "");
      document.getElementById("calls").textContent = `${job.tool_actions} data, ${job.model_actions} model`;
      startedAt = job.started_at || job.created_at;
      const events = (await api("GET", `/api/v1/investigations/${jobId}/events?after_seq=${afterSeq}`)).data.events;
      events.forEach((event) => {
        const item = element("li");
        item.appendChild(element("time", event.created_at.slice(11, 19), "muted"));
        item.appendChild(document.createTextNode(" " + describe(event)));
        timeline.appendChild(item);
        afterSeq = event.sequence;
      });
      notice("");
      delay = 2000;
      if (TERMINAL.includes(job.status)) {
        document.getElementById("cancel").hidden = true;
        if (job.report_available) window.location.reload();
        return;
      }
    } catch (error) {
      notice(`Could not refresh (${error.message}); retrying in 10 seconds.`);
      delay = 10000;
    }
    setTimeout(poll, delay);
  }

  setInterval(() => {
    if (startedAt) {
      const seconds = Math.max(0, Math.round((Date.now() - new Date(startedAt).getTime()) / 1000));
      document.getElementById("elapsed").textContent = `${seconds} s`;
    }
  }, 1000);
  document.getElementById("cancel").addEventListener("click", async (event) => {
    event.target.disabled = true;
    try {
      await api("POST", `/api/v1/investigations/${jobId}/cancel`);
      notice("Cancellation requested.");
    } catch (error) {
      notice(error.message);
      event.target.disabled = false;
    }
  });
  poll();
}

// ---- Report ----
function initReport() {
  document.querySelectorAll("a.cite").forEach((link) => {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      const target = document.getElementById(link.dataset.handle);
      if (!target) return;
      target.querySelector("details").open = true;
      target.focus();
      target.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") document.querySelectorAll("details[open]").forEach((d) => { d.open = false; });
  });
}

document.addEventListener("DOMContentLoaded", () => {
  const page = document.body.dataset.page;
  const signOut = document.getElementById("sign-out");
  if (signOut && page !== "login") {
    signOut.hidden = false;
    signOut.addEventListener("click", async () => {
      try { await api("DELETE", "/api/v1/session"); } finally { window.location.assign("/login"); }
    });
  }
  if (page === "login") initLogin();
  if (page === "start") initStart();
  if (page === "progress") initProgress();
  if (page === "report") initReport();
});
