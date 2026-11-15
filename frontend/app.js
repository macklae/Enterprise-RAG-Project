"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const state = {
  roles: [],
  managers: [],
  admin: [],
  samples: [],
  role: "",
  user: "",
  username: "",
  csrfToken: "",
  authenticated: false,
  busy: false,
};

const ROLE_HINTS = {
  employee: "Sees internal policies, SOPs and the service catalogue.",
  line_manager:
    "Also sees the operations report and training rows for their own team.",
  hr_ld: "Sees all training records. Can manage documents.",
  sre_ops:
    "Sees the operations report and the on-call roster. Can manage documents.",
};

// ---------------------------------------------------------------- helpers
function esc(s) {
  return String(s ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
}
async function api(path, opts = {}) {
  const method = (opts.method || "GET").toUpperCase();
  const headers = new Headers(opts.headers || {});

  if (["POST", "PUT", "PATCH", "DELETE"].includes(method) && state.csrfToken) {
    headers.set("X-CSRF-Token", state.csrfToken);
  }

  const res = await fetch(path, {
    ...opts,
    method,
    headers,
    credentials: "same-origin",
  });

  let data = null;

  try {
    data = await res.json();
  } catch (_) {
    // Response is not JSON.
  }

  if (!res.ok) {
    const detail = data?.detail || data?.error;
    throw new Error(
      typeof detail === "string" ? detail : `Request failed (${res.status})`,
    );
  }

  return data;
}

async function login(username, password) {
  const result = await api("/api/auth/login", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ username, password }),
  });

  state.username = result.username;
  state.role = result.role;
  state.user = result.manager_name || "";
  state.csrfToken = result.csrf_token;
  state.authenticated = true;

  $("#currentUsername").textContent = state.username;
  $("#currentRole").textContent = state.role.replaceAll("_", " ");
  $("#roleHint").textContent = ROLE_HINTS[state.role] || "";

  $("#loginPassword").value = "";
  $("#loginScreen").hidden = true;
  $("#mainApp").hidden = false;
}

$("#loginForm").addEventListener("submit", async (event) => {
  event.preventDefault();

  const username = $("#loginUsername").value.trim();
  const password = $("#loginPassword").value;
  const errorBox = $("#loginError");

  errorBox.hidden = true;

  try {
    await login(username, password);
    await loadIdentities();
  } catch (error) {
    errorBox.textContent = error.message;
    errorBox.hidden = false;
  }
});

async function restoreSession() {
  try {
    const result = await api("/api/auth/me");

    state.username = result.username;
    state.role = result.role;
    state.user = result.manager_name || "";
    state.csrfToken = result.csrf_token;
    state.authenticated = true;

    $("#loginScreen").hidden = true;
    $("#mainApp").hidden = false;

    await loadIdentities();
  } catch (error) {
    state.username = "";
    state.role = "";
    state.user = "";
    state.csrfToken = "";
    state.authenticated = false;

    $("#mainApp").hidden = true;
    $("#loginScreen").hidden = false;
  }
}

async function logout() {
  try {
    await api("/api/auth/logout", {
      method: "POST",
    });
  } catch (error) {
    console.error("Logout failed:", error);
    alert("Could not sign out. Please try again.");
    return;
  }

  state.username = "";
  state.role = "";
  state.user = "";
  state.csrfToken = "";
  state.authenticated = false;

  $("#mainApp").hidden = true;
  $("#loginScreen").hidden = false;

  $("#loginUsername").value = "";
  $("#loginPassword").value = "";
  $("#loginError").hidden = true;
}

$("#logoutBtn").addEventListener("click", async () => {
  await logout();
});

function store(key, val) {
  try {
    localStorage.setItem(key, val);
  } catch (_) {
    /* private mode */
  }
}
function recall(key) {
  try {
    return localStorage.getItem(key);
  } catch (_) {
    return null;
  }
}
function ms(v) {
  return v >= 1000 ? (v / 1000).toFixed(1) + " s" : Math.round(v) + " ms";
}

