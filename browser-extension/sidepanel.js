const AUTO_REFRESH_MS = 1000;

const pageTitle = document.getElementById("page-title");
const pageUrl = document.getElementById("page-url");
const contentKind = document.getElementById("content-kind");
const selectionLength = document.getElementById("selection-length");
const pageTextLength = document.getElementById("page-text-length");
const pdfImageStatus = document.getElementById("pdf-image-status");
const transcriptStatus = document.getElementById("transcript-status");
const enablePreviewPolling = document.getElementById("enable-preview-polling");
const chatMessages = document.getElementById("chat-messages");
const chatInput = document.getElementById("chat-input");
const sharePageCheckbox = document.getElementById("share-page");
const includeTranscript = document.getElementById("include-transcript");
const includeTranscriptLabel = document.getElementById("include-transcript-label");
const statusText = document.getElementById("status-text");
const domainPermissionStatus = document.getElementById("domain-permission-status");
const domainPermissionButton = document.getElementById("domain-permission-button");
const activityText = document.getElementById("activity-text");
const sendButton = document.getElementById("send-button");
const interruptButton = document.getElementById("interrupt-button");
const resetChatButton = document.getElementById("reset-chat-button");
const activityPanel = document.getElementById("activity-panel");
const sessionHistorySelect = document.getElementById("session-history-select");
const refreshSessionsButton = document.getElementById("refresh-sessions-button");
const sessionMetaText = document.getElementById("session-meta-text");
const copySessionIdButton = document.getElementById("copy-session-id-button");
const quickPromptsLabel = document.getElementById("quick-prompts-label");
const challengeModeButton = document.getElementById("challenge-mode-button");
const bundlePanel = document.getElementById("bundle-panel");
const bundleList = document.getElementById("bundle-list");
const bundleModeNote = document.getElementById("bundle-mode-note");
const presetStrip = document.getElementById("preset-strip");
const attachmentInput = document.getElementById("attachment-input");
const attachmentStrip = document.getElementById("attachment-strip");
const attachButton = document.getElementById("attach-button");
const screengrabButton = document.getElementById("screengrab-button");
const voiceInputButton = document.getElementById("voice-input-button");
const voiceRecorderSheet = document.getElementById("voice-recorder-sheet");
const voiceRecorderSheetStatus = document.getElementById("voice-recorder-sheet-status");
const voiceRecorderCloseButton = document.getElementById("voice-recorder-close-button");
const activityLogPanel = document.getElementById("activity-log-panel");
const activityLogPre = document.getElementById("activity-log-pre");
const activityLogBadge = document.getElementById("activity-log-badge");
const openControlRoomButton = document.getElementById("open-control-room-button");
const composer = chatInput?.closest(".composer");
const STATUS_INLINE_MAX_CHARS = 220;
const STATUS_INLINE_MAX_LINES = 3;
const STATUS_ACTIVITY_MAX_CHARS = 700;
const STATUS_ACTIVITY_MAX_LINES = 10;
const PROGRESS_DETAIL_MAX_CHARS = 140;
const PROGRESS_EVENT_MAX_CHARS = 110;
const CHAT_AUTO_SCROLL_THRESHOLD_PX = 56;
const MAX_IMAGE_ATTACHMENTS = 4;
const MAX_IMAGE_ATTACHMENT_BYTES = 10 * 1024 * 1024;
const SUPPORTED_IMAGE_MIME_TYPES = new Set([
  "image/png",
  "image/jpeg",
  "image/webp",
  "image/gif",
  "image/bmp"
]);

let activeTabId = null;
let pollTimer = null;
let previewTimer = null;
let refreshDebounceTimer = null;
let previewInFlight = false;
let currentMessages = [];
let currentProgress = null;
/** Last gateway progress payload (keeps activity_log after a turn finishes). */
let lastProgressSnapshot = null;
let pendingUserMessage = null;
let lastPreview = null;
let sharePageByDefault = true;
let isBusy = false;
let interruptRequested = false;
let pendingQueuedAt = 0;
let expectedSessionKey = "";
let selectedSessionKey = "";
let isApplyingSessionSelection = false;
let pageContextUnavailable = false;
let latestDomainPermission = null;
let challengeModeEnabled = false;
let bundleSelectionState = null;
let activeAudioMessageKey = "";
let activeReplyAudio = null;
let activeReplyAudioUrl = "";
let pendingAttachments = [];
let previewPollingEnabled = false;
let voiceRecordingActive = false;
let voiceTranscriptionPending = false;
const voiceInputChannel = typeof BroadcastChannel !== "undefined"
  ? new BroadcastChannel("hermes-sidecar-voice-input")
  : null;
let sidebarSettings = {
  showQuickPrompts: false,
  showChallengeMode: false,
  enablePreviewPolling: false,
  quickPrompts: [],
  challengeModeLabel: "Challenge my framing",
  challengeModePrompt: "",
  themeName: window.HermesTheme?.defaultThemeId || "obsidian",
  customThemeAccent: "#9ca3af",
  customThemes: [],
  sidecarActivityLogLevel: "normal",
  activityLogPanelOpen: false
};

window.HermesTheme?.applyThemeToDocument({
  themeName: window.HermesTheme?.defaultThemeId || "obsidian"
});
let sessionHistoryByKey = new Map();
let selectedSessionCanSend = true;
let extensionContextInvalidated = false;

function compactStatusText(
  message,
  {
    maxChars = STATUS_INLINE_MAX_CHARS,
    maxLines = STATUS_INLINE_MAX_LINES,
    perLineMax = 120
  } = {}
) {
  const normalized = String(message || "")
    .replace(/\r\n/g, "\n")
    .replace(/\r/g, "\n")
    .replace(/\t/g, " ")
    .trim();
  if (!normalized) {
    return "";
  }

  const lines = normalized
    .split("\n")
    .map((line) => line.replace(/\s+/g, " ").trim())
    .filter(Boolean)
    .slice(0, maxLines)
    .map((line) => {
      if (line.length > perLineMax) {
        return `${line.slice(0, perLineMax - 1).trimEnd()}...`;
      }
      return line;
    });

  let compacted = lines.join("\n");
  if (compacted.length > maxChars) {
    compacted = `${compacted.slice(0, maxChars - 1).trimEnd()}...`;
  }
  return compacted;
}

function summarizeToolLine(line) {
  const text = String(line || "").trim();
  if (!text) {
    return "";
  }

  const callMatch = text.match(/\bCALL\s+([A-Za-z0-9_.-]+)/i);
  if (callMatch) {
    return `CALL ${callMatch[1]}`;
  }

  const runMatch = text.match(/\bRUN\s+([A-Za-z0-9_.-]+)/i);
  if (runMatch) {
    return `RUN ${runMatch[1]}`;
  }

  return "";
}

