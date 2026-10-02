const $ = (sel, root = document) => root.querySelector(sel);
const state = { slots: [], autoFields: [], jobs: [] };

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return res;
}
const json = (body) => ({ method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// ------------------------------------------------------------------ setup

function renderSlots() {
  const el = $("#slots");
  if (!state.slots.length) {
    el.innerHTML = `<p class="muted small">No template uploaded yet.</p>`;
    return;
  }
  el.innerHTML = `<p class="small ok">Template loaded — detected ${state.slots.length} placeholder(s):</p>
    <div class="chips">${state.slots.map((s) => {
      if (s.kind === "written") return `<span class="chip written" title="${esc(s.guidance)}">${s.id}: ${esc(s.guidance.slice(0, 40))}${s.guidance.length > 40 ? "…" : ""}</span>`;
      const auto = state.autoFields.includes(s.id);
      return `<span class="chip ${auto ? "auto" : ""}" title="${auto ? "Filled from your details" : "Extracted from the job description"}">${esc(s.id)}</span>`;
    }).join("")}</div>`;
}

async function loadState() {
  const s = await (await api("/api/state")).json();
  state.slots = s.slots || [];
  state.autoFields = s.auto_fields;
  renderSlots();
  $("#first-name").value = s.settings.first_name;
  $("#last-name").value = s.settings.last_name;
  $("#filename-pattern").value = s.settings.filename_pattern;
  $("#static-fields").value = Object.entries(s.settings.static_fields).map(([k, v]) => `${k} = ${v}`).join("\n");
  $("#include-resume").checked = s.settings.include_resume_default;
  $("#resume-status").textContent = s.has_resume ? `Resume saved: “${s.resume_preview.slice(0, 80)}…”` : "No resume uploaded.";
  state.settings = s.settings;
}

$("#template-file").addEventListener("change", async (e) => {
  const fd = new FormData();
  fd.append("file", e.target.files[0]);
  try {
    await api("/api/template", { method: "POST", body: fd });
    await loadState();
  } catch (err) { $("#slots").innerHTML = `<p class="error small">${esc(err.message)}</p>`; }
});

$("#resume-file").addEventListener("change", async (e) => {
  const fd = new FormData();
  fd.append("file", e.target.files[0]);
  try {
    const r = await (await api("/api/resume", { method: "POST", body: fd })).json();
    $("#resume-status").textContent = `Resume saved: “${r.resume_preview.slice(0, 80)}…”`;
  } catch (err) { $("#resume-status").innerHTML = `<span class="error">${esc(err.message)}</span>`; }
});

$("#save-settings").addEventListener("click", async () => {
  const staticFields = {};
  for (const line of $("#static-fields").value.split("\n")) {
    const i = line.indexOf("=");
    if (i > 0) staticFields[line.slice(0, i).trim()] = line.slice(i + 1).trim();
  }
  const body = {
    ...state.settings,
    first_name: $("#first-name").value.trim(),
    last_name: $("#last-name").value.trim(),
    filename_pattern: $("#filename-pattern").value.trim(),
    static_fields: staticFields,
    include_resume_default: $("#include-resume").checked,
  };
  try {
    await api("/api/settings", { ...json(body), method: "PUT" });
    await loadState();
    $("#settings-status").innerHTML = `<span class="ok">Saved.</span>`;
  } catch (err) { $("#settings-status").innerHTML = `<span class="error">${esc(err.message)}</span>`; }
});

// ------------------------------------------------------------------ jobs

function addJd() {
  const node = $("#jd-tpl").content.firstElementChild.cloneNode(true);
  $(".remove", node).addEventListener("click", () => { if ($("#jd-list").children.length > 1) node.remove(); });
  $("#jd-list").append(node);
}
$("#add-jd").addEventListener("click", addJd);

$("#generate").addEventListener("click", async () => {
  const jds = [...document.querySelectorAll("#jd-list textarea")].map((t) => t.value).filter((v) => v.trim());
  if (!jds.length) return alert("Paste at least one job description.");
  const btn = $("#generate");
  const progress = $("#progress");
  btn.disabled = true;
  progress.hidden = false;
  progress.textContent = "";
  state.jobs = [];
  $("#results").innerHTML = "";
  $("#review").hidden = false;
  const log = (line) => { progress.textContent += line + "\n"; progress.scrollTop = progress.scrollHeight; };
  try {
    const res = await api("/api/generate", json({ jds, include_resume: $("#include-resume").checked }));
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const ev = JSON.parse(buf.slice(0, nl));
        buf = buf.slice(nl + 1);
        if (ev.type === "status") log(ev.message);
        else if (ev.type === "error") log("Error: " + ev.message);
        else if (ev.type === "job") {
          state.jobs.push(ev.job);
          log(ev.job.error ? `✗ ${ev.job.error}` : `✓ Draft ready: ${ev.job.role_short} at ${ev.job.company_short}`);
          renderJob(ev.job);
        } else if (ev.type === "done") log("Done. Review below, then export.");
      }
    }
  } catch (err) {
    log("Error: " + err.message);
  } finally {
    btn.disabled = false;
  }
});

// ------------------------------------------------------------------ review

function syncJob(job, card) {
  for (const input of card.querySelectorAll("[data-field]")) job.fields[input.dataset.field] = input.value;
  for (const input of card.querySelectorAll("[data-written]")) job.written[input.dataset.written] = input.value;
  job.company_short = $("[data-meta=company_short]", card).value;
  job.role_short = $("[data-meta=role_short]", card).value;
}