// Small, safe Markdown: escapes first, then adds bold, code, lists, tables and citation chips.
function renderMarkdown(text) {
  const lines = String(text || "")
    .replace(/\r/g, "")
    .split("\n");
  const out = [];
  let i = 0;
  const inline = (s) =>
    esc(s)
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(
        /\[(\d{1,2})\]/g,
        '<button type="button" class="cite" data-n="$1" title="Show source $1">$1</button>',
      );
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i++;
      continue;
    }
    if (
      /^\s*\|/.test(line) &&
      i + 1 < lines.length &&
      /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])
    ) {
      const cells = (l) =>
        l
          .trim()
          .replace(/^\||\|$/g, "")
          .split("|")
          .map((c) => c.trim());
      const head = cells(line);
      i += 2;
      const rows = [];
      while (i < lines.length && /^\s*\|/.test(lines[i]))
        rows.push(cells(lines[i++]));
      out.push(
        "<table><thead><tr>" +
          head.map((h) => `<th>${inline(h)}</th>`).join("") +
          "</tr></thead><tbody>" +
          rows
            .map(
              (r) =>
                "<tr>" +
                r.map((c) => `<td>${inline(c)}</td>`).join("") +
                "</tr>",
            )
            .join("") +
          "</tbody></table>",
      );
      continue;
    }
    if (/^\s*[-*] /.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*[-*] /.test(lines[i]))
        items.push(lines[i++].replace(/^\s*[-*] /, ""));
      out.push(
        "<ul>" + items.map((x) => `<li>${inline(x)}</li>`).join("") + "</ul>",
      );
      continue;
    }
    if (/^\s*\d+[.)] /.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*\d+[.)] /.test(lines[i]))
        items.push(lines[i++].replace(/^\s*\d+[.)] /, ""));
      out.push(
        "<ol>" + items.map((x) => `<li>${inline(x)}</li>`).join("") + "</ol>",
      );
      continue;
    }
    const para = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !/^\s*([-*] |\d+[.)] |\|)/.test(lines[i])
    )
      para.push(lines[i++]);
    if (!para.length) {
      para.push(lines[i++]);
    }
    const html = para.map(inline).join("<br>");
    out.push(
      /^Found in restricted records/.test(para[0])
        ? `<p class="restricted-note">${html}</p>`
        : `<p>${html}</p>`,
    );
  }
  return out.join("");
}

// ---------------------------------------------------------------- status and identity
async function loadStatus() {
  const box = $("#status");
  try {
    const h = await api("/api/health");
    const pill = (label, ok, warn) =>
      `<span class="pill ${ok ? "ok" : warn ? "warn" : "bad"}"><span class="dot"></span>${esc(label)}</span>`;
    box.innerHTML =
      pill("Qdrant", h.qdrant) +
      pill("Redis cache", h.redis, true) +
      pill(
        h.embedding_model_loaded ? "BGE-M3 ready" : "BGE-M3 loading",
        h.embedding_model_loaded,
        true,
      ) +
      pill(
        h.writer_configured
          ? "Writer: " + (h.writer_model || "OpenAI")
          : "Writer not set",
        h.writer_configured,
        true,
      ) +
      `<span class="pill"><span class="dot"></span>${h.chunks} chunks</span>`;
  } catch (_) {
    box.innerHTML =
      '<span class="pill bad"><span class="dot"></span>API offline</span>';
  }
}

function fillSelect(sel, items, value) {
  sel.innerHTML = items
    .map((i) => `<option value="${esc(i.value)}">${esc(i.label)}</option>`)
    .join("");
  if (value && items.some((i) => i.value === value)) sel.value = value;
}

