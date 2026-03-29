const AUTO_POLL_MS = 1000;
const SESSION_LIST_REFRESH_MS = 12000;
const INSPECT_IDLE_REFRESH_MS = 1500;
const INSPECT_RUNNING_REFRESH_MS = 4000;
const MAX_IMAGE_ATTACHMENTS = 4;
const MAX_IMAGE_ATTACHMENT_BYTES = 10 * 1024 * 1024;
const SUPPORTED_IMAGE_MIME_TYPES = new Set([
  "image/png",
  "image/jpeg",
  "image/webp",
  "image/gif",
  "image/bmp"
]);

const QUICK_ACTIONS = [
  {
    label: "Explain failure",
    prompt: "Inspect this Hermes session and explain the latest failure, root cause, blast radius, and the exact next fix."
  },
  {
    label: "Walk tool calls",
    prompt: "Walk through the recent tool calls one by one. Explain what each command or tool was trying to do, what inputs it saw, and what happened."
  },
  {
    label: "Timing audit",
    prompt: "Analyze the timing and activity log for this session. Call out waits, retries, slow tools, and likely bottlenecks."
  },
  {
    label: "Prompt audit",
    prompt: "Audit the current session context: user request, system prompt assumptions, injected context, tool outputs, and any hidden gotchas."
  },
  {
    label: "Patch plan",
    prompt: "Based on this session, propose the minimal code or config changes to fix the problem. Name the files and the exact edits or commands."
  },
  {
    label: "Benchmark tools",
    prompt: "Compare the tools used in this session against plausible alternatives. What was fast, slow, noisy, brittle, or overkill?"
  }
];

const openOptionsButton = document.getElementById("open-options-button");
const refreshAllButton = document.getElementById("refresh-all-button");
const livePollToggle = document.getElementById("live-poll-toggle");
const bridgeStatusText = document.getElementById("bridge-status-text");
const sessionSelect = document.getElementById("session-select");
const refreshSessionsButton = document.getElementById("refresh-sessions-button");
const refreshInspectButton = document.getElementById("refresh-inspect-button");
const newChatButton = document.getElementById("new-chat-button");
const interruptButton = document.getElementById("interrupt-button");
const statusText = document.getElementById("status-text");
const sessionMetaText = document.getElementById("session-meta-text");
const modeText = document.getElementById("mode-text");
const statsChipGrid = document.getElementById("stats-chip-grid");
const askInput = document.getElementById("ask-input");
const sendButton = document.getElementById("send-button");
const quickActions = document.getElementById("quick-actions");
const conversationTimeline = document.getElementById("conversation-timeline");
const activityLogPre = document.getElementById("activity-log-pre");
const auditEventsList = document.getElementById("audit-events-list");
const branchGraph = document.getElementById("branch-graph");
const benchmarkSummary = document.getElementById("benchmark-summary");
const systemPromptPre = document.getElementById("system-prompt-pre");
const toolSummary = document.getElementById("tool-summary");
const toolSchemaPre = document.getElementById("tool-schema-pre");
const settingsSourceStack = document.getElementById("settings-source-stack");
const pathsStack = document.getElementById("paths-stack");
const rawInspectPre = document.getElementById("raw-inspect-pre");
const attachmentInput = document.getElementById("attachment-input");
const attachmentStrip = document.getElementById("attachment-strip");
const attachButton = document.getElementById("attach-button");
const screengrabButton = document.getElementById("screengrab-button");
const voiceInputButton = document.getElementById("voice-input-button");
const voiceRecorderSheet = document.getElementById("voice-recorder-sheet");
const voiceRecorderSheetStatus = document.getElementById("voice-recorder-sheet-status");
const voiceRecorderCloseButton = document.getElementById("voice-recorder-close-button");

let extensionSettings = {
  themeName: window.HermesTheme?.defaultThemeId || "obsidian",
  customThemeAccent: window.HermesTheme?.defaultCustomThemePrimary || "#8b5cf6",
  customThemes: [],
  audioInputDeviceId: ""
};
let runtimeConfig = null;
let sessionHistoryByKey = new Map();
let selectedSessionKey = "";
let selectedSessionCanSend = true;
let currentState = null;
let currentInspection = null;
let pendingAttachments = [];
let activeReplyAudio = null;
let activeReplyAudioUrl = "";
let activeAudioMessageKey = "";
let voiceRecordingActive = false;
let voiceTranscriptionPending = false;
let pollTimer = null;
let stateRefreshInFlight = false;
let inspectRefreshInFlight = false;
let lastSessionListRefreshAt = 0;
let lastInspectSignature = "";
let lastInspectRequestedAt = 0;
const voiceInputChannel = typeof BroadcastChannel !== "undefined"
  ? new BroadcastChannel("hermes-sidecar-voice-input")
  : null;

window.HermesTheme?.applyThemeToDocument({
  themeName: window.HermesTheme?.defaultThemeId || "obsidian"
});

function setStatus(message, isError = false) {
  statusText.textContent = String(message || "").trim() || "Waiting for the first snapshot.";
  statusText.classList.toggle("is-error", Boolean(isError));
}

function setBridgeStatus(message, isError = false) {
  bridgeStatusText.textContent = String(message || "").trim() || "Bridge status unknown.";
  bridgeStatusText.classList.toggle("is-error", Boolean(isError));
}

function isExtensionContextInvalidated(error) {
  const message = String(error?.message || error || "").toLowerCase();
  return (
    message.includes("extension context invalidated") ||
    message.includes("context invalidated") ||
    message.includes("message port closed before a response was received")
  );
}

function explainExtensionError(error) {
  if (isExtensionContextInvalidated(error)) {
    return "Hermes Sidecar was reloaded or updated. Reload this control-room tab to reconnect.";
  }
  return String(error?.message || error || "Unknown extension error.");
}

async function sendRuntimeMessage(payload) {
  let response;
  try {
    response = await chrome.runtime.sendMessage(payload);
  } catch (error) {
    throw new Error(explainExtensionError(error));
  }
  if (!response?.ok) {
    throw new Error(explainExtensionError(response?.error || "Unknown extension error."));
  }
  return response;
}

async function copyTextToClipboard(text) {
  const value = String(text || "");
  if (!value) {
    return;
  }
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const fallback = document.createElement("textarea");
  fallback.value = value;
  fallback.setAttribute("readonly", "readonly");
  fallback.style.position = "fixed";
  fallback.style.opacity = "0";
  document.body.appendChild(fallback);
  fallback.select();
  document.execCommand("copy");
  document.body.removeChild(fallback);
}

function stringifyPretty(value, fallback = "(No data)") {
  if (value === undefined || value === null) {
    return fallback;
  }
  if (typeof value === "string") {
    return value.trim() || fallback;
  }
  try {
    const serialized = JSON.stringify(value, null, 2);
    return serialized && serialized !== "{}" && serialized !== "[]"
      ? serialized
      : fallback;
  } catch (_error) {
    return String(value);
  }
}

