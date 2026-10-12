"use strict";
const $ = (s, el = document) => el.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const api = async (path, opts = {}) => {
  const r = await fetch(path, {headers: {"Content-Type": "application/json"}, ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined});
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `Request failed (${r.status})`);
  return data;
};
const view = $("#view");
let overview = null, es = null, pollTimer = null;
const ENGINE = {gemini: "Gemini (live)", scripted: "Scripted test client"};
const AGENT = k => k === "gemini" ? "Gemini" : k === "scripted" ? "Scripted client" : k === "rules" ? "Rules" : k === "human" ? "Human" : k;
const STATUS_TAG = {completed: "ok", verified: "ok", committed: "ok", agree: "ok", resolved: "ok",
  failed: "bad", rejected: "bad", blocked: "bad", disagree: "bad", denied: "bad", cancelled: "bad", not_handled: "bad",
  running: "agent", queued: "agent", waiting_for_input: "human", waiting_for_approval: "human", pending: "human",
  partially_completed: "wait", waiting_for_quota: "wait", not_covered: "wait", authorized: "rules", uncertain: "wait",
  stopped_by_human: "human", waiting: "human", needs_clarification: "human", partial: "wait", reported: "ok", revoked: "bad", stale: "bad", expired: "bad"};
const tag = (s, extra = "") => `<span class="tag ${STATUS_TAG[s] || ""} ${extra}">${esc(String(s).replaceAll("_", " "))}</span>`;
const inr = v => { const n = Number(v); return isNaN(n) ? esc(v) : "₹" + n.toLocaleString("en-IN", {minimumFractionDigits: 2}); };
const when = ts => ts ? new Date(ts * 1000).toLocaleString("en-IN", {dateStyle: "medium", timeStyle: "short"}) : "";

async function refreshChrome() {
  try { overview = await api("/api/overview"); } catch { return; }
  const pc = $("#pending-count");
  pc.hidden = !overview.pending_decisions; pc.textContent = overview.pending_decisions;
  const g = overview.gemini;
  $("#engine-status").innerHTML = g.configured
    ? `Gemini: <strong>${esc(g.model)}</strong><br>${g.calls_today} calls today${g.rpd ? ` of ${esc(g.rpd)}` : ""}` +
      (g.daily_protection ? "" : `<br><span class="err">Daily protection off: set GEMINI_RPD in .env</span>`)
    : `Gemini not configured.<br>Add a key to <strong>.env</strong> for live runs.`;
}

function setNav(name) {
  document.querySelectorAll("[data-nav]").forEach(a => a.classList.toggle("on", a.dataset.nav === name));
}

function stopLive() { if (es) { es.close(); es = null; } clearInterval(pollTimer); pollTimer = null; }

async function route() {
  stopLive();
  const [, page, id] = (location.hash || "#/").split("/");
  await refreshChrome();
  try {
    if (!page) return homeView();
    if (page === "new") return newView(id);
    if (page === "runs") return runsView();
    if (page === "run") return runView(id);
    if (page === "decisions") return decisionsView();
    if (page === "report") return reportView(id);
    if (page === "tests") return testsView();
    view.innerHTML = `<h1>Page not found</h1><p><a href="#/">Go to companies</a></p>`;
  } catch (e) { view.innerHTML = `<h1>Something went wrong</h1><p class="err">${esc(e.message)}</p>`; }
}
window.addEventListener("hashchange", route);

/* ---------------- companies ---------------- */
const EFFECT = {hard: "bad", hold: "wait", deny: "bad", clarify: "human", substitute: "rules", approval: "human"};
async function homeView() {
  setNav("home");
  const runs = await api("/api/runs");
  view.innerHTML = `
  <h1>One operator, three businesses</h1>
  <p class="lede">The same agent runtime completes accounts-payable work for a steel trader, support work for an
  online store and admissions work for a coaching institute. Only each company's profile, policies, handbook and adapter differ. Every business decision is checked by an
  independent rule engine; disagreements and large amounts go to a person.</p>
  <div class="companies">${overview.companies.map(c => `
    <section class="company">
      <h2>${esc(c.name)}</h2>
      <p class="muted small">Policy ${esc(c.policy_version)} · ${c.rules.length} rules · ${c.protected_routes.length} protected write routes</p>
      <h3>What the operator can do</h3>
      <ul>${Object.entries(c.actions).map(([k, v]) => `<li><strong>${esc(k.replaceAll("_", " "))}</strong>: ${esc(v)}</li>`).join("")}</ul>
      <h3>Rules checked independently</h3>
      <ul class="rulelist">${c.rules.map(r => `<li><span class="tag ${EFFECT[r.effect] || ""}">${esc(r.effect)}</span><span>${esc(r.message)}</span></li>`).join("")}</ul>
      <a class="btn" href="#/new/${esc(c.id)}">Start a task for this company</a>
    </section>`).join("")}
  </div>
  <div class="shared"><strong>Shared by all companies:</strong> the planning and execution loop, generic browser and file tools,
  the proposal-and-check protocol, signed write authorization, recovery, and independent verification.</div>
  <h2>Recent runs</h2>${runsTable(runs.slice(0, 8))}`;
}

