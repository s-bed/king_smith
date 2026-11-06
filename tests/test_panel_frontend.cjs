/* Exercise the real panel with a small DOM and authenticated HA API adapter. */
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = readFileSync(path.join(__dirname, "../custom_components/king_smith/frontend/king-smith-panel.js"), "utf8");

function runtime() {
  const downloads = []; const blobs = []; const revocations = [];
  class Element {
    constructor(tag = "host") { this.tagName = tag; this.children = []; this.attributes = {}; this.events = {}; this._text = ""; }
    set textContent(value) { this._text = String(value); this.children = []; }
    get textContent() { return this._text + this.children.map((child) => child.textContent).join(" "); }
    set innerHTML(_value) { throw new Error("The panel must render data through safe DOM text, not HTML"); }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this._text = ""; this.children = children; }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    toggleAttribute(name, value) { if (value) this.attributes[name] = ""; else delete this.attributes[name]; }
    addEventListener(name, handler) { this.events[name] = handler; }
    attachShadow() { this.shadowRoot = new Element("shadow"); return this.shadowRoot; }
    getElementById(id) { if (this.id === id) return this; return this.children.map((child) => child.getElementById(id)).find(Boolean); }
    click() { if (this.tagName === "a") downloads.push({ filename: this.download, href: this.href }); this.events.click?.(); }
    remove() {}
    focus() {}
    dispatchEvent(event) { this.lastEvent = event; }
  }
  const registry = new Map();
  const context = vm.createContext({
    HTMLElement: Element, Blob, Uint8Array, atob,
    document: { createElement: (tag) => new Element(tag), createElementNS: (_ns, tag) => new Element(tag) },
    customElements: { get: (tag) => registry.get(tag), define: (tag, value) => registry.set(tag, value) },
    CustomEvent: class { constructor(type, options) { this.type = type; Object.assign(this, options); } },
    URL: { createObjectURL(blob) { blobs.push(blob); return "blob:private-session"; }, revokeObjectURL(url) { revocations.push(url); } },
    setTimeout: (handler) => handler(),
  });
  vm.runInContext(`${source}\n;globalThis.panelTest = {KingSmithPanel, duration, sampleChart};`, context);
  return { ...context.panelTest, downloads, blobs, revocations };
}

function session(overrides = {}) {
  return { session_id: "9d3cf12f-4699-4746-8116-5641d4ab8519", device_id: "pad-one", name: "Desk treadmill", start_time: 1700000000, end_time: 1700000210, moving_seconds: 180, elapsed_seconds: 210, distance_m: 100, steps: 301, partial: false, quality_flags: [], status: "sent", ...overrides };
}

function detail(summary = session()) {
  return {
    session: summary,
    recording: { ...summary, samples: [
      { timestamp: summary.start_time, distance_m: 0, steps: 0, speed_mps: 1.25, cadence_spm: 120 },
      { timestamp: summary.end_time, distance_m: 100, steps: 301, speed_mps: 0, cadence_spm: 0 },
    ] },
    quality_details: [], fit_error: null,
    fit_summary: { integrity_ok: true, distance_m: 100, timer_seconds: 180, elapsed_seconds: 210, steps: 301, avg_cadence_spm: 100.333, max_cadence_spm: 120, avg_speed_mps: 100 / 180, max_speed_mps: 1.25, record_count: 2, lap_count: 1, sport: "walking", sub_sport: "indoor_walking" },
  };
}

test("renders saved data safely and exposes the source/FIT comparison", async () => {
  const { KingSmithPanel } = runtime();
  const panel = new KingSmithPanel(); const calls = [];
  const malicious = session({ name: "<img src=x onerror=alert(1)>" });
  panel.hass = { config: { time_zone: "America/Toronto" }, locale: { language: "en" }, connection: {}, callWS: async (request) => {
    calls.push(request);
    return request.type.endsWith("/list") ? { sessions: [malicious], devices: [], storage_error: null } : detail(malicious);
  } };
  panel._connected = true;
  await panel._loadSessions();
  await new Promise(setImmediate);
  const text = panel._container.textContent;
  assert.ok(text.includes(malicious.name), "names remain literal text");
  assert.ok(text.includes("Session and exported FIT"));
  assert.ok(text.includes("2.0 km/h"));
  assert.ok(text.includes("Garmin can calculate its Moving Time"));
  assert.ok(text.includes("Download FIT"));
  assert.deepEqual(calls.map((call) => call.type), ["king_smith/sessions/list", "king_smith/sessions/get"]);
});