function summarizeText(text, maxLength = 140) {
  const normalized = String(text || "").replace(/\s+/g, " ").trim();
  if (!normalized) {
    return "";
  }
  if (normalized.length <= maxLength) {
    return normalized;
  }
  return `${normalized.slice(0, maxLength - 3).trimEnd()}...`;
}

function formatTimestamp(value) {
  const raw = String(value || "").trim();
  if (!raw) {
    return "";
  }
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) {
    return raw;
  }
  return date.toLocaleString();
}

function formatElapsed(seconds) {
  const total = Math.max(0, Number(seconds || 0));
  if (!total) {
    return "0s";
  }
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const remainder = Math.floor(total % 60);
  if (hours) {
    return `${hours}h ${minutes}m ${remainder}s`;
  }
  if (minutes) {
    return `${minutes}m ${remainder}s`;
  }
  return `${remainder}s`;
}

function formatCount(value) {
  return Number(value || 0).toLocaleString();
}

function formatAttachmentSize(bytes) {
  const value = Number(bytes || 0);
  if (!value) {
    return "0 B";
  }
  if (value < 1024) {
    return `${value} B`;
  }
  if (value < 1024 * 1024) {
    return `${(value / 1024).toFixed(1)} KB`;
  }
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function decodeBase64Audio(base64Text) {
  const binary = atob(String(base64Text || ""));
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

function clearReplyAudioState() {
  if (activeReplyAudio) {
    try {
      activeReplyAudio.pause();
    } catch (_error) {
      // ignore cleanup failures
    }
    activeReplyAudio.src = "";
  }
  if (activeReplyAudioUrl) {
    URL.revokeObjectURL(activeReplyAudioUrl);
  }
  activeReplyAudio = null;
  activeReplyAudioUrl = "";
  activeAudioMessageKey = "";
}

function messageKey(message) {
  return JSON.stringify({
    role: message?.role || "",
    kind: message?.kind || "",
    content: message?.display_content || message?.content || "",
    pageTitle: message?.page_title || "",
    pageUrl: message?.page_url || ""
  });
}

function getMessageText(message) {
  return String(message?.display_content || message?.content || "").trim();
}

function extractTaggedReplySections(rawText) {
  const full = String(rawText || "").trim();
  if (!full) {
    return { full: "", knight: "", answer: "" };
  }
  const pattern = /<ʞᴎiʜƚ>([\s\S]*?)<\/ʞᴎiʜƚ>/gi;
  const knightParts = [];
  let match;
  while ((match = pattern.exec(full)) !== null) {
    const value = String(match[1] || "").trim();
    if (value) {
      knightParts.push(value);
    }
  }
  const knight = knightParts.join("\n\n").trim();
  const answer = full.replace(pattern, "").trim();
  return { full, knight, answer };
}

function buildReplyActionSpecs(message) {
  const full = getMessageText(message);
  const sections = extractTaggedReplySections(full);
  const key = messageKey(message);

  if (sections.knight || sections.answer) {
    return [
      sections.knight
        ? {
            kind: "copy",
            label: "Copy ʞᴎiʜƚ",
            text: sections.knight,
            successMessage: "ʞᴎiʜƚ section copied to clipboard.",
            errorMessage: "Could not copy the ʞᴎiʜƚ section.",
          }
        : null,
      sections.answer
        ? {
            kind: "copy",
            label: "Copy answer",
            text: sections.answer,
            successMessage: "Answer section copied to clipboard.",
            errorMessage: "Could not copy the answer section.",
          }
        : null,
      sections.knight
        ? {
            kind: "speak",
            label: "Read ʞᴎiʜƚ",
            text: sections.knight,
            audioKey: `${key}:knight`,
            errorMessage: "Could not generate Hermes TTS audio for the ʞᴎiʜƚ section.",
          }
        : null,
      sections.answer
        ? {
            kind: "speak",
            label: "Read answer",
            text: sections.answer,
            audioKey: `${key}:answer`,
            errorMessage: "Could not generate Hermes TTS audio for the answer section.",
          }
        : null,
    ].filter(Boolean);
  }

  return [
    {
      kind: "copy",
      label: "Copy",
      text: full,
      successMessage: "Reply copied to clipboard.",
      errorMessage: "Could not copy this reply.",
    },
    {
      kind: "speak",
      label: "Read aloud",
      text: full,
      audioKey: key,
      errorMessage: "Could not generate Hermes TTS audio.",
    },
  ];
}

function getMessageImages(message) {
  return Array.isArray(message?.images)
    ? message.images.filter((image) => image && typeof image === "object" && image.media_url)
    : [];
}

function renderMessageImages(container, message) {
  const images = getMessageImages(message);
  if (!images.length) {
    return;
  }
  const gallery = document.createElement("div");
  gallery.className = "message-images";
  for (const image of images) {
    const link = document.createElement("a");
    link.className = "message-image-link";
    link.href = image.media_url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";

    const img = document.createElement("img");
    img.className = "message-image";
    img.src = image.media_url;
    img.alt = image.alt_text || image.file_name || "Hermes image";
    img.loading = "lazy";

    link.appendChild(img);
    gallery.appendChild(link);
  }
  container.appendChild(gallery);
}

function createStatChip(label, value) {
  const chip = document.createElement("span");
  chip.className = "stat-chip";
  const title = document.createElement("span");
  title.textContent = label;
  const strong = document.createElement("strong");
  strong.textContent = value;
  chip.appendChild(title);
  chip.appendChild(strong);
  return chip;
}

function buildSessionOptionLabel(session) {
  const label = String(session?.browser_label || "Hermes session").trim() || "Hermes session";
  const running = session?.running ? "running" : `${formatCount(session?.message_count || 0)} msgs`;
  const source = String(session?.source || "").trim();
  return source && source !== "browser"
    ? `${label} - ${source} - ${running}`
    : `${label} - ${running}`;
}

function ensureCurrentSessionListed() {
  if (!currentState?.session_key) {
    return;
  }
  if (sessionHistoryByKey.has(currentState.session_key)) {
    return;
  }
  sessionHistoryByKey.set(currentState.session_key, {
    session_key: currentState.session_key,
    session_id: currentState.session_id || "",
    browser_label: currentState.browser_label || "Browser Sidecar",
    source: currentState.source || "",
    message_count: Array.isArray(currentState.messages) ? currentState.messages.length : 0,
    running: Boolean(currentState.progress?.running)
  });
}

function renderSessionOptions(activeKey = selectedSessionKey) {
  ensureCurrentSessionListed();
  const sessions = Array.from(sessionHistoryByKey.values());
  sessions.sort((left, right) => {
    const leftUpdated = String(left.updated_at || "").trim();
    const rightUpdated = String(right.updated_at || "").trim();
    return rightUpdated.localeCompare(leftUpdated);
  });

  sessionSelect.textContent = "";
  if (!sessions.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "Current browser sidecar session";
    sessionSelect.appendChild(option);
    sessionSelect.value = "";
    return;
  }

  for (const session of sessions) {
    const option = document.createElement("option");
    option.value = String(session.session_key || session.session_id || "").trim();
    option.textContent = buildSessionOptionLabel(session);
    sessionSelect.appendChild(option);
  }

  const fallbackValue = sessions[0]?.session_key || "";
  sessionSelect.value = sessions.some((session) => session.session_key === activeKey)
    ? activeKey
    : fallbackValue;
}

function renderConversation(messages) {
  conversationTimeline.textContent = "";
  const list = Array.isArray(messages) ? messages : [];
  if (!list.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "No messages yet.";
    conversationTimeline.appendChild(empty);
    return;
  }

  for (const message of list) {
    const entry = document.createElement("article");
    const roleName = String(message.role || "assistant").toLowerCase();
    entry.className = `timeline-entry ${roleName}`;

    const meta = document.createElement("div");
    meta.className = "timeline-meta";

    const role = document.createElement("span");
    role.className = "timeline-role";
    role.textContent = String(message.role || "assistant").toUpperCase();
    meta.appendChild(role);

    const kind = String(message.kind || "chat").trim();
    if (kind) {
      const badge = document.createElement("span");
      badge.className = "timeline-badge";
      badge.textContent = kind;
      meta.appendChild(badge);
    }

    const timestamp = formatTimestamp(message.timestamp);
    if (timestamp) {
      const time = document.createElement("span");
      time.className = "timeline-badge";
      time.textContent = timestamp;
      meta.appendChild(time);
    }
    entry.appendChild(meta);

    if (message.page_title) {
      const title = document.createElement("p");
      title.className = "meta-value";
      title.textContent = message.page_title;
      entry.appendChild(title);
    }

    if (message.page_url) {
      const url = document.createElement("p");
      url.className = "meta-value";
      url.textContent = message.page_url;
      entry.appendChild(url);
    }

    const body = document.createElement("pre");
    body.className = "timeline-body";
    body.textContent = getMessageText(message) || "(Empty message)";
    entry.appendChild(body);
    renderMessageImages(entry, message);

    const canActOnReply = roleName === "assistant" && getMessageText(message);
    if (canActOnReply) {
      const actions = document.createElement("div");
      actions.className = "message-actions";

      for (const action of buildReplyActionSpecs(message)) {
        const button = document.createElement("button");
        button.className = "message-action-button";
        button.type = "button";
        if (action.kind === "speak") {
          const isSpeaking = activeAudioMessageKey === action.audioKey;
          if (isSpeaking) {
            button.classList.add("is-active");
          }
          button.textContent = isSpeaking ? "Stop audio" : action.label;
          button.addEventListener("click", () => {
            speakReply(message, { text: action.text, audioKey: action.audioKey }).catch((error) => {
              setStatus(error?.message || action.errorMessage, true);
            });
          });
        } else {
          button.textContent = action.label;
          button.addEventListener("click", () => {
            copyTextToClipboard(action.text)
              .then(() => setStatus(action.successMessage))
              .catch(() => setStatus(action.errorMessage, true));
          });
        }
        actions.appendChild(button);
      }

      entry.appendChild(actions);
    }

    conversationTimeline.appendChild(entry);
  }
}

function renderActivityLog(progress) {
  const lines = Array.isArray(progress?.activity_log)
    ? progress.activity_log
    : Array.isArray(progress?.recent_events)
      ? progress.recent_events
      : [];
  activityLogPre.textContent = lines.length
    ? lines.join("\n")
    : "(No gateway activity captured for this session yet.)";
}

function renderStats() {
  statsChipGrid.textContent = "";
  const progress = currentState?.progress || {};
  const stats = currentInspection?.stats || {};
  const chips = [
    ["Mode", selectedSessionCanSend ? "Interactive" : "Read-only"],
    ["Running", progress.running ? "Yes" : "No"],
    ["Elapsed", formatElapsed(progress.elapsed_seconds || stats.elapsed_seconds || 0)],
    ["Visible", formatCount(Array.isArray(currentState?.messages) ? currentState.messages.length : 0)],
    ["Transcript", formatCount(stats.transcript_message_count || 0)],
    ["Tool calls", formatCount(stats.tool_call_count || 0)],
    ["Tool results", formatCount(stats.tool_result_count || 0)],
    ["Audit events", formatCount(stats.audit_event_count || 0)],
    ["Delegate calls", formatCount(stats.delegate_call_count || 0)],
    ["Branches", formatCount(stats.delegate_branch_count || 0)],
    ["Prompt chars", formatCount(stats.system_prompt_chars || 0)],
    ["Configured tools", formatCount(stats.configured_tool_count || 0)]
  ];
  for (const [label, value] of chips) {
    statsChipGrid.appendChild(createStatChip(label, value));
  }
}

function renderDataCards(container, cards) {
  container.textContent = "";
  const validCards = Array.isArray(cards)
    ? cards.filter((card) => card && String(card.text || "").trim())
    : [];
  if (!validCards.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "No data available.";
    container.appendChild(empty);
    return;
  }

  for (const card of validCards) {
    const section = document.createElement("section");
    section.className = "data-card";

    const head = document.createElement("div");
    head.className = "data-card-head";
    const title = document.createElement("p");
    title.className = "data-card-title";
    title.textContent = card.title;
    head.appendChild(title);

    if (card.copyValue) {
      const copyButton = document.createElement("button");
      copyButton.className = "ghost-button small-button";
      copyButton.type = "button";
      copyButton.textContent = "Copy";
      copyButton.addEventListener("click", () => {
        copyTextToClipboard(card.copyValue)
          .then(() => setStatus(`Copied ${card.title.toLowerCase()}.`))
          .catch(() => setStatus(`Could not copy ${card.title.toLowerCase()}.`, true));
      });
      head.appendChild(copyButton);
    }

    section.appendChild(head);
    const copy = document.createElement("pre");
    copy.className = "data-card-copy";
    copy.textContent = card.text;
    section.appendChild(copy);
    container.appendChild(section);
  }
}

function renderAuditEvents() {
  const events = Array.isArray(currentInspection?.audit_events) ? currentInspection.audit_events : [];
  const cards = events.map((event, index) => {
    const titleBits = [
      String(event.kind || "event").replace(/_/g, " ").trim().toUpperCase(),
      String(event.tool_name || "").trim(),
      String(event.status || "").trim(),
      formatTimestamp(event.ts),
    ].filter(Boolean);
    const summaryPayload = {
      index: index + 1,
      phase: event.phase || null,
      title: event.title || null,
      preview: event.preview || null,
      duration_ms: event.duration_ms ?? null,
      payload: event.payload ?? null,
    };
    return {
      title: titleBits.join(" - ") || `EVENT ${index + 1}`,
      text: stringifyPretty(summaryPayload, ""),
      copyValue: stringifyPretty(event, "")
    };
  });
  renderDataCards(auditEventsList, cards);
}

function renderBranches() {
  const delegateSummary = currentInspection?.delegate_summary || {};
  const calls = Array.isArray(delegateSummary.calls) ? delegateSummary.calls : [];
  const cards = [
    {
      title: "Branch totals",
      text: stringifyPretty({
        total_delegate_calls: delegateSummary.total_delegate_calls || 0,
        total_branches: delegateSummary.total_branches || 0,
      }, "")
    }
  ];
  for (const call of calls) {
    cards.push({
      title: `${call.id} - ${call.branch_count} branch${call.branch_count === 1 ? "" : "es"}`,
      text: stringifyPretty(call, "")
    });
  }
  renderDataCards(branchGraph, cards);
}

function renderBenchmarks() {
  const benchmark = currentInspection?.benchmark_summary || {};
  const cards = [
    {
      title: "Runtime summary",
      text: stringifyPretty({
        runtime_ms: benchmark.runtime_ms || 0,
        tool_count: benchmark.tool_count || 0,
        message_count: benchmark.message_count || 0,
        error_count: benchmark.error_count || 0,
      }, "")
    },
    {
      title: "Slowest tools",
      text: stringifyPretty(benchmark.slowest_tools, "(No slow-tool metrics recorded.)")
    },
    {
      title: "All tool stats",
      text: stringifyPretty(benchmark.tools, "(No tool benchmark stats recorded.)")
    }
  ];
  renderDataCards(benchmarkSummary, cards);
}

function renderSystemPrompt() {
  const prompt = String(currentInspection?.session_log?.system_prompt || "").trim();
  systemPromptPre.textContent = prompt || "(No saved session log for this session yet.)";
}

function renderTools() {
  const stats = currentInspection?.stats || {};
  const configuredToolNames = Array.isArray(currentInspection?.configured_tools)
    ? currentInspection.configured_tools
    : [];
  const observedToolNames = Array.isArray(stats.tool_names)
    ? stats.tool_names
    : [];

  renderDataCards(toolSummary, [
    {
      title: "Configured tools",
      text: configuredToolNames.length
        ? configuredToolNames.join("\n")
        : "(No configured tool metadata saved for this session.)"
    },
    {
      title: "Observed tool names",
      text: observedToolNames.length
        ? observedToolNames.join("\n")
        : "(No tool calls observed yet.)"
    },
    {
      title: "Role counts",
      text: stringifyPretty(stats.roles, "(No raw role counts yet.)")
    }
  ]);

  toolSchemaPre.textContent = stringifyPretty(
    currentInspection?.session_log?.tools,
    "(No tool schema snapshot yet.)"
  );
}

function renderSettingsAndSource() {
  renderDataCards(settingsSourceStack, [
    {
      title: "Extension settings",
      text: stringifyPretty(extensionSettings, "(Settings not loaded.)")
    },
    {
      title: "Runtime config",
      text: stringifyPretty(runtimeConfig?.config, "(Runtime config not loaded.)")
    },
    {
      title: "Source details",
      text: stringifyPretty(currentInspection?.source_details, "(No source details.)")
    },
    {
      title: "Session record",
      text: stringifyPretty(currentInspection?.session_record, "(No persisted session record.)")
    }
  ]);
}

function renderPaths() {
  const paths = currentInspection?.paths || {};
  const runtimePaths = runtimeConfig?.paths || {};
  renderDataCards(pathsStack, [
    {
      title: "Hermes home",
      text: String(paths.hermes_home || "").trim(),
      copyValue: String(paths.hermes_home || "").trim()
    },
    {
      title: "Session log path",
      text: String(paths.session_log_path || "").trim() || "(No saved session log path.)",
      copyValue: String(paths.session_log_path || "").trim()
    },
    {
      title: "Config path",
      text: String(runtimePaths.config_path || paths.config_path || "").trim(),
      copyValue: String(runtimePaths.config_path || paths.config_path || "").trim()
    },
    {
      title: "Env path",
      text: String(runtimePaths.env_path || "").trim() || "(No env path reported.)",
      copyValue: String(runtimePaths.env_path || "").trim()
    },
    {
      title: "Logs directory",
      text: String(paths.logs_dir || "").trim(),
      copyValue: String(paths.logs_dir || "").trim()
    },
    {
      title: "Sessions directory",
      text: String(paths.sessions_dir || "").trim(),
      copyValue: String(paths.sessions_dir || "").trim()
    }
  ]);
}

function renderRawInspectJson() {
  rawInspectPre.textContent = stringifyPretty(currentInspection, "(No inspect payload loaded yet.)");
}

function renderInspectionPanels() {
  renderStats();
  renderAuditEvents();
  renderBranches();
  renderBenchmarks();
  renderSystemPrompt();
  renderTools();
  renderSettingsAndSource();
  renderPaths();
  renderRawInspectJson();
}

function updateModeText() {
  if (!currentState) {
    modeText.textContent = "Detecting session capabilities...";
    return;
  }
  if (selectedSessionCanSend) {
    modeText.textContent = "Browser sidecar session. Interactive sends, images, voice input, and interrupts are enabled here.";
    return;
  }
  const source = String(currentState.source || currentInspection?.source_details?.platform || "").trim();
  modeText.textContent = source
    ? `Read-only ${source} session. Inspect here, then start a new browser chat for interactive follow-up.`
    : "Read-only session. Inspect here, then start a new browser chat for interactive follow-up.";
}

function updateSessionMeta() {
  if (!currentState) {
    sessionMetaText.textContent = "Session ID unavailable.";
    return;
  }
  const parts = [];
  const sessionId = String(currentState.session_id || "").trim();
  const sessionKey = String(currentState.session_key || "").trim();
  if (currentState.browser_label) {
    parts.push(`Label: ${currentState.browser_label}`);
  }
  if (sessionId) {
    parts.push(`Session ID: ${sessionId}`);
  }
  if (sessionKey) {
    parts.push(`Key: ${sessionKey}`);
  }
  if (currentState.source) {
    parts.push(`Source: ${currentState.source}`);
  }
  sessionMetaText.textContent = parts.join("\n") || "Session ID unavailable.";
}

function renderAttachmentStrip() {
  if (!attachmentStrip) {
    return;
  }
  attachmentStrip.textContent = "";
  attachmentStrip.hidden = pendingAttachments.length === 0;
  for (const attachment of pendingAttachments) {
    const chip = document.createElement("div");
    chip.className = "attachment-chip";

    const thumb = document.createElement("img");
    thumb.className = "attachment-thumb";
    thumb.src = attachment.previewUrl;
    thumb.alt = attachment.name;
    chip.appendChild(thumb);

    const meta = document.createElement("div");
    meta.className = "attachment-meta";
    const name = document.createElement("span");
    name.className = "attachment-name";
    name.textContent = attachment.name;
    meta.appendChild(name);

    const size = document.createElement("span");
    size.className = "attachment-size";
    size.textContent = formatAttachmentSize(attachment.size_bytes);
    meta.appendChild(size);
    chip.appendChild(meta);

    const removeButton = document.createElement("button");
    removeButton.className = "attachment-remove";
    removeButton.type = "button";
    removeButton.textContent = "×";
    removeButton.title = `Remove ${attachment.name}`;
    removeButton.addEventListener("click", () => {
      removePendingAttachment(attachment.id);
    });
    chip.appendChild(removeButton);

    attachmentStrip.appendChild(chip);
  }
}

function removePendingAttachment(attachmentId) {
  const nextAttachments = [];
  for (const attachment of pendingAttachments) {
    if (attachment.id === attachmentId) {
      if (attachment.previewUrl && attachment.previewUrlRevocable) {
        URL.revokeObjectURL(attachment.previewUrl);
      }
      continue;
    }
    nextAttachments.push(attachment);
  }
  pendingAttachments = nextAttachments;
  renderAttachmentStrip();
  updateActionAvailability();
}

function clearPendingAttachments() {
  for (const attachment of pendingAttachments) {
    if (attachment.previewUrl && attachment.previewUrlRevocable) {
      URL.revokeObjectURL(attachment.previewUrl);
    }
  }
  pendingAttachments = [];
  if (attachmentInput) {
    attachmentInput.value = "";
  }
  renderAttachmentStrip();
}

function fileToDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error(`Could not read ${file?.name || "image file"}.`));
    reader.onload = () => resolve(String(reader.result || ""));
    reader.readAsDataURL(file);
  });
}

