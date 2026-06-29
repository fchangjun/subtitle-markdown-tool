const urlInput = document.querySelector("#urlInput");
const runButton = document.querySelector("#runButton");
const clearButton = document.querySelector("#clearButton");
const progressLabel = document.querySelector("#progressLabel");
const progressCount = document.querySelector("#progressCount");
const progressBar = document.querySelector("#progressBar");
const summaryGrid = document.querySelector("#summaryGrid");
const messages = document.querySelector("#messages");
const resultList = document.querySelector("#resultList");
const preview = document.querySelector("#preview");
const jobLabel = document.querySelector("#jobLabel");
const downloadAll = document.querySelector("#downloadAll");
const openPreview = document.querySelector("#openPreview");
const serverState = document.querySelector("#serverState span:last-child");
const historyList = document.querySelector("#historyList");
const refreshHistoryButton = document.querySelector("#refreshHistoryButton");

let activeJobId = null;
let pollTimer = null;
let selectedFileUrl = null;
const ACTIVE_JOB_KEY = "subtitleMarkdownTool.activeJobId";
const RECENT_JOBS_KEY = "subtitleMarkdownTool.recentJobIds";

const statusText = {
  queued: "排队中",
  running: "处理中",
  completed: "已完成",
  completed_with_errors: "部分完成",
};

function setBusy(isBusy) {
  runButton.disabled = isBusy;
  serverState.textContent = isBusy ? "处理中" : "就绪";
}

function setMessage(text, type = "info") {
  messages.innerHTML = "";
  if (!text) return;
  const item = document.createElement("div");
  item.className = `message ${type}`;
  item.textContent = text;
  messages.appendChild(item);
}

function recentJobIds() {
  try {
    return JSON.parse(localStorage.getItem(RECENT_JOBS_KEY) || "[]");
  } catch {
    return [];
  }
}

function rememberJob(jobId) {
  if (!jobId) return;
  const ids = [jobId, ...recentJobIds().filter((id) => id !== jobId)].slice(0, 20);
  localStorage.setItem(ACTIVE_JOB_KEY, jobId);
  localStorage.setItem(RECENT_JOBS_KEY, JSON.stringify(ids));
}

function appendError(text) {
  const item = document.createElement("div");
  item.className = "message error";
  item.textContent = text;
  messages.appendChild(item);
}

function updateProgress(job) {
  const total = job.total || 0;
  const current = job.current || 0;
  const pct = total > 0 ? Math.round((current / total) * 100) : 0;
  progressLabel.textContent = statusText[job.status] || "等待任务";
  progressCount.textContent = `${current} / ${total}`;
  progressBar.style.width = `${pct}%`;
  const summary = job.summary || {};
  summaryGrid.innerHTML = `
    <span>成功 ${summary.success_count || 0}</span>
    <span>失败 ${summary.error_count || 0}</span>
    <span>平均耗时 ${formatSeconds(summary.average_elapsed_seconds)}</span>
    <span>平均重试 ${Number(summary.average_retry_count || 0).toFixed(2)}</span>
    <span>并发 ${summary.worker_count || 0}</span>
  `;
}

