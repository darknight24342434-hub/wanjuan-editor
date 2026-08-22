"use strict";

const state = {
  health: null,
  documents: [],
  currentDocument: null,
  catalogs: { style_cards: [], templates: [], editors: [] },
  activeKind: "style_cards",
  trainingFilter: "all",
  selected: { style_cards: "", templates: "", editors: "" },
  selectedAsset: null,
  historyMode: "revisions",
  mode: "news",
  advancedOpen: false,
  libraryOpen: false,
  historyOpen: false,
  lastReviewRun: null,
  lastNewsReviewRun: null,
  lastWritingReviewRun: null,
  reviewMode: false,
  provisionalReview: false,
  workflow: null,
  structure: null,
  structurePreview: null,
  semanticJob: null,
  semanticRun: null,
  semanticPollToken: 0,
  writerCards: [],
  selectedWriterCard: "",
  writerJob: null,
  writerPollToken: 0,
  writerPollFailures: 0,
  editorFunctionFilter: "all",
  importedSourceHint: "",
  dirty: false,
  media: null,
  mediaJobs: { transcribe: null, highlights: null, clips: null },
  mediaPollTokens: { transcribe: 0, highlights: 0, clips: 0 },
};

const el = {};

function bindElements() {
  [
    "news-mode", "writing-mode", "media-mode", "runtime-status", "save-status", "document-list", "new-document", "catalog-count",
    "library-toggle", "library-close", "library-backdrop", "history-toggle", "news-progress", "approval-status",
    "stage-kicker", "stage-title", "stage-description", "results-section", "asset-detail-section",
    "catalog-search", "training-filter-wrap", "training-filter", "training-filter-note", "catalog-list", "document-title", "save-document", "run-review",
    "document-stats", "document-hash", "editor", "history-list", "style-select",
    "template-select", "persona-select", "asset-detail", "issue-count", "review-summary",
    "suggestion-list", "diff-dialog", "diff-output", "close-diff", "toast", "catalog-section",
    "news-controls", "writing-controls", "invocation-section", "article-upload", "source-notes", "source-detection-status",
    "writing-concept", "recommend-styles", "style-recommendations", "toggle-advanced",
    "video-plan-status", "build-video-plan", "video-plan-output", "video-plan-drawer",
    "source-clue-list", "source-readiness", "source-readiness-detail", "run-writing-review",
    "back-to-edit", "rerun-review", "article-review", "review-title-anchor", "annotated-article",
    "case-overview", "overview-warning", "overview-note", "overview-stage", "approval-blockers", "approve-revision",
    "news-review-content", "writing-review-content", "editorial-department", "chief-department",
    "editor-issue-count", "chief-issue-count", "editor-suggestion-list", "chief-suggestion-list",
    "rewrite-actions", "rewrite-preview",
    "structure-preview", "semantic-status", "semantic-error", "semantic-boundary",
    "editor-function-filters", "headline-candidates", "chief-recommendations",
    "writer-seat", "writer-card-select", "writer-target-length", "writer-recommendation",
    "start-writer", "skip-writer", "writer-status", "writer-error", "writer-report",
    "media-workspace", "media-upload", "media-file-status", "media-source-card",
    "media-transcribe", "media-highlight", "media-cut", "media-save-document", "media-error",
    "media-transcribe-status", "media-highlight-status", "media-clip-status",
    "media-transcript", "media-highlights", "media-plans", "media-clips", "media-provenance-output",
  ].forEach((id) => { el[id] = document.getElementById(id); });
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (response.status === 401) {
    window.location.replace("/login");
  }
  if (!response.ok) {
    const error = new Error(payload.error || `HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return payload;
}

function toast(message) {
  el.toast.textContent = message;
  el.toast.classList.add("show");
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => el.toast.classList.remove("show"), 2600);
}

function shortDate(value) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-TW", { hour12: false });
}

function shortHash(value) {
  return value ? value.slice(0, 12) : "";
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function button(label, className, onClick) {
  const item = document.createElement("button");
  item.type = "button";
  item.className = className;
  item.textContent = label;
  item.addEventListener("click", onClick);
  return item;
}

function setLibraryOpen(open) {
  state.libraryOpen = open;
  const panel = document.querySelector(".library-panel");
  panel.classList.toggle("open", open);
  panel.setAttribute("aria-hidden", open ? "false" : "true");
  el["library-toggle"].setAttribute("aria-expanded", open ? "true" : "false");
  el["library-backdrop"].hidden = !open;
}

function setHistoryOpen(open) {
  state.historyOpen = open;
  document.querySelector(".history-drawer").classList.toggle("collapsed", !open);
  el["history-toggle"].setAttribute("aria-expanded", open ? "true" : "false");
  el["history-toggle"].textContent = open ? "收起紀錄" : "版本紀錄";
}

function updateNewsStage() {
  const news = state.mode === "news";
  const hasContent = Boolean(el.editor?.value.trim());
  const reviewed = Boolean(state.lastReviewRun) && !state.dirty;
  const approved = Boolean(state.workflow?.approved) && !state.dirty;
  const stage = state.dirty
    ? (hasContent ? "writer" : "intake")
    : (state.workflow?.stage || (!hasContent ? "intake" : "writer"));
  const order = ["intake", "writer", "editor", "chief", "finalize"];
  const activeIndex = order.indexOf(stage);

  document.body.dataset.mode = state.mode;
  document.body.dataset.newsStage = stage;
  document.body.dataset.reviewMode = news && state.reviewMode ? "true" : "false";
  const semanticComplete = state.semanticRun?.status === "complete";
  el["approval-status"].textContent = approved
    ? `人工核准・${semanticComplete ? "語義審完成" : "語義審未完成"}`
    : reviewed ? `初審完成・${semanticComplete ? "語義審完成" : "語義審未完成"}` : "草稿・未核准";
  el["approval-status"].className = `approval-badge ${approved ? "approved" : reviewed ? "reviewed" : "draft"}`;
  el["news-progress"].hidden = !news;
  document.querySelectorAll(".progress-step").forEach((item) => {
    const index = order.indexOf(item.dataset.stage);
    item.classList.toggle("active", news && index === activeIndex);
    item.classList.toggle("complete", news && index < activeIndex);
    item.classList.remove("failed", "running", "skipped", "proxy");
    if (approved && item.dataset.stage === "finalize") item.classList.add("complete");
    if (item.dataset.stage === "writer") {
      const writerStatus = state.writerJob?.status || state.workflow?.writer?.status;
      item.classList.toggle("running", news && ["queued", "running"].includes(writerStatus));
      item.classList.toggle("failed", news && writerStatus === "failed");
      item.classList.toggle("skipped", news && writerStatus === "skipped");
      item.classList.toggle("proxy", news && Boolean(state.workflow?.writer?.proxy || state.writerJob?.proxy));
    }
    if (["editor", "chief"].includes(item.dataset.stage) && state.semanticJob?.status === "running") {
      const runningStage = state.semanticJob.pass === "chief" ? "chief" : "editor";
      item.classList.toggle("running", item.dataset.stage === runningStage);
    }
    if (["editor", "chief"].includes(item.dataset.stage) && (state.semanticJob?.status || state.semanticRun?.status) === "failed") {
      item.classList.toggle("failed", item.dataset.stage === (state.semanticJob?.pass === "chief" ? "chief" : "editor"));
    }
  });

  if (!news) {
    el["stage-kicker"].textContent = "寫作助手";
    el["stage-title"].textContent = "從概念選擇寫作方向";
    el["stage-description"].textContent = "先說明想寫什麼，系統再解釋並推薦合適的大師卡。";
    el["news-controls"].hidden = true;
    el["writer-seat"].hidden = true;
    el["writing-controls"].hidden = false;
    el["results-section"].hidden = false;
    el["asset-detail-section"].hidden = !state.advancedOpen;
    el["news-review-content"].hidden = true;
    el["writing-review-content"].hidden = false;
    el["case-overview"].hidden = true;
    el.editor.hidden = false;
    el["article-review"].hidden = true;
    el["document-title"].readOnly = false;
    el["save-document"].hidden = false;
    el["back-to-edit"].hidden = true;
    el["rerun-review"].hidden = true;
    return;
  }

  const copy = {
    intake: ["步驟 1｜收稿", "放入一篇稿件", "上傳檔案或直接貼入中央編輯器，來源會自動辨識。"],
    writer: ["步驟 2｜寫手", "先把素材整理成稿", "選擇寫手卡與目標字數；完成後改寫稿會自動載入中央主板。"],
    editor: ["步驟 3｜編輯", "開始編輯審", "寫手已完成或已明確跳過；編輯審會使用主板目前版本。"],
    chief: ["步驟 4｜總編", "總編審執行中", "總編只在編輯之後進場；兩層建議仍需人工裁定。"],
    finalize: ["步驟 5｜人工定稿", approved ? "目前版本已由人工核准" : "確認修訂版並人工核准", approved ? "這是人類定稿，不代表百鬼語義總編通過；再次改稿會自動失效。" : "完成三項確認後，才能核准並解鎖影音企劃。"],
  }[stage];
  [el["stage-kicker"].textContent, el["stage-title"].textContent, el["stage-description"].textContent] = copy;
  const inReview = state.reviewMode && reviewed;
  el["news-controls"].hidden = inReview;
  el["writer-seat"].hidden = false;
  el["writing-controls"].hidden = true;
  el["results-section"].hidden = !inReview;
  el["asset-detail-section"].hidden = true;
  el["news-review-content"].hidden = !inReview;
  el["writing-review-content"].hidden = true;
  el["case-overview"].hidden = !inReview;
  el.editor.hidden = inReview;
  el["article-review"].hidden = !inReview;
  el["document-title"].readOnly = inReview;
  el["save-document"].hidden = inReview;
  el["back-to-edit"].hidden = !inReview;
  el["rerun-review"].hidden = !inReview;
  updateWriterControls();
}

function setMode(mode) {
  state.mode = mode;
  const media = mode === "media";
  const news = mode === "news";
  document.body.dataset.mode = mode;
  if (!media) state.lastReviewRun = news ? state.lastNewsReviewRun : state.lastWritingReviewRun;
  el["news-mode"].classList.toggle("active", news);
  el["news-mode"].setAttribute("aria-selected", news ? "true" : "false");
  el["writing-mode"].classList.toggle("active", !news && !media);
  el["writing-mode"].setAttribute("aria-selected", !news && !media ? "true" : "false");
  el["media-mode"].classList.toggle("active", media);
  el["media-mode"].setAttribute("aria-selected", media ? "true" : "false");
  el["media-workspace"].hidden = !media;
  if (media) {
    el["news-progress"].hidden = true;
    renderMediaState();
    return;
  }
  const showAdvanced = !news && state.advancedOpen;
  el["invocation-section"].hidden = !showAdvanced;
  el["catalog-section"].hidden = !showAdvanced;
  state.reviewMode = news && !state.dirty && Boolean(
    state.provisionalReview || state.lastReviewRun?.output?.workflow?.id === "chinatimes_newsroom",
  );
  if (news) {
    state.selected.style_cards = "";
    state.selected.templates = "";
    state.selected.editors = pickDefault("editors", "editor.news");
  } else {
    state.selected.style_cards = pickDefault("style_cards", state.selected.style_cards || "style.genre.knowledge_talk");
    state.selected.templates = pickDefault("templates", state.selected.templates || "template.copy.poison_short_text");
    state.selected.editors = pickDefault("editors", state.selected.editors && state.selected.editors !== "editor.news" ? state.selected.editors : "editor.de_ai");
  }
  if (el["style-select"]) el["style-select"].value = state.selected.style_cards;
  if (el["template-select"]) el["template-select"].value = state.selected.templates;
  if (el["persona-select"]) el["persona-select"].value = state.selected.editors;
  updateNewsStage();
  updateVideoPlanAvailability();
  renderCatalog();
  if (state.lastReviewRun && !state.dirty) renderReview(state.lastReviewRun.output);
}

function toggleAdvanced() {
  state.advancedOpen = !state.advancedOpen;
  el["catalog-section"].hidden = !state.advancedOpen;
  el["invocation-section"].hidden = !state.advancedOpen;
  el["toggle-advanced"].textContent = state.advancedOpen ? "收起全部卡片" : "手動瀏覽全部卡片";
  if (state.advancedOpen) {
    renderCatalog();
    setLibraryOpen(true);
  }
  updateNewsStage();
}

function bytesToBase64(bytes) {
  let binary = "";
  const chunk = 0x8000;
  for (let offset = 0; offset < bytes.length; offset += chunk) {
    binary += String.fromCharCode(...bytes.subarray(offset, Math.min(offset + chunk, bytes.length)));
  }
  return window.btoa(binary);
}

function inferSourceNotes(content, importedHint = "") {
  const notes = [];
  if (importedHint) notes.push(importedHint);
  const urls = content.match(/https?:\/\/[^\s)）\]】>]+/gi) || [];
  urls.slice(0, 8).forEach((url) => notes.push(`內文網址：${url}`));
  content.split(/\r?\n/).forEach((line) => {
    const clean = line.trim();
    if (/^(來源|資料來源|記者|編譯|攝影|圖|文|採訪)\s*[：:]/.test(clean)) notes.push(clean);
  });
  return [...new Set(notes)].slice(0, 12);
}

function refreshAutomaticSourceNotes() {
  const notes = inferSourceNotes(el.editor.value, state.importedSourceHint);
  el["source-notes"].value = notes.join("\n");
  el["source-detection-status"].textContent = notes.length
    ? `已辨識 ${notes.length} 項來源線索；審稿時會一併核對。`
    : "尚未辨識到來源線索；審稿時會直接列為待補缺口。";
  return el["source-notes"].value;
}

function renderStructurePreview(structure) {
  if (!el["structure-preview"]) return;
  if (!structure?.blocks?.length) {
    el["structure-preview"].textContent = "尚未辨識到可標記的段落。";
    return;
  }
  const summary = structure.summary || {};
  const parts = [
    ["主標", summary.main_title], ["副標", summary.subtitle], ["前言", summary.lead],
    ["小標", summary.subheading], ["正文", summary.body],
  ].filter(([, count]) => count).map(([label, count]) => `${label} ${count}`);
  el["structure-preview"].textContent = `已自動辨識：${parts.join("・")}。進入批註視圖後可逐段改判。`;
}

function scheduleStructurePreview() {
  window.clearTimeout(scheduleStructurePreview.timer);
  const content = el.editor.value;
  scheduleStructurePreview.timer = window.setTimeout(async () => {
    try {
      const detected = await api("/api/structure/detect", {
        method: "POST",
        body: JSON.stringify({ content }),
      });
      if (content !== el.editor.value) return;
      state.structurePreview = detected;
      if (state.dirty || !state.structure) renderStructurePreview(detected);
    } catch (error) {
      el["structure-preview"].textContent = `結構偵測失敗：${error.message}`;
    }
  }, 180);
}

async function loadStructure() {
  if (!state.currentDocument || state.dirty) return null;
  state.structure = await api(`/api/documents/${encodeURIComponent(state.currentDocument.id)}/structure`);
  renderStructurePreview(state.structure);
  return state.structure;
}

async function loadWorkflow() {
  if (!state.currentDocument || state.dirty) return;
  state.workflow = await api(`/api/documents/${encodeURIComponent(state.currentDocument.id)}/workflow`);
  state.importedSourceHint = state.workflow.sources?.find((item) => item.clue_kind === "attachment")?.clue_text || "";
  renderSourceClues();
  renderWriterState();
  updateNewsStage();
  updateVideoPlanAvailability();
}

function renderSourceClues() {
  if (!el["source-clue-list"]) return;
  clear(el["source-clue-list"]);
  const workflow = state.workflow;
  if (!workflow || state.dirty) {
    const p = document.createElement("p");
    p.className = "empty-state";
    p.textContent = "請先儲存目前稿件，再逐項裁定來源線索。";
    el["source-clue-list"].appendChild(p);
    el["source-readiness"].textContent = "等待儲存";
    el["source-readiness"].className = "metric-badge source pending";
    el["source-readiness-detail"].textContent = "稿件尚未儲存，來源線索未建立。";
    return;
  }
  (workflow.sources || []).forEach((clue) => {
    const row = document.createElement("article");
    row.className = "source-clue";
    row.dataset.clueId = clue.clue_id;
    row.dataset.clueKind = clue.clue_kind;
    row.dataset.clueText = clue.clue_text;
    row.dataset.status = clue.status || "pending";

    const head = document.createElement("div");
    head.className = "source-clue-head";
    const kind = document.createElement("span");
    kind.className = "source-kind";
    kind.textContent = ({ attachment: "附件", url: "網址", byline: "署名", attribution: "歸因", missing: "缺少來源" })[clue.clue_kind] || "來源";
    const saved = document.createElement("span");
    saved.className = "source-save-state";
    saved.textContent = clue.updated_at ? "已儲存" : "待裁定";
    head.append(kind, saved);

    const text = document.createElement("p");
    text.textContent = clue.clue_text;
    const controls = document.createElement("div");
    controls.className = "source-clue-controls";
    const statusGroup = document.createElement("div");
    statusGroup.className = "source-status-buttons";
    statusGroup.setAttribute("role", "group");
    statusGroup.setAttribute("aria-label", `裁定來源：${clue.clue_text}`);
    const note = document.createElement("input");
    note.type = "text";
    note.className = "source-note-input";
    note.maxLength = 500;
    note.value = clue.note || "";
    note.placeholder = "存疑時必填原因；其他狀態可補充說明";
    note.setAttribute("aria-label", `來源裁定說明：${clue.clue_text}`);

    [["confirmed", "確認"], ["doubt", "存疑"], ["gap", "缺口"]].forEach(([value, label]) => {
      const action = button(label, `source-status-button status-${value}`, async () => {
        if (value === "doubt" && !note.value.trim()) {
          toast("存疑必須先填寫備註。 ");
          note.focus();
          return;
        }
        row.dataset.status = value;
        await saveSourceDecisions();
      });
      action.dataset.status = value;
      action.setAttribute("aria-pressed", clue.status === value ? "true" : "false");
      if (clue.clue_kind === "missing" && value !== "gap") action.disabled = true;
      statusGroup.appendChild(action);
    });
    note.addEventListener("blur", async () => {
      if (row.dataset.status !== "doubt" || note.value.trim() === (clue.note || "").trim()) return;
      if (!note.value.trim()) {
        toast("存疑備註不可留白，尚未儲存。 ");
        return;
      }
      await saveSourceDecisions();
    });
    controls.append(statusGroup, note);
    row.append(head, text, controls);
    el["source-clue-list"].appendChild(row);
  });

  const readiness = workflow.source_readiness || {};
  el["source-readiness"].textContent = readiness.ready
    ? "來源就緒"
    : `${readiness.pending || 0} 待裁・${readiness.gaps || 0} 缺口`;
  el["source-readiness"].className = `metric-badge source ${readiness.ready ? "ready" : "pending"}`;
  el["source-readiness-detail"].textContent = readiness.ready
    ? `共 ${workflow.sources?.length || 0} 項來源線索，已完成裁定。`
    : `${readiness.pending || 0} 項待裁、${readiness.gaps || 0} 項缺口、${readiness.doubts_without_note || 0} 項存疑未註。`;
}

async function saveSourceDecisions() {
  if (!state.currentDocument || state.dirty) {
    toast("請先儲存目前稿件。");
    return;
  }
  const decisions = [...el["source-clue-list"].querySelectorAll(".source-clue")].map((row) => ({
    clue_id: row.dataset.clueId,
    clue_kind: row.dataset.clueKind,
    clue_text: row.dataset.clueText,
    status: row.dataset.status || "pending",
    note: row.querySelector(".source-note-input").value.trim(),
  }));
  if (decisions.some((item) => item.status === "doubt" && !item.note)) {
    toast("存疑來源必須填寫備註。 ");
    return;
  }
  el["source-clue-list"].setAttribute("aria-busy", "true");
  el["source-clue-list"].querySelectorAll("button, input").forEach((control) => { control.disabled = true; });
  try {
    state.workflow = await api(`/api/documents/${encodeURIComponent(state.currentDocument.id)}/sources`, {
      method: "POST",
      body: JSON.stringify({ decisions, expected_revision_id: state.workflow?.revision_id }),
    });
    if (state.lastReviewRun) renderReview(state.lastReviewRun.output);
    else renderSourceClues();
    if (!state.workflow.video_plan_eligible) resetVideoPlan();
    updateNewsStage();
    updateVideoPlanAvailability();
    if (state.workflow.source_readiness.ready && state.provisionalReview) {
      toast("來源已就緒，正在建立正式審稿紀錄…");
      await runReview({ skipSave: true, quiet: true, skipSemantic: true });
    } else {
      toast(state.workflow.source_readiness.ready ? "來源裁定已完成。" : "來源裁定已自動儲存。");
    }
  } catch (error) {
    toast(error.message);
    if (error.status === 409) await openDocument(state.currentDocument.id).catch(() => {});
  } finally {
    el["source-clue-list"].removeAttribute("aria-busy");
  }
}

async function importArticle() {
  const file = el["article-upload"].files[0];
  if (!file) return;
  if (file.size > 2 * 1024 * 1024) {
    toast("稿件超過 2 MB，目前不接受。 ");
    el["article-upload"].value = "";
    return;
  }
  try {
    const bytes = new Uint8Array(await file.arrayBuffer());
    const imported = await api("/api/import", {
      method: "POST",
      body: JSON.stringify({ filename: file.name, content_base64: bytesToBase64(bytes) }),
    });
    if (el.editor.value.trim() && !window.confirm("要用上傳稿件取代中央編輯器目前的內容嗎？既有已儲存版本不會被刪除。")) return;
    el["document-title"].value = imported.title;
    el.editor.value = imported.content;
    state.importedSourceHint = `附件檔名：${imported.filename}`;
    refreshAutomaticSourceNotes();
    markDirty();
    toast(`已載入 ${imported.filename}，共 ${imported.characters} 字。`);
  } catch (error) {
    toast(error.message);
  } finally {
    el["article-upload"].value = "";
  }
}

async function recommendStyles() {
  const concept = el["writing-concept"].value.trim();
  if (!concept) {
    toast("請先輸入想寫的概念、用途或感覺。 ");
    el["writing-concept"].focus();
    return;
  }
  el["recommend-styles"].disabled = true;
  el["recommend-styles"].textContent = "正在比對卡庫…";
  try {
    const payload = await api("/api/recommend/styles", {
      method: "POST",
      body: JSON.stringify({ concept, limit: 5 }),
    });
    renderStyleRecommendations(payload.items || []);
  } catch (error) {
    toast(error.message);
  } finally {
    el["recommend-styles"].disabled = false;
    el["recommend-styles"].textContent = "建議大師卡方向";
  }
}

function renderStyleRecommendations(items) {
  clear(el["style-recommendations"]);
  el["style-recommendations"].className = "style-recommendations";
  items.forEach((item) => {
    const card = document.createElement("article");
    card.className = "recommendation-card";
    const title = document.createElement("h3");
    title.textContent = item.name;
    const direction = document.createElement("p");
    direction.className = "direction";
    direction.textContent = item.direction;
    const why = document.createElement("p");
    why.textContent = `為何推薦：${item.why}`;
    const best = document.createElement("p");
    best.textContent = `適合：${(item.best_for || []).join("、") || "請查看卡片正本"}`;
    card.append(title, direction, why, best);
    if ((item.avoid || []).length) {
      const avoid = document.createElement("p");
      avoid.textContent = `不適合／避免：${item.avoid.join("；")}`;
      card.appendChild(avoid);
    }
    if (item.quality_label) {
      const quality = document.createElement("p");
      quality.textContent = `卡片資格：${item.quality_label}`;
      card.appendChild(quality);
    }
    card.appendChild(button("選用這張卡", "text-button", async () => {
      state.selected.style_cards = item.id;
      el["style-select"].value = item.id;
      await selectAsset("style_cards", item.id);
      toast(`已選用 ${item.name}。`);
    }));
    el["style-recommendations"].appendChild(card);
  });
  if (!items.length) {
    const empty = document.createElement("p");
    empty.textContent = "目前找不到適合的正式卡。";
    el["style-recommendations"].appendChild(empty);
  }
}

async function loadHealth() {
  state.health = await api("/api/health");
  el["runtime-status"].textContent = `本機模式・${state.health.counts.style_cards} 張卡`;
  el["runtime-status"].className = "status-pill status-ok";
}

async function loadCatalogs() {
  const kinds = ["style_cards", "templates", "editors"];
  const responses = await Promise.all(kinds.map((kind) => api(`/api/catalog?kind=${kind}`)));
  kinds.forEach((kind, index) => { state.catalogs[kind] = responses[index].items; });
  state.selected.style_cards = pickDefault("style_cards", "style.genre.taiwan_poison_short");
  state.selected.templates = pickDefault("templates", "template.copy.poison_short_text");
  state.selected.editors = pickDefault("editors", "editor.de_ai");
  populateSelect(el["style-select"], "style_cards", "不套用大師卡");
  populateSelect(el["template-select"], "templates", "不套用模板");
  populateSelect(el["persona-select"], "editors", "不指定人物卡");
  renderCatalog();
}

async function loadWriterCards(concept = "", preserveSelection = true) {
  const payload = await api(`/api/writer/cards?concept=${encodeURIComponent(concept)}`);
  state.writerCards = payload.items || [];
  const previous = preserveSelection ? state.selectedWriterCard : "";
  const previousUsable = state.writerCards.some((item) => item.id === previous && item.available);
  state.selectedWriterCard = previousUsable ? previous : payload.default_id;
  clear(el["writer-card-select"]);
  state.writerCards.forEach((item) => {
    const option = document.createElement("option");
    option.value = item.id;
    option.disabled = !item.available;
    const flags = [item.category];
    if (item.default) flags.push("建議");
    if (!item.available) flags.push("卡源失聯");
    option.textContent = `${item.name}｜${flags.join("｜")}`;
    el["writer-card-select"].appendChild(option);
  });
  if (!state.writerCards.some((item) => item.id === state.selectedWriterCard && item.available)) {
    state.selectedWriterCard = state.writerCards.find((item) => item.available)?.id || "";
  }
  el["writer-card-select"].value = state.selectedWriterCard;
  renderWriterRecommendation();
  updateWriterControls();
}

function suggestedWriterCard() {
  const text = `${el["document-title"]?.value || ""}\n${el.editor?.value || ""}`.toLocaleLowerCase("zh-TW");
  if (/(汽車|車市|車款|試駕|新車)/.test(text)) return ["writer.chen_car", "內容提到汽車／車市，建議陳宏銘車版筆法"];
  if (/(財經|金融|企業|營收|投資|股市|銀行)/.test(text)) return ["writer.chen_finance", "內容偏財經事件，建議陳宏銘財經筆法"];
  if (/(訪談|專訪|受訪|人物|逐字稿)/.test(text)) return ["writer.dong_chengyu", "內容偏人物／訪談，建議董成瑜人物筆法"];
  return ["writer.dong_chengyu", "預設建議人物訪談筆法"];
}

function renderWriterRecommendation() {
  const selected = state.writerCards.find((item) => item.id === state.selectedWriterCard);
  const [recommendedId, reason] = suggestedWriterCard();
  const recommended = state.writerCards.find((item) => item.id === recommendedId);
  const selection = selected ? `目前：${selected.category}` : "尚無可用寫手卡";
  el["writer-recommendation"].textContent = `${selection}。${reason}${recommended && recommended.id !== selected?.id ? `；可改選「${recommended.name}」` : ""}。建議不會自動改掉你的選擇。`;
}

function updateWriterControls() {
  if (!el["start-writer"]) return;
  const writerStatus = state.writerJob?.status || state.workflow?.writer?.status || "not_started";
  const busy = ["queued", "running"].includes(writerStatus);
  const selected = state.writerCards.find((item) => item.id === state.selectedWriterCard);
  const hasContent = Boolean(el.editor?.value.trim());
  el["start-writer"].disabled = busy || !hasContent || !selected?.available;
  el["skip-writer"].disabled = busy || !hasContent || Boolean(state.workflow?.writer?.allowed_to_edit);
  // 未儲存變更不擋審稿：runReview 會自動先存（審稿前儲存）；這裡曾經静默灰化，使用者按下去沒反應。
  const allowed = Boolean(state.workflow?.writer?.allowed_to_edit);
  el["run-review"].disabled = !allowed || busy;
  if (el["rerun-review"] && !el["rerun-review"].hidden) el["rerun-review"].disabled = !allowed || busy;
}

function appendWriterReportSection(container, titleText, values) {
  const section = document.createElement("section");
  const title = document.createElement("h3");
  title.textContent = titleText;
  section.appendChild(title);
  const rows = Array.isArray(values) ? values : (values ? [values] : []);
  if (rows.length) {
    const list = document.createElement("ul");
    rows.forEach((value) => {
      const item = document.createElement("li");
      item.textContent = value;
      list.appendChild(item);
    });
    section.appendChild(list);
  } else {
    const empty = document.createElement("p");
    empty.textContent = "未列出";
    section.appendChild(empty);
  }
  container.appendChild(section);
}

function renderWriterState() {
  if (!el["writer-status"]) return;
  const writer = state.workflow?.writer || {};
  const active = state.writerJob;
  const status = active?.status || writer.status || "not_started";
  const labels = {
    not_started: "尚未開始",
    queued: "排隊中",
    running: active?.proxy ? "備位代打中" : "寫稿中",
    complete: writer.proxy ? "完成・代打" : "完成",
    skipped: "已明確跳過",
    failed: "失敗",
  };
  el["writer-status"].textContent = labels[status] || status;
  el["writer-status"].className = `writer-status ${status}${writer.proxy || active?.proxy ? " proxy" : ""}`;
  const error = active?.error || writer.error;
  el["writer-error"].hidden = !error;
  el["writer-error"].textContent = error ? `${error.code || "failed"}：${error.message || "寫手工作失敗"}` : "";
  clear(el["writer-report"]);
  const report = writer.report;
  if (report) {
    el["writer-report"].className = "writer-report";
    if (writer.proxy_label) {
      const proxy = document.createElement("p");
      proxy.className = "proxy-label";
      proxy.textContent = writer.proxy_label;
      el["writer-report"].appendChild(proxy);
    }
    appendWriterReportSection(el["writer-report"], "講者判讀", report.speaker_assessment);
    appendWriterReportSection(el["writer-report"], "字數與素材說明", report.length_note);
    appendWriterReportSection(el["writer-report"], "缺口清單", report.gaps);
    appendWriterReportSection(el["writer-report"], "待查證專名", report.names_to_verify);
  } else {
    el["writer-report"].className = "writer-report empty-state";
    const p = document.createElement("p");
    p.textContent = status === "skipped" ? "本輪由使用者明確跳過寫手，編輯將直接審原稿。" : "完成後會顯示講者判讀、素材缺口與待查證專名。";
    el["writer-report"].appendChild(p);
  }
  updateWriterControls();
}

function pickDefault(kind, preferredId) {
  const exists = state.catalogs[kind].some((item) => item.id === preferredId);
  return exists ? preferredId : (state.catalogs[kind][0]?.id || "");
}

function populateSelect(select, kind, emptyLabel) {
  clear(select);
  const empty = document.createElement("option");
  empty.value = "";
  empty.textContent = emptyLabel;
  select.appendChild(empty);
  state.catalogs[kind].forEach((item) => {
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent = item.name;
    select.appendChild(option);
  });
  select.value = state.selected[kind];
}

function renderCatalog() {
  const kind = state.activeKind;
  const query = el["catalog-search"].value.trim().toLocaleLowerCase("zh-TW");
  const items = state.catalogs[kind].filter((item) => {
    if (kind === "style_cards" && state.trainingFilter === "complete_text_20plus" && !item.complete_text_training) return false;
    if (kind === "style_cards" && state.trainingFilter === "other_literary" && (item.family !== "literary_master" || item.complete_text_training)) return false;
    const haystack = [item.name, item.id, item.family, item.role, item.style_axis, item.quality_label].join(" ").toLocaleLowerCase("zh-TW");
    return !query || haystack.includes(query);
  });
  el["training-filter-wrap"].hidden = kind !== "style_cards";
  updateTrainingFilterLabels();
  el["catalog-count"].textContent = String(items.length);
  clear(el["catalog-list"]);
  items.forEach((item) => {
    const row = button("", `asset-row${item.complete_text_training ? " complete-text-card" : ""}${state.selected[kind] === item.id ? " active" : ""}`, () => selectAsset(kind, item.id));
    const title = document.createElement("strong");
    title.textContent = item.name;
    const meta = document.createElement("span");
    meta.className = "asset-meta";
    const left = document.createElement("span");
    const dot = document.createElement("i");
    dot.className = `quality-dot ${item.quality_tone || "active"}`;
    dot.setAttribute("aria-hidden", "true");
    left.append(dot, document.createTextNode(item.family || item.kind));
    const right = document.createElement("span");
    right.textContent = item.quality_label || item.status;
    meta.append(left, right);
    row.append(title, meta);
    el["catalog-list"].appendChild(row);
  });
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "找不到符合條件的資產。";
    el["catalog-list"].appendChild(empty);
  }
}

function updateTrainingFilterLabels() {
  const summary = state.health?.training_summary;
  if (!summary) return;
  const options = el["training-filter"].options;
  options[0].textContent = `全部能力卡（${summary.all_style_cards}）`;
  options[1].textContent = `完整文本書目大師卡（${summary.complete_text_20plus}）`;
  options[2].textContent = `其他文學大師卡（${summary.other_literary_masters}）`;
  const notes = {
    all: "完整文本卡會有獨立綠色標章；其他卡仍保留真實資格標示。",
    complete_text_20plus: "只顯示新制 original_language_20plus_close_read_complete，不包含舊制 20 作品包。",
    other_literary: "包含 10+／近 20、部分文本、譯本邊界及未取得新制完整證明的文學大師卡。",
  };
  el["training-filter-note"].textContent = notes[state.trainingFilter];
}

async function selectAsset(kind, id) {
  state.selected[kind] = id;
  const selectMap = { style_cards: el["style-select"], templates: el["template-select"], editors: el["persona-select"] };
  selectMap[kind].value = id;
  renderCatalog();
  try {
    state.selectedAsset = await api(`/api/catalog/item?kind=${encodeURIComponent(kind)}&id=${encodeURIComponent(id)}`);
    renderAssetDetail();
  } catch (error) {
    toast(error.message);
  }
}

function renderAssetDetail() {
  const item = state.selectedAsset;
  clear(el["asset-detail"]);
  el["asset-detail"].className = "asset-detail";
  if (!item) return;
  const title = document.createElement("h3");
  title.textContent = item.name;
  const id = document.createElement("code");
  id.textContent = item.id;
  const badge = document.createElement("span");
  badge.className = `quality-badge ${item.quality_tone || "active"}`;
  badge.textContent = item.quality_label;
  const description = document.createElement("p");
  description.textContent = item.role || item.style_axis || (Array.isArray(item.best_for) ? item.best_for.join("、") : item.best_for) || "請閱讀正本摘要判斷用途。";
  const excerpt = document.createElement("div");
  excerpt.className = "asset-excerpt";
  excerpt.textContent = item.content.slice(0, 900);
  const trainingNote = document.createElement("p");
  trainingNote.textContent = item.training_note || "";
  el["asset-detail"].append(title, id, document.createElement("br"), badge, description, trainingNote, excerpt);
}

async function loadDocuments(preferredId = "") {
  const payload = await api("/api/documents");
  state.documents = payload.items;
  renderDocuments();
  const target = preferredId || state.currentDocument?.id || state.documents[0]?.id;
  if (target) await openDocument(target);
}

function renderDocuments() {
  clear(el["document-list"]);
  state.documents.forEach((doc) => {
    const row = button("", `list-button${state.currentDocument?.id === doc.id ? " active" : ""}`, () => openDocument(doc.id));
    const title = document.createElement("strong");
    title.textContent = doc.title;
    const meta = document.createElement("span");
    meta.textContent = `${doc.content_length} 字元・${shortDate(doc.updated_at)}`;
    row.append(title, meta);
    el["document-list"].appendChild(row);
  });
}

async function openDocument(id) {
  if (state.dirty && state.currentDocument && !window.confirm("目前修改尚未儲存，確定切換文件？")) return;
  state.currentDocument = await api(`/api/documents/${encodeURIComponent(id)}`);
  el["document-title"].value = state.currentDocument.title;
  el.editor.value = state.currentDocument.content;
  state.importedSourceHint = "";
  refreshAutomaticSourceNotes();
  state.dirty = false;
  updateSaveState("已載入版本");
  renderDocuments();
  updateDocumentMeta();
  await loadHistory();
  await loadWorkflow();
  await loadWriterCards(`${state.currentDocument.title}\n${state.currentDocument.content}`, true);
  await loadStructure();
  const runs = await api(`/api/documents/${encodeURIComponent(id)}/runs`);
  state.lastReviewRun = runs.items.find((item) => item.id === state.workflow?.review_run_id) || null;
  state.lastNewsReviewRun = state.lastReviewRun;
  state.lastWritingReviewRun = runs.items.find((item) => (
    item.action === "local_review"
    && item.revision_id === state.currentDocument.current_revision_id
    && !item.output?.workflow
  )) || null;
  state.provisionalReview = false;
  state.semanticJob = null;
  state.semanticRun = runs.items.find((item) => (
    item.action === "semantic_review"
    && item.revision_id === state.currentDocument.current_revision_id
    && item.output?.provenance?.structure_hash === state.structure?.structure_hash
  )) || null;
  if (!state.semanticRun && state.workflow?.semantic?.status === "stale") {
    state.semanticRun = {
      status: "stale",
      output: { error: { code: "structure_changed", message: "段落結構已改判，請重新語義審。" } },
    };
  }
  state.semanticPollToken += 1;
  state.writerPollToken += 1;
  state.writerPollFailures = 0;
  state.writerJob = null;
  if (["queued", "running"].includes(state.workflow?.writer?.status) && state.workflow.writer.job_id) {
    state.writerJob = await api(`/api/writer/jobs/${encodeURIComponent(state.workflow.writer.job_id)}`);
    const writerToken = state.writerPollToken;
    window.setTimeout(() => pollWriterJob(state.writerJob.id, id, writerToken), 120);
  }
  if (["queued", "running"].includes(state.workflow?.semantic?.status) && state.workflow.semantic.job_id) {
    state.semanticJob = await api(`/api/semantic/jobs/${encodeURIComponent(state.workflow.semantic.job_id)}`);
    const token = state.semanticPollToken;
    window.setTimeout(() => pollSemanticJob(state.semanticJob.id, token), 120);
  }
  if (!state.lastReviewRun && state.semanticRun && state.mode === "news") {
    const preflight = await api(`/api/documents/${encodeURIComponent(id)}/preflight`, {
      method: "POST",
      body: JSON.stringify({
        expected_revision_id: state.currentDocument.current_revision_id,
        source_notes: refreshAutomaticSourceNotes(),
      }),
    });
    state.lastReviewRun = { id: null, output: preflight, provisional: true };
    state.lastNewsReviewRun = state.lastReviewRun;
    state.provisionalReview = true;
  }
  state.reviewMode = state.mode === "news" && Boolean(state.lastReviewRun || state.semanticRun);
  const latestPlan = runs.items.find((item) => item.action === "video_plan_draft" && item.revision_id === state.currentDocument.current_revision_id);
  if (state.lastReviewRun) renderReview(state.lastReviewRun.output);
  else resetReview();
  renderSemanticStatus();
  renderWriterState();
  if (
    latestPlan?.status === "approved_source_ready_draft"
    && state.workflow?.video_plan_eligible
    && latestPlan.output?.provenance?.review_run_id === state.workflow?.review_run_id
  ) renderVideoPlan(latestPlan.output);
  else resetVideoPlan();
  updateVideoPlanAvailability();
  updateNewsStage();
  setLibraryOpen(false);
}

function markDirty() {
  state.dirty = true;
  state.lastReviewRun = null;
  state.lastNewsReviewRun = null;
  state.lastWritingReviewRun = null;
  state.reviewMode = false;
  state.provisionalReview = false;
  state.workflow = null;
  state.structure = null;
  state.semanticJob = null;
  state.semanticRun = null;
  state.semanticPollToken += 1;
  updateSaveState("有尚未儲存的修改");
  updateDocumentMeta();
  resetVideoPlan();
  renderSourceClues();
  updateNewsStage();
  updateVideoPlanAvailability();
  renderSemanticStatus();
  renderWriterRecommendation();
  renderWriterState();
  scheduleStructurePreview();
}

function updateSaveState(message) {
  el["save-status"].textContent = message;
}

function updateDocumentMeta() {
  const text = el.editor.value;
  const characters = text.replace(/\s/g, "").length;
  const paragraphs = text.split(/\n\s*\n/).filter((part) => part.trim()).length;
  el["document-stats"].textContent = `${characters} 字・${paragraphs} 段`;
  el["document-hash"].textContent = state.dirty ? "內容已變更，尚未產生雜湊" : `SHA-256 ${shortHash(state.currentDocument?.content_hash)}`;
}

async function saveDocument(note = "手動儲存") {
  if (!state.currentDocument) return null;
  el["save-document"].disabled = true;
  try {
    const updated = await api(`/api/documents/${encodeURIComponent(state.currentDocument.id)}`, {
      method: "PUT",
      body: JSON.stringify({
        title: el["document-title"].value,
        content: el.editor.value,
        note,
        source_hint: state.importedSourceHint,
      }),
    });
    state.currentDocument = updated;
    state.dirty = false;
    if (note !== "審稿前儲存") state.lastReviewRun = null;
    updateSaveState("版本已儲存");
    updateDocumentMeta();
    await refreshDocumentListOnly();
    await loadHistory();
    await loadWorkflow();
    await loadStructure();
    updateVideoPlanAvailability();
    return updated;
  } finally {
    el["save-document"].disabled = false;
  }
}

async function refreshDocumentListOnly() {
  const payload = await api("/api/documents");
  state.documents = payload.items;
  renderDocuments();
}

async function createDocument() {
  const title = window.prompt("新文件名稱", "未命名文件");
  if (title === null) return;
  const created = await api("/api/documents", {
    method: "POST",
    body: JSON.stringify({ title, content: "" }),
  });
  await loadDocuments(created.id);
  el.editor.focus();
  toast("已建立新文件，原稿快照已保存。 ");
}

function provisionalSuggestion(category, severity, message, excerpt = "", start = null, end = null) {
  return {
    id: `preview-${crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`}`,
    category, severity, message, excerpt, start, end,
  };
}

function buildProvisionalNewsReview() {
  const content = el.editor.value;
  const title = el["document-title"].value;
  const suggestions = [];
  if (!title.trim() || title.trim() === "未命名文件") {
    suggestions.push(provisionalSuggestion("標題", "warning", "標題尚未定稿，匯出或送審前應補上。"));
  }
  for (const match of content.matchAll(/[!?！？]{2,}/g)) {
    suggestions.push(provisionalSuggestion("標點", "warning", "連續驚嘆或問號會削弱語氣，建議保留一個。", match[0], match.index, match.index + match[0].length));
  }
  for (const match of content.matchAll(/ {2,}/g)) {
    suggestions.push(provisionalSuggestion("格式", "note", "發現連續空白，建議整理格式。", match[0], match.index, match.index + match[0].length));
  }
  const aiPhrases = ["賦能", "打造", "關鍵", "深入探討", "提升效率", "全方位", "生態系", "價值創造", "核心競爭力", "在這個快速變化的時代"];
  aiPhrases.forEach((phrase) => {
    let offset = 0;
    while ((offset = content.indexOf(phrase, offset)) >= 0) {
      suggestions.push(provisionalSuggestion("AI 痕跡", "warning", `「${phrase}」屬百鬼禁用／高風險套語，請改成具體動作或事實。`, phrase, offset, offset + phrase.length));
      offset += phrase.length;
    }
  });
  if (/\d/.test(content)) suggestions.push(provisionalSuggestion("數字查核", "note", "稿件含數字；送總編前應逐項核對數值、單位、期間與來源定位。"));
  if (/批|轟|控|指控|質疑|痛批|怒斥/.test(content) && !/回應|說明|尚未回覆|聯繫|求證/.test(content)) {
    suggestions.push(provisionalSuggestion("同項回應", "warning", "稿件含攻防或指控語句，但未找到回應／求證狀態；需確認是否回應同一項指控。"));
  }
  const warnings = suggestions.filter((item) => item.severity === "warning").length;
  const notes = suggestions.filter((item) => item.severity === "note").length;
  const rewritePersonas = [
    { id: "editor.news", name: "新聞編輯卡", reason: "負責標題、錯字、專名、數字、引文、分桌與結構改寫。" },
  ];
  if (suggestions.some((item) => ["AI 痕跡", "節奏", "格式", "標點"].includes(item.category))) {
    rewritePersonas.push({ id: "editor.de_ai", name: "去AI味編輯", reason: "稿件出現模板腔、節奏或句面問題，適合在事實與結構修正後收尾。" });
  }
  return {
    engine: "local_preview",
    notice: "已顯示本機預檢；完成來源裁定後會自動建立版本綁定的正式審稿紀錄。",
    summary: { warnings, notes, total: suggestions.length },
    suggestions,
    rewrite_personas: rewritePersonas,
  };
}

function renderSemanticStatus() {
  if (!el["semantic-status"]) return;
  const active = state.semanticJob;
  const run = state.semanticRun;
  const status = active?.status || run?.status || "not_started";
  const labels = {
    queued: "語義審排隊中",
    running: active?.pass === "chief" ? "總編審執行中" : "編輯審執行中",
    complete: "編輯審與總編審已完成",
    failed: "語義審未完成",
    stale: "段落結構已變更・語義審需重跑",
    not_started: "語義審尚未開始",
  };
  el["semantic-status"].textContent = labels[status] || "語義審狀態未知";
  el["semantic-status"].className = `semantic-status ${status.replace("_", "-")}`;
  const error = active?.error || run?.output?.error;
  el["semantic-error"].hidden = !error;
  el["semantic-error"].textContent = error ? `${error.code || "failed"}：${error.message || "語義引擎失敗"}` : "";
  if (status === "complete") {
    el["semantic-boundary"].textContent = "兩遍語義審已完成；批註仍是編輯建議，最終定稿只由人工核准。";
  } else if (status === "failed" || status === "stale") {
    el["semantic-boundary"].textContent = "語義審未完成；下方只保留本機預檢與來源裁定，不會補造語義批註。";
  } else if (status === "queued" || status === "running") {
    el["semantic-boundary"].textContent = "語義引擎在背景工作；編輯器與來源裁定仍可操作。";
  } else {
    el["semantic-boundary"].textContent = "語義審尚未完成；本機預檢不能冒充總編通過。";
  }
}

async function startWriter() {
  if (!state.currentDocument) return;
  try {
    if (state.dirty) await saveDocument("寫手前儲存");
    const targetLength = Number(el["writer-target-length"].value);
    if (!Number.isInteger(targetLength) || targetLength < 200 || targetLength > 20000) {
      throw new Error("目標字數必須是 200 到 20000 的整數");
    }
    state.writerPollToken += 1;
    state.writerPollFailures = 0;
    const token = state.writerPollToken;
    const documentId = state.currentDocument.id;
    state.writerJob = await api(`/api/documents/${encodeURIComponent(documentId)}/rewrite`, {
      method: "POST",
      body: JSON.stringify({
        writer_card_id: state.selectedWriterCard,
        target_length: targetLength,
        expected_revision_id: state.currentDocument.current_revision_id,
      }),
    });
    renderWriterState();
    updateNewsStage();
    window.setTimeout(() => pollWriterJob(state.writerJob.id, documentId, token), 120);
  } catch (error) {
    toast(error.message);
    if (error.status === 409) await openDocument(state.currentDocument.id).catch(() => {});
  }
}

async function pollWriterJob(jobId, documentId, token) {
  if (token !== state.writerPollToken) return;
  try {
    const job = await api(`/api/writer/jobs/${encodeURIComponent(jobId)}`);
    if (token !== state.writerPollToken) return;
    state.writerPollFailures = 0;
    state.writerJob = job;
    renderWriterState();
    updateNewsStage();
    if (["queued", "running"].includes(job.status)) {
      window.setTimeout(() => pollWriterJob(jobId, documentId, token), 900);
      return;
    }
    if (job.status === "complete") {
      state.writerJob = null;
      if (state.currentDocument?.id === documentId && !state.dirty) {
        await openDocument(documentId);
        toast("寫手已完成，已顯示於主板");
      } else if (state.currentDocument?.id === documentId) {
        toast("寫手已完成並建立版本；主板有未儲存修改，因此未自動切換。 ");
      }
    } else {
      await loadWorkflow();
      renderWriterState();
      toast(`寫手失敗：${job.error?.message || "未知原因"}；主板原稿未變更`);
    }
  } catch (error) {
    if (token !== state.writerPollToken) return;
    state.writerPollFailures += 1;
    if (state.writerPollFailures < 3) {
      window.setTimeout(() => pollWriterJob(jobId, documentId, token), 1200);
      return;
    }
    state.writerJob = { status: "failed", error: { code: "poll_failed", message: error.message } };
    renderWriterState();
    updateNewsStage();
  }
}

async function skipWriter() {
  if (!state.currentDocument) return;
  try {
    if (state.dirty) await saveDocument("跳過寫手前儲存");
    const payload = await api(`/api/documents/${encodeURIComponent(state.currentDocument.id)}/rewrite`, {
      method: "POST",
      body: JSON.stringify({
        skip_writer: true,
        expected_revision_id: state.currentDocument.current_revision_id,
      }),
    });
    state.workflow = payload.workflow;
    renderWriterState();
    updateNewsStage();
    await loadHistory();
    toast("已明確跳過寫手；編輯審已解鎖。 ");
  } catch (error) {
    toast(error.message);
    if (error.status === 409) await openDocument(state.currentDocument.id).catch(() => {});
  }
}

async function pollSemanticJob(jobId, token) {
  if (token !== state.semanticPollToken) return;
  try {
    const job = await api(`/api/semantic/jobs/${encodeURIComponent(jobId)}`);
    if (token !== state.semanticPollToken) return;
    state.semanticJob = job;
    renderSemanticStatus();
    if (job.status === "queued" || job.status === "running") {
      window.setTimeout(() => pollSemanticJob(jobId, token), 900);
      return;
    }
    state.semanticRun = job.run || null;
    state.semanticJob = null;
    await loadWorkflow();
    if (token !== state.semanticPollToken) return;
    renderReview(state.lastReviewRun?.output || buildProvisionalNewsReview());
    await loadHistory();
    renderSemanticStatus();
    toast(job.status === "complete" ? "兩遍語義審完成。" : `語義審失敗：${job.error?.message || "未知原因"}`);
  } catch (error) {
    if (token !== state.semanticPollToken) return;
    state.semanticJob = { status: "failed", error: { code: "poll_failed", message: error.message } };
    renderSemanticStatus();
  }
}

async function startSemanticReview() {
  if (!state.currentDocument || !state.structure) return;
  state.semanticRun = null;
  state.semanticPollToken += 1;
  const token = state.semanticPollToken;
  const job = await api(`/api/documents/${encodeURIComponent(state.currentDocument.id)}/semantic-review`, {
    method: "POST",
    body: JSON.stringify({
      expected_revision_id: state.currentDocument.current_revision_id,
      expected_structure_hash: state.structure.structure_hash,
    }),
  });
  state.semanticJob = job;
  renderSemanticStatus();
  window.setTimeout(() => pollSemanticJob(job.id, token), 120);
}

async function runReview(options = {}) {
  if (!state.currentDocument) return;
  const triggers = [el["run-review"], el["run-writing-review"], el["rerun-review"]].filter(Boolean);
  triggers.forEach((item) => { item.disabled = true; });
  const originalLabels = triggers.map((item) => item.textContent);
  triggers.forEach((item) => { item.textContent = "啟動審稿…"; });
  try {
    if (!options.skipSave) await saveDocument("審稿前儲存");
    const automaticSourceNotes = refreshAutomaticSourceNotes();
    if (state.mode === "news") {
      await loadStructure();
      const preflight = await api(`/api/documents/${encodeURIComponent(state.currentDocument.id)}/preflight`, {
        method: "POST",
        body: JSON.stringify({
          expected_revision_id: state.currentDocument.current_revision_id,
          source_notes: automaticSourceNotes,
        }),
      });
      state.lastReviewRun = { id: null, output: preflight, provisional: true };
      state.lastNewsReviewRun = state.lastReviewRun;
      state.provisionalReview = !state.workflow?.source_readiness?.ready;
      state.reviewMode = true;
      renderReview(preflight);
      updateNewsStage();
      if (!options.skipSemantic) await startSemanticReview();
      if (!state.workflow?.source_readiness?.ready) {
        if (!options.quiet) toast("本機預檢已完成；語義審在背景執行，來源仍可逐項裁定。 ");
        return;
      }
    }
    const run = await api(`/api/documents/${encodeURIComponent(state.currentDocument.id)}/review`, {
      method: "POST",
      body: JSON.stringify({
        card_id: state.mode === "news" ? null : (state.selected.style_cards || null),
        template_id: state.mode === "news" ? null : (state.selected.templates || null),
        persona_id: state.mode === "news" ? "editor.news" : (state.selected.editors || null),
        chief_persona_id: state.mode === "news" ? "editor.baigui_editor_in_chief" : null,
        workflow_id: state.mode === "news" ? "chinatimes_newsroom" : "general",
        source_notes: state.mode === "news" ? automaticSourceNotes : "",
      }),
    });
    state.lastReviewRun = run;
    if (state.mode === "news") state.lastNewsReviewRun = run;
    else state.lastWritingReviewRun = run;
    state.provisionalReview = false;
    state.reviewMode = state.mode === "news";
    await loadWorkflow();
    renderReview(run.output);
    resetVideoPlan();
    updateVideoPlanAvailability();
    await loadHistory();
    if (!options.quiet) toast(state.mode === "news" ? "新聞預檢完成；兩遍語義審已在背景啟動。" : "本機規則審稿完成；尚未呼叫外部 AI。 ");
  } catch (error) {
    toast(error.message);
    if (error.status === 409) await openDocument(state.currentDocument.id).catch(() => {});
  } finally {
    triggers.forEach((item, index) => {
      item.disabled = false;
      item.textContent = originalLabels[index];
    });
    updateWriterControls();
  }
}

function resetReview() {
  state.reviewMode = false;
  state.provisionalReview = false;
  el["issue-count"].textContent = "0";
  clear(el["review-summary"]);
  el["review-summary"].className = "review-summary empty-state";
  const p = document.createElement("p");
  p.textContent = "執行審稿後，問題與統計會出現在這裡。";
  el["review-summary"].appendChild(p);
  clear(el["suggestion-list"]);
  clear(el["editor-suggestion-list"]);
  clear(el["chief-suggestion-list"]);
  clear(el["annotated-article"]);
  clear(el["review-title-anchor"]);
  clear(el["rewrite-actions"]);
  clear(el["rewrite-preview"]);
  clear(el["headline-candidates"]);
  clear(el["chief-recommendations"]);
  el["rewrite-preview"].hidden = true;
  updateNewsStage();
}

function resetVideoPlan() {
  if (!el["video-plan-output"]) return;
  clear(el["video-plan-output"]);
  el["video-plan-output"].className = "video-plan-output empty-state";
  const p = document.createElement("p");
  p.textContent = "完成來源裁定、人物卡修訂、修訂版重審與人工核准後即可建立。";
  el["video-plan-output"].appendChild(p);
  el["video-plan-status"].textContent = "尚未建立";
  el["video-plan-status"].className = "quality-badge boundary";
  el["video-plan-drawer"].hidden = true;
}

function updateVideoPlanAvailability() {
  if (!el["build-video-plan"]) return;
  const ready = state.mode === "news" && !state.dirty && Boolean(state.workflow?.video_plan_eligible);
  el["build-video-plan"].disabled = !ready;
  el["build-video-plan"].hidden = !state.workflow?.approved;
  if (ready && el["video-plan-status"].textContent === "尚未建立") {
    el["video-plan-status"].textContent = "已解鎖";
    el["video-plan-status"].className = "quality-badge transparent";
  }
}

function appendPlanDetails(container, titleText, lines, open = false) {
  const details = document.createElement("details");
  details.className = "video-plan-details";
  details.open = open;
  const summary = document.createElement("summary");
  summary.textContent = titleText;
  details.appendChild(summary);
  const list = document.createElement("ul");
  lines.filter(Boolean).forEach((line) => {
    const item = document.createElement("li");
    item.textContent = line;
    list.appendChild(item);
  });
  details.appendChild(list);
  container.appendChild(details);
}

function renderVideoPlan(output) {
  if (!output) return resetVideoPlan();
  el["video-plan-drawer"].hidden = false;
  el["video-plan-drawer"].open = true;
  clear(el["video-plan-output"]);
  el["video-plan-output"].className = "video-plan-output";
  el["video-plan-status"].textContent = output.status?.label || "企劃草稿";
  el["video-plan-status"].className = "quality-badge boundary";
  el["build-video-plan"].textContent = "重新建立影音企劃";

  const notice = document.createElement("p");
  notice.className = "video-plan-notice";
  notice.textContent = output.notice || "";
  el["video-plan-output"].appendChild(notice);

  const assessment = output.editorial_assessment || {};
  appendPlanDetails(el["video-plan-output"], "影音價值判斷", [assessment.decision, ...(assessment.questions || [])], true);

  const brief = output.production_brief || {};
  appendPlanDetails(
    el["video-plan-output"],
    "採訪任務",
    [brief.core_question, ...(brief.interview_targets || []).map((item) => `對象：${item}`), ...(brief.interview_questions || []).map((item) => `必問：${item}`)],
  );
  appendPlanDetails(
    el["video-plan-output"],
    "攝影與交付",
    [...(brief.must_shots || []).map((item) => `必拍：${item}`), ...(brief.b_roll || []).map((item) => `B-roll：${item}`), ...(brief.delivery_checklist || []).map((item) => `交付：${item}`)],
  );
  appendPlanDetails(
    el["video-plan-output"],
    "平台版本",
    (output.platform_versions || []).map((item) => `${item.name}｜${item.purpose}｜${(item.structure || []).join(" → ")}`),
  );
  appendPlanDetails(
    el["video-plan-output"],
    `風險與缺口（${output.risk_gate?.length || 0}）`,
    (output.risk_gate || []).map((item) => `${item.label}：${item.message}`),
    true,
  );
  appendPlanDetails(el["video-plan-output"], "下一步", output.next_steps || []);
}

async function buildVideoPlan() {
  if (!state.currentDocument || !state.lastReviewRun) return;
  el["build-video-plan"].disabled = true;
  el["build-video-plan"].textContent = "建立中…";
  try {
    const automaticSourceNotes = refreshAutomaticSourceNotes();
    const run = await api(`/api/documents/${encodeURIComponent(state.currentDocument.id)}/video-plan`, {
      method: "POST",
      body: JSON.stringify({
        source_notes: automaticSourceNotes,
        expected_revision_id: state.workflow?.revision_id,
        expected_review_run_id: state.workflow?.review_run_id,
        expected_approval_id: state.workflow?.approval?.id,
      }),
    });
    renderVideoPlan(run.output);
    await loadHistory();
    toast("核准後影音企劃草稿已建立；不會自動派工。");
  } catch (error) {
    toast(error.message);
    if (error.status === 409) await openDocument(state.currentDocument.id).catch(() => {});
  } finally {
    el["build-video-plan"].textContent = "重新建立影音企劃";
    updateVideoPlanAvailability();
  }
}

function buildRewritePreview(personaId) {
  const original = el.editor.value;
  let revised = original.replace(/\r\n?/g, "\n");
  revised = revised.replace(/[ \t]{2,}/g, " ");
  revised = revised.replace(/[!?！？]{2,}/g, (match) => match[0]);
  revised = revised.replace(/\n{3,}/g, "\n\n").trim();
  const changes = ["整理空白、標點與段落格式"];
  if (["editor.news", "editor.baigui_editor_in_chief"].includes(personaId)) {
    revised = revised.replace(/^(來源|資料來源|記者|編譯|攝影|採訪)\s*:/gm, "$1：");
    const paragraphs = [];
    revised.split("\n\n").forEach((paragraph) => {
      const compact = paragraph.trim();
      if (compact.length > 60) {
        const sentences = compact.match(/[^。！？!?]*[。！？!?]|[^。！？!?]+$/g)?.map((item) => item.trim()).filter(Boolean) || [];
        if (sentences.length > 1) {
          const midpoint = Math.max(1, Math.floor(sentences.length / 2));
          paragraphs.push(sentences.slice(0, midpoint).join(""), sentences.slice(midpoint).join(""));
          return;
        }
      }
      if (compact) paragraphs.push(compact);
    });
    revised = paragraphs.join("\n\n");
    changes.push("統一新聞署名格式，必要時拆分過長段落");
  }
  if (personaId === "editor.de_ai") {
    const replacements = {
      "賦能": "提供支援", "打造": "建立", "關鍵": "主要", "深入探討": "說明", "提升效率": "縮短處理時間",
      "全方位": "各項", "生態系": "協作體系", "價值創造": "實際成果", "核心競爭力": "主要優勢", "在這個快速變化的時代": "目前",
    };
    const replaced = [];
    Object.entries(replacements).forEach(([phrase, replacement]) => {
      if (!revised.includes(phrase)) return;
      revised = revised.split(phrase).join(replacement);
      replaced.push(phrase);
    });
    changes.push(replaced.length ? "替換空泛套語" : "未發現需替換的高風險套語");
  }
  return { original, revised, changes };
}

function previewPersonaRewrite(personaId, personaName) {
  const preview = buildRewritePreview(personaId);
  clear(el["rewrite-preview"]);
  el["rewrite-preview"].hidden = false;
  const head = document.createElement("div");
  head.className = "rewrite-preview-head";
  const title = document.createElement("strong");
  title.textContent = `${personaName}保守修訂預覽`;
  const changeSummary = document.createElement("span");
  changeSummary.textContent = preview.changes.join("；");
  head.append(title, changeSummary);
  const comparison = document.createElement("div");
  comparison.className = "rewrite-comparison";
  [["修訂前", preview.original], ["修訂後", preview.revised]].forEach(([label, content]) => {
    const pane = document.createElement("section");
    const heading = document.createElement("h4");
    heading.textContent = label;
    const body = document.createElement("pre");
    body.textContent = content;
    pane.append(heading, body);
    comparison.appendChild(pane);
  });
  const actions = document.createElement("div");
  actions.className = "rewrite-preview-actions";
  const accept = button("採納為新版本", "button primary", () => applyPersonaRewrite(personaId));
  accept.disabled = state.provisionalReview || !state.workflow?.source_readiness?.ready || !state.workflow?.review_current;
  const discard = button("放棄", "button secondary", () => {
    clear(el["rewrite-preview"]);
    el["rewrite-preview"].hidden = true;
  });
  actions.append(accept, discard);
  el["rewrite-preview"].append(head, comparison, actions);
  el["rewrite-preview"].scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function applyPersonaRewrite(personaId) {
  if (!state.currentDocument || !state.lastReviewRun) return;
  try {
    const payload = await api(`/api/documents/${encodeURIComponent(state.currentDocument.id)}/rewrite`, {
      method: "POST",
      body: JSON.stringify({
        persona_id: personaId,
        expected_revision_id: state.workflow?.revision_id,
        expected_review_run_id: state.workflow?.review_run_id,
      }),
    });
    state.currentDocument = payload.document;
    state.workflow = payload.workflow;
    state.lastReviewRun = null;
    state.lastNewsReviewRun = null;
    state.dirty = false;
    el["document-title"].value = payload.document.title;
    el.editor.value = payload.document.content;
    refreshAutomaticSourceNotes();
    updateSaveState("人物卡修訂版本已建立");
    updateDocumentMeta();
    resetReview();
    resetVideoPlan();
    renderSourceClues();
    updateNewsStage();
    updateVideoPlanAvailability();
    await refreshDocumentListOnly();
    await loadHistory();
    toast(`${payload.run.output.persona_name}已建立獨立修訂版本；請重新審稿。`);
  } catch (error) {
    toast(error.message);
    if (error.status === 409) await openDocument(state.currentDocument.id).catch(() => {});
  }
}

async function approveCurrentRevision() {
  if (!state.currentDocument || !state.lastReviewRun) return;
  const unlocked = state.workflow?.source_readiness?.ready && state.workflow?.rewritten && state.workflow?.review_current;
  if (!unlocked) {
    toast("核准條件尚未完成，請查看總覽列出的缺項。 ");
    return;
  }
  const semanticNotice = state.semanticRun?.status === "complete"
    ? "你已閱讀兩遍語義審；它不取代人工裁定。"
    : "語義審尚未完成或失敗；本次屬人工越權定稿。";
  if (!window.confirm(`確定以人工裁定核准目前版本？你已確認機器預檢與來源裁定。${semanticNotice}`)) return;
  const approveButton = el["approve-revision"];
  approveButton.disabled = true;
  try {
    const payload = await api(`/api/documents/${encodeURIComponent(state.currentDocument.id)}/approve`, {
      method: "POST",
      body: JSON.stringify({
        ack_machine: true,
        ack_sources: true,
        ack_final: true,
        expected_revision_id: state.workflow?.revision_id,
        expected_review_run_id: state.workflow?.review_run_id,
        note: "人工核准目前修訂版本",
      }),
    });
    state.workflow = payload.workflow;
    renderReview(state.lastReviewRun.output);
    updateNewsStage();
    updateVideoPlanAvailability();
    toast("目前修訂版本已人工核准，影音企劃已解鎖。");
  } catch (error) {
    approveButton.disabled = false;
    toast(error.message);
    if (error.status === 409) await openDocument(state.currentDocument.id).catch(() => {});
  }
}

const SOURCE_SUGGESTION_CATEGORIES = new Set(["消息來源", "同項回應", "數字查核"]);

function semanticEditorAnnotations() {
  if (state.semanticRun?.status !== "complete") return [];
  return (state.semanticRun.output?.editor?.annotations || []).map((item) => ({
    ...item,
    category: item.function,
    semanticRole: "editor",
  }));
}

function semanticChiefAnnotations() {
  if (state.semanticRun?.status !== "complete") return [];
  return (state.semanticRun.output?.chief?.annotations || []).map((item) => ({
    ...item,
    category: item.function,
    semanticRole: "chief",
  }));
}

function allReviewAnnotations(output) {
  return [...(output.suggestions || []), ...semanticEditorAnnotations(), ...semanticChiefAnnotations()];
}

function suggestionTone(item) {
  if (SOURCE_SUGGESTION_CATEGORIES.has(item.category)) return "source";
  return item.severity === "warning" ? "warning" : "note";
}

function focusSuggestion(id) {
  const card = document.getElementById(`suggestion-${id}`);
  if (!card) return;
  const section = card.closest("details");
  if (section) section.open = true;
  card.scrollIntoView({ behavior: "smooth", block: "center" });
  card.focus({ preventScroll: true });
  card.classList.remove("card-flash");
  requestAnimationFrame(() => card.classList.add("card-flash"));
  window.setTimeout(() => card.classList.remove("card-flash"), 1300);
}

function jumpToAnnotation(id) {
  const markers = [...el["annotated-article"].querySelectorAll("[data-suggestion-ids]")];
  const target = markers.find((item) => item.dataset.suggestionIds.split(" ").includes(id))
    || el["review-title-anchor"].querySelector(`[data-suggestion-id="${id}"]`);
  if (!target) return;
  target.scrollIntoView({ behavior: "smooth", block: "center" });
  target.classList.remove("annotation-flash");
  requestAnimationFrame(() => target.classList.add("annotation-flash"));
  window.setTimeout(() => target.classList.remove("annotation-flash"), 1300);
}

function appendAnnotatedText(container, content, start, end, suggestions) {
  const activeSuggestions = suggestions.filter((item) => item.start < end && item.end > start);
  const boundaries = [...new Set([
    start,
    end,
    ...activeSuggestions.flatMap((item) => [Math.max(start, item.start), Math.min(end, item.end)]),
  ])].sort((a, b) => a - b);
  for (let index = 0; index < boundaries.length - 1; index += 1) {
    const partStart = boundaries[index];
    const partEnd = boundaries[index + 1];
    if (partEnd <= partStart) continue;
    const text = content.slice(partStart, partEnd);
    const active = activeSuggestions.filter((item) => item.start <= partStart && item.end >= partEnd);
    if (!active.length) {
      container.appendChild(document.createTextNode(text));
      continue;
    }
    const marker = document.createElement("mark");
    const tone = active.some((item) => SOURCE_SUGGESTION_CATEGORIES.has(item.category))
      ? "source"
      : active.some((item) => item.severity === "warning") ? "warning" : "note";
    marker.className = `article-annotation ${tone}`;
    marker.dataset.suggestionIds = active.map((item) => item.id).join(" ");
    marker.tabIndex = 0;
    marker.setAttribute("role", "button");
    marker.setAttribute("aria-label", `查看批註：${active.map((item) => item.category).join("、")}`);
    marker.textContent = text;
    marker.addEventListener("click", () => focusSuggestion(active[0].id));
    marker.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      focusSuggestion(active[0].id);
    });
    container.appendChild(marker);
  }
}

async function saveStructureType(blockId, type, select) {
  if (!state.currentDocument || !state.structure) return;
  select.disabled = true;
  try {
    state.structure = await api(`/api/documents/${encodeURIComponent(state.currentDocument.id)}/structure`, {
      method: "POST",
      body: JSON.stringify({
        expected_revision_id: state.currentDocument.current_revision_id,
        overrides: [{ id: blockId, type }],
      }),
    });
    state.semanticPollToken += 1;
    state.semanticJob = null;
    if (state.semanticRun) {
      state.semanticRun = {
        status: "stale",
        output: { error: { code: "structure_changed", message: "段落結構已人工改判，請重新語義審。" } },
      };
    }
    await loadWorkflow();
    renderStructurePreview(state.structure);
    renderReview(state.lastReviewRun?.output || buildProvisionalNewsReview());
    renderSemanticStatus();
    await loadHistory();
    toast("結構改判已綁定目前版本儲存。 ");
  } catch (error) {
    toast(error.message);
    if (error.status === 409) await openDocument(state.currentDocument.id).catch(() => {});
  } finally {
    select.disabled = false;
  }
}

function renderAnnotatedArticle(output) {
  clear(el["annotated-article"]);
  clear(el["review-title-anchor"]);
  const content = el.editor.value;
  const suggestions = allReviewAnnotations(output);
  const anchored = suggestions.filter((item) => Number.isInteger(item.start) && Number.isInteger(item.end) && item.start >= 0 && item.end > item.start && item.end <= content.length);
  const unanchored = suggestions.filter((item) => !anchored.includes(item));

  el["review-title-anchor"].hidden = !unanchored.length;
  unanchored.forEach((item) => {
    const note = button(`${item.category}：${item.message}`, `title-annotation ${suggestionTone(item)}`, () => focusSuggestion(item.id));
    note.dataset.suggestionId = item.id;
    el["review-title-anchor"].appendChild(note);
  });

  const structure = state.structure?.blocks || [];
  if (structure.length) {
    structure.forEach((block) => {
      const section = document.createElement("section");
      section.className = `structure-block structure-${block.type}`;
      section.dataset.blockId = block.id;
      const head = document.createElement("div");
      head.className = "structure-block-head";
      const select = document.createElement("select");
      select.className = "structure-type-select";
      select.setAttribute("aria-label", `改判「${block.text.slice(0, 20)}」的段落結構`);
      [
        ["main_title", "主標"], ["subtitle", "副標"], ["lead", "前言"],
        ["subheading", "小標"], ["body", "正文段"],
      ].forEach(([value, label]) => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = label;
        option.selected = value === block.type;
        select.appendChild(option);
      });
      select.addEventListener("change", () => saveStructureType(block.id, select.value, select));
      const origin = document.createElement("span");
      origin.className = "structure-origin";
      origin.textContent = block.source === "human" ? "人工改判" : "自動偵測";
      head.append(select, origin);
      const body = document.createElement("div");
      body.className = "structure-block-text";
      appendAnnotatedText(body, content, block.start, block.end, anchored);
      section.append(head, body);
      el["annotated-article"].appendChild(section);
    });
  } else if (content) {
    appendAnnotatedText(el["annotated-article"], content, 0, content.length, anchored);
  } else {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "稿件正文為空。";
    el["annotated-article"].appendChild(empty);
  }
}