async function addAttachmentFiles(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length) {
    return;
  }
  if (pendingAttachments.length + files.length > MAX_IMAGE_ATTACHMENTS) {
    throw new Error(`You can attach up to ${MAX_IMAGE_ATTACHMENTS} images per turn.`);
  }
  for (const file of files) {
    const mimeType = String(file.type || "").toLowerCase();
    if (!SUPPORTED_IMAGE_MIME_TYPES.has(mimeType)) {
      throw new Error(`${file.name} is not a supported image type.`);
    }
    if (Number(file.size || 0) > MAX_IMAGE_ATTACHMENT_BYTES) {
      throw new Error(`${file.name} is larger than ${formatAttachmentSize(MAX_IMAGE_ATTACHMENT_BYTES)}.`);
    }
    const dataUrl = await fileToDataUrl(file);
    pendingAttachments.push({
      id: crypto.randomUUID(),
      name: file.name || "image",
      mime_type: mimeType || "image/png",
      size_bytes: Number(file.size || 0),
      data_url: dataUrl,
      previewUrl: URL.createObjectURL(file),
      previewUrlRevocable: true
    });
  }
  renderAttachmentStrip();
  updateActionAvailability();
}

function estimateDataUrlByteLength(dataUrl) {
  const value = String(dataUrl || "");
  const marker = "base64,";
  const index = value.indexOf(marker);
  if (index === -1) {
    return 0;
  }
  const base64 = value.slice(index + marker.length);
  const paddingMatch = base64.match(/=+$/);
  const paddingLength = paddingMatch ? paddingMatch[0].length : 0;
  return Math.max(0, Math.floor((base64.length * 3) / 4) - paddingLength);
}