function runsTable(runs) {
  if (!runs.length) return `<p class="muted">No runs yet. <a href="#/new">Start a task</a>.</p>`;
  return `<table><thead><tr><th>Run</th><th>Company</th><th>Engine</th><th>Status</th><th>Model calls</th><th>Started</th><th></th></tr></thead><tbody>
  ${runs.map(r => `<tr><td><a href="#/run/${r.id}">${r.id}</a>${r.label ? `<div class="small muted">${esc(r.label)}</div>` : ""}</td>
    <td>${esc(r.company)}</td><td>${esc(ENGINE[r.model_kind] || r.model_kind)}</td><td>${tag(r.status)}</td>
    <td class="num">${r.calls}</td><td class="small">${when(r.created_at)}</td>
    <td><a href="#/report/${r.id}">Report</a></td></tr>`).join("")}</tbody></table>`;
}

async function runsView() {
  setNav("runs");
  view.innerHTML = `<h1>Runs</h1><p class="lede">Every run keeps its full event log, decisions, writes and verification.</p>${runsTable(await api("/api/runs"))}`;
}

/* ---------------- new task ---------------- */
const FAULTS = [
  ["drop_after_commit", 1, "Lose the response after a save succeeds", "Tests reconciliation without duplicates"],
  ["error_before_commit", 1, "Fail the next save with a server error", "Tests safe retry"],
  ["expire_sessions_after", 4, "Expire the session part-way through", "Tests re-sign-in and resume"],
  ["rename_labels", true, "Rename form labels", "Tests adapting to a changed page"],
  ["slow_ms", 1500, "Make every page slow (1.5 s)", "Tests patience with slow systems"],
  ["corrupt_next_save", 1, "Corrupt the next saved value (steel)", "Tests that verification does not trust success pages"],
];
function newView(preset) {
  setNav("new");
  const comps = overview.companies;
  let company = preset && comps.some(c => c.id === preset) ? preset : comps[0].id;
  let engine = overview.gemini.configured ? "gemini" : "scripted";
  const render = () => {
    const c = comps.find(x => x.id === company);
    view.innerHTML = `<h1>New task</h1>
    <p class="lede">Describe the outcome you want. The operator plans and carries out the steps itself.</p>
    <div class="form">
      <div><h3>Company</h3><div class="seg">${comps.map(x => `<button type="button" data-co="${x.id}" class="${x.id === company ? "on" : ""}">${esc(x.name)}</button>`).join("")}</div></div>
      <div><label for="task"><h3>Request</h3></label><textarea id="task" rows="3">${esc(c.default_task)}</textarea></div>
      <div><h3>Decision engine</h3><div class="seg">
        <button type="button" data-en="gemini" class="${engine === "gemini" ? "on" : ""}" ${overview.gemini.configured ? "" : "disabled"}>Gemini (live)</button>
        <button type="button" data-en="scripted" class="${engine === "scripted" ? "on" : ""}">Scripted test client</button></div>
        <p class="small muted">${engine === "gemini" ? `Uses the free-tier Gemini model ${esc(overview.gemini.model)}. Up to ${overview.gemini.run_cap} model calls per run.`
          : "Offline mechanism test: a scripted client drives the real browser, apps, checks and verification. It is not counted as live autonomy."}</p></div>
      <label class="check"><input type="checkbox" id="reset" checked> Reset this company's sandbox data before the run</label>
      <label class="check"><input type="checkbox" id="ro"> Read-only: information only, no changes may be made <span class="small muted">(also detected from wording such as "don't change anything")</span></label>
      <details class="faults"><summary>Failure injection (optional)</summary>
        ${FAULTS.map(([k, v, label, hint]) => `<label class="check"><input type="checkbox" data-fault="${k}" data-val='${JSON.stringify(v)}'> ${esc(label)} <span class="small muted">${esc(hint)}</span></label>`).join("")}
      </details>
      <div><button id="start">Start run</button> <span id="msg" class="err"></span></div>
    </div>`;
    view.querySelectorAll("[data-co]").forEach(b => b.onclick = () => { company = b.dataset.co; render(); });
    view.querySelectorAll("[data-en]").forEach(b => b.onclick = () => { engine = b.dataset.en; render(); });
    $("#start").onclick = async () => {
      const faults = {};
      view.querySelectorAll("[data-fault]:checked").forEach(i => faults[i.dataset.fault] = JSON.parse(i.dataset.val));
      $("#start").disabled = true;
      try {
        const r = await api("/api/runs", {method: "POST", body: {company, engine, task: $("#task").value,
          reset: $("#reset").checked, read_only: $("#ro").checked, faults, human: "console"}});
        location.hash = `#/run/${r.run_id}`;
      } catch (e) { $("#msg").textContent = e.message; $("#start").disabled = false; }
    };
  };
  render();
}