function createSuggestionCard(item) {
  const card = document.createElement("article");
  card.id = `suggestion-${item.id}`;
  const functionClass = {
    "正確性": "correctness", "可讀性": "readability", "文筆": "prose",
    "下標": "headline", "結構": "structure", "指錯": "challenge",
    "糾漏": "omission", "建議": "recommendation",
  }[item.function] || "local";
  card.className = `annotation-card ${suggestionTone(item)} function-${functionClass}`;
  card.tabIndex = -1;
  const head = document.createElement("div");
  head.className = "annotation-card-head";
  const category = document.createElement("span");
  category.className = `category-badge function-badge ${functionClass}`;
  category.textContent = item.category;
  const severity = document.createElement("span");
  severity.className = "severity-label";
  severity.textContent = item.severity === "warning" ? "需處理" : "提醒";
  head.append(category, severity);
  const message = document.createElement("p");
  message.textContent = item.message;
  card.append(head, message);
  if (item.excerpt) {
    const quote = document.createElement("blockquote");
    quote.textContent = item.excerpt;
    card.appendChild(quote);
  }
  if (item.questioned_annotation_id) {
    card.appendChild(button("質疑對象：查看編輯批註", "text-button questioned-link", () => focusSuggestion(item.questioned_annotation_id)));
  }
  card.appendChild(button("跳到原文", "text-button jump-to-source", () => jumpToAnnotation(item.id)));
  return card;
}