function summarizeStatusMessage(message, fallback = "Working...") {
  const normalized = String(message || "").replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  const lines = normalized.split("\n").map((line) => line.trim()).filter(Boolean);
  if (!lines.length) {
    return fallback;
  }

  const toolSummaries = [];
  for (const line of lines) {
    const summary = summarizeToolLine(line);
    if (summary && !toolSummaries.includes(summary)) {
      toolSummaries.push(summary);
    }
  }
  if (toolSummaries.length) {
    return `Working: ${toolSummaries.slice(0, 3).join(", ")}`;
  }

  const firstLine = lines[0];
  if (/[{[]/.test(firstLine) && firstLine.length > 80) {
    return fallback;
  }
  return firstLine;
}

function sliceActivityLogEvents(events, level) {
  const list = Array.isArray(events) ? events.slice() : [];
  const normalized = String(level || "normal").toLowerCase();
  if (normalized === "minimal") {
    return list.slice(-8);
  }
  if (normalized === "verbose") {
    return list;
  }
  return list.slice(-40);
}

function syncActivityLogUi() {
  if (!activityLogPre) {
    return;
  }
  const progress = lastProgressSnapshot;
  const raw = progress?.activity_log || progress?.recent_events || [];
  const level = String(sidebarSettings.sidecarActivityLogLevel || "normal").toLowerCase();
  const lines = sliceActivityLogEvents(raw, level);
  activityLogPre.textContent = lines.length
    ? lines.join("\n")
    : "(No gateway activity captured for this session yet.)";
  if (activityLogBadge) {
    const total = raw.length;
    const shown = lines.length;
    activityLogBadge.textContent = total === 0 ? "0 lines" : `${shown}/${total} lines`;
  }
}

function setStatus(message, { openActivity = false } = {}) {
  const summarized = summarizeStatusMessage(message, "Waiting for input.");
  const inlineMessage = compactStatusText(summarized, {
    maxChars: STATUS_INLINE_MAX_CHARS,
    maxLines: 2,
    perLineMax: 110
  }) || "Waiting for input.";
  const activityMessage = compactStatusText(message, {
    maxChars: STATUS_ACTIVITY_MAX_CHARS,
    maxLines: STATUS_ACTIVITY_MAX_LINES,
    perLineMax: 180
  }) || "Waiting for input.";

  statusText.textContent = inlineMessage;
  if (activityText) {
    activityText.textContent = activityMessage;
  }
  if (openActivity && activityPanel) {
    activityPanel.open = true;
  }
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

function rerenderCurrentMessages() {
  renderMessages(currentMessages, currentProgress, pendingUserMessage);
}

function updateComposerAvailability() {
  const canSend = !isBusy && selectedSessionCanSend;
  const canInterrupt = Boolean(interruptButton) && isBusy && selectedSessionCanSend;
  sendButton.disabled = !canSend;
  chatInput.disabled = !selectedSessionCanSend;
  if (attachButton) {
    attachButton.disabled = !canSend;
  }
  if (screengrabButton) {
    screengrabButton.disabled = !canSend;
  }
  if (attachmentInput) {
    attachmentInput.disabled = !canSend;
  }
  if (voiceInputButton) {
    const voiceSupported = Boolean(chrome?.runtime?.id);
    const canUseVoice = Boolean(voiceSupported && selectedSessionCanSend && !isBusy && !voiceTranscriptionPending);
    voiceInputButton.disabled = !canUseVoice;
    voiceInputButton.classList.toggle("is-recording", voiceRecordingActive);
    voiceInputButton.classList.toggle("is-transcribing", voiceTranscriptionPending);
    voiceInputButton.textContent = voiceRecordingActive
      ? "Stop recording"
      : voiceTranscriptionPending
        ? "Transcribing..."
        : "Voice input";
    voiceInputButton.title = !voiceSupported
      ? "Voice input is not supported in this browser."
      : voiceRecordingActive
        ? "Stop recording and transcribe this voice note"
      : voiceTranscriptionPending
        ? "Hermes is transcribing your voice note"
      : selectedSessionCanSend
        ? "Record a short voice note directly from the sidecar"
        : "This session is read-only in the side panel";
  }
  sendButton.textContent = isBusy ? "Working..." : "Send";
  sendButton.title = isBusy
    ? "Hermes is working on the current turn"
    : selectedSessionCanSend
      ? "Send your current message to Hermes"
      : "This session is read-only in the side panel";
  chatInput.placeholder = selectedSessionCanSend
    ? "Ask Hermes something, or leave this empty and just share the page..."
    : "Browsing a non-sidecar Hermes session. Select a browser sidecar session or start a new chat to send.";
  if (interruptButton) {
    interruptButton.hidden = !canInterrupt;
    interruptButton.disabled = !canInterrupt || interruptRequested;
    interruptButton.textContent = interruptRequested ? "Stopping..." : "Interrupt";
    interruptButton.title = !canInterrupt
      ? "Interrupt is only available while Hermes is working on this browser sidecar session"
      : interruptRequested
        ? "Interrupt already requested for the current turn"
        : "Ask Hermes to stop the current response chain";
  }
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
  if (!text || !chatInput) {
    return;
  }
  const current = String(chatInput.value || "").trim();
  chatInput.value = current ? `${current}\n\n${text}` : text;
  chatInput.focus();
  chatInput.setSelectionRange(chatInput.value.length, chatInput.value.length);
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
    setStatus("Stopping voice recording...", { openActivity: true });
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
    updateComposerAvailability();
    setStatus("Starting voice recording...", { openActivity: true });
  } catch (error) {
    voiceRecordingActive = false;
    voiceTranscriptionPending = false;
    updateComposerAvailability();
    const message = String(error?.message || error);
    setVoiceRecorderSheetVisible(true, message);
    throw error;
  }
}

function clearReplyAudioState() {
  if (activeReplyAudio) {
    try {
      activeReplyAudio.pause();
    } catch (_error) {
      // Ignore pause failures during cleanup.
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

function stopReplySpeech({ rerender = true } = {}) {
  clearReplyAudioState();
  if (rerender) {
    rerenderCurrentMessages();
  }
}

function decodeBase64Audio(base64Text) {
  const binary = atob(String(base64Text || ""));
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

async function speakReply(message, options = {}) {
  const text = String(options.text || getMessageText(message)).trim();
  if (!text) {
    return;
  }

  const nextKey = String(options.audioKey || messageKey(message)).trim() || messageKey(message);
  if (activeAudioMessageKey === nextKey) {
    stopReplySpeech();
    return;
  }

  clearReplyAudioState();
  activeAudioMessageKey = nextKey;
  rerenderCurrentMessages();
  setStatus("Generating reply audio with Hermes TTS...");

  try {
    const response = await sendRuntimeMessage({
      type: "hermes:speak-chat-message",
      text
    });
    const result = response.result || {};
    const audioBase64 = String(result.audio_base64 || "");
    if (!audioBase64) {
      throw new Error("Hermes TTS did not return any audio.");
    }

    const mimeType = String(result.mime_type || "audio/mpeg");
    const audioBytes = decodeBase64Audio(audioBase64);
    const blob = new Blob([audioBytes], { type: mimeType });
    const audioUrl = URL.createObjectURL(blob);
    const audio = new Audio(audioUrl);

    activeReplyAudio = audio;
    activeReplyAudioUrl = audioUrl;
    audio.onended = () => {
      if (activeReplyAudio !== audio) {
        return;
      }
      clearReplyAudioState();
      rerenderCurrentMessages();
    };
    audio.onerror = () => {
      if (activeReplyAudio !== audio) {
        return;
      }
      clearReplyAudioState();
      rerenderCurrentMessages();
      setStatus("Could not play Hermes TTS audio.");
    };

    await audio.play();
    const provider = String(result.provider || "").trim();
    setStatus(
      provider
        ? `Playing reply with Hermes TTS (${provider}).`
        : "Playing reply with Hermes TTS."
    );
  } catch (error) {
    clearReplyAudioState();
    rerenderCurrentMessages();
    setStatus(error?.message || "Could not generate Hermes TTS audio.", { openActivity: true });
  }
}

function formatCharCount(value) {
  const count = Number(value || 0);
  if (!count) {
    return "0 chars";
  }
  return `${count.toLocaleString()} chars`;
}

function createDefaultBundleSelectionState(preview) {
  const chunks = preview?.bundle?.chunks || {};
  return {
    includeTitle: Boolean(chunks.title?.includedByDefault),
    includeUrl: Boolean(chunks.url?.includedByDefault),
    includeMetadata: Boolean(chunks.metadata?.includedByDefault),
    includeSelection: Boolean(chunks.selection?.includedByDefault),
    includeDiscordThreadText: Boolean(chunks.discordThreadText?.includedByDefault),
    includeDiscordThreadImages: Boolean(chunks.discordThreadImages?.includedByDefault),
    includePageText: Boolean(chunks.pageText?.includedByDefault)
  };
}

function syncBundleSelectionState(preview, { preserveExisting = false } = {}) {
  const defaults = createDefaultBundleSelectionState(preview);
  if (!preserveExisting || !bundleSelectionState) {
    bundleSelectionState = defaults;
    return;
  }
  bundleSelectionState = {
    includeTitle: preview?.bundle?.chunks?.title?.available
      ? bundleSelectionState.includeTitle !== false
      : defaults.includeTitle,
    includeUrl: preview?.bundle?.chunks?.url?.available
      ? bundleSelectionState.includeUrl !== false
      : defaults.includeUrl,
    includeMetadata: preview?.bundle?.chunks?.metadata?.available
      ? bundleSelectionState.includeMetadata !== false
      : defaults.includeMetadata,
    includeSelection: preview?.bundle?.chunks?.selection?.available
      ? bundleSelectionState.includeSelection !== false
      : defaults.includeSelection,
    includeDiscordThreadText: preview?.bundle?.chunks?.discordThreadText?.available
      ? bundleSelectionState.includeDiscordThreadText !== false
      : defaults.includeDiscordThreadText,
    includeDiscordThreadImages: preview?.bundle?.chunks?.discordThreadImages?.available
      ? bundleSelectionState.includeDiscordThreadImages !== false
      : defaults.includeDiscordThreadImages,
    includePageText: preview?.bundle?.chunks?.pageText?.available
      ? bundleSelectionState.includePageText !== false
      : defaults.includePageText
  };
}

function buildOutgoingMessage(message, { skipChallengeMode = false } = {}) {
  const userMessage = String(message || "").trim();
  if (skipChallengeMode || !challengeModeEnabled) {
    return userMessage;
  }
  const challengeInstruction = String(sidebarSettings.challengeModePrompt || "").trim();
  if (!challengeInstruction) {
    return userMessage;
  }
  if (!userMessage) {
    return challengeInstruction;
  }
  return `${userMessage}\n\n${challengeInstruction}`;
}

function getContextOptionsForSend() {
  const state = bundleSelectionState || createDefaultBundleSelectionState(lastPreview);
  return {
    includeTitle: state.includeTitle !== false,
    includeUrl: state.includeUrl !== false,
    includeMetadata: state.includeMetadata !== false,
    includeSelection: state.includeSelection !== false,
    includeDiscordThreadText: state.includeDiscordThreadText !== false,
    includeDiscordThreadImages: state.includeDiscordThreadImages !== false,
    includePageText: state.includePageText !== false
  };
}

function listEnabledBundleChunks() {
  const chunks = lastPreview?.bundle?.chunks || {};
  const enabled = [];
  const state = getContextOptionsForSend();
  if (chunks.title?.available && state.includeTitle) {
    enabled.push(chunks.title.label || "Title");
  }
  if (chunks.url?.available && state.includeUrl) {
    enabled.push(chunks.url.label || "URL");
  }
  if (chunks.metadata?.available && state.includeMetadata) {
    enabled.push(chunks.metadata.label || "Metadata");
  }
  if (chunks.selection?.available && state.includeSelection) {
    enabled.push(chunks.selection.label || "Selected text");
  }
  if (chunks.discordThreadText?.available && state.includeDiscordThreadText) {
    enabled.push(chunks.discordThreadText.label || "Discord thread");
  }
  if (chunks.discordThreadImages?.available && state.includeDiscordThreadImages) {
    enabled.push(chunks.discordThreadImages.label || "Discord thread images");
  }
  if (chunks.pageText?.available && state.includePageText) {
    enabled.push(chunks.pageText.label || "Page text");
  }
  if (chunks.pdfImages?.available) {
    enabled.push(chunks.pdfImages.label || "PDF page images");
  }
  if (chunks.transcript?.available && includeTranscript.checked && !includeTranscript.disabled) {
    enabled.push(chunks.transcript.label || "YouTube transcript");
  }
  return enabled;
}

function renderPdfImageStatus(result) {
  if (!pdfImageStatus) {
    return;
  }
  const contentKindValue = String(result?.contentKind || "").trim();
  const imageCount = Math.max(
    0,
    Number(result?.pdfPreviewImageCount || result?.bundle?.chunks?.pdfImages?.length || 0)
  );
  if (imageCount > 0) {
    pdfImageStatus.hidden = false;
    pdfImageStatus.textContent = `${imageCount} PDF image${imageCount === 1 ? "" : "s"}`;
    return;
  }
  if (contentKindValue === "pdf-document" || contentKindValue === "pdf-embed") {
    pdfImageStatus.hidden = false;
    pdfImageStatus.textContent = "PDF images auto";
    return;
  }
  pdfImageStatus.hidden = true;
  pdfImageStatus.textContent = "0 PDF images";
}

function applyPresetTemplate(template) {
  const value = String(template || "").trim();
  if (!value) {
    return;
  }
  const current = chatInput.value.trim();
  chatInput.value = current ? `${current}\n\n${value}` : value;
  chatInput.focus();
  chatInput.setSelectionRange(chatInput.value.length, chatInput.value.length);
}

function formatAttachmentSize(size) {
  const bytes = Number(size || 0);
  if (!bytes) {
    return "0 B";
  }
  if (bytes >= 1024 * 1024) {
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }
  if (bytes >= 1024) {
    return `${Math.round(bytes / 1024)} KB`;
  }
  return `${bytes} B`;
}

function getMessageImages(message) {
  return Array.isArray(message?.images)
    ? message.images.filter((image) => image && typeof image === "object" && image.media_url)
    : [];
}

function getAttachmentPreviewImages() {
  return pendingAttachments.map((attachment) => ({
    source: "local",
    // Use data_url so optimistic message previews survive composer cleanup.
    // previewUrl values are object URLs revoked after successful send.
    media_url: attachment.data_url || attachment.previewUrl,
    mime_type: attachment.mime_type,
    alt_text: attachment.name,
    local_path: ""
  }));
}

function renderAttachmentStrip() {
  if (!attachmentStrip) {
    return;
  }
  attachmentStrip.textContent = "";
  attachmentStrip.hidden = pendingAttachments.length === 0;
  if (!pendingAttachments.length) {
    return;
  }

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
}

function estimateDataUrlByteLength(dataUrl) {
  const value = String(dataUrl || "");
  const marker = "base64,";
  const index = value.indexOf(marker);
  if (index === -1) {
    return 0;
  }
  const base64 = value.slice(index + marker.length);
  if (!base64) {
    return 0;
  }
  const paddingMatch = base64.match(/=+$/);
  const paddingLength = paddingMatch ? paddingMatch[0].length : 0;
  return Math.max(0, Math.floor((base64.length * 3) / 4) - paddingLength);
}

function addAttachmentDataUrl({
  dataUrl,
  name = "image.png",
  mimeType = "image/png",
  sizeBytes = 0
}) {
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
}

async function captureCurrentTabScreengrab() {
  const tab = await getActiveTab();
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

function buildAttachmentPayloads() {
  return pendingAttachments.map((attachment) => ({
    name: attachment.name,
    mime_type: attachment.mime_type,
    size_bytes: attachment.size_bytes,
    data_url: attachment.data_url
  }));
}

function renderMessageImages(bubble, message) {
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
  bubble.appendChild(gallery);
}

function renderPromptControls() {
  const prompts = Array.isArray(sidebarSettings.quickPrompts) ? sidebarSettings.quickPrompts : [];
  const showQuickPrompts = sidebarSettings.showQuickPrompts && prompts.length > 0;
  const showChallengeMode =
    sidebarSettings.showChallengeMode &&
    String(sidebarSettings.challengeModePrompt || "").trim().length > 0;

  if (quickPromptsLabel) {
    quickPromptsLabel.hidden = !showQuickPrompts;
    quickPromptsLabel.style.display = showQuickPrompts ? "" : "none";
  }

  if (challengeModeButton) {
    challengeModeButton.hidden = !showChallengeMode;
    challengeModeButton.style.display = showChallengeMode ? "" : "none";
    challengeModeButton.textContent = String(sidebarSettings.challengeModeLabel || "").trim() || "Challenge mode";
    challengeModeButton.title = showChallengeMode
      ? `Toggle "${challengeModeButton.textContent}" for this turn`
      : "Challenge mode is disabled in Options";
  }

  if (presetStrip) {
    presetStrip.hidden = !showQuickPrompts;
    presetStrip.style.display = showQuickPrompts ? "" : "none";
    presetStrip.textContent = "";
    if (showQuickPrompts) {
      for (const prompt of prompts) {
        const button = document.createElement("button");
        button.className = "ghost-button chip-button preset-button";
        button.type = "button";
        button.dataset.template = String(prompt.template || "").trim();
        button.dataset.includeTranscript = prompt.includeTranscript ? "true" : "false";
        button.title = button.dataset.template || "Send this saved prompt";
        button.textContent = String(prompt.label || "").trim() || "Quick prompt";
        presetStrip.appendChild(button);
      }
    }
  }

  if (!showChallengeMode) {
    setChallengeModeEnabled(false);
  } else if (challengeModeButton) {
    challengeModeButton.setAttribute("aria-pressed", challengeModeEnabled ? "true" : "false");
  }
}

function setChallengeModeEnabled(enabled) {
  challengeModeEnabled =
    Boolean(enabled) &&
    sidebarSettings.showChallengeMode === true &&
    String(sidebarSettings.challengeModePrompt || "").trim().length > 0;
  if (challengeModeButton) {
    challengeModeButton.setAttribute("aria-pressed", challengeModeEnabled ? "true" : "false");
  }
  if (bundleModeNote) {
    bundleModeNote.hidden = !challengeModeEnabled;
  }
}

function applySidebarSettings(settings, { preserveBusyState = false } = {}) {
  const nextSettings = settings && typeof settings === "object" ? settings : {};
  sidebarSettings = {
    showQuickPrompts: nextSettings.showQuickPrompts === true,
    showChallengeMode: nextSettings.showChallengeMode === true,
    enablePreviewPolling: nextSettings.enablePreviewPolling === true,
    quickPrompts: Array.isArray(nextSettings.quickPrompts) ? nextSettings.quickPrompts : [],
    challengeModeLabel: String(nextSettings.challengeModeLabel || "").trim() || "Challenge my framing",
    challengeModePrompt: String(nextSettings.challengeModePrompt || "").trim(),
    themeName:
      String(nextSettings.themeName || window.HermesTheme?.defaultThemeId || "obsidian").trim().toLowerCase() ||
      window.HermesTheme?.defaultThemeId ||
      "obsidian",
    customThemeAccent: String(nextSettings.customThemeAccent || "#9ca3af").trim() || "#9ca3af",
    customThemes: Array.isArray(nextSettings.customThemes) ? nextSettings.customThemes : [],
    sidecarActivityLogLevel: (() => {
      const raw = String(nextSettings.sidecarActivityLogLevel || "normal").toLowerCase();
      if (raw === "minimal" || raw === "verbose") {
        return raw;
      }
      return "normal";
    })(),
    activityLogPanelOpen: nextSettings.activityLogPanelOpen === true
  };

  if (activityLogPanel) {
    activityLogPanel.open = sidebarSettings.activityLogPanelOpen === true;
  }
  syncActivityLogUi();

  window.HermesTheme?.applyThemeToDocument(sidebarSettings);
  includeTranscript.checked = nextSettings.includeTranscriptByDefault !== false;
  sharePageByDefault = nextSettings.sharePageByDefault !== false;
  previewPollingEnabled = sidebarSettings.enablePreviewPolling === true;
  syncPreviewPollingUi();
  if ((!preserveBusyState || !isBusy) && !pageContextUnavailable && !sharePageCheckbox.disabled) {
    sharePageCheckbox.checked = sharePageByDefault;
  }

  if (previewPollingEnabled) {
    startPreviewLoop();
  } else {
    stopPreviewLoop();
  }

  renderPromptControls();
  renderContextBundle();
  updateComposerAvailability();
}

function renderContextBundle() {
  if (!bundlePanel || !bundleList) {
    return;
  }
  const preview = lastPreview;
  const chunks = preview?.bundle?.chunks || {};
  const shouldShow = Boolean(
    sharePageCheckbox.checked &&
    !pageContextUnavailable &&
    preview &&
    Object.values(chunks).some((chunk) => chunk?.available)
  );

  const wasHidden = bundlePanel.hidden;
  bundlePanel.hidden = !shouldShow;
  bundleList.textContent = "";
  if (bundleModeNote) {
    bundleModeNote.hidden = !challengeModeEnabled;
  }
  if (!shouldShow) {
    return;
  }
  if (wasHidden) {
    bundlePanel.open = true;
  }

  const rows = [
    { chunkKey: "title", stateKey: "includeTitle" },
    { chunkKey: "url", stateKey: "includeUrl" },
    { chunkKey: "metadata", stateKey: "includeMetadata" },
    { chunkKey: "selection", stateKey: "includeSelection" },
    { chunkKey: "discordThreadText", stateKey: "includeDiscordThreadText" },
    { chunkKey: "discordThreadImages", stateKey: "includeDiscordThreadImages" },
    { chunkKey: "pageText", stateKey: "includePageText" },
    { chunkKey: "pdfImages", stateKey: null },
    { chunkKey: "transcript", stateKey: null }
  ];

  for (const row of rows) {
    const chunk = chunks[row.chunkKey];
    if (!chunk?.available) {
      continue;
    }

    const wrapper = document.createElement("label");
    wrapper.className = "bundle-row";

    const toggle = document.createElement("input");
    toggle.className = "bundle-toggle";
    toggle.type = "checkbox";

    let checked = false;
    let disabled = false;
    if (row.chunkKey === "transcript") {
      checked = includeTranscript.checked && !includeTranscript.disabled;
      disabled = includeTranscript.disabled;
      toggle.addEventListener("change", () => {
        includeTranscript.checked = toggle.checked;
        renderContextBundle();
      });
    } else if (row.stateKey === null) {
      checked = true;
      disabled = true;
    } else {
      checked = bundleSelectionState?.[row.stateKey] !== false;
      toggle.addEventListener("change", () => {
        bundleSelectionState = {
          ...(bundleSelectionState || createDefaultBundleSelectionState(preview)),
          [row.stateKey]: toggle.checked
        };
        renderContextBundle();
      });
    }
    toggle.checked = checked;
    toggle.disabled = disabled;
    wrapper.classList.toggle("is-disabled", !checked || disabled);
    wrapper.appendChild(toggle);

    const body = document.createElement("div");
    body.className = "bundle-body";

    const top = document.createElement("div");
    top.className = "bundle-row-top";

    const label = document.createElement("span");
    label.className = "bundle-label";
    label.textContent = chunk.label || row.chunkKey;
    top.appendChild(label);

    const metric = document.createElement("span");
    metric.className = "bundle-metric";
    metric.textContent = chunk.metricText || formatCharCount(chunk.length || 0);
    top.appendChild(metric);
    body.appendChild(top);

    if (chunk.preview) {
      const previewText = document.createElement("p");
      previewText.className = "bundle-preview";
      previewText.textContent = chunk.preview;
      body.appendChild(previewText);
    }

    if (chunk.reason) {
      const reason = document.createElement("p");
      reason.className = "bundle-reason";
      reason.textContent = chunk.reason;
      body.appendChild(reason);
    }

    wrapper.appendChild(body);
    bundleList.appendChild(wrapper);
  }
}

function renderChatNotice(message) {
  chatMessages.textContent = "";
  const empty = document.createElement("div");
  empty.className = "empty-state";
  empty.textContent = message;
  chatMessages.appendChild(empty);
}

function setBusyState(busy) {
  isBusy = busy;
  if (!busy) {
    interruptRequested = false;
  }
  resetChatButton.disabled = busy;
  if (sessionHistorySelect) {
    sessionHistorySelect.disabled = busy;
  }
  if (refreshSessionsButton) {
    refreshSessionsButton.disabled = busy;
  }
  updateComposerAvailability();
}

function stopPolling() {
  if (pollTimer) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
}

function stopPreviewLoop() {
  if (previewTimer) {
    clearInterval(previewTimer);
    previewTimer = null;
  }
}

function isExtensionContextInvalidated(error) {
  const message = String(error?.message || error || "").toLowerCase();
  return (
    message.includes("extension context invalidated") ||
    message.includes("context invalidated") ||
    message.includes("message port closed before a response was received")
  );
}

function isExtensionsPolicyBlockedMessage(error) {
  const message = String(error?.message || error || "").toLowerCase();
  return (
    message.includes("cannot be scripted due to an extensionssettings policy") ||
    message.includes("cannot be scripted because the browser blocked extension injection with an extensionssettings policy") ||
    message.includes("extensionssettings policy")
  );
}

function getExtensionContextInvalidatedMessage() {
  return (
    "Hermes Sidecar was reloaded or updated. Reload the side panel to reconnect."
  );
}

function handleObservedPageRouteChange(message, sender = {}) {
  const senderTabId = Number(sender?.tab?.id || 0);
  const senderTabActive = sender?.tab?.active === true;
  if (senderTabId) {
    if (activeTabId && senderTabId !== activeTabId && !senderTabActive) {
      return;
    }
    activeTabId = senderTabId;
  }

  const nextUrl = String(message?.url || sender?.tab?.url || "").trim();
  if (!nextUrl) {
    return;
  }

  if (lastPreview?.url && String(lastPreview.url).trim() !== nextUrl && previewPollingEnabled) {
    setStatus("Detected in-page navigation. Refreshing current page preview...");
  }

  scheduleRefresh();
}

function handleExtensionContextInvalidated(error, { openActivity = true } = {}) {
  const message = getExtensionContextInvalidatedMessage(error);
  extensionContextInvalidated = true;
  stopPolling();
  stopPreviewLoop();
  previewInFlight = false;
  pendingUserMessage = null;
  isBusy = false;
  selectedSessionCanSend = false;
  updateComposerAvailability();
  renderUnavailablePreview(message);
  renderChatNotice("Reload the Hermes side panel to resume this browser-side session.");
  if (sessionHistorySelect) {
    sessionHistorySelect.disabled = true;
  }
  if (refreshSessionsButton) {
    refreshSessionsButton.disabled = true;
  }
  if (resetChatButton) {
    resetChatButton.disabled = true;
  }
  renderDomainPermissionStatus({
    supported: false,
    detail: message
  });
  setStatus(message, { openActivity });
  return message;
}

function schedulePolling() {
  stopPolling();
  pollTimer = setTimeout(() => {
    loadChatSession({ quiet: true }).catch((error) => {
      setStatus(error.message || String(error), { openActivity: true });
      stopPolling();
      setBusyState(false);
    });
  }, 900);
}

function startPreviewLoop() {
  if (extensionContextInvalidated || !previewPollingEnabled) {
    stopPreviewLoop();
    return;
  }
  stopPreviewLoop();
  previewTimer = setInterval(() => {
    refreshPreview({ quiet: true }).catch((error) => {
      const message = isExtensionContextInvalidated(error)
        ? handleExtensionContextInvalidated(error)
        : explainBackgroundMismatch(error);
      setStatus(message, { openActivity: true });
    });
  }, AUTO_REFRESH_MS);
}

function syncPreviewPollingUi() {
  if (!enablePreviewPolling) {
    return;
  }
  enablePreviewPolling.checked = previewPollingEnabled;
  enablePreviewPolling.disabled = extensionContextInvalidated;
}

async function setPreviewPollingEnabled(enabled, { persist = true, quiet = false } = {}) {
  previewPollingEnabled = Boolean(enabled);
  syncPreviewPollingUi();

  if (previewPollingEnabled) {
    startPreviewLoop();
    if (!quiet) {
      setStatus("Site polling enabled. Hermes will keep refreshing the current page preview.", { openActivity: true });
    }
  } else {
    stopPreviewLoop();
    if (!quiet) {
      setStatus("Site polling disabled. Use Refresh now when you want a one-shot page update.");
    }
  }

  if (!persist) {
    return;
  }

  await sendRuntimeMessage({
    type: "hermes:save-settings",
    settings: {
      enablePreviewPolling: previewPollingEnabled
    }
  });
}

async function sendRuntimeMessage(payload) {
  if (extensionContextInvalidated) {
    throw new Error(getExtensionContextInvalidatedMessage());
  }

  let response;
  try {
    response = await chrome.runtime.sendMessage(payload);
  } catch (error) {
    if (isExtensionContextInvalidated(error)) {
      throw new Error(handleExtensionContextInvalidated(error));
    }
    throw error;
  }

  if (!response?.ok) {
    const responseError = response?.error || "Unknown extension error.";
    if (isExtensionContextInvalidated(responseError)) {
      throw new Error(handleExtensionContextInvalidated(responseError));
    }
    throw new Error(responseError);
  }
  return response;
}

function explainBackgroundMismatch(error) {
  const message = String(error?.message || error || "");
  const lower = message.toLowerCase();
  if (isExtensionContextInvalidated(error)) {
    return getExtensionContextInvalidatedMessage();
  }
  if (message.includes("Unknown message type")) {
    return (
      "This side panel is talking to an older Hermes extension worker. " +
      "Reload the unpacked extension in chrome://extensions, then close and reopen the side panel."
    );
  }
  if (
    lower.includes("cannot access contents of url") ||
    lower.includes("browser-internal tab") ||
    lower.includes("internal browser page") ||
    lower.includes("only http/https pages can be shared")
  ) {
    return (
      "This tab cannot be shared with Hermes (only http/https pages are supported). " +
      "Switch to a normal website tab, or uncheck \"Use the current page in this turn\"."
    );
  }
  if (
    lower.includes("receiving end does not exist") ||
    lower.includes("could not establish connection")
  ) {
    return (
      "This tab is using an old or unavailable Hermes page bridge. " +
      "Reload this tab and try again."
    );
  }
  if (lower.includes("capturevisibletab")) {
    return (
      "Hermes could not capture the visible tab image. " +
      "Make sure the page is visible in the current window and try again."
    );
  }
  return message;
}

function getNonHttpProtocol(rawUrl) {
  const value = String(rawUrl || "").trim();
  if (!value) {
    return "";
  }
  try {
    const parsed = new URL(value);
    if (["http:", "https:"].includes(parsed.protocol)) {
      return "";
    }
    return parsed.protocol || "unknown";
  } catch (_error) {
    return "";
  }
}

async function getActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id) {
    throw new Error("No active tab found.");
  }
  activeTabId = tab.id;
  return tab;
}

function formatTimestamp(value) {
  if (!value) {
    return "";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "";
  }
  return date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function formatSessionLabel(session) {
  const title =
    session?.browser_label ||
    session?.session_label ||
    session?.display_name ||
    (session?.source ? String(session.source).replace(/_/g, " ") : "") ||
    "Hermes session";
  const updatedAt = session?.updated_at ? formatTimestamp(session.updated_at) : "";
  const rawCount = Number(session?.message_count);
  const messageCount = Number.isFinite(rawCount) ? rawCount : null;
  const running = session?.running ? " [Working]" : "";
  const countPart = messageCount === null ? "" : ` (${messageCount})`;
  const updatedPart = updatedAt ? ` \u00b7 ${updatedAt}` : "";
  return `${title}${countPart}${updatedPart}${running}`;
}

function getSelectedSessionInfo() {
  if (!selectedSessionKey) {
    return null;
  }
  return sessionHistoryByKey.get(selectedSessionKey) || null;
}

function renderSelectedSessionMeta(session = null) {
  if (!sessionMetaText || !copySessionIdButton) {
    return;
  }

  const selected = session || getSelectedSessionInfo();
  const sessionId = String(selected?.session_id || "").trim();
  if (!sessionId) {
    sessionMetaText.textContent = "Session ID unavailable.";
    copySessionIdButton.disabled = true;
    copySessionIdButton.dataset.sessionId = "";
    copySessionIdButton.title = "Copy the selected Hermes session ID";
    return;
  }

  const source = String(selected?.source || "").trim();
  const sourceLabel = source ? source.replace(/_/g, " ") : "";
  const accessLabel = selected?.can_send === false ? "read-only here" : "sendable here";
  sessionMetaText.textContent = sourceLabel
    ? `Session ID: ${sessionId} \u00b7 ${sourceLabel} \u00b7 ${accessLabel}`
    : `Session ID: ${sessionId} \u00b7 ${accessLabel}`;
  copySessionIdButton.disabled = false;
  copySessionIdButton.dataset.sessionId = sessionId;
  copySessionIdButton.title = `Copy session ID ${sessionId}`;
}

function renderSessionHistory(sessions, activeSessionKey = "") {
  if (!sessionHistorySelect) {
    return;
  }
  const normalizedSessions = Array.isArray(sessions) ? sessions : [];
  sessionHistoryByKey = new Map();
  const knownKeys = new Set();
  for (const session of normalizedSessions) {
    if (session?.session_key) {
      knownKeys.add(session.session_key);
      sessionHistoryByKey.set(session.session_key, session);
    }
  }

  let nextSelected = "";
  if (selectedSessionKey && knownKeys.has(selectedSessionKey)) {
    nextSelected = selectedSessionKey;
  } else if (activeSessionKey && knownKeys.has(activeSessionKey)) {
    nextSelected = activeSessionKey;
  } else if (expectedSessionKey && knownKeys.has(expectedSessionKey)) {
    nextSelected = expectedSessionKey;
  } else if (normalizedSessions.length > 0) {
    nextSelected = normalizedSessions[0].session_key || "";
  }

  isApplyingSessionSelection = true;
  sessionHistorySelect.textContent = "";
  if (!normalizedSessions.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "No Hermes sessions yet";
    sessionHistorySelect.appendChild(option);
    sessionHistorySelect.disabled = true;
    isApplyingSessionSelection = false;
    selectedSessionKey = "";
    selectedSessionCanSend = true;
    renderSelectedSessionMeta(null);
    updateComposerAvailability();
    return;
  }

  for (const session of normalizedSessions) {
    const option = document.createElement("option");
    option.value = session.session_key || "";
    option.textContent = formatSessionLabel(session);
    option.title = String(session.session_id || "").trim()
      ? `Session ID: ${session.session_id}`
      : option.textContent;
    sessionHistorySelect.appendChild(option);
  }

  sessionHistorySelect.disabled = isBusy;
  sessionHistorySelect.value = nextSelected;
  selectedSessionKey = nextSelected;
  if (nextSelected) {
    expectedSessionKey = nextSelected;
  }
  isApplyingSessionSelection = false;
  selectedSessionCanSend = sessionHistoryByKey.get(nextSelected)?.can_send !== false;
  renderSelectedSessionMeta(sessionHistoryByKey.get(nextSelected) || null);
  updateComposerAvailability();
}

function renderSessionHistoryUnavailable(message = "") {
  if (!sessionHistorySelect) {
    return;
  }

  isApplyingSessionSelection = true;
  sessionHistorySelect.textContent = "";
  const option = document.createElement("option");
  option.value = "";
  option.textContent = "Session history unavailable";
  sessionHistorySelect.appendChild(option);
  sessionHistorySelect.value = "";
  sessionHistorySelect.disabled = true;
  isApplyingSessionSelection = false;
  selectedSessionKey = "";
  expectedSessionKey = "";
  selectedSessionCanSend = true;
  renderSelectedSessionMeta(null);
  updateComposerAvailability();

  if (message) {
    setStatus(message, { openActivity: true });
  }
}

function renderDomainPermissionStatus(result) {
  latestDomainPermission = result || null;
  if (!domainPermissionStatus || !domainPermissionButton) {
    return;
  }

  const detail = String(result?.detail || "Domain access unavailable");
  if (!result?.supported) {
    domainPermissionStatus.textContent = `Domain access: ${detail}`;
    domainPermissionButton.textContent = "Allow domain";
    domainPermissionButton.disabled = true;
    return;
  }

  domainPermissionStatus.textContent = `Domain access: ${detail}`;
  if (!result.granted) {
    domainPermissionButton.textContent = "Allow domain";
    domainPermissionButton.disabled = false;
    return;
  }

  if (result.removable) {
    domainPermissionButton.textContent = "Remove domain";
    domainPermissionButton.disabled = false;
    return;
  }

  domainPermissionButton.textContent = "Built-in";
  domainPermissionButton.disabled = true;
}

async function refreshDomainPermissionStatus({ quiet = false, tabId = null } = {}) {
  const resolvedTabId = tabId || activeTabId || (await getActiveTab()).id;
  const response = await sendRuntimeMessage({
    type: "hermes:get-domain-permission-status",
    tabId: resolvedTabId
  });
  const result = response.result || {};
  renderDomainPermissionStatus(result);
  if (!quiet && result.detail) {
    setStatus(result.detail);
  }
}

async function loadSessionHistory({ quiet = false, preferredSessionKey = "" } = {}) {
  const response = await sendRuntimeMessage({
    type: "hermes:list-chat-sessions",
    sessionKey: preferredSessionKey || selectedSessionKey || expectedSessionKey || "",
    limit: 40
  });
  const result = response.result || {};
  const sessions = result.sessions || [];
  renderSessionHistory(sessions, result.active_session_key || preferredSessionKey || "");
  if (!quiet) {
    setStatus("Session history refreshed.");
  }
}

function createPendingAssistantMessage(progress) {
  const detail = compactStatusText(
    summarizeStatusMessage(progress?.detail || "Hermes is thinking...", "Hermes is thinking..."),
    {
    maxChars: PROGRESS_DETAIL_MAX_CHARS,
    maxLines: 2,
    perLineMax: 100
    }
  );
  const events = Array.isArray(progress?.recent_events)
    ? progress.recent_events
        .slice(-6)
        .map((event) => summarizeStatusMessage(event, "Working..."))
        .filter(Boolean)
        .map((event) => compactStatusText(event, {
          maxChars: PROGRESS_EVENT_MAX_CHARS,
          maxLines: 1,
          perLineMax: 100
        }))
        .filter(Boolean)
        .filter((event, index, list) => list.indexOf(event) === index)
        .slice(-3)
    : [];
  const elapsed = progress?.elapsed_seconds ? ` (${progress.elapsed_seconds}s)` : "";
  const body = events.length ? `${detail}\n\n${events.join("\n")}` : `${detail}${elapsed}`;
  return {
    role: "assistant",
    kind: "pending",
    display_content: body,
    timestamp: ""
  };
}

function buildOptimisticUserMessage(message, sharePage) {
  const images = getAttachmentPreviewImages();
  if (sharePage) {
    return {
      role: "user",
      kind: "page_context",
      display_content: message || "Shared the current page context.",
      page_title: lastPreview?.title || "Current page",
      page_url: lastPreview?.url || "",
      images,
      timestamp: new Date().toISOString()
    };
  }

  return {
    role: "user",
    kind: "chat",
    display_content: message,
    images,
    timestamp: new Date().toISOString()
  };
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

function clearPendingIfAcknowledged() {
  if (!pendingUserMessage) {
    return;
  }
  const lastUser = [...currentMessages].reverse().find((item) => item.role === "user");
  if (lastUser && messageKey(lastUser) === messageKey(pendingUserMessage)) {
    pendingUserMessage = null;
    pendingQueuedAt = 0;
    clearPendingAttachments();
  }
}

function renderMessages(
  messages,
  progress = null,
  optimisticMessage = null,
  { forceScroll = false } = {}
) {
  const previousScrollTop = chatMessages.scrollTop;
  const previousScrollHeight = chatMessages.scrollHeight;
  const previousClientHeight = chatMessages.clientHeight;
  const distanceFromBottom =
    previousScrollHeight - (previousScrollTop + previousClientHeight);
  const wasNearBottom = distanceFromBottom <= CHAT_AUTO_SCROLL_THRESHOLD_PX;

  chatMessages.textContent = "";

  const displayMessages = [...(Array.isArray(messages) ? messages : [])];
  if (optimisticMessage) {
    displayMessages.push(optimisticMessage);
  }
  if (progress?.running) {
    displayMessages.push(createPendingAssistantMessage(progress));
  }

  if (!displayMessages.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "This sidecar session does not have any Hermes messages yet.";
    chatMessages.appendChild(empty);
    return;
  }

  for (const message of displayMessages) {
    const wrapper = document.createElement("article");
    const roleName = message.role === "user" ? "user" : "assistant";
    wrapper.className = `message ${roleName}`;

    const bubble = document.createElement("div");
    bubble.className = "bubble";
    if (message.kind === "pending") {
      bubble.classList.add("pending-bubble");
    }

    const meta = document.createElement("div");
    meta.className = "message-meta";

    const role = document.createElement("span");
    role.className = "message-role";
    role.textContent = message.role === "user" ? "You" : "Hermes";
    meta.appendChild(role);

    if (message.kind === "page_context") {
      const kind = document.createElement("span");
      kind.className = "message-kind";
      kind.textContent = "Page share";
      meta.appendChild(kind);
    } else if (message.kind === "pending") {
      const kind = document.createElement("span");
      kind.className = "message-kind";
      kind.textContent = "Working";
      meta.appendChild(kind);
    }

    const time = formatTimestamp(message.timestamp);
    if (time) {
      const timeLabel = document.createElement("span");
      timeLabel.textContent = time;
      meta.appendChild(timeLabel);
    }

    bubble.appendChild(meta);

    if (message.page_title) {
      const title = document.createElement("p");
      title.className = "message-title";
      title.textContent = message.page_title;
      bubble.appendChild(title);
    }

    const body = document.createElement("p");
    body.className = "message-body";
    body.textContent = getMessageText(message);
    bubble.appendChild(body);
    renderMessageImages(bubble, message);

    const canActOnReply = message.role === "assistant" && message.kind !== "pending" && getMessageText(message);
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
              setStatus(error?.message || action.errorMessage, { openActivity: true });
            });
          });
        } else {
          button.textContent = action.label;
          button.addEventListener("click", async () => {
            try {
              await copyTextToClipboard(action.text);
              setStatus(action.successMessage);
            } catch (error) {
              setStatus(error?.message || action.errorMessage);
            }
          });
        }
        actions.appendChild(button);
      }

      bubble.appendChild(actions);
    }

    wrapper.appendChild(bubble);
    chatMessages.appendChild(wrapper);
  }

  if (forceScroll || wasNearBottom) {
    chatMessages.scrollTop = chatMessages.scrollHeight;
    return;
  }
  chatMessages.scrollTop = previousScrollTop;
}