function addAttachmentDataUrl({ dataUrl, name = "image.png", mimeType = "image/png", sizeBytes = 0 }) {
  const normalizedDataUrl = String(dataUrl || "").trim();
  const normalizedMimeType = String(mimeType || "").toLowerCase();
  if (!normalizedDataUrl) {
    throw new Error("No screenshot data was returned.");
  }
  if (!SUPPORTED_IMAGE_MIME_TYPES.has(normalizedMimeType)) {
    throw new Error("This screenshot format is not supported.");
  }
  if (pendingAttachments.length >= MAX_IMAGE_ATTACHMENTS) {
    throw new Error(`You can attach up to ${MAX_IMAGE_ATTACHMENTS} images per turn.`);
  }
  const resolvedSizeBytes = Number(sizeBytes || 0) || estimateDataUrlByteLength(normalizedDataUrl);
  if (resolvedSizeBytes > MAX_IMAGE_ATTACHMENT_BYTES) {
    throw new Error(`Screenshot is larger than ${formatAttachmentSize(MAX_IMAGE_ATTACHMENT_BYTES)}.`);
  }
  pendingAttachments.push({
    id: crypto.randomUUID(),
    name: String(name || "image.png").trim() || "image.png",
    mime_type: normalizedMimeType,
    size_bytes: resolvedSizeBytes,
    data_url: normalizedDataUrl,
    previewUrl: normalizedDataUrl,
    previewUrlRevocable: false
  });
  renderAttachmentStrip();
  updateActionAvailability();
}