function syncIdentity() {
  if (!state.authenticated) return;

  $("#currentUsername").textContent = state.username;
  $("#currentRole").textContent = state.role.replaceAll("_", " ");
  $("#roleHint").textContent = ROLE_HINTS[state.role] || "";

  $("#admin").hidden = !state.admin.includes(state.role);

  loadDocs();
}

async function loadIdentities() {
  const d = await api("/api/identities");

  state.roles = d.roles;
  state.managers = d.managers;
  state.admin = d.admin_roles;
  state.samples = d.sample_questions;

  $("#samples").innerHTML = d.sample_questions
    .map((q) => `<button type="button" class="chip">${esc(q)}</button>`)
    .join("");

  syncIdentity();
}

// ---------------------------------------------------------------- documents
async function loadDocs() {
  const list = $("#docs");
  try {
    const q = new URLSearchParams({ role: state.role, user: state.user });
    const d = await api("/api/documents?" + q);
    if (!d.documents.length) {
      list.innerHTML =
        '<li class="empty-docs">No documents yet. ' +
        (state.admin.includes(state.role)
          ? "Load the folder or upload a file."
          : "Ask an admin to load documents.") +
        "</li>";
      return;
    }
    const canDelete = state.admin.includes(state.role);
    list.innerHTML = d.documents
      .map(
        (x) => `
      <li class="doc">
        <b title="${esc(x.doc_name)}">${esc(x.doc_name)}</b>
        <small>${esc(x.doc_id)} · v${esc(x.version)}</small>
        <div class="row">
          <span class="badge ${esc(x.classification)}">${esc(x.classification)}</span>
          <span class="badge neutral">${esc(x.doc_type)}</span>
          <span class="badge neutral">${x.chunks} chunks</span>
        </div>
        ${canDelete ? `<button class="del" data-doc="${esc(x.doc_id)}" title="Remove document" aria-label="Remove ${esc(x.doc_name)}"><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg></button>` : ""}
      </li>`,
      )
      .join("");
  } catch (e) {
    list.innerHTML = `<li class="empty-docs">${esc(e.message)}</li>`;
  }
}

function trackJob(id, name) {
  const box = document.createElement("div");
  box.className = "job";
  box.innerHTML = `<div class="top"><b>${esc(name)}</b><span class="pct">0%</span></div><div class="bar"><i></i></div><div class="jmsg">Queued</div>`;
  $("#jobs").prepend(box);
  const tick = async () => {
    try {
      const j = await api("/api/jobs/" + id);
      $(".bar i", box).style.width = j.progress + "%";
      $(".pct", box).textContent = j.progress + "%";
      let msg = j.message;
      if (j.status === "done" && j.result) {
        msg =
          `${j.result.chunk_count} chunks stored` +
          (j.result.warnings && j.result.warnings.length
            ? ". " + j.result.warnings.join(" ")
            : "");
      }
      $(".jmsg", box).textContent = msg;
      if (j.status === "done") {
        box.classList.add("done");
        loadDocs();
        loadStatus();
        setTimeout(() => box.remove(), 12000);
        return;
      }
      if (j.status === "error") {
        box.classList.add("err");
        return;
      }
    } catch (_) {
      return;
    }
    setTimeout(tick, 600);
  };
  tick();
}

async function uploadFiles(files) {
  for (const f of files) {
    const fd = new FormData();
    fd.append("file", f);
    fd.append("role", state.role);
    fd.append("user", state.user);
    fd.append("classification", $("#classification").value);
    try {
      const r = await api("/api/documents", { method: "POST", body: fd });
      trackJob(r.job_id, f.name);
    } catch (e) {
      alert(e.message);
    }
  }
}

// ---------------------------------------------------------------- chat
function scrollDown() {
  const s = $("#stream");
  s.scrollTop = s.scrollHeight;
}

function addUser(text) {
  $("#empty")?.remove();
  const el = document.createElement("div");
  el.className = "msg user";
  el.innerHTML = `<div class="bubble-user">${esc(text)}</div>`;
  $("#stream").appendChild(el);
  scrollDown();
}

