import { LitElement, html, css, repeat, classMap } from "/static/vendor/lit.js";

const api = {
  async get(path) {
    const r = await fetch(path);
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || r.statusText);
    return j;
  },
  async post(path, body) {
    const r = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body ?? {}),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || r.statusText);
    return j;
  },
};

const shared = css`
  :host { display: block; }
  table { border-collapse: collapse; width: 100%; font-family: var(--mono); font-size: 12px; }
  th, td { text-align: left; padding: 4px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }
  th { color: var(--muted); font-weight: 500; position: sticky; top: 0; background: var(--panel); }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
  button { background: var(--panel-2); color: var(--text); border: 1px solid var(--line); border-radius: 4px; padding: 4px 10px; font: inherit; cursor: pointer; }
  button:hover { border-color: var(--accent); }
  button.primary { background: var(--accent); color: #0b1016; border-color: var(--accent); }
  input, select, textarea { background: var(--bg); color: var(--text); border: 1px solid var(--line); border-radius: 4px; padding: 4px 6px; font: inherit; }
  textarea { font-family: var(--mono); font-size: 12px; }
  .muted { color: var(--muted); }
  .ok { color: var(--ok); } .warn { color: var(--warn); } .bad { color: var(--bad); }
  .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 6px; overflow: auto; }
  .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  pre { margin: 0; font-family: var(--mono); font-size: 12px; white-space: pre-wrap; word-break: break-word; }
  .tag { display: inline-block; padding: 0 6px; border-radius: 3px; font-size: 11px; font-family: var(--mono); border: 1px solid var(--line); }
  .tag.verified { color: var(--ok); border-color: var(--ok); }
  .tag.candidate { color: var(--warn); border-color: var(--warn); }
  h3 { margin: 0 0 6px; font-size: 13px; color: var(--muted); font-weight: 500; text-transform: uppercase; letter-spacing: .04em; }
`;

function fmtTime(t) {
  const d = new Date(t * 1000);
  return d.toLocaleTimeString([], { hour12: false }) + "." + String(d.getMilliseconds()).padStart(3, "0");
}

