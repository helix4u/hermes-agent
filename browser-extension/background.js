const DEFAULT_BRIDGE_URL = "http://127.0.0.1:8765/inject";
/** Default Hermes gateway wiki host (see `/wiki-host`); override in extension options. */
const DEFAULT_WIKI_BASE_URL = "http://127.0.0.1:8000/knowledge_wiki.html";
const BRIDGE_TIMEOUT_MS = 12000;
/** Session history/state loads can be larger than normal bridge requests. */
const SESSION_BRIDGE_TIMEOUT_MS = 30000;
/** TTS can be slow for long text (chunked synthesis + stitching); keep timeout generous. */
const TTS_BRIDGE_TIMEOUT_MS = 300000;
const TRANSCRIPT_STATE_KEY = "sharedTranscriptKeys";
const CLIENT_SESSION_ID_KEY = "clientSessionId";
const PRIMARY_BROWSER_LABEL = "Hermes Sidecar";
const LEGACY_BROWSER_LABEL = "Chrome Extension";
const ACTIVE_LABEL_KEY = "activeBrowserLabel";
const PAGE_CONTEXT_CACHE_TTL_MS = 90000;
const TRANSCRIPT_TEXT_CACHE_TTL_MS = 10 * 60 * 1000;
const OFFSCREEN_RECORDER_PATH = "offscreen-recorder.html";
const DEFAULT_CHALLENGE_MODE_LABEL = "Challenge my framing";
const DEFAULT_CHALLENGE_MODE_PROMPT =
  "Before answering, briefly challenge my framing. " +
  "Call out likely assumptions, missing context, and plausible alternative interpretations, then continue with the best answer.";
const DEFAULT_THEME_NAME = "original";
const DEFAULT_CUSTOM_THEME_ACCENT = "#9ca3af";
const DEFAULT_CUSTOM_THEMES = [];
const DEFAULT_QUICK_PROMPTS = [
  {
    id: "summarize-page",
    label: "Summarize page",
    template: "Summarize this page.",
    includeTranscript: false
  },
  {
    id: "summarize-video",
    label: "Summarize the video",
    template: "Summarize this video.",
    includeTranscript: true
  },
  {
    id: "argue-against-me",
    label: "Argue against me",
    template: "Argue against my current approach.",
    includeTranscript: false
  },
  {
    id: "extract-action-items",
    label: "Extract action items",
    template: "Extract the action items and decisions from this page.",
    includeTranscript: false
  },
  {
    id: "coding-context",
    label: "Coding context",
    template: "Turn this into coding context I can use right away.",
    includeTranscript: false
  },
  {
    id: "compare-to-transcript",
    label: "Compare to transcript",
    template: "Compare what this page says to the transcript and call out any mismatch.",
    includeTranscript: true
  }
];
const REQUIRED_HOST_ORIGINS = new Set([
  "http://127.0.0.1/*",
  "http://localhost/*",
  "https://x.com/*",
  "https://www.x.com/*",
  "https://twitter.com/*",
  "https://www.twitter.com/*",
  "https://www.youtube.com/*",
  "https://youtube.com/*"
]);
const pageContextCache = new Map();
const transcriptTextCache = new Map();
let offscreenRecorderCreation = null;

function cloneContext(value) {
  try {
    return JSON.parse(JSON.stringify(value || {}));
  } catch (_error) {
    return { ...(value || {}) };
  }
}

function createDefaultQuickPrompts() {
  return DEFAULT_QUICK_PROMPTS.map((prompt) => ({ ...prompt }));
}

function normalizeActivityLogLevel(raw) {
  const value = String(raw || "").trim().toLowerCase();
  if (value === "minimal" || value === "verbose") {
    return value;
  }
  return "normal";
}

function normalizeQuickPrompts(value) {
  if (!Array.isArray(value)) {
    return createDefaultQuickPrompts();
  }

  const normalized = [];
  for (let index = 0; index < value.length; index += 1) {
    const prompt = value[index];
    if (!prompt || typeof prompt !== "object") {
      continue;
    }

    const label = String(prompt.label || "").trim();
    const template = String(prompt.template || "").trim();
    if (!label || !template) {
      continue;
    }

    const fallbackId = label
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "") || `prompt-${index + 1}`;

    normalized.push({
      id: String(prompt.id || "").trim() || `${fallbackId}-${index + 1}`,
      label,
      template,
      includeTranscript: Boolean(prompt.includeTranscript)
    });
  }

  return normalized;
}

function normalizeCustomThemes(value) {
  if (!Array.isArray(value)) {
    return DEFAULT_CUSTOM_THEMES.slice();
  }

  return value
    .filter((theme) => theme && typeof theme === "object")
    .map((theme, index) => {
      const mode = String(theme.mode || "").trim().toLowerCase() === "light" ? "light" : "dark";
      return {
        id: String(theme.id || "").trim() || `custom-theme-${index + 1}`,
        label: String(theme.label || "").trim() || `Custom Theme ${index + 1}`,
        mode,
        primaryColor: String(theme.primaryColor || "").trim() || (mode === "light" ? "#111827" : "#8b5cf6"),
        secondaryColor: String(theme.secondaryColor || "").trim() || (mode === "light" ? "#64748b" : "#22d3ee"),
        textColor: String(theme.textColor || "").trim() || (mode === "light" ? "#111827" : "#f8fafc"),
        mutedTextColor: String(theme.mutedTextColor || "").trim() || (mode === "light" ? "#475569" : "#94a3b8"),
        surfaceColor: String(theme.surfaceColor || "").trim() || (mode === "light" ? "#ffffff" : "#1b1a25"),
        fieldColor: String(theme.fieldColor || "").trim() || (mode === "light" ? "#ffffff" : "#11131d"),
        fieldTextColor: String(theme.fieldTextColor || "").trim() || (mode === "light" ? "#111827" : "#f8fafc")
      };
    });
}

function normalizeStoredSettings(settings) {
  const next = settings && typeof settings === "object" ? settings : {};
  const themeName = String(next.themeName || "").trim().toLowerCase() || DEFAULT_THEME_NAME;
  const customThemeAccent = String(next.customThemeAccent || "").trim() || DEFAULT_CUSTOM_THEME_ACCENT;
  return {
    bridgeUrl: String(next.bridgeUrl || "").trim() || DEFAULT_BRIDGE_URL,
    bridgeToken: String(next.bridgeToken || "").trim(),
    audioInputDeviceId: String(next.audioInputDeviceId || "").trim(),
    audioCaptureMode: String(next.audioCaptureMode || "").trim().toLowerCase() === "speech" ? "speech" : "raw",
    includeTranscriptByDefault: next.includeTranscriptByDefault !== false,
    sharePageByDefault: next.sharePageByDefault !== false,
    enablePreviewPolling: next.enablePreviewPolling === true,
    showQuickPrompts: next.showQuickPrompts === true,
    showChallengeMode: next.showChallengeMode === true,
    quickPrompts: normalizeQuickPrompts(next.quickPrompts),
    challengeModeLabel: String(next.challengeModeLabel || "").trim() || DEFAULT_CHALLENGE_MODE_LABEL,
    challengeModePrompt: String(next.challengeModePrompt || "").trim() || DEFAULT_CHALLENGE_MODE_PROMPT,
    themeName,
    customThemeAccent,
    customThemes: normalizeCustomThemes(next.customThemes),
    wikiBaseUrl: String(next.wikiBaseUrl || "").trim() || DEFAULT_WIKI_BASE_URL,
    sidecarActivityLogLevel: normalizeActivityLogLevel(next.sidecarActivityLogLevel),
    activityLogPanelOpen: next.activityLogPanelOpen === true
  };
}

function buildSettingsPatch(settings) {
  if (!settings || typeof settings !== "object") {
    return {};
  }

  const patch = {};
  if (Object.prototype.hasOwnProperty.call(settings, "bridgeUrl")) {
    patch.bridgeUrl = String(settings.bridgeUrl || "").trim();
  }
  if (Object.prototype.hasOwnProperty.call(settings, "bridgeToken")) {
    patch.bridgeToken = String(settings.bridgeToken || "").trim();
  }
  if (Object.prototype.hasOwnProperty.call(settings, "audioInputDeviceId")) {
    patch.audioInputDeviceId = String(settings.audioInputDeviceId || "").trim();
  }
  if (Object.prototype.hasOwnProperty.call(settings, "audioCaptureMode")) {
    patch.audioCaptureMode =
      String(settings.audioCaptureMode || "").trim().toLowerCase() === "speech" ? "speech" : "raw";
  }
  if (Object.prototype.hasOwnProperty.call(settings, "includeTranscriptByDefault")) {
    patch.includeTranscriptByDefault = settings.includeTranscriptByDefault !== false;
  }
  if (Object.prototype.hasOwnProperty.call(settings, "sharePageByDefault")) {
    patch.sharePageByDefault = settings.sharePageByDefault !== false;
  }
  if (Object.prototype.hasOwnProperty.call(settings, "enablePreviewPolling")) {
    patch.enablePreviewPolling = settings.enablePreviewPolling === true;
  }
  if (Object.prototype.hasOwnProperty.call(settings, "showQuickPrompts")) {
    patch.showQuickPrompts = settings.showQuickPrompts === true;
  }
  if (Object.prototype.hasOwnProperty.call(settings, "showChallengeMode")) {
    patch.showChallengeMode = settings.showChallengeMode === true;
  }
  if (Object.prototype.hasOwnProperty.call(settings, "quickPrompts")) {
    patch.quickPrompts = normalizeQuickPrompts(settings.quickPrompts);
  }
  if (Object.prototype.hasOwnProperty.call(settings, "challengeModeLabel")) {
    patch.challengeModeLabel =
      String(settings.challengeModeLabel || "").trim() || DEFAULT_CHALLENGE_MODE_LABEL;
  }
  if (Object.prototype.hasOwnProperty.call(settings, "challengeModePrompt")) {
    patch.challengeModePrompt =
      String(settings.challengeModePrompt || "").trim() || DEFAULT_CHALLENGE_MODE_PROMPT;
  }
  if (Object.prototype.hasOwnProperty.call(settings, "themeName")) {
    patch.themeName = String(settings.themeName || "").trim().toLowerCase() || DEFAULT_THEME_NAME;
  }
  if (Object.prototype.hasOwnProperty.call(settings, "customThemeAccent")) {
    patch.customThemeAccent = String(settings.customThemeAccent || "").trim() || DEFAULT_CUSTOM_THEME_ACCENT;
  }
  if (Object.prototype.hasOwnProperty.call(settings, "customThemes")) {
    patch.customThemes = normalizeCustomThemes(settings.customThemes);
  }
  if (Object.prototype.hasOwnProperty.call(settings, "wikiBaseUrl")) {
    patch.wikiBaseUrl = String(settings.wikiBaseUrl || "").trim();
  }
  if (Object.prototype.hasOwnProperty.call(settings, "sidecarActivityLogLevel")) {
    patch.sidecarActivityLogLevel = normalizeActivityLogLevel(settings.sidecarActivityLogLevel);
  }
  if (Object.prototype.hasOwnProperty.call(settings, "activityLogPanelOpen")) {
    patch.activityLogPanelOpen = settings.activityLogPanelOpen === true;
  }

  return patch;
}