function applyTranscriptUiState(result) {
  if (result.contentKind === "youtube-watch" && result.transcriptAvailable && result.transcriptAlreadyShared) {
    includeTranscript.checked = false;
    includeTranscript.disabled = true;
    includeTranscriptLabel.textContent = "Transcript already shared";
    return;
  }

  includeTranscript.disabled = false;
  includeTranscriptLabel.textContent = "Include transcript once per video";
}

function renderPreview(result) {
  pageContextUnavailable = false;
  const previousUrl = lastPreview?.url || "";
  lastPreview = result || null;
  const nonHttpProtocol = getNonHttpProtocol(result?.url || "");
  syncBundleSelectionState(result, {
    preserveExisting: Boolean(previousUrl && previousUrl === (result?.url || ""))
  });
  pageTitle.textContent = result.title || "Untitled page";
  pageUrl.textContent = result.url || "";
  contentKind.textContent = result.contentKind || "web-page";
  selectionLength.textContent = `${result.selectionLength || 0} chars selected`;
  pageTextLength.textContent = `${result.pageTextLength || 0} chars page text`;
  renderPdfImageStatus(result);

  if (result.contentKind === "restricted-page") {
    pageContextUnavailable = true;
    sharePageCheckbox.checked = false;
    sharePageCheckbox.disabled = false;
    includeTranscript.checked = false;
    includeTranscript.disabled = true;
    includeTranscriptLabel.textContent = "Transcript unavailable on this tab";
    transcriptStatus.textContent = "Unavailable on this tab";
    renderContextBundle();
    return;
  }

  if (nonHttpProtocol) {
    pageContextUnavailable = true;
    sharePageCheckbox.checked = false;
    sharePageCheckbox.disabled = false;
    includeTranscript.checked = false;
    includeTranscript.disabled = true;
    includeTranscriptLabel.textContent = "Transcript unavailable on non-http(s) tabs";
    transcriptStatus.textContent = `Unavailable on ${nonHttpProtocol} pages`;
    renderContextBundle();
    return;
  }

  if (isExtensionsPolicyBlockedMessage(result?.unavailableReason || "")) {
    pageContextUnavailable = true;
    sharePageCheckbox.checked = false;
    sharePageCheckbox.disabled = false;
    includeTranscript.checked = false;
    includeTranscript.disabled = true;
    includeTranscriptLabel.textContent = "Transcript unavailable on policy-blocked pages";
    transcriptStatus.textContent = "Blocked by browser policy";
    renderContextBundle();
    return;
  }

  sharePageCheckbox.disabled = false;

  if (result.transcriptAvailable) {
    if (result.transcriptAlreadyShared) {
      transcriptStatus.textContent = "Transcript already sent";
    } else {
      transcriptStatus.textContent = result.transcriptLanguage
        ? `Transcript ready (${result.transcriptLanguage})`
        : "Transcript ready";
    }
  } else {
    transcriptStatus.textContent = "No transcript";
  }

  applyTranscriptUiState(result);
  renderContextBundle();
}

