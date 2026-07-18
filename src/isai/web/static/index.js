"use strict";
/* Index page: drag-and-drop upload + job list. All dynamic text rendered via
 * textContent — never innerHTML — because filenames and errors are untrusted. */

const TOKEN = document.body.dataset.token;

function api(path, options = {}) {
  const headers = Object.assign({ "X-IsAI-Token": TOKEN }, options.headers || {});
  return fetch(path, Object.assign({}, options, { headers }));
}

const dropzone = document.getElementById("dropzone");
const fileInput = document.getElementById("file-input");
const statusEl = document.getElementById("upload-status");
const jobsEl = document.getElementById("jobs");

function setStatus(text) {
  statusEl.textContent = text;
}

async function upload(file) {
  if (!file) return;
  const lower = file.name.toLowerCase();
  if (lower.endsWith(".sqlite3")) {
    return importJournal(file);
  }
  if (!lower.endsWith(".docx")) {
    setStatus("Drop a .docx to review, or an exported .sqlite3 journal to view.");
    return;
  }
  setStatus("Uploading " + file.name + " …");
  const form = new FormData();
  form.append("file", file, file.name);
  const options = document.getElementById("upload-options");
  form.append("provider", options.elements.provider.value);
  form.append("min_words", options.elements.min_words.value);
  form.append("context_assisted", options.elements.context_assisted.checked ? "on" : "off");
  form.append("include_tables", options.elements.include_tables.checked ? "on" : "off");
  try {
    const res = await api("/api/jobs", { method: "POST", body: form });
    const data = await res.json();
    if (!res.ok) {
      setStatus("Rejected: " + (data.detail || res.statusText));
      return;
    }
    window.location.href = "/job/" + data.job_id + "?token=" + encodeURIComponent(TOKEN);
  } catch (err) {
    setStatus("Upload failed: " + err.message);
  }
}

["dragenter", "dragover"].forEach((name) =>
  dropzone.addEventListener(name, (ev) => {
    ev.preventDefault();
    dropzone.classList.add("dragover");
  })
);
["dragleave", "drop"].forEach((name) =>
  dropzone.addEventListener(name, (ev) => {
    ev.preventDefault();
    dropzone.classList.remove("dragover");
  })
);
dropzone.addEventListener("drop", (ev) => upload(ev.dataTransfer.files[0]));
fileInput.addEventListener("change", () => upload(fileInput.files[0]));

async function refreshJobs() {
  const res = await api("/api/jobs");
  if (!res.ok) return;
  const data = await res.json();
  jobsEl.replaceChildren();
  if (!data.jobs.length) {
    const li = document.createElement("li");
    li.textContent = "No jobs yet.";
    jobsEl.appendChild(li);
    return;
  }
  for (const job of data.jobs.slice().reverse()) {
    const li = document.createElement("li");
    const link = document.createElement("a");
    link.href = "/job/" + job.job_id + "?token=" + encodeURIComponent(TOKEN);
    link.textContent = job.display_name || job.job_id;
    const status = document.createElement("span");
    status.className = "muted";
    const progress = job.progress ? ` — ${job.progress.done}/${job.progress.total}` : "";
    status.textContent =
      (job.status || "new") +
      (job.paused_reason ? " (" + job.paused_reason + ")" : "") +
      progress +
      (job.created_at ? " — started " + job.created_at : "");
    const del = document.createElement("button");
    del.type = "button";
    del.textContent = "Delete";
    del.addEventListener("click", async (ev) => {
      ev.preventDefault();
      if (!window.confirm("Delete this job and all its data?")) return;
      await api("/api/jobs/" + job.job_id, { method: "DELETE" });
      refreshJobs();
    });
    li.append(link, status, del);
    jobsEl.appendChild(li);
  }
}

async function importJournal(file) {
  setStatus("Importing " + file.name + " …");
  const form = new FormData();
  form.append("file", file, file.name);
  try {
    const res = await api("/api/import", { method: "POST", body: form });
    const data = await res.json();
    if (!res.ok) {
      setStatus("Rejected: " + (data.detail || res.statusText));
      return;
    }
    window.location.href = "/job/" + data.job_id + "?token=" + encodeURIComponent(TOKEN);
  } catch (err) {
    setStatus("Import failed: " + err.message);
  }
}

refreshJobs();
setInterval(refreshJobs, 5000);