function getTextLength(value) {
  return String(value || "").length;
}

function getTranscriptCacheKey(context) {
  const transcript = context?.transcript || {};
  const transcriptKey = String(transcript.key || transcript.videoId || transcript.video_id || "").trim();
  if (transcriptKey) {
    return transcriptKey;
  }
  return String(context?.url || "").trim();
}

function readCachedTranscriptText(context) {
  const cacheKey = getTranscriptCacheKey(context);
  if (!cacheKey) {
    return null;
  }

  const entry = transcriptTextCache.get(cacheKey);
  if (!entry) {
    return null;
  }

  const text = String(entry.text || "").trim();
  if (!text) {
    transcriptTextCache.delete(cacheKey);
    return null;
  }
  const capturedAt = Number(entry.capturedAt || 0);
  if (!capturedAt || Date.now() - capturedAt > TRANSCRIPT_TEXT_CACHE_TTL_MS) {
    transcriptTextCache.delete(cacheKey);
    return null;
  }

  return text;
}

function rememberTranscriptText(context, text) {
  const cacheKey = getTranscriptCacheKey(context);
  if (!cacheKey) {
    return;
  }
  const normalizedText = String(text || "").trim();
  if (!normalizedText) {
    transcriptTextCache.delete(cacheKey);
    return;
  }
  transcriptTextCache.set(cacheKey, {
    capturedAt: Date.now(),
    text: normalizedText
  });
}

function withPageTextSource(context, sourceLabel) {
  return {
    ...context,
    metadata: {
      ...(context.metadata || {}),
      pageTextSource: sourceLabel
    }
  };
}

function applySelectionFallback(context, sourceLabel = "selection-fallback-background") {
  const pageTextLength = getTextLength(context?.pageText);
  const selectionLength = getTextLength(context?.selection);
  if (pageTextLength < 500 && selectionLength > pageTextLength + 300) {
    return withPageTextSource(
      {
        ...context,
        pageText: context.selection || ""
      },
      sourceLabel
    );
  }
  return context;
}

function isLikelyPdfNoiseText(text) {
  const value = String(text || "").trim();
  if (!value) {
    return false;
  }
  const lowered = value.toLowerCase();
  const noiseSignals = [
    ".mwd-content-iframe",
    ".mwd-review-iframe",
    "@keyframes",
    "position: fixed",
    "border-radius:",
    "z-index:",
    "opacity:",
    "visibility: hidden",
    "min-height:",
    "max-width:",
    "pointer-events:",
    "transform:",
    "background-color:",
    "iframe {",
    "html, body, iframe",
    "sandbox=",
    "allow-popups",
    "allow-downloads"
  ];
  const noiseHits = noiseSignals.filter((token) => lowered.includes(token)).length;
  if (noiseHits >= 2) {
    return true;
  }
  const lineCount = value.split(/\r?\n/).length;
  const cssishLines = value
    .split(/\r?\n/)
    .filter((line) => /[:;{}]/.test(line) && !/[.!?]\s*$/.test(line.trim()))
    .length;
  if (lineCount >= 4 && cssishLines >= Math.max(3, Math.floor(lineCount * 0.5))) {
    return true;
  }
  return false;
}

function applyRenderedTextFallback(context, fallbackText, sourceLabel = "dom-fallback-background") {
  const currentLength = getTextLength(context?.pageText);
  const fallbackValue = String(fallbackText || "");
  const fallbackLength = getTextLength(fallbackValue);
  const contentKind = String(context?.contentKind || "").trim();
  if (
    (contentKind === "pdf-document" || contentKind === "pdf-embed") &&
    isLikelyPdfNoiseText(fallbackValue)
  ) {
    return context;
  }
  if (fallbackLength > currentLength) {
    return withPageTextSource(
      {
        ...context,
        pageText: fallbackValue
      },
      sourceLabel
    );
  }
  return context;
}

function applyCachedContextFallback(context, cachedContext, sourceLabel = "preview-cache-fallback-background") {
  if (!cachedContext) {
    return context;
  }

  const currentPageTextLength = getTextLength(context?.pageText);
  const cachedPageTextLength = getTextLength(cachedContext?.pageText);
  let nextContext = context;

  if (cachedPageTextLength > currentPageTextLength + 180) {
    nextContext = withPageTextSource(
      {
        ...nextContext,
        pageText: String(cachedContext.pageText || "")
      },
      sourceLabel
    );
  }

  const currentSelectionLength = getTextLength(nextContext?.selection);
  const cachedSelectionLength = getTextLength(cachedContext?.selection);
  if (cachedSelectionLength > currentSelectionLength + 180) {
    nextContext = {
      ...nextContext,
      selection: String(cachedContext.selection || "")
    };
  }

  return nextContext;
}

function rememberPageContext(tabId, context) {
  if (!tabId || !context) {
    return;
  }
  const url = String(context.url || "").trim();
  if (!url) {
    return;
  }
  pageContextCache.set(String(tabId), {
    capturedAt: Date.now(),
    url,
    context: cloneContext(context)
  });
}

function readCachedPageContext(tabId, expectedUrl = "") {
  if (!tabId) {
    return null;
  }
  const cacheKey = String(tabId);
  const entry = pageContextCache.get(cacheKey);
  if (!entry) {
    return null;
  }
  if (Date.now() - Number(entry.capturedAt || 0) > PAGE_CONTEXT_CACHE_TTL_MS) {
    pageContextCache.delete(cacheKey);
    return null;
  }
  if (expectedUrl && String(entry.url || "").trim() && String(entry.url || "").trim() !== String(expectedUrl || "").trim()) {
    return null;
  }
  return cloneContext(entry.context || {});
}

function clampPreviewText(value, maxLength = 220) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  if (!text) {
    return "";
  }
  if (text.length <= maxLength) {
    return text;
  }
  return `${text.slice(0, Math.max(0, maxLength - 1)).trimEnd()}...`;
}

function summarizeMetadataPreview(context) {
  const metadata = context?.metadata || {};
  const parts = [];
  const addPart = (value, prefix = "") => {
    const text = clampPreviewText(value, 80);
    if (text) {
      parts.push(prefix ? `${prefix}${text}` : text);
    }
  };

  addPart(context?.siteName, "Site: ");
  addPart(context?.contentKind, "Kind: ");
  addPart(context?.description, "Summary: ");
  addPart(metadata.author, "Author: ");
  addPart(metadata.byline, "Byline: ");
  addPart(metadata.channelName, "Channel: ");
  addPart(metadata.publishedTime, "Published: ");

  return parts.slice(0, 4).join(" | ");
}

function normalizeContextOptions(options) {
  const next = options && typeof options === "object" ? options : {};
  return {
    includeTitle: next.includeTitle !== false,
    includeUrl: next.includeUrl !== false,
    includeMetadata: next.includeMetadata !== false,
    includeSelection: next.includeSelection !== false,
    includePageText: next.includePageText !== false
  };
}

function buildContextBundlePreview(context, transcriptAlreadyShared = false) {
  const transcript = context?.transcript || {};
  const transcriptText = String(transcript.text || "");
  const transcriptLength = getTextLength(transcriptText);
  const transcriptReady = transcriptLength > 0;
  const transcriptChunkAvailable = transcriptReady || transcriptAlreadyShared;
  const metadataPreview = summarizeMetadataPreview(context);
  const contentKind = String(context?.contentKind || "").trim();
  const pdfPreviewImageCount = Math.max(0, Number(context?.metadata?.pdfPreviewImageCount || 0));
  const pdfImagesAvailable =
    pdfPreviewImageCount > 0 || contentKind === "pdf-document" || contentKind === "pdf-embed";
  return {
    chunks: {
      title: {
        key: "title",
        label: "Title",
        available: Boolean(context?.title),
        includedByDefault: Boolean(context?.title),
        length: getTextLength(context?.title),
        preview: clampPreviewText(context?.title, 120),
        reason: "Quickly tells Hermes what page or tab this came from."
      },
      url: {
        key: "url",
        label: "URL",
        available: Boolean(context?.url),
        includedByDefault: Boolean(context?.url),
        length: getTextLength(context?.url),
        preview: clampPreviewText(context?.url, 140),
        reason: "Preserves the exact source and makes follow-up references clearer."
      },
      metadata: {
        key: "metadata",
        label: "Metadata",
        available: Boolean(metadataPreview),
        includedByDefault: Boolean(metadataPreview),
        length: getTextLength(metadataPreview),
        preview: metadataPreview,
        reason: "Adds author, site, kind, and other framing details when they help."
      },
      selection: {
        key: "selection",
        label: "Selected text",
        available: Boolean(context?.selection),
        includedByDefault: Boolean(context?.selection),
        length: getTextLength(context?.selection),
        preview: clampPreviewText(context?.selection, 220),
        reason: "Usually the highest-signal chunk because you pointed at it directly."
      },
      pageText: {
        key: "pageText",
        label: "Page text",
        available: Boolean(context?.pageText),
        includedByDefault: Boolean(context?.pageText),
        length: getTextLength(context?.pageText),
        preview: clampPreviewText(context?.pageText, 220),
        reason: "Gives Hermes the surrounding page context beyond the exact selection."
      },
      pdfImages: {
        key: "pdfImages",
        label: "PDF page images",
        available: pdfImagesAvailable,
        includedByDefault: pdfImagesAvailable,
        length: pdfPreviewImageCount,
        metricText: pdfPreviewImageCount > 0
          ? `${pdfPreviewImageCount} image${pdfPreviewImageCount === 1 ? "" : "s"}`
          : "Auto",
        preview: pdfPreviewImageCount > 0
          ? `Rendered preview images from the first ${pdfPreviewImageCount} PDF page${pdfPreviewImageCount === 1 ? "" : "s"} will be attached automatically.`
          : pdfImagesAvailable
            ? "Hermes will try to attach rendered PDF page images automatically for this document."
            : "",
        reason: "Useful when the PDF contains diagrams, charts, scanned pages, or other visual content."
      },
      transcript: {
        key: "transcript",
        label: "YouTube transcript",
        available: transcriptChunkAvailable,
        includedByDefault: transcriptReady && !transcriptAlreadyShared,
        length: transcriptLength,
        preview: transcriptAlreadyShared
          ? "Already shared earlier in this browser session."
          : transcriptReady
            ? transcript.language
              ? `Full transcript ready (${transcript.language}).`
              : "Full transcript ready."
            : "",
        reason: "Best for spoken content that is missing from the visible page."
      }
    }
  };
}