async function adoptHeadlineCandidate(candidateId) {
  if (!state.currentDocument || state.semanticRun?.status !== "complete") return;
  try {
    const payload = await api(`/api/documents/${encodeURIComponent(state.currentDocument.id)}/semantic-title`, {
      method: "POST",
      body: JSON.stringify({
        expected_revision_id: state.currentDocument.current_revision_id,
        semantic_run_id: state.semanticRun.id,
        candidate_id: candidateId,
      }),
    });
    await openDocument(payload.document.id);
    toast("候選主標已採用並建立新版本；舊版與原稿仍保留。 ");
  } catch (error) {
    toast(error.message);
    if (error.status === 409) await openDocument(state.currentDocument.id).catch(() => {});
  }
}

function renderHeadlineCandidates() {
  clear(el["headline-candidates"]);
  const candidates = state.semanticRun?.status === "complete"
    ? (state.semanticRun.output?.editor?.headline_candidates || [])
    : [];
  if (!candidates.length) return;
  const heading = document.createElement("strong");
  heading.textContent = "下標候選";
  el["headline-candidates"].appendChild(heading);
  candidates.forEach((candidate) => {
    const card = document.createElement("article");
    card.className = "headline-candidate";
    const title = document.createElement("h4");
    title.textContent = candidate.main_title;
    const subtitle = document.createElement("p");
    subtitle.textContent = candidate.subtitle || "（不設副標）";
    const angle = document.createElement("small");
    angle.textContent = `取向：${candidate.angle}`;
    card.append(title, subtitle, angle, button("採用", "button secondary", () => adoptHeadlineCandidate(candidate.id)));
    el["headline-candidates"].appendChild(card);
  });
}

