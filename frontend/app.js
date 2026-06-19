/* ─────────────────────────────────────────────────────────────
   Knowledge Agent — Frontend logic
   Talks to the FastAPI backend over a small REST API.
   ───────────────────────────────────────────────────────────── */

const API = "";  // same origin
const SESSION_ID = (() => {
  let id = sessionStorage.getItem("ka_session");
  if (!id) {
    id = "s_" + Math.random().toString(36).slice(2) + Date.now().toString(36);
    sessionStorage.setItem("ka_session", id);
  }
  return id;
})();

const headers = { "X-Session-Id": SESSION_ID };

// Robot avatar SVG (used for each agent message)
const ROBOT_SVG = `
<svg viewBox="0 0 32 32" width="18" height="18">
  <rect x="7" y="10" width="18" height="14" rx="4" fill="none" stroke="#081626" stroke-width="2.2"/>
  <circle cx="12" cy="17" r="2" fill="#081626"/>
  <circle cx="20" cy="17" r="2" fill="#081626"/>
  <line x1="16" y1="5" x2="16" y2="10" stroke="#081626" stroke-width="2.2"/>
  <circle cx="16" cy="4" r="1.8" fill="#081626"/>
</svg>`;

// ─── Elements ───────────────────────────────────────────────
const chat = document.getElementById("chat");
const emptyState = document.getElementById("empty-state");
const queryInput = document.getElementById("query-input");
const sendBtn = document.getElementById("send-btn");
const fileInput = document.getElementById("file-input");
const uploadZone = document.getElementById("upload-zone");
const uploadText = document.getElementById("upload-text");
const uploadStatus = document.getElementById("upload-status");
const docList = document.getElementById("doc-list");
const clearBtn = document.getElementById("clear-btn");

const statDocs = document.getElementById("stat-docs");
const statChunks = document.getElementById("stat-chunks");
const statQueries = document.getElementById("stat-queries");
const statusPill = document.getElementById("status-pill");
const statusText = document.getElementById("status-text");

let documents = [];

// ─── Helpers ────────────────────────────────────────────────
function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

// Minimal, safe markdown: bold, bullet lists, paragraphs
function renderMarkdown(text) {
  const safe = escapeHtml(text);
  const lines = safe.split("\n");
  let html = "";
  let inList = false;

  for (let raw of lines) {
    let line = raw.trim();
    if (!line) { if (inList) { html += "</ul>"; inList = false; } continue; }

    line = line.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");

    const bullet = line.match(/^[-*•]\s+(.*)/);
    if (bullet) {
      if (!inList) { html += "<ul>"; inList = true; }
      html += `<li>${bullet[1]}</li>`;
    } else {
      if (inList) { html += "</ul>"; inList = false; }
      html += `<p>${line}</p>`;
    }
  }
  if (inList) html += "</ul>";
  return html;
}

function setStatus(state) {
  if (state === "active") {
    statusPill.className = "pill pill-status active";
    statusPill.innerHTML = '<div class="pulse"></div><span>Active</span>';
  } else if (state === "thinking") {
    statusPill.className = "pill pill-status active";
    statusPill.innerHTML = '<div class="pulse"></div><span>Processing</span>';
  } else {
    statusPill.className = "pill pill-status";
    statusPill.innerHTML = '<div class="status-dot-idle"></div><span>Idle</span>';
  }
}

// ─── Rendering ──────────────────────────────────────────────
function clearEmptyState() {
  if (emptyState && emptyState.parentNode) emptyState.remove();
}

function addUserMessage(text) {
  clearEmptyState();
  const el = document.createElement("div");
  el.className = "msg user";
  el.innerHTML = `
    <div class="avatar user">YOU</div>
    <div class="bubble user">${escapeHtml(text)}</div>`;
  chat.appendChild(el);
  chat.scrollTop = chat.scrollHeight;
}

function addTypingIndicator() {
  const el = document.createElement("div");
  el.className = "msg bot";
  el.id = "typing-msg";
  el.innerHTML = `
    <div class="avatar bot">${ROBOT_SVG}</div>
    <div class="bubble bot"><div class="typing"><span></span><span></span><span></span></div></div>`;
  chat.appendChild(el);
  chat.scrollTop = chat.scrollHeight;
}

function removeTyping() {
  const t = document.getElementById("typing-msg");
  if (t) t.remove();
}

function addBotMessage(answer, sources, hadContext) {
  removeTyping();
  const el = document.createElement("div");
  el.className = "msg bot";

  let inner = renderMarkdown(answer);

  if (sources && sources.length && hadContext) {
    const srcLines = sources.map(s => `— ${escapeHtml(s)}`).join("<br>");
    inner += `
      <div class="citation">
        <div class="cit-label">Referenced from</div>
        <div class="cit-src">${srcLines}</div>
      </div>`;
    inner += `<div class="badge high"><span class="dot"></span>HIGH CONFIDENCE</div>`;
  } else if (!hadContext) {
    inner += `<div class="no-context">No matching content found in indexed documents.</div>`;
  }

  el.innerHTML = `
    <div class="avatar bot">${ROBOT_SVG}</div>
    <div class="bubble bot">${inner}</div>`;
  chat.appendChild(el);
  chat.scrollTop = chat.scrollHeight;
}