/* ---------------- live run ---------------- */
function laneOf(e) {
  const t = e.type, a = e.actor;
  if (["decision_requested", "decision_resolved"].includes(t)) return 3;
  if (["comparison", "hard_denial", "authorized", "write_submitted", "write_committed", "write_rejected",
       "write_blocked", "write_uncertain", "write_reconciled", "verification", "authorization_stale", "recommendation",
       "rules_executor_decision", "rules_executor_write", "rules_executor_started"].includes(t)) return 2;
  if (["case_stopped", "case_reopened", "scope_clarified"].includes(t)) return 3;
  if (["step", "proposal", "plan", "model_text"].includes(t)) return 1;
  return 0;
}
function argSummary(tool, a) {
  if (!a) return "";
  if (tool === "open_page") return a.path;
  if (tool === "click") return a.ref;
  if (tool === "fill") return (a.fields || []).length + " field(s)";
  if (tool === "read_document") return a.name;
  if (tool === "note") return `${a.key} = ${a.value}`;
  if (tool === "propose_decision") return `${a.case_id}: ${a.action}`;
  if (tool === "ask_human") return a.question;
  return "";
}
function evHtml(e) {
  const p = e.payload || {}, lane = laneOf(e);
  let cls = "", title = "", body = "";
  switch (e.type) {
    case "step": title = `${p.tool}${argSummary(p.tool, p.args) ? " · " + argSummary(p.tool, p.args) : ""}`;
      body = (p.result || "").split("\n").slice(0, 3).join("\n");
      if (/^(ERROR|DENIED)/.test(p.result || "")) cls = "bad";
      if (p.tool === "propose_decision") cls = "hl"; break;
    case "plan": title = "Plan set"; body = (p.steps || []).map((s, i) => `${i + 1}. ${s}`).join("\n"); break;
    case "proposal": title = `Proposes ${p.action} for ${e.case_id}`; body = p.rationale; cls = "hl"; break;
    case "model_text": title = "Model note"; body = p.text; break;
    case "comparison": {
      const c = p.comparison || {};
      title = `Rules: ${(p.rules_outcome || []).join(" or ") || "not covered"} · ${c.status}`;
      body = (c.differences || []).map(d => `${d.field}: ${d.gemini} vs ${d.rules}`).join("\n") ||
        (p.fired || []).map(f => f.id).join(", ");
      cls = c.status === "agree" ? "good" : "bad"; break; }
    case "hard_denial": title = "Hard denial"; body = p.message; cls = "bad"; break;
    case "authorized": title = `Authorized ${p.action}`; body = `source: ${AGENT(p.source)}`; cls = "good"; break;
    case "write_submitted": title = `Write sent · ${p.action}`; body = p.route; break;
    case "write_committed": title = "Write saved"; body = p.message; cls = "good"; break;
    case "write_rejected": title = "Write rejected by app"; body = p.message; cls = "bad"; break;
    case "write_blocked": title = "Write blocked by gate"; body = p.message; cls = "bad"; break;
    case "write_uncertain": title = "Write result uncertain"; body = p.why; cls = "bad"; break;
    case "write_reconciled": title = "Reconciled"; body = p.result; cls = "good"; break;
    case "verification": title = `Verification: ${p.overall}`;
      body = Object.entries(p.cases || {}).map(([k, v]) => `${k}: ${v.status}`).join("\n");
      cls = p.overall === "completed" ? "good" : "bad"; break;
    case "authorization_stale": title = "Authorization stale: records changed"; body = (p.changes || []).join("\n"); cls = "bad"; break;
    case "recommendation": title = `Recommendation recorded (read-only): ${p.action}`; body = `rules: ${(p.rules_outcome || []).join(" or ") || "not covered"}`; break;
    case "case_stopped": title = "Case stopped"; body = `${p.actor || ""} — ${p.reason || ""} (${p.revoked || 0} permission(s) revoked)`; cls = "bad"; break;
    case "case_reopened": title = "Case reopened"; body = `${p.actor || ""} — ${p.reason || ""}`; break;
    case "scope_captured": title = `Scope: ${(p.cases || []).length} case(s)${p.read_only ? " · read-only" : ""}`; body = [p.note, ...(p.issues || []).map(x => "Needs a human: " + x)].filter(Boolean).join("\n"); if ((p.issues || []).length) cls = "bad"; break;
    case "scope_clarified": title = "Scope clarified by a human"; body = p.read_only ? "Recommendations only: no changes may be made" : ""; break;
    case "rules_executor_started": title = "Rules executor started (Gemini capacity ended)"; body = (p.plan || []).map(x => `${x.case}: ${x.mode}${x.action ? " " + x.action : ""}`).join("\n"); break;
    case "rules_executor_decision": title = `Rules executor: ${p.action}`; body = (p.result || "").split("\n")[0]; break;
    case "rules_executor_write": title = `Rules executor write: ${p.result}`; body = p.message; cls = p.result === "committed" ? "good" : "bad"; break;
    case "decision_requested": title = `Asks human: ${p.kind}`; body = p.kind === "question" ? p.request.question : (e.case_id || ""); break;
    case "decision_resolved": { const r = p.response || {}; title = `Human: ${r.choice || "answered"}`;
      body = `${r.actor || ""}${r.reason ? " — " + r.reason : ""}${r.answer ? " — " + r.answer : ""}`; break; }
    default: title = e.type.replaceAll("_", " "); body = p.note || p.status || p.detail || p.error || "";
  }
  const laneCls = lane ? `l${lane} ${["", "agent", "rules", "human"][lane]}` : "sys";
  return `<div class="ev ${laneCls} ${cls}" style="grid-row:auto"><div class="t">${esc(title)}</div>${body ? `<div class="r">${esc(body)}</div>` : ""}</div>`;
}