function renderJob(job) {
  const card = document.createElement("div");
  card.className = "card";
  card.id = `job-${job.id}`;
  if (job.error) {
    card.innerHTML = `<h3 class="error">Couldn't generate this letter</h3><p class="error">${esc(job.error)}</p>
      <details><summary>Job description</summary><pre>${esc(job.jd.slice(0, 2000))}</pre></details>`;
    $("#results").append(card);
    return;
  }
  const fieldSlots = state.slots.filter((s) => s.kind === "field" && !state.autoFields.includes(s.id));
  const writtenSlots = state.slots.filter((s) => s.kind === "written");
  card.innerHTML = `
    <div class="result">
      <div>
        <h3>${esc(job.role)} — ${esc(job.company)}</h3>
        <div class="filename" data-filename></div>
        <div class="row">
          <label>Company (filename) <input data-meta="company_short" value="${esc(job.company_short)}"></label>
          <label>Role (filename) <input data-meta="role_short" value="${esc(job.role_short)}"></label>
        </div>
        ${fieldSlots.map((s) => `
          <div class="field"><label>{{${esc(s.id)}}}
            <input data-field="${esc(s.id)}" value="${esc(job.fields[s.id] ?? "")}"></label></div>`).join("")}
        ${writtenSlots.map((s) => `
          <div class="field">
            <label>${s.id}<span class="guidance">${esc(s.guidance)}</span>
              <textarea rows="3" data-written="${s.id}">${esc(job.written[s.id] ?? "")}</textarea></label>
            <div class="actions">
              <input placeholder="Optional note, e.g. “mention their API platform”" data-hint="${s.id}">
              <button class="secondary" data-regen="${s.id}">Rewrite</button>
            </div>
          </div>`).join("")}
        <details><summary>Company research used</summary><pre>${esc(job.research)}</pre></details>
        <div class="row">
          <button data-preview>Preview PDF</button>
          <button class="primary" data-export>Export PDF</button>
          <span class="small" data-status></span>
        </div>
      </div>
      <div data-preview-pane><div class="preview-empty muted small">Click “Preview PDF”.</div></div>
    </div>`;
  $("#results").append(card);

  const status = (html) => { $("[data-status]", card).innerHTML = html; };
  const updateFilename = () => {
    const part = (t) => t.trim().replace(/[^A-Za-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
    const name = $("#filename-pattern").value
      .replace("{first}", part($("#first-name").value)).replace("{last}", part($("#last-name").value))
      .replace("{company}", part($("[data-meta=company_short]", card).value))
      .replace("{role}", part($("[data-meta=role_short]", card).value)).replace(/_+/g, "_");
    $("[data-filename]", card).textContent = name + ".pdf";
  };
  updateFilename();
  card.querySelectorAll("[data-meta]").forEach((i) => i.addEventListener("input", updateFilename));

  card.querySelectorAll("[data-regen]").forEach((btn) => btn.addEventListener("click", async () => {
    const id = btn.dataset.regen;
    syncJob(job, card);
    btn.disabled = true;
    btn.textContent = "Rewriting…";
    try {
      const r = await (await api("/api/regenerate-slot", json({
        job, slot_id: id, hint: $(`[data-hint=${id}]`, card).value || null,
        include_resume: $("#include-resume").checked,
      }))).json();
      $(`[data-written=${id}]`, card).value = r.text;
      job.written[id] = r.text;
    } catch (err) { status(`<span class="error">${esc(err.message)}</span>`); }
    btn.disabled = false;
    btn.textContent = "Rewrite";
  }));

  $("[data-preview]", card).addEventListener("click", async (e) => {
    syncJob(job, card);
    e.target.disabled = true;
    status("Rendering…");
    try {
      const blob = await (await api("/api/preview", json(job))).blob();
      $("[data-preview-pane]", card).innerHTML = `<iframe class="preview" src="${URL.createObjectURL(blob)}"></iframe>`;
      status("");
    } catch (err) { status(`<span class="error">${esc(err.message)}</span>`); }
    e.target.disabled = false;
  });

  $("[data-export]", card).addEventListener("click", async (e) => {
    syncJob(job, card);
    e.target.disabled = true;
    status("Exporting…");
    try {
      const r = await (await api("/api/export", json({ jobs: [job] }))).json();
      const f = r.files[0];
      status(`<span class="ok">Saved</span> <a href="${f.url}" download>${esc(f.filename)}</a>`);
    } catch (err) { status(`<span class="error">${esc(err.message)}</span>`); }
    e.target.disabled = false;
  });
}

$("#export-all").addEventListener("click", async (e) => {
  const jobs = state.jobs.filter((j) => !j.error);
  if (!jobs.length) return;
  jobs.forEach((j) => syncJob(j, $(`#job-${j.id}`)));
  e.target.disabled = true;
  const st = $("#export-status");
  st.textContent = "Exporting…";
  try {
    const r = await (await api("/api/export", json({ jobs }))).json();
    st.innerHTML = `<span class="ok">Saved ${r.files.length} PDF(s) to ${esc(r.output_dir)}</span> · ` +
      r.files.map((f) => `<a href="${f.url}" download>${esc(f.filename)}</a>`).join(" · ") +
      (r.files.length > 1 ? ` · <a href="#" id="zip-link">Download all (.zip)</a>` : "");
    $("#zip-link")?.addEventListener("click", async (ev) => {
      ev.preventDefault();
      const blob = await (await api("/api/zip", json({ filenames: r.files.map((f) => f.filename) }))).blob();
      const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: "cover_letters.zip" });
      a.click();
    });
  } catch (err) { st.innerHTML = `<span class="error">${esc(err.message)}</span>`; }
  e.target.disabled = false;
});

addJd();
loadState();
