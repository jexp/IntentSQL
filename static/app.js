const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
function compact(value) {
  if (value == null || typeof value !== "object") return String(value);
  if (Array.isArray(value)) return value.map(compact).join(", ");
  if (value.column && value.operator) return `${value.table ? value.table + "." : ""}${value.column} ${value.operator}${value.value == null ? "" : " " + JSON.stringify(value.value)}`;
  if (value.join) return `${value.join}${value.on ? " · " + value.on : ""}`;
  if (value.table) return value.table;
  if (value["group by"]) return `Group by ${value["group by"]}`;
  if (value.operation && value.column) return `${value.operation} · ${value.column}`;
  if (value.operation) return value.operation;
  if (Object.hasOwn(value,"value")) return `${JSON.stringify(value.value)}${value.column ? ` · ${value.column}` : ""}`;
  if (value.column) return value.column;
  if (value.operator) return value.operator;
  return JSON.stringify(value);
}
const pct = (value) => Number.isFinite(Number(value)) ? `${(Number(value) * 100).toFixed(0)}%` : "—";
let mode = "auto", running = false, startAt = 0, timer = null, calls = 0;
let currentSQL = "", currentToken = null, currentUndo = null, readResult = null, readPage = 0;
let connectionProfiles = {};
let connectionPresets = {};
let connectionPending = false;
let runEvents = [], terminalReceived = false, completedSkills = 0, runDetailCount = 0;
let providerCalls = new Map(), pendingProviderCallIds = [];
let schemaVersion = 0;
let databasesById = new Map();
let schemaTablesByName = new Map();
const phases = ["inspect", "resolve", "compile", "execute"];
function setPhase(phase, failed = false) {
  const index = phases.indexOf(phase);
  document.querySelectorAll("[data-phase]").forEach((node, i) => {
    node.classList.toggle("done", phase === "done" || i < index);
    node.classList.toggle("active", i === index && !failed);
    node.classList.toggle("failed", i === index && failed);
    node.querySelector("span").textContent = phase === "done" || i < index ? "✓" : String(i + 1).padStart(2, "0");
  });
}
function resetResult() {
  document.body.classList.remove("has-result");
  $("#output").classList.remove("result-arrived");
  currentToken = null; readResult = null; readPage = 0;
  $("#export-csv").disabled = true;
  $("#row-count").textContent = "—";
}
function resetRunDetails() {
  runDetailCount = 0;
  $("#run-detail-count").textContent = "0 events";
  $("#run-detail-list").innerHTML = '<p class="run-detail-empty">Live build details will be retained here after the result arrives.</p>';
  $("#run-details").open = false;
}
function recordRunDetail(label, value, meta = "") {
  const list = $("#run-detail-list");
  if (!list) return;
  list.querySelector(".run-detail-empty")?.remove();
  runDetailCount += 1;
  $("#run-detail-count").textContent = `${runDetailCount} event${runDetailCount === 1 ? "" : "s"}`;
  const elapsed = startAt ? `${Math.max(0, (performance.now() - startAt) / 1000).toFixed(2)}s` : "";
  const body = value && typeof value === "object"
    ? `<pre>${esc(JSON.stringify(value, null, 2))}</pre>`
    : `<p>${esc(value)}</p>`;
  list.insertAdjacentHTML("beforeend", `<details class="run-detail"><summary><span>${esc(label)}</span><small>${esc(meta || elapsed)}</small></summary>${body}</details>`);
}
function setTraceDetails(open) {
  document.querySelectorAll("#trace .trace-call > details.decision-details").forEach(node => { node.open = open; });
}
function download(name, content, type) {
  const url = URL.createObjectURL(new Blob([content], {type}));
  const link = document.createElement("a"); link.href = url; link.download = name; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function activeDecision(message, inFlight = false) {
  const node = $("#active-decision");
  node.textContent = message;
  node.title = message;
  node.classList.toggle("in-flight", inFlight);
}
function clearQuery() {
  resetResult(); renderSQL(null); resetRunDetails(); renderStats(); setPhase(null);
  runEvents = []; completedSkills = 0; currentUndo = null;
  providerCalls = new Map(); pendingProviderCallIds = [];
  $("#export-trace").disabled = true;
  $("#typed-program").open = false;
  $("#program-status").textContent = "Waiting";
  $("#program").innerHTML = '<p class="muted small">Resolved query state will appear here.</p>';
  $("#trace").innerHTML = '<div class="empty"><strong>A new source, a fresh start</strong><p>Run a request to see its semantic decisions.</p></div>';
  $("#output").innerHTML = '<div class="empty"><strong>Ready to explore</strong><p>Ask a question about the selected database.</p></div>';
  $("#row-count-label").textContent = "rows";
  $("#output-title").textContent = "Results";
  $("#result-summary").textContent = $("#database").selectedOptions[0]?.textContent || "Select a data source";
  $("#elapsed").textContent = "\u2014";
  activeDecision("Most recent first · expand to inspect alternatives.");
  notice("Source selected · ask a question below.");
}
function setBusy(busy) {
  document.body.classList.toggle("is-running", busy);
  for (const selector of ["#run", "#database", "#delete-db", "#save-connection", "#import-db", ".sample"]) {
    document.querySelectorAll(selector).forEach(node => node.disabled = busy);
  }
  $("#prompt").readOnly = busy;
  $("#run").innerHTML = busy ? 'Resolving <span aria-hidden="true">◌</span>' : 'Explore query <span aria-hidden="true">↗</span>';
  $(".journey").setAttribute("aria-busy", String(busy));
}

function notice(message, kind = "ready") {
  $("#notice").className = `notice ${kind}`;
  $("#notice-text").textContent = message;
  $("#notice-text").title = message;
}
function error(message) {
  document.body.classList.add("has-result");
  notice(message, "error");
  activeDecision("Recorded decisions / most recent first");
  $("#output").innerHTML = `<div class="error-state"><strong>We couldn't complete this request</strong><p>${esc(message)}</p><p>Inspect the recorded semantic decisions for details. Revise the request or connection settings and try again.</p></div>`;
  $("#row-count").textContent = "—";
  $("#result-summary").textContent = "No result was produced.";
  $("#program-status").textContent = "Stopped";
  document.querySelector(".pipeline .active")?.classList.add("failed");
  document.querySelector(".pipeline .active")?.classList.remove("active");
}
async function jsonRequest(path, options = {}) {
  const response = await fetch(path, {cache:"no-store", ...options});
  const value = await response.json();
  if (!response.ok) throw new Error(typeof value.detail === "string" ? value.detail : `HTTP ${response.status}`);
  return value;
}
async function loadDatabases(prefer) {
  const databases = await jsonRequest("/api/databases");
  databasesById = new Map(databases.map(database => [database.id, database]));
  const selector = $("#database");
  const prior = prefer || selector.value;
  selector.innerHTML = databases.map(db => `<option value="${esc(db.id)}">${esc(db.label)}</option>`).join("");
  if (databases.some(db => db.id === prior)) selector.value = prior;
  syncDatabaseActions();
  if (selector.value !== prior || prefer) clearQuery();
  await loadSchema();
}
async function loadSchema() {
  const database = $("#database").value;
  if (!database) return;
  const version = ++schemaVersion;
  $("#schema").innerHTML = '<p class="muted small">Inspecting schema…</p>';
  const data = await jsonRequest(`/api/schema/${encodeURIComponent(database)}`);
  if (version !== schemaVersion) return;
  schemaTablesByName = new Map(data.tables.map(table => [table.name, table]));
  $("#source-caption").textContent = `${data.tables.length} tables · ${data.tables.reduce((sum, table) => sum + table.rows, 0).toLocaleString()} rows`;
  const subjects = data.tables.slice(0, 3).map(table => table.name).join(", ");
  $("#prompt").placeholder = subjects ? `Ask about ${subjects}… or choose an example` : "Write your question about this database here…";
  $("#table-count").textContent = `${data.tables.length}`;
  $("#schema").innerHTML = data.tables.map(table => `
    <details class="schema-table"><summary><strong>${esc(table.name)}</strong><small>${table.rows.toLocaleString()} rows</small></summary>
    <div class="schema-table-actions"><button class="link-button inspect-table" type="button" data-table="${esc(table.name)}">Preview rows / query</button></div>
    <div class="columns">${table.columns.map(col => `<div class="column"><b>${esc(col.name)}${col.pk ? " · PK" : ""}</b><span>${esc(col.declared_type || col.kind)}</span></div>`).join("")}</div></details>`).join("");
  for (const button of document.querySelectorAll(".inspect-table")) {
    button.addEventListener("click", () => openInspector(button.dataset.table));
  }
}
async function loadConnection() {
  const data = await jsonRequest("/api/connection");
  connectionPending = false;
  connectionProfiles = data.profiles;
  connectionPresets = data.presets;
  $("#provider").value = data.provider;
  $("#endpoint").value = data.url;
  $("#model").value = data.model;
  $("#api-key").value = "";
  syncEndpointAddress();
  updateKeyStatus();
  const ready = data.has_key || !data.requires_key;
  const engineLabel = [data.name, data.model].filter(Boolean).join(" · ");
  $("#active-engine span").textContent = engineLabel || data.provider || "Connection";
  $("#active-engine").title = `${engineLabel} · ${data.url}`;
  $("#active-engine").classList.toggle("ready", ready);
  $("#connection-status").classList.toggle("ready", ready);
  $("#connection-status span:last-child").textContent = ready
    ? `${data.name} · ${data.has_key ? "key configured" : "no key required"}`
    : `${data.name} · add an API key`;
  if (!ready) $("#connection-details").open = true;
}
function syncDatabaseActions() {
  const selected = databasesById.get($("#database").value);
  $("#delete-db").hidden = !selected?.deletable;
}
function syncEndpointAddress() {
  $("#endpoint-host").setCustomValidity("");
  try {
    const url = new URL($("#endpoint").value);
    $("#endpoint-host").value = url.hostname;
    $("#endpoint-port").value = url.port;
  } catch {
    $("#endpoint-host").value = "";
    $("#endpoint-port").value = "";
  }
}
function updateKeyStatus() {
  const profile = connectionProfiles[$("#provider").value];
  $("#key-status").textContent = profile?.has_key
    ? "A key is configured for this provider. Leave blank to keep it, or enter a replacement."
    : profile?.requires_key ? "This provider requires an API key." : "Model and API key are optional for this provider.";
}
function connectionEdited() {
  connectionPending = true;
  $("#settings-feedback").textContent = "Unsaved changes · closing discards edits.";
  $("#settings-feedback").classList.remove("error-text");
}
async function saveConnection() {
  for (const selector of ["#endpoint", "#endpoint-host", "#endpoint-port"]) {
    if (!$(selector).reportValidity()) return;
  }
  const button = $("#save-connection");
  button.disabled = true;
  button.textContent = "Saving…";
  try {
    await jsonRequest("/api/connection", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({
      provider:$("#provider").value, url:$("#endpoint").value,
      model:$("#model").value, api_key:$("#api-key").value,
    })});
    await loadConnection();
    $("#settings-feedback").textContent = "Connection saved.";
    notice("Connection saved locally");
    $("#settings-dialog").close();
  } catch (exc) {
    $("#settings-feedback").textContent = exc.message;
    $("#settings-feedback").classList.add("error-text");
  } finally {
    button.disabled = false;
    button.textContent = "Save connection";
  }
}
function chips(label, values) {
  if (!values?.length) return "";
  return `<div class="program-line"><label>${esc(label)}</label><div class="chips">${values.map(v => `<span class="chip">${esc(compact(v))}</span>`).join("")}</div></div>`;
}
function renderProgram(program) {
  if (!program) return;
  if (program.resolving) {
    $("#program-status").textContent = "Resolving";
    $("#typed-program").open = true;
    $("#sql").hidden = true;
    $("#query-label").textContent = "QUERY TAKING SHAPE";
    $("#program").innerHTML = '<p class="draft-label">Resolved semantic state · compiler input is still developing</p>' +
      Object.entries(program.decisions).map(([name,value]) => `<div class="draft-decision"><span>${esc(name)}</span><b>${esc(name === "Route" ? Object.entries(value).filter(([,v]) => v !== false && v !== "none").map(([k,v]) => v === true ? k : `${k}: ${v}`).join(" · ") : compact(value))}</b></div>`).join("");
    $(".sql-box").scrollTop = $(".sql-box").scrollHeight;
    return;
  }
  const mutation = !!program.operation && program.operation !== "SELECT";
  let html = "";
  if (mutation) {
    html += chips("Operation", [program.operation]);
    html += chips("Table", [program.table]);
    html += chips("Assignments", Object.entries(program.assignments || {}).map(([key, value]) => `${key} = ${JSON.stringify(value)}`));
    html += chips("Condition", program.where_column ? [`${program.where_column} = ${JSON.stringify(program.where_value)}`] : []);
  } else {
    html += chips("Source", [program.base_table]);
    html += chips("Joins", program.joins);
    html += chips("Outputs", program.outputs);
    html += chips(`Filters (${program.filter_connector || "AND"})`, program.filters);
    html += chips("Groups", program.groups);
    html += chips("Having", program.having);
    html += chips("Order", (program.order_by || []).map(v => `${v.key} ${v.direction}`));
    html += chips("Limit", program.limit == null ? [] : [program.limit]);
    html += chips("Distinct", program.distinct === "UNSET" ? [] : [String(program.distinct)]);
    html += chips("Extremum", program.extremum ? [program.extremum] : []);
  }
  if (program.typed_query) html += `<details><summary>Inspect typed query JSON</summary><pre>${esc(JSON.stringify(program.typed_query, null, 2))}</pre></details>`;
  $("#program").innerHTML = html || `<div class="empty"><strong>Choosing a source…</strong></div>`;
  $("#program-status").textContent = mutation ? "Change plan" : "Compiled";
  $(".sql-box").scrollTop = 0;
  $("#typed-program").open = false;
}
function sqlLiteral(value) {
  if (value == null) return "NULL";
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  if (typeof value === "boolean") return value ? "1" : "0";
  return `'${String(value).replaceAll("'", "''")}'`;
}
function displaySQL(sql, params = []) {
  if (!sql) return "";
  let result = "", quote = null, paramIndex = 0;
  for (let i = 0; i < sql.length; i++) {
    const char = sql[i];
    if (quote) {
      result += char;
      if (char === quote) {
        if (sql[i + 1] === quote) result += sql[++i];
        else quote = null;
      }
      continue;
    }
    if (char === "'" || char === '"') { quote = char; result += char; continue; }
    if (char === "?" && paramIndex < params.length) { result += sqlLiteral(params[paramIndex++]); continue; }
    result += char;
  }
  return result;
}
function highlightSQL(sql) {
  if (!sql) return "";
  const token = /("(?:""|[^"])*"|'(?:''|[^'])*'|\b(?:SELECT|DISTINCT|FROM|WHERE|JOIN|ON|AND|OR|NOT|NULL|IS|AS|GROUP BY|ORDER BY|HAVING|LIMIT|ASC|DESC|BETWEEN|IN|LIKE|ESCAPE|COUNT|SUM|AVG|MIN|MAX|ROUND|INSERT INTO|UPDATE|DELETE FROM|VALUES|SET)\b|\b-?\d+(?:\.\d+)?\b|\?)/gi;
  return sql.split(token).map(part => {
    if (!part) return "";
    if (part.startsWith('"')) return `<span class="sql-identifier">${esc(part)}</span>`;
    if (part.startsWith("'")) return `<span class="sql-value sql-string">${esc(part)}</span>`;
    if (part === "?") return '<span class="sql-param">?</span>';
    if (/^-?\d+(?:\.\d+)?$/.test(part)) return `<span class="sql-value sql-number">${esc(part)}</span>`;
    token.lastIndex = 0;
    return token.test(part) ? `<span class="sql-keyword">${esc(part)}</span>` : esc(part);
  }).join("");
}
function formatSQL(sql) {
  // Match quoted literals/identifiers first so their contents remain untouched.
  return sql.replace(/("(?:""|[^"])*"|'(?:''|[^'])*')|\s+(FROM|WHERE|JOIN|ON|GROUP BY|HAVING|ORDER BY|LIMIT|SET|VALUES)\b/gi,
    (match, quoted, clause) => quoted || `\n${clause.toUpperCase()}`).trim();
}
function renderSQL(sql, params = []) {
  currentSQL = sql ? formatSQL(displaySQL(sql, params)) : "";
  $("#sql").hidden = false;
  $("#query-label").textContent = sql ? "READABLE SQL" : "QUERY CONSTRUCTION";
  $("#query-kind").textContent = sql ? "DISPLAY" : "DETERMINISTIC";
  $("#sql").innerHTML = sql ? highlightSQL(currentSQL) : "SQL appears when the typed program is ready.";
  const details = $("#execution-details");
  details.hidden = !sql;
  if (sql) {
    $("#parameterized-sql").innerHTML = highlightSQL(formatSQL(sql));
    $("#params").innerHTML = params.length
      ? params.map((value, i) => `<span class="bound-value"><b>${i + 1}</b><code>${esc(sqlLiteral(value))}</code></span>`).join("")
      : '<span class="no-bound-values">No bound values for this query.</span>';
  } else {
    $("#parameterized-sql").textContent = "";
    $("#params").textContent = "";
    details.open = false;
  }
  $("#copy-sql").disabled = !currentSQL;
}
function displayColumn(name, program) {
  let label = String(name);
  if (label === "row_count") return "Count";
  const aggregate = label.match(/^(count_distinct|count|avg|sum|min|max)_(.+)$/i);
  if (aggregate) label = aggregate[2];
  const tables = [...(program?.joined_tables || [])].sort((a,b) => b.length-a.length);
  for (const table of tables) if (label.startsWith(`${table}_`)) { label = label.slice(table.length + 1); break; }
  label = label.replaceAll("_", " ");
  label = label === "id" ? "ID" : label.charAt(0).toUpperCase() + label.slice(1);
  const names = {count:"Count",count_distinct:"Unique",avg:"Average",sum:"Total",min:"Minimum",max:"Maximum"};
  const detail = label === label.toUpperCase() ? label : label.charAt(0).toLowerCase() + label.slice(1);
  return aggregate ? `${names[aggregate[1].toLowerCase()]} ${detail}` : label;
}
function renderTable(columns, rows, start = 0, limit = 100, showNote = true, program = null) {
  if (!rows?.length) return `<div class="empty-result">No matching rows. The query completed successfully.</div>`;
  const names = columns || Object.keys(rows[0]);
  return `<div class="table-wrap"><table><thead><tr>${names.map(name => `<th title="${esc(name)}">${esc(displayColumn(name, program))}</th>`).join("")}</tr></thead><tbody>${rows.slice(start, start + limit).map(row => `<tr>${names.map((name, i) => (() => { const value = Array.isArray(row) ? row[i] : row[name]; return `<td class="${typeof value === "number" ? "numeric" : ""}" title="${esc(value ?? "NULL")}">${value == null ? '<span class="null-value">NULL</span>' : esc(value)}</td>`; })()).join("")}</tr>`).join("")}</tbody></table></div>${showNote && rows.length > limit ? `<p class="muted small">Showing first ${limit} of ${rows.length} rows.</p>` : ""}`;
}
function renderReadPage() {
  const target = $("#read-page");
  if (!target || !readResult) return;
  const loaded = readResult.rows.length, total = readResult.total_rows ?? loaded;
  const start = readPage * 100, end = Math.min(start + 100, loaded);
  target.innerHTML = renderTable(readResult.columns, readResult.rows, start, 100, false, readResult.program) +
    `<div class="table-meta"><span>Rows ${loaded ? start + 1 : 0}–${end} of ${total.toLocaleString()}${readResult.truncated ? ` · first ${loaded.toLocaleString()} loaded` : ""}</span><div class="page-controls"><button id="page-prev" ${readPage === 0 ? "disabled" : ""}>Previous</button><button id="page-next" ${end >= loaded ? "disabled" : ""}>Next</button></div></div>`;
  $("#page-prev").addEventListener("click", () => { readPage--; renderReadPage(); });
  $("#page-next").addEventListener("click", () => { readPage++; renderReadPage(); });
}
function vetHTML(vet) {
  if (!vet) return "";
  const passed = vet.passed ?? (vet.raw?.verdict?.choice === "PASS" && Math.min(...Object.values(vet.checks || {unknown:0})) >= .72);
  const checks = Object.entries(vet.checks || {}).map(([key, value]) => `${key.replaceAll("_", " ")} ${pct(value)}`).join(" · ");
  return `<div class="vet-banner ${passed ? "" : "fail"}">${passed ? "✓ Semantic vet passed" : "✕ Semantic vet did not pass"}${checks ? ` · ${esc(checks)}` : ""}</div>`;
}
function renderStats(stats = {}) {
  const cost = stats.cost_usd;
  const items = [
    ["CALLS", String(stats.jev_calls || 0), "attempted"],
    ["INPUT TOKENS", (stats.input_tokens || 0).toLocaleString(), "reported"],
    ["OUTPUT TOKENS", (stats.output_tokens || 0).toLocaleString(), "reported"],
    ["EST. COST / USD", cost == null ? "—" : `$${Number(cost).toFixed(6)}`, cost == null ? "unavailable" : "cumulative"],
  ];
  $("#stats").innerHTML = items.map(([label,value,detail]) => `<div class="stat"><label>${label}</label><strong>${value}</strong><small>${detail}</small></div>`).join("") +
    (stats.usage_complete === false ? '<div class="usage-warning">Some call usage was not reported. Totals and estimated cost may be incomplete.</div>' : '');
  calls = Number(stats.jev_calls || 0);
  $("#call-count").textContent = `${calls} call${calls === 1 ? "" : "s"}`;
}