async function runView(rid) {
  setNav("runs");
  view.innerHTML = `<div id="runhead"></div>
  <div class="rungrid">
    <div><div class="panel" id="cases"></div><div class="panel plan" id="plan" style="margin-top:14px"></div></div>
    <div><div class="lanes"><div class="lh agent">Agent</div><div class="lh rules">Rules and write gate</div><div class="lh human">Human</div>
      <div class="timeline" id="timeline"></div></div></div>
    <div><div class="panel shot" id="shot"><h3>Agent's browser</h3><img id="shotimg" alt="Latest screenshot of the agent's browser"><p class="small muted" id="shotts"></p></div>
      <div class="panel" id="facts" style="margin-top:14px"></div></div>
  </div><div id="inline-decisions" style="margin-top:18px"></div>`;
  const tl = $("#timeline");
  let lastSeq = 0;
  const refresh = async () => {
    const d = await api(`/api/runs/${rid}`);
    renderRunHead(d); renderCases(d); renderFacts(d);
    $("#shotimg").src = `/runs/${rid}/latest.jpg?t=${Date.now()}`;
    $("#shotts").textContent = "Most recent captured state, refreshed as the agent acts.";
    const pend = d.decisions.filter(x => x.status === "pending");
    $("#inline-decisions").innerHTML = pend.length ? `<h2>Waiting for you</h2>` + pend.map(x => decisionCard(x)).join("") : "";
    bindDecisionCards($("#inline-decisions"), refresh);
    return d;
  };
  await refresh();
  es = new EventSource(`/api/runs/${rid}/events?after=0`);
  let pending = null;
  es.addEventListener("ev", m => {
    const e = JSON.parse(m.data); if (e.seq <= lastSeq) return; lastSeq = e.seq;
    if (["screenshot", "decision_requested_internal"].includes(e.type)) return;
    const nearBottom = tl.scrollHeight - tl.scrollTop - tl.clientHeight < 80;
    tl.insertAdjacentHTML("beforeend", evHtml(e));
    if (nearBottom) tl.scrollTop = tl.scrollHeight;
    clearTimeout(pending); pending = setTimeout(() => { refresh(); refreshChrome(); }, 400);
  });
  es.addEventListener("end", () => { es.close(); refresh(); });
}

function renderRunHead(d) {
  const r = d.run;
  const live = ["running", "queued", "waiting_for_input", "waiting_for_approval", "cancel_requested"].includes(r.status);
  $("#runhead").innerHTML = `<div class="runhead"><div>
    <div class="runmeta">${esc(r.company)} · ${esc(r.id)}</div>
    <div class="task">${esc(r.task)}</div>
    <div class="runmeta">${tag(r.status)} <span>${esc(ENGINE[r.model_kind] || r.model_kind)}${r.model && r.model_kind === "gemini" ? " (" + esc(r.model) + ")" : ""}</span>
      <span class="num">${d.usage.calls} model calls · ${d.usage.input_tokens.toLocaleString()} input tokens</span>
      ${r.label ? `<span>${esc(r.label)}</span>` : ""}</div>
    ${r.model_kind === "scripted" ? `<p class="small muted">Scripted test client: exercises the real system but is not live model autonomy.</p>` : ""}
    ${r.read_only ? `<p class="small"><span class="tag human">read-only</span> Information only: decisions are recorded as recommendations and nothing can be changed.</p>` : ""}
    ${r.mode === "rules_fallback" ? `<p class="small"><span class="tag rules">rules executor</span> Gemini capacity ended; covered cases were completed by the deterministic rules executor through the same controls.</p>` : ""}
  </div><div style="display:flex;gap:8px">
    ${live ? `<button class="danger" id="cancel">Cancel run</button>` : ""}
    ${r.status === "waiting_for_quota" ? `<button id="resume">Resume with Gemini</button>` : ""}
    <a class="btn quiet" href="#/report/${r.id}">Report</a></div></div>
    ${r.status === "waiting_for_quota" ? `<div class="notice">Gemini's free capacity ran out. Work stopped safely; the rule engine's recommendations for the remaining cases are in Decisions. Nothing was executed without the model or you.</div>` : ""}`;
  const c = $("#cancel"); if (c) c.onclick = async () => { c.disabled = true; await api(`/api/runs/${r.id}/cancel`, {method: "POST"}); };
  const s = $("#resume"); if (s) s.onclick = async () => { try { await api(`/api/runs/${r.id}/resume`, {method: "POST"}); route(); } catch (e) { alert(e.message); } };
}