function addTyping() {
  const el = document.createElement("div");
  el.className = "msg";
  el.innerHTML =
    '<div class="typing"><i></i><i></i><i></i><span>Searching the documents</span></div>';
  $("#stream").appendChild(el);
  scrollDown();
  return el;
}

const FLAG_LABELS = {
  sensitive_data_masked: "Sensitive data masked",
  prompt_injection: "Instruction override blocked",
  permission_recheck_removed: "Permission re-check",
  below_relevance_floor: "No relevant passage",
  context_instruction_removed: "Instruction text removed from source",
  restricted_not_sent_to_llm: "Restricted text kept from the AI model",
  unsupported_numbers: "Number not found in source",
  invalid_citation_removed: "Bad citation removed",
  ungrounded_answer_blocked: "Ungrounded answer blocked",
  no_citation: "No citation",
  system_prompt_leak: "Prompt leak blocked",
  missing_document: "Document not in knowledge base",
  writer_not_configured: "Writer not configured",
  writer_not_found: "Writer found no answer",
  too_long: "Question too long",
  empty: "Empty question",
  rate_limited: "Rate limited",
  llm_error: "Writer error",
};

function addReply(r) {
  const el = document.createElement("div");
  el.className = "msg";
  const blocked = r.mode === "blocked" || r.mode === "error";
  const cls = blocked ? "blocked" : r.found ? "" : "notfound";
  const tag = blocked ? "Cannot answer" : r.found ? "" : "Not found";
  const c = r.cache || {};
  const cacheLabel =
    c.status === "exact"
      ? "Cache: same question"
      : c.status === "semantic"
        ? `Cache: similar question (${c.distance})`
        : c.status === "miss"
          ? "Fresh answer"
          : "Cache off";
  const total =
    r.timings_ms && r.timings_ms.total != null ? ms(r.timings_ms.total) : "";
  const flags = (r.guardrails || [])
    .map(
      (g) =>
        `<span class="pill warn" title="${esc(g.stage)}${g.detail ? ": " + esc(g.detail) : ""}">${esc(FLAG_LABELS[g.name] || g.name)}</span>`,
    )
    .join("");
  const modeLabel =
    r.mode === "extractive"
      ? "Passages only"
      : r.mode === "llm"
        ? "AI written"
        : "";
  const sources = (r.sources || [])
    .map((s) => {
      const where = [
        s.section,
        s.page
          ? s.page_end && s.page_end !== s.page
            ? `pages ${s.page}-${s.page_end}`
            : `page ${s.page}`
          : null,
        s.sheet && s.row ? `${s.sheet}, row ${s.row}` : null,
      ]
        .filter(Boolean)
        .filter((v, i, a) => a.indexOf(v) === i)
        .join(" · ");
      return `<details class="src" id="src-${r._id}-${s.n}">
      <summary><span class="num">${s.n}</span>
        <span class="title"><b>${esc(s.doc_name)}</b><small>${esc(s.doc_id)} · ${esc(where)}</small></span>
        <span class="badge ${esc(s.classification)}">${esc(s.classification)}</span>
        <svg class="chev" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M9 6l6 6-6 6"/></svg></summary>
      <pre>${esc(s.excerpt)}</pre></details>`;
    })
    .join("");
  el.innerHTML = `<div class="reply ${cls}">
      ${tag ? `<div class="tag">${esc(tag)}</div>` : ""}
      <div class="answer">${renderMarkdown(r.answer)}</div>
      ${sources ? `<div class="sources"><h3>Sources</h3>${sources}</div>` : ""}
      <div class="meta">
        <span class="pill ${c.status === "exact" || c.status === "semantic" ? "ok" : ""}"><span class="dot"></span>${esc(cacheLabel)}</span>
        ${total ? `<span class="pill">${esc(total)}</span>` : ""}
        ${modeLabel ? `<span class="pill">${esc(modeLabel)}</span>` : ""}
        ${flags}
      </div></div>`;
  el.addEventListener("click", (ev) => {
    const b = ev.target.closest(".cite");
    if (!b) return;
    const target = $(`#src-${r._id}-${b.dataset.n}`, el);
    if (target) {
      target.open = true;
      target.scrollIntoView({ block: "nearest", behavior: "smooth" });
      target.classList.add("flash");
      setTimeout(() => target.classList.remove("flash"), 1400);
    }
  });
  $("#stream").appendChild(el);
  scrollDown();
}