function formatSize(bytes) {
  if (!bytes) return "0 KB";
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

function formatSeconds(value) {
  const number = Number(value || 0);
  if (number >= 60) return `${Math.floor(number / 60)}m ${Math.round(number % 60)}s`;
  return `${number.toFixed(number >= 10 ? 0 : 1)}s`;
}

function fileUrl(jobId, filename) {
  return `/api/jobs/${encodeURIComponent(jobId)}/files/${encodeURIComponent(filename)}`;
}

async function loadPreview(jobId, filename) {
  selectedFileUrl = fileUrl(jobId, filename);
  openPreview.href = selectedFileUrl;
  openPreview.classList.remove("disabled");
  const response = await fetch(selectedFileUrl);
  if (!response.ok) {
    preview.textContent = "无法读取文件";
    return;
  }
  preview.textContent = await response.text();
}

function startPolling(jobId) {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(() => pollJob(jobId), 2500);
}

function renderFiles(job) {
  if (!job.files.length) {
    resultList.innerHTML = '<div class="empty-state">处理完成后显示 Markdown 文件</div>';
    preview.textContent = "选择一个结果查看 Markdown";
    openPreview.classList.add("disabled");
    return;
  }

  resultList.innerHTML = "";
  job.files.forEach((file, index) => {
    const item = document.createElement("article");
    item.className = "result-item";

    const info = document.createElement("div");
    const title = document.createElement("div");
    title.className = "result-title";
    title.textContent = file.title || file.filename;

    const meta = document.createElement("div");
    meta.className = "result-meta";
    const sourceClass = file.subtitle_source === "manual" ? "manual" : "automatic";
    const sourceText = file.subtitle_source === "manual" ? "人工字幕" : "自动字幕";
    meta.innerHTML = `
      <span class="pill">${file.filename}</span>
      <span class="pill ${sourceClass}">${sourceText}:${file.subtitle_language || "unknown"}</span>
      <span class="pill">${formatSize(file.size_bytes)}</span>
      <span class="pill">耗时 ${formatSeconds(file.elapsed_seconds)}</span>
      <span class="pill">重试 ${file.retry_count || 0}</span>
      ${file.from_cache ? '<span class="pill manual">历史缓存</span>' : ""}
    `;
    info.append(title, meta);

    const actions = document.createElement("div");
    actions.className = "file-actions";
    const view = document.createElement("button");
    view.className = "file-link";
    view.type = "button";
    view.textContent = "预览";
    view.addEventListener("click", () => loadPreview(job.id, file.filename));

    const download = document.createElement("a");
    download.className = "file-link";
    download.href = fileUrl(job.id, file.filename);
    download.download = file.filename;
    download.textContent = "下载";
    actions.append(view, download);

    item.append(info, actions);
    resultList.appendChild(item);

    if (index === 0 && !selectedFileUrl) {
      loadPreview(job.id, file.filename);
    }
  });
}

function renderJob(job) {
  activeJobId = job.id;
  rememberJob(job.id);
  jobLabel.textContent = `任务 ${job.id}`;
  updateProgress(job);
  renderFiles(job);

  messages.innerHTML = "";
  job.errors.forEach((error) => {
    appendError(
      `${error.url}：${error.message}（耗时 ${formatSeconds(error.elapsed_seconds)}，重试 ${error.retry_count || 0}）`
    );
  });

  const hasDownload = job.files.length > 0 || job.status === "completed_with_errors" || job.status === "completed";
  if (hasDownload) {
    downloadAll.href = `/api/jobs/${encodeURIComponent(job.id)}/download`;
    downloadAll.classList.remove("disabled");
    downloadAll.setAttribute("aria-disabled", "false");
  }

  const done = job.status === "completed" || job.status === "completed_with_errors";
  setBusy(!done);
  if (done && pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  loadHistory();
}

async function pollJob(jobId) {
  try {
    const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const job = await response.json();
    renderJob(job);
    return job;
  } catch (error) {
    setBusy(false);
    setMessage(`任务状态读取失败：${error.message}`, "error");
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }
}

async function createJob() {
  const urls = urlInput.value.trim();
  if (!urls) {
    setMessage("请输入视频 URL", "error");
    return;
  }

  selectedFileUrl = null;
  setBusy(true);
  setMessage("任务已提交");
  resultList.innerHTML = '<div class="empty-state">正在处理</div>';
  preview.textContent = "等待 Markdown 输出";
  openPreview.classList.add("disabled");
  downloadAll.classList.add("disabled");
  downloadAll.setAttribute("aria-disabled", "true");
  progressBar.style.width = "0%";
  progressCount.textContent = "0 / 0";
  progressLabel.textContent = "排队中";
  summaryGrid.innerHTML = `
    <span>成功 0</span>
    <span>失败 0</span>
    <span>平均耗时 0s</span>
    <span>平均重试 0</span>
    <span>并发 0</span>
  `;

  try {
    const response = await fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ urls }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    renderJob(payload);
    startPolling(payload.id);
    pollJob(payload.id);
  } catch (error) {
    setBusy(false);
    setMessage(error.message, "error");
  }
}

async function restoreJob(jobId) {
  selectedFileUrl = null;
  openPreview.classList.add("disabled");
  preview.textContent = "正在恢复历史任务";
  const job = await pollJob(jobId);
  const done = job && (job.status === "completed" || job.status === "completed_with_errors");
  if (job && !done) startPolling(jobId);
}

function renderHistory(jobs) {
  if (!jobs.length) {
    historyList.innerHTML = '<div class="history-empty">暂无历史任务</div>';
    return;
  }
  historyList.innerHTML = "";
  jobs.forEach((job) => {
    const item = document.createElement("button");
    item.className = "history-item";
    item.type = "button";
    const summary = job.summary || {};
    item.innerHTML = `
      <span class="history-title">${job.id} · ${statusText[job.status] || job.status}</span>
      <span class="history-meta">${job.current || 0}/${job.total || 0} · 成功 ${summary.success_count || 0} · 失败 ${summary.error_count || 0} · 缓存 ${summary.cache_hit_count || 0}</span>
    `;
    item.addEventListener("click", () => restoreJob(job.id));
    historyList.appendChild(item);
  });
}

async function loadHistory() {
  try {
    const response = await fetch("/api/jobs");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    renderHistory(payload.jobs || []);
  } catch {
    const ids = recentJobIds();
    renderHistory(ids.map((id) => ({ id, status: "unknown", current: 0, total: 0, summary: {} })));
  }
}

async function init() {
  await loadHistory();
  const active = localStorage.getItem(ACTIVE_JOB_KEY);
  if (active) {
    restoreJob(active);
  }
}

runButton.addEventListener("click", createJob);
refreshHistoryButton.addEventListener("click", loadHistory);

clearButton.addEventListener("click", () => {
  urlInput.value = "";
  urlInput.focus();
});

init();