function applyContextOptionsToPayload(context, options) {
  const normalized = normalizeContextOptions(options);
  return {
    ...context,
    title: normalized.includeTitle ? String(context?.title || "") : "",
    url: normalized.includeUrl ? String(context?.url || "") : "",
    description: normalized.includeMetadata ? String(context?.description || "") : "",
    canonicalUrl: normalized.includeMetadata ? String(context?.canonicalUrl || "") : "",
    siteName: normalized.includeMetadata ? String(context?.siteName || "") : "",
    contentKind: normalized.includeMetadata ? String(context?.contentKind || "") : "",
    metadata: normalized.includeMetadata ? cloneContext(context?.metadata || {}) : {},
    selection: normalized.includeSelection ? String(context?.selection || "") : "",
    pageText: normalized.includePageText ? String(context?.pageText || "") : ""
  };
}

function isYouTubeWatchUrl(rawUrl) {
  try {
    const u = new URL(String(rawUrl || ""));
    return (/^(www\.)?youtube\.com$/i.test(u.hostname) && u.pathname === "/watch" && u.searchParams.has("v"));
  } catch (_e) {
    return false;
  }
}

function isRestrictedPageUrl(rawUrl) {
  const value = String(rawUrl || "").trim();
  if (!value) {
    return true;
  }
  try {
    const parsed = new URL(value);
    return !["http:", "https:"].includes(parsed.protocol);
  } catch (_error) {
    return true;
  }
}

function getUnsupportedPageReason(rawUrl) {
  const value = String(rawUrl || "").trim();
  if (!value) {
    return "Only http/https pages can be shared with Hermes on this tab.";
  }
  try {
    const parsed = new URL(value);
    if (["http:", "https:"].includes(parsed.protocol)) {
      return "";
    }
    return `Only http/https pages can be shared with Hermes (current protocol: ${parsed.protocol || "unknown"}).`;
  } catch (_error) {
    return "Only http/https pages can be shared with Hermes on this tab.";
  }
}

async function getTabSnapshot(tabId) {
  try {
    return await chrome.tabs.get(tabId);
  } catch (_error) {
    return null;
  }
}

function resolveDomainAccessTarget(tab) {
  const rawUrl = String(tab?.url || "");
  if (!rawUrl) {
    return {
      supported: false,
      reason: "No active tab URL is available yet."
    };
  }

  let parsed;
  try {
    parsed = new URL(rawUrl);
  } catch (_error) {
    return {
      supported: false,
      reason: "Current tab URL is not valid for permission checks."
    };
  }

  if (!["http:", "https:"].includes(parsed.protocol)) {
    return {
      supported: false,
      reason: "Only http/https pages can be granted or removed."
    };
  }

  return {
    supported: true,
    tabTitle: tab?.title || "",
    tabUrl: rawUrl,
    hostname: parsed.hostname || "",
    originPattern: `${parsed.origin}/*`
  };
}

async function getDomainPermissionStatus(tabId) {
  const tab = await getTabSnapshot(tabId);
  const target = resolveDomainAccessTarget(tab);
  if (!target.supported) {
    return {
      supported: false,
      granted: false,
      removable: false,
      builtin: false,
      hostname: "",
      originPattern: "",
      detail: target.reason
    };
  }

  const granted = await chrome.permissions.contains({ origins: [target.originPattern] });
  const builtin = REQUIRED_HOST_ORIGINS.has(target.originPattern);
  const removable = Boolean(granted && !builtin);
  let detail = "";
  if (!granted) {
    detail = `Not granted for ${target.hostname}`;
  } else if (builtin) {
    detail = `Built-in access for ${target.hostname}`;
  } else {
    detail = `Granted for ${target.hostname}`;
  }

  return {
    supported: true,
    granted,
    removable,
    builtin,
    hostname: target.hostname,
    originPattern: target.originPattern,
    detail
  };
}

async function setDomainPermission(tabId, grant) {
  const tab = await getTabSnapshot(tabId);
  const target = resolveDomainAccessTarget(tab);
  if (!target.supported) {
    throw new Error(target.reason || "Domain permission is not available on this tab.");
  }

  if (grant) {
    const allowed = await chrome.permissions.request({ origins: [target.originPattern] });
    if (!allowed) {
      throw new Error("Domain permission request was not granted.");
    }
  } else {
    const builtin = REQUIRED_HOST_ORIGINS.has(target.originPattern);
    if (builtin) {
      throw new Error("This domain is part of required extension access and cannot be removed.");
    }
    await chrome.permissions.remove({ origins: [target.originPattern] });
  }

  return getDomainPermissionStatus(tabId);
}

async function configureSidePanelBehavior() {
  if (!chrome.sidePanel?.setPanelBehavior) {
    return;
  }
  await chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
}

async function getSettings() {
  const stored = await chrome.storage.sync.get({
    bridgeUrl: DEFAULT_BRIDGE_URL,
    bridgeToken: "",
    audioInputDeviceId: "",
    audioCaptureMode: "raw",
    includeTranscriptByDefault: true,
    sharePageByDefault: true,
    enablePreviewPolling: false,
    showQuickPrompts: false,
    showChallengeMode: false,
    quickPrompts: createDefaultQuickPrompts(),
    challengeModeLabel: DEFAULT_CHALLENGE_MODE_LABEL,
    challengeModePrompt: DEFAULT_CHALLENGE_MODE_PROMPT,
    themeName: DEFAULT_THEME_NAME,
    customThemeAccent: DEFAULT_CUSTOM_THEME_ACCENT,
    customThemes: DEFAULT_CUSTOM_THEMES.slice(),
    wikiBaseUrl: DEFAULT_WIKI_BASE_URL,
    sidecarActivityLogLevel: "normal",
    activityLogPanelOpen: false
  });
  return normalizeStoredSettings(stored);
}

async function setSettings(settings) {
  const patch = buildSettingsPatch(settings);
  const obsoleteKeys = ["showCommandTools", "commandCatalog", "logAnalysisServerUrl"];
  if (!Object.keys(patch).length) {
    await chrome.storage.sync.remove(obsoleteKeys);
    return;
  }
  await chrome.storage.sync.set(patch);
  await chrome.storage.sync.remove(obsoleteKeys);
}

async function getClientSessionId() {
  const stored = await chrome.storage.local.get({ [CLIENT_SESSION_ID_KEY]: "" });
  let sessionId = stored[CLIENT_SESSION_ID_KEY] || "";
  if (!sessionId) {
    sessionId = crypto.randomUUID();
    await chrome.storage.local.set({ [CLIENT_SESSION_ID_KEY]: sessionId });
  }
  return sessionId;
}

async function rotateClientSessionId() {
  const sessionId = crypto.randomUUID();
  await chrome.storage.local.set({ [CLIENT_SESSION_ID_KEY]: sessionId });
  return sessionId;
}

async function getActiveBrowserLabel() {
  const stored = await chrome.storage.local.get({ [ACTIVE_LABEL_KEY]: PRIMARY_BROWSER_LABEL });
  const label = String(stored[ACTIVE_LABEL_KEY] || "").trim();
  if (label) {
    return label;
  }
  await chrome.storage.local.set({ [ACTIVE_LABEL_KEY]: PRIMARY_BROWSER_LABEL });
  return PRIMARY_BROWSER_LABEL;
}

async function setActiveBrowserLabel(label) {
  await chrome.storage.local.set({ [ACTIVE_LABEL_KEY]: label });
}

function resolveBridgeEndpoint(pathname, bridgeUrl) {
  const url = new URL((bridgeUrl || DEFAULT_BRIDGE_URL).trim() || DEFAULT_BRIDGE_URL);
  url.pathname = pathname;
  url.search = "";
  url.hash = "";
  return url.toString();
}

async function callBridge(pathname, { method = "POST", token = "", body, timeoutMs = BRIDGE_TIMEOUT_MS } = {}) {
  const settings = await getSettings();
  const headers = {};
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs);

  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
  }

  let response;
  try {
    response = await fetch(resolveBridgeEndpoint(pathname, settings.bridgeUrl), {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal
    });
  } catch (error) {
    if (error?.name === "AbortError") {
      throw new Error(`Bridge request timed out after ${timeoutMs / 1000}s.`);
    }
    throw error;
  } finally {
    clearTimeout(timeoutId);
  }

  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.ok === false) {
    const message = data.error || `Bridge request failed with status ${response.status}.`;
    throw new Error(message);
  }
  return data;
}

async function getTranscriptState() {
  const stored = await chrome.storage.session.get({ [TRANSCRIPT_STATE_KEY]: {} });
  return stored[TRANSCRIPT_STATE_KEY] || {};
}

async function markTranscriptShared(key) {
  if (!key) {
    return;
  }
  const state = await getTranscriptState();
  state[key] = true;
  await chrome.storage.session.set({ [TRANSCRIPT_STATE_KEY]: state });
}

function canRetryContentScript(error) {
  const message = String(error?.message || error || "").toLowerCase();
  return (
    message.includes("receiving end does not exist") ||
    message.includes("could not establish connection") ||
    message.includes("extension context invalidated")
  );
}