let replyCounter = 0;
async function ask(question) {
  question = question.trim();
  if (!question || state.busy) return;
  state.busy = true;
  $("#send").disabled = true;
  addUser(question);
  const typing = addTyping();
  try {
    const r = await api("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question,
        role: state.role,
        user: state.user || null,
      }),
    });
    r._id = ++replyCounter;
    typing.remove();
    addReply(r);
  } catch (e) {
    typing.remove();
    addReply({
      _id: ++replyCounter,
      answer: e.message,
      found: false,
      mode: "error",
      sources: [],
      guardrails: [],
      cache: {},
      timings_ms: {},
    });
  } finally {
    state.busy = false;
    $("#send").disabled = false;
    $("#q").focus();
  }
}

// ---------------------------------------------------------------- wiring
function autosize() {
  const t = $("#q");
  t.style.height = "auto";
  t.style.height = Math.min(t.scrollHeight, 140) + "px";
}

document.addEventListener("DOMContentLoaded", async () => {
  $("#refreshDocs").addEventListener("click", loadDocs);
  $("#menuBtn").addEventListener("click", () =>
    $("#side").classList.toggle("open"),
  );
  $("#form").addEventListener("submit", (e) => {
    e.preventDefault();
    const t = $("#q");
    const v = t.value;
    t.value = "";
    autosize();
    ask(v);
  });
  $("#q").addEventListener("input", autosize);
  $("#q").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      $("#form").requestSubmit();
    }
  });
  $("#samples").addEventListener("click", (e) => {
    const b = e.target.closest(".chip");
    if (b) ask(b.textContent);
  });

  const drop = $("#drop"),
    file = $("#file");
  drop.addEventListener("click", () => file.click());
  drop.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      file.click();
    }
  });
  file.addEventListener("change", () => {
    uploadFiles([...file.files]);
    file.value = "";
  });
  ["dragenter", "dragover"].forEach((n) =>
    drop.addEventListener(n, (e) => {
      e.preventDefault();
      drop.classList.add("over");
    }),
  );
  ["dragleave", "drop"].forEach((n) =>
    drop.addEventListener(n, (e) => {
      e.preventDefault();
      drop.classList.remove("over");
    }),
  );
  drop.addEventListener("drop", (e) => uploadFiles([...e.dataTransfer.files]));

  $("#loadFolder").addEventListener("click", async () => {
    const fd = new FormData();
    fd.append("role", state.role);
    fd.append("user", state.user);
    try {
      const r = await api("/api/ingest-folder", { method: "POST", body: fd });
      r.job_ids.forEach((id, i) => trackJob(id, r.files[i]));
    } catch (e) {
      alert(e.message);
    }
  });
  $("#docs").addEventListener("click", async (e) => {
    const b = e.target.closest(".del");
    if (!b || !confirm("Remove this document and its saved answers?")) return;
    const q = new URLSearchParams({ role: state.role, user: state.user });
    try {
      await api(`/api/documents/${encodeURIComponent(b.dataset.doc)}?${q}`, {
        method: "DELETE",
      });
      loadDocs();
      loadStatus();
    } catch (err) {
      alert(err.message);
    }
  });

  await restoreSession();

  if (state.authenticated) {
    loadStatus();
    $("#q").focus();
  }

  setInterval(() => {
    if (state.authenticated) {
      loadStatus();
    }
  }, 8000);
});