function fmtValue(v) {
  if (v === null || v === undefined) return "null";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

class StudioApp extends LitElement {
  static properties = {
    tab: { state: true },
    summary: { state: true },
    messages: { state: true },
    diffs: { state: true },
    wsOpen: { state: true },
    paused: { state: true },
  };
  static styles = [
    shared,
    css`
      :host { display: grid; grid-template-rows: auto 1fr; height: 100%; }
      header { display: flex; align-items: center; gap: 16px; padding: 8px 14px; border-bottom: 1px solid var(--line); background: var(--panel); }
      header .title { font-weight: 600; letter-spacing: .02em; }
      header .facts { display: flex; gap: 14px; font-family: var(--mono); font-size: 12px; color: var(--muted); }
      header .facts b { color: var(--text); font-weight: 500; }
      nav { display: flex; gap: 4px; margin-left: auto; }
      nav button { border-radius: 4px 4px 0 0; }
      nav button.active { background: var(--accent); color: #0b1016; border-color: var(--accent); }
      main { overflow: hidden; padding: 12px 14px; }
      .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--bad); display: inline-block; margin-right: 6px; }
      .dot.on { background: var(--ok); }
    `,
  ];

  constructor() {
    super();
    this.tab = "stream";
    this.summary = null;
    this.messages = [];
    this.diffs = [];
    this.wsOpen = false;
    this.paused = false;
  }

  connectedCallback() {
    super.connectedCallback();
    this.refresh();
    this._timer = setInterval(() => this.refresh(), 2000);
    this.openWs();
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    clearInterval(this._timer);
    this._ws?.close();
  }

  async refresh() {
    try {
      this.summary = await api.get("/api/summary");
    } catch (err) {
      console.warn("summary failed", err);
    }
  }

  openWs() {
    const ws = new WebSocket(`ws://${location.host}/ws`);
    this._ws = ws;
    ws.onopen = () => (this.wsOpen = true);
    ws.onclose = () => {
      this.wsOpen = false;
      setTimeout(() => this.openWs(), 1500);
    };
    ws.onmessage = (e) => this.onEvent(JSON.parse(e.data));
  }

  onEvent(ev) {
    if (ev.type === "hello") {
      this.summary = ev;
      return;
    }
    if (this.paused) return;
    if (ev.type === "message") this.messages = [ev, ...this.messages].slice(0, 400);
    else if (ev.type === "diff") this.diffs = [ev, ...this.diffs].slice(0, 200);
    else if (ev.type === "connection") this.refresh();
  }

  render() {
    const s = this.summary;
    const tabs = [
      ["stream", "Stream"],
      ["knowledge", "Knowledge"],
      ["fixtures", "Fixtures"],
      ["console", "Console"],
    ];
    return html`
      <header>
        <span class="title">Yarbo Local Studio</span>
        <span class="facts">
          <span><span class=${classMap({ dot: true, on: s?.connected && this.wsOpen })}></span>${s?.connected ? "broker" : "no broker"}</span>
          <span>serial <b>${s?.serial ?? "?"}</b></span>
          <span>fw <b>${s?.firmware ?? "?"}</b></span>
          <span>${s?.awake ? html`<b class="ok">awake</b>` : html`<b class="muted">asleep</b>`}</span>
          <span>activity <b>${s?.activity ?? "?"}</b></span>
          <span>battery <b>${s?.battery ?? "?"}%</b></span>
          <span>head <b>${s?.head ?? "?"}</b></span>
          <span class="muted">${s?.editable ? "editing " + s.protocol_dir : "read-only (no protocol dir)"}</span>
        </span>
        <nav>
          ${tabs.map(([id, label]) => html`<button class=${classMap({ active: this.tab === id })} @click=${() => (this.tab = id)}>${label}</button>`)}
        </nav>
      </header>
      <main>
        ${this.tab === "stream"
          ? html`<stream-pane .summary=${s} .messages=${this.messages} .diffs=${this.diffs} .paused=${this.paused} @pause=${(e) => (this.paused = e.detail)}></stream-pane>`
          : this.tab === "knowledge"
            ? html`<knowledge-pane .editable=${s?.editable}></knowledge-pane>`
            : this.tab === "fixtures"
              ? html`<fixtures-pane .editable=${s?.editable} .ring=${s?.ring}></fixtures-pane>`
              : html`<console-pane></console-pane>`}
      </main>
    `;
  }
}

class StreamPane extends LitElement {
  static properties = {
    summary: {},
    messages: {},
    diffs: {},
    paused: {},
    filter: { state: true },
    selected: { state: true },
    hideEcho: { state: true },
  };
  static styles = [
    shared,
    css`
      :host { display: grid; grid-template-columns: 1fr 1.4fr; grid-template-rows: 1fr 1fr; gap: 12px; height: 100%; }
      .topics { grid-row: 1 / span 2; }
      .log, .diffs { display: flex; flex-direction: column; min-height: 0; }
      .log .panel, .diffs .panel { flex: 1; }
      tr.msg { cursor: pointer; }
      tr.msg:hover td { background: var(--panel-2); }
      tr.selected td { background: var(--panel-2); }
      td.payload { max-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .detail { border-top: 1px solid var(--line); padding: 8px; max-height: 40%; overflow: auto; background: var(--bg); }
      .toolbar { display: flex; gap: 8px; align-items: center; margin-bottom: 6px; }
      .toolbar input[type=text] { flex: 1; }
      .kv { display: grid; grid-template-columns: max-content 1fr; gap: 2px 10px; font-family: var(--mono); font-size: 12px; }
      .old { color: var(--muted); text-decoration: line-through; margin-right: 6px; }
    `,
  ];

  constructor() {
    super();
    this.filter = "";
    this.selected = null;
    this.hideEcho = false;
  }

  render() {
    const topics = this.summary?.topics ?? [];
    const f = this.filter.toLowerCase();
    const rows = this.messages.filter(
      (m) => (!this.hideEcho || !m.echo) && (!f || m.topic.toLowerCase().includes(f) || fmtValue(m.payload).toLowerCase().includes(f)),
    );
    return html`
      <section class="topics">
        <h3>Topics</h3>
        <div class="panel" style="max-height: calc(100% - 24px)">
          <table>
            <thead><tr><th>topic</th><th class="num">count</th><th class="num">rate/s</th><th class="num">age s</th><th>enc</th><th class="num">bytes</th></tr></thead>
            <tbody>
              ${repeat(topics, (t) => t.topic, (t) => html`
                <tr @click=${() => (this.filter = t.topic.split("/").slice(-2).join("/"))} style="cursor:pointer">
                  <td>${t.topic}</td><td class="num">${t.count}</td><td class="num">${t.rate}</td>
                  <td class="num ${t.age > 30 ? "muted" : ""}">${t.age}</td><td>${t.enc}</td><td class="num">${t.size}</td>
                </tr>`)}
              ${topics.length ? "" : html`<tr><td colspan="6" class="muted">nothing seen yet; the robot heartbeats every 5 s asleep</td></tr>`}
            </tbody>
          </table>
        </div>
      </section>
      <section class="log">
        <h3>Messages</h3>
        <div class="toolbar">
          <input type="text" placeholder="filter topic or payload" .value=${this.filter} @input=${(e) => (this.filter = e.target.value)}>
          <label><input type="checkbox" .checked=${this.hideEcho} @change=${(e) => (this.hideEcho = e.target.checked)}> hide own echoes</label>
          <button @click=${() => this.dispatchEvent(new CustomEvent("pause", { detail: !this.paused }))}>${this.paused ? "Resume" : "Pause"}</button>
          <button @click=${() => api.post("/api/wake")}>Wake</button>
        </div>
        <div class="panel">
          <table>
            <thead><tr><th>time</th><th>topic</th><th>enc</th><th class="num">bytes</th><th>payload</th></tr></thead>
            <tbody>
              ${repeat(rows, (m) => m.t + m.topic, (m) => html`
                <tr class=${classMap({ msg: true, selected: this.selected === m })} @click=${() => (this.selected = this.selected === m ? null : m)}>
                  <td>${fmtTime(m.t)}</td>
                  <td>${m.side}/${m.leaf}${m.echo ? html` <span class="tag">echo</span>` : ""}</td>
                  <td>${m.enc}</td><td class="num">${m.size}</td>
                  <td class="payload">${fmtValue(m.payload)}</td>
                </tr>`)}
            </tbody>
          </table>
        </div>
        ${this.selected ? html`<div class="detail"><pre>${JSON.stringify(this.selected.payload, null, 2)}</pre></div>` : ""}
      </section>
      <section class="diffs">
        <h3>DeviceMSG changes (frame to frame)</h3>
        <div class="panel" style="padding: 6px 8px">
          ${this.diffs.length ? "" : html`<span class="muted">no frames yet; DeviceMSG streams at 1 Hz while the robot is awake</span>`}
          ${repeat(this.diffs, (d) => d.t, (d) => html`
            <div style="margin-bottom: 8px">
              <div class="muted" style="font-family: var(--mono); font-size: 11px">${fmtTime(d.t)}
                ${d.added.length ? html` +${d.added.length} keys` : ""}${d.removed.length ? html` -${d.removed.length} keys` : ""}</div>
              <div class="kv">
                ${Object.entries(d.changed).map(([k, [a, b]]) => html`<span>${k}</span><span><span class="old">${fmtValue(a)}</span>${fmtValue(b)}</span>`)}
              </div>
            </div>`)}
        </div>
      </section>
    `;
  }
}

const ENTITY_TYPES = ["", "sensor", "binary_sensor", "switch", "number", "light", "device_tracker", "update", "select"];

class KnowledgePane extends LitElement {
  static properties = {
    editable: {},
    diff: { state: true },
    fields: { state: true },
    filter: { state: true },
    editing: { state: true },
    form: { state: true },
    status: { state: true },
    onlyUnknown: { state: true },
  };
  static styles = [
    shared,
    css`
      :host { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; height: 100%; }
      section { display: flex; flex-direction: column; min-height: 0; }
      .panel { flex: 1; }
      form { display: grid; grid-template-columns: max-content 1fr; gap: 6px 10px; padding: 10px; background: var(--panel-2); border-radius: 6px; margin-bottom: 8px; align-items: center; }
      form .full { grid-column: 1 / -1; display: flex; gap: 8px; justify-content: flex-end; }
      .promote { color: var(--accent); }
      .toolbar { display: flex; gap: 8px; margin-bottom: 6px; align-items: center; }
      .toolbar input[type=text] { flex: 1; }
    `,
  ];

  constructor() {
    super();
    this.diff = null;
    this.fields = {};
    this.filter = "";
    this.editing = null;
    this.form = {};
    this.status = "";
    this.onlyUnknown = false;
  }

  connectedCallback() {
    super.connectedCallback();
    this.load();
  }

  async load() {
    try {
      const [diff, knowledge] = await Promise.all([api.get("/api/diff"), api.get("/api/knowledge")]);
      this.diff = diff;
      this.fields = knowledge.fields;
    } catch (err) {
      this.status = String(err);
    }
  }

  startEdit(path) {
    const existing = this.fields[path] ?? {};
    this.editing = path;
    this.form = {
      entity: existing.entity ?? "",
      device_class: existing.device_class ?? "",
      unit: existing.unit ?? "",
      scale: existing.scale ?? "",
      sentinel: existing.sentinel ?? "",
      enabled: existing.enabled ?? false,
      category: existing.category ?? "",
      heads: (existing.heads ?? []).join(","),
      notes: existing.notes ?? "",
      source: existing.source ?? "capture",
    };
  }

  async submit(e) {
    e.preventDefault();
    const body = { path: this.editing, ...this.form };
    body.heads = body.heads ? body.heads.split(",").map((h) => h.trim()).filter(Boolean) : [];
    for (const k of ["scale", "sentinel"]) if (body[k] !== "" && !isNaN(Number(body[k]))) body[k] = Number(body[k]);
    try {
      const entry = await api.post("/api/fields", body);
      this.status = `wrote ${this.editing} (${entry.status})`;
      this.editing = null;
      await this.load();
    } catch (err) {
      this.status = `error: ${err.message}`;
    }
  }

  renderForm() {
    const f = this.form;
    const set = (k) => (e) => (this.form = { ...f, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value });
    return html`
      <form @submit=${this.submit}>
        <b class="full" style="justify-content:flex-start">${this.editing}</b>
        <label>entity</label><select .value=${f.entity} @change=${set("entity")}>${ENTITY_TYPES.map((t) => html`<option value=${t} ?selected=${t === f.entity}>${t || "(none)"}</option>`)}</select>
        <label>device_class</label><input type="text" .value=${f.device_class} @input=${set("device_class")}>
        <label>unit</label><input type="text" .value=${f.unit} @input=${set("unit")}>
        <label>scale</label><input type="text" .value=${f.scale} @input=${set("scale")} placeholder="multiplier, e.g. 0.001">
        <label>sentinel</label><input type="text" .value=${f.sentinel} @input=${set("sentinel")} placeholder="raw value meaning no reading">
        <label>enabled</label><input type="checkbox" .checked=${f.enabled} @change=${set("enabled")}>
        <label>category</label><select .value=${f.category} @change=${set("category")}>${["", "diagnostic", "config"].map((c) => html`<option value=${c} ?selected=${c === f.category}>${c || "(none)"}</option>`)}</select>
        <label>heads</label><input type="text" .value=${f.heads} @input=${set("heads")} placeholder="comma-separated head types, e.g. 3,5">
        <label>source</label><select .value=${f.source} @change=${set("source")}>${["capture", "vendor SDK", "community", "guess"].map((c) => html`<option value=${c} ?selected=${c === f.source}>${c}</option>`)}</select>
        <label>notes</label><textarea rows="3" .value=${f.notes} @input=${set("notes")}></textarea>
        <div class="full">
          <span class="muted" style="margin-right:auto">source "capture" marks the path verified; anything else stays candidate</span>
          <button type="button" @click=${() => (this.editing = null)}>Cancel</button>
          <button type="submit" class="primary">Write to fields.seed.yaml</button>
        </div>
      </form>`;
  }

  render() {
    const d = this.diff;
    const f = this.filter.toLowerCase();
    const known = Object.entries(this.fields).filter(([p, e]) => !f || p.toLowerCase().includes(f) || (e.notes ?? "").toLowerCase().includes(f));
    return html`
      <section>
        <h3>Unknown paths seen live ${d ? html`<span class="muted">(${d.unknown.length} unknown of ${d.seen} seen; ${d.known} in the map)</span>` : ""}</h3>
        ${this.editing ? this.renderForm() : ""}
        <div class="panel">
          <table>
            <thead><tr><th>path</th><th>type</th><th>example</th><th class="num">count</th><th></th></tr></thead>
            <tbody>
              ${(d?.unknown ?? []).map((u) => html`<tr><td>${u.path}</td><td>${u.type}</td><td>${fmtValue(u.example)}</td><td class="num">${u.count}</td>
                <td>${this.editable ? html`<button class="promote" @click=${() => this.startEdit(u.path)}>Promote</button>` : ""}</td></tr>`)}
              ${d && !d.unknown.length ? html`<tr><td colspan="5" class="muted">every path the robot has sent so far is in the field map</td></tr>` : ""}
            </tbody>
          </table>
        </div>
        <div class="muted" style="margin-top:6px">${this.status}</div>
      </section>
      <section>
        <h3>Field map</h3>
        <div class="toolbar">
          <input type="text" placeholder="filter path or notes" .value=${this.filter} @input=${(e) => (this.filter = e.target.value)}>
          <button @click=${this.load}>Reload</button>
        </div>
        <div class="panel">
          <table>
            <thead><tr><th>path</th><th>status</th><th>entity</th><th>unit</th><th>notes</th><th></th></tr></thead>
            <tbody>
              ${repeat(known, ([p]) => p, ([p, e]) => html`<tr>
                <td>${p}</td><td><span class="tag ${e.status}">${e.status}</span></td><td>${e.entity ?? ""}</td><td>${e.unit ?? ""}</td>
                <td class="muted" style="max-width: 320px; white-space: normal">${e.notes ?? ""}</td>
                <td>${this.editable ? html`<button @click=${() => this.startEdit(p)}>Edit</button>` : ""}</td></tr>`)}
            </tbody>
          </table>
        </div>
      </section>
    `;
  }
}

class FixturesPane extends LitElement {
  static properties = { editable: {}, ring: {}, items: { state: true }, name: { state: true }, seconds: { state: true }, result: { state: true } };
  static styles = [
    shared,
    css`
      :host { display: flex; flex-direction: column; gap: 12px; height: 100%; }
      .save { display: flex; gap: 8px; align-items: center; padding: 10px; background: var(--panel-2); border-radius: 6px; }
      .panel { flex: 1; }
    `,
  ];

  constructor() {
    super();
    this.items = [];
    this.name = "";
    this.seconds = 120;
    this.result = "";
  }

  connectedCallback() {
    super.connectedCallback();
    this.load();
  }

  async load() {
    if (!this.editable) return;
    try {
      this.items = await api.get("/api/fixtures");
    } catch (err) {
      this.result = String(err);
    }
  }

  async save() {
    try {
      const r = await api.post("/api/fixtures", { name: this.name, seconds: Number(this.seconds) });
      this.result = `wrote ${r.records} records to ${r.path}; serial leaks: ${r.leaks.serial}`;
      this.name = "";
      await this.load();
    } catch (err) {
      this.result = `error: ${err.message}`;
    }
  }

  render() {
    return html`
      <div class="save">
        <span>Save the last</span>
        <input type="number" min="1" style="width: 80px" .value=${String(this.seconds)} @input=${(e) => (this.seconds = e.target.value)}>
        <span>seconds (${this.ring ?? 0} messages buffered) as</span>
        <input type="text" placeholder="scenario name, e.g. mowing-start" .value=${this.name} @input=${(e) => (this.name = e.target.value)}>
        <button class="primary" ?disabled=${!this.editable || !this.name} @click=${this.save}>Save redacted fixture</button>
        <span class="muted">serial, MAC, IP, Wi-Fi and coordinates are redacted; replayable through the simulator</span>
      </div>
      <div class="muted">${this.result}${this.editable ? "" : "start the Studio with --protocol-dir to save fixtures"}</div>
      <div class="panel">
        <table>
          <thead><tr><th>firmware</th><th>name</th><th class="num">records</th><th class="num">bytes</th><th>path</th></tr></thead>
          <tbody>${this.items.map((i) => html`<tr><td>${i.firmware}</td><td>${i.name}</td><td class="num">${i.records}</td><td class="num">${i.bytes}</td><td class="muted">${i.path}</td></tr>`)}</tbody>
        </table>
      </div>
    `;
  }
}

class ConsolePane extends LitElement {
  static properties = { commands: { state: true }, name: { state: true }, payload: { state: true }, result: { state: true }, busy: { state: true } };
  static styles = [
    shared,
    css`
      :host { display: grid; grid-template-columns: 1fr 1.2fr; gap: 12px; height: 100%; }
      .form { display: flex; flex-direction: column; gap: 8px; }
      textarea { min-height: 120px; }
      .panel { padding: 8px; }
    `,
  ];

  constructor() {
    super();
    this.commands = [];
    this.name = "";
    this.payload = "{}";
    this.result = null;
    this.busy = false;
  }

  connectedCallback() {
    super.connectedCallback();
    api.get("/api/knowledge").then((k) => {
      this.commands = k.commands;
      const first = k.commands.find((c) => c.status === "verified");
      if (first) this.pick(first.name);
    });
  }

  pick(name) {
    this.name = name;
    const cmd = this.commands.find((c) => c.name === name);
    this.payload = JSON.stringify(cmd?.payload ?? {}, null, 2);
  }

  async send() {
    let payload;
    try {
      payload = JSON.parse(this.payload || "null");
    } catch (err) {
      this.result = { error: `payload is not JSON: ${err.message}` };
      return;
    }
    this.busy = true;
    try {
      this.result = await api.post("/api/command", { name: this.name, payload });
    } catch (err) {
      this.result = { error: err.message };
    } finally {
      this.busy = false;
    }
  }

  render() {
    const verified = this.commands.filter((c) => c.status === "verified");
    const candidates = this.commands.filter((c) => c.status !== "verified");
    const cmd = this.commands.find((c) => c.name === this.name);
    return html`
      <div class="form">
        <h3>Send a verified command</h3>
        <select @change=${(e) => this.pick(e.target.value)}>
          ${verified.map((c) => html`<option value=${c.name} ?selected=${c.name === this.name}>${c.name} (${c.ack})</option>`)}
        </select>
        <div class="muted">${cmd?.notes ?? ""}</div>
        <textarea .value=${this.payload} @input=${(e) => (this.payload = e.target.value)}></textarea>
        <div class="row">
          <button class="primary" ?disabled=${this.busy || !this.name} @click=${this.send}>Send</button>
          <span class="muted">${candidates.length} candidates are listed but cannot be sent from here until verified</span>
        </div>
        <h3 style="margin-top: 12px">Candidates</h3>
        <div class="panel" style="max-height: 40%">
          <table><tbody>${candidates.map((c) => html`<tr><td>${c.name}</td><td><span class="tag candidate">${c.status}</span></td><td class="muted">${c.risk}${c.controller ? ", controller" : ""}${c.awake ? ", awake" : ""}</td></tr>`)}</tbody></table>
        </div>
      </div>
      <div>
        <h3>Reply</h3>
        <div class="panel">
          ${this.result
            ? this.result.error
              ? html`<span class="bad">${this.result.error}</span>`
              : html`<div class="row"><span class=${this.result.ok ? "ok" : "bad"}>state ${this.result.state ?? "-"}</span><span>${this.result.msg}</span><span class="muted">${this.result.latency_ms ?? "?"} ms</span></div>
                <pre>${JSON.stringify(this.result.data, null, 2)}</pre>`
            : html`<span class="muted">no command sent yet</span>`}
        </div>
      </div>
    `;
  }
}

customElements.define("studio-app", StudioApp);
customElements.define("stream-pane", StreamPane);
customElements.define("knowledge-pane", KnowledgePane);
customElements.define("fixtures-pane", FixturesPane);
customElements.define("console-pane", ConsolePane);
