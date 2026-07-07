const urlInput = document.querySelector("#urlInput");
const runButton = document.querySelector("#runButton");
const runButtonText = document.querySelector("#runButton span:last-child");
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
const stopJobButton = document.querySelector("#stopJobButton");
const openPreview = document.querySelector("#openPreview");
const serverState = document.querySelector("#serverState span:last-child");
const historyList = document.querySelector("#historyList");
const refreshHistoryButton = document.querySelector("#refreshHistoryButton");
const rangeInput = document.querySelector("#rangeInput");
const refreshDiscoveryHistory = document.querySelector("#refreshDiscoveryHistory");
const discoverySummary = document.querySelector("#discoverySummary");
const activeDiscoveryLabel = document.querySelector("#activeDiscoveryLabel");
const discoveryFilterInput = document.querySelector("#discoveryFilterInput");
const discoveryHistoryList = document.querySelector("#discoveryHistoryList");
const discoveryList = document.querySelector("#discoveryList");
const discoveryActions = document.querySelector("#discoveryActions");
const discoveryStatus = document.querySelector("#discoveryStatus");
const discoveryProgress = document.querySelector("#discoveryProgress");
const discoveryProgressLabel = document.querySelector("#discoveryProgressLabel");
const discoveryProgressCount = document.querySelector("#discoveryProgressCount");
const discoveryProgressBar = document.querySelector("#discoveryProgressBar");
const discoveryProgressMessage = document.querySelector("#discoveryProgressMessage");
const discoverySourceProgress = document.querySelector("#discoverySourceProgress");
const selectAllDiscovery = document.querySelector("#selectAllDiscovery");
const clearDiscoverySelection = document.querySelector("#clearDiscoverySelection");
const importDiscovery = document.querySelector("#importDiscovery");
const followInput = document.querySelector("#followInput");
const addChannelsButton = document.querySelector("#addChannelsButton");
const selectAllChannelsButton = document.querySelector("#selectAllChannelsButton");
const invertChannelsButton = document.querySelector("#invertChannelsButton");
const refreshSelectedChannelsButton = document.querySelector("#refreshSelectedChannelsButton");
const refreshChannels = document.querySelector("#refreshChannels");
const refreshChannelButton = document.querySelector("#refreshChannelButton");
const channelList = document.querySelector("#channelList");
const channelDetail = document.querySelector("#channelDetail");
const importQueueSummary = document.querySelector("#importQueueSummary");
const settingsToggleButton = document.querySelector("#settingsToggleButton");
const settingsPanel = document.querySelector("#settingsPanel");
const settingsBackdrop = document.querySelector("#settingsBackdrop");
const settingsCloseButton = document.querySelector("#settingsCloseButton");
const cancelSettingsButton = document.querySelector("#cancelSettingsButton");
const saveSettingsButton = document.querySelector("#saveSettingsButton");
const settingsProviderState = document.querySelector("#settingsProviderState");
const discoveryProviderSetting = document.querySelector("#discoveryProviderSetting");
const youtubeApiKeyInput = document.querySelector("#youtubeApiKeyInput");
const youtubeApiKeyState = document.querySelector("#youtubeApiKeyState");
const discoveryCheckLimitInput = document.querySelector("#discoveryCheckLimitInput");
const youtubeApiTimeoutInput = document.querySelector("#youtubeApiTimeoutInput");
const youtubeApiAttemptsInput = document.querySelector("#youtubeApiAttemptsInput");
const apiFallbackInput = document.querySelector("#apiFallbackInput");
const settingsMessage = document.querySelector("#settingsMessage");
const pageTabs = Array.from(document.querySelectorAll(".tab-button"));
const pages = {
  search: document.querySelector("#searchPage"),
  extract: document.querySelector("#extractPage"),
};

let activeJobId = null;
let pollTimer = null;
let discoveryTaskPollTimer = null;
let selectedFileUrl = null;
let activeDiscoveryRecord = null;
let discoveredVideos = [];
let selectedDiscoveryUrls = new Set();
let followedChannels = [];
let activeChannel = null;
let selectedChannelIds = new Set();
let channelSelectionInitialized = false;
let currentSettings = null;
let discoveryBusy = false;
let pendingArchiveTitle = "";
let submittingJob = false;
let activeJobStatus = "";
const ACTIVE_JOB_KEY = "subtitleMarkdownTool.activeJobId";
const RECENT_JOBS_KEY = "subtitleMarkdownTool.recentJobIds";
const ACTIVE_DISCOVERY_KEY = "subtitleMarkdownTool.activeDiscoveryId";
const DISCOVERY_SELECTION_PREFIX = "subtitleMarkdownTool.discoverySelection.";

const statusText = {
  queued: "排队中",
  running: "处理中",
  completed: "已完成",
  completed_with_errors: "部分完成",
  failed: "失败",
  cancelled: "已停止",
};

function isTerminalJobStatus(status) {
  return ["completed", "completed_with_errors", "failed", "cancelled"].includes(status);
}

function switchPage(pageName) {
  Object.entries(pages).forEach(([name, page]) => {
    page.classList.toggle("active", name === pageName);
  });
  pageTabs.forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.page === pageName);
  });
}

function switchContentView(viewName) {
  const showingResults = viewName === "results";
  discoveryList.hidden = !showingResults;
  channelDetail.hidden = showingResults;
  discoveryActions.hidden = !showingResults;
  discoveryStatus.hidden = !showingResults;
  if (!showingResults && !activeChannel) {
    activeDiscoveryLabel.textContent = "选择一个关注频道查看内容";
  } else if (showingResults && activeDiscoveryRecord) {
    activeDiscoveryLabel.textContent = `扫描 ${activeDiscoveryRecord.id} · 更新于 ${formatDateTime(activeDiscoveryRecord.updated_at)}`;
  } else if (showingResults) {
    activeDiscoveryLabel.textContent = "尚未选择扫描历史";
  }
}

function setBusy(isBusy) {
  serverState.textContent = isBusy ? "处理中" : "就绪";
  updateRunButtonState();
}

