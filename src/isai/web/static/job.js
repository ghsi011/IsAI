"use strict";
/* Job page: live paragraph cards, exact-text highlighting, analysis pane.
 *
 * Security invariants:
 * - every dynamic string (document text, provider output, filenames, errors)
 *   is inserted with textContent / createTextNode — never innerHTML;
 * - SSE events carry IDs only; authoritative data is re-fetched from the API.
 */

const TOKEN = document.body.dataset.token;
const JOB_ID = document.body.dataset.jobId;

function api(path, options = {}) {
  const headers = Object.assign({ "X-IsAI-Token": TOKEN }, options.headers || {});
  return fetch(path, Object.assign({}, options, { headers }));
}

const documentPane = document.getElementById("document-pane");
const analysisPane = document.getElementById("analysis-pane");
const jobMetaEl = document.getElementById("job-meta");
const progressEl = document.getElementById("progress");
const filterEl = document.getElementById("filter");
const searchEl = document.getElementById("search");

let state = { job: {}, elements: [] };
let detailCache = new Map();
let selectedId = null;

/* ---------- rendering ---------- */

const KIND_LABELS = {
  indicator: "AI-associated indicator",
  counter_indicator: "Counter-indicator",
  quality: "Writing-quality issue",
  citation: "Citation observation",
  suggestion: "Revision suggestion",
};
const KIND_ICONS = {
  indicator: "◆",
  counter_indicator: "✓",
  quality: "✎",
  citation: "❝",
  suggestion: "➤",
};

function chip(text, cls) {
  const span = document.createElement("span");
  span.className = "chip " + (cls || "");
  span.textContent = text;
  return span;
}

function cardMatchesFilter(card) {
  const mode = filterEl.value;
  switch (mode) {
    case "unanalyzed":
      return card.status === "pending";
    case "analyzing":
      return card.status === "active";
    case "high_priority":
      return card.review_priority === "high";
    case "moderate_strong":
      return card.style_signal === "moderate" || card.style_signal === "strong";
    case "needs_source_check":
      return card.needs_source_check;
    case "has_suggestions":
      return card.has_suggestions;
    case "disagreement":
      return card.agreement === "disagreement" || card.agreement === "partial_agreement";
    case "errors":
      return card.status === "error";
    case "short_indeterminate":
      return card.style_signal === "indeterminate";
    default:
      return true;
  }
}

function cardMatchesSearch(card, detail) {
  const q = searchEl.value.trim().toLowerCase();
  if (!q) return true;
  if (String(card.review_number || "") === q) return true;
  if ((card.nearest_heading || "").toLowerCase().includes(q)) return true;
  if ((card.style_signal || "").includes(q)) return true;
  if (detail && detail.element.text.toLowerCase().includes(q)) return true;
  return false;
}

function renderDocument() {
  documentPane.replaceChildren();
  for (const card of state.elements) {
    if (!cardMatchesFilter(card)) continue;
    const detail = detailCache.get(card.element_id);
    if (!cardMatchesSearch(card, detail)) continue;
    documentPane.appendChild(renderCard(card, detail));
  }
}

function renderCard(card, detail) {
  const div = document.createElement("article");
  div.className = "card" + (card.is_heading ? " heading-card" : "");
  div.tabIndex = 0;
  div.dataset.elementId = card.element_id;
  if (card.element_id === selectedId) div.classList.add("selected");

  const head = document.createElement("div");
  head.className = "card-head";
  const num = document.createElement("span");
  num.className = "num";
  num.textContent = card.review_number ? "¶" + card.review_number : "§";
  head.appendChild(num);
  if (card.kind === "table") head.appendChild(chip("table cell"));
  if (card.style_name && card.style_name !== "Normal") head.appendChild(chip(card.style_name));
  if (card.nearest_heading && !card.is_heading) {
    const h = document.createElement("span");
    h.textContent = "under: " + card.nearest_heading;
    head.appendChild(h);
  }
  if (card.status === "active") head.appendChild(chip("analyzing…", "status-active"));
  else if (card.status === "error") head.appendChild(chip("error: " + (card.error_category || ""), "status-error"));
  else if (card.status === "skipped") head.appendChild(chip("not reviewed"));
  if (card.style_signal) {
    const label = "signal: " + card.style_signal;
    head.appendChild(chip(label, "signal-" + card.style_signal));
  }
  if (card.agreement && card.agreement !== "agreement") head.appendChild(chip(card.agreement));
  div.appendChild(head);

  const text = document.createElement("div");
  text.className = "text";
  if (detail && detail.segments.length && detail.highlights.length) {
    renderHighlightedText(text, detail);
  } else if (detail) {
    text.textContent = detail.element.text;
  } else {
    text.textContent = "";
    if (card.status !== "skipped" || card.is_heading) loadDetail(card.element_id);
  }
  div.appendChild(text);

  div.addEventListener("click", () => selectElement(card.element_id));
  div.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" || ev.key === " ") {
      ev.preventDefault();
      selectElement(card.element_id);
    }
  });
  return div;
}

