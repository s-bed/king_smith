/* Home Assistant's authenticated WalkingPad session browser. No external assets. */

const WS = "king_smith/sessions/";
const SVG_NS = "http://www.w3.org/2000/svg";
const STATUS_LABELS = {
  awaiting_review: "Awaiting review", queued: "Upload queued", blocked: "Upload waiting",
  uploading: "Uploading", sent: "Sent to Garmin", needs_review: "Check upload result",
  discarded: "Kept locally", failed_local: "File needs attention",
};
const QUALITY_LABELS = {
  started_mid_walk: ["Recording started during walking", "Walking before the first observation was not included."],
  data_gap: ["Some samples were delayed or missing", "The device counters may still preserve the totals; a gap does not necessarily mean distance or steps were lost."],
  connection_lost: ["Connection interrupted", "This recording ends at the last received sample, before the connection timeout."],
  counter_reset: ["Device counters restarted", "The recorder combined the counter segments. This routine reset does not mark the recording as partial."],
  counter_reset_while_moving: ["Device counters restarted during walking", "The recorder combined the observed segments, but measurements between the last observation and the reset may be missing."],
  counter_jump: ["Device timing was inconsistent", "The recorder limited an implausible duration change. Timing may be incomplete."],
};

function finite(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function duration(value) {
  const seconds = Math.max(0, Math.round(finite(value)));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainder = String(seconds % 60).padStart(2, "0");
  return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${remainder}` : `${minutes}:${remainder}`;
}

function metric(value, fraction = 1, suffix = "") {
  return value == null ? "—" : `${finite(value).toFixed(fraction)}${suffix}`;
}

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text != null) element.textContent = String(text);
  if (className) element.className = className;
  return element;
}

function button(text, handler, className = "") {
  const element = node("button", text, className);
  element.type = "button";
  element.addEventListener("click", handler);
  return element;
}

function errorText(error) {
  return typeof error?.message === "string" ? error.message : "Please try again.";
}

function svgNode(tag, attributes, text) {
  const element = document.createElementNS(SVG_NS, tag);
  for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, String(value));
  if (text != null) element.textContent = String(text);
  return element;
}

function sampleChart(samples, start, end, field, multiplier, title, unit) {
  const figure = node("figure", null, "chart");
  figure.append(node("figcaption", `${title} · ${unit}`));
  const values = samples.filter((sample) => Number.isFinite(Number(sample.timestamp))
    && Number.isFinite(Number(sample[field]))).slice().sort((a, b) => a.timestamp - b.timestamp);
  if (!values.length) {
    figure.append(node("p", "No samples are available for this recording.", "muted"));
    return figure;
  }
  const width = 600; const height = 185;
  const left = 48; const right = 16; const top = 16; const bottom = 33;
  const span = Math.max(1, end - start);
  const maximum = Math.max(1, ...values.map((sample) => finite(sample[field]) * multiplier));
  const scale = Math.ceil(maximum * 10) / 10;
  const x = (timestamp) => left + Math.max(0, Math.min(1, (timestamp - start) / span)) * (width - left - right);
  const y = (value) => height - bottom - Math.max(0, value / scale) * (height - top - bottom);
  const svg = svgNode("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": `${title} over the recorded session` });
  svg.append(svgNode("title", {}, `${title}. ${values.length} observed samples. Gaps longer than 15 seconds are unconnected.`));
  for (const amount of [0, scale / 2, scale]) {
    svg.append(svgNode("line", { x1: left, x2: width - right, y1: y(amount), y2: y(amount), class: "grid" }));
    svg.append(svgNode("text", { x: left - 8, y: y(amount) + 4, "text-anchor": "end" }, amount.toFixed(1)));
  }
  for (const fraction of [0, 0.5, 1]) {
    svg.append(svgNode("text", { x: x(start + span * fraction), y: height - 8, "text-anchor": fraction === 0 ? "start" : fraction === 1 ? "end" : "middle" }, duration(span * fraction)));
  }
  let segment = [];
  const flush = () => {
    if (segment.length === 1) {
      const [cx, cy] = segment[0].split(",");
      svg.append(svgNode("circle", { cx, cy, r: 2.5, class: "sample-point" }));
    } else if (segment.length) svg.append(svgNode("polyline", { points: segment.join(" "), class: "series" }));
    segment = [];
  };
  values.forEach((sample, index) => {
    if (index && sample.timestamp - values[index - 1].timestamp > 15) flush();
    segment.push(`${x(sample.timestamp)},${y(finite(sample[field]) * multiplier)}`);
  });
  flush();
  figure.append(svg);
  return figure;
}

const STYLE = `
  :host { display:block; height:100%; color:var(--primary-text-color,#212121); background:var(--primary-background-color,#f5f5f5); font:14px var(--paper-font-body1_-_font-family,Roboto,Arial,sans-serif); overflow:auto; }
  * { box-sizing:border-box; } button,select { font:inherit; } button { cursor:pointer; } button:disabled { cursor:default; opacity:.55; }
  button:focus-visible,select:focus-visible,summary:focus-visible { outline:3px solid var(--primary-color,#03a9f4); outline-offset:3px; }
  header { display:flex; align-items:center; gap:12px; padding:14px 20px; position:sticky; top:0; z-index:2; background:var(--app-header-background-color,var(--primary-background-color,#fff)); color:var(--app-header-text-color,var(--primary-text-color)); border-bottom:1px solid var(--divider-color,#ddd); }
  header h1 { font-size:20px; line-height:1.3; margin:0; font-weight:500; flex:1; } .menu { border:0; background:transparent; color:inherit; padding:8px; font-size:22px; line-height:1; }
  main { max-width:1440px; padding:24px; margin:auto; } h2 { font-size:20px; font-weight:500; margin:0 0 8px; } h3 { font-size:16px; font-weight:500; margin:22px 0 12px; } p { line-height:1.55; } .muted { color:var(--secondary-text-color,#666); } .small { font-size:12px; } .summary { display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:20px; }
  .summary > div,.card { border:1px solid var(--divider-color,#ddd); border-radius:12px; background:var(--card-background-color,#fff); } .summary > div { padding:16px; } .summary strong { display:block; font-size:24px; font-weight:500; margin:5px 0 0; font-variant-numeric:tabular-nums; }
  .filters { display:flex; gap:16px; flex-wrap:wrap; margin-bottom:20px; } label { display:flex; flex-direction:column; gap:6px; font-size:12px; color:var(--secondary-text-color,#666); } select { color:var(--primary-text-color); background:var(--card-background-color,#fff); border:1px solid var(--divider-color,#ddd); border-radius:6px; padding:10px; min-width:180px; }
  .layout { display:grid; grid-template-columns:minmax(270px,350px) minmax(0,1fr); gap:20px; align-items:start; } .session-list { list-style:none; margin:0; padding:0; max-height:78vh; overflow:auto; } .session-list li + li { border-top:1px solid var(--divider-color,#ddd); } .session { display:block; width:100%; text-align:left; padding:16px; border:0; color:inherit; background:transparent; } .session[aria-pressed=true] { background:var(--secondary-background-color,#eaf4f8); box-shadow:inset 4px 0 var(--primary-color,#03a9f4); } .session:hover { background:var(--secondary-background-color,#f5f5f5); } .session strong { display:block; font-size:15px; font-weight:500; margin-bottom:5px; } .session .line { display:flex; justify-content:space-between; flex-wrap:wrap; gap:8px; margin:7px 0; }
  .badge { display:inline-block; font-size:11px; padding:3px 7px; border-radius:4px; color:var(--secondary-text-color); background:var(--secondary-background-color,#eee); } .badge.partial { color:var(--warning-color,#9c6a00); border:1px solid currentColor; background:transparent; }
  .detail { padding:22px; min-height:280px; } .detail-header { display:flex; gap:14px; justify-content:space-between; align-items:flex-start; flex-wrap:wrap; } .detail-header p { margin:5px 0; } .actions { display:flex; gap:8px; flex-wrap:wrap; margin:18px 0; } .action { background:transparent; border:1px solid var(--primary-color,#03a9f4); color:var(--primary-color,#0395ce); padding:9px 14px; border-radius:7px; font-weight:500; } .header-refresh { border:1px solid currentColor; color:inherit; background:transparent; padding:7px 12px; border-radius:6px; }
  .metrics { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:14px; padding:18px 0; margin:12px 0; border-top:1px solid var(--divider-color,#ddd); border-bottom:1px solid var(--divider-color,#ddd); } .metrics strong { display:block; font-size:22px; font-weight:500; margin:5px 0; font-variant-numeric:tabular-nums; }
  .notice { border:1px solid var(--divider-color,#ddd); border-left:4px solid var(--info-color,#039be5); padding:12px 14px; border-radius:6px; margin:12px 0; } .notice.warning { border-left-color:var(--warning-color,#ff9800); } .notice.error { border-left-color:var(--error-color,#db4437); } .notice strong { display:block; margin-bottom:4px; } .notice p { margin:0; }
  .garmin-controls { margin:18px 0; padding:0 0 16px; border-bottom:1px solid var(--divider-color,#ddd); } .garmin-controls h3 { margin:0 0 12px; } .garmin-controls .actions { margin:12px 0; } .retry-confirmation { display:flex; flex-direction:row; align-items:flex-start; gap:9px; margin:14px 0; line-height:1.5; font-size:14px; color:var(--primary-text-color); } .retry-confirmation input { flex:none; margin-top:4px; } .primary-action { color:var(--text-primary-color,#fff); background:var(--primary-color,#03a9f4); }
  .chart { margin:20px 0; } figcaption { font-size:14px; font-weight:500; margin-bottom:5px; } svg { display:block; width:100%; overflow:visible; } svg text { fill:var(--secondary-text-color,#666); font-size:11px; font-family:inherit; } .grid { stroke:var(--divider-color,#ddd); stroke-width:1; } .series { fill:none; stroke:var(--primary-color,#03a9f4); stroke-width:2; stroke-linecap:round; stroke-linejoin:round; }
  .sample-point { fill:var(--primary-color,#03a9f4); } .table-scroll { overflow-x:auto; } table { width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; } th,td { text-align:left; padding:10px 8px; border-bottom:1px solid var(--divider-color,#ddd); white-space:nowrap; } th { font-weight:500; color:var(--secondary-text-color,#666); } td:not(:first-child),th:not(:first-child) { text-align:right; } details { margin-top:22px; } summary { cursor:pointer; font-weight:500; padding:8px 0; } code { font-size:12px; overflow-wrap:anywhere; } .empty { padding:30px 20px; text-align:center; } .status { margin:0 0 16px; min-height:20px; } .quality { margin-bottom:18px; }
  @media(max-width:950px) { main { padding:16px; } .layout { grid-template-columns:1fr; } .session-list { max-height:350px; } .summary { gap:8px; } .summary > div { padding:12px; } .summary strong { font-size:20px; } }
  @media(max-width:520px) { header { padding:12px; gap:6px; } header h1 { font-size:18px; } main { padding:12px; } .summary,.metrics { grid-template-columns:repeat(2,1fr); } .detail { padding:16px; } label,select { width:100%; } .filters { gap:10px; } th,td { padding:9px 6px; } }
`;

class KingSmithPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    const style = node("style", STYLE);
    this._container = node("div");
    this.shadowRoot.append(style, this._container);
    this._sessions = []; this._devices = []; this._device = "all"; this._status = "all";
    this._selectedId = null; this._details = null; this._loading = false;
    this._detailLoading = false; this._error = ""; this._detailError = "";
    this._storageError = ""; this._downloadError = ""; this._downloading = "";
    this._listRequest = 0; this._detailRequest = 0; this._connected = false;
    this._garminAccounts = []; this._garminUploadAvailable = false; this._garminSelections = {};
    this._actionSession = null; this._actionError = ""; this._actionMessage = ""; this._actionResultSession = null;
    this._retryConfirmed = false; this._retryAttempt = ""; this._subscriptions = []; this._subscriptionRequest = 0;
  }

  set hass(value) {
    const changed = this._hass?.connection !== value?.connection;
    this._hass = value;
    if (changed && this._connected) { this._subscribe(); this._loadSessions(); }
  }

  set panel(value) { this._panel = value; }
  set narrow(value) { this.toggleAttribute("narrow", Boolean(value)); }
  set route(value) { this._route = value; }

  connectedCallback() {
    this._connected = true;
    this._render();
    if (this._hass) { this._subscribe(); this._loadSessions(); }
  }

  disconnectedCallback() {
    this._connected = false;
    this._listRequest += 1; this._detailRequest += 1;
    this._loading = false; this._detailLoading = false;
    this._unsubscribe();
  }

  _unsubscribe() {
    this._subscriptionRequest += 1;
    this._subscriptions.forEach((unsubscribe) => unsubscribe());
    this._subscriptions = [];
  }

  async _subscribe() {
    this._unsubscribe();
    const request = this._subscriptionRequest;
    if (!this._hass?.connection?.subscribeEvents) return;
    for (const event of ["king_smith_session_ready", "king_smith_session_updated"]) {
      try {
        const unsubscribe = await this._hass.connection.subscribeEvents(() => {
          if (this._connected) this._loadSessions();
        }, event);
        if (!this._connected || request !== this._subscriptionRequest) unsubscribe();
        else this._subscriptions.push(unsubscribe);
      } catch (_error) { /* Refresh remains available when event subscriptions fail. */ }
    }
  }

  _when(timestamp) {
    if (timestamp == null || !Number.isFinite(Number(timestamp))) return "—";
    const options = { dateStyle: "medium", timeStyle: "short" };
    if (this._hass?.config?.time_zone) options.timeZone = this._hass.config.time_zone;
    return new Date(Number(timestamp) * 1000).toLocaleString(this._hass?.locale?.language, options);
  }

  _filtered() {
    return this._sessions.filter((session) => (this._device === "all" || session.device_id === this._device)
      && (this._status === "all" || session.status === this._status));
  }

  async _loadSessions() {
    if (!this._hass) return;
    const request = ++this._listRequest;
    this._loading = true; this._error = ""; this._render();
    try {
      const response = await this._hass.callWS({ type: `${WS}list` });
      if (!this._connected || request !== this._listRequest) return;
      this._sessions = response.sessions || []; this._devices = response.devices || [];
      this._storageError = response.storage_error || "";
      this._garminAccounts = response.garmin_accounts || [];
      this._garminUploadAvailable = Boolean(response.garmin_upload_available);
      const filtered = this._filtered();
      const selected = filtered.find((session) => session.session_id === this._selectedId) || filtered[0];
      if (selected) this._select(selected.session_id);
      else this._clearSelection();
    } catch (error) {
      if (request === this._listRequest) this._error = `Could not load session history. ${errorText(error)}`;
    } finally {
      if (request === this._listRequest) { this._loading = false; this._render(); }
    }
  }

  async _select(sessionId) {
    if (sessionId !== this._selectedId) this._retryConfirmed = false;
    this._selectedId = sessionId; this._details = null; this._detailError = "";
    this._downloadError = ""; this._detailLoading = true;
    const request = ++this._detailRequest;
    this._render();
    try {
      const details = await this._hass.callWS({ type: `${WS}get`, session_id: sessionId });
      if (request === this._detailRequest && this._connected) {
        const session = details.session;
        const attempt = JSON.stringify([session.session_id, session.status, session.attempts, session.last_attempt, session.garmin_entity_id]);
        if (attempt !== this._retryAttempt) this._retryConfirmed = false;
        this._retryAttempt = attempt;
        this._details = details;
      }
    } catch (error) {
      if (request === this._detailRequest) this._detailError = `Could not open this session. ${errorText(error)}`;
    } finally {
      if (request === this._detailRequest) { this._detailLoading = false; this._render(); }
    }
  }

  _changeFilter(kind, value) {
    this[kind] = value;
    const filtered = this._filtered();
    if (!filtered.some((session) => session.session_id === this._selectedId)) {
      if (filtered[0]) this._select(filtered[0].session_id);
      else this._clearSelection();
    }
    this._render();
  }

  _clearSelection() {
    this._selectedId = null; this._details = null; this._detailRequest += 1;
    this._detailLoading = false; this._detailError = ""; this._downloadError = "";
    this._retryConfirmed = false; this._retryAttempt = "";
  }

  _garminAccount(session) {
    const selected = this._garminSelections[session.session_id];
    if (selected && this._garminAccounts.some((account) => account.entity_id === selected)) return selected;
    if (session.garmin_entity_id && this._garminAccounts.some((account) => account.entity_id === session.garmin_entity_id || account.entity_ids?.includes(session.garmin_entity_id))) return session.garmin_entity_id;
    return this._garminAccounts.length === 1 ? this._garminAccounts[0].entity_id : "";
  }

  async _sessionAction(action) {
    const session = this._details?.session;
    if (!session || this._actionSession) return;
    const upload = action === "upload";
    const retry = session.status === "needs_review";
    const account = this._garminAccount(session);
    if (upload && (!account || (retry && !this._retryConfirmed)
      || !["awaiting_review", "failed_local", "needs_review", "blocked"].includes(session.status))) return;
    if (!upload && ["uploading", "sent", "discarded"].includes(session.status)) return;
    this._actionSession = session.session_id; this._actionResultSession = session.session_id;
    this._actionError = ""; this._actionMessage = ""; this._render();
    try {
      const data = { session_id: session.session_id, device_id: session.device_id };
      if (upload) Object.assign(data, { garmin_entity_id: account, retry });
      if (upload && retry) data.expected_attempts = session.attempts;
      await this._hass.callService("king_smith", upload ? "upload_session" : "discard_session", data);
      this._retryConfirmed = false;
      this._actionMessage = upload ? "Upload approval saved. The status below follows the upload result." : "Recording kept locally; Garmin upload skipped.";
      await this._loadSessions();
    } catch (error) {
      this._actionError = `Could not ${upload ? "approve this upload" : "keep this recording locally"}. ${errorText(error)}`;
    } finally {
      this._actionSession = null; this._render();
    }
  }

  async _download(format) {
    const sessionId = this._selectedId;
    if (!sessionId || this._downloading) return;
    this._downloading = format; this._downloadError = ""; this._render();
    try {
      const response = await this._hass.callWS({ type: `${WS}download`, session_id: sessionId, format });
      const decoded = atob(response.content);
      const bytes = Uint8Array.from(decoded, (character) => character.charCodeAt(0));
      const blob = new Blob([bytes], { type: response.mime_type || "application/octet-stream" });
      const url = URL.createObjectURL(blob);
      const link = node("a");
      link.href = url; link.download = String(response.filename).replace(/[\\/]/g, "_");
      this.shadowRoot.append(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) {
      this._downloadError = `Could not download the ${format.toUpperCase()} file. ${errorText(error)}`;
    } finally {
      this._downloading = ""; this._render();
    }
  }

  _notice(title, description, severity = "info") {
    const notice = node("div", null, `notice ${severity === "warning" ? "warning" : severity === "error" ? "error" : "info"}`);
    notice.append(node("strong", title), node("p", description));
    return notice;
  }

  _selectControl(labelText, id, entries, selected, handler) {
    const label = node("label", labelText);
    const select = node("select"); select.id = id;
    for (const [value, text] of entries) {
      const option = node("option", text); option.value = value; option.selected = value === selected;
      select.append(option);
    }
    select.addEventListener("change", () => handler(select.value));
    label.append(select);
    return label;
  }

  _render() {
    const focused = this.shadowRoot.activeElement?.id;
    this._container.replaceChildren();
    const header = node("header");
    const menu = button("☰", () => this.dispatchEvent(new CustomEvent("hass-toggle-menu", { bubbles: true, composed: true })), "menu");
    menu.setAttribute("aria-label", "Toggle sidebar");
    const refresh = button(this._loading ? "Refreshing…" : "Refresh", () => this._loadSessions(), "header-refresh");
    refresh.id = "refresh"; refresh.disabled = this._loading;
    header.append(menu, node("h1", "WalkingPad sessions"), refresh);
    const main = node("main");
    const status = node("p", this._loading ? "Loading saved sessions…" : "Review saved walks, upload to Garmin, and download activity files.", "status muted small");
    status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite"); main.append(status);
    if (this._error) main.append(this._notice("Session history unavailable", this._error, "error"));
    if (this._storageError) main.append(this._notice("Recording storage needs attention", this._storageError, "error"));
    const filtered = this._filtered();
    const summary = node("div", null, "summary");
    for (const [label, value] of [
      ["Saved sessions", filtered.length],
      ["Recorded walking time", duration(filtered.reduce((sum, s) => sum + finite(s.moving_seconds), 0))],
      ["Distance", metric(filtered.reduce((sum, s) => sum + finite(s.distance_m), 0) / 1000, 2, " km")],
      ["Awaiting resolution", filtered.filter((s) => !["sent", "discarded"].includes(s.status)).length],
    ]) {
      const card = node("div"); card.append(node("span", label, "muted small"), node("strong", value)); summary.append(card);
    }
    main.append(summary);
    const filters = node("div", null, "filters");
    const deviceEntries = [["all", "All WalkingPads"], ...this._devices.map((device) => [device.device_id, device.name])];
    for (const session of this._sessions) {
      if (!deviceEntries.some(([id]) => id === session.device_id)) deviceEntries.push([session.device_id, session.name]);
    }
    filters.append(this._selectControl("WalkingPad", "device-filter", deviceEntries, this._device, (value) => this._changeFilter("_device", value)));
    filters.append(this._selectControl("Review status", "status-filter", [["all", "All statuses"], ...Object.entries(STATUS_LABELS)], this._status, (value) => this._changeFilter("_status", value)));
    main.append(filters);
    const layout = node("div", null, "layout");
    const list = node("ul", null, "session-list card"); list.setAttribute("aria-label", "Saved sessions");
    if (!filtered.length) list.append(node("li", this._loading ? "Loading…" : this._sessions.length ? "No sessions match these filters." : "No saved sessions yet. A walk is saved after a two-minute stop or data timeout.", "empty muted"));
    for (const session of filtered) {
      const item = node("li");
      const row = button(null, () => this._select(session.session_id), "session");
      row.id = `session-${session.session_id}`; row.setAttribute("aria-pressed", session.session_id === this._selectedId ? "true" : "false");
      row.append(node("strong", this._when(session.start_time)), node("span", session.name, "muted small"));
      const line = node("div", null, "line"); line.append(node("span", duration(session.moving_seconds)), node("span", metric(finite(session.distance_m) / 1000, 2, " km")), node("span", `${Math.round(finite(session.steps))} steps`)); row.append(line);
      const badges = node("div", null, "line"); badges.append(node("span", STATUS_LABELS[session.status] || session.status, "badge"));
      if (session.partial) badges.append(node("span", "Partial recording", "badge partial"));
      row.append(badges); item.append(row); list.append(item);
    }
    layout.append(list, this._renderDetails()); main.append(layout); this._container.append(header, main);
    if (focused) this.shadowRoot.getElementById(focused)?.focus();
  }

  _renderDetails() {
    const detail = node("section", null, "card detail"); detail.setAttribute("aria-label", "Selected session details");
    if (this._detailLoading) { detail.append(node("p", "Loading session details…", "muted")); return detail; }
    if (this._detailError) { detail.append(this._notice("Session unavailable", this._detailError, "error")); return detail; }
    if (!this._details) { detail.append(node("p", "Select a session to inspect its measurements and download its files.", "muted")); return detail; }
    const { session, recording, fit_summary: fit } = this._details;
    const samples = recording?.samples || [];
    const heading = node("div", null, "detail-header");
    const title = node("div"); title.append(node("h2", session.name), node("p", this._when(session.start_time), "muted"));
    heading.append(title, node("span", STATUS_LABELS[session.status] || session.status, "badge")); detail.append(heading);
    const metrics = node("div", null, "metrics");
    for (const [label, value] of [["Recorded walking time", duration(session.moving_seconds)], ["Elapsed time", duration(session.elapsed_seconds)], ["Distance", metric(finite(session.distance_m) / 1000, 2, " km")], ["Steps", Math.round(finite(session.steps))]]) {
      const item = node("div"); item.append(node("span", label, "muted small"), node("strong", value)); metrics.append(item);
    }
    detail.append(metrics);
    detail.append(this._renderGarminControls(session));
    const quality = node("div", null, "quality");
    let explanations = this._details.quality_details;
    if (!Array.isArray(explanations) || !explanations.length) explanations = (session.quality_flags || []).map((code) => ({
      code, title: QUALITY_LABELS[code]?.[0] || String(code).replaceAll("_", " "),
      description: QUALITY_LABELS[code]?.[1] || "Review this session's measurements.", severity: session.partial ? "warning" : "info",
    }));
    for (const explanation of explanations) quality.append(this._notice(explanation.title, explanation.description, explanation.severity));
    if (!explanations.length) quality.append(node("p", "No recording-quality warnings were reported.", "muted small"));
    if (session.error) quality.append(this._notice("Review status", session.error, "warning"));
    detail.append(quality);
    const actions = node("div", null, "actions");
    for (const [format, label] of [["fit", "Download FIT"], ["json", "Download session JSON"]]) {
      const download = button(this._downloading === format ? "Preparing download…" : label, () => this._download(format), "action"); download.disabled = Boolean(this._downloading); actions.append(download);
    }
    detail.append(actions);
    if (this._downloadError) detail.append(this._notice("Download failed", this._downloadError, "error"));
    if (this._details.fit_error) detail.append(this._notice("FIT inspection unavailable", this._details.fit_error, "warning"));
    if (fit) detail.append(this._fitComparison(session, samples, fit));
    detail.append(node("h3", "Recorded samples"), node("p", "These charts use actual observations. Gaps longer than 15 seconds are left unconnected.", "muted small"));
    detail.append(sampleChart(samples, finite(session.start_time), finite(session.end_time), "speed_mps", 3.6, "Speed", "km/h"));
    detail.append(sampleChart(samples, finite(session.start_time), finite(session.end_time), "cadence_spm", 1, "Cadence", "steps/min"));
    const technical = node("details"); technical.append(node("summary", "Sample table and recording details"));
    technical.append(node("p", `Started ${this._when(session.start_time)} · Ended ${this._when(session.end_time)}`, "muted small"), node("p", `${samples.length} recorded samples. Showing the first 100 below.`, "muted small"));
    const idLine = node("p", "Session ID: ", "muted small"); idLine.append(node("code", session.session_id)); technical.append(idLine);
    technical.append(this._table(["Time", "Distance (m)", "Speed (km/h)", "Cadence (spm)", "Steps"], samples.slice(0, 100).map((sample) => [this._when(sample.timestamp), metric(sample.distance_m, 0), metric(finite(sample.speed_mps) * 3.6), metric(sample.cadence_spm), metric(sample.steps, 0)])));
    detail.append(technical);
    return detail;
  }

  _renderGarminControls(session) {
    const section = node("section", null, "garmin-controls"); section.append(node("h3", "Garmin Connect"));
    const status = session.status;
    const accountId = this._garminAccount(session);
    const account = this._garminAccounts.find((item) => item.entity_id === accountId || item.entity_ids?.includes(accountId));
    const busy = Boolean(this._actionSession);
    if (["sent", "discarded", "uploading", "queued"].includes(status)) {
      const messages = {
        sent: "This recording has already been sent to Garmin. It will not be uploaded again.",
        discarded: "This recording is kept locally and excluded from Garmin uploads.",
        uploading: "This activity is being sent to Garmin. It can no longer be cancelled.",
        queued: "Your upload approval is saved. The recording is waiting for the upload worker.",
      };
      section.append(node("p", messages[status], "muted small"));
      if (session.garmin_entity_id && ["sent", "uploading", "queued"].includes(status)) section.append(node("p", `Selected account: ${account?.name || session.garmin_entity_id}`, "muted small"));
    } else {
      const entries = [["", "Select a Garmin account"], ...this._garminAccounts.map((item) => [
        item === account && item.entity_ids?.includes(accountId) ? accountId : item.entity_id,
        `${item.name}${item.available ? "" : " (unavailable)"}`,
      ])];
      const selector = this._selectControl("Garmin account", "garmin-account", entries, accountId, (value) => {
        this._garminSelections[session.session_id] = value; this._retryConfirmed = false; this._render();
      });
      selector.children[selector.children.length - 1].disabled = busy;
      section.append(selector);
      if (!this._garminAccounts.length) section.append(node("p", "No Garmin Connect account sensors are configured. Add the Garmin Connect integration, then refresh this page.", "muted small"));
      else if (!this._garminUploadAvailable || (account && !account.available)) section.append(node("p", "Garmin is currently unavailable. You can approve the upload now; it will wait until the selected account and upload action recover.", "muted small"));
      if (status === "blocked") section.append(node("p", "Upload approval is already saved and will resume when Garmin recovers. You may select another account or keep this recording locally.", "muted small"));
      if (status === "needs_review") {
        section.append(this._notice("Check Garmin before retrying", "The previous upload may have reached Garmin. Retry only after checking that this activity is absent, to avoid a duplicate.", "warning"));
        const label = node("label", null, "retry-confirmation"); const checkbox = node("input");
        checkbox.type = "checkbox"; checkbox.id = "confirm-retry"; checkbox.checked = this._retryConfirmed; checkbox.disabled = busy;
        checkbox.addEventListener("change", () => { this._retryConfirmed = checkbox.checked; this._render(); });
        label.append(checkbox, node("span", "I checked Garmin and this activity is not there.")); section.append(label);
      }
      const actions = node("div", null, "actions");
      const uploadLabel = status === "needs_review" ? "Retry upload to Garmin" : status === "failed_local" ? "Rebuild file and upload" : status === "blocked" ? "Update upload account" : "Upload to Garmin";
      const upload = button(this._actionSession === session.session_id ? "Saving…" : uploadLabel, () => this._sessionAction("upload"), "action primary-action");
      upload.id = "upload-session"; upload.disabled = busy || !accountId || (status === "needs_review" && !this._retryConfirmed) || (status === "blocked" && accountId === session.garmin_entity_id);
      actions.append(upload); section.append(actions);
    }
    if (!["sent", "discarded", "uploading"].includes(status)) {
      const discard = button(["queued", "blocked"].includes(status) ? "Cancel queued upload; keep locally" : "Keep locally; skip upload", () => this._sessionAction("discard"), "action");
      discard.id = "discard-session"; discard.disabled = busy;
      section.append(discard, node("p", "Kept recordings remain available here for 90 days. This does not remove an activity already in Garmin.", "muted small"));
    }
    if (this._actionResultSession === session.session_id) {
      if (this._actionError) section.append(this._notice("Session action failed", this._actionError, "error"));
      else if (this._actionMessage) { const message = node("p", this._actionMessage, "muted small"); message.setAttribute("role", "status"); section.append(message); }
    }
    return section;
  }

  _fitComparison(session, samples, fit) {
    const section = node("section"); section.append(node("h3", "Session and exported FIT"));
    section.append(node("p", fit.integrity_ok ? "FIT file integrity verified." : "FIT file integrity could not be verified.", "muted small"));
    const moving = finite(session.moving_seconds);
    const maximum = (field) => samples.length ? Math.max(...samples.map((sample) => finite(sample[field]))) : null;
    const averageCadence = moving ? finite(session.steps) * 60 / moving : 0;
    const averageSpeed = moving ? finite(session.distance_m) / moving : 0;
    const rows = [
      ["Distance", metric(session.distance_m, 0, " m"), metric(fit.distance_m, 0, " m")],
      ["Timer duration", duration(moving), fit.timer_seconds == null ? "—" : duration(fit.timer_seconds)],
      ["Elapsed duration", duration(session.elapsed_seconds), fit.elapsed_seconds == null ? "—" : duration(fit.elapsed_seconds)],
      ["Steps", metric(session.steps, 0), metric(fit.steps, 0)],
      ["Average cadence", metric(averageCadence, 1, " spm"), metric(fit.avg_cadence_spm, 1, " spm")],
      ["Maximum cadence", metric(maximum("cadence_spm"), 1, " spm"), metric(fit.max_cadence_spm, 1, " spm")],
      ["Average speed", metric(averageSpeed * 3.6, 1, " km/h"), fit.avg_speed_mps == null ? "—" : metric(fit.avg_speed_mps * 3.6, 1, " km/h")],
      ["Maximum speed", maximum("speed_mps") == null ? "—" : metric(maximum("speed_mps") * 3.6, 1, " km/h"), fit.max_speed_mps == null ? "—" : metric(fit.max_speed_mps * 3.6, 1, " km/h")],
      ["Samples", samples.length, fit.record_count ?? "—"],
    ];
    section.append(this._table(["Metric", "Recorded session", "FIT file"], rows));
    section.append(node("p", "Garmin can calculate its Moving Time from sample data. Compare Garmin Total Time with FIT timer duration, and Garmin Elapsed Time with FIT elapsed duration.", "muted small"));
    return section;
  }

  _table(headings, rows) {
    const wrapper = node("div", null, "table-scroll"); const table = node("table");
    const head = node("thead"); const heading = node("tr");
    headings.forEach((title) => { const cell = node("th", title); cell.scope = "col"; heading.append(cell); }); head.append(heading);
    const body = node("tbody");
    rows.forEach((values) => { const row = node("tr"); values.forEach((value) => row.append(node("td", value))); body.append(row); });
    table.append(head, body); wrapper.append(table); return wrapper;
  }
}

if (!customElements.get("king-smith-panel")) customElements.define("king-smith-panel", KingSmithPanel);