function buildAttachmentPayloads() {
  return pendingAttachments.map((attachment) => ({
    name: attachment.name,
    mime_type: attachment.mime_type,
    size_bytes: attachment.size_bytes,
    data_url: attachment.data_url
  }));
}

async function captureCurrentTabScreengrab() {
  const tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  const tab = Array.isArray(tabs) ? tabs[0] : null;
  if (!tab?.id) {
    throw new Error("Could not find an active browser tab to capture.");
  }
  const response = await sendRuntimeMessage({
    type: "hermes:capture-visible-tab",
    tabId: tab.id
  });
  const result = response.result || {};
  addAttachmentDataUrl({
    dataUrl: result.data_url,
    name: result.name || "page-screengrab.png",
    mimeType: result.mime_type || "image/png",
    sizeBytes: result.size_bytes || 0
  });
  return result;
}

function setVoiceRecorderSheetVisible(visible, message = "") {
  if (!voiceRecorderSheet) {
    return;
  }
  voiceRecorderSheet.hidden = !visible;
  if (voiceRecorderSheetStatus && message) {
    voiceRecorderSheetStatus.textContent = String(message);
  }
}

function appendTranscriptToComposer(value) {
  const text = String(value || "").trim();
  if (!text || !askInput) {
    return;
  }
  const current = String(askInput.value || "").trim();
  askInput.value = current ? `${current}\n\n${text}` : text;
  askInput.focus();
  askInput.setSelectionRange(askInput.value.length, askInput.value.length);
}

async function getMicrophonePermissionState() {
  if (!navigator.permissions?.query) {
    return "unknown";
  }
  try {
    const status = await navigator.permissions.query({ name: "microphone" });
    return String(status?.state || "unknown");
  } catch (_error) {
    return "unknown";
  }
}