function renderUnavailablePreview(message) {
  pageContextUnavailable = true;
  lastPreview = null;
  bundleSelectionState = null;
  pageTitle.textContent = "Page context unavailable";
  pageUrl.textContent = "";
  contentKind.textContent = "unavailable";
  selectionLength.textContent = "0 chars selected";
  pageTextLength.textContent = "0 chars page text";
  renderPdfImageStatus(null);
  transcriptStatus.textContent = "No transcript";
  sharePageCheckbox.checked = false;
  sharePageCheckbox.disabled = false;
  includeTranscript.checked = false;
  includeTranscript.disabled = true;
  includeTranscriptLabel.textContent = "Transcript unavailable until page context loads";
  renderContextBundle();
  if (message) {
    setStatus(message);
  }
}

async function loadSettings() {
  const response = await sendRuntimeMessage({ type: "hermes:get-settings" });
  const settings = response.settings || {};
  applySidebarSettings(settings, { preserveBusyState: true });
}

async function refreshPreview({ quiet = false } = {}) {
  if (previewInFlight) {
    return;
  }
  previewInFlight = true;
  let tab = null;
  try {
    tab = await getActiveTab();
    const response = await sendRuntimeMessage({
      type: "hermes:preview-page-context",
      tabId: tab.id
    });
    const preview = response.result || {};
    renderPreview(preview);
    if (isExtensionsPolicyBlockedMessage(preview.unavailableReason || "")) {
      if (previewPollingEnabled) {
        await setPreviewPollingEnabled(false, { persist: true, quiet: true });
      }
      setStatus(
        "This page blocks scripted scraping due to browser policy.",
        { openActivity: true }
      );
      return;
    }
    if (!quiet) {
      if (preview.contentKind === "restricted-page") {
        setStatus(
          preview.unavailableReason ||
          "This tab is a browser internal page and cannot be shared with Hermes."
        );
      } else {
        setStatus("Current page context is ready.");
      }
    }
  } catch (error) {
    if (!quiet) {
      throw error;
    }
  } finally {
    if (tab?.id) {
      refreshDomainPermissionStatus({ quiet: true, tabId: tab.id }).catch(() => {});
    }
    previewInFlight = false;
  }
}