function renderChiefRecommendations() {
  clear(el["chief-recommendations"]);
  if (state.semanticRun?.status !== "complete") return;
  const chief = state.semanticRun.output?.chief || {};
  [
    ["更好的標", chief.headline_recommendation],
    ["更好的導言", chief.lead_recommendation],
    ["更好的切角", chief.angle_recommendation],
  ].forEach(([label, item]) => {
    if (!item) return;
    const card = document.createElement("article");
    card.className = "chief-recommendation";
    const title = document.createElement("strong");
    title.textContent = label;
    const text = document.createElement("p");
    text.textContent = item.text;
    const reason = document.createElement("small");
    reason.textContent = item.reason;
    card.append(title, text, reason);
    el["chief-recommendations"].appendChild(card);
  });
}

function renderCaseOverview(output) {
  const summary = output.summary || {};
  el["overview-warning"].textContent = `${summary.warnings || 0} 警告`;
  el["overview-note"].textContent = `${summary.notes || 0} 提醒`;
  const approved = Boolean(state.workflow?.approved);
  const semanticComplete = state.semanticRun?.status === "complete";
  const approvalReady = Boolean(state.workflow?.source_readiness?.ready && state.workflow?.rewritten && state.workflow?.review_current && !state.provisionalReview);
  el["overview-stage"].textContent = approved
    ? `人工已核准・${semanticComplete ? "語義審完成" : "語義審未完成"}`
    : state.provisionalReview
      ? "本機預檢・等待來源裁定"
      : state.workflow?.rewritten ? "修訂版重審完成・待人工核准" : "初審完成・待建立修訂版本";
  el["approve-revision"].textContent = approved ? "已人工核准" : "人工核准";
  el["approve-revision"].disabled = approved || !approvalReady;
  clear(el["approval-blockers"]);
  let blockers = [];
  if (approved) {
    blockers = [`核准綁定版本 ${shortHash(state.workflow?.revision_id)}；再次改稿會自動失效。`];
  } else if (state.provisionalReview) {
    blockers = [...(state.workflow?.source_readiness?.blockers || []), "來源完成後才會建立正式審稿紀錄並解鎖人物卡修訂。"];
  } else if (!approvalReady) {
    blockers = (state.workflow?.blockers || []).filter((item) => item !== "目前版本尚未人工核准");
  } else {
    blockers = [`核准條件已齊；核准前請再次確認機器預檢、來源裁定與${semanticComplete ? "語義審批註" : "語義審未完成邊界"}。`];
  }
  blockers.forEach((text) => {
    const item = document.createElement("li");
    item.textContent = text;
    el["approval-blockers"].appendChild(item);
  });
  updateVideoPlanAvailability();
}