function buildVoiceAudioConstraints(selectedDeviceId = "", captureMode = "raw") {
  const normalizedMode = String(captureMode || "").trim().toLowerCase() === "speech" ? "speech" : "raw";
  const constraints = normalizedMode === "speech"
    ? {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
        channelCount: { ideal: 1 }
      }
    : {
        echoCancellation: false,
        noiseSuppression: false,
        autoGainControl: false,
        channelCount: { ideal: 1 }
      };
  const normalizedDeviceId = String(selectedDeviceId || "").trim();
  if (normalizedDeviceId) {
    constraints.deviceId = { exact: normalizedDeviceId };
  }
  return constraints;
}

async function ensureMicrophoneCapturePermission(selectedDeviceId = "", captureMode = "raw") {
  if (!navigator.mediaDevices?.getUserMedia) {
    throw new Error("This browser does not support microphone capture from extension pages.");
  }
  const permissionState = await getMicrophonePermissionState();
  if (permissionState === "granted") {
    return;
  }
  if (permissionState === "denied") {
    throw new Error("Microphone access is blocked for this extension. Re-enable it in Chrome site permissions or Hermes Options.");
  }
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: buildVoiceAudioConstraints(selectedDeviceId, captureMode)
  });
  for (const track of stream.getTracks()) {
    track.stop();
  }
}

async function toggleVoiceRecording() {
  if (voiceTranscriptionPending) {
    return;
  }
  if (voiceRecordingActive) {
    voiceInputChannel?.postMessage({ type: "hermes:stop-recording" });
    setVoiceRecorderSheetVisible(true, "Voice note captured. Hermes is transcribing it now...");
    setStatus("Stopping voice recording...");
    return;
  }
  setVoiceRecorderSheetVisible(true, "Requesting microphone access if needed...");
  try {
    const settingsResponse = await sendRuntimeMessage({ type: "hermes:get-settings" });
    const selectedDeviceId = String(settingsResponse.settings?.audioInputDeviceId || "").trim();
    await ensureMicrophoneCapturePermission(selectedDeviceId, "raw");
    await sendRuntimeMessage({ type: "hermes:ensure-offscreen-voice-recorder" });
    voiceInputChannel?.postMessage({
      type: "hermes:start-recording",
      deviceId: selectedDeviceId,
      captureMode: "raw"
    });
    voiceRecordingActive = true;
    updateActionAvailability();
    setStatus("Starting voice recording...");
  } catch (error) {
    voiceRecordingActive = false;
    voiceTranscriptionPending = false;
    updateActionAvailability();
    const message = String(error?.message || error);
    setVoiceRecorderSheetVisible(true, message);
    throw error;
  }
}

async function speakReply(message, options = {}) {
  const text = String(options.text || getMessageText(message)).trim();
  if (!text) {
    return;
  }
  const nextKey = String(options.audioKey || messageKey(message)).trim() || messageKey(message);
  if (activeAudioMessageKey === nextKey) {
    clearReplyAudioState();
    renderConversation(currentState?.messages || []);
    return;
  }
  clearReplyAudioState();
  activeAudioMessageKey = nextKey;
  renderConversation(currentState?.messages || []);
  setStatus("Generating reply audio with Hermes TTS...");
  try {
    const response = await sendRuntimeMessage({ type: "hermes:speak-chat-message", text });
    const result = response.result || {};
    const audioBase64 = String(result.audio_base64 || "");
    if (!audioBase64) {
      throw new Error("Hermes TTS did not return any audio.");
    }
    const blob = new Blob([decodeBase64Audio(audioBase64)], { type: String(result.mime_type || "audio/mpeg") });
    const audioUrl = URL.createObjectURL(blob);
    const audio = new Audio(audioUrl);
    activeReplyAudio = audio;
    activeReplyAudioUrl = audioUrl;
    audio.onended = () => {
      if (activeReplyAudio !== audio) {
        return;
      }
      clearReplyAudioState();
      renderConversation(currentState?.messages || []);
    };
    audio.onerror = () => {
      if (activeReplyAudio !== audio) {
        return;
      }
      clearReplyAudioState();
      renderConversation(currentState?.messages || []);
      setStatus("Could not play Hermes TTS audio.", true);
    };
    await audio.play();
    const provider = String(result.provider || "").trim();
    setStatus(provider ? `Playing reply with Hermes TTS (${provider}).` : "Playing reply with Hermes TTS.");
  } catch (error) {
    clearReplyAudioState();
    renderConversation(currentState?.messages || []);
    setStatus(error?.message || "Could not generate Hermes TTS audio.", true);
  }
}

function updateActionAvailability() {
  const busy = Boolean(currentState?.progress?.running);
  selectedSessionCanSend = currentState?.can_send !== false;
  const canSendTurn = selectedSessionCanSend && !busy;
  sendButton.disabled = !canSendTurn;
  attachButton.disabled = !canSendTurn;
  screengrabButton.disabled = !canSendTurn;
  if (attachmentInput) {
    attachmentInput.disabled = !canSendTurn;
  }
  askInput.disabled = !selectedSessionCanSend;
  askInput.placeholder = selectedSessionCanSend
    ? "Ask Hermes to explain the latest failure, walk the tool calls, compare tooling, or propose the fix..."
    : "This session is read-only. Start a new browser chat to ask follow-up questions.";
  sendButton.textContent = busy ? "Working..." : "Send to Hermes";
  interruptButton.disabled = !(busy && selectedSessionCanSend);
  refreshInspectButton.disabled = inspectRefreshInFlight;
  quickActions.querySelectorAll("button").forEach((button) => {
    button.disabled = !canSendTurn;
  });
  if (voiceInputButton) {
    const voiceSupported = Boolean(chrome?.runtime?.id);
    const canUseVoice = Boolean(voiceSupported && selectedSessionCanSend && !busy && !voiceTranscriptionPending);
    voiceInputButton.disabled = !canUseVoice;
    voiceInputButton.classList.toggle("is-recording", voiceRecordingActive);
    voiceInputButton.classList.toggle("is-transcribing", voiceTranscriptionPending);
    voiceInputButton.textContent = voiceRecordingActive ? "Stop recording" : voiceTranscriptionPending ? "Transcribing..." : "Voice input";
  }
}

function applyStateSnapshot(result) {
  currentState = result || null;
  if (currentState?.session_key) {
    selectedSessionKey = String(currentState.session_key || "").trim();
  }
  ensureCurrentSessionListed();
  renderSessionOptions(selectedSessionKey);
  renderConversation(currentState?.messages || []);
  renderActivityLog(currentState?.progress || {});
  updateSessionMeta();
  updateModeText();
  renderStats();
  const detail = String(currentState?.progress?.detail || "").trim();
  const error = String(currentState?.progress?.error || "").trim();
  if (error) {
    setStatus(error, true);
  } else if (detail) {
    setStatus(detail, false);
  } else if (currentState?.browser_label) {
    setStatus(`Loaded ${currentState.browser_label}.`);
  } else {
    setStatus("Waiting for session activity.");
  }
  updateActionAvailability();
}