async function loadChatSession({ quiet = false, sessionKey = "" } = {}) {
  const wasBusy = isBusy;
  if (!quiet) {
    setStatus("Loading Hermes sidecar...");
  }

  const targetSessionKey = String(sessionKey || selectedSessionKey || expectedSessionKey || "").trim();
  const response = await sendRuntimeMessage({
    type: "hermes:get-chat-session",
    sessionKey: targetSessionKey
  });
  const incomingMessages = response.result?.messages || [];
  const incomingSessionKey = response.result?.session_key || "";
  const sessionKeyChanged = Boolean(
    incomingSessionKey &&
    expectedSessionKey &&
    expectedSessionKey !== incomingSessionKey
  );

  if (incomingSessionKey) {
    if (sessionKeyChanged) {
      setStatus(
        "Sidecar session changed. Synced to the current session. If your last queued turn does not appear, send it once more.",
        { openActivity: true }
      );
      pendingUserMessage = null;
      pendingQueuedAt = 0;
    }
    expectedSessionKey = incomingSessionKey;
    selectedSessionKey = incomingSessionKey;
    selectedSessionCanSend = response.result?.can_send !== false;
    const mergedSession = {
      ...(sessionHistoryByKey.get(incomingSessionKey) || {}),
      ...(response.result || {})
    };
    sessionHistoryByKey.set(incomingSessionKey, mergedSession);
    if (sessionHistorySelect && !isApplyingSessionSelection) {
      const hasOption = Array.from(sessionHistorySelect.options).some((option) => option.value === incomingSessionKey);
      if (hasOption) {
        isApplyingSessionSelection = true;
        sessionHistorySelect.value = incomingSessionKey;
        isApplyingSessionSelection = false;
      }
    }
    renderSelectedSessionMeta(mergedSession);
  }

  currentMessages = incomingMessages;
  currentProgress = response.result?.progress || null;
  if (response.result?.progress && typeof response.result.progress === "object") {
    lastProgressSnapshot = response.result.progress;
  }
  updateComposerAvailability();
  clearPendingIfAcknowledged();

  if (currentProgress?.running) {
    setBusyState(true);
    renderMessages(currentMessages, currentProgress, pendingUserMessage);
    setStatus(currentProgress.detail || "Hermes is working...", { openActivity: true });
    schedulePolling();
  } else {
    const waitingForQueuedTurn =
      Boolean(pendingUserMessage) &&
      !currentMessages.length &&
      !currentProgress?.error &&
      Date.now() - pendingQueuedAt < 90000;

    if (waitingForQueuedTurn) {
      setBusyState(true);
      renderMessages(
        currentMessages,
        { running: true, detail: "Waiting for Hermes queue state...", recent_events: [] },
        pendingUserMessage
      );
      setStatus("Your turn is still queued. Waiting for queue state to sync...", { openActivity: true });
      schedulePolling();
      syncActivityLogUi();
      return;
    }

    if (currentProgress?.error) {
      setStatus(currentProgress.error, { openActivity: true });
    } else if (!quiet) {
      setStatus("Hermes sidecar is ready.");
    } else if (wasBusy) {
      setStatus("Reply ready.");
    }
    pendingUserMessage = null;
    pendingQueuedAt = 0;
    renderMessages(currentMessages, null, null);
    setBusyState(false);
    stopPolling();
  }

  syncActivityLogUi();

  if (sessionKeyChanged && quiet) {
    loadSessionHistory({ quiet: true, preferredSessionKey: incomingSessionKey }).catch(() => {});
  }
}