function isScriptingBlockedByPolicy(error) {
  const message = String(error?.message || error || "").toLowerCase();
  return (
    message.includes("cannot be scripted due to an extensionssettings policy") ||
    message.includes("this page cannot be scripted due to an extensionssettings policy") ||
    message.includes("extensionssettings policy")
  );
}

function createEmptyTranscriptState() {
  return {
    available: false,
    shared: false,
    sharedPreviously: false,
    source: "",
    key: ""
  };
}

function getPdfContentKindFromUrl(tabUrl = "") {
  const url = String(tabUrl || "").trim();
  if (!url) {
    return "";
  }
  if (/\.pdf(?:[?#]|$)/i.test(url) || /^data:application\/pdf/i.test(url) || /^blob:/i.test(url)) {
    return "pdf-document";
  }
  return "";
}

function applyPdfUrlFallback(context, tabUrl = "") {
  const nextContext = context && typeof context === "object" ? { ...context } : {};
  const metadata =
    nextContext.metadata && typeof nextContext.metadata === "object"
      ? { ...nextContext.metadata }
      : {};
  const normalizedUrl = String(tabUrl || nextContext.url || metadata.pdfUrl || "").trim();
  const contentKind = String(nextContext.contentKind || getPdfContentKindFromUrl(normalizedUrl) || "").trim();
  const embeddedPdfUrl = String(metadata.embeddedPdfUrl || "").trim();
  let pageText = String(nextContext.pageText || "").trim();

  if (contentKind === "pdf-document" && normalizedUrl) {
    metadata.pdfUrl = normalizedUrl;
    if (!pageText) {
      pageText = `Direct PDF document detected.\nPDF URL: ${normalizedUrl}`;
      metadata.pageTextSource = String(metadata.pageTextSource || "").trim() || "pdf-url-fallback-background";
    }
  } else if (contentKind === "pdf-embed" && embeddedPdfUrl) {
    if (!pageText) {
      pageText = `Embedded PDF detected.\nEmbedded PDF URL: ${embeddedPdfUrl}`;
      metadata.pageTextSource = String(metadata.pageTextSource || "").trim() || "pdf-embed-url-fallback-background";
    }
  }

  return {
    ...nextContext,
    contentKind: contentKind || nextContext.contentKind || "",
    metadata,
    pageText
  };
}

async function buildPolicyBlockedContext(tabId, reason = "") {
  const tab = await getTabSnapshot(tabId);
  const tabUrl = String(tab?.url || "").trim();
  const cachedContext = readCachedPageContext(tabId, tabUrl) || {};
  const cachedMetadata =
    cachedContext?.metadata && typeof cachedContext.metadata === "object"
      ? cachedContext.metadata
      : {};
  const captureReason =
    String(reason || "").trim() ||
    "This page cannot be scripted due to an ExtensionsSettings policy.";

  return {
    url: tabUrl || String(cachedContext?.url || "").trim(),
    title: String(tab?.title || cachedContext?.title || "").trim(),
    description: String(cachedContext?.description || "").trim(),
    canonicalUrl: String(cachedContext?.canonicalUrl || "").trim(),
    siteName: String(cachedContext?.siteName || "").trim(),
    selection: String(cachedContext?.selection || "").trim(),
    pageText: String(cachedContext?.pageText || "").trim(),
    contentKind: String(cachedContext?.contentKind || "").trim() || "web-page",
    metadata: {
      ...cachedMetadata,
      pageCaptureBlocked: true,
      pageCaptureBlockedReason: captureReason,
      pageTextSource:
        String(cachedMetadata.pageTextSource || "").trim() || "script-policy-blocked"
    },
    transcript:
      cachedContext?.transcript && typeof cachedContext.transcript === "object"
        ? cachedContext.transcript
        : createEmptyTranscriptState()
  };
}

async function ensureContentScript(tabId) {
  if (!chrome.scripting?.executeScript) {
    return false;
  }
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      files: ["content.js"]
    });
    return true;
  } catch (error) {
    if (isScriptingBlockedByPolicy(error)) {
      return false;
    }
    throw error;
  }
}

async function captureRenderedPageTextFallback(tabId) {
  if (!chrome.scripting?.executeScript) {
    return "";
  }

  try {
    const [{ result }] = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => {
        const collapse = (text) => (text || "")
          .replace(/\u00a0/g, " ")
          .replace(/[ \t]+\n/g, "\n")
          .replace(/\n{3,}/g, "\n\n")
          .trim();
        const clamp = (text, maxLength) => {
          const value = collapse(text);
          if (!value) {
            return "";
          }
          if (value.length <= maxLength) {
            return value;
          }
          return value.slice(0, maxLength).trim();
        };
        const collectTextFromSelectors = (selectors, perNodeMax = 2000, totalMax = 24000) => {
          const parts = [];
          const seen = new Set();
          for (const selector of selectors) {
            const nodes = document.querySelectorAll(selector);
            for (const node of nodes) {
              const text = clamp(node?.innerText || node?.textContent || "", perNodeMax);
              if (!text || seen.has(text)) {
                continue;
              }
              seen.add(text);
              parts.push(text);
              const joined = parts.join("\n\n");
              if (joined.length >= totalMax) {
                return clamp(joined, totalMax);
              }
            }
          }
          return clamp(parts.join("\n\n"), totalMax);
        };
        const collectShadowDomText = (root, totalMax = 24000) => {
          const parts = [];
          const seen = new Set();
          const stack = [root];
          while (stack.length) {
            const current = stack.pop();
            if (!current) {
              continue;
            }
            if (current.nodeType === Node.ELEMENT_NODE) {
              const element = current;
              const text = clamp(element.innerText || element.textContent || "", 1800);
              if (text && text.length >= 40 && !seen.has(text) && !/^\d+\s*\/\s*\d+$/.test(text)) {
                seen.add(text);
                parts.push(text);
                const joined = parts.join("\n\n");
                if (joined.length >= totalMax) {
                  return clamp(joined, totalMax);
                }
              }
              if (element.shadowRoot) {
                stack.push(element.shadowRoot);
              }
              for (const child of Array.from(element.children || [])) {
                stack.push(child);
              }
              continue;
            }
            if (current.nodeType === Node.DOCUMENT_FRAGMENT_NODE) {
              for (const child of Array.from(current.childNodes || [])) {
                stack.push(child);
              }
            }
          }
          return clamp(parts.join("\n\n"), totalMax);
        };

        let host = "";
        try {
          host = new URL(window.location.href).hostname.toLowerCase();
        } catch (_error) {
          host = "";
        }
        const isX = host === "x.com" || host.endsWith(".x.com") || host === "twitter.com" || host.endsWith(".twitter.com");
        const isPdfDocument =
          document.contentType === "application/pdf" ||
          /\.pdf(?:[?#]|$)/i.test(window.location.href) ||
          /^data:application\/pdf/i.test(window.location.href) ||
          /^blob:/i.test(window.location.href);

        if (isPdfDocument) {
          const pdfSelectorText = collectTextFromSelectors(
            [
              ".textLayer",
              ".textLayer span",
              "#viewer .page",
              "#viewer .textLayer",
              "viewer-pdf-page",
              "viewer-pdf-page .textLayer",
              "pdf-viewer",
              "pdf-viewer .page",
              "pdf-viewer .textLayer",
              "#mainContainer",
              "#scroller",
              "[role='document']"
            ],
            2000,
            24000
          );
          if (pdfSelectorText) {
            return pdfSelectorText;
          }
          const pdfShadowText = collectShadowDomText(document.documentElement, 24000);
          if (pdfShadowText) {
            return pdfShadowText;
          }
        }

        const parts = [];
        const seen = new Set();

        if (isX) {
          const tweetSelectors = [
            "article[data-testid='tweet'] div[data-testid='tweetText']",
            "article [data-testid='tweetText']",
            "[data-testid='tweet'] [data-testid='tweetText']",
            "article [lang]",
            "[data-testid='tweet'] [lang]",
            "article[data-testid='tweet']",
            "[data-testid='tweet']"
          ];
          for (const sel of tweetSelectors) {
            const nodes = document.querySelectorAll(sel);
            for (const node of nodes) {
              const text = clamp(node?.innerText || node?.textContent || "", 800);
              if (!text || text.length < 25 || seen.has(text)) {
                continue;
              }
              seen.add(text);
              parts.push(text);
              if (parts.length >= 20) {
                break;
              }
            }
            if (parts.length >= 20) {
              break;
            }
          }
        }

        if (!parts.length) {
          const roots = [
            document.querySelector("[data-testid='primaryColumn']"),
            document.querySelector("main"),
            document.querySelector("[role='main']"),
            document.body
          ].filter(Boolean);
          for (const root of roots) {
            const text = clamp(root?.innerText || "", 14000);
            if (!text) {
              continue;
            }
            parts.push(text);
            break;
          }
        }

        return clamp(parts.join("\n\n"), 14000);
      }
    });
    return String(result || "").trim();
  } catch (_error) {
    return "";
  }
}

async function fetchTranscriptTextViaBridge(context, urlHint = "") {
  const cachedText = readCachedTranscriptText(context);
  if (cachedText !== null) {
    return cachedText;
  }

  const transcript = context?.transcript || {};
  const targetUrl = String(context?.url || urlHint || "").trim();
  const fallbackVideoId = String(
    transcript.videoId || transcript.video_id || transcript.key || ""
  ).trim();
  if (!targetUrl && !fallbackVideoId) {
    return "";
  }

  try {
    const token = await getBridgeToken();
    const language = String(transcript.language || "").trim();
    const response = await callBridge("/session", {
      token,
      body: {
        action: "fetch_transcript",
        ...(targetUrl ? { url: targetUrl } : {}),
        ...(!targetUrl && fallbackVideoId ? { video_id: fallbackVideoId } : {}),
        language
      }
    });
    const transcriptText = String(response?.transcript_text || "").trim();
    if (transcriptText) {
      rememberTranscriptText(context, transcriptText);
    }
    return transcriptText;
  } catch (_error) {
    return "";
  }
}

async function fetchPdfTextViaBridge(context, urlHint = "") {
  const targetUrl = String(
    context?.metadata?.pdfUrl ||
    context?.metadata?.embeddedPdfUrl ||
    context?.url ||
    urlHint ||
    ""
  ).trim();
  if (!targetUrl || /^blob:/i.test(targetUrl)) {
    return "";
  }

  try {
    const token = await getBridgeToken();
    const response = await callBridge("/session", {
      token,
      body: {
        action: "fetch_pdf_text",
        url: targetUrl
      }
    });
    return String(response?.pdf_text || "").trim();
  } catch (_error) {
    return "";
  }
}