function renderCases(d) {
  const byCase = {};
  d.cases.forEach(c => byCase[c] = {props: [], writes: [], decs: []});
  d.proposals.forEach(p => (byCase[p.case_id] ||= {props: [], writes: [], decs: []}).props.push(p));
  d.writes.forEach(w => (byCase[w.case_id] ||= {props: [], writes: [], decs: []}).writes.push(w));
  d.decisions.forEach(x => x.case_id && (byCase[x.case_id] ||= {props: [], writes: [], decs: []}).decs.push(x));
  $("#cases").innerHTML = `<h3>Cases in scope <span class="muted small">${d.cases.length}</span></h3><div class="cases">${
    Object.entries(byCase).map(([id, c]) => {
      const first = c.props.find(p => p.comparison), last = c.props[c.props.length - 1];
      const v = d.verifications[id];
      const chips = [];
      if (first) chips.push(`<span class="tag agent">${esc(AGENT(first.source))}: ${esc(first.action)}</span>`,
        `<span class="tag rules">rules: ${esc(((first.checker || {}).allowed_actions || []).join("/") || "not covered")}</span>`, tag(first.comparison.status));
      if (last && last !== first) chips.push(`<span class="tag ${last.source === "human" ? "human" : last.source === "rules" ? "rules" : "agent"}">final: ${esc(last.action)} (${esc(AGENT(last.source))})</span>`);
      c.decs.forEach(x => chips.push(`<span class="tag human">${esc(x.kind)}: ${esc((x.response || {}).choice || x.status)}</span>`));
      c.writes.forEach(w => chips.push(tag(w.status) .replace("</span>", ` ${esc(w.action_type.split(".")[1])}</span>`)));
      if (v) chips.push(`<span class="tag ${STATUS_TAG[v.status] || ""}">verified: ${esc(v.status)}</span>`);
      const stopped = (d.stopped_cases || []).includes(id);
      if (stopped) chips.push(`<span class="tag bad">stopped by human</span> <button class="quiet small" data-reopen="${esc(id)}">Reopen…</button>`);
      return `<div class="case"><div class="id">${esc(id)}</div><div class="chips">${chips.join("") || `<span class="muted small">not started</span>`}</div></div>`;
    }).join("")}</div>`;
  $("#cases").querySelectorAll("[data-reopen]").forEach(b => b.onclick = async () => {
    const reason = prompt(`Reason for reopening ${b.dataset.reopen}?`);
    if (!reason) return;
    try { await api(`/api/runs/${d.run.id}/cases/${b.dataset.reopen}/reopen`, {method: "POST", body: {reason}}); b.remove(); }
    catch (e) { alert(e.message); }
  });
  const plan = d.run.plan;
  $("#plan").innerHTML = `<h3>Plan</h3>${plan ? `<ol>${plan.steps.map(s => `<li>${esc(s)}</li>`).join("")}</ol>
    <h3>Success criteria</h3><ul>${plan.success_criteria.map(s => `<li>${esc(s)}</li>`).join("")}</ul>` : `<p class="muted small">Not set yet.</p>`}`;
}

function renderFacts(d) {
  $("#facts").innerHTML = `<h3>Working memory</h3>${d.facts.length ? `<ul class="facts">${d.facts.slice(-14).map(f =>
    `<li><strong>${esc(f.key)}</strong> = ${esc(f.value)} <span class="muted">(${esc(f.source)})</span></li>`).join("")}</ul>` : `<p class="muted small">No facts noted yet.</p>`}`;
}