async function sendChatMessage(messageOverride = null, options = {}) {
  stopReplySpeech({ rerender: false });
  if (!selectedSessionCanSend) {
    throw new Error(
      "This session is read-only in the browser side panel right now. " +
      "Select a browser sidecar session or start a new chat to send from here."
    );
  }
  if (isBusy) {
    await loadChatSession({ quiet: true });
    if (isBusy) {
      setStatus("Hermes is already working on this sidecar session. Waiting for the current turn to finish.", { openActivity: true });
      return;
    }
  }
  if (!activeTabId) {
    await getActiveTab();
  }

  const message = messageOverride === null
    ? chatInput.value.trim()
    : String(messageOverride || "").trim();
  let outgoingMessage = buildOutgoingMessage(message);
  const forceIncludeTranscript = Boolean(options.forceIncludeTranscript);
  const sharePage = forceIncludeTranscript || sharePageCheckbox.checked;
  const includeTranscriptForSend = forceIncludeTranscript || includeTranscript.checked;
  const attachments = buildAttachmentPayloads();
  if (sharePage && pageContextUnavailable) {
    throw new Error(
      "Current tab context is unavailable. Switch to a normal webpage tab, or turn off page sharing for this turn."
    );
  }
  const effectiveSharePage = sharePage;
  if (!outgoingMessage && !effectiveSharePage && !attachments.length) {
    throw new Error("Type a message, attach an image, or enable page sharing before sending.");
  }

  pendingUserMessage = buildOptimisticUserMessage(message || outgoingMessage, effectiveSharePage);
  pendingQueuedAt = Date.now();
  renderMessages(
    currentMessages,
    { running: true, detail: "Sending your turn to Hermes...", recent_events: [] },
    pendingUserMessage,
    { forceScroll: true }
  );
  setBusyState(true);
  setStatus(
    effectiveSharePage
      ? "Sending your message with current page context..."
      : "Sending your message...",
    { openActivity: true }
  );
  const targetSessionKey = String(selectedSessionKey || expectedSessionKey || "").trim();

  let response;
  try {
    response = await sendRuntimeMessage({
      type: "hermes:start-chat-message",
      tabId: activeTabId,
      message: outgoingMessage,
      sharePage: effectiveSharePage,
      includeTranscript: includeTranscriptForSend,
      sessionKey: targetSessionKey,
      contextOptions: effectiveSharePage ? getContextOptionsForSend() : null,
      attachments
    });
  } catch (error) {
    if (String(error?.message || "").includes("Unknown message type")) {
      response = await sendRuntimeMessage({
        type: "hermes:send-chat-message",
        tabId: activeTabId,
        message: outgoingMessage,
        sharePage: effectiveSharePage,
        includeTranscript: includeTranscriptForSend,
        sessionKey: targetSessionKey,
        contextOptions: effectiveSharePage ? getContextOptionsForSend() : null,
        attachments
      });
    } else {
      throw error;
    }
  }

  currentMessages = response.result?.messages || currentMessages;
  currentProgress = response.result?.progress || { running: true, detail: "Hermes is thinking..." };
  expectedSessionKey = response.result?.session_key || expectedSessionKey;
  selectedSessionKey = expectedSessionKey;

  if (response.result?.accepted === false && response.result?.busy) {
    pendingUserMessage = null;
    pendingQueuedAt = 0;
    renderMessages(currentMessages, currentProgress, null);
    setBusyState(Boolean(currentProgress?.running));
    setStatus(response.result?.detail || "Hermes is already working on this sidecar session.", { openActivity: true });
    schedulePolling();
    return;
  }

  if (attachments.length) {
    // Clear composer chips right after a successful send so users do not need
    // to manually click "x" for images that were already queued to Hermes.
    clearPendingAttachments();
  }

  clearPendingIfAcknowledged();
  renderMessages(currentMessages, currentProgress, pendingUserMessage, { forceScroll: true });

  const lines = ["Your turn was queued."];
  const sentPageTextLength = Number(response.result?.sent_page_text_length || 0);
  const sentSelectionLength = Number(response.result?.sent_selection_length || 0);
  if (sharePage) {
    const contextOptions = getContextOptionsForSend();
    const enabledChunks = listEnabledBundleChunks();
    if (enabledChunks.length) {
      lines.push(`Included chunks: ${enabledChunks.join(", ")}.`);
    }
    lines.push(
      `Sent page context: ${sentPageTextLength} chars page text, ${sentSelectionLength} chars selection.`
    );
    const previewPageTextChunkLength = Number(lastPreview?.bundle?.chunks?.pageText?.length || 0);
    const expectedPreviewPageTextLength = contextOptions.includePageText ? previewPageTextChunkLength : 0;
    if (
      contextOptions.includePageText &&
      expectedPreviewPageTextLength > 0 &&
      sentPageTextLength + 300 < expectedPreviewPageTextLength
    ) {
      lines.push(
        `Warning: preview showed ${expectedPreviewPageTextLength} page-text chars, but only ${sentPageTextLength} were prepared for this send.`
      );
    }
  }
  if (response.result?.transcript_shared) {
    lines.push("The YouTube transcript was included.");
  } else if (response.result?.transcript_shared_previously) {
    lines.push("Transcript was skipped because this video was already shared earlier.");
  }
  setStatus(lines.join("\n"), { openActivity: true });

  if (messageOverride === null) {
    chatInput.value = "";
  }
  if (sharePage) {
    await refreshPreview({ quiet: true });
  }
  loadSessionHistory({ quiet: true, preferredSessionKey: expectedSessionKey }).catch(() => {});
  schedulePolling();
}