function renderRewriteActions(output) {
  clear(el["rewrite-actions"]);
  const personas = (output.rewrite_personas || []).filter((persona) => ["editor.news", "editor.de_ai"].includes(persona.id));
  personas.forEach((persona) => {
    const action = button(
      persona.id === "editor.news" ? "套用編輯保守修訂" : "套用去AI味修訂",
      persona.id === "editor.news" ? "button primary" : "button secondary",
      () => previewPersonaRewrite(persona.id, persona.name),
    );
    action.disabled = state.provisionalReview || !state.workflow?.source_readiness?.ready || !state.workflow?.review_current;
    action.title = action.disabled ? "完成來源裁定與正式審稿後才可預覽修訂" : persona.reason;
    el["rewrite-actions"].appendChild(action);
  });
  const note = document.createElement("p");
  note.className = "rewrite-boundary";
  note.textContent = "先預覽前後差異；只有按下「採納為新版本」才會呼叫人物卡修訂 API。";
  el["rewrite-actions"].appendChild(note);
}

function renderNewsReview(output) {
  const suggestions = output.suggestions || [];
  const localEditorSuggestions = suggestions.filter((item) => !SOURCE_SUGGESTION_CATEGORIES.has(item.category));
  const semanticEditor = semanticEditorAnnotations();
  const editorSuggestions = state.editorFunctionFilter === "all"
    ? [...localEditorSuggestions, ...semanticEditor]
    : semanticEditor.filter((item) => item.function === state.editorFunctionFilter);
  const chiefSuggestions = [
    ...suggestions.filter((item) => SOURCE_SUGGESTION_CATEGORIES.has(item.category)),
    ...semanticChiefAnnotations(),
  ];
  el["editor-issue-count"].textContent = String(editorSuggestions.length);
  el["chief-issue-count"].textContent = String((state.workflow?.sources?.length || 0) + chiefSuggestions.length);
  clear(el["editor-suggestion-list"]);
  clear(el["chief-suggestion-list"]);
  editorSuggestions.forEach((item) => el["editor-suggestion-list"].appendChild(createSuggestionCard(item)));
  chiefSuggestions.forEach((item) => el["chief-suggestion-list"].appendChild(createSuggestionCard(item)));
  if (!editorSuggestions.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "本機規則未發現編輯部結構問題；這不代表總編審核已通過。";
    el["editor-suggestion-list"].appendChild(empty);
  }
  renderHeadlineCandidates();
  renderChiefRecommendations();
  renderRewriteActions(output);
  renderSourceClues();
  renderAnnotatedArticle(output);
  renderCaseOverview(output);
  renderSemanticStatus();
  state.reviewMode = true;
  updateNewsStage();
}