test("older asynchronous detail responses cannot replace a newer selection", async () => {
  const { KingSmithPanel } = runtime(); const panel = new KingSmithPanel();
  const pending = {};
  panel.hass = { connection: {}, callWS: (request) => new Promise((resolve) => { pending[request.session_id] = resolve; }) };
  panel._connected = true;
  const first = panel._select("first");
  const second = panel._select("second");
  pending.second(detail(session({ session_id: "second" })));
  await second;
  pending.first(detail(session({ session_id: "first" })));
  await first;
  assert.equal(panel._selectedId, "second");
  assert.equal(panel._details.session.session_id, "second");
});

test("FIT download uses authenticated WS bytes and never a public activity URL", async () => {
  const { KingSmithPanel, blobs, downloads, revocations } = runtime();
  const panel = new KingSmithPanel(); const calls = [];
  panel.hass = { connection: {}, callWS: async (request) => {
    calls.push(request);
    return { filename: "session.fit", mime_type: "application/vnd.ant.fit", content: Buffer.from([0, 1, 255]).toString("base64") };
  } };
  panel._connected = true; panel._selectedId = session().session_id;
  await panel._download("fit");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].type, "king_smith/sessions/download");
  assert.equal(calls[0].session_id, session().session_id);
  assert.equal(calls[0].format, "fit");
  assert.deepEqual([...new Uint8Array(await blobs[0].arrayBuffer())], [0, 1, 255]);
  assert.deepEqual(downloads, [{ filename: "session.fit", href: "blob:private-session" }]);
  assert.deepEqual(revocations, ["blob:private-session"]);
});

test("recording quality explanations distinguish gaps from lost counters", () => {
  const { KingSmithPanel } = runtime(); const panel = new KingSmithPanel();
  panel._details = detail(session({ partial: true, quality_flags: ["data_gap", "started_mid_walk"] }));
  panel._render();
  const text = panel._container.textContent;
  assert.ok(text.includes("does not necessarily mean distance or steps were lost"));
  assert.ok(text.includes("Walking before the first observation was not included"));
});

test("filters keep devices and review states independent", () => {
  const { KingSmithPanel } = runtime(); const panel = new KingSmithPanel();
  panel._sessions = [session(), session({ session_id: "other", device_id: "pad-two", status: "awaiting_review" })];
  panel._device = "pad-two";
  assert.equal(panel._filtered().length, 1);
  assert.equal(panel._filtered()[0].session_id, "other");
  panel._status = "sent";
  assert.equal(panel._filtered().length, 0);
});

test("an empty filter clears an in-flight detail request", async () => {
  const { KingSmithPanel } = runtime(); const panel = new KingSmithPanel();
  let resolve;
  panel.hass = { connection: {}, callWS: () => new Promise((done) => { resolve = done; }) };
  panel._connected = true; panel._sessions = [session()];
  const loading = panel._select(session().session_id);
  panel._changeFilter("_status", "awaiting_review");
  assert.equal(panel._detailLoading, false);
  assert.equal(panel._selectedId, null);
  resolve(detail());
  await loading;
  assert.equal(panel._details, null);
  assert.ok(panel._container.textContent.includes("No sessions match these filters"));
});

test("sample charts leave long data gaps disconnected", () => {
  const { sampleChart } = runtime();
  const figure = sampleChart([
    { timestamp: 0, speed_mps: 1 }, { timestamp: 5, speed_mps: 1 },
    { timestamp: 40, speed_mps: 2 }, { timestamp: 45, speed_mps: 2 },
  ], 0, 45, "speed_mps", 3.6, "Speed", "km/h");
  const svg = figure.children.find((child) => child.tagName === "svg");
  assert.equal(svg.children.filter((child) => child.tagName === "polyline").length, 2);
});