async function interruptChatSession() {
  stopReplySpeech({ rerender: false });
  if (!selectedSessionCanSend) {
    throw new Error(
      "This session is read-only in the browser side panel right now. " +
      "Select a browser sidecar session to interrupt from here."
    );
  }
  if (!isBusy && !currentProgress?.running) {
    setStatus("No active Hermes turn to interrupt.");
    return;
  }
  if (interruptRequested) {
    setStatus("Interrupt already requested. Waiting for Hermes to stop...", { openActivity: true });
    return;
  }

  const targetSessionKey = String(selectedSessionKey || expectedSessionKey || "").trim();
  interruptRequested = true;
  updateComposerAvailability();
  setStatus("Stopping the current Hermes turn...", { openActivity: true });

  try {
    const response = await sendRuntimeMessage({
      type: "hermes:interrupt-chat-session",
      sessionKey: targetSessionKey
    });
    currentMessages = response.result?.messages || currentMessages;
    currentProgress = response.result?.progress || currentProgress;
    expectedSessionKey = response.result?.session_key || expectedSessionKey;
    selectedSessionKey = expectedSessionKey || targetSessionKey;
    interruptRequested = Boolean(response.result?.interrupt_requested);
    renderMessages(currentMessages, currentProgress, pendingUserMessage);
    setStatus(
      response.result?.detail || "Interrupt requested. Hermes will stop after the current step.",
      { openActivity: true }
    );
    loadSessionHistory({
      quiet: true,
      preferredSessionKey: expectedSessionKey || targetSessionKey
    }).catch(() => {});
    if (currentProgress?.running || isBusy) {
      schedulePolling();
    } else {
      setBusyState(false);
    }
  } catch (error) {
    interruptRequested = false;
    updateComposerAvailability();
    throw error;
  }
}

function handleSendError(error) {
  setBusyState(false);
  pendingUserMessage = null;
  renderMessages(currentMessages, null, null);
  setStatus(explainBackgroundMismatch(error), { openActivity: true });
}

async function resetChatSession() {
  stopReplySpeech({ rerender: false });
  if (isBusy) {
    setStatus("Wait for the current Hermes turn to finish before starting a new chat.", { openActivity: true });
    return;
  }

  setBusyState(true);
  setStatus("Starting a fresh sidecar session...", { openActivity: true });
  try {
    const response = await sendRuntimeMessage({
      type: "hermes:reset-chat-session",
      createNew: true,
      sessionKey: ""
    });
    currentMessages = response.result?.messages || [];
    currentProgress = response.result?.progress || null;
    pendingUserMessage = null;
    pendingQueuedAt = 0;
    expectedSessionKey = response.result?.session_key || "";
    selectedSessionKey = expectedSessionKey;
    renderMessages(currentMessages, null, null);
    chatInput.value = "";
    if (!pageContextUnavailable && !sharePageCheckbox.disabled) {
      sharePageCheckbox.checked = sharePageByDefault;
    } else {
      sharePageCheckbox.checked = false;
    }
    setStatus("Started a fresh Hermes sidecar session.");
    loadSessionHistory({ quiet: true, preferredSessionKey: expectedSessionKey }).catch(() => {});
  } finally {
    setBusyState(false);
    stopPolling();
  }
}

function scheduleRefresh() {
  if (extensionContextInvalidated || !previewPollingEnabled) {
    return;
  }
  if (refreshDebounceTimer) {
    clearTimeout(refreshDebounceTimer);
  }
  refreshDebounceTimer = setTimeout(() => {
    refreshPreview({ quiet: true }).catch((error) => {
      const message = isExtensionContextInvalidated(error)
        ? handleExtensionContextInvalidated(error)
        : explainBackgroundMismatch(error);
      setStatus(message, { openActivity: true });
    });
  }, 250);
}

document.getElementById("refresh-button").addEventListener("click", () => {
  refreshPreview()
    .then(() => refreshDomainPermissionStatus({ quiet: true }))
    .catch((error) => setStatus(error.message, { openActivity: true }));
});

if (enablePreviewPolling) {
  enablePreviewPolling.addEventListener("change", () => {
    setPreviewPollingEnabled(enablePreviewPolling.checked).catch((error) => {
      previewPollingEnabled = !enablePreviewPolling.checked;
      syncPreviewPollingUi();
      setStatus(explainBackgroundMismatch(error), { openActivity: true });
    });
  });
}

if (domainPermissionButton) {
  domainPermissionButton.addEventListener("click", async () => {
    try {
      const permission = latestDomainPermission;
      if (!permission) {
        setStatus("Checking domain access status...");
        await refreshDomainPermissionStatus({ quiet: true });
        return;
      }
      if (!permission.supported) {
        setStatus(permission.detail || "Domain access is unavailable on this tab.");
        return;
      }

      if (permission.granted && !permission.removable) {
        setStatus("This domain is built into extension permissions and cannot be removed.");
        return;
      }

      const originPattern = String(permission.originPattern || "").trim();
      if (!originPattern) {
        throw new Error("Could not determine the current tab origin for domain access.");
      }

      const grant = !permission.granted;
      if (grant) {
        // Must happen directly in this click handler so Chrome accepts user gesture.
        setStatus(`Requesting domain access for ${permission.hostname || originPattern}...`);
        const allowed = await chrome.permissions.request({ origins: [originPattern] });
        if (!allowed) {
          setStatus(`Domain access request was not granted for ${permission.hostname || originPattern}.`);
          await refreshDomainPermissionStatus({ quiet: true });
          return;
        }
      } else {
        setStatus(`Removing domain access for ${permission.hostname || originPattern}...`);
        const removed = await chrome.permissions.remove({ origins: [originPattern] });
        if (!removed) {
          setStatus(`Could not remove domain access for ${permission.hostname || originPattern}.`);
          await refreshDomainPermissionStatus({ quiet: true });
          return;
        }
      }

      await refreshDomainPermissionStatus({ quiet: true });
      await refreshPreview({ quiet: true });
      setStatus(latestDomainPermission?.detail || "Domain permission updated.");
    } catch (error) {
      setStatus(explainBackgroundMismatch(error), { openActivity: true });
    }
  });
}

if (refreshSessionsButton) {
  refreshSessionsButton.addEventListener("click", () => {
    loadSessionHistory({ quiet: false }).catch((error) => {
      renderSessionHistoryUnavailable(explainBackgroundMismatch(error));
    });
  });
}

if (sessionHistorySelect) {
  sessionHistorySelect.addEventListener("change", () => {
    if (isApplyingSessionSelection) {
      return;
    }
    if (isBusy) {
      setStatus("Wait for the current Hermes turn to finish before switching sessions.", { openActivity: true });
      if (expectedSessionKey) {
        isApplyingSessionSelection = true;
        sessionHistorySelect.value = expectedSessionKey;
        isApplyingSessionSelection = false;
      }
      return;
    }
    selectedSessionKey = String(sessionHistorySelect.value || "").trim();
    expectedSessionKey = selectedSessionKey;
    pendingUserMessage = null;
    pendingQueuedAt = 0;
    renderSelectedSessionMeta();
    loadChatSession({ sessionKey: selectedSessionKey }).catch((error) => {
      renderChatNotice("Unable to load this sidecar session right now.");
      setStatus(explainBackgroundMismatch(error), { openActivity: true });
    });
  });
}

if (copySessionIdButton) {
  copySessionIdButton.addEventListener("click", async () => {
    const sessionId = String(copySessionIdButton.dataset.sessionId || "").trim();
    if (!sessionId) {
      setStatus("No Hermes session ID is available for the current selection.");
      return;
    }
    try {
      await navigator.clipboard.writeText(sessionId);
      setStatus(`Copied session ID: ${sessionId}`);
    } catch (_error) {
      setStatus("Could not copy the session ID. You can still select and copy it from the row above.");
    }
  });
}

document.getElementById("send-button").addEventListener("click", () => {
  sendChatMessage().catch(handleSendError);
});

if (interruptButton) {
  interruptButton.addEventListener("click", () => {
    interruptChatSession().catch((error) => {
      setStatus(explainBackgroundMismatch(error), { openActivity: true });
    });
  });
}

document.getElementById("reset-chat-button").addEventListener("click", () => {
  resetChatSession().catch((error) => {
    setBusyState(false);
    setStatus(explainBackgroundMismatch(error), { openActivity: true });
  });
});

document.getElementById("open-options-button").addEventListener("click", () => {
  chrome.runtime.openOptionsPage();
});

openControlRoomButton?.addEventListener("click", () => {
  chrome.tabs.create({ url: chrome.runtime.getURL("control-room.html") });
});

if (challengeModeButton) {
  challengeModeButton.addEventListener("click", () => {
    setChallengeModeEnabled(!challengeModeEnabled);
    renderContextBundle();
  });
}

if (presetStrip) {
  presetStrip.addEventListener("click", (event) => {
    const button = event.target instanceof HTMLElement ? event.target.closest(".preset-button") : null;
    if (!(button instanceof HTMLButtonElement)) {
      return;
    }
    const template = button.dataset.template || "";
    const forceIncludeTranscript = button.dataset.includeTranscript === "true";
    sendChatMessage(template, { forceIncludeTranscript }).catch(handleSendError);
  });
}