function renderWritingReview(output, historical = false) {
  el["issue-count"].textContent = String(output.summary?.total || 0);
  clear(el["review-summary"]);
  el["review-summary"].className = "review-summary";
  const headline = document.createElement("p");
  const strong = document.createElement("strong");
  strong.textContent = `${historical ? "歷史報告・唯讀" : "本機檢查完成"}｜${output.summary?.warnings || 0} 項警示、${output.summary?.notes || 0} 項提醒`;
  headline.append(strong, document.createElement("br"), document.createTextNode(output.notice || ""));
  el["review-summary"].appendChild(headline);
  clear(el["suggestion-list"]);
  (output.suggestions || []).forEach((item) => {
    const card = document.createElement("article");
    card.className = `suggestion ${item.severity}`;
    const title = document.createElement("strong");
    title.textContent = item.category;
    const message = document.createElement("p");
    message.textContent = item.message;
    card.append(title, message);
    el["suggestion-list"].appendChild(card);
  });
}

function renderReview(output, options = {}) {
  if (!output) return resetReview();
  if (options.interactive === false) {
    el["results-section"].hidden = false;
    el["case-overview"].hidden = true;
    el["news-review-content"].hidden = true;
    el["writing-review-content"].hidden = false;
    renderWritingReview(output, true);
    return;
  }
  if (state.mode === "news") renderNewsReview(output);
  else renderWritingReview(output);
}

