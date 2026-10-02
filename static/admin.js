let queueData = [];
let selectedId = null;

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}

function renderStats() {
  const counts = { pending_approval: 0, sent: 0, rejected: 0, send_failed: 0 };
  queueData.forEach((r) => {
    if (r.status in counts) counts[r.status]++;
  });
  const cards = [
    { cls: "total", label: "Total requests", value: queueData.length },
    { cls: "pending", label: "Pending", value: counts.pending_approval },
    { cls: "sent", label: "Accepted & sent", value: counts.sent },
    { cls: "rejected", label: "Rejected", value: counts.rejected },
  ];
  if (counts.send_failed > 0) {
    cards.push({ cls: "failed", label: "Send failed", value: counts.send_failed });
  }
  document.getElementById("stats-row").innerHTML = cards
    .map(
      (c) => `
      <div class="stat-card ${c.cls}">
        <div class="stat-number">${c.value}</div>
        <div class="stat-label">${c.label}</div>
      </div>`
    )
    .join("");
}

function renderList() {
  const container = document.getElementById("queue-list");
  if (!queueData.length) {
    container.innerHTML = '<p class="empty-state">No requests yet.</p>';
    return;
  }
  container.innerHTML = queueData
    .map(
      (r) => `
      <div class="run-row ${r.id === selectedId ? "active" : ""}" data-id="${r.id}">
        <div class="name">${escapeHtml(r.prospect_name)}</div>
        <div class="company">${escapeHtml(r.company_name)}</div>
        <span class="badge ${r.status}">${r.status.replace(/_/g, " ")}</span>
      </div>`
    )
    .join("");
  container.querySelectorAll(".run-row").forEach((el) => {
    el.addEventListener("click", () => selectRequest(el.dataset.id));
  });
}

function renderDetail(item) {
  const container = document.getElementById("request-detail");

  let actionBlock = "";
  if (item.status === "pending_approval") {
    actionBlock = `
      <div class="admin-actions">
        <button class="btn-primary" id="approve-btn">Approve &amp; send</button>
        <div class="reject-block">
          <textarea id="reject-reason" placeholder="Reason for rejecting (required)" rows="2"></textarea>
          <button class="btn-outline" id="reject-btn">Reject</button>
        </div>
      </div>`;
  } else if (item.status === "sent") {
    actionBlock = `<p class="ok-text">Sent to ${escapeHtml(item.prospect_email)}.</p>`;
  } else if (item.status === "rejected") {
    actionBlock = `<p class="bad-text">Rejected: ${escapeHtml(item.rejection_reason || "")}</p>`;
  } else if (item.status === "send_failed") {
    actionBlock = `<p class="bad-text">Send failed: ${escapeHtml(item.send_error || "unknown error")}</p>`;
  }

  container.innerHTML = `
    <h2>${escapeHtml(item.prospect_name)} &middot; ${escapeHtml(item.company_name)}${item.title ? " &middot; " + escapeHtml(item.title) : ""}</h2>
    <div class="meta">requested by ${escapeHtml(item.requested_by)} &middot; sending to ${escapeHtml(item.prospect_email || "")}</div>
    <span class="badge ${item.status}">${item.status.replace(/_/g, " ")}</span>
    <div class="draft-box" style="margin-top:1rem">
      <div class="subject">Subject: ${escapeHtml(item.draft_subject || "")}</div>
      <div>${escapeHtml(item.draft_body || "").replace(/\n/g, "<br/>")}</div>
    </div>
    <div style="margin-top:1rem">${actionBlock}</div>
  `;

  const approveBtn = document.getElementById("approve-btn");
  if (approveBtn) {
    approveBtn.addEventListener("click", () => approveRequest(item.id));
  }
  const rejectBtn = document.getElementById("reject-btn");
  if (rejectBtn) {
    rejectBtn.addEventListener("click", () => rejectRequest(item.id));
  }
}

function selectRequest(id) {
  selectedId = id;
  renderList();
  const item = queueData.find((r) => r.id === id);
  if (item) renderDetail(item);
}

async function approveRequest(id) {
  if (!confirm("Send this email now? This cannot be undone.")) return;
  const btn = document.getElementById("approve-btn");
  btn.disabled = true;
  btn.textContent = "Sending...";
  const res = await fetch(`/admin/runs/${id}/approve`, { method: "POST" });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    alert(body.detail || "Send failed.");
    btn.disabled = false;
    btn.textContent = "Approve & send";
    return;
  }
  await loadQueue();
  selectRequest(id);
}

async function rejectRequest(id) {
  const reason = document.getElementById("reject-reason").value.trim();
  if (!reason) {
    alert("Please enter a reason for rejecting.");
    return;
  }
  const btn = document.getElementById("reject-btn");
  btn.disabled = true;
  btn.textContent = "Rejecting...";
  const res = await fetch(`/admin/runs/${id}/reject`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reason }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    alert(body.detail || "Reject failed.");
    btn.disabled = false;
    btn.textContent = "Reject";
    return;
  }
  await loadQueue();
  selectRequest(id);
}

async function loadQueue() {
  const res = await fetch("/admin/queue");
  if (res.status === 401) {
    window.location.href = "/login";
    return;
  }
  if (res.status === 403) {
    document.getElementById("queue-list").innerHTML =
      '<p class="bad-text">This account doesn\'t have admin access (ADMIN_DASHBOARD_ACCESS is set to admin_only).</p>';
    document.getElementById("request-detail").innerHTML = "";
    return;
  }
  queueData = await res.json();
  renderStats();
  renderList();
}

loadQueue();
// Only auto-poll the list while nothing is selected, so an in-progress
// review (and an unsent rejection reason being typed) is never disrupted.
setInterval(() => {
  if (!selectedId) loadQueue();
}, 5000);