function makeInspectSignature(result = currentState) {
  const progress = result?.progress || {};
  const activityLog = Array.isArray(progress.activity_log) ? progress.activity_log : [];
  return [
    String(result?.session_id || ""),
    String(result?.session_key || ""),
    String(Array.isArray(result?.messages) ? result.messages.length : 0),
    progress.running ? "1" : "0",
    String(activityLog.length),
    String(progress.error || ""),
    String(progress.detail || "")
  ].join("|");
}

async function refreshInspect({ force = false } = {}) {
  if (inspectRefreshInFlight) {
    return;
  }
  const targetSessionKey = String(selectedSessionKey || currentState?.session_key || "").trim();
  const signature = makeInspectSignature(currentState);
  const now = Date.now();
  const minInterval = currentState?.progress?.running ? INSPECT_RUNNING_REFRESH_MS : INSPECT_IDLE_REFRESH_MS;
  if (!force && signature === lastInspectSignature && now - lastInspectRequestedAt < minInterval) {
    return;
  }

  inspectRefreshInFlight = true;
  lastInspectRequestedAt = now;
  lastInspectSignature = signature;
  updateActionAvailability();

  try {
    const response = await sendRuntimeMessage({
      type: "hermes:inspect-chat-session",
      sessionKey: targetSessionKey
    });
    currentInspection = response.result || {};
    renderInspectionPanels();
    updateModeText();
    updateSessionMeta();
    renderStats();
  } catch (error) {
    setStatus(error.message || String(error), true);
  } finally {
    inspectRefreshInFlight = false;
    updateActionAvailability();
  }
}

async function loadSessionState({ forceInspect = false } = {}) {
  if (stateRefreshInFlight) {
    return;
  }
  stateRefreshInFlight = true;
  try {
    const response = await sendRuntimeMessage({
      type: "hermes:get-chat-session",
      sessionKey: selectedSessionKey
    });
    applyStateSnapshot(response.result || {});
    await refreshInspect({ force: forceInspect });
  } catch (error) {
    setStatus(error.message || String(error), true);
  } finally {
    stateRefreshInFlight = false;
    updateActionAvailability();
  }
}

async function refreshSessionList({ preserveSelection = true, forceInspect = false } = {}) {
  const previousKey = String(selectedSessionKey || "").trim();
  const response = await sendRuntimeMessage({
    type: "hermes:list-chat-sessions",
    sessionKey: previousKey,
    limit: 40
  });
  const result = response.result || {};
  const sessions = Array.isArray(result.sessions) ? result.sessions : [];
  sessionHistoryByKey = new Map();
  for (const session of sessions) {
    const key = String(session.session_key || session.session_id || "").trim();
    if (!key) {
      continue;
    }
    sessionHistoryByKey.set(key, session);
  }
  lastSessionListRefreshAt = Date.now();
  const preferredKey = preserveSelection && previousKey ? previousKey : String(result.active_session_key || "").trim();
  selectedSessionKey = preferredKey;
  renderSessionOptions(preferredKey);
  selectedSessionKey = String(sessionSelect.value || "").trim();
  await loadSessionState({ forceInspect });
}

async function sendQuestion(messageOverride = "") {
  const message = String(messageOverride || askInput.value || "").trim();
  const attachments = buildAttachmentPayloads();
  if (!selectedSessionCanSend) {
    setStatus("This session is read-only. Start a new browser chat for interactive follow-up.", true);
    return;
  }
  if (!message && !attachments.length) {
    setStatus("Type a question or attach an image first.", true);
    askInput.focus();
    return;
  }
  setStatus("Queueing message to Hermes...");
  const response = await sendRuntimeMessage({
    type: "hermes:send-chat-message",
    message,
    sharePage: false,
    includeTranscript: false,
    sessionKey: selectedSessionKey,
    attachments
  });
  applyStateSnapshot(response.result || {});
  if (!messageOverride) {
    askInput.value = "";
  }
  clearPendingAttachments();
  await refreshSessionList({ preserveSelection: true, forceInspect: true });
}

async function loadRuntimeConfig() {
  const selectedProvider = runtimeConfig?.config?.model?.provider || "";
  const response = await sendRuntimeMessage({
    type: "hermes:get-runtime-config",
    selectedProvider
  });
  runtimeConfig = response.result || null;
  renderSettingsAndSource();
  renderPaths();
}

async function startNewBrowserChat() {
  setStatus("Starting a fresh browser sidecar session...");
  const response = await sendRuntimeMessage({
    type: "hermes:reset-chat-session",
    createNew: true
  });
  applyStateSnapshot(response.result || {});
  selectedSessionKey = String(response.result?.session_key || "").trim();
  currentInspection = null;
  renderInspectionPanels();
  await refreshSessionList({ preserveSelection: false, forceInspect: true });
}

async function interruptSelectedSession() {
  if (!selectedSessionCanSend || !currentState?.progress?.running) {
    return;
  }
  const response = await sendRuntimeMessage({
    type: "hermes:interrupt-chat-session",
    sessionKey: selectedSessionKey
  });
  applyStateSnapshot(response.result || {});
  await refreshInspect({ force: true });
}

async function refreshBridgeHealth() {
  try {
    const response = await sendRuntimeMessage({ type: "hermes:check-bridge-health" });
    const result = response.result || {};
    if (result.ok) {
      setBridgeStatus(`Bridge healthy on port ${result.port}.`);
    } else {
      setBridgeStatus("Bridge health returned an unexpected payload.", true);
    }
  } catch (error) {
    setBridgeStatus(error.message || String(error), true);
  }
}

async function loadSettings() {
  const response = await sendRuntimeMessage({ type: "hermes:get-settings" });
  extensionSettings = response.settings || {};
  window.HermesTheme?.applyThemeToDocument(extensionSettings);
  renderSettingsAndSource();
}

function renderQuickActionButtons() {
  quickActions.textContent = "";
  for (const action of QUICK_ACTIONS) {
    const button = document.createElement("button");
    button.className = "ghost-button quick-action-button";
    button.type = "button";
    button.textContent = action.label;
    button.title = action.prompt;
    button.addEventListener("click", () => {
      sendQuestion(action.prompt).catch((error) => setStatus(error.message || String(error), true));
    });
    quickActions.appendChild(button);
  }
}

function stopPolling() {
  if (pollTimer) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
}

function scheduleNextPoll() {
  stopPolling();
  if (!livePollToggle.checked) {
    return;
  }
  pollTimer = setTimeout(() => {
    pollCurrentSession().catch((error) => setStatus(error.message || String(error), true));
  }, AUTO_POLL_MS);
}