function renderHighlightedText(container, detail) {
  const text = detail.element.text;
  const byId = new Map(detail.highlights.map((h) => [h.highlight_id, h]));
  for (const seg of detail.segments) {
    const piece = text.slice(seg.start, seg.end);
    if (!seg.highlight_ids.length) {
      container.appendChild(document.createTextNode(piece));
      continue;
    }
    const mark = document.createElement("mark");
    const kinds = seg.highlight_ids.map((id) => byId.get(id)).filter(Boolean);
    mark.className =
      "hl " + kinds.map((h) => "hl-" + h.kind).join(" ") + (kinds.length > 1 ? " multi" : "");
    mark.dataset.highlightIds = seg.highlight_ids.join(",");
    const labels = kinds
      .map((h) => KIND_ICONS[h.kind] + " " + KIND_LABELS[h.kind] + " (" + h.category + ")")
      .join("; ");
    mark.title = labels;
    mark.setAttribute("aria-label", labels);
    mark.tabIndex = 0;
    mark.textContent = piece;
    mark.addEventListener("click", (ev) => {
      ev.stopPropagation();
      selectElement(detail.element.element_id, seg.highlight_ids[0]);
    });
    container.appendChild(mark);
  }
}

function annotationBlock(kind, category, quote, occurrence, bodyLines, highlightId, unresolved) {
  const div = document.createElement("div");
  div.className = "annotation kind-" + kind;
  if (highlightId) div.dataset.highlightId = highlightId;
  div.tabIndex = 0;
  const cat = document.createElement("span");
  cat.className = "cat";
  cat.textContent = KIND_ICONS[kind] + " " + (category || KIND_LABELS[kind]);
  div.appendChild(cat);
  if (quote) {
    const q = document.createElement("span");
    q.className = "quote";
    q.textContent =
      "“" + quote + "”" + (occurrence && occurrence > 1 ? " (occurrence " + occurrence + ")" : "");
    div.appendChild(q);
  }
  for (const line of bodyLines) {
    if (!line) continue;
    const p = document.createElement("div");
    p.textContent = line;
    div.appendChild(p);
  }
  if (unresolved) {
    const u = document.createElement("div");
    u.className = "unresolved";
    u.textContent = "Quoted text could not be located exactly in this paragraph.";
    div.appendChild(u);
  }
  div.addEventListener("click", () => focusHighlight(highlightId));
  return div;
}

function sectionTitle(text) {
  const h = document.createElement("h3");
  h.textContent = text;
  return h;
}