function updateRunButtonState() {
  runButton.disabled = submittingJob;
  if (submittingJob) {
    runButtonText.textContent = "添加中";
  } else if (activeJobStatus && !isTerminalJobStatus(activeJobStatus)) {
    runButtonText.textContent = "添加到队列";
  } else {
    runButtonText.textContent = "开始提取";
  }
}

function setMessage(text, type = "info") {
  messages.innerHTML = "";
  if (!text) return;
  appendMessage(text, type);
}

function discoveryProviderLabel(provider) {
  if (provider === "youtube_api") return "YouTube API";
  if (provider === "yt_dlp") return "yt-dlp";
  return provider || "未设置";
}

function setSettingsOpen(isOpen) {
  settingsPanel.hidden = !isOpen;
  settingsBackdrop.hidden = !isOpen;
  if (isOpen) {
    settingsMessage.textContent = "";
    settingsMessage.className = "settings-message";
    youtubeApiKeyInput.value = "";
    loadSettings();
  }
}

function renderSettings(settings) {
  currentSettings = settings;
  discoveryProviderSetting.value = settings.discovery_provider || "yt_dlp";
  discoveryCheckLimitInput.value = String(settings.discovery_check_limit || 120);
  youtubeApiTimeoutInput.value = Math.round(Number(settings.youtube_api_timeout_seconds || 20));
  youtubeApiAttemptsInput.value = Number(settings.youtube_api_max_attempts || 3);
  apiFallbackInput.checked = Boolean(settings.api_fallback_to_ytdlp);

  const keyText = settings.youtube_api_key_configured
    ? `已配置 ${settings.youtube_api_key_masked || ""}`.trim()
    : "未配置";
  youtubeApiKeyState.textContent = keyText;
  youtubeApiKeyInput.placeholder = settings.youtube_api_key_configured ? "留空则保持当前 key" : "AIza...";
  settingsProviderState.textContent = `${discoveryProviderLabel(settings.discovery_provider)} · ${keyText}`;
}

async function loadSettings() {
  try {
    const response = await fetch("/api/settings");
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    renderSettings(payload);
    return payload;
  } catch (error) {
    settingsProviderState.textContent = "设置读取失败";
    settingsMessage.textContent = error.message;
    settingsMessage.className = "settings-message error";
  }
}

async function saveSettings() {
  saveSettingsButton.disabled = true;
  settingsMessage.textContent = "正在保存";
  settingsMessage.className = "settings-message";
  const apiKey = youtubeApiKeyInput.value.trim();
  const payload = {
    discovery_provider: discoveryProviderSetting.value,
    api_fallback_to_ytdlp: apiFallbackInput.checked,
    discovery_check_limit: Number(discoveryCheckLimitInput.value || 120),
    youtube_api_timeout_seconds: Number(youtubeApiTimeoutInput.value || 20),
    youtube_api_max_attempts: Number(youtubeApiAttemptsInput.value || 3),
  };
  if (apiKey) payload.youtube_api_key = apiKey;

  try {
    const response = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
    youtubeApiKeyInput.value = "";
    renderSettings(result);
    settingsMessage.textContent = "已保存";
    settingsMessage.className = "settings-message success";
  } catch (error) {
    settingsMessage.textContent = error.message;
    settingsMessage.className = "settings-message error";
  } finally {
    saveSettingsButton.disabled = false;
  }
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

function appendMessage(text, type = "info") {
  const item = document.createElement("div");
  item.className = `message ${type}`;
  item.textContent = text;
  messages.appendChild(item);
}

function appendError(text) {
  appendMessage(text, "error");
}

function cooldownRemaining(job) {
  if (!job.cooldown_until) return 0;
  return Math.max(0, job.cooldown_until - Date.now() / 1000);
}

function updateProgress(job) {
  const total = job.total || 0;
  const current = job.current || 0;
  const pct = total > 0 ? Math.round((current / total) * 100) : 0;
  const coolingDown = cooldownRemaining(job) > 0;
  progressLabel.textContent =
    job.cancel_requested && !isTerminalJobStatus(job.status)
      ? "停止中"
      : coolingDown
      ? "自动冷却"
      : statusText[job.status] || "等待任务";
  progressCount.textContent = `${current} / ${total}`;
  progressBar.style.width = `${pct}%`;
  const summary = job.summary || {};
  summaryGrid.innerHTML = `
    <span>成功 ${summary.success_count || 0}</span>
    <span>失败 ${summary.error_count || 0}</span>
    <span>视频总长 ${formatSummaryVideoTotal(summary)}</span>
    <span>视频均长 ${formatSummaryVideoAverage(summary)}</span>
    <span>平均提取 ${formatSeconds(summary.average_elapsed_seconds)}</span>
    <span>提取总耗时 ${formatSeconds(summary.total_item_elapsed_seconds)}</span>
    <span>平均重试 ${Number(summary.average_retry_count || 0).toFixed(2)}</span>
    <span>分批 ${summary.auto_batch_size || "关闭"}</span>
  `;
}

function formatSize(bytes) {
  if (!bytes) return "0 KB";
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

function formatSeconds(value) {
  const number = Number(value || 0);
  if (number >= 3600) {
    const hours = Math.floor(number / 3600);
    const minutes = Math.round((number % 3600) / 60);
    return minutes ? `${hours}h ${minutes}m` : `${hours}h`;
  }
  if (number >= 60) return `${Math.floor(number / 60)}m ${Math.round(number % 60)}s`;
  return `${number.toFixed(number >= 10 ? 0 : 1)}s`;
}

function hasNumber(value) {
  return value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value));
}

function formatOptionalSeconds(value) {
  return hasNumber(value) ? formatSeconds(value) : "未知";
}

function formatSummaryVideoTotal(summary) {
  if (summary.video_duration_known_count) return formatSeconds(summary.total_video_duration_seconds);
  return summary.success_count ? "未知" : "0s";
}

function formatSummaryVideoAverage(summary) {
  if (summary.video_duration_known_count) return formatSeconds(summary.average_video_duration_seconds);
  return summary.success_count ? "未知" : "0s";
}

function formatViews(value) {
  if (!hasNumber(value)) return "播放未知";
  return `${new Intl.NumberFormat("zh-CN").format(Number(value))} 次播放`;
}