function renderDocList() {
  docList.innerHTML = "";
  documents.forEach(doc => {
    const ext = doc.filename.split(".").pop().toUpperCase().slice(0, 3);
    const shortName = doc.filename.length > 26 ? doc.filename.slice(0, 23) + "..." : doc.filename;
    const card = document.createElement("div");
    card.className = "doc-card";
    card.innerHTML = `
      <div class="doc-top">
        <div class="doc-icon">${ext}</div>
        <div class="doc-name">${escapeHtml(shortName)}</div>
        <button class="doc-remove" title="Remove" data-id="${doc.doc_id}">×</button>
      </div>
      <div class="doc-meta">
        <div class="pulse"></div>
        <span>${doc.chunk_count} chunks indexed</span>
      </div>`;
    docList.appendChild(card);
  });

  docList.querySelectorAll(".doc-remove").forEach(btn => {
    btn.addEventListener("click", () => removeDocument(btn.dataset.id));
  });

  uploadText.textContent = documents.length ? "Add another document" : "Index a document";
}

// ─── API calls ──────────────────────────────────────────────
async function refreshStats() {
  const r = await fetch(`${API}/api/stats`, { headers });
  const s = await r.json();
  statDocs.textContent = s.documents;
  statChunks.textContent = s.chunks;
  statQueries.textContent = s.queries;
  setStatus(s.documents > 0 ? "active" : "idle");
  queryInput.disabled = s.documents === 0;
  sendBtn.disabled = s.documents === 0 || !queryInput.value.trim();
}

async function loadDocuments() {
  const r = await fetch(`${API}/api/documents`, { headers });
  documents = await r.json();
  renderDocList();
  await refreshStats();
}

async function uploadFile(file) {
  uploadStatus.innerHTML = `<div class="status-msg working">Indexing ${escapeHtml(file.name)}...</div>`;
  const form = new FormData();
  form.append("file", file);

  try {
    const r = await fetch(`${API}/api/documents`, { method: "POST", headers, body: form });
    if (!r.ok) {
      const err = await r.json();
      uploadStatus.innerHTML = `<div class="status-msg error">${escapeHtml(err.detail || "Upload failed")}</div>`;
      return;
    }
    uploadStatus.innerHTML = "";
    await loadDocuments();
  } catch (e) {
    uploadStatus.innerHTML = `<div class="status-msg error">Upload failed. Try again.</div>`;
  }
}

async function removeDocument(docId) {
  await fetch(`${API}/api/documents/${docId}`, { method: "DELETE", headers });
  await loadDocuments();
}

async function sendQuery() {
  const q = queryInput.value.trim();
  if (!q || documents.length === 0) return;

  queryInput.value = "";
  sendBtn.disabled = true;
  addUserMessage(q);
  addTypingIndicator();
  setStatus("thinking");

  try {
    const r = await fetch(`${API}/api/query`, {
      method: "POST",
      headers: { ...headers, "Content-Type": "application/json" },
      body: JSON.stringify({ query: q }),
    });

    if (!r.ok) {
      const err = await r.json();
      addBotMessage(err.detail || "Something went wrong. Please try again.", [], false);
    } else {
      const data = await r.json();
      addBotMessage(data.answer, data.sources, data.had_context);
    }
  } catch (e) {
    addBotMessage("Connection error. Please try again.", [], false);
  } finally {
    await refreshStats();
    setStatus("active");
  }
}

async function clearConversation() {
  await fetch(`${API}/api/conversation`, { method: "DELETE", headers });
  chat.querySelectorAll(".msg").forEach(m => m.remove());
  if (documents.length === 0 && !document.getElementById("empty-state")) {
    location.reload();
  }
  await refreshStats();
}

// ─── Events ─────────────────────────────────────────────────
fileInput.addEventListener("change", e => {
  if (e.target.files.length) uploadFile(e.target.files[0]);
  fileInput.value = "";
});

uploadZone.addEventListener("dragover", e => { e.preventDefault(); uploadZone.classList.add("dragover"); });
uploadZone.addEventListener("dragleave", () => uploadZone.classList.remove("dragover"));
uploadZone.addEventListener("drop", e => {
  e.preventDefault();
  uploadZone.classList.remove("dragover");
  if (e.dataTransfer.files.length) uploadFile(e.dataTransfer.files[0]);
});

queryInput.addEventListener("input", () => {
  sendBtn.disabled = !queryInput.value.trim() || documents.length === 0;
});
queryInput.addEventListener("keydown", e => {
  if (e.key === "Enter" && !sendBtn.disabled) sendQuery();
});
sendBtn.addEventListener("click", sendQuery);
clearBtn.addEventListener("click", clearConversation);

// ─── Init ───────────────────────────────────────────────────
loadDocuments();