async function fetchPdfPreviewInfoViaBridge(context, urlHint = "") {
  const targetUrl = String(
    context?.metadata?.pdfUrl ||
    context?.metadata?.embeddedPdfUrl ||
    context?.url ||
    urlHint ||
    ""
  ).trim();
  if (!targetUrl || /^blob:/i.test(targetUrl)) {
    return { imageCount: 0 };
  }

  try {
    const token = await getBridgeToken();
    const response = await callBridge("/session", {
      token,
      body: {
        action: "fetch_pdf_preview_info",
        url: targetUrl
      }
    });
    return {
      imageCount: Math.max(0, Number(response?.image_count || 0))
    };
  } catch (_error) {
    return { imageCount: 0 };
  }
}

async function ensurePreviewPdfText(tabId, context, urlHint = "") {
  const contentKind = String(context?.contentKind || "").trim();
  if (contentKind !== "pdf-document" && contentKind !== "pdf-embed") {
    return context;
  }

  const pageText = String(context?.pageText || "").trim();
  const pageTextSource = String(context?.metadata?.pageTextSource || "").trim();
  const looksLikeFallbackNoise =
    !pageText ||
    isLikelyPdfNoiseText(pageText) ||
    pageText.startsWith("Direct PDF document detected.") ||
    pageText.startsWith("Embedded PDF detected.") ||
    pageTextSource.includes("pdf-url-fallback") ||
    pageTextSource.includes("dom-fallback");

  if (!looksLikeFallbackNoise && pageText.length >= 500) {
    return context;
  }

  const extractedPdfText = await fetchPdfTextViaBridge(context, urlHint);
  if (!extractedPdfText) {
    if (isLikelyPdfNoiseText(pageText)) {
      return applyPdfUrlFallback(
        {
          ...context,
          pageText: "",
          metadata: {
            ...(context?.metadata || {}),
            pageTextSource: ""
          }
        },
        urlHint || context?.url || ""
      );
    }
    return context;
  }

  return {
    ...context,
    pageText: extractedPdfText.slice(0, 24000),
    metadata: {
      ...(context?.metadata || {}),
      pageTextSource: "pdf-direct-extract"
    }
  };
}

async function ensurePreviewPdfAssets(tabId, context, urlHint = "") {
  let nextContext = context;
  nextContext = await ensurePreviewPdfText(tabId, nextContext, urlHint);

  const contentKind = String(nextContext?.contentKind || "").trim();
  if (contentKind !== "pdf-document" && contentKind !== "pdf-embed") {
    return nextContext;
  }

  const metadata = nextContext?.metadata || {};
  const existingCount = Math.max(0, Number(metadata.pdfPreviewImageCount || 0));
  if (existingCount > 0) {
    return nextContext;
  }

  const previewInfo = await fetchPdfPreviewInfoViaBridge(nextContext, urlHint);
  if (!previewInfo.imageCount) {
    return nextContext;
  }

  return {
    ...nextContext,
    metadata: {
      ...metadata,
      pdfPreviewImageCount: previewInfo.imageCount
    }
  };
}