async function pollCurrentSession() {
  try {
    await loadSessionState({ forceInspect: false });
    if (Date.now() - lastSessionListRefreshAt >= SESSION_LIST_REFRESH_MS) {
      await refreshSessionList({ preserveSelection: true, forceInspect: false });
    }
  } finally {
    scheduleNextPoll();
  }
}

openOptionsButton?.addEventListener("click", () => chrome.runtime.openOptionsPage());
refreshAllButton?.addEventListener("click", () => {
  Promise.all([
    loadSettings(),
    loadRuntimeConfig(),
    refreshBridgeHealth(),
    refreshSessionList({ preserveSelection: true, forceInspect: true })
  ]).catch((error) => setStatus(error.message || String(error), true));
});
refreshSessionsButton?.addEventListener("click", () => refreshSessionList({ preserveSelection: true }).catch((error) => setStatus(error.message || String(error), true)));
refreshInspectButton?.addEventListener("click", () => refreshInspect({ force: true }).catch((error) => setStatus(error.message || String(error), true)));
newChatButton?.addEventListener("click", () => startNewBrowserChat().catch((error) => setStatus(error.message || String(error), true)));
interruptButton?.addEventListener("click", () => interruptSelectedSession().catch((error) => setStatus(error.message || String(error), true)));
sessionSelect?.addEventListener("change", () => {
  selectedSessionKey = String(sessionSelect.value || "").trim();
  currentInspection = null;
  renderInspectionPanels();
  loadSessionState({ forceInspect: true }).catch((error) => setStatus(error.message || String(error), true));
});
sendButton?.addEventListener("click", () => sendQuestion().catch((error) => setStatus(error.message || String(error), true)));
askInput?.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    sendQuestion().catch((error) => setStatus(error.message || String(error), true));
  }
});
livePollToggle?.addEventListener("change", () => livePollToggle.checked ? scheduleNextPoll() : stopPolling());
attachButton?.addEventListener("click", () => {
  if (attachmentInput && !attachmentInput.disabled) {
    attachmentInput.click();
  }
});
attachmentInput?.addEventListener("change", (event) => {
  const files = event.target instanceof HTMLInputElement ? event.target.files : null;
  addAttachmentFiles(files)
    .then(() => {
      const count = files?.length || 0;
      if (count) {
        setStatus(`Attached ${count} image${count === 1 ? "" : "s"}.`);
      }
    })
    .catch((error) => setStatus(error.message || String(error), true));
});
screengrabButton?.addEventListener("click", () => {
  setStatus("Capturing the current tab as an image attachment...");
  captureCurrentTabScreengrab()
    .then(() => setStatus("Attached a screen grab from the active tab."))
    .catch((error) => setStatus(error.message || String(error), true));
});
voiceInputButton?.addEventListener("click", () => toggleVoiceRecording().catch((error) => setStatus(error.message || String(error), true)));
voiceRecorderCloseButton?.addEventListener("click", () => setVoiceRecorderSheetVisible(false));

chrome.runtime.onMessage.addListener((message) => {
  if (message?.type !== "hermes:voice-input-broadcast") {
    return;
  }
  const payload = message.event && typeof message.event === "object" ? message.event : {};
  const type = String(payload.type || "");
  if (type === "recording") {
    voiceRecordingActive = true;
    voiceTranscriptionPending = false;
    setVoiceRecorderSheetVisible(true, "Recording in progress. Press Voice input again to stop.");
    updateActionAvailability();
    setStatus("Recording voice note from the control room...");
    return;
  }
  if (type === "transcribing") {
    voiceRecordingActive = false;
    voiceTranscriptionPending = true;
    setVoiceRecorderSheetVisible(true, "Voice note captured. Hermes is transcribing it now...");
    updateActionAvailability();
    setStatus("Uploading voice note to Hermes for transcription...");
    return;
  }
  if (type === "transcript") {
    voiceRecordingActive = false;
    voiceTranscriptionPending = false;
    setVoiceRecorderSheetVisible(false);
    appendTranscriptToComposer(payload.transcript || "");
    updateActionAvailability();
    setStatus("Voice note transcribed into the composer.");
    return;
  }
  if (type === "error") {
    voiceRecordingActive = false;
    voiceTranscriptionPending = false;
    setVoiceRecorderSheetVisible(true, String(payload.error || "Voice input failed."));
    updateActionAvailability();
    setStatus(String(payload.error || "Voice input failed."), true);
    return;
  }
  if (type === "closed") {
    voiceRecordingActive = false;
    voiceTranscriptionPending = false;
    setVoiceRecorderSheetVisible(false);
    updateActionAvailability();
  }
});

if (voiceInputChannel) {
  voiceInputChannel.addEventListener("message", (event) => {
    const payload = event?.data && typeof event.data === "object" ? event.data : {};
    const type = String(payload.type || "");
    if (type === "recording") {
      voiceRecordingActive = true;
      voiceTranscriptionPending = false;
      setVoiceRecorderSheetVisible(true, "Recording in progress. Press Voice input again to stop.");
      updateActionAvailability();
      setStatus("Recording voice note from the control room...");
    } else if (type === "error") {
      voiceRecordingActive = false;
      voiceTranscriptionPending = false;
      setVoiceRecorderSheetVisible(true, String(payload.error || "Voice input failed."));
      updateActionAvailability();
      setStatus(String(payload.error || "Voice input failed."), true);
    } else if (type === "closed") {
      voiceRecordingActive = false;
      voiceTranscriptionPending = false;
      setVoiceRecorderSheetVisible(false);
      updateActionAvailability();
    }
  });
}

chrome.storage?.onChanged?.addListener((changes, areaName) => {
  if (areaName === "sync" && changes && Object.keys(changes).length > 0) {
    loadSettings().catch(() => {});
  }
});

window.addEventListener("error", (event) => {
  if (isExtensionContextInvalidated(event?.error || event?.message)) {
    const message = explainExtensionError(event.error || event.message);
    setStatus(message, true);
    setBridgeStatus(message, true);
    event.preventDefault();
  }
});

window.addEventListener("unhandledrejection", (event) => {
  if (isExtensionContextInvalidated(event?.reason)) {
    const message = explainExtensionError(event.reason);
    setStatus(message, true);
    setBridgeStatus(message, true);
    event.preventDefault();
  }
});

renderQuickActionButtons();
renderInspectionPanels();
renderAttachmentStrip();

(async () => {
  try {
    await loadSettings();
    await loadRuntimeConfig();
    await refreshBridgeHealth();
    await refreshSessionList({ preserveSelection: false, forceInspect: true });
  } catch (error) {
    const message = error.message || String(error);
    setStatus(message, true);
    setBridgeStatus(message, true);
  } finally {
    scheduleNextPoll();
  }
})();