if (attachButton) {
  attachButton.addEventListener("click", () => {
    if (attachmentInput && !attachmentInput.disabled) {
      attachmentInput.click();
    }
  });
}

if (screengrabButton) {
  screengrabButton.addEventListener("click", async () => {
    try {
      setStatus("Capturing the current tab as an image attachment...");
      const result = await captureCurrentTabScreengrab();
      const label = String(result.name || "page screenshot");
      setStatus(`Attached ${label}.`);
    } catch (error) {
      setStatus(explainBackgroundMismatch(error), { openActivity: true });
    }
  });
}

if (voiceInputButton) {
  voiceInputButton.addEventListener("click", () => {
    toggleVoiceRecording().catch((error) => {
      setStatus(explainBackgroundMismatch(error), { openActivity: true });
    });
  });
}

chrome.runtime.onMessage.addListener((message, sender) => {
  if (message?.type === "hermes:page-route-changed") {
    handleObservedPageRouteChange(message, sender);
    return;
  }

  if (message?.type !== "hermes:voice-input-broadcast") {
    return;
  }
  const payload = message.event && typeof message.event === "object" ? message.event : {};
  const type = String(payload.type || "");
  if (!type) {
    return;
  }

  if (type === "recording") {
    voiceRecordingActive = true;
    voiceTranscriptionPending = false;
    setVoiceRecorderSheetVisible(true, "Recording in progress. Press Voice input again to stop.");
    updateComposerAvailability();
    setStatus("Recording voice note from the sidecar...", { openActivity: true });
    return;
  }

  if (type === "transcribing") {
    voiceRecordingActive = false;
    voiceTranscriptionPending = true;
    setVoiceRecorderSheetVisible(true, "Voice note captured. Hermes is transcribing it now...");
    updateComposerAvailability();
    setStatus("Uploading voice note to Hermes for transcription...", { openActivity: true });
    return;
  }

  if (type === "transcript") {
    voiceRecordingActive = false;
    voiceTranscriptionPending = false;
    setVoiceRecorderSheetVisible(false);
    appendTranscriptToComposer(payload.transcript || "");
    updateComposerAvailability();
    setStatus("Voice note transcribed into the composer.");
    return;
  }

  if (type === "error") {
    voiceRecordingActive = false;
    voiceTranscriptionPending = false;
    setVoiceRecorderSheetVisible(true, String(payload.error || "Voice input failed."));
    updateComposerAvailability();
    setStatus(String(payload.error || "Voice input failed."), { openActivity: true });
    return;
  }

  if (type === "closed") {
    voiceRecordingActive = false;
    voiceTranscriptionPending = false;
    setVoiceRecorderSheetVisible(false);
    updateComposerAvailability();
  }
});

if (voiceInputChannel) {
  voiceInputChannel.addEventListener("message", (event) => {
    const payload = event?.data && typeof event.data === "object" ? event.data : {};
    const type = String(payload.type || "");
    if (!type) {
      return;
    }

    if (type === "recording") {
      voiceRecordingActive = true;
      voiceTranscriptionPending = false;
      setVoiceRecorderSheetVisible(true, "Recording in progress. Press Voice input again to stop.");
      updateComposerAvailability();
      setStatus("Recording voice note from the sidecar...", { openActivity: true });
      return;
    }

    if (type === "error") {
      voiceRecordingActive = false;
      voiceTranscriptionPending = false;
      setVoiceRecorderSheetVisible(true, String(payload.error || "Voice input failed."));
      updateComposerAvailability();
      setStatus(String(payload.error || "Voice input failed."), { openActivity: true });
      return;
    }

    if (type === "closed") {
      voiceRecordingActive = false;
      voiceTranscriptionPending = false;
      setVoiceRecorderSheetVisible(false);
      updateComposerAvailability();
    }
  });
}

if (voiceRecorderCloseButton) {
  voiceRecorderCloseButton.addEventListener("click", () => {
    setVoiceRecorderSheetVisible(false);
  });
}

if (attachmentInput) {
  attachmentInput.addEventListener("change", (event) => {
    const files = event.target instanceof HTMLInputElement ? event.target.files : null;
    addAttachmentFiles(files)
      .then(() => {
        if (files?.length) {
          setStatus(`Attached ${files.length} image${files.length === 1 ? "" : "s"}.`);
        }
      })
      .catch((error) => {
        setStatus(explainBackgroundMismatch(error), { openActivity: true });
      });
  });
}

if (chatInput) {
  chatInput.addEventListener("paste", (event) => {
    const clipboardItems = Array.from(event.clipboardData?.items || []);
    const imageFiles = clipboardItems
      .filter((item) => item.kind === "file" && SUPPORTED_IMAGE_MIME_TYPES.has(String(item.type || "").toLowerCase()))
      .map((item) => item.getAsFile())
      .filter(Boolean);
    if (!imageFiles.length) {
      return;
    }
    event.preventDefault();
    addAttachmentFiles(imageFiles)
      .then(() => {
        setStatus(`Pasted ${imageFiles.length} image${imageFiles.length === 1 ? "" : "s"} from the clipboard.`);
      })
      .catch((error) => {
        setStatus(explainBackgroundMismatch(error), { openActivity: true });
      });
  });
}

if (composer) {
  let dragDepth = 0;
  const setDragOver = (enabled) => {
    composer.classList.toggle("is-dragover", enabled);
  };

  composer.addEventListener("dragenter", (event) => {
    if (!event.dataTransfer?.types?.includes("Files")) {
      return;
    }
    dragDepth += 1;
    setDragOver(true);
  });

  composer.addEventListener("dragover", (event) => {
    if (!event.dataTransfer?.types?.includes("Files")) {
      return;
    }
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
    setDragOver(true);
  });

  composer.addEventListener("dragleave", () => {
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) {
      setDragOver(false);
    }
  });

  composer.addEventListener("drop", (event) => {
    if (!event.dataTransfer?.files?.length) {
      return;
    }
    event.preventDefault();
    dragDepth = 0;
    setDragOver(false);
    addAttachmentFiles(event.dataTransfer.files)
      .then(() => {
        const count = event.dataTransfer.files.length;
        setStatus(`Attached ${count} dropped image${count === 1 ? "" : "s"}.`);
      })
      .catch((error) => {
        setStatus(explainBackgroundMismatch(error), { openActivity: true });
      });
  });
}

sharePageCheckbox.addEventListener("change", () => {
  if (pageContextUnavailable && sharePageCheckbox.checked) {
    sharePageCheckbox.checked = false;
    setStatus(
      "Current tab context is unavailable. Switch to a normal webpage tab, or keep page sharing off for this turn."
    );
    renderContextBundle();
    return;
  }
  renderContextBundle();
});

includeTranscript.addEventListener("change", () => {
  renderContextBundle();
});

chatInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    sendChatMessage().catch(handleSendError);
  }
});

chrome.tabs.onActivated.addListener(() => {
  scheduleRefresh();
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (tabId === activeTabId && (changeInfo.status === "complete" || changeInfo.url)) {
    scheduleRefresh();
  }
});

chrome.windows.onFocusChanged.addListener((windowId) => {
  if (windowId !== chrome.windows.WINDOW_ID_NONE) {
    scheduleRefresh();
  }
});

chrome.storage.onChanged.addListener((changes, areaName) => {
  if (areaName !== "sync") {
    return;
  }
  const watchedKeys = [
    "includeTranscriptByDefault",
    "sharePageByDefault",
    "enablePreviewPolling",
    "showQuickPrompts",
    "showChallengeMode",
    "quickPrompts",
    "challengeModeLabel",
    "challengeModePrompt",
    "themeName",
    "customThemeAccent",
    "customThemes"
  ];
  if (!watchedKeys.some((key) => Object.prototype.hasOwnProperty.call(changes, key))) {
    return;
  }
  loadSettings().catch((error) => {
    setStatus(explainBackgroundMismatch(error), { openActivity: true });
  });
});

window.addEventListener("error", (event) => {
  if (!isExtensionContextInvalidated(event?.error || event?.message)) {
    return;
  }
  handleExtensionContextInvalidated(event.error || event.message);
  event.preventDefault();
});

window.addEventListener("unhandledrejection", (event) => {
  if (!isExtensionContextInvalidated(event?.reason)) {
    return;
  }
  handleExtensionContextInvalidated(event.reason);
  event.preventDefault();
});

renderAttachmentStrip();
updateComposerAvailability();
syncPreviewPollingUi();

(async () => {
  const startupWarnings = [];

  try {
    await loadSettings();
  } catch (error) {
    const message = explainBackgroundMismatch(error);
    startupWarnings.push(message);
    setStatus(message, { openActivity: true });
  }

  try {
    await refreshPreview();
  } catch (error) {
    const message = explainBackgroundMismatch(error);
    startupWarnings.push(message);
    renderUnavailablePreview(message);
  }

  try {
    await refreshDomainPermissionStatus({ quiet: true });
  } catch (error) {
    const message = explainBackgroundMismatch(error);
    startupWarnings.push(message);
    renderDomainPermissionStatus({
      supported: false,
      detail: message
    });
  }

  try {
    await loadSessionHistory({ quiet: true });
  } catch (error) {
    const message = explainBackgroundMismatch(error);
    startupWarnings.push(message);
    renderSessionHistoryUnavailable(message);
  }

  try {
    await loadChatSession({ quiet: true });
  } catch (error) {
    const message = explainBackgroundMismatch(error);
    startupWarnings.push(message);
    renderChatNotice("Unable to load Hermes sidecar history right now.");
    setStatus(message, { openActivity: true });
    setBusyState(false);
    stopPolling();
  }

  if (startupWarnings.length) {
    const uniqueWarnings = [...new Set(startupWarnings)];
    setStatus(uniqueWarnings[uniqueWarnings.length - 1], { openActivity: true });
  } else {
    setStatus("Hermes sidecar is ready.");
  }

  setChallengeModeEnabled(false);

  try {
    chrome.storage.onChanged.addListener((changes, area) => {
      if (area !== "sync") {
        return;
      }
      const keys = [
        "sidecarActivityLogLevel",
        "activityLogPanelOpen",
        "wikiBaseUrl"
      ];
      if (!keys.some((key) => Object.prototype.hasOwnProperty.call(changes, key))) {
        return;
      }
      loadSettings().catch(() => {});
    });
  } catch (_error) {
    // Ignore if storage API unavailable.
  }
})();