async function loadHistory() {
  if (!state.currentDocument) return;
  const suffix = state.historyMode === "revisions" ? "revisions" : "runs";
  const payload = await api(`/api/documents/${encodeURIComponent(state.currentDocument.id)}/${suffix}`);
  renderHistory(payload.items);
}

function renderHistory(items) {
  clear(el["history-list"]);
  if (state.historyMode === "revisions") {
    items.forEach((item) => {
      const card = document.createElement("article");
      card.className = "history-card";
      const title = document.createElement("strong");
      title.textContent = `${item.is_original ? "原稿" : item.is_current ? "目前版本" : "歷史版本"}・${item.actor}`;
      const meta = document.createElement("p");
      meta.textContent = `${item.note}｜${shortDate(item.created_at)}｜${shortHash(item.content_hash)}`;
      const actions = document.createElement("div");
      actions.className = "history-actions";
      actions.appendChild(button("與目前比較", "text-button", () => showDiff(item.id)));
      if (!item.is_current) actions.appendChild(button("還原成新版本", "text-button", () => restoreRevision(item.id)));
      card.append(title, meta, actions);
      el["history-list"].appendChild(card);
    });
  } else {
    items.forEach((item) => {
      const card = document.createElement("article");
      card.className = "history-card";
      const title = document.createElement("strong");
      title.textContent = item.action === "video_plan_draft" ? "影音企劃草稿・不可派工" : `${item.engine}・${item.status}`;
      const meta = document.createElement("p");
      meta.textContent = item.action === "video_plan_draft"
        ? `${shortDate(item.created_at)}｜${item.output.risk_gate?.length || 0} 項風險與缺口`
        : `${shortDate(item.created_at)}｜${item.persona_id || "未指定人物"}｜${item.output.summary?.total || 0} 項`;
      const show = item.action === "video_plan_draft"
        ? () => renderVideoPlan(item.output)
        : () => renderReview(item.output, { interactive: false, run: item });
      card.append(title, meta, button("查看結果", "text-button", show));
      el["history-list"].appendChild(card);
    });
  }
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.textContent = "尚無紀錄。";
    el["history-list"].appendChild(empty);
  }
}

async function showDiff(fromId) {
  if (!state.currentDocument) return;
  const toId = state.currentDocument.current_revision_id;
  const payload = await api(`/api/documents/${encodeURIComponent(state.currentDocument.id)}/diff?from=${encodeURIComponent(fromId)}&to=${encodeURIComponent(toId)}`);
  el["diff-output"].textContent = payload.diff || "兩個版本沒有文字差異。";
  el["diff-dialog"].showModal();
  el["diff-output"].focus();
}

async function restoreRevision(revisionId) {
  if (!window.confirm("要把這個歷史版本還原成新的目前版本嗎？原稿與既有版本都會保留。")) return;
  const restored = await api(`/api/documents/${encodeURIComponent(state.currentDocument.id)}/restore`, {
    method: "POST",
    body: JSON.stringify({ revision_id: revisionId }),
  });
  state.currentDocument = restored;
  el["document-title"].value = restored.title;
  el.editor.value = restored.content;
  state.importedSourceHint = "";
  refreshAutomaticSourceNotes();
  state.dirty = false;
  updateDocumentMeta();
  state.lastReviewRun = null;
  resetReview();
  resetVideoPlan();
  await refreshDocumentListOnly();
  await loadHistory();
  await loadWorkflow();
  toast("已還原為新版本，舊版本仍保留。 ");
}

function mediaSegments() {
  const transcript = state.media?.transcript;
  return Array.isArray(transcript) ? transcript : (transcript?.segments || []);
}

function mediaScores() {
  const highlights = state.media?.highlights;
  if (Array.isArray(highlights)) return highlights;
  return highlights?.segments || highlights?.scores || [];
}

function mediaPlans() {
  const highlights = state.media?.highlights;
  const plans = state.media?.plans || highlights?.plans || highlights?.clip_plans || [];
  return Array.isArray(plans) ? plans : Object.values(plans || {});
}

function mediaClips() {
  const clips = state.media?.clips;
  return Array.isArray(clips) ? clips : (clips?.clips || clips?.items || []);
}

function mediaSeconds(value) {
  const seconds = Math.max(0, Number(value) || 0);
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = (seconds % 60).toFixed(1).padStart(4, "0");
  return hours ? `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${rest}` : `${String(minutes).padStart(2, "0")}:${rest}`;
}

function mediaRunning(action) {
  return ["queued", "running"].includes(state.mediaJobs[action]?.status);
}