/* ---------------- decisions ---------------- */
function kv(obj, diffs = []) {
  const dset = new Set(diffs.map(d => d.field));
  return `<div class="kv">${Object.entries(obj || {}).map(([k, v]) =>
    `<span class="muted">${esc(k)}</span><span class="${dset.has(k) ? "diff" : ""}">${esc(v)}</span>`).join("")}</div>`;
}
function decisionCard(d) {
  const r = d.request || {};
  const head = `<p class="small muted">${esc(d.run_company || "")} ${esc(d.run_id)} · ${when(d.created_at)}${d.case_id ? " · case " + esc(d.case_id) : ""}</p>`;
  const reason = `<label class="small" for="why-${d.id}">Reason (recorded with your decision)</label><textarea id="why-${d.id}" rows="2" data-why></textarea>`;
  if (d.kind === "disagreement") {
    const c = r.comparison || {}, g = r.gemini || {}, ru = r.rules || {};
    const blocked = (r.hard_denied_actions || []).includes(g.action);
    return `<div class="dcard" data-id="${d.id}" data-kind="disagreement">${head}
      <h2 style="margin-top:0">${c.status === "not_covered" ? "The rules cannot decide this case" : "The agent and the rules disagree"}</h2>
      <div class="vs"><div class="a"><h3>Agent proposal: ${esc(g.action)}</h3>${kv(g.values, c.differences)}<p class="small">${esc(g.rationale)}</p></div>
      <div class="b"><h3>Rules: ${esc((ru.outcome || []).join(" or ") || "no outcome")}</h3>${kv(ru.expected_values, c.differences)}
        <p class="small">${(ru.fired || []).map(f => esc(f.message)).join("<br>") || "No rule fired."}
        ${(ru.gaps || []).length ? `<br>Not covered: ${ru.gaps.map(x => esc(x.rule + " (" + x.missing.join(", ") + ")")).join("; ")}` : ""}</p></div></div>
      ${reason}
      <div class="custom" hidden><label class="small">Custom action</label><select data-cact>${(r.allowed_custom_actions || []).map(a => `<option>${esc(a)}</option>`).join("")}</select>
        <label class="small">Values to change (one per line, name=value)</label><textarea rows="3" data-cvals></textarea></div>
      <div class="dactions">
        <button class="agent" data-choice="follow_gemini" ${blocked ? "disabled title='Blocked by a hard policy rule'" : ""}>Follow agent proposal</button>
        <button class="rules" data-choice="follow_rules" ${r.rules_option_available ? "" : "disabled"}>Follow rules</button>
        <button class="quiet" data-choice="custom">Custom decision</button>
        <button class="danger" data-choice="stop">Stop this case</button></div><p class="err" data-msg></p></div>`;
  }
  if (d.kind === "approval") {
    return `<div class="dcard" data-id="${d.id}" data-kind="approval">${head}
      <h2 style="margin-top:0">Approval needed: ${esc(r.action)} ${r.values && (r.values.total || r.values.amount) ? inr(r.values.total || r.values.amount) : ""}</h2>
      <p>${(r.rules || []).map(x => esc(x.message)).join("<br>")}</p>
      <div class="vs"><div class="a"><h3>Proposed by ${esc(AGENT(r.source))}</h3>${kv(r.values)}</div>
      <div class="b"><h3>Why</h3><p class="small">${esc(r.rationale)}</p><p class="small muted">Evidence: ${esc((r.evidence || []).join(", "))}</p></div></div>
      ${reason}<div class="dactions"><button data-choice="approve">Approve</button><button class="danger" data-choice="reject">Reject</button></div><p class="err" data-msg></p></div>`;
  }
  if (d.kind === "scope") {
    return `<div class="dcard" data-id="${d.id}" data-kind="scope">${head}
      <h2 style="margin-top:0">The request can't be enforced exactly as written</h2>
      <p class="small muted">Request: ${esc(r.task)}</p>
      <ul>${(r.issues || []).map(x => `<li>${esc(x)}</li>`).join("")}</ul>
      <p>Cases that would be in scope: <strong>${esc((r.cases || []).join(", ") || "none")}</strong>. Nothing has been changed yet.</p>
      ${reason}
      <div class="dactions"><button data-choice="proceed">Proceed with these cases</button>
        <button class="quiet" data-choice="read_only">Recommendations only (no changes)</button>
        <button class="danger" data-choice="stop">Stop</button></div><p class="err" data-msg></p></div>`;
  }
  if (d.kind === "question") {
    return `<div class="dcard" data-id="${d.id}" data-kind="question">${head}<h2 style="margin-top:0">The agent asks</h2>
      <p>${esc(r.question)}</p><textarea rows="3" data-answer placeholder="Your answer"></textarea>
      <div class="dactions"><button data-choice="answer">Send answer</button></div><p class="err" data-msg></p></div>`;
  }
  if (d.kind === "quota_handoff") {
    return `<div class="dcard" data-id="${d.id}" data-kind="quota">${head}<h2 style="margin-top:0">Gemini capacity ran out (${esc(r.reason)})</h2>
      <p>Rule-engine recommendations for the remaining cases. These were <strong>not executed</strong>.</p>
      <table><thead><tr><th>Case</th><th>Rules outcome</th><th>Key values</th></tr></thead><tbody>${(r.recommendations || []).map(x =>
        `<tr><td>${esc(x.case)}</td><td>${esc((x.rules_outcome || []).join(" or ") || x.rules_status)}</td><td class="small">${esc(JSON.stringify(x.expected_values || {}))}</td></tr>`).join("")}</tbody></table>
      <div class="dactions"><a class="btn" href="#/run/${d.run_id}">Open run to resume</a></div></div>`;
  }
  return "";
}
function bindDecisionCards(root, after) {
  root.querySelectorAll(".dcard").forEach(card => {
    card.querySelectorAll("[data-choice]").forEach(b => b.onclick = async () => {
      const choice = b.dataset.choice, msg = $("[data-msg]", card);
      const body = {choice};
      const why = $("[data-why]", card); if (why) body.reason = why.value;
      if (choice === "custom") {
        const box = $(".custom", card);
        if (box.hidden) { box.hidden = false; return; }
        const vals = {};
        $("[data-cvals]", card).value.split("\n").forEach(l => { const i = l.indexOf("="); if (i > 0) vals[l.slice(0, i).trim()] = l.slice(i + 1).trim(); });
        body.custom = {action: $("[data-cact]", card).value, values: vals};
      }
      if (choice === "answer") body.answer = $("[data-answer]", card).value;
      try { await api(`/api/decisions/${card.dataset.id}`, {method: "POST", body}); card.remove(); after && after(); refreshChrome(); }
      catch (e) { msg.textContent = e.message; }
    });
  });
}
async function decisionsView() {
  setNav("decisions");
  const load = async () => {
    const pend = await api("/api/decisions?status=pending");
    const done = (await api("/api/decisions?status=resolved")).slice(-12).reverse();
    view.innerHTML = `<h1>Decisions</h1><p class="lede">Cases where the agent and the rules disagree, amounts that need
      approval, and questions from the agent. Hard policy restrictions cannot be overridden here.</p>
      ${pend.length ? pend.map(decisionCard).join("") : `<p class="muted">Nothing is waiting for you.</p>`}
      <h2>Recently decided</h2>${done.length ? `<table><thead><tr><th>Run</th><th>Case</th><th>Kind</th><th>Decision</th><th>By</th></tr></thead><tbody>
      ${done.map(x => `<tr><td><a href="#/run/${x.run_id}">${esc(x.run_id)}</a></td><td>${esc(x.case_id || "")}</td><td>${esc(x.kind)}</td>
      <td>${esc((x.response || {}).choice || (x.response || {}).answer || "")}${(x.response || {}).reason ? `<div class="small muted">${esc(x.response.reason)}</div>` : ""}</td>
      <td class="small">${esc((x.response || {}).actor || "")}</td></tr>`).join("")}</tbody></table>` : `<p class="muted">None yet.</p>`}`;
    bindDecisionCards(view, load);
  };
  await load();
  pollTimer = setInterval(async () => { const n = (await api("/api/overview")).pending_decisions;
    if (n !== overview.pending_decisions) { await refreshChrome(); load(); } }, 3000);
}