function quotedIdentifier(name) { return `"${String(name).replaceAll('"', '""')}"`; }
function recentRowsSQL(table, limit = 20) {
  const primaryKey = (schemaTablesByName.get(table)?.columns || [])
    .filter(column => column.pk).map(column => column.name);
  const ordering = primaryKey.length
    ? primaryKey.map(name => `${quotedIdentifier(name)} DESC`).join(", ")
    : "rowid DESC";
  return `SELECT * FROM ${quotedIdentifier(table)} ORDER BY ${ordering} LIMIT ${limit};`;
}
function inspectorForeignKeys(table) {
  if (!table.foreign_keys?.length) return "";
  return `<div class="inspector-fks"><strong>Foreign keys</strong>${table.foreign_keys.map(fk =>
    `<span>${esc(fk.from_column)} → ${esc(fk.to_table)}.${esc(fk.to_column)}</span>`).join("")}</div>`;
}
function renderInspectorOverview(data) {
  if (!data.tables?.length) return `<div class="empty compact-empty"><span>▦</span><strong>No tables</strong><p>This database has no user tables to preview.</p></div>`;
  return `<div class="inspector-overview-head"><div><span class="eyebrow">DATABASE OVERVIEW</span><h3>${data.tables.length} table${data.tables.length === 1 ? "" : "s"}</h3></div><span class="muted small">Highest ${data.preview_rows} primary-key / rowid rows · no System One calls</span></div>` +
    `<div class="inspector-preview-list">${data.tables.map(table => {
      const columnNames = table.columns.map(col => col.name);
      const schema = table.columns.map(col => `<span class="chip">${esc(col.name)}${col.pk ? " · PK" : ""} · ${esc(col.declared_type || col.kind)}</span>`).join("");
      return `<section class="inspector-preview-card">
        <div class="inspector-preview-head"><div><h3>${esc(table.name)}</h3><span>${Number(table.row_count).toLocaleString()} rows</span></div><button class="link-button overview-query-table" type="button" data-table="${esc(table.name)}">Query this table</button></div>
        <div class="chips inspector-schema-chips">${schema}</div>
        ${inspectorForeignKeys(table)}
        ${renderTable(columnNames, table.rows, 0, data.preview_rows, false)}
      </section>`;
    }).join("")}</div>`;
}
async function loadInspectorOverview(database) {
  const overview = $("#inspector-overview");
  overview.hidden = false;
  overview.innerHTML = `<div class="empty compact-empty"><span>▦</span><strong>Inspecting database…</strong><p>Loading the highest five primary-key or rowid rows from every table.</p></div>`;
  try {
    const data = await jsonRequest(`/api/inspect/${encodeURIComponent(database)}/overview`);
    overview.innerHTML = renderInspectorOverview(data);
    for (const button of overview.querySelectorAll(".overview-query-table")) {
      button.addEventListener("click", async () => {
        const table = button.dataset.table;
        $("#inspector-sql").value = recentRowsSQL(table);
        await runInspectorQuery();
      });
    }
  } catch (exc) {
    overview.innerHTML = `<div class="query-error">${esc(exc.message)}</div>`;
  }
}
async function runInspectorQuery() {
  const database = $("#database").value;
  const sql = $("#inspector-sql").value.trim();
  if (!database || !sql) return;
  const button = $("#run-inspector-query"), errorBox = $("#inspector-error");
  button.disabled = true; button.textContent = "Running…";
  errorBox.hidden = true; errorBox.textContent = "";
  $("#inspector-meta").textContent = "Running read-only query…";
  try {
    const data = await jsonRequest(`/api/inspect/${encodeURIComponent(database)}`, {
      method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({sql})
    });
    $("#inspector-output").innerHTML = renderTable(data.columns, data.rows, 0, data.max_rows, false);
    $("#inspector-meta").textContent = `${data.rows.length.toLocaleString()} row${data.rows.length === 1 ? "" : "s"} returned${data.truncated ? ` · truncated at ${data.max_rows.toLocaleString()}` : ""} · ${data.elapsed_ms} ms`;
  } catch (exc) {
    errorBox.hidden = false; errorBox.textContent = exc.message;
    $("#inspector-meta").textContent = "Query failed · database unchanged";
    $("#inspector-output").innerHTML = `<div class="empty"><span>!</span><strong>SQLite rejected the query</strong><p>Fix the SQL above and run it again.</p></div>`;
  } finally {
    button.disabled = false; button.innerHTML = `Run query <span aria-hidden="true">↗</span>`;
  }
}
async function openInspector(table) {
  const database = $("#database").value;
  $("#inspector-title").textContent = table ? `Inspect ${table}` : "Database inspector";
  $("#inspector-subtitle").textContent = table
    ? `${database} · read-only working copy · newest keys first`
    : `${database} · highest five primary-key / rowid rows, plus a read-only SQL console`;
  $("#inspector-sql").value = table ? recentRowsSQL(table) : "";
  $("#inspector-error").hidden = true;
  $("#inspector-meta").textContent = "";
  const overview = $("#inspector-overview");
  overview.hidden = !!table;
  overview.innerHTML = "";
  $("#db-inspector-dialog").showModal();
  if (table) {
    await runInspectorQuery();
  } else {
    $("#inspector-output").innerHTML = `<div class="empty compact-empty"><span>⌘</span><strong>SQL console</strong><p>Use the editor below when you want to inspect beyond the automatic previews.</p></div>`;
    await loadInspectorOverview(database);
  }
}
function renderResult(result) {
  $("#export-csv").disabled = !result.supported || !result.rows?.length;
  renderProgram(result.program);
  renderSQL(result.sql, result.params);
  renderStats(result.stats);
  if (!result.program?.operation || result.program.operation === "SELECT") {
    $("#output-title").textContent = "Results";
    const total = result.total_rows ?? result.rows?.length ?? 0;
    $("#row-count").textContent = result.supported ? total.toLocaleString() : "—";
    $("#row-count-label").textContent = result.supported ? (total === 1 ? "row returned" : "rows returned") : "no result";
    $("#result-summary").textContent = result.supported
      ? `${result.columns.length} output column${result.columns.length === 1 ? "" : "s"} · SQLite result${result.truncated ? ` · first ${result.rows.length.toLocaleString()} rows loaded` : ""}`
      : "The proposed program did not pass semantic review. No query was executed.";
    readResult = result.supported ? result : null; readPage = 0;
    if (!result.supported) {
      $("#output").innerHTML = `<p class="warning">No result was executed. Inspect the trace and SQL, then try a clearer request.</p>` + vetHTML(result.vet);
    } else if (result.rows.length === 1 && result.columns.length === 1) {
      $("#output").innerHTML = `<div class="scalar-answer"><strong>${esc(result.rows[0][0] ?? "NULL")}</strong><span title="${esc(result.columns[0])}">${esc(displayColumn(result.columns[0], result.program))}</span></div>` + vetHTML(result.vet);
    } else {
      $("#output").innerHTML = `<div id="read-page"></div>` + vetHTML(result.vet);
      renderReadPage();
    }
  } else {
    readResult = null;
    currentToken = result.commit_token || null;
    $("#output-title").textContent = "Proposed change";
    $("#row-count").textContent = result.supported ? Number(result.affected || 0).toLocaleString() : "—";
    $("#row-count-label").textContent = "rows proposed";
    $("#result-summary").textContent = result.supported ? "Preview only · no database write yet" : "The change was rejected before execution.";
    $("#output").innerHTML = vetHTML(result.vet) + (result.supported ? `
      <p class="muted small">${result.affected} row${result.affected === 1 ? "" : "s"} would change. This is an in-memory preview; the database has not been written.</p>
      <div class="preview-grid"><div><h3>Before</h3>${renderTable(null, result.before)}</div><div><h3>After</h3>${renderTable(null, result.after)}</div></div>
      <div class="commit-row"><span class="warning">Review the SQL, parameters and rows before committing.</span><button id="commit" class="button danger" type="button">Review & commit</button></div>`
      : `<p class="warning">Nothing was written. Revise your request with exact quoted values and a narrow identifier.</p>`);
    $("#commit")?.addEventListener("click", () => { $("#confirm-summary").textContent = `${result.program.operation} ${result.affected} row(s) in ${result.program.table}.`; $("#confirm-dialog").showModal(); });
  }
}
function jevQuestionType(question) {
  return String(question?.type || "unknown").toLowerCase();
}
function jevTypeBadge(type) {
  const normalized = String(type || "unknown").toLowerCase();
  const label = normalized === "noul" ? "NOUL" : normalized === "choice" ? "CHOICE" : normalized === "score" ? "SCORE" : normalized.toUpperCase();
  return `<span class="jev-type-badge ${esc(normalized)}">${esc(label)}</span>`;
}
function providerQuestionEntries(call) {
  const questions = call?.request?.questions || call?.questions || {};
  return Object.entries(questions).map(([name, question]) => ({name, question: question || {}}));
}
function providerCallTypes(calls) {
  return [...new Set((calls || []).flatMap(call => providerQuestionEntries(call).map(({question}) => jevQuestionType(question))))];
}
function providerQuestionPreview(calls) {
  const entries = (calls || []).flatMap(call => providerQuestionEntries(call).map(item => ({...item, call:call.call})));
  if (!entries.length) return "";
  const first = entries[0];
  const instruction = first.question?.instructions || first.name.replaceAll("_", " ");
  const badges = providerCallTypes(calls).map(jevTypeBadge).join("");
  const more = entries.length > 1 ? `<small>+${entries.length - 1} more exact question${entries.length === 2 ? "" : "s"} inside</small>` : "";
  return `<div class="jev-question-preview"><div class="jev-question-preview-head">${badges}<span>Exact Jev question</span></div><p>${esc(instruction)}</p>${more}</div>`;
}
function rawProviderRequest(call) {
  return call?.request || {state:call?.state, questions:call?.questions};
}
function rawProviderResponse(call) {
  if (call?.response) return call.response;
  if (call?.error) return {error:call.error};
  if (call?.answers || call?.usage) return {answers:call.answers || {}, usage:call.usage || {}};
  return {status:"Response not recorded yet"};
}
function renderProviderExchange(calls) {
  if (!calls?.length) return "";
  return calls.map(call => {
    const entries = providerQuestionEntries(call);
    const types = [...new Set(entries.map(({question}) => jevQuestionType(question)))];
    const answers = call?.response?.answers || call?.answers || {};
    const questions = entries.map(({name,question}) => {
      const answer = answers[name] || {};
      const type = jevQuestionType(question);
      const selected = answer[type] ?? answer.choice ?? answer.noul ?? answer.score;
      const confidence = answer.confidence;
      const probabilities = answer.probabilities || answer.distribution || {};
      const alternatives = Object.entries(probabilities).filter(([,value]) => Number.isFinite(Number(value)))
        .sort((a,b) => Number(b[1])-Number(a[1])).slice(0,8)
        .map(([label,value]) => `<div class="call-probability${String(label) === String(selected) ? " selected" : ""}"><div class="call-probability-main"><span title="${esc(label)}">${esc(label)}</span><b>${evidencePercent(value)}</b></div><i aria-hidden="true"><span style="width:${Math.max(0,Math.min(100,Number(value)*100))}%"></span></i></div>`).join("");
      const interpreted = question.criteria?.[selected];
      const hasConfidence = confidence != null && Number.isFinite(Number(confidence));
      const confidenceValue = hasConfidence ? Math.max(0,Math.min(100,Number(confidence)*100)) : 0;
      const returned = call.ended || call.error
        ? `<div class="jev-return"><div class="jev-result-row"><div class="jev-answer">${call.error ? `<span>Provider error</span>` : ""}<strong>${esc(call.error || compact(selected ?? answer))}</strong></div>${hasConfidence ? `<div class="confidence-summary"><div><span>Confidence</span><strong>${evidencePercent(confidence)}</strong></div><div class="confidence-meter" role="meter" aria-label="Confidence" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${confidenceValue.toFixed(0)}"><span style="width:${confidenceValue}%"></span></div></div>` : ""}</div>${interpreted != null ? `<div class="jev-interpretation"><p>${esc(compact(interpreted))}</p></div>` : ""}${alternatives ? `<div class="call-probabilities"><div class="call-probabilities-title">Alternatives</div>${alternatives}</div>` : ""}</div>`
        : '<div class="jev-return pending"><span>Jev is deciding…</span></div>';
      return `<div class="jev-question-item"><div class="jev-question-key">${jevTypeBadge(type)}<code>${esc(name)}</code></div><p>${esc(question?.instructions || "No instruction text recorded.")}</p>${returned}</div>`;
    }).join("");
    const meta = `${entries.length} question${entries.length === 1 ? "" : "s"}${call.latency_ms != null ? ` · ${call.latency_ms} ms` : ""}`;
    const input = call?.request?.state ?? call?.state;
    return `<section class="jev-exchange"><div class="jev-exchange-head"><strong>Call ${esc(call.call)}</strong><span>${types.map(type => jevTypeBadge(type)).join("")}<small>${esc(meta)}</small></span></div>${input != null ? `<details class="raw-jev-exchange"><summary>Input for this call</summary><pre>${esc(JSON.stringify(input,null,2))}</pre></details>` : ""}<div class="jev-question-list">${questions}</div><details class="raw-jev-exchange"><summary>Raw provider exchange</summary><div class="raw-exchange-grid"><details><summary>Sent to System One</summary><pre>${esc(JSON.stringify(rawProviderRequest(call),null,2))}</pre></details><details><summary>Received from System One</summary><pre>${esc(JSON.stringify(rawProviderResponse(call),null,2))}</pre></details></div></details></section>`;
  }).join("");
}
function completedCallsForSkill(detail) {
  if (!detail || detail.input_tokens == null) return [];
  const ready = pendingProviderCallIds.map(id => providerCalls.get(id)).filter(call => call && (call.ended || call.error));
  if (!ready.length) return [];
  const exact = ready.find(call => Number(call.usage?.input_tokens || 0) === Number(detail.input_tokens || 0)
    && Number(call.usage?.output_tokens || 0) === Number(detail.output_tokens || 0));
  let selected = exact ? [exact] : [];
  if (!selected.length && ready.length > 1) {
    const summedInput = ready.reduce((sum, call) => sum + Number(call.usage?.input_tokens || 0), 0);
    const summedOutput = ready.reduce((sum, call) => sum + Number(call.usage?.output_tokens || 0), 0);
    if (summedInput === Number(detail.input_tokens || 0) && summedOutput === Number(detail.output_tokens || 0)) selected = ready;
  }
  if (!selected.length) selected = [ready[0]];
  const selectedIds = new Set(selected.map(call => call.call));
  pendingProviderCallIds = pendingProviderCallIds.filter(id => !selectedIds.has(id));
  return selected;
}