function reviewPanel(options = {}) {
  const { KingSmithPanel } = runtime(); const panel = new KingSmithPanel(); const services = [];
  let current = session({ status: "awaiting_review", ...options.session });
  panel.hass = { connection: {}, callService: async (...args) => {
    services.push(args);
    if (options.service) await options.service(...args);
    current = { ...current, status: args[1] === "upload_session" ? "queued" : "discarded" };
  }, callWS: async (request) => request.type.endsWith("/list") ? {
    sessions: [current], devices: [],
    garmin_accounts: options.accounts || [{ entity_id: "sensor.garmin_account_a", entity_ids: ["sensor.garmin_account_a", "sensor.garmin_legacy_a"], name: "Garmin A", available: true }],
    garmin_upload_available: options.available !== false,
  } : detail(current) };
  panel._connected = true;
  return { panel, services, setSession: (updates) => { current = { ...current, ...updates }; } };
}

test("web upload requires a selected account and a click, and duplicate clicks dispatch once", async () => {
  let release;
  const { panel, services } = reviewPanel({ accounts: [
    { entity_id: "sensor.account_a", name: "Garmin A", available: true },
    { entity_id: "sensor.account_b", name: "Garmin B", available: true },
  ], service: () => new Promise((done) => { release = done; }) });
  await panel._loadSessions(); await new Promise(setImmediate);
  assert.equal(services.length, 0, "listing and rendering cannot approve an upload");
  assert.equal(panel.shadowRoot.getElementById("upload-session").disabled, true);
  const selector = panel.shadowRoot.getElementById("garmin-account"); selector.value = "sensor.account_b"; selector.events.change();
  assert.equal(panel.shadowRoot.getElementById("upload-session").disabled, false);
  const first = panel._sessionAction("upload");
  await panel._sessionAction("upload"); await panel._sessionAction("discard");
  assert.equal(services.length, 1);
  assert.equal(services[0][0], "king_smith"); assert.equal(services[0][1], "upload_session");
  assert.equal(services[0][2].garmin_entity_id, "sensor.account_b");
  assert.equal(services[0][2].device_id, "pad-one");
  assert.equal(services[0][2].session_id, session().session_id);
  assert.equal(services[0][2].retry, false);
  assert.equal(Object.hasOwn(services[0][2], "expected_attempts"), false);
  release(); await first; await new Promise(setImmediate);
  assert.ok(panel._container.textContent.includes("Your upload approval is saved"));
  assert.equal(panel.shadowRoot.getElementById("upload-session"), undefined);
});

test("uncertain uploads require checking Garmin before an explicit retry and retain account routing", async () => {
  const { panel, services } = reviewPanel({ session: { status: "needs_review", attempts: 1, garmin_entity_id: "sensor.garmin_legacy_a" } });
  await panel._loadSessions(); await new Promise(setImmediate);
  assert.ok(panel._container.textContent.includes("previous upload may have reached Garmin"));
  assert.equal(panel.shadowRoot.getElementById("upload-session").disabled, true);
  await panel._sessionAction("upload"); assert.equal(services.length, 0);
  const checkbox = panel.shadowRoot.getElementById("confirm-retry"); checkbox.checked = true; checkbox.events.change();
  assert.equal(panel.shadowRoot.getElementById("upload-session").disabled, false);
  await panel._sessionAction("upload");
  assert.equal(services[0][2].retry, true);
  assert.equal(services[0][2].expected_attempts, 1);
  assert.equal(services[0][2].garmin_entity_id, "sensor.garmin_legacy_a");
  assert.equal(panel._retryConfirmed, false);
});

test("upload approval remains possible while Garmin is unavailable, but requires a known account", async () => {
  const { panel, services } = reviewPanel({ available: false, accounts: [
    { entity_id: "sensor.garmin_offline", name: "Garmin offline", available: false },
  ] });
  await panel._loadSessions(); await new Promise(setImmediate);
  assert.ok(panel._container.textContent.includes("it will wait until the selected account"));
  assert.equal(panel.shadowRoot.getElementById("upload-session").disabled, false);
  await panel._sessionAction("upload"); assert.equal(services.length, 1);
  const missing = reviewPanel({ accounts: [] });
  await missing.panel._loadSessions(); await new Promise(setImmediate);
  assert.ok(missing.panel._container.textContent.includes("No Garmin Connect account sensors"));
  assert.equal(missing.panel.shadowRoot.getElementById("upload-session").disabled, true);
  await missing.panel._sessionAction("upload"); assert.equal(missing.services.length, 0);
});