async function collectPageContextFallback(tabId) {
  if (!chrome.scripting?.executeScript) {
    throw new Error("Page context fallback is unavailable in this browser.");
  }

  try {
    const [{ result }] = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => {
      const collapse = (text) => (text || "")
        .replace(/\u00a0/g, " ")
        .replace(/[ \t]+\n/g, "\n")
        .replace(/\n{3,}/g, "\n\n")
        .trim();
      const clamp = (text, maxLength) => {
        const value = collapse(text);
        if (!value) {
          return "";
        }
        if (value.length <= maxLength) {
          return value;
        }
        return value.slice(0, maxLength).trim();
      };
      const getMeta = (selectors) => {
        for (const selector of selectors) {
          const element = document.querySelector(selector);
          const value = element?.content || element?.getAttribute?.("content") || "";
          if (value && String(value).trim()) {
            return String(value).trim();
          }
        }
        return "";
      };
      let host = "";
      let url = window.location.href;
      let contentKind = "web-page";
      try {
        host = new URL(url).hostname.toLowerCase();
      } catch (_error) {
        host = "";
      }
      const isX = host === "x.com" || host.endsWith(".x.com") || host === "twitter.com" || host.endsWith(".twitter.com");
      const isYoutubeWatch = /(^|\.)youtube\.com$/i.test(host) && window.location.pathname === "/watch";
      const embeddedPdfNode =
        document.querySelector("embed[type='application/pdf'][src]") ||
        document.querySelector("object[type='application/pdf'][data]") ||
        Array.from(document.querySelectorAll("embed[src], iframe[src], object[data]")).find((node) => {
          const rawUrl =
            node.getAttribute("src") ||
            node.getAttribute("data") ||
            node.src ||
            node.data ||
            "";
          return /\.pdf(?:[?#]|$)/i.test(String(rawUrl || "").trim());
        }) ||
        null;
      const embeddedPdfUrl = String(
        embeddedPdfNode?.getAttribute?.("src") ||
        embeddedPdfNode?.getAttribute?.("data") ||
        embeddedPdfNode?.src ||
        embeddedPdfNode?.data ||
        ""
      ).trim();
      const isPdfDocument =
        document.contentType === "application/pdf" ||
        /\.pdf(?:[?#]|$)/i.test(url) ||
        /^data:application\/pdf/i.test(url) ||
        /^blob:/i.test(url);
      if (isX) {
        contentKind = "x-feed";
      } else if (isYoutubeWatch) {
        contentKind = "youtube-watch";
      } else if (isPdfDocument) {
        contentKind = "pdf-document";
      } else if (embeddedPdfUrl) {
        contentKind = "pdf-embed";
      }

      const selection = clamp(window.getSelection?.().toString() || "", 8000);
      const parts = [];
      const seen = new Set();

      if (isX) {
        const tweetSelectors = [
          "article[data-testid='tweet'] div[data-testid='tweetText']",
          "article [data-testid='tweetText']",
          "[data-testid='tweet'] [data-testid='tweetText']",
          "article [lang]",
          "[data-testid='tweet'] [lang]",
          "article[data-testid='tweet']",
          "[data-testid='tweet']"
        ];
        for (const sel of tweetSelectors) {
          const nodes = document.querySelectorAll(sel);
          for (const node of nodes) {
            const text = clamp(node?.innerText || node?.textContent || "", 800);
            if (!text || text.length < 25 || seen.has(text)) {
              continue;
            }
            seen.add(text);
            parts.push(text);
            if (parts.length >= 20) {
              break;
            }
          }
          if (parts.length >= 20) {
            break;
          }
        }
      }

      if (!parts.length) {
        const root =
          document.querySelector("[data-testid='primaryColumn']") ||
          document.querySelector("article") ||
          document.querySelector("main") ||
          document.querySelector("[role='main']") ||
          document.body;
        const rootText = clamp(root?.innerText || "", 14000);
        if (rootText) {
          parts.push(rootText);
        }
      }

      return {
        url,
        title: clamp(document.title || "", 512),
        description: clamp(getMeta([
          "meta[name='description']",
          "meta[property='og:description']",
          "meta[name='twitter:description']"
        ]), 2000),
        canonicalUrl: String(document.querySelector("link[rel='canonical']")?.href || "").trim(),
        siteName: getMeta([
          "meta[property='og:site_name']",
          "meta[name='application-name']"
        ]),
        selection,
        pageText: clamp(parts.join("\n\n"), 14000),
        contentKind,
        metadata: {
          author: getMeta([
            "meta[name='author']",
            "meta[property='article:author']",
            "meta[itemprop='author']"
          ]),
          timelineItems: isX ? document.querySelectorAll("article").length : undefined,
          embeddedPdfUrl: embeddedPdfUrl || undefined,
          embeddedPdfTag: embeddedPdfNode?.tagName?.toLowerCase?.() || undefined,
          pdfUrl: isPdfDocument ? url : undefined
        },
        transcript: {
          available: false,
          shared: false,
          sharedPreviously: false,
          source: "",
          key: ""
        }
      };
    }
    });

    return result || {
      url: "",
      title: "",
      description: "",
      canonicalUrl: "",
      siteName: "",
      selection: "",
      pageText: "",
      contentKind: "web-page",
      metadata: {},
      transcript: createEmptyTranscriptState()
    };
  } catch (error) {
    if (isScriptingBlockedByPolicy(error)) {
      return buildPolicyBlockedContext(tabId, error?.message || String(error));
    }
    throw error;
  }
}

async function requestPageContext(tabId, includeTranscriptText, waitForHydration = false) {
  return chrome.tabs.sendMessage(tabId, {
    type: "hermes:collect-page-context",
    includeTranscriptText: Boolean(includeTranscriptText),
    waitForHydration: Boolean(waitForHydration)
  });
}

async function collectPageContext(tabId, includeTranscriptText, waitForHydration = false) {
  try {
    const result = await requestPageContext(tabId, includeTranscriptText, waitForHydration);
    if (result?.error) {
      throw new Error(result.error);
    }
    return result;
  } catch (error) {
    if (isScriptingBlockedByPolicy(error)) {
      return buildPolicyBlockedContext(tabId, error?.message || String(error));
    }
    if (canRetryContentScript(error)) {
      try {
        await ensureContentScript(tabId);
        const retried = await requestPageContext(tabId, includeTranscriptText, waitForHydration);
        if (retried?.error) {
          throw new Error(retried.error);
        }
        return retried;
      } catch (retryError) {
        if (isScriptingBlockedByPolicy(retryError)) {
          return buildPolicyBlockedContext(tabId, retryError?.message || String(retryError));
        }
        return collectPageContextFallback(tabId);
      }
    }
    return collectPageContextFallback(tabId);
  }
}

async function ensurePreviewTranscriptText(tabId, context, onYouTubeWatch, urlHint = "") {
  if (!onYouTubeWatch) {
    return context;
  }

  let nextContext = context;
  let transcript = nextContext?.transcript || {};
  const existingTranscriptText = String(transcript.text || "").trim();
  if (existingTranscriptText) {
    rememberTranscriptText(nextContext, existingTranscriptText);
    return nextContext;
  }

  let bridgeTranscriptText = await fetchTranscriptTextViaBridge(nextContext, urlHint);
  if (bridgeTranscriptText) {
    return {
      ...nextContext,
      transcript: {
        ...transcript,
        available: true,
        text: bridgeTranscriptText.slice(0, 30000),
        source: transcript.source || "hermes-bridge-transcript"
      }
    };
  }

  const retried = await collectPageContext(tabId, true, true);
  if ((retried.pageText || "").length > (nextContext.pageText || "").length) {
    nextContext = retried;
  } else {
    nextContext = {
      ...nextContext,
      transcript: retried?.transcript || nextContext.transcript
    };
  }

  transcript = nextContext?.transcript || {};
  const hydratedTranscriptText = String(transcript.text || "").trim();
  if (hydratedTranscriptText) {
    rememberTranscriptText(nextContext, hydratedTranscriptText);
    return nextContext;
  }

  bridgeTranscriptText = await fetchTranscriptTextViaBridge(nextContext, urlHint);
  if (bridgeTranscriptText) {
    return {
      ...nextContext,
      transcript: {
        ...transcript,
        available: true,
        text: bridgeTranscriptText.slice(0, 30000),
        source: transcript.source || "hermes-bridge-transcript"
      }
    };
  }

  return nextContext;
}

async function previewPageContext(tabId) {
  const tab = await getTabSnapshot(tabId);
  const tabUrl = String(tab?.url || "");
  if (isRestrictedPageUrl(tab?.url || "")) {
    const unsupportedReason = getUnsupportedPageReason(tab?.url || "");
    pageContextCache.delete(String(tabId || ""));
    return {
      title: tab?.title || "Internal browser page",
      url: tab?.url || "",
      contentKind: "restricted-page",
      selectionLength: 0,
      pageTextLength: 0,
      transcriptAvailable: false,
      transcriptAlreadyShared: false,
      transcriptLanguage: "",
      transcriptKey: "",
      unavailableReason: unsupportedReason || "Only http/https pages can be shared with Hermes on this tab."
    };
  }

  const onYouTubeWatch = isYouTubeWatchUrl(tabUrl);
  const wantTranscriptForPreview = onYouTubeWatch;
  let context = await collectPageContext(tabId, wantTranscriptForPreview, wantTranscriptForPreview);
  if (!context?.contentKind) {
    const pdfContentKind = getPdfContentKindFromUrl(tabUrl);
    if (pdfContentKind) {
      context = {
        ...context,
        contentKind: pdfContentKind,
        metadata: {
          ...(context?.metadata || {}),
          pdfUrl: tabUrl || String(context?.metadata?.pdfUrl || "").trim()
        }
      };
    }
  }
  context = await ensurePreviewPdfAssets(tabId, context, tabUrl);
  context = await ensurePreviewTranscriptText(tabId, context, onYouTubeWatch, tabUrl);
  if ((context.pageText || "").length < 300) {
    const retried = await collectPageContext(tabId, wantTranscriptForPreview, true);
    if ((retried.pageText || "").length > (context.pageText || "").length) {
      context = retried;
    }
  }
  context = applySelectionFallback(context, "selection-fallback-background");
  if ((context.pageText || "").length < 300) {
    const fallbackText = await captureRenderedPageTextFallback(tabId);
    context = applyRenderedTextFallback(context, fallbackText, "dom-fallback-preview");
  }
  const cachedContext = readCachedPageContext(tabId, tabUrl || context.url || "");
  context = applyCachedContextFallback(context, cachedContext, "preview-cache-fallback-background");
  context = applyPdfUrlFallback(context, tabUrl);
  rememberPageContext(tabId, context);

  const transcript = context.transcript || {};
  const state = await getTranscriptState();
  const transcriptKey = transcript.key || "";
  const transcriptAlreadyShared = Boolean(transcriptKey && state[transcriptKey]);
  const transcriptTextReady = Boolean(String(transcript.text || "").trim());
  const transcriptReady = transcriptTextReady || transcriptAlreadyShared;

  return {
    title: context.title || "",
    url: context.url || "",
    contentKind: context.contentKind || "",
    selectionLength: (context.selection || "").length,
    pageTextLength: (context.pageText || "").length,
    pdfPreviewImageCount: Math.max(0, Number(context?.metadata?.pdfPreviewImageCount || 0)),
    transcriptAvailable: transcriptReady,
    transcriptAlreadyShared,
    transcriptLanguage: transcript.language || "",
    transcriptKey,
    bundle: buildContextBundlePreview(context, transcriptAlreadyShared),
    unavailableReason: String(context?.metadata?.pageCaptureBlockedReason || "").trim()
  };
}

async function buildPageContextPayload(tabId, message, includeTranscript, browserLabel, contextOptions = null) {
  const tab = await getTabSnapshot(tabId);
  const tabUrl = String(tab?.url || "");
  if (isRestrictedPageUrl(tab?.url || "")) {
    const unsupportedReason = getUnsupportedPageReason(tab?.url || "");
    throw new Error(
      `${unsupportedReason || "Only http/https pages can be shared with Hermes on this tab."} ` +
      "Switch to a normal webpage or turn off \"Use the current page in this turn\"."
    );
  }

  const preview = await previewPageContext(tabId);
  const cachedPreviewContext = readCachedPageContext(tabId, tabUrl || preview.url || "");
  const onYouTubeWatch = isYouTubeWatchUrl(tabUrl);
  const shouldIncludeTranscript =
    Boolean(includeTranscript) &&
    (preview.transcriptAvailable || onYouTubeWatch) &&
    !preview.transcriptAlreadyShared;

  let context = await collectPageContext(tabId, shouldIncludeTranscript, true);
  if (!context?.contentKind) {
    const pdfContentKind = getPdfContentKindFromUrl(tabUrl);
    if (pdfContentKind) {
      context = {
        ...context,
        contentKind: pdfContentKind,
        metadata: {
          ...(context?.metadata || {}),
          pdfUrl: tabUrl || String(context?.metadata?.pdfUrl || "").trim()
        }
      };
    }
  }
  context = await ensurePreviewPdfAssets(tabId, context, tabUrl || preview.url || "");
  if (shouldIncludeTranscript && onYouTubeWatch) {
    context = await ensurePreviewTranscriptText(tabId, context, true, tabUrl || preview.url || "");
    if (!String(context?.transcript?.text || "").trim() && context?.metadata?.pageCaptureBlocked !== true) {
      throw new Error(
        "YouTube transcript was requested but no transcript text was retrieved. " +
        "Click Refresh now and send again."
      );
    }
  }
  context = applySelectionFallback(context, "selection-fallback-background");
  if ((context.pageText || "").length < 220) {
    const retried = await collectPageContext(tabId, shouldIncludeTranscript, true);
    const promotedRetried = applySelectionFallback(retried, "selection-fallback-background");
    if ((promotedRetried.pageText || "").length > (context.pageText || "").length) {
      context = promotedRetried;
    } else if ((retried.pageText || "").length > (context.pageText || "").length) {
      context = retried;
    }
  }
  if ((context.pageText || "").length < 220) {
    const fallbackText = await captureRenderedPageTextFallback(tabId);
    context = applyRenderedTextFallback(context, fallbackText, "dom-fallback-background");
  }
  context = applyCachedContextFallback(context, cachedPreviewContext, "preview-cache-fallback-background");
  context = applyPdfUrlFallback(context, tabUrl);
  const pageCaptureBlocked = context?.metadata?.pageCaptureBlocked === true;

  const previewPageTextLength = Number(preview.pageTextLength || 0);
  const preparedPageTextLength = getTextLength(context.pageText);
  if (!pageCaptureBlocked && previewPageTextLength > 900 && preparedPageTextLength + 300 < previewPageTextLength) {
    const fallbackText = await captureRenderedPageTextFallback(tabId);
    context = applyRenderedTextFallback(context, fallbackText, "dom-fallback-send");
    context = applyCachedContextFallback(context, cachedPreviewContext, "preview-cache-fallback-background");
  }

  const finalPreparedPageTextLength = getTextLength(context.pageText);
  if (!pageCaptureBlocked && previewPageTextLength > 900 && finalPreparedPageTextLength + 300 < previewPageTextLength) {
    throw new Error(
      `Prepared ${finalPreparedPageTextLength} chars of page text, but preview showed ${previewPageTextLength}. ` +
      "Refresh page context and send again."
    );
  }
  rememberPageContext(tabId, context);

  const contextKind = String(context.contentKind || "");
  const selectionLength = (context.selection || "").length;
  const pageTextLength = (context.pageText || "").length;
  if (contextKind === "x-feed" && pageTextLength < 300 && selectionLength < 300 && context?.metadata?.pageCaptureBlocked !== true) {
    throw new Error(
      "Could not capture enough rendered X timeline text yet. " +
      "Scroll briefly to let the feed hydrate, then send again."
    );
  }

  context = applyContextOptionsToPayload(context, contextOptions);
  const transcript = context.transcript || {};
  return {
    preview,
    payload: {
      ...context,
      note: message || "",
      browserLabel: browserLabel || PRIMARY_BROWSER_LABEL,
      clientSessionId: await getClientSessionId(),
      transcript: {
        ...transcript,
        shared: Boolean(shouldIncludeTranscript && transcript.text),
        sharedPreviously: Boolean(preview.transcriptAlreadyShared)
      }
    }
  };
}

async function getBridgeToken() {
  const settings = await getSettings();
  const token = (settings.bridgeToken || "").trim();
  if (!token) {
    throw new Error("Add your browser bridge token in the extension settings before sending.");
  }
  return token;
}

async function loadChatSession(sessionKey = "") {
  const token = await getBridgeToken();
  const clientSessionId = await getClientSessionId();
  const normalizedSessionKey = String(sessionKey || "").trim();

  const requestState = async (label) => callBridge("/session", {
    token,
    timeoutMs: SESSION_BRIDGE_TIMEOUT_MS,
    body: {
      action: "state",
      browserLabel: label,
      clientSessionId,
      sessionKey: normalizedSessionKey || undefined
    }
  });

  if (normalizedSessionKey) {
    return requestState(await getActiveBrowserLabel());
  }

  const hasActivity = (result) =>
    (Array.isArray(result?.messages) && result.messages.length > 0) ||
    Boolean(result?.progress?.running) ||
    Boolean(result?.progress?.error);

  const activeLabel = await getActiveBrowserLabel();
  const primaryResult = await requestState(activeLabel);
  if (hasActivity(primaryResult)) {
    return primaryResult;
  }

  const fallbackLabel = activeLabel === PRIMARY_BROWSER_LABEL ? LEGACY_BROWSER_LABEL : PRIMARY_BROWSER_LABEL;
  const fallbackResult = await requestState(fallbackLabel);
  if (hasActivity(fallbackResult)) {
    await setActiveBrowserLabel(fallbackLabel);
    return fallbackResult;
  }

  return primaryResult;
}

function buildInspectionFallbackFromState(state, browserLabel, fallbackReason = "") {
  const messages = Array.isArray(state?.messages) ? state.messages : [];
  const transcript = messages.map((message, index) => ({
    index: index + 1,
    role: String(message?.role || "").trim(),
    timestamp: String(message?.timestamp || "").trim(),
    content_text: String(message?.content || message?.display_content || "").trim(),
    tool_name: "",
    tool_call_count: 0,
    raw: message
  }));
  const progress = state?.progress && typeof state.progress === "object" ? state.progress : {};
  const activityLog = Array.isArray(progress.activity_log) ? progress.activity_log : [];
  const normalizedReason = String(fallbackReason || "").trim();
  return {
    ...state,
    inspect_generated_at: new Date().toISOString(),
    transcript,
    session_log: null,
    session_log_available: false,
    session_log_exists: false,
    session_log_error: normalizedReason,
    configured_tools: [],
    source_details: {
      platform: String(state?.source || "").trim(),
      session_key: String(state?.session_key || "").trim(),
      browser_label: String(state?.browser_label || browserLabel || "").trim(),
      inspection_mode: "fallback"
    },
    session_record: {},
    paths: {
      hermes_home: "",
      config_path: "",
      logs_dir: "",
      sessions_dir: "",
      session_log_path: ""
    },
    stats: {
      visible_message_count: messages.length,
      transcript_message_count: transcript.length,
      raw_message_count: 0,
      activity_event_count: activityLog.length,
      system_prompt_chars: 0,
      configured_tool_count: 0,
      tool_call_count: 0,
      tool_result_count: 0,
      reasoning_message_count: 0,
      roles: transcript.reduce((acc, message) => {
        const role = String(message.role || "").trim();
        if (role) {
          acc[role] = (acc[role] || 0) + 1;
        }
        return acc;
      }, {}),
      tool_names: [],
      elapsed_seconds: Number(progress.elapsed_seconds || 0),
      last_updated: ""
    }
  };
}

async function inspectChatSession(sessionKey = "") {
  const token = await getBridgeToken();
  const clientSessionId = await getClientSessionId();
  const browserLabel = await getActiveBrowserLabel();
  const normalizedSessionKey = String(sessionKey || "").trim();
  try {
    return await callBridge("/session", {
      token,
      timeoutMs: SESSION_BRIDGE_TIMEOUT_MS,
      body: {
        action: "inspect",
        browserLabel,
        clientSessionId,
        sessionKey: normalizedSessionKey || undefined
      }
    });
  } catch (error) {
    const message = String(error?.message || error || "");
    if (!message.includes("Unsupported browser bridge action: inspect")) {
      throw error;
    }
    const state = await loadChatSession(normalizedSessionKey);
    return buildInspectionFallbackFromState(
      state,
      browserLabel,
      "Inspect mode is unavailable on this bridge. Showing transcript-only fallback."
    );
  }
}

async function listChatSessions(limit = 25, sessionKey = "") {
  const token = await getBridgeToken();
  const clientSessionId = await getClientSessionId();
  const browserLabel = await getActiveBrowserLabel();
  try {
    return await callBridge("/session", {
      token,
      timeoutMs: SESSION_BRIDGE_TIMEOUT_MS,
      body: {
        action: "list",
        browserLabel,
        clientSessionId,
        sessionKey: String(sessionKey || "").trim() || undefined,
        limit
      }
    });
  } catch (error) {
    const message = String(error?.message || error || "");
    if (!message.includes("Unsupported browser bridge action: list")) {
      throw error;
    }
    const state = await loadChatSession(String(sessionKey || "").trim());
    return {
      ...state,
      active_session_key: state.session_key || "",
      sessions: state.session_key
        ? [{
            session_key: state.session_key,
            session_id: state.session_id || "",
            browser_label: browserLabel,
            updated_at: new Date().toISOString(),
            created_at: "",
            message_count: Array.isArray(state.messages) ? state.messages.length : 0,
            last_message_role: "",
            last_message_preview: "",
            running: Boolean(state.progress?.running)
          }]
        : []
    };
  }
}

async function getRuntimeConfig(selectedProvider = "") {
  const token = await getBridgeToken();
  return callBridge("/session", {
    token,
    timeoutMs: SESSION_BRIDGE_TIMEOUT_MS,
    body: {
      action: "runtime_config_get",
      selected_provider: String(selectedProvider || "").trim() || undefined
    }
  });
}

async function saveRuntimeConfig(configPatch = {}, envUpdates = {}, selectedProvider = "") {
  const token = await getBridgeToken();
  return callBridge("/session", {
    token,
    timeoutMs: SESSION_BRIDGE_TIMEOUT_MS,
    body: {
      action: "runtime_config_save",
      config_patch: configPatch && typeof configPatch === "object" ? configPatch : {},
      env_updates: envUpdates && typeof envUpdates === "object" ? envUpdates : {},
      selected_provider: String(selectedProvider || "").trim() || undefined
    }
  });
}

async function getRuntimeProviderModels(selectedProvider = "") {
  const token = await getBridgeToken();
  return callBridge("/session", {
    token,
    timeoutMs: SESSION_BRIDGE_TIMEOUT_MS,
    body: {
      action: "runtime_provider_models",
      selected_provider: String(selectedProvider || "").trim() || undefined
    }
  });
}

async function recallSearch(query, limit = 5, sessionKey = "", dateAwareness = "smart") {
  const token = await getBridgeToken();
  const clientSessionId = await getClientSessionId();
  const browserLabel = await getActiveBrowserLabel();
  return callBridge("/session", {
    token,
    timeoutMs: SESSION_BRIDGE_TIMEOUT_MS,
    body: {
      action: "recall_search",
      browserLabel,
      clientSessionId,
      sessionKey: String(sessionKey || "").trim() || undefined,
      query: String(query || "").trim(),
      limit: Math.max(1, Number(limit || 5) || 5),
      date_awareness: String(dateAwareness || "smart").trim().toLowerCase() || "smart"
    }
  });
}

async function resetChatSession(sessionKey = "", createNew = false) {
  const token = await getBridgeToken();
  let clientSessionId = await getClientSessionId();
  const normalizedSessionKey = createNew ? "" : String(sessionKey || "").trim();
  if (createNew) {
    clientSessionId = await rotateClientSessionId();
  }
  if (!normalizedSessionKey) {
    await setActiveBrowserLabel(PRIMARY_BROWSER_LABEL);
  }
  return callBridge("/session", {
    token,
    timeoutMs: SESSION_BRIDGE_TIMEOUT_MS,
    body: {
      action: "reset",
      browserLabel: PRIMARY_BROWSER_LABEL,
      clientSessionId,
      sessionKey: normalizedSessionKey || undefined
    }
  });
}

async function interruptChatSession(sessionKey = "") {
  const token = await getBridgeToken();
  const clientSessionId = await getClientSessionId();
  const browserLabel = await getActiveBrowserLabel();
  const normalizedSessionKey = String(sessionKey || "").trim();
  return callBridge("/session", {
    token,
    timeoutMs: SESSION_BRIDGE_TIMEOUT_MS,
    body: {
      action: "interrupt",
      browserLabel,
      clientSessionId,
      sessionKey: normalizedSessionKey || undefined
    }
  });
}

async function startChatMessage(
  tabId,
  message,
  sharePage,
  includeTranscript,
  sessionKey = "",
  contextOptions = null,
  attachments = []
) {
  const token = await getBridgeToken();
  const clientSessionId = await getClientSessionId();
  const browserLabel = await getActiveBrowserLabel();
  const normalizedSessionKey = String(sessionKey || "").trim();
  const normalizedAttachments = Array.isArray(attachments)
    ? attachments.filter((attachment) => attachment && typeof attachment === "object")
    : [];
  const body = {
    action: "send_async",
    browserLabel,
    clientSessionId,
    message: message || "",
    sessionKey: normalizedSessionKey || undefined
  };
  if (normalizedAttachments.length) {
    body.attachments = normalizedAttachments;
  }

  let preview = null;
  let sentSelectionLength = 0;
  let sentPageTextLength = 0;
  if (sharePage) {
    const pageContext = await buildPageContextPayload(
      tabId,
      message,
      includeTranscript,
      browserLabel,
      contextOptions
    );
    body.pageContext = pageContext.payload;
    preview = pageContext.preview;
    sentSelectionLength = String(pageContext.payload.selection || "").length;
    sentPageTextLength = String(pageContext.payload.pageText || "").length;
    const sentTranscript = pageContext.payload.transcript || {};
    const sentTranscriptLength = String(sentTranscript.text || "").length;
    console.debug(
      "[Hermes] Sending page context: pageText=" + sentPageTextLength + " chars, selection=" + sentSelectionLength + " chars" +
      (sentTranscriptLength ? ", transcript=" + sentTranscriptLength + " chars" : "") +
      ", url=" + (pageContext.payload.url || "")
    );
  }

  const data = await callBridge("/session", {
    token,
    body
  });

  const transcript = body.pageContext?.transcript || {};
  if (transcript.shared && transcript.key) {
    await markTranscriptShared(transcript.key);
  }

  return {
    ...data,
    preview,
    sent_selection_length: sentSelectionLength,
    sent_page_text_length: sentPageTextLength
  };
}

async function injectPageContext(tabId, note, includeTranscript) {
  const token = await getBridgeToken();
  const browserLabel = await getActiveBrowserLabel();
  const pageContext = await buildPageContextPayload(tabId, note, includeTranscript, browserLabel);
  const data = await callBridge("/inject", {
    token,
    body: pageContext.payload
  });

  const transcript = pageContext.payload.transcript || {};
  if (transcript.shared && transcript.key) {
    await markTranscriptShared(transcript.key);
  }

  return {
    ...data,
    preview: pageContext.preview
  };
}

async function checkBridgeHealth() {
  const settings = await getSettings();
  const response = await fetch(resolveBridgeEndpoint("/health", settings.bridgeUrl), { method: "GET" });
  if (!response.ok) {
    throw new Error(`Health check failed with status ${response.status}.`);
  }
  return response.json();
}

async function generateChatSpeech(text) {
  const token = await getBridgeToken();
  return callBridge("/session", {
    token,
    timeoutMs: TTS_BRIDGE_TIMEOUT_MS,
    body: {
      action: "tts",
      text: String(text || "")
    }
  });
}

async function transcribeChatAudio(audioBase64, mimeType = "audio/webm") {
  const token = await getBridgeToken();
  return callBridge("/session", {
    token,
    timeoutMs: Math.max(TTS_BRIDGE_TIMEOUT_MS, 120000),
    body: {
      action: "transcribe_audio",
      audio_base64: String(audioBase64 || ""),
      mime_type: String(mimeType || "audio/webm")
    }
  });
}

async function openVoiceRecorderWindow(options = {}) {
  const recorderUrl = new URL(chrome.runtime.getURL("voice-recorder.html"));
  if (options && typeof options === "object") {
    if (options.autoStart === true) {
      recorderUrl.searchParams.set("autostart", "1");
    }
    const deviceId = String(options.deviceId || "").trim();
    if (deviceId) {
      recorderUrl.searchParams.set("deviceId", deviceId);
    }
    const captureMode = String(options.captureMode || "").trim().toLowerCase();
    if (captureMode === "speech" || captureMode === "raw") {
      recorderUrl.searchParams.set("captureMode", captureMode);
    }
  }
  return chrome.windows.create({
    url: recorderUrl.toString(),
    type: "popup",
    width: 420,
    height: 560,
    focused: true
  });
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

async function captureVisibleTabImage(tabId) {
  const tab = await getTabSnapshot(tabId);
  if (!tab?.windowId) {
    throw new Error("No active browser tab is available for screen capture.");
  }

  const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, {
    format: "png"
  });
  const url = String(tab.url || "").trim();
  let host = "page";
  try {
    host = new URL(url).hostname.replace(/^www\./i, "") || host;
  } catch (_error) {
    host = "page";
  }

  return {
    data_url: dataUrl,
    mime_type: "image/png",
    size_bytes: estimateDataUrlByteLength(dataUrl),
    name: `${host}-screengrab.png`,
    tab_id: tab.id || null,
    tab_title: String(tab.title || "").trim(),
    tab_url: url
  };
}

async function hasOffscreenRecorderDocument() {
  if (!chrome.runtime.getContexts) {
    return false;
  }
  const contexts = await chrome.runtime.getContexts({
    contextTypes: ["OFFSCREEN_DOCUMENT"],
    documentUrls: [chrome.runtime.getURL(OFFSCREEN_RECORDER_PATH)]
  });
  return Array.isArray(contexts) && contexts.length > 0;
}

async function ensureOffscreenRecorderDocument() {
  if (!chrome.offscreen?.createDocument) {
    throw new Error("Offscreen recording is not supported in this browser.");
  }
  if (await hasOffscreenRecorderDocument()) {
    return;
  }
  if (offscreenRecorderCreation) {
    return offscreenRecorderCreation;
  }

  offscreenRecorderCreation = chrome.offscreen.createDocument({
    url: OFFSCREEN_RECORDER_PATH,
    reasons: ["USER_MEDIA"],
    justification: "Record short voice notes for Hermes sidecar voice input."
  });
  try {
    await offscreenRecorderCreation;
  } finally {
    offscreenRecorderCreation = null;
  }
}

async function dispatchVoiceInputEvent(event) {
  try {
    await chrome.runtime.sendMessage({
      type: "hermes:voice-input-broadcast",
      event
    });
  } catch (_error) {
    // Ignore if no sidecar/options page is actively listening.
  }
}

chrome.runtime.onInstalled.addListener(() => {
  configureSidePanelBehavior().catch((error) => {
    console.debug("Hermes extension: failed to configure side panel behavior", error);
  });
});

chrome.runtime.onStartup?.addListener(() => {
  configureSidePanelBehavior().catch((error) => {
    console.debug("Hermes extension: failed to restore side panel behavior", error);
  });
});

chrome.action.onClicked.addListener(async (tab) => {
  if (!chrome.sidePanel?.open || !tab?.windowId) {
    return;
  }
  try {
    await chrome.sidePanel.open({ windowId: tab.windowId });
  } catch (error) {
    console.debug("Hermes extension: failed to open side panel", error);
  }
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  (async () => {
    if (message.type === "hermes:get-settings") {
      sendResponse({ ok: true, settings: await getSettings() });
      return;
    }

    if (message.type === "hermes:save-settings") {
      await setSettings(message.settings || {});
      sendResponse({ ok: true });
      return;
    }

    if (message.type === "hermes:preview-page-context") {
      const result = await previewPageContext(message.tabId);
      sendResponse({ ok: true, result });
      return;
    }

    if (message.type === "hermes:get-domain-permission-status") {
      const result = await getDomainPermissionStatus(message.tabId);
      sendResponse({ ok: true, result });
      return;
    }

    if (message.type === "hermes:set-domain-permission") {
      const result = await setDomainPermission(message.tabId, Boolean(message.grant));
      sendResponse({ ok: true, result });
      return;
    }

    if (message.type === "hermes:inject-page-context") {
      const result = await injectPageContext(
        message.tabId,
        message.note || "",
        message.includeTranscript
      );
      sendResponse({ ok: true, result });
      return;
    }

    if (message.type === "hermes:get-chat-session") {
      const result = await loadChatSession(message.sessionKey || "");
      sendResponse({ ok: true, result });
      return;
    }

    if (message.type === "hermes:inspect-chat-session") {
      const result = await inspectChatSession(message.sessionKey || "");
      sendResponse({ ok: true, result });
      return;
    }

    if (message.type === "hermes:list-chat-sessions") {
      const result = await listChatSessions(message.limit || 25, message.sessionKey || "");
      sendResponse({ ok: true, result });
      return;
    }

    if (message.type === "hermes:get-runtime-config") {
      const result = await getRuntimeConfig(message.selectedProvider || "");
      sendResponse({ ok: true, result });
      return;
    }

    if (message.type === "hermes:save-runtime-config") {
      const result = await saveRuntimeConfig(
        message.configPatch || {},
        message.envUpdates || {},
        message.selectedProvider || ""
      );
      sendResponse({ ok: true, result });
      return;
    }

    if (message.type === "hermes:get-runtime-provider-models") {
      const result = await getRuntimeProviderModels(message.selectedProvider || "");
      sendResponse({ ok: true, result });
      return;
    }

    if (message.type === "hermes:recall-search") {
      const result = await recallSearch(
        message.query || "",
        message.limit || 5,
        message.sessionKey || "",
        message.dateAwareness || "smart"
      );
      sendResponse({ ok: true, result });
      return;
    }

    if (message.type === "hermes:start-chat-message" || message.type === "hermes:send-chat-message") {
      const result = await startChatMessage(
        message.tabId,
        message.message || "",
        message.sharePage,
        message.includeTranscript,
        message.sessionKey || "",
        message.contextOptions || null,
        message.attachments || []
      );
      sendResponse({ ok: true, result });
      return;
    }

    if (message.type === "hermes:reset-chat-session") {
      const result = await resetChatSession(
        message.sessionKey || "",
        Boolean(message.createNew)
      );
      sendResponse({ ok: true, result });
      return;
    }

    if (message.type === "hermes:interrupt-chat-session") {
      const result = await interruptChatSession(message.sessionKey || "");
      sendResponse({ ok: true, result });
      return;
    }

    if (message.type === "hermes:check-bridge-health") {
      const result = await checkBridgeHealth();
      sendResponse({ ok: true, result });
      return;
    }

    if (message.type === "hermes:speak-chat-message") {
      const result = await generateChatSpeech(message.text || "");
      sendResponse({ ok: true, result });
      return;
    }

    if (message.type === "hermes:transcribe-chat-audio") {
      const result = await transcribeChatAudio(
        message.audioBase64 || "",
        message.mimeType || "audio/webm"
      );
      sendResponse({ ok: true, result });
      return;
    }

    if (message.type === "hermes:open-voice-recorder") {
      const result = await openVoiceRecorderWindow({
        autoStart: message.autoStart === true,
        deviceId: message.deviceId || "",
        captureMode: message.captureMode || "raw"
      });
      sendResponse({ ok: true, result: { windowId: result?.id || null } });
      return;
    }

    if (message.type === "hermes:ensure-offscreen-voice-recorder") {
      await ensureOffscreenRecorderDocument();
      sendResponse({ ok: true, result: { ready: true } });
      return;
    }

    if (message.type === "hermes:capture-visible-tab") {
      const result = await captureVisibleTabImage(message.tabId);
      sendResponse({ ok: true, result });
      return;
    }

    if (message.type === "hermes:start-voice-recording") {
      await ensureOffscreenRecorderDocument();
      const result = { ready: true };
      sendResponse({ ok: true, result });
      return;
    }

    if (message.type === "hermes:stop-voice-recording") {
      await ensureOffscreenRecorderDocument();
      const result = { ready: true };
      sendResponse({ ok: true, result });
      return;
    }

    if (message.type === "hermes:voice-recording-audio") {
      await dispatchVoiceInputEvent({ type: "transcribing" });
      try {
        const result = await transcribeChatAudio(
          message.audioBase64 || "",
          message.mimeType || "audio/webm"
        );
        await dispatchVoiceInputEvent({
          type: "transcript",
          transcript: String(result?.transcript || "")
        });
        sendResponse({ ok: true, result });
      } catch (error) {
        const msg = error instanceof Error ? error.message : String(error);
        await dispatchVoiceInputEvent({ type: "error", error: msg });
        throw error;
      }
      return;
    }

    sendResponse({ ok: false, error: "Unknown message type." });
  })().catch((error) => {
    sendResponse({
      ok: false,
      error: error instanceof Error ? error.message : String(error)
    });
  });

  return true;
});