function renderDecision(event) {
  const card = $(".trace-call:first-child");
  if (!card) return;
  const sorted = [...(event.candidates || [])].sort((a,b) => b.probability-a.probability).slice(0, 8);
  const selected = compact(event.selected);
  const rows = sorted.map(candidate => {
    const label = compact(candidate.label), active = label === selected;
    return `<div class="choice-row ${active ? "selected" : ""}"><span>${esc(label)}</span><b>${pct(candidate.probability)}</b><div class="prob-track"><div class="prob-fill" style="width:${Math.max(0,Math.min(100,Number(candidate.probability)*100))}%"></div></div></div>`;
  }).join("");
  card.insertAdjacentHTML("beforeend", `<div class="decision"><div class="decision-title"><span>${esc(event.label)}</span><span>${pct(event.probability)}</span></div><div class="decision-sub">Chosen: ${esc(selected)} · confidence ${pct(event.confidence)}${event.automatic ? " · deterministic" : ""}</div>${rows}</div>`);
  $(".trace-content").scrollTop = 0;
}
function evidencePercent(value) {
  const number = Number(value);
  return Number.isFinite(number) ? `${(number * 100).toFixed(1).replace(/\.0$/, "")}%` : "—";
}
function evidenceChoiceLabel(key, choices) {
  const value = choices && Object.hasOwn(choices, key) ? choices[key] : key;
  return value == null ? key : compact(value);
}
function evidenceSelected(key, label, selected) {
  if (Array.isArray(selected)) return selected.some(item => evidenceSelected(key, label, item));
  if (selected && typeof selected === "object") return Object.values(selected).some(item => evidenceSelected(key, label, item));
  return String(selected) === String(key) || String(selected) === String(label);
}
function renderEvidence(detail, calls = []) {
  if (!detail || typeof detail !== "object") return '<p class="evidence-note">Resolved deterministically from explicit facts or inspected schema. No model call was needed.</p>';
  const exchange = renderProviderExchange(calls);
  const groups = [];
  const probabilities = detail.probabilities || {};
  const nested = Object.values(probabilities).some(value => value && typeof value === "object");
  const sets = nested ? probabilities : Object.keys(probabilities).length ? {Choices: probabilities} : {};
  for (const [name, values] of Object.entries(sets)) {
    if (!values || typeof values !== "object") continue;
    const choices = nested ? detail.choices?.[name] : detail.choices;
    const selected = nested ? (detail.selected?.[name] ?? detail.selected) : detail.selected;
    const confidence = nested ? detail.confidence?.[name] : detail.confidence;
    const rows = Object.entries(values).filter(([,value]) => Number.isFinite(Number(value)))
      .sort((a,b) => Number(b[1]) - Number(a[1])).map(([key,value]) => {
        const label = evidenceChoiceLabel(key, choices);
        const active = evidenceSelected(key, label, selected);
        return `<div class="evidence-option${active ? " chosen" : ""}"><div class="evidence-option-head"><span>${active ? '<span class="evidence-check" aria-label="Selected">✓</span>' : ""}${esc(label)}</span><b>${evidencePercent(value)}</b></div><div class="evidence-meter"><span style="width:${Math.max(0,Math.min(100,Number(value)*100))}%"></span></div></div>`;
      }).join("");
    if (rows) groups.push(`<section class="evidence-group"><div class="evidence-group-head"><strong>${esc(name === "Choices" ? "Interpreted probabilities" : name.replaceAll("_", " "))}</strong>${Number.isFinite(Number(confidence)) && confidence != null ? `<b class="evidence-confidence">Confidence ${evidencePercent(confidence)}</b>` : ""}</div><div class="evidence-options">${rows}</div></section>`);
  }
  const scores = detail.scores || (detail.score != null ? {Judgment: detail.score} : {});
  const scoreRows = Object.entries(scores).filter(([,value]) => Number.isFinite(Number(value)))
    .map(([name,value]) => `<div class="evidence-score"><div class="evidence-option-head"><span>${esc(name.replaceAll("_", " "))}</span><b>${evidencePercent(value)}</b></div><div class="evidence-meter"><span style="width:${Math.max(0,Math.min(100,Number(value)*100))}%"></span></div></div>`).join("");
  if (scoreRows) groups.push(`<section class="evidence-group"><div class="evidence-group-head"><strong>Yes / no judgments</strong></div><div class="evidence-options">${scoreRows}</div></section>`);
  if (!groups.length) groups.push(`<p class="evidence-note">${detail.input_tokens ? "This step has no probability distribution in its recorded trace." : "Resolved from explicit facts or schema; no model probability was needed."}</p>`);
  const visual = calls.length ? "" : `<div class="evidence-visual">${groups.join("")}</div>`;
  return `${exchange}${visual}<details class="evidence-json"><summary>Decision trace JSON</summary><pre>${esc(JSON.stringify(detail,null,2))}</pre></details>`;
}
function handleEvent(event) {
  runEvents.push(event);
  $("#export-trace").disabled = false;
  if (event.stats) renderStats(event.stats);
  if (event.kind === "phase") { setPhase(event.phase); return; }
  if (event.kind === "status") { notice(event.message, "running"); recordRunDetail("Status", event.message); return; }
  if (event.kind === "program") {
    renderProgram(event.program);
    if (event.program?.resolving) recordRunDetail("Semantic state", event.program.decisions || event.program);
    return;
  }
  if (event.kind === "skill") {
    completedSkills++;
    const detail = event.usage;
    const relatedCalls = completedCallsForSkill(detail);
    const usage = detail?.input_tokens != null ? `${detail.input_tokens} in · ${detail.output_tokens || 0} out` : "deterministic";
    const typeBadges = providerCallTypes(relatedCalls).map(jevTypeBadge).join("");
    for (const call of relatedCalls) document.querySelector(`.trace-call[data-provider-call="${CSS.escape(String(call.call))}"]`)?.remove();
    $("#trace").insertAdjacentHTML("afterbegin", `<article class="trace-call"><div class="trace-call-head"><span class="step-number">${String(completedSkills).padStart(2,"0")} / ${esc(event.name)}</span><span class="trace-call-meta">${typeBadges}<small>${esc(usage)}</small></span></div><div class="interpreted-result"><span>Interpreted result</span><strong>${esc(compact(event.selected))}</strong></div>${providerQuestionPreview(relatedCalls)}<details class="decision-details"><summary>Inspect decision${detail?.elapsed_ms != null ? ` · ${detail.elapsed_ms} ms` : ""}</summary>${renderEvidence(detail, relatedCalls)}</details></article>`);
    $(".trace-content").scrollTop = 0;
    return;
  }
  if (event.kind === "call_start") {
    setPhase("resolve");
    const requestPayload = event.request_payload || {state:event.state, questions:event.questions};
    const callRecord = {call:event.call, request:requestPayload, state:event.state, questions:event.questions || requestPayload.questions || {}, ended:false};
    providerCalls.set(event.call, callRecord);
    pendingProviderCallIds.push(event.call);
    const names = Object.keys(callRecord.questions || {}).map(name => name.replaceAll("_", " "));
    const stage = event.state?.stage || event.state?.task || names.join(" · ");
    activeDecision(`Call ${event.call} · ${names.length} bounded question${names.length === 1 ? "" : "s"}: ${String(stage).slice(0,180)}`, true);
    recordRunDetail(`Decision call ${event.call} started`, requestPayload, `${names.length} bounded question${names.length === 1 ? "" : "s"}`);
    const question = Object.values(callRecord.questions || {})[0]?.instructions;
    notice(event.state?.stage || (question ? String(question).slice(0,115) : `Resolving ${names.length} bounded questions`), "running");
    const types = providerCallTypes([callRecord]).map(jevTypeBadge).join("");
    $("#trace").insertAdjacentHTML("afterbegin", `<article class="trace-call provider-live-call" data-provider-call="${esc(event.call)}"><div class="trace-call-head"><span class="step-number">CALL ${event.call}</span><span class="trace-call-meta">${types}<small>In flight</small></span></div><strong>${esc(stage)}</strong>${providerQuestionPreview([callRecord])}<details class="decision-details"><summary>Inspect call</summary>${renderProviderExchange([callRecord])}</details></article>`);
    renderProgram(event.state?.current_program || event.state?.program);
    return;
  }
  if (event.kind === "call_end" || event.kind === "call_error") {
    const callRecord = providerCalls.get(event.call) || {call:event.call, request:null, state:null, questions:{}};
    callRecord.ended = event.kind === "call_end";
    callRecord.error = event.kind === "call_error" ? event.message : null;
    callRecord.response = event.response_payload || (event.kind === "call_end" ? {answers:event.answers || {}, usage:event.usage || {}} : null);
    callRecord.answers = event.answers; callRecord.usage = event.usage; callRecord.latency_ms = event.latency_ms;
    providerCalls.set(event.call, callRecord);
    activeDecision(`Call ${event.call} ${event.kind === "call_error" ? "failed" : "completed"}`);
    recordRunDetail(event.kind === "call_error" ? `Decision call ${event.call || ""} failed` : `Decision call ${event.call || ""} completed`, callRecord.response || {error:event.message}, event.latency_ms == null ? "" : `${event.latency_ms} ms`);
    const card = document.querySelector(`.trace-call[data-provider-call="${CSS.escape(String(event.call))}"]`);
    if (card) {
      const meta = card.querySelector(".trace-call-meta small");
      if (meta) meta.textContent = event.kind === "call_error" ? "Failed" : `${event.latency_ms} ms`;
      const details = card.querySelector(".decision-details");
      if (details) details.innerHTML = `<summary>Inspect call</summary>${renderProviderExchange([callRecord])}`;
    }
    return;
  }
  if (event.kind === "decision") { renderDecision(event); return; }
  if (event.kind === "compiled") {
    renderProgram(event.program); renderSQL(event.sql,event.params); setPhase("compile");
    recordRunDetail("Compiled query", {program:event.program, sql:event.sql, params:event.params});
    notice("Program compiled · parameterized SQL ready", "running");
    return;
  }
  if (event.kind === "vet") { notice("Semantic vet complete", "running"); recordRunDetail("Semantic vet", event.vet || event); return; }
  if (event.kind === "result") {
    terminalReceived = true;
    renderResult(event.result);
    document.body.classList.add("has-result");
    $("#result-summary").title = $("#prompt").value;
    $("#output").classList.add("result-arrived");
    activeDecision("Recorded decisions / most recent first");
    if (event.result.supported) setPhase("done");
    else setPhase("compile", true);
    const mutation = event.result.program?.operation && event.result.program.operation !== "SELECT";
    notice(event.result.supported ? (mutation ? "Preview ready · awaiting your confirmation" : "Query complete · exact SQLite result ready") : "Stopped · semantic vet did not pass", event.result.supported ? "success" : "error");
    $("#elapsed").textContent = `${(event.elapsed_ms / 1000).toFixed(2)}s`;
    return;
  }
  if (event.kind === "error") { terminalReceived = true; error(event.message); }
}
async function run() {
  if (running) return;
  if (connectionPending) return notice("Save connection settings before running", "error");
  const prompt = $("#prompt").value.trim();
  if (prompt.length < 3) { $("#prompt").focus(); return notice("Enter a request with at least three characters.", "error"); }
  running = true; calls = 0; completedSkills = 0; terminalReceived = false; runEvents = [];
  resetResult(); setBusy(true); setPhase("inspect"); renderStats();
  $("#export-trace").disabled = true;
  $("#trace").innerHTML = "";
  resetRunDetails();
  activeDecision("Inspecting the database structure");
  $("#typed-program").open = true;
  $("#program-status").textContent = "Resolving";
  $("#program").innerHTML = '<div class="loading-lines" aria-label="Waiting for resolved semantics"><i></i><i></i><i></i></div>';
  $("#output").innerHTML = '<div class="empty"><span class="empty-icon">⌁</span><strong>Your query is taking shape</strong><p>Follow the live query program and semantic decisions. SQL runs only when the program is complete.</p></div>';
  $("#row-count").textContent = "…";
  $("#row-count-label").textContent = "rows";
  $("#result-summary").textContent = `${$("#database").value} · ${prompt}`;
  $("#result-summary").title = prompt;
  renderSQL(null);
  startAt = performance.now();
  timer = setInterval(() => { if (!terminalReceived) $("#elapsed").textContent = `${((performance.now()-startAt)/1000).toFixed(1)}s`; },100);
  notice("Opening the database working copy", "running");
  try {
    const response = await fetch("/api/run", {method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({database:$("#database").value,prompt,mode:"auto"}),cache:"no-store"});
    if (!response.ok) { const body = await response.json(); throw new Error(typeof body.detail === "string" ? body.detail : `Request rejected (HTTP ${response.status}).`); }
    const reader = response.body.getReader(), decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const {value,done} = await reader.read();
      buffer += done ? decoder.decode() : decoder.decode(value,{stream:true});
      const messages = buffer.split("\n\n"); buffer = messages.pop();
      for (const message of messages) if (message.startsWith("data: ")) handleEvent(JSON.parse(message.slice(6)));
      if (done) break;
    }
    if (!terminalReceived) throw new Error("The stream ended before a result arrived. Run the request again.");
  } catch (exc) { error(exc.message); }
  finally { running = false; clearInterval(timer); setBusy(false); activeDecision("Recorded decisions / most recent first"); }
}
async function commit() {
  if (!currentToken) return;
  $("#confirm-dialog").close();
  try {
    const data = await jsonRequest("/api/commit", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({token:currentToken,confirm:true})});
    currentUndo = data.undo_token; currentToken = null;
    const table = data.table || "selected table";
    notice(`Committed ${data.affected} row(s) to ${table}`);
    $("#result-summary").textContent = `Committed to ${table} in the local working copy · undo is available`;
    $("#row-count-label").textContent = "rows changed";
    $("#output").insertAdjacentHTML("beforeend", `<div class="commit-row"><span>Changes committed to the local working copy.</span><button id="undo" class="button secondary">Undo last commit</button></div>`);
    $("#commit")?.remove();
    $("#undo").addEventListener("click", undo);
    await loadSchema();
    const inspector = $("#db-inspector-dialog");
    if (inspector.open) {
      if ($("#inspector-overview").hidden) await runInspectorQuery();
      else await loadInspectorOverview($("#database").value);
    }
  } catch (exc) { error(exc.message); }
}
async function undo() {
  if (!currentUndo) return;
  try {
    await jsonRequest("/api/undo", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({token:currentUndo})});
    currentUndo = null; $("#undo")?.remove(); notice("Last commit was rolled back from its snapshot"); await loadSchema();
    $("#result-summary").textContent = "Last commit was undone · working copy restored";
    $("#row-count").textContent = "0";
    $("#row-count-label").textContent = "rows changed";
  } catch (exc) { error(exc.message); }
}
document.addEventListener("DOMContentLoaded", async () => {
  const clientVersion = document.querySelector('meta[name="intentsql-client-version"]')?.content || "unknown";
  const storedVersion = localStorage.getItem("intentsql-client-version");
  if (storedVersion !== clientVersion) {
    const theme = localStorage.getItem("intentsql-theme") ?? localStorage.getItem("jevql-theme");
    for (const key of Object.keys(localStorage)) {
      if (key.startsWith("intentsql-") || key.startsWith("jevql-")) localStorage.removeItem(key);
    }
    if (theme) localStorage.setItem("intentsql-theme", theme);
    localStorage.setItem("intentsql-client-version", clientVersion);
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.getRegistrations().then(registrations => {
        for (const registration of registrations) registration.unregister();
      }).catch(() => {});
    }
  }
  if ((localStorage.getItem("intentsql-theme") ?? localStorage.getItem("jevql-theme")) === "light") document.body.classList.add("light");
  $("#theme").addEventListener("click", () => { document.body.classList.toggle("light"); localStorage.setItem("intentsql-theme",document.body.classList.contains("light") ? "light":"dark"); });
  $("#database").addEventListener("change", async () => {
    syncDatabaseActions();
    clearQuery();
    try { await loadSchema(); } catch(exc) { error(exc.message); }
  });
  $("#run").addEventListener("click", run);
  $("#prompt").addEventListener("keydown", e => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter") run(); });
  $("#copy-sql").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(currentSQL); $("#copy-sql").textContent = "Copied ✓"; setTimeout(() => $("#copy-sql").textContent = "Copy SQL", 1800); }
    catch { notice("Clipboard unavailable. Select and copy the SQL directly.", "error"); }
  });
  for (const id of ["#open-settings", "#connection-shortcut"]) $(id).addEventListener("click", () => $("#settings-dialog").showModal());
  $("#close-settings").addEventListener("click", () => $("#settings-dialog").close());
  $("#settings-dialog").addEventListener("close", () => { if (connectionPending) loadConnection().catch(exc => notice(exc.message, "error")); });
  $("#expand-trace").addEventListener("click", () => setTraceDetails(true));
  $("#collapse-trace").addEventListener("click", () => setTraceDetails(false));
  $("#export-trace").addEventListener("click", () => download("intentsql-trace.json", JSON.stringify(runEvents,null,2), "application/json"));
  $("#export-csv").addEventListener("click", () => {
    if (!readResult) return;
    // Avoid spreadsheet formula interpretation in exported text cells.
    const cell = value => { let text = String(value ?? ""); if (typeof value === "string" && /^[=+@\-\t\r\n]/.test(text)) text = "'" + text; return '"' + text.replaceAll('"','""') + '"'; };
    download("intentsql-results.csv", [readResult.columns, ...readResult.rows].map(row => row.map(cell).join(",")).join("\r\n"), "text/csv;charset=utf-8");
  });
  renderStats();
  $("#provider").addEventListener("change", () => {
    const profile = connectionProfiles[$("#provider").value];
    $("#endpoint").value = profile.url;
    $("#model").value = profile.model;
    $("#api-key").value = "";
    syncEndpointAddress(); updateKeyStatus(); connectionEdited();
  });
  $("#reset-provider").addEventListener("click", () => {
    const preset = connectionPresets[$("#provider").value];
    $("#endpoint").value = preset.url;
    $("#model").value = preset.model;
    syncEndpointAddress(); connectionEdited();
  });
  $("#endpoint").addEventListener("input", syncEndpointAddress);
  for (const selector of ["#endpoint-host", "#endpoint-port"]) $(selector).addEventListener("input", () => {
    connectionEdited();
    try {
      const url = new URL($("#endpoint").value);
      const host = $("#endpoint-host").value.trim();
      const hostname = host.includes(":") && !host.startsWith("[") ? `[${host}]` : host;
      url.hostname = hostname;
      $("#endpoint-host").setCustomValidity(url.hostname.toLowerCase() === hostname.toLowerCase() ? "" : "Enter a valid host name or IP address.");
      url.port = $("#endpoint-port").value;
      $("#endpoint").value = url.href;
    } catch { $("#settings-feedback").textContent = "Enter a full endpoint URL first."; }
  });
  for (const selector of ["#endpoint", "#model", "#api-key"]) {
    $(selector).addEventListener("input", () => {
      connectionEdited();
    });
  }
  $("#save-connection").addEventListener("click", saveConnection);
  $("#import-db").addEventListener("click", () => $("#path-dialog").showModal());
  $("#delete-db").addEventListener("click", () => {
    const selected = databasesById.get($("#database").value);
    if (!selected?.deletable) return;
    $("#delete-db-summary").textContent = `Delete ${selected.label}? This removes IntentSQL's local working copy. The original file you imported is not changed.`;
    $("#delete-db-dialog").showModal();
  });
  $("#delete-db-dialog").addEventListener("close", async () => {
    if ($("#delete-db-dialog").returnValue !== "delete") return;
    const database = $("#database").value;
    try {
      await jsonRequest(`/api/databases/${encodeURIComponent(database)}`, {method:"DELETE", headers:{"Content-Type":"application/json"}, body:JSON.stringify({confirm:true})});
      await loadDatabases();
      notice("Imported working copy deleted");
    } catch (exc) { error(exc.message); }
  });
  $("#inspect-db").addEventListener("click", () => openInspector(null));
  $("#path-dialog").addEventListener("close", async () => { if ($("#path-dialog").returnValue !== "import") return; try { const data = await jsonRequest("/api/databases",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({path:$("#db-path").value})}); await loadDatabases(data.id); notice("Database imported as a local working copy"); } catch (exc) { error(exc.message); } });
  $("#cancel-commit").addEventListener("click", () => $("#confirm-dialog").close());
  $("#close-inspector").addEventListener("click", () => $("#db-inspector-dialog").close());
  $("#run-inspector-query").addEventListener("click", runInspectorQuery);
  $("#inspector-sql").addEventListener("keydown", event => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") { event.preventDefault(); runInspectorQuery(); }
  });
  $("#confirm-commit").addEventListener("click", commit);
  for (const button of document.querySelectorAll(".sample")) button.addEventListener("click", async () => { if (running) return; try { await loadDatabases(button.dataset.db); $("#prompt").value = button.dataset.prompt; $("#prompt").dispatchEvent(new Event("input")); $("#prompt").focus(); } catch (exc) { error(exc.message); } });
  try { await Promise.all([loadDatabases(),loadConnection()]); } catch (exc) { error(exc.message); }
});