test("web controls respect resolved and in-flight states and retain discarded recordings", async () => {
  for (const status of ["sent", "discarded", "uploading"]) {
    const { panel, services } = reviewPanel({ session: { status } });
    await panel._loadSessions(); await new Promise(setImmediate);
    assert.equal(panel.shadowRoot.getElementById("upload-session"), undefined);
    assert.equal(panel.shadowRoot.getElementById("discard-session"), undefined);
    await panel._sessionAction("upload"); await panel._sessionAction("discard");
    assert.equal(services.length, 0);
    assert.ok(panel._container.textContent.includes("Download FIT"));
  }
  const { panel, services } = reviewPanel({ session: { status: "blocked", garmin_entity_id: "sensor.garmin_account_a" } });
  await panel._loadSessions(); await new Promise(setImmediate);
  assert.equal(panel.shadowRoot.getElementById("upload-session").disabled, true);
  assert.ok(panel.shadowRoot.getElementById("discard-session").textContent.includes("Cancel queued upload"));
  await panel._sessionAction("discard"); await new Promise(setImmediate);
  assert.equal(services[0][1], "discard_session");
  assert.equal(services[0][2].session_id, session().session_id);
  assert.ok(panel._container.textContent.includes("kept locally and excluded"));
});

test("account changes reset retry confirmation and service errors remain visible", async () => {
  const { panel, services } = reviewPanel({ session: { status: "needs_review", garmin_entity_id: "sensor.account_a" }, accounts: [
    { entity_id: "sensor.account_a", name: "Garmin A", available: true },
    { entity_id: "sensor.account_b", name: "Garmin B", available: true },
  ], service: async () => { throw new Error("Storage is unavailable"); } });
  await panel._loadSessions(); await new Promise(setImmediate);
  panel._retryConfirmed = true;
  const selector = panel.shadowRoot.getElementById("garmin-account"); selector.value = "sensor.account_b"; selector.events.change();
  assert.equal(panel._retryConfirmed, false);
  assert.equal(panel.shadowRoot.getElementById("upload-session").disabled, true);
  const checkbox = panel.shadowRoot.getElementById("confirm-retry"); checkbox.checked = true; checkbox.events.change();
  await panel._sessionAction("upload");
  assert.equal(services[0][2].garmin_entity_id, "sensor.account_b");
  assert.ok(panel._container.textContent.includes("Storage is unavailable"));
  assert.equal(panel._actionSession, null);
  assert.equal(panel.shadowRoot.getElementById("upload-session").disabled, false);
});

test("session events refresh upload status and subscriptions end when the panel disconnects", async () => {
  const { panel, services } = reviewPanel(); const callbacks = {}; const unsubscribed = [];
  panel._hass.connection.subscribeEvents = async (handler, event) => {
    callbacks[event] = handler; return () => unsubscribed.push(event);
  };
  await panel._subscribe();
  assert.deepEqual(Object.keys(callbacks), ["king_smith_session_ready", "king_smith_session_updated"]);
  callbacks.king_smith_session_updated(); await new Promise(setImmediate);
  assert.equal(panel._sessions.length, 1);
  assert.equal(services.length, 0);
  panel.disconnectedCallback();
  assert.deepEqual(unsubscribed, ["king_smith_session_ready", "king_smith_session_updated"]);
});

test("retry confirmation belongs to one saved upload attempt, including retries from another client", async () => {
  const { panel, setSession } = reviewPanel({ session: {
    status: "needs_review", attempts: 1, last_attempt: 1700000300, garmin_entity_id: "sensor.garmin_account_a",
  } });
  await panel._loadSessions(); await new Promise(setImmediate);
  const checkbox = panel.shadowRoot.getElementById("confirm-retry"); checkbox.checked = true; checkbox.events.change();
  await panel._select(session().session_id);
  assert.equal(panel._retryConfirmed, true, "refreshing the same attempt keeps its confirmation");
  setSession({ status: "uploading", attempts: 2, last_attempt: 1700000400 });
  await panel._select(session().session_id);
  assert.equal(panel._retryConfirmed, false, "another client starting dispatch invalidates the old confirmation");
  setSession({ status: "needs_review" }); await panel._select(session().session_id);
  assert.equal(panel.shadowRoot.getElementById("confirm-retry").checked, false);
  assert.equal(panel.shadowRoot.getElementById("upload-session").disabled, true);
});