function formatDateTime(seconds) {
  if (!seconds) return "未知时间";
  return new Date(Number(seconds) * 1000).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function channelTitle(channel) {
  const rawTitle = String(channel.title || "");
  if (rawTitle && !rawTitle.startsWith("http")) {
    return rawTitle.replace(/\s+-\s+Videos$/i, "");
  }
  const value = String(channel.source_url || channel.listing_url || "");
  const match = value.match(/youtube\.com\/(@[^/?#]+|channel\/[^/?#]+|c\/[^/?#]+|user\/[^/?#]+)/i);
  if (match) return match[1].replace(/^channel\//, "频道 ").replace(/^c\//, "").replace(/^user\//, "");
  return value || channel.id;
}

function channelIntro(channel) {
  const description = String(channel.description || "").trim();
  if (description) return description;
  const handle = channelTitle(channel);
  if (channel.video_count) return `${handle} 已记录 ${channel.video_count} 个候选视频。`;
  return "尚未获取频道简介，更新频道后会尝试补全。";
}

function parseUrlText(text) {
  return text
    .split(/[\s,]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function updateImportQueueSummary() {
  const count = parseUrlText(urlInput.value).length;
  if (!count) {
    importQueueSummary.textContent = "待提取队列为空";
    return;
  }
  const sourceText = pendingArchiveTitle ? ` · 来自 ${pendingArchiveTitle}` : "";
  importQueueSummary.textContent = `待提取 ${count} 个视频${sourceText}`;
}

function setChannelImportQueue(channel) {
  const items = channel.items || [];
  pendingArchiveTitle = channelTitle(channel);
  urlInput.value = items.map((item) => item.url).join("\n");
  updateImportQueueSummary();
}

function renderChannelList(channels) {
  followedChannels = channels || [];
  if (!followedChannels.length) {
    channelList.innerHTML = '<div class="history-empty">暂无关注频道</div>';
    selectedChannelIds = new Set();
    channelSelectionInitialized = false;
    updateChannelSelectionSummary();
    return;
  }

  if (!channelSelectionInitialized) {
    selectedChannelIds = new Set(followedChannels.map((channel) => channel.id));
    channelSelectionInitialized = true;
  } else {
    const validIds = new Set(followedChannels.map((channel) => channel.id));
    selectedChannelIds = new Set(Array.from(selectedChannelIds).filter((id) => validIds.has(id)));
  }

  channelList.innerHTML = "";
  followedChannels.forEach((channel) => {
    const item = document.createElement("article");
    item.className = "history-item channel-item";
    if (activeChannel && activeChannel.id === channel.id) item.classList.add("active");
    const detail = [
      `${channel.video_count || 0} 个内容`,
      `${channel.scanned_count || 0} 个已扫`,
      `${channel.unknown_date_count || 0} 个日期未知已跳过`,
      formatDateTime(channel.last_discovered_at || channel.updated_at),
    ];

    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = selectedChannelIds.has(channel.id);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) selectedChannelIds.add(channel.id);
      else selectedChannelIds.delete(channel.id);
      updateChannelSelectionSummary();
    });

    const button = document.createElement("button");
    button.className = "channel-open";
    button.type = "button";
    button.innerHTML = `
      <span class="history-title">${channelTitle(channel)}</span>
      <span class="channel-intro">${channelIntro(channel)}</span>
      <span class="history-meta">${detail.join(" · ")}</span>
    `;
    button.addEventListener("click", () => loadChannel(channel.id));
    item.append(checkbox, button);
    channelList.appendChild(item);
  });
  updateChannelSelectionSummary();
}

function selectedChannels() {
  return followedChannels.filter((channel) => selectedChannelIds.has(channel.id));
}

function updateChannelSelectionSummary() {
  const count = selectedChannels().length;
  const hasChannels = followedChannels.length > 0;
  selectAllChannelsButton.disabled = discoveryBusy || !hasChannels;
  invertChannelsButton.disabled = discoveryBusy || !hasChannels;
  refreshSelectedChannelsButton.disabled = discoveryBusy || !count;
  refreshChannelButton.disabled = discoveryBusy || !activeChannel;
  refreshSelectedChannelsButton.textContent = count ? `更新选中 ${count}` : "更新选中";
  if (discoveryBusy) {
    refreshSelectedChannelsButton.textContent = "更新中";
    return;
  }
}

function selectAllFollowedChannels() {
  selectedChannelIds = new Set(followedChannels.map((channel) => channel.id));
  renderChannelList(followedChannels);
}

function invertFollowedChannelSelection() {
  const nextSelection = new Set();
  followedChannels.forEach((channel) => {
    if (!selectedChannelIds.has(channel.id)) nextSelection.add(channel.id);
  });
  selectedChannelIds = nextSelection;
  renderChannelList(followedChannels);
}

function renderChannelDetail(channel) {
  activeChannel = channel;
  refreshChannelButton.disabled = false;
  renderChannelList(followedChannels);
  channelDetail.innerHTML = "";
  activeDiscoveryLabel.textContent = `频道内容 · ${channelTitle(channel)}`;
  switchContentView("channel");

  const head = document.createElement("div");
  head.className = "channel-detail-head";
  const title = document.createElement("div");
  title.className = "channel-detail-title";
  title.textContent = channelTitle(channel);
  const intro = document.createElement("div");
  intro.className = "channel-detail-intro";
  intro.textContent = channelIntro(channel);
  const meta = document.createElement("div");
  meta.className = "history-meta";
  meta.textContent = `${channel.video_count || 0} 个内容 · ${channel.known_date_count || 0} 个日期明确 · ${channel.unknown_date_count || 0} 个日期未知已跳过 · 更新 ${formatDateTime(channel.last_discovered_at || channel.updated_at)}`;
  const copy = document.createElement("div");
  copy.append(title, intro, meta);
  head.appendChild(copy);

  const actions = document.createElement("div");
  actions.className = "channel-detail-actions";
  const refreshButton = document.createElement("button");
  refreshButton.className = "file-link";
  refreshButton.type = "button";
  refreshButton.textContent = "更新当前频道";
  refreshButton.addEventListener("click", refreshActiveChannel);
  actions.appendChild(refreshButton);

  const openLink = document.createElement("a");
  openLink.className = "file-link";
  openLink.href = channel.listing_url || channel.source_url || "#";
  openLink.target = "_blank";
  openLink.rel = "noreferrer";
  openLink.textContent = "打开频道";
  actions.appendChild(openLink);
  if (channel.items && channel.items.length) {
    const importButton = document.createElement("button");
    importButton.className = "file-link";
    importButton.type = "button";
    importButton.textContent = "导入本频道";
    importButton.addEventListener("click", () => {
      setChannelImportQueue(channel);
      switchPage("extract");
    });
    actions.appendChild(importButton);
  }
  head.appendChild(actions);
  channelDetail.appendChild(head);

  if (channel.last_error) {
    const error = document.createElement("div");
    error.className = "message error";
    error.textContent = channel.last_error;
    channelDetail.appendChild(error);
  }

  const items = channel.items || [];
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "history-empty";
    empty.textContent = "这个频道还没有内容记录，可以点击“更新当前频道”。";
    channelDetail.appendChild(empty);
    return;
  }

  const list = document.createElement("div");
  list.className = "channel-video-list";
  items.forEach((video) => {
    const row = document.createElement("a");
    row.className = "channel-video-item";
    row.href = video.url;
    row.target = "_blank";
    row.rel = "noreferrer";

    const copy = document.createElement("span");
    copy.className = "channel-video-copy";

    const title = document.createElement("span");
    title.className = "channel-video-title";
    title.textContent = video.title || video.url;

    const meta = document.createElement("span");
    meta.className = "history-meta";
    meta.textContent = `${video.publish_date || "日期未知"} · 视频 ${formatOptionalSeconds(video.duration_seconds)} · ${formatViews(video.view_count)}`;

    copy.append(title, meta);
    row.append(createVideoThumbnail(video, "channel-thumbnail"), copy);
    list.appendChild(row);
  });
  channelDetail.appendChild(list);
}

async function loadChannels() {
  try {
    const response = await fetch("/api/channels");
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    renderChannelList(payload.channels || []);
  } catch (error) {
    channelList.innerHTML = `<div class="history-empty">${error.message}</div>`;
  }
}

async function loadChannel(channelId) {
  try {
    const response = await fetch(`/api/channels/${encodeURIComponent(channelId)}`);
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    renderChannelDetail(payload);
  } catch (error) {
    channelDetail.innerHTML = `<div class="history-empty">${error.message}</div>`;
  }
}

async function addFollowedChannels() {
  const sources = followInput.value.trim();
  if (!sources) {
    channelList.innerHTML = '<div class="history-empty">请输入要关注的频道主页</div>';
    return;
  }

  addChannelsButton.disabled = true;
  try {
    const response = await fetch("/api/channels", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sources }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    followInput.value = "";
    (payload.added || []).forEach((channel) => selectedChannelIds.add(channel.id));
    renderChannelList(payload.channels || []);
    channelDetail.innerHTML = `<div class="history-empty">新增 ${payload.added_count || 0} 个，已存在 ${payload.existing_count || 0} 个</div>`;
  } catch (error) {
    channelDetail.innerHTML = `<div class="history-empty">${error.message}</div>`;
  } finally {
    addChannelsButton.disabled = false;
  }
}

async function refreshActiveChannel() {
  if (!activeChannel) return;
  setDiscoveryBusy(true);
  channelDetail.innerHTML = '<div class="history-empty">频道更新任务已提交，正在准备</div>';
  try {
    await createDiscoveryTask({
      channel_id: activeChannel.id,
      range: rangeInput.value.trim() || "1年",
    });
  } catch (error) {
    channelDetail.innerHTML = `<div class="history-empty">${error.message}</div>`;
    setDiscoveryBusy(false);
  }
}

function setDiscoveryBusy(isBusy, label = "查找视频") {
  discoveryBusy = isBusy;
  const selectedCount = selectedChannels().length;
  refreshChannelButton.disabled = isBusy || !activeChannel;
  refreshSelectedChannelsButton.disabled = isBusy || !selectedCount;
  selectAllChannelsButton.disabled = isBusy || !followedChannels.length;
  invertChannelsButton.disabled = isBusy || !followedChannels.length;
  if (isBusy) {
    refreshSelectedChannelsButton.textContent = "更新中";
  } else if (label) {
    updateChannelSelectionSummary();
  }
  serverState.textContent = isBusy ? "查找中" : "就绪";
}

function selectionKey(recordId) {
  return `${DISCOVERY_SELECTION_PREFIX}${recordId}`;
}

function saveDiscoverySelection() {
  if (!activeDiscoveryRecord) return;
  localStorage.setItem(selectionKey(activeDiscoveryRecord.id), JSON.stringify(Array.from(selectedDiscoveryUrls)));
  updateDiscoverySelectionSummary();
}

function loadDiscoverySelection(record) {
  try {
    const saved = JSON.parse(localStorage.getItem(selectionKey(record.id)) || "null");
    if (Array.isArray(saved)) return new Set(saved);
  } catch {
    return new Set((record.items || []).map((item) => item.url));
  }
  return new Set((record.items || []).map((item) => item.url));
}

function selectedDiscoveryVideos() {
  return discoveredVideos.filter((video) => selectedDiscoveryUrls.has(video.url));
}

function filteredDiscoveryVideos() {
  const query = discoveryFilterInput.value.trim().toLowerCase();
  if (!query) return discoveredVideos;
  return discoveredVideos.filter((video) =>
    [video.title, video.channel, video.url, video.source_title]
      .some((value) => String(value || "").toLowerCase().includes(query))
  );
}

function updateDiscoverySelectionSummary() {
  const selectedCount = selectedDiscoveryVideos().length;
  const totalCount = discoveredVideos.length;
  const visibleCount = filteredDiscoveryVideos().length;
  const summary = activeDiscoveryRecord?.summary || {};
  const notes = [];
  const providerText = providerSummaryText(summary.provider_counts || {});
  if (providerText) notes.push(providerText);
  if (summary.fallback_count) notes.push(`${summary.fallback_count} 个频道 API 降级`);
  if (summary.yt_dlp_capped_count) {
    const cap = summary.yt_dlp_cap || 1000;
    notes.push(`${summary.yt_dlp_capped_count} 个频道 yt-dlp 已按环境上限 ${cap} 条执行`);
  }
  if (summary.api_request_count) notes.push(`API ${summary.api_request_count} 次`);
  if (summary.yt_dlp_batch_count) {
    notes.push(`yt-dlp ${summary.yt_dlp_batch_count} 批，每批 ${summary.yt_dlp_batch_size || 100} 条`);
  }
  if (summary.stopped_by_cutoff_count) notes.push(`${summary.stopped_by_cutoff_count} 个频道遇到旧日期已提前停止`);
  if (summary.detail_lookup_count) {
    notes.push(`补日期 ${summary.detail_lookup_success_count || 0}/${summary.detail_lookup_count}`);
  }
  if (summary.unknown_date_count) notes.push(`${summary.unknown_date_count} 个日期未知已跳过`);
  if (summary.detail_lookup_limit_reached_count) notes.push(`${summary.detail_lookup_limit_reached_count} 个频道达到检查上限`);
  if (summary.detail_lookup_error_count) notes.push(`${summary.detail_lookup_error_count} 个补日期失败`);
  if (summary.limit_reached_count) notes.push(`${summary.limit_reached_count} 个频道达到上限`);
  if (summary.error_count) notes.push(`${summary.error_count} 个频道失败`);
  if (!totalCount) {
    discoverySummary.textContent = notes.length ? `候选 0 个 · ${notes.join(" · ")}` : "尚未发现视频";
    return;
  }
  const filterText = visibleCount === totalCount ? "" : `，当前显示 ${visibleCount} 个`;
  discoverySummary.textContent = `候选 ${totalCount} 个，已选 ${selectedCount} 个${filterText}${notes.length ? ` · ${notes.join(" · ")}` : ""}`;
}

function addDiscoveryPill(meta, text, className = "") {
  const item = document.createElement("span");
  item.className = `pill ${className}`.trim();
  item.textContent = text;
  meta.appendChild(item);
}

function providerSummaryText(providerCounts = {}) {
  const labels = {
    youtube_api: "YouTube API",
    yt_dlp: "yt-dlp",
    unknown: "未知来源",
  };
  return Object.entries(providerCounts)
    .filter(([, count]) => Number(count) > 0)
    .map(([provider, count]) => `${labels[provider] || provider} ${count}`)
    .join(" · ");
}

function normalizedSourceKey(value) {
  return String(value || "").trim().replace(/\/+$/, "").toLowerCase();
}

function followedChannelForSource(sourceUrl) {
  const sourceKey = normalizedSourceKey(sourceUrl);
  if (!sourceKey) return null;
  return followedChannels.find((channel) =>
    [channel.source_url, channel.listing_url]
      .map(normalizedSourceKey)
      .includes(sourceKey)
  ) || null;
}

function discoverySourceGroups(videos) {
  const groups = [];
  const byKey = new Map();
  videos.forEach((video) => {
    const sourceUrl = video.source_url || "";
    const key = normalizedSourceKey(sourceUrl) || `unknown:${video.source_title || video.channel || ""}`;
    let group = byKey.get(key);
    if (!group) {
      const channel = followedChannelForSource(sourceUrl);
      group = {
        key,
        sourceUrl,
        title: video.source_title || (channel ? channelTitle(channel) : "") || sourceUrl || "未知频道",
        channel,
        items: [],
      };
      byKey.set(key, group);
      groups.push(group);
    }
    group.items.push(video);
  });
  return groups;
}

function youtubeIdFromUrl(value) {
  try {
    const parsed = new URL(value);
    const host = parsed.hostname.toLowerCase();
    if (host.endsWith("youtu.be")) {
      return parsed.pathname.split("/").filter(Boolean)[0] || "";
    }
    if (host.includes("youtube.com")) {
      const watchId = parsed.searchParams.get("v");
      if (watchId) return watchId;
      const parts = parsed.pathname.split("/").filter(Boolean);
      const markerIndex = parts.findIndex((part) => ["shorts", "embed", "live"].includes(part));
      if (markerIndex >= 0) return parts[markerIndex + 1] || "";
    }
  } catch {
    return "";
  }
  return "";
}

function thumbnailUrlForVideo(video) {
  const explicitUrl = String(video.thumbnail_url || video.thumbnail || "").trim();
  if (explicitUrl) return explicitUrl;
  const videoId = youtubeIdFromUrl(video.url);
  return videoId ? `https://i.ytimg.com/vi/${videoId}/hqdefault.jpg` : "";
}

function createVideoThumbnail(video, className = "", asLink = false) {
  const thumbnail = document.createElement(asLink ? "a" : "span");
  thumbnail.className = `video-thumbnail ${className}`.trim();
  if (asLink) {
    thumbnail.href = video.url;
    thumbnail.target = "_blank";
    thumbnail.rel = "noreferrer";
  }

  const thumbnailUrl = thumbnailUrlForVideo(video);
  if (!thumbnailUrl) {
    thumbnail.classList.add("missing");
    return thumbnail;
  }

  const image = document.createElement("img");
  image.src = thumbnailUrl;
  image.alt = "";
  image.loading = "lazy";
  image.referrerPolicy = "no-referrer";
  image.addEventListener("error", () => {
    image.remove();
    thumbnail.classList.add("missing");
  });
  thumbnail.appendChild(image);
  return thumbnail;
}

function createDiscoveryItem(video) {
  const item = document.createElement("article");
  item.className = "discovery-item";

  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.checked = selectedDiscoveryUrls.has(video.url);
  checkbox.addEventListener("change", () => {
    if (checkbox.checked) selectedDiscoveryUrls.add(video.url);
    else selectedDiscoveryUrls.delete(video.url);
    saveDiscoverySelection();
  });

  const info = document.createElement("div");
  info.className = "discovery-info";
  const title = document.createElement("a");
  title.className = "discovery-title";
  title.href = video.url;
  title.target = "_blank";
  title.rel = "noreferrer";
  title.textContent = video.title || video.url;

  const meta = document.createElement("div");
  meta.className = "result-meta";
  addDiscoveryPill(meta, video.publish_date || "日期未知", video.date_known ? "manual" : "automatic");
  addDiscoveryPill(meta, `视频 ${formatOptionalSeconds(video.duration_seconds)}`);
  addDiscoveryPill(meta, formatViews(video.view_count));
  if (video.channel) addDiscoveryPill(meta, video.channel);

  info.append(title, meta);
  if (video.description) {
    const description = document.createElement("div");
    description.className = "discovery-description";
    description.textContent = video.description;
    info.appendChild(description);
  }

  item.append(checkbox, createVideoThumbnail(video, "discovery-thumbnail", true), info);
  return item;
}

function createDiscoveryGroup(group, showHeader) {
  const section = document.createElement("section");
  section.className = "discovery-group";
  if (showHeader) {
    const header = document.createElement("div");
    header.className = "discovery-group-head";

    const copy = document.createElement("div");
    const title = document.createElement("div");
    title.className = "discovery-group-title";
    title.textContent = group.title;
    const meta = document.createElement("div");
    meta.className = "history-meta";
    const selectedCount = group.items.filter((video) => selectedDiscoveryUrls.has(video.url)).length;
    meta.textContent = `${group.items.length} 个候选 · 已选 ${selectedCount} 个`;
    copy.append(title, meta);
    header.appendChild(copy);

    if (group.channel) {
      const openButton = document.createElement("button");
      openButton.className = "file-link";
      openButton.type = "button";
      openButton.textContent = "查看频道";
      openButton.addEventListener("click", () => loadChannel(group.channel.id));
      header.appendChild(openButton);
    }

    section.appendChild(header);
  }

  group.items.forEach((video) => {
    section.appendChild(createDiscoveryItem(video));
  });
  return section;
}

function renderDiscoveryCandidates() {
  discoveryList.innerHTML = "";
  const videos = filteredDiscoveryVideos();
  if (!videos.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = discoveredVideos.length ? "没有匹配当前筛选的视频" : "在左侧查找频道后显示候选视频";
    discoveryList.appendChild(empty);
    updateDiscoverySelectionSummary();
    return;
  }

  const groups = discoverySourceGroups(videos);
  const showHeaders = groups.length > 1 || (activeDiscoveryRecord?.sources || []).length > 1;
  groups.forEach((group) => {
    discoveryList.appendChild(createDiscoveryGroup(group, showHeaders));
  });
  updateDiscoverySelectionSummary();
}

function renderDiscoveryRecord(record, options = {}) {
  const activateResults = options.activateResults !== false;
  activeDiscoveryRecord = record;
  discoveredVideos = record.items || [];
  selectedDiscoveryUrls = loadDiscoverySelection(record);
  localStorage.setItem(ACTIVE_DISCOVERY_KEY, record.id);
  if (activateResults) {
    activeDiscoveryLabel.textContent = `扫描 ${record.id} · 更新于 ${formatDateTime(record.updated_at)}`;
  }
  discoveryFilterInput.value = "";
  renderDiscoveryCandidates();
  if (activateResults) switchContentView("results");
}

function stopDiscoveryTaskPolling() {
  if (discoveryTaskPollTimer) {
    clearInterval(discoveryTaskPollTimer);
    discoveryTaskPollTimer = null;
  }
}

function discoverySourceTitle(source) {
  const channel = followedChannelForSource(source.source_url);
  return channel ? channelTitle(channel) : source.source_url || `频道 ${source.index || ""}`;
}

function renderDiscoverySourceProgress(sources = []) {
  discoverySourceProgress.innerHTML = "";
  if (!sources.length) return;

  sources.forEach((source) => {
    const total = source.total || 0;
    const current = Math.min(source.current || 0, total || source.current || 0);
    const pct = total ? Math.min(100, Math.round((current / total) * 100)) : 0;
    const item = document.createElement("div");
    item.className = `discovery-source-progress-item ${source.status || "queued"}`;

    const top = document.createElement("div");
    top.className = "discovery-source-progress-top";

    const title = document.createElement("div");
    title.className = "discovery-source-progress-title";
    title.textContent = discoverySourceTitle(source);

    const status = document.createElement("div");
    status.className = "history-meta";
    const statusLabel = statusText[source.status] || "等待";
    status.textContent = total ? `${statusLabel} · ${current}/${total}` : statusLabel;
    top.append(title, status);

    const track = document.createElement("div");
    track.className = "source-progress-track";
    const fill = document.createElement("div");
    fill.style.width = `${pct}%`;
    track.appendChild(fill);

    const message = document.createElement("div");
    message.className = "discovery-source-progress-message";
    const scanned = Number(source.scanned_count || 0);
    const included = Number(source.included_count || 0);
    const countText = scanned || included ? ` · 已扫 ${scanned} · 候选 ${included}` : "";
    message.textContent = `${source.message || "等待扫描"}${countText}`;

    item.append(top, track, message);
    discoverySourceProgress.appendChild(item);
  });
}

function renderDiscoveryTaskProgress(task) {
  const total = task.total || 0;
  const current = Math.min(task.current || 0, total || task.current || 0);
  const pct = total ? Math.min(100, Math.round((current / total) * 100)) : 0;
  discoveryProgress.hidden = false;
  discoveryProgressLabel.textContent = statusText[task.status] || "扫描中";
  discoveryProgressCount.textContent = total ? `${current} / ${total}` : `${current}`;
  discoveryProgressBar.style.width = `${pct}%`;
  discoveryProgressMessage.textContent = task.message || "正在扫描频道";
  discoverySummary.textContent = task.message || "扫描中";
  renderDiscoverySourceProgress(task.source_progress || []);
}

async function finishDiscoveryTask(task) {
  stopDiscoveryTaskPolling();
  renderDiscoveryTaskProgress(task);
  if (task.status === "failed") {
    discoverySummary.textContent = `扫描失败：${task.error || task.message || "未知错误"}`;
    setDiscoveryBusy(false);
    return;
  }

  const result = task.result;
  if (!result) {
    discoverySummary.textContent = "扫描完成，但没有返回结果";
    setDiscoveryBusy(false);
    return;
  }

  if (task.kind === "channel_refresh") {
    renderChannelList(result.channels || []);
    renderChannelDetail(result.channel);
    renderDiscoveryRecord(result.record, { activateResults: false });
  } else {
    renderDiscoveryRecord(result);
  }
  await Promise.all([loadDiscoveryHistory(), loadChannels()]);
  discoveryProgressMessage.textContent = "扫描完成，内容已按频道保存";
  setDiscoveryBusy(false);
}

async function pollDiscoveryTask(taskId) {
  try {
    const response = await fetch(`/api/discovery-tasks/${encodeURIComponent(taskId)}`);
    const task = await response.json();
    if (!response.ok) throw new Error(task.error || `HTTP ${response.status}`);
    renderDiscoveryTaskProgress(task);
    if (task.status === "completed" || task.status === "completed_with_errors" || task.status === "failed") {
      await finishDiscoveryTask(task);
    }
    return task;
  } catch (error) {
    stopDiscoveryTaskPolling();
    discoverySummary.textContent = `扫描状态读取失败：${error.message}`;
    discoveryProgressMessage.textContent = error.message;
    setDiscoveryBusy(false);
  }
}

function startDiscoveryTaskPolling(taskId) {
  stopDiscoveryTaskPolling();
  pollDiscoveryTask(taskId);
  discoveryTaskPollTimer = setInterval(() => pollDiscoveryTask(taskId), 1200);
}

async function createDiscoveryTask(payload) {
  const response = await fetch("/api/discovery-tasks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const task = await response.json();
  if (!response.ok) throw new Error(task.error || `HTTP ${response.status}`);
  renderDiscoveryTaskProgress(task);
  startDiscoveryTaskPolling(task.id);
  return task;
}

async function discoverChannelVideos() {
  const channels = selectedChannels();
  if (!channels.length) {
    discoverySummary.textContent = followedChannels.length ? "请先勾选要查找的频道" : "请先添加关注频道";
    return;
  }
  const sources = channels.map((channel) => channel.source_url || channel.listing_url).join("\n");

  setDiscoveryBusy(true);
  switchContentView("results");
  discoveryList.innerHTML = '<div class="empty-state">扫描任务已提交，正在准备</div>';
  discoverySummary.textContent = "扫描任务已提交";

  try {
    await createDiscoveryTask({
      sources,
      range: rangeInput.value.trim() || "1年",
    });
  } catch (error) {
    discoverySummary.textContent = "查找失败";
    discoveryList.innerHTML = "";
    const item = document.createElement("div");
    item.className = "empty-state";
    item.textContent = error.message;
    discoveryList.appendChild(item);
    setDiscoveryBusy(false);
  }
}

function selectDiscoveryVideos(checked) {
  const videos = filteredDiscoveryVideos();
  videos.forEach((video) => {
    if (checked) selectedDiscoveryUrls.add(video.url);
    else selectedDiscoveryUrls.delete(video.url);
  });
  saveDiscoverySelection();
  renderDiscoveryCandidates();
}

function importSelectedDiscoveryUrls() {
  const urls = selectedDiscoveryVideos().map((video) => video.url);
  if (!urls.length) {
    discoverySummary.textContent = "请先选择视频";
    return false;
  }
  pendingArchiveTitle = "";
  urlInput.value = urls.join("\n");
  updateImportQueueSummary();
  switchPage("extract");
  urlInput.focus();
  return true;
}

async function loadDiscoveryRecord(recordId) {
  try {
    const response = await fetch(`/api/discoveries/${encodeURIComponent(recordId)}`);
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    renderDiscoveryRecord(payload);
    switchPage("search");
    return payload;
  } catch (error) {
    discoverySummary.textContent = `记录读取失败：${error.message}`;
  }
}

function renderDiscoveryHistory(records) {
  if (!records.length) {
    discoveryHistoryList.innerHTML = '<div class="history-empty">暂无扫描历史</div>';
    return;
  }
  discoveryHistoryList.innerHTML = "";
  records.forEach((record) => {
    const item = document.createElement("button");
    item.className = "history-item";
    item.type = "button";
    const providerText = providerSummaryText(record.provider_counts || {});
    const detailText = record.detail_lookup_count
      ? ` · 补日期 ${record.detail_lookup_success_count || 0}/${record.detail_lookup_count}`
      : "";
    const providerMeta = providerText ? ` · ${providerText}` : "";
    item.innerHTML = `
      <span class="history-title">${record.source_titles?.[0] || record.sources?.[0] || record.id}</span>
      <span class="history-meta">${record.video_count || 0} 个候选 · ${record.scanned_count || 0} 个已扫${providerMeta}${detailText} · ${formatDateTime(record.updated_at)}</span>
    `;
    item.addEventListener("click", () => loadDiscoveryRecord(record.id));
    discoveryHistoryList.appendChild(item);
  });
}

async function loadDiscoveryHistory() {
  try {
    const response = await fetch("/api/discoveries");
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    renderDiscoveryHistory(payload.records || []);
  } catch (error) {
    discoveryHistoryList.innerHTML = `<div class="history-empty">${error.message}</div>`;
  }
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
      <span class="pill">视频 ${formatOptionalSeconds(file.video_duration_seconds)}</span>
      <span class="pill">提取耗时 ${formatSeconds(file.elapsed_seconds)}</span>
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
  activeJobStatus = job.status;
  rememberJob(job.id);
  jobLabel.textContent = job.archive_title ? `${job.archive_title} · 任务 ${job.id}` : `任务 ${job.id}`;
  updateProgress(job);
  renderFiles(job);

  messages.innerHTML = "";
  if (job.queue_position) {
    appendMessage(`任务已排队，前面还有 ${job.queue_position - 1} 个任务。`);
  }
  if (job.message) {
    const remaining = cooldownRemaining(job);
    const suffix = remaining > 0 ? `（剩余 ${formatSeconds(remaining)}）` : "";
    appendMessage(`${job.message}${suffix}`);
  }
  job.errors.forEach((error) => {
    appendError(
      `${error.url}：${error.message}（提取耗时 ${formatSeconds(error.elapsed_seconds)}，重试 ${error.retry_count || 0}）`
    );
  });

  const hasDownload = job.files.length > 0 || job.status === "completed_with_errors" || job.status === "completed";
  if (hasDownload) {
    downloadAll.href = `/api/jobs/${encodeURIComponent(job.id)}/download`;
    if (job.download_filename) downloadAll.download = job.download_filename;
    downloadAll.classList.remove("disabled");
    downloadAll.setAttribute("aria-disabled", "false");
  }

  const done = isTerminalJobStatus(job.status);
  stopJobButton.hidden = done;
  stopJobButton.disabled = done || Boolean(job.cancel_requested);
  stopJobButton.textContent = job.cancel_requested && !done ? "停止中" : "停止任务";
  setBusy(!done);
  if (!done && cooldownRemaining(job) > 0) {
    serverState.textContent = "自动冷却";
  }
  if (done && pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  updateRunButtonState();
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

async function stopActiveJob() {
  if (!activeJobId) return;
  stopJobButton.disabled = true;
  stopJobButton.textContent = "停止中";
  try {
    const response = await fetch(`/api/jobs/${encodeURIComponent(activeJobId)}/stop`, {
      method: "POST",
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    renderJob(payload);
    if (!isTerminalJobStatus(payload.status)) startPolling(payload.id);
  } catch (error) {
    setMessage(`停止失败：${error.message}`, "error");
    stopJobButton.disabled = false;
    stopJobButton.textContent = "停止任务";
  }
}

async function createJob() {
  const urls = urlInput.value.trim();
  if (!urls) {
    setMessage("请先导入或输入视频 URL", "error");
    return;
  }

  selectedFileUrl = null;
  submittingJob = true;
  updateRunButtonState();
  setMessage("任务已加入队列");
  resultList.innerHTML = '<div class="empty-state">正在处理</div>';
  preview.textContent = "等待 Markdown 输出";
  openPreview.classList.add("disabled");
  downloadAll.classList.add("disabled");
  downloadAll.setAttribute("aria-disabled", "true");
  downloadAll.removeAttribute("download");
  progressBar.style.width = "0%";
  progressCount.textContent = "0 / 0";
  progressLabel.textContent = "排队中";
  summaryGrid.innerHTML = `
    <span>成功 0</span>
    <span>失败 0</span>
    <span>视频总长 0s</span>
    <span>视频均长 0s</span>
    <span>平均提取 0s</span>
    <span>提取总耗时 0s</span>
    <span>平均重试 0</span>
    <span>分批 -</span>
  `;

  try {
    const response = await fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ urls, archive_title: pendingArchiveTitle }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    renderJob(payload);
    startPolling(payload.id);
    pollJob(payload.id);
  } catch (error) {
    setMessage(error.message, "error");
  } finally {
    submittingJob = false;
    updateRunButtonState();
  }
}

async function restoreJob(jobId) {
  selectedFileUrl = null;
  openPreview.classList.add("disabled");
  preview.textContent = "正在恢复历史任务";
  switchPage("extract");
  const job = await pollJob(jobId);
  const done = job && isTerminalJobStatus(job.status);
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
    const title = job.archive_title || job.id;
    const idText = job.archive_title ? ` · ${job.id}` : "";
    const statusLabel = job.cancel_requested && !isTerminalJobStatus(job.status) ? "停止中" : statusText[job.status] || job.status;
    const queueText = job.queue_position ? ` · 排队第 ${job.queue_position}` : "";
    item.innerHTML = `
      <span class="history-title">${title}${idText} · ${statusLabel}${queueText}</span>
      <span class="history-meta">${job.current || 0}/${job.total || 0} · 成功 ${summary.success_count || 0} · 失败 ${summary.error_count || 0} · 视频 ${formatSummaryVideoTotal(summary)} · 缓存 ${summary.cache_hit_count || 0}</span>
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
  pageTabs.forEach((tab) => tab.addEventListener("click", () => switchPage(tab.dataset.page)));
  runButton.addEventListener("click", createJob);
  stopJobButton.addEventListener("click", stopActiveJob);
  refreshHistoryButton.addEventListener("click", loadHistory);
  refreshDiscoveryHistory.addEventListener("click", loadDiscoveryHistory);
  refreshChannels.addEventListener("click", loadChannels);
  addChannelsButton.addEventListener("click", addFollowedChannels);
  selectAllChannelsButton.addEventListener("click", selectAllFollowedChannels);
  invertChannelsButton.addEventListener("click", invertFollowedChannelSelection);
  refreshSelectedChannelsButton.addEventListener("click", discoverChannelVideos);
  refreshChannelButton.addEventListener("click", refreshActiveChannel);
  settingsToggleButton.addEventListener("click", () => setSettingsOpen(true));
  settingsCloseButton.addEventListener("click", () => setSettingsOpen(false));
  cancelSettingsButton.addEventListener("click", () => setSettingsOpen(false));
  settingsBackdrop.addEventListener("click", () => setSettingsOpen(false));
  saveSettingsButton.addEventListener("click", saveSettings);
  selectAllDiscovery.addEventListener("click", () => selectDiscoveryVideos(true));
  clearDiscoverySelection.addEventListener("click", () => selectDiscoveryVideos(false));
  importDiscovery.addEventListener("click", importSelectedDiscoveryUrls);
  discoveryFilterInput.addEventListener("input", renderDiscoveryCandidates);
  urlInput.addEventListener("input", () => {
    pendingArchiveTitle = "";
    updateImportQueueSummary();
  });
  clearButton.addEventListener("click", () => {
    urlInput.value = "";
    pendingArchiveTitle = "";
    updateImportQueueSummary();
    urlInput.focus();
  });

  updateImportQueueSummary();
  updateRunButtonState();
  await Promise.all([loadHistory(), loadDiscoveryHistory(), loadChannels(), loadSettings()]);

  const activeDiscoveryId = localStorage.getItem(ACTIVE_DISCOVERY_KEY);
  if (activeDiscoveryId) {
    loadDiscoveryRecord(activeDiscoveryId);
  }

  const active = localStorage.getItem(ACTIVE_JOB_KEY);
  if (active) {
    restoreJob(active);
  }
}

init();