function renderResultInto(pane, detail, result, heading) {
  if (heading) {
    const h = document.createElement("h3");
    h.textContent = heading;
    pane.appendChild(h);
  }
  const kv = document.createElement("p");
  kv.className = "kv";
  kv.textContent =
    "Signal: " + result.style_signal +
    " · confidence in observations: " + result.assessment_confidence +
    " · priority: " + result.review_priority +
    (result.provider ? " · provider: " + result.provider : "") +
    (result.agreement ? " · consensus: " + result.agreement : "");
  pane.appendChild(kv);
  const summary = document.createElement("p");
  summary.textContent = result.summary;
  pane.appendChild(summary);

  const unresolvedIds = new Set(
    detail.highlights.filter((h) => h.tier === "unresolved").map((h) => h.highlight_id)
  );
  const hlFor = (field) => {
    const h = detail.highlights.find((x) => x.field === field);
    return h ? h.highlight_id : null;
  };

  if (result.indicators.length) {
    pane.appendChild(sectionTitle("Indicators (AI-associated style)"));
    result.indicators.forEach((ind, i) => {
      const id = hlFor("indicators[" + i + "]");
      pane.appendChild(
        annotationBlock("indicator", ind.category, ind.evidence, ind.occurrence_index,
          [ind.explanation], id, id && unresolvedIds.has(id))
      );
    });
  }
  if (result.counter_indicators.length) {
    pane.appendChild(sectionTitle("Counter-indicators"));
    result.counter_indicators.forEach((ind, i) => {
      const id = hlFor("counter_indicators[" + i + "]");
      pane.appendChild(
        annotationBlock("counter_indicator", ind.category, ind.evidence, ind.occurrence_index,
          [ind.explanation], id, id && unresolvedIds.has(id))
      );
    });
  }
  if (result.quality_issues.length) {
    pane.appendChild(sectionTitle("Writing-quality issues"));
    result.quality_issues.forEach((qi, i) => {
      const id = hlFor("quality_issues[" + i + "]");
      pane.appendChild(
        annotationBlock("quality", qi.category, qi.target_text, qi.occurrence_index,
          [qi.description], id, id && unresolvedIds.has(id))
      );
    });
  }
  if (result.citation_observations.length) {
    pane.appendChild(sectionTitle("Citation observations"));
    result.citation_observations.forEach((co, i) => {
      const id = hlFor("citation_observations[" + i + "]");
      const lines = [co.observation];
      if (co.requires_source_check) lines.push("⚠ Requires source check.");
      pane.appendChild(
        annotationBlock("citation", "citation", co.target_text, co.occurrence_index,
          lines, id, id && unresolvedIds.has(id))
      );
    });
  }
  pane.appendChild(sectionTitle("Revision suggestions"));
  if (!result.revision_suggestions.length) {
    const p = document.createElement("p");
    p.className = "kv";
    p.textContent = "None — no substantial revision recommended.";
    pane.appendChild(p);
  } else {
    result.revision_suggestions.forEach((rs, i) => {
      const id = hlFor("revision_suggestions[" + i + "]");
      const lines = [rs.issue, "Change: " + rs.recommended_change];
      if (rs.proposed_replacement) lines.push("Proposed wording: “" + rs.proposed_replacement + "”");
      lines.push("Why: " + rs.reason);
      if (rs.requires_source_check) lines.push("⚠ Verify against the cited source first.");
      pane.appendChild(
        annotationBlock("suggestion", rs.target_text ? "suggestion" : "whole paragraph",
          rs.target_text, rs.occurrence_index, lines, id, id && unresolvedIds.has(id))
      );
    });
  }
  if (result.manual_checks.length) {
    pane.appendChild(sectionTitle("Manual checks"));
    for (const mc of result.manual_checks) {
      const p = document.createElement("p");
      p.textContent = "• " + mc;
      pane.appendChild(p);
    }
  }
  const lim = document.createElement("p");
  lim.className = "kv";
  lim.textContent = "Limitations: " + result.limitations_note;
  pane.appendChild(lim);
}

function renderAnalysis(detail) {
  analysisPane.replaceChildren();
  const h2 = document.createElement("h2");
  h2.textContent = "Paragraph " + (detail.element.review_number || detail.element.display_number);
  analysisPane.appendChild(h2);
  const kv = document.createElement("p");
  kv.className = "kv";
  kv.textContent =
    "Location: " + detail.element.location +
    " · style: " + detail.element.style_name +
    (detail.element.nearest_heading ? " · under: " + detail.element.nearest_heading : "") +
    " · " + detail.element.word_count + " words";
  analysisPane.appendChild(kv);

  if (detail.status === "error") {
    const p = document.createElement("p");
    p.textContent = "Review error (" + detail.error_category + "): " + (detail.error_message || "");
    analysisPane.appendChild(p);
    return;
  }
  if (!detail.result) {
    const p = document.createElement("p");
    p.className = "hint";
    p.textContent =
      detail.status === "active" ? "Analyzing…" :
      detail.status === "skipped" ? "Not reviewed (heading, empty, or out of range)." :
      "Not analyzed yet.";
    analysisPane.appendChild(p);
    return;
  }
  renderResultInto(analysisPane, detail, detail.result, null);
  if (detail.second_opinion && detail.second_opinion.result) {
    const badge = document.createElement("p");
    const b = document.createElement("span");
    b.className = "badge-second";
    b.textContent = "Second opinion — " + (detail.second_opinion.agreement || "");
    badge.appendChild(b);
    analysisPane.appendChild(badge);
    renderResultInto(analysisPane, detail, detail.second_opinion.result, "Second opinion (" +
      (detail.second_opinion.provider || "") + ")");
  }
  if (detail.result && detail.result.scope === "context_window") {
    const p = document.createElement("p");
    p.className = "kv";
    p.textContent = "Assessed within a context window of neighboring paragraphs.";
    analysisPane.appendChild(p);
  }
}

/* ---------- interactions ---------- */

