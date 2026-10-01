const STAGE_ORDER = ["research", "dedup", "judgment", "draft", "guardrail"];
const STAGE_LABELS = {
  research: "Research (PredictLeads + Tavily)",
  dedup: "Dedup signals",
  judgment: "Relevance judgment",
  draft: "Draft generation",
  guardrail: "Grounding guardrail check",
};

let activeRunId = null;
let activeSource = null;
let openStages = new Set(); // which stage rows are expanded -- survives re-renders while a run streams in

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}

async function loadRuns() {
  const res = await fetch("/runs");
  if (res.status === 401) {
    window.location.href = "/login";
    return;
  }
  const runs = await res.json();
  const container = document.getElementById("runs-table");
  if (!runs.length) {
    container.innerHTML = '<p class="empty-state">No runs yet.</p>';
    return;
  }
  container.innerHTML = runs
    .map(
      (r) => `
      <div class="run-row ${r.id === activeRunId ? "active" : ""}" data-id="${r.id}">
        <div class="name">${escapeHtml(r.prospect_name)}</div>
        <div class="company">${escapeHtml(r.company_name)}</div>
        <span class="badge ${r.status}">${r.status.replace(/_/g, " ")}</span>
      </div>`
    )
    .join("");
  container.querySelectorAll(".run-row").forEach((el) => {
    el.addEventListener("click", () => selectRun(el.dataset.id));
  });
}

function stageIcon(status) {
  if (status === "done") return "✅";
  if (status === "error") return "❌";
  if (status === "running") return "⏳";
  return "⬜";
}

function renderStageBody(name, output, error) {
  if (error) return `<span style="color:var(--red)">${escapeHtml(error)}</span>`;
  if (output == null) return "waiting...";

  if (name === "research") {
    const errors = output.predictleads_errors || [];
    const errorBlock = errors.length
      ? `<div style="color:var(--yellow); margin-top:0.4rem">PredictLeads errors (fell back to Tavily):<br/>${errors.map((e) => escapeHtml(e)).join("<br/>")}</div>`
      : "";
    return `
      <div>domain: ${escapeHtml(output.domain || "unresolved")} · used_fallback: ${output.used_fallback} · signals found: ${output.signal_count}</div>${errorBlock}`;
  }
  if (name === "dedup") {
    return `<div>${output.count_before} raw signals &rarr; ${output.count_after} after merging duplicates.</div>`;
  }
  if (name === "judgment") {
    return `
      <div><strong>confidence:</strong> ${escapeHtml(output.confidence)}</div>
      <div><strong>chosen_signal_id:</strong> ${escapeHtml(output.chosen_signal_id || "none")}</div>
      <div><strong>reasoning:</strong> ${escapeHtml(output.reasoning)}</div>
      ${output.rejected_reason ? `<div><strong>why rejected:</strong> ${escapeHtml(output.rejected_reason)}</div>` : ""}`;
  }
  if (name === "draft") {
    return `
      <div><strong>subject:</strong> ${escapeHtml(output.subject)}</div>
      <div><strong>cited signals:</strong> ${escapeHtml((output.cited_signal_ids || []).join(", "))}</div>`;
  }
  if (name === "guardrail") {
    const claims = (output.claims || [])
      .map((c) => `<div>${c.supported ? "✅" : "⚠️"} ${escapeHtml(c.claim)} (signal ${escapeHtml(c.signal_id)})</div>`)
      .join("");
    return `<div><strong>all_supported:</strong> ${output.all_supported}</div>${claims}`;
  }
  return `<pre>${escapeHtml(JSON.stringify(output, null, 2))}</pre>`;
}

function renderSignals(run) {
  const signals = run.signals || [];
  if (!signals.length) return "";
  const chosenId = run.chosen_hook?.chosen_signal_id;
  const items = signals
    .map(
      (s) => `
      <div class="signal-item ${s.id === chosenId ? "chosen" : ""}">
        <div><strong>[${s.id}] ${escapeHtml(s.title)}</strong> &mdash; ${escapeHtml(s.type)} &middot; ${escapeHtml(s.date || "undated")}</div>
        <div>${escapeHtml(s.snippet || "")}</div>
        ${s.url ? `<a href="${escapeHtml(s.url)}" target="_blank" rel="noopener">source</a>` : ""}
      </div>`
    )
    .join("");
  return `<h2>Signals retrieved</h2>${items}`;
}

function renderDraft(run) {
  if (!run.draft_body) return "";
  return `
    <div class="draft-box">
      <h3>Draft (pending human review -- nothing is sent automatically)</h3>
      <div class="subject">Subject: ${escapeHtml(run.draft_subject || "")}</div>
      <div>${escapeHtml(run.draft_body).replace(/\n/g, "<br/>")}</div>
    </div>`;
}

function renderDetail(run) {
  const container = document.getElementById("run-detail");
  const stagesByName = {};
  (run.stages || []).forEach((s) => (stagesByName[s.name] = s));

  const stageRows = STAGE_ORDER.map((name) => {
    const stage = stagesByName[name];
    const status = stage ? stage.status : "pending";
    const output = stage?.output;
    const isOpen = openStages.has(name);
    return `
      <div class="stage-row ${isOpen ? "open" : ""}">
        <div class="stage-header" data-stage="${name}">
          <span><span class="icon">${stageIcon(status)}</span>${STAGE_LABELS[name]}</span>
          <span class="badge ${status}">${status}</span>
        </div>
        <div class="stage-body">${renderStageBody(name, output, stage?.error)}</div>
      </div>`;
  }).join("");

  container.innerHTML = `
    <h2>${escapeHtml(run.prospect_name)} &middot; ${escapeHtml(run.company_name)}</h2>
    <span class="badge ${run.status}">${run.status.replace(/_/g, " ")}</span>
    <div style="margin-top:1rem">${stageRows}</div>
    ${renderDraft(run)}
    ${renderSignals(run)}
  `;

  // Re-render (e.g. from an SSE update) replaces this whole subtree, so click
  // handlers must be re-attached each time -- state itself lives in openStages,
  // not in the DOM, which is what makes it survive the replacement.
  container.querySelectorAll(".stage-header").forEach((el) => {
    el.addEventListener("click", () => {
      const name = el.dataset.stage;
      if (openStages.has(name)) {
        openStages.delete(name);
      } else {
        openStages.add(name);
      }
      el.parentElement.classList.toggle("open");
    });
  });
}

function selectRun(runId) {
  if (runId !== activeRunId) openStages = new Set();
  activeRunId = runId;
  if (activeSource) activeSource.close();
  loadRuns();

  activeSource = new EventSource(`/runs/${runId}/stream`);
  activeSource.onmessage = (event) => {
    const run = JSON.parse(event.data);
    renderDetail(run);
  };
}

document.getElementById("new-run-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const prospect_name = document.getElementById("prospect_name").value.trim();
  const company_name = document.getElementById("company_name").value.trim();
  const title = document.getElementById("title").value.trim() || null;

  const res = await fetch("/runs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prospect_name, company_name, title }),
  });
  const { id } = await res.json();
  document.getElementById("new-run-form").reset();
  await loadRuns();
  selectRun(id);
});

document.getElementById("logout-btn").addEventListener("click", async () => {
  await fetch("/auth/logout", { method: "POST" });
  window.location.href = "/login";
});

loadRuns();
setInterval(() => {
  if (!activeRunId) loadRuns();
}, 4000);