/* ---------------- report ---------------- */
async function reportView(rid) {
  setNav("runs");
  const d = await api(`/api/runs/${rid}`);
  const r = d.run, v = d.verifications;
  const compared = d.proposals.filter(p => ["gemini", "scripted"].includes(p.source) && p.comparison);
  const agree = compared.filter(p => p.comparison.status === "agree").length;
  const firstByCase = {};  // the earliest model proposal for each case = first-attempt accuracy
  compared.forEach(p => { if (!firstByCase[p.case_id] || p.version < firstByCase[p.case_id].version) firstByCase[p.case_id] = p; });
  const firsts = Object.values(firstByCase);
  const firstAgree = firsts.filter(p => p.comparison.status === "agree").length;
  view.innerHTML = `<p class="runmeta">${esc(r.company)} · <a href="#/run/${rid}">${esc(rid)}</a></p>
  <h1>${esc(r.task)}</h1>
  <p>${tag(r.status)} · ${esc(ENGINE[r.model_kind] || r.model_kind)} · ${d.usage.calls} model calls · ${firstAgree} of ${firsts.length} first proposals matched the rules (${agree} of ${compared.length} including re-proposals)</p>
  ${r.model_kind === "scripted" ? `<p class="notice">Offline test with the scripted client. It exercises the real system but is not evidence of live model autonomy.</p>` : ""}
  ${r.summary && r.summary.agent_summary ? `<h2>Agent's summary</h2><p>${esc(r.summary.agent_summary)}</p>` : ""}
  <h2>Outcomes, verified against the database</h2>
  <table><thead><tr><th>Case</th><th>Final action</th><th>Decided by</th><th>Verification</th><th>Checks</th></tr></thead><tbody>
  ${d.cases.map(c => { const x = v[c] || {status: "not verified", checks: []};
    return `<tr><td>${esc(c)}</td><td>${esc(x.action || "—")}</td><td>${esc(AGENT(x.decision_source || "—"))}</td><td>${tag(x.status)}<div class="small muted">${esc(x.disposition || "")}</div></td>
    <td><details ${x.checks.some(ch => !ch.ok) ? "open" : ""}><summary>${x.checks.filter(ch => ch.ok).length} of ${x.checks.length} checks passed</summary>
    <ul class="checks">${x.checks.map(ch => `<li class="${ch.ok ? "" : "no"}">${esc(ch.check)}${ch.detail ? ` <span class="muted small">${esc(ch.detail)}</span>` : ""}</li>`).join("")}</ul></details></td></tr>`; }).join("")}</tbody></table>
  <h2>Agent proposals compared with the rules</h2>
  <table><thead><tr><th>Case</th><th>Proposal</th><th>Rules</th><th>Result</th><th>Differences</th></tr></thead><tbody>
  ${compared.map(p => `<tr><td>${esc(p.case_id)} v${p.version}</td><td>${esc(p.action)}<div class="small muted">${esc(p.rationale)}</div></td>
   <td>${esc(((p.checker || {}).allowed_actions || []).join(" or ") || "not covered")}</td><td>${tag(p.comparison.status)}</td>
   <td class="small">${p.comparison.differences.map(x => `${esc(x.field)}: ${esc(x.gemini)} vs ${esc(x.rules)}`).join("<br>") || "—"}</td></tr>`).join("")}</tbody></table>
  <h2>Human decisions</h2>${d.decisions.length ? `<table><thead><tr><th>Case</th><th>Kind</th><th>Decision</th><th>By</th></tr></thead><tbody>
  ${d.decisions.map(x => `<tr><td>${esc(x.case_id || "—")}</td><td>${esc(x.kind)}</td><td>${esc((x.response || {}).choice || (x.response || {}).answer || x.status)}${(x.response || {}).reason ? `<div class="small muted">${esc(x.response.reason)}</div>` : ""}</td><td class="small">${esc((x.response || {}).actor || "")}</td></tr>`).join("")}</tbody></table>` : `<p class="muted">None were needed.</p>`}
  <h2>Writes through the gate</h2><table><thead><tr><th>Case</th><th>Action</th><th>Status</th><th>Result</th></tr></thead><tbody>
  ${d.writes.map(w => `<tr><td>${esc(w.case_id)}</td><td>${esc(w.action_type)}<div class="small muted">${esc(w.route)}</div></td><td>${tag(w.status)}</td><td class="small">${esc((w.result || {}).message || "")}</td></tr>`).join("")}</tbody></table>
  <h2>Evidence</h2><div class="gallery" id="gallery"></div>
  <p style="margin-top:20px"><a class="btn quiet" href="/runs/${rid}/report.md" target="_blank">Open report.md</a> <a class="btn quiet" href="/runs/${rid}/report.json" target="_blank">report.json</a></p>`;
  const rep = await fetch(`/runs/${rid}/report.json`).then(x => x.ok ? x.json() : null).catch(() => null);
  if (rep) $("#gallery").innerHTML = rep.screenshots.slice(-12).map(s => `<figure><a href="/runs/${rid}/${s.path}" target="_blank"><img loading="lazy" src="/runs/${rid}/${s.path}" alt="${esc(s.label)}"></a><figcaption>${esc(s.label)}</figcaption></figure>`).join("") || `<p class="muted">No screenshots.</p>`;
}

/* ---------------- tests ---------------- */
async function testsView() {
  setNav("tests");
  const d = await api("/api/evals");
  const latest = {};
  d.results.forEach(r => latest[`${r.scenario}|${r.engine}`] = r);
  const rows = d.scenarios.flatMap(s => ["scripted", "gemini"].map(en => ({s, en, r: latest[`${s.id}|${en}`]}))).filter(x => x.r || x.en === "scripted");
  const passed = en => Object.values(latest).filter(r => r.engine === en && r.passed).length;
  const ran = en => Object.values(latest).filter(r => r.engine === en && r.passed !== null && r.passed !== undefined).length;
  view.innerHTML = `<h1>Tests</h1>
  <p class="lede">Each scenario starts from fresh sandbox data, may inject a failure, runs the operator, then checks the
  database against expectations written by hand from the policies. Offline runs use the scripted client and make no Gemini calls.</p>
  <p><strong>Scripted:</strong> ${passed("scripted")} of ${ran("scripted")} passing · <strong>Gemini live:</strong> ${passed("gemini")} of ${ran("gemini")} passing</p>
  <div style="display:flex;gap:10px;margin:14px 0 20px"><button id="off" ${d.running ? "disabled" : ""}>Run offline suite</button>
   <button class="agent" id="live" ${d.running || !overview.gemini.configured ? "disabled" : ""}>Run live smoke suite (Gemini, max 160 calls)</button>
   ${d.running ? `<span class="muted">A suite is running…</span>` : ""}</div>
  <table><thead><tr><th>Scenario</th><th>Covers</th><th>Engine</th><th>Result</th><th>Final status</th><th>Model calls</th><th>Run</th></tr></thead><tbody>
  ${rows.map(({s, en, r}) => `<tr><td>${esc(s.id)}</td><td class="small">${esc((s.covers || []).join(", "))}</td><td>${esc(ENGINE[en])}</td>
    <td>${r ? (r.passed === null ? tag("not run") : r.passed ? tag("passed").replace("tag ", "tag ok ") : `${tag("failed")}<div class="small">${(r.checks || []).filter(c => !c.ok).map(c => esc(c.check)).join("<br>")}</div>`) : `<span class="muted small">not run</span>`}</td>
    <td>${r ? tag(r.final_status) : ""}</td><td class="num">${r ? (r.model_calls ?? "") : ""}</td><td>${r && r.run_id ? `<a href="#/report/${r.run_id}">${esc(r.run_id)}</a>` : ""}</td></tr>`).join("")}</tbody></table>`;
  $("#off").onclick = async () => { await api("/api/evals", {method: "POST", body: {suite: "offline", engine: "scripted"}}); testsView(); };
  $("#live").onclick = async () => { await api("/api/evals", {method: "POST", body: {suite: "live-smoke", engine: "gemini", max_calls: 160}}); testsView(); };
  if (d.running) pollTimer = setInterval(async () => { const x = await api("/api/evals"); if (!x.running) testsView(); }, 4000);
}

route();