function focusHighlight(highlightId) {
  if (!highlightId) return;
  document.querySelectorAll("mark.hl.focused").forEach((m) => m.classList.remove("focused"));
  document.querySelectorAll(".annotation.focused").forEach((a) => a.classList.remove("focused"));
  // highlight IDs are unique within one result only; search the selected card.
  const scope = selectedId
    ? document.querySelector('.card[data-element-id="' + selectedId + '"]')
    : null;
  const marks = (scope || document).querySelectorAll("mark.hl");
  for (const mark of marks) {
    const ids = (mark.dataset.highlightIds || "").split(",");
    if (ids.includes(highlightId)) {
      mark.classList.add("focused");
      mark.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }
  const ann = document.querySelector('.annotation[data-highlight-id="' + highlightId + '"]');
  if (ann) {
    ann.classList.add("focused");
    ann.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
}

async function selectElement(elementId, highlightId) {
  selectedId = elementId;
  const detail = await loadDetail(elementId, true);
  document.querySelectorAll(".card.selected").forEach((c) => c.classList.remove("selected"));
  const card = document.querySelector('.card[data-element-id="' + elementId + '"]');
  if (card) card.classList.add("selected");
  if (detail) renderAnalysis(detail);
  if (highlightId) focusHighlight(highlightId);
}

const pendingDetail = new Set();
async function loadDetail(elementId, force) {
  if (!force && detailCache.has(elementId)) return detailCache.get(elementId);
  if (!force && pendingDetail.has(elementId)) return null;
  pendingDetail.add(elementId);
  try {
    const res = await api("/api/jobs/" + JOB_ID + "/elements/" + encodeURIComponent(elementId));
    if (!res.ok) return null;
    const detail = await res.json();
    detailCache.set(elementId, detail);
    const card = state.elements.find((c) => c.element_id === elementId);
    if (card) {
      const cardEl = document.querySelector('.card[data-element-id="' + elementId + '"]');
      if (cardEl) cardEl.replaceWith(renderCard(card, detail));
    }
    return detail;
  } finally {
    pendingDetail.delete(elementId);
  }
}

/* ---------- state + events ---------- */

async function refreshState() {
  const res = await api("/api/jobs/" + JOB_ID + "/state");
  if (!res.ok) return;
  state = await res.json();
  jobMetaEl.textContent =
    (state.job.display_name || state.job.source_filename || "") +
    " — " + state.job.status +
    (state.job.paused_reason ? " (" + state.job.paused_reason + ")" : "") +
    (state.job.last_error ? " — " + state.job.last_error : "");
  if (state.job.progress) {
    progressEl.textContent = state.job.progress.done + "/" + state.job.progress.total + " done";
  }
  renderDocument();
  if (selectedId) {
    const detail = detailCache.get(selectedId);
    if (detail) renderAnalysis(detail);
  }
}

async function onElementEvent(elementId) {
  await loadDetail(elementId, true);
  await refreshState();
}

function connectEvents() {
  const source = new EventSource(
    "/api/jobs/" + JOB_ID + "/events?token=" + encodeURIComponent(TOKEN)
  );
  const refetch = (ev) => {
    try {
      const data = JSON.parse(ev.data);
      if (data.element_id) onElementEvent(data.element_id);
      else refreshState();
    } catch {
      refreshState();
    }
  };
  ["paragraph_started", "primary_review_completed", "second_opinion_completed",
   "paragraph_error", "job_paused", "job_completed", "job_failed", "job_started",
   "consensus_degraded"].forEach((kind) => source.addEventListener(kind, refetch));
  source.onerror = () => {
    // Reconnect handled by EventSource; refresh authoritative state on recovery.
    setTimeout(refreshState, 2000);
  };
}

/* ---------- controls ---------- */

function bindControls() {
  const post = (action) => () =>
    api("/api/jobs/" + JOB_ID + "/" + action, { method: "POST" }).then(refreshState);
  document.getElementById("btn-pause").addEventListener("click", post("pause"));
  document.getElementById("btn-stop").addEventListener("click", post("stop"));
  document.getElementById("btn-resume").addEventListener("click", post("resume"));
  document.getElementById("btn-rebuild").addEventListener("click", post("rebuild"));
  document.getElementById("btn-report").href =
    "/api/jobs/" + JOB_ID + "/report?token=" + encodeURIComponent(TOKEN);
  document.getElementById("btn-export").addEventListener("click", () => {
    if (
      !window.confirm(
        "The journal contains the full document text. Export it for viewing " +
        "this review on another PC (via drop zone or `isai import`)?"
      )
    )
      return;
    window.location.href =
      "/api/jobs/" + JOB_ID + "/journal?confirm=yes&token=" + encodeURIComponent(TOKEN);
  });
  document.getElementById("btn-folder").addEventListener("click", post("open-folder"));
  filterEl.addEventListener("change", renderDocument);
  searchEl.addEventListener("input", () => renderDocument());
}

bindControls();
refreshState().then(connectEvents);