function renderMediaState() {
  if (!el["media-workspace"]) return;
  const media = state.media;
  const segments = mediaSegments();
  const scores = mediaScores();
  const plans = mediaPlans();
  const clips = mediaClips();
  const source = media?.source || {};
  const mediaId = media?.id || media?.media_id;
  const hasSource = Boolean(mediaId && (
    source.filename || media?.source_filename || media?.original_filename || media?.filename || media?.source_path || media?.status === "uploaded"
  ));

  el["media-file-status"].textContent = hasSource ? "原片已落盤" : "尚未上傳";
  el["media-file-status"].className = `status-pill ${hasSource ? "status-ok" : "status-waiting"}`;
  clear(el["media-source-card"]);
  if (!hasSource) {
    el["media-source-card"].className = "media-source-card empty-state";
    const p = document.createElement("p");
    p.textContent = "檔案會分塊串流落盤，不會整支載入伺服器記憶體。";
    el["media-source-card"].appendChild(p);
  } else {
    el["media-source-card"].className = "media-source-card";
    const meta = document.createElement("div");
    meta.className = "media-source-meta";
    const rows = [
      ["檔名", source.original_filename || source.filename || media.original_filename || media.source_filename || media.filename || "原始媒體"],
      ["大小", `${Number(source.size_bytes || media.size_bytes || 0).toLocaleString("zh-TW")} bytes`],
      ["媒體編號", mediaId],
      ["SHA-256", shortHash(source.sha256 || media.sha256 || media.provenance?.source_sha256)],
    ];
    rows.forEach(([label, value]) => {
      const span = document.createElement("span");
      const strong = document.createElement("strong");
      strong.textContent = `${label}：`;
      span.append(strong, document.createTextNode(String(value || "—")));
      meta.appendChild(span);
    });
    el["media-source-card"].appendChild(meta);
  }

  el["media-transcribe"].disabled = !hasSource || mediaRunning("transcribe");
  el["media-highlight"].disabled = !segments.length || mediaRunning("highlights");
  el["media-cut"].disabled = !plans.length || mediaRunning("clips");
  el["media-save-document"].disabled = !segments.length;
  el["media-transcribe-status"].textContent = mediaRunning("transcribe") ? "背景工作執行中" : segments.length ? `已完成 ${segments.length} 段` : "等待手動啟動";
  el["media-highlight-status"].textContent = mediaRunning("highlights") ? "語義引擎判斷中" : scores.length ? `已完成 ${scores.length} 段與 ${plans.length} 種剪法` : "等待逐字稿";
  el["media-clip-status"].textContent = mediaRunning("clips") ? "ffmpeg 重編碼中" : clips.length ? `已產生 ${clips.length} 支` : "等待切片計畫";

  clear(el["media-transcript"]);
  el["media-transcript"].className = segments.length ? "media-transcript" : "media-transcript empty-state";
  if (!segments.length) {
    const p = document.createElement("p"); p.textContent = "完成轉寫後顯示每段時間碼與文字。"; el["media-transcript"].appendChild(p);
  } else segments.forEach((segment) => {
    const row = document.createElement("article"); row.className = "media-segment";
    const time = document.createElement("span"); time.className = "media-timecode"; time.textContent = `${mediaSeconds(segment.start)}–${mediaSeconds(segment.end)}`;
    const textNode = document.createElement("p"); textNode.textContent = segment.text || "（空白段落）";
    row.append(time, textNode); el["media-transcript"].appendChild(row);
  });

  clear(el["media-highlights"]);
  el["media-highlights"].className = scores.length ? "media-highlights" : "media-highlights empty-state";
  if (!scores.length) {
    const p = document.createElement("p"); p.textContent = "完成判斷後顯示有料、搞笑、爭議分數與理由。"; el["media-highlights"].appendChild(p);
  } else scores.forEach((score, index) => {
    const row = document.createElement("article"); row.className = "media-score-row";
    const head = document.createElement("div"); head.className = "media-score-badges";
    [["有料", score.有料 ?? score.material_score ?? score.scores?.material], ["搞笑", score.搞笑 ?? score.humor_score ?? score.scores?.humor], ["爭議", score.爭議 ?? score.controversy_score ?? score.scores?.controversy]].forEach(([label, value]) => {
      const badge = document.createElement("span"); badge.className = `media-score-badge ${Number(value) >= 7 ? "high" : ""}`; badge.textContent = `${label} ${Number(value) || 0}/10`; head.appendChild(badge);
    });
    const time = document.createElement("span"); time.className = "media-timecode"; time.textContent = `${mediaSeconds(score.start ?? segments[index]?.start)}–${mediaSeconds(score.end ?? segments[index]?.end)}`;
    const reason = document.createElement("p"); reason.textContent = score.reason || score.理由 || "引擎未提供理由，已留警示。";
    row.append(time, head, reason); el["media-highlights"].appendChild(row);
  });

  clear(el["media-plans"]);
  el["media-plans"].className = plans.length ? "media-plan-grid" : "media-plan-grid empty-state";
  if (!plans.length) {
    const p = document.createElement("p"); p.textContent = "精華判斷完成後，會分別規劃 10、30、60、90 秒剪法。"; el["media-plans"].appendChild(p);
  } else plans.forEach((plan) => {
    const card = document.createElement("article"); card.className = "media-plan-card";
    const title = document.createElement("h3"); title.textContent = plan.suggested_title || plan.title || `${plan.target_duration || plan.target_seconds || "短"}秒剪法`;
    const duration = document.createElement("p"); duration.textContent = `目標 ${plan.target_duration || plan.target_seconds || "—"} 秒・實際 ${Number(plan.actual_duration ?? plan.actual_seconds ?? 0).toFixed(1)} 秒`;
    const reason = document.createElement("p"); reason.textContent = plan.reason || plan.selection_reason || "依本機規則貼齊段落邊界。";
    const list = document.createElement("ul");
    (plan.ranges || plan.timecodes || plan.segments || []).forEach((range) => {
      const item = document.createElement("li"); item.textContent = `${mediaSeconds(range.start)}–${mediaSeconds(range.end)}`; list.appendChild(item);
    });
    card.append(title, duration, reason, list); el["media-plans"].appendChild(card);
  });

  clear(el["media-clips"]);
  el["media-clips"].className = clips.length ? "media-clip-grid" : "media-clip-grid empty-state";
  if (!clips.length) {
    const p = document.createElement("p"); p.textContent = "切短片完成後可在此播放或下載。"; el["media-clips"].appendChild(p);
  } else clips.forEach((clip) => {
    const filename = clip.filename || String(clip.path || "").split(/[\\/]/).pop();
    const url = clip.url || `/api/media/${encodeURIComponent(mediaId)}/clips/${encodeURIComponent(filename)}`;
    const card = document.createElement("article"); card.className = "media-clip-card";
    const title = document.createElement("h3"); title.textContent = clip.suggested_title || clip.title || filename || "短片";
    const video = document.createElement("video"); video.controls = true; video.preload = "metadata"; video.src = url;
    const duration = document.createElement("p"); duration.textContent = `ffprobe 實測 ${Number(clip.measured_seconds ?? clip.duration ?? clip.actual_duration ?? 0).toFixed(2)} 秒`;
    const label = document.createElement("p"); label.className = "media-sensory-label"; label.textContent = "吸不吸引人，感官未判，待人工裁定";
    const actions = document.createElement("div"); actions.className = "media-clip-actions";
    const download = document.createElement("a"); download.href = url; download.download = filename || "clip.mp4"; download.textContent = "下載短片"; actions.appendChild(download);
    card.append(title, video, duration, label, actions); el["media-clips"].appendChild(card);
  });

  const traces = media ? {
    media_id: mediaId,
    source: source.provenance || media.provenance,
    transcript: state.media.transcript?.provenance,
    highlights: state.media.highlights?.provenance,
    clips: state.media.clips?.provenance,
    warnings: [state.media.transcript?.warnings, state.media.highlights?.warnings, state.media.clips?.warnings].flat().filter(Boolean),
  } : null;
  el["media-provenance-output"].textContent = traces ? JSON.stringify(traces, null, 2) : "尚無執行紀錄。";
}

async function uploadMedia(event) {
  const file = event.target.files?.[0];
  if (!file) return;
  el["media-error"].hidden = true;
  el["media-file-status"].textContent = "串流上傳中";
  el["media-file-status"].className = "status-pill status-waiting";
  try {
    const response = await fetch(`/api/media/upload?filename=${encodeURIComponent(file.name)}`, {
      method: "POST",
      headers: { "Content-Type": file.type || "application/octet-stream" },
      body: file,
    });
    const payload = await response.json().catch(() => ({}));
    if (response.status === 401) window.location.replace("/login");
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    state.media = payload.media || payload;
    state.mediaJobs = { transcribe: null, highlights: null, clips: null };
    renderMediaState();
    toast("原片已串流落盤；尚未自動執行轉寫。 ");
  } catch (error) {
    el["media-file-status"].textContent = "上傳失敗";
    el["media-file-status"].className = "status-pill status-error";
    el["media-error"].textContent = `上傳失敗：${error.message}`;
    el["media-error"].hidden = false;
  } finally {
    event.target.value = "";
  }
}

async function refreshMedia() {
  const mediaId = state.media?.id || state.media?.media_id;
  if (!mediaId) return;
  state.media = await api(`/api/media/${encodeURIComponent(mediaId)}`);
  renderMediaState();
}

async function pollMediaJob(action, jobId, token) {
  if (token !== state.mediaPollTokens[action]) return;
  try {
    const job = await api(`/api/media/jobs/${encodeURIComponent(jobId)}`);
    if (token !== state.mediaPollTokens[action]) return;
    state.mediaJobs[action] = job;
    renderMediaState();
    if (["queued", "running"].includes(job.status)) {
      window.setTimeout(() => pollMediaJob(action, jobId, token), 900);
      return;
    }
    await refreshMedia();
    if (job.status === "failed") throw new Error(`${job.error?.code ? `${job.error.code}：` : ""}${job.error?.message || "未知原因"}`);
    toast(({ transcribe: "逐字稿完成；尚未自動判斷精華。", highlights: "精華判斷與切片計畫完成；尚未自動切片。", clips: "短片已完成重編碼與長度驗證。" })[action]);
  } catch (error) {
    if (token !== state.mediaPollTokens[action]) return;
    el["media-error"].textContent = `${({ transcribe: "轉寫", highlights: "精華判斷", clips: "切片" })[action]}失敗：${error.message}`;
    el["media-error"].hidden = false;
    renderMediaState();
  }
}

async function startMediaJob(action) {
  const mediaId = state.media?.id || state.media?.media_id;
  if (!mediaId || mediaRunning(action)) return;
  el["media-error"].hidden = true;
  try {
    const job = await api(`/api/media/${encodeURIComponent(mediaId)}/${action}`, { method: "POST", body: "{}" });
    state.mediaJobs[action] = job;
    state.mediaPollTokens[action] += 1;
    const token = state.mediaPollTokens[action];
    renderMediaState();
    window.setTimeout(() => pollMediaJob(action, job.id, token), 120);
  } catch (error) {
    el["media-error"].textContent = `無法啟動工作：${error.message}`;
    el["media-error"].hidden = false;
  }
}

async function saveTranscriptAsDocument() {
  const segments = mediaSegments();
  if (!segments.length) return;
  const sourceName = state.media?.source?.original_filename || state.media?.original_filename || "影音逐字稿";
  try {
    const mediaId = state.media?.id || state.media?.media_id;
    const payload = await api(`/api/media/${encodeURIComponent(mediaId)}/document`, {
      method: "POST",
      body: JSON.stringify({ title: `${sourceName}・逐字稿` }),
    });
    await loadDocuments(payload.document.id);
    setMode("news");
    toast("逐字稿已建立為普通文件，可接續使用寫手席。 ");
  } catch (error) {
    el["media-error"].textContent = `存成文件失敗：${error.message}`;
    el["media-error"].hidden = false;
  }
}

function wireEvents() {
  el["library-toggle"].addEventListener("click", () => setLibraryOpen(!state.libraryOpen));
  el["library-close"].addEventListener("click", () => setLibraryOpen(false));
  el["library-backdrop"].addEventListener("click", () => setLibraryOpen(false));
  el["history-toggle"].addEventListener("click", () => setHistoryOpen(!state.historyOpen));
  el["news-mode"].addEventListener("click", () => setMode("news"));
  el["writing-mode"].addEventListener("click", () => setMode("writing"));
  el["media-mode"].addEventListener("click", () => setMode("media"));
  el["media-upload"].addEventListener("change", uploadMedia);
  el["media-transcribe"].addEventListener("click", () => startMediaJob("transcribe"));
  el["media-highlight"].addEventListener("click", () => startMediaJob("highlights"));
  el["media-cut"].addEventListener("click", () => startMediaJob("clips"));
  el["media-save-document"].addEventListener("click", saveTranscriptAsDocument);
  el["article-upload"].addEventListener("change", importArticle);
  el["recommend-styles"].addEventListener("click", recommendStyles);
  el["toggle-advanced"].addEventListener("click", toggleAdvanced);
  el["new-document"].addEventListener("click", createDocument);
  el["save-document"].addEventListener("click", () => saveDocument().catch((error) => toast(error.message)));
  el["run-review"].addEventListener("click", runReview);
  el["run-writing-review"].addEventListener("click", runReview);
  el["rerun-review"].addEventListener("click", runReview);
  el["start-writer"].addEventListener("click", startWriter);
  el["skip-writer"].addEventListener("click", skipWriter);
  el["writer-card-select"].addEventListener("change", () => {
    state.selectedWriterCard = el["writer-card-select"].value;
    renderWriterRecommendation();
    updateWriterControls();
  });
  el["back-to-edit"].addEventListener("click", () => {
    state.reviewMode = false;
    updateNewsStage();
    el.editor.focus();
  });
  el["editor-function-filters"].querySelectorAll(".function-filter").forEach((filter) => {
    filter.addEventListener("click", () => {
      state.editorFunctionFilter = filter.dataset.function;
      el["editor-function-filters"].querySelectorAll(".function-filter").forEach((item) => {
        item.classList.toggle("active", item === filter);
      });
      renderReview(state.lastReviewRun?.output || buildProvisionalNewsReview());
    });
  });
  el["approve-revision"].addEventListener("click", approveCurrentRevision);
  el["build-video-plan"].addEventListener("click", buildVideoPlan);
  el.editor.addEventListener("input", () => {
    state.importedSourceHint = "";
    refreshAutomaticSourceNotes();
    markDirty();
  });
  el["document-title"].addEventListener("input", markDirty);
  el["catalog-search"].addEventListener("input", renderCatalog);
  el["training-filter"].addEventListener("change", () => {
    state.trainingFilter = el["training-filter"].value;
    el["catalog-search"].value = "";
    renderCatalog();
  });
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((item) => {
        item.classList.toggle("active", item === tab);
        item.setAttribute("aria-selected", item === tab ? "true" : "false");
      });
      state.activeKind = tab.dataset.kind;
      el["catalog-search"].value = "";
      renderCatalog();
    });
  });
  document.querySelectorAll(".history-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".history-tab").forEach((item) => {
        item.classList.toggle("active", item === tab);
        item.setAttribute("aria-selected", item === tab ? "true" : "false");
      });
      state.historyMode = tab.dataset.history;
      loadHistory().catch((error) => toast(error.message));
    });
  });
  [[el["style-select"], "style_cards"], [el["template-select"], "templates"], [el["persona-select"], "editors"]].forEach(([select, kind]) => {
    select.addEventListener("change", () => {
      state.selected[kind] = select.value;
      renderCatalog();
      if (select.value) selectAsset(kind, select.value);
    });
  });
  el["close-diff"].addEventListener("click", () => el["diff-dialog"].close());
  window.addEventListener("beforeunload", (event) => {
    if (!state.dirty) return;
    event.preventDefault();
    event.returnValue = "";
  });
}

async function init() {
  bindElements();
  wireEvents();
  try {
    await Promise.all([loadHealth(), loadCatalogs(), loadWriterCards("", false)]);
    setMode("news");
    await loadDocuments();
    if (state.selected.style_cards) await selectAsset("style_cards", state.selected.style_cards);
  } catch (error) {
    el["runtime-status"].textContent = "啟動失敗";
    el["runtime-status"].className = "status-pill status-error";
    toast(error.message);
  }
}

document.addEventListener("DOMContentLoaded", init);
