function collapseWhitespace(text) {
  return (text || "")
    .replace(/\u00a0/g, " ")
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function clamp(text, maxLength) {
  if (!text) {
    return "";
  }
  if (text.length <= maxLength) {
    return text;
  }
  return text.slice(0, maxLength).trim();
}

function isExtensionContextInvalidated(error) {
  const message = String(error?.message || error || "").toLowerCase();
  return (
    message.includes("extension context invalidated") ||
    message.includes("context invalidated") ||
    message.includes("message port closed before a response was received")
  );
}

function getMetaValue(selectors) {
  for (const selector of selectors) {
    const element = document.querySelector(selector);
    const value = element?.content || element?.getAttribute?.("content") || "";
    if (value && value.trim()) {
      return value.trim();
    }
  }
  return "";
}

function getSelectedText() {
  return clamp(collapseWhitespace(window.getSelection?.().toString() || ""), 8000);
}

function getCurrentUrl() {
  try {
    return new URL(window.location.href);
  } catch (_error) {
    return null;
  }
}

let lastRouteSignalUrl = String(window.location.href || "").trim();
let lastRouteSignalTitle = String(document.title || "").trim();
let routeSignalTimer = null;
let routeWatchInstalled = false;
let routeWatchIntervalId = null;

function getCurrentContentKind() {
  if (isYouTubeWatchPage()) {
    return "youtube-watch";
  }
  if (isXOrTwitterHost()) {
    return "x-feed";
  }
  if (isPdfDocumentPage()) {
    return "pdf-document";
  }
  if (getEmbeddedPdfInfo()) {
    return "pdf-embed";
  }
  return "web-page";
}

function emitRouteChange(reason = "") {
  const url = String(window.location.href || "").trim();
  const title = clamp(String(document.title || "").trim(), 512);
  if (!url) {
    return;
  }
  if (url === lastRouteSignalUrl && title === lastRouteSignalTitle) {
    return;
  }

  lastRouteSignalUrl = url;
  lastRouteSignalTitle = title;

  try {
    chrome.runtime.sendMessage(
      {
        type: "hermes:page-route-changed",
        url,
        title,
        contentKind: getCurrentContentKind(),
        reason: String(reason || "").trim(),
        emittedAt: Date.now()
      },
      () => {
        const runtimeError = chrome.runtime?.lastError;
        if (runtimeError && !isExtensionContextInvalidated(runtimeError)) {
          console.debug("Hermes extension: failed to report route change", runtimeError);
        }
      }
    );
  } catch (error) {
    if (!isExtensionContextInvalidated(error)) {
      console.debug("Hermes extension: failed to emit route change", error);
    }
  }
}

function scheduleRouteChange(reason = "", delayMs = 180) {
  if (routeSignalTimer) {
    clearTimeout(routeSignalTimer);
  }
  routeSignalTimer = setTimeout(() => {
    routeSignalTimer = null;
    emitRouteChange(reason);
  }, Math.max(0, Number(delayMs) || 0));
}

function installRouteWatchers() {
  if (routeWatchInstalled) {
    return;
  }
  routeWatchInstalled = true;

  const patchHistoryMethod = (methodName) => {
    const original = history[methodName];
    if (typeof original !== "function") {
      return;
    }
    history[methodName] = function patchedHistoryMethod(...args) {
      const result = original.apply(this, args);
      scheduleRouteChange(`history.${methodName}`, 120);
      return result;
    };
  };

  patchHistoryMethod("pushState");
  patchHistoryMethod("replaceState");

  window.addEventListener("popstate", () => scheduleRouteChange("popstate", 120), true);
  window.addEventListener("hashchange", () => scheduleRouteChange("hashchange", 120), true);
  document.addEventListener("yt-navigate-start", () => scheduleRouteChange("yt-navigate-start", 60), true);
  document.addEventListener("yt-navigate-finish", () => scheduleRouteChange("yt-navigate-finish", 180), true);
  document.addEventListener("yt-page-data-updated", () => scheduleRouteChange("yt-page-data-updated", 180), true);

  routeWatchIntervalId = window.setInterval(() => {
    const currentUrl = String(window.location.href || "").trim();
    const currentTitle = String(document.title || "").trim();
    if (currentUrl !== lastRouteSignalUrl || currentTitle !== lastRouteSignalTitle) {
      scheduleRouteChange("interval", 120);
    }
  }, 500);
}

function isXOrTwitterHost() {
  const url = getCurrentUrl();
  const host = (url?.hostname || "").toLowerCase();
  return host === "x.com" || host.endsWith(".x.com") || host === "twitter.com" || host.endsWith(".twitter.com");
}

function isPdfUrlLike(value) {
  const source = String(value || "").trim();
  if (!source) {
    return false;
  }
  return /\.pdf(?:[?#]|$)/i.test(source) || source.startsWith("blob:") || source.startsWith("data:application/pdf");
}

function getEmbeddedPdfInfo() {
  const candidates = [
    ...document.querySelectorAll("embed[src], iframe[src], object[data]")
  ];

  for (const node of candidates) {
    const rawUrl =
      node.getAttribute("src") ||
      node.getAttribute("data") ||
      node.src ||
      node.data ||
      "";
    const type = String(node.getAttribute("type") || "").toLowerCase();
    if (type === "application/pdf" || isPdfUrlLike(rawUrl)) {
      return {
        url: String(rawUrl || "").trim(),
        tagName: String(node.tagName || "").toLowerCase()
      };
    }
  }

  return null;
}

function isPdfDocumentPage() {
  const url = window.location.href;
  const mimeType = String(document.contentType || "").toLowerCase();
  if (mimeType === "application/pdf" || isPdfUrlLike(url)) {
    return true;
  }

  return Boolean(
    document.querySelector("embed[type='application/pdf'], object[type='application/pdf']")
  );
}

function isProbablyReadableText(text) {
  const value = collapseWhitespace(text || "");
  if (!value || value.length < 120) {
    return false;
  }

  const words = value.split(/\s+/).filter(Boolean);
  if (words.length < 20) {
    return false;
  }

  const longWords = words.filter((word) => word.length >= 7);
  const sentenceMarks = (value.match(/[.!?:]/g) || []).length;
  return longWords.length >= 8 || sentenceMarks >= 3;
}

function collectTextFromSelectors(selectors, perNodeMax = 1800, totalMax = 12000) {
  const parts = [];
  const seen = new Set();

  for (const selector of selectors) {
    const nodes = document.querySelectorAll(selector);
    for (const node of nodes) {
      const text = clamp(collapseWhitespace(node?.innerText || node?.textContent || ""), perNodeMax);
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
}

function getXTimelineText() {
  if (!isXOrTwitterHost()) {
    return "";
  }

  const parts = [];
  const seen = new Set();

  function addTweetText(text) {
    const normalized = clamp(collapseWhitespace(text || ""), 900);
    if (!normalized || normalized.length < 30 || seen.has(normalized)) {
      return false;
    }
    seen.add(normalized);
    parts.push(normalized);
    return true;
  }

  // 1) Same as web_utils.py / DiscordSam: article[data-testid="tweet"] + div[data-testid="tweetText"]
  const tweetArticles = document.querySelectorAll("article[data-testid='tweet']");
  for (const article of tweetArticles) {
    const tweetText =
      article.querySelector("div[data-testid='tweetText']")?.innerText ||
      article.querySelector("[data-testid='tweetText']")?.innerText ||
      article.querySelector("[lang]")?.innerText ||
      article.innerText ||
      "";
    if (addTweetText(tweetText) && parts.length >= 12) {
      return clamp(parts.join("\n\n"), 12000);
    }
  }

  // 2) Fallback: plain article or [data-testid="tweet"] (div or other)
  const fallbackContainers = document.querySelectorAll("article, [data-testid='tweet']");
  for (const container of fallbackContainers) {
    const tweetText =
      container.querySelector("div[data-testid='tweetText']")?.innerText ||
      container.querySelector("[data-testid='tweetText']")?.innerText ||
      container.querySelector("[lang]")?.innerText ||
      container.innerText ||
      "";
    if (addTweetText(tweetText) && parts.length >= 12) {
      return clamp(parts.join("\n\n"), 12000);
    }
  }

  return clamp(parts.join("\n\n"), 12000);
}

function getVisiblePageText() {
  const embeddedPdf = getEmbeddedPdfInfo();
  if (embeddedPdf) {
    const viewerRoot =
      document.querySelector("main") ||
      document.querySelector("[role='main']") ||
      document.body;
    const text = clamp(collapseWhitespace(viewerRoot?.innerText || ""), 12000);
    if (text) {
      return text;
    }
  }

  // On X, prefer timeline-specific extraction so we send tweet content, not nav/sidebar chrome.
  if (isXOrTwitterHost()) {
    const timelineText = getXTimelineText();
    if (timelineText.length >= 300 || isProbablyReadableText(timelineText)) {
      return timelineText;
    }
  }

  const rootCandidates = [
    document.querySelector("article"),
    document.querySelector("[data-testid='primaryColumn']"),
    document.querySelector("main"),
    document.querySelector("[role='main']"),
    document.body
  ].filter(Boolean);

  let bestText = "";
  for (const root of rootCandidates) {
    const text = clamp(collapseWhitespace(root?.innerText || ""), 12000);
    if (text.length > bestText.length) {
      bestText = text;
    }
    if (isProbablyReadableText(bestText)) {
      return bestText;
    }
  }

  const timelineText = getXTimelineText();
  if (timelineText.length > bestText.length) {
    bestText = timelineText;
  }
  if (isProbablyReadableText(bestText)) {
    return bestText;
  }

  const fallback = collectTextFromSelectors(
    [
      "article[data-testid='tweet'] div[data-testid='tweetText']",
      "article [data-testid='tweetText']",
      "[data-testid='tweet'] [data-testid='tweetText']",
      "article [lang]",
      "[data-testid='tweet'] [lang]",
      "article[data-testid='tweet']",
      "article",
      "[data-testid='tweet']",
      "main article",
      "main section",
      "main div[dir='auto']",
      "[role='main'] article",
      "[role='main'] div[dir='auto']"
    ],
    1000,
    12000
  );
  if (fallback.length > bestText.length) {
    bestText = fallback;
  }

  if (!bestText) {
    bestText = clamp(collapseWhitespace(document.body?.innerText || ""), 12000);
  }

  return bestText;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function getHydrationState() {
  const main =
    document.querySelector("[data-testid='primaryColumn']") ||
    document.querySelector("main") ||
    document.querySelector("[role='main']") ||
    document.body;

  const mainText = collapseWhitespace(main?.innerText || "");
  const articleCount = document.querySelectorAll("article").length;
  const tweetTextCount = document.querySelectorAll(
    "article[data-testid='tweet'] div[data-testid='tweetText'], article [data-testid='tweetText'], article [lang], [data-testid='tweet'] [data-testid='tweetText'], [data-testid='tweet'] [lang], article[data-testid='tweet'], [data-testid='tweet']"
  ).length;

  return {
    mainTextLength: mainText.length,
    articleCount,
    tweetTextCount
  };
}

function isHydratedEnoughForCapture() {
  const state = getHydrationState();
  if (isXOrTwitterHost()) {
    return (
      state.tweetTextCount >= 2 ||
      (state.articleCount >= 2 && state.mainTextLength >= 500)
    );
  }
  return state.mainTextLength >= 400 || state.articleCount >= 1;
}

async function waitForHydratedCapture(timeoutMs = 4500) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (isHydratedEnoughForCapture()) {
      return true;
    }
    await sleep(250);
  }
  return false;
}

async function getVisiblePageTextWithRetry() {
  let best = "";
  for (let attempt = 0; attempt < 4; attempt += 1) {
    const text = getVisiblePageText();
    if (text.length > best.length) {
      best = text;
    }
    if (isProbablyReadableText(best)) {
      return best;
    }
    if (attempt < 3) {
      await sleep(140);
    }
  }
  return best;
}

function getPageDescription() {
  return clamp(
    getMetaValue([
      "meta[name='description']",
      "meta[property='og:description']",
      "meta[name='twitter:description']"
    ]),
    2000
  );
}

function getSiteName() {
  return getMetaValue([
    "meta[property='og:site_name']",
    "meta[name='application-name']"
  ]);
}

function getCanonicalUrl() {
  const canonical = document.querySelector("link[rel='canonical']")?.href || "";
  return canonical.trim();
}

function isYouTubeWatchPage() {
  const url = new URL(window.location.href);
  return /(^|\.)youtube\.com$/i.test(url.hostname) && url.pathname === "/watch" && url.searchParams.has("v");
}

function extractBalancedJson(source, marker) {
  const markerIndex = source.indexOf(marker);
  if (markerIndex === -1) {
    return null;
  }

  const startIndex = source.indexOf("{", markerIndex + marker.length);
  if (startIndex === -1) {
    return null;
  }

  let depth = 0;
  let inString = false;
  let escaped = false;
  for (let index = startIndex; index < source.length; index += 1) {
    const character = source[index];
    if (escaped) {
      escaped = false;
      continue;
    }
    if (character === "\\") {
      escaped = true;
      continue;
    }
    if (character === "\"") {
      inString = !inString;
      continue;
    }
    if (inString) {
      continue;
    }
    if (character === "{") {
      depth += 1;
    } else if (character === "}") {
      depth -= 1;
      if (depth === 0) {
        return source.slice(startIndex, index + 1);
      }
    }
  }
  return null;
}

function findYouTubePlayerResponse() {
  const markers = [
    "var ytInitialPlayerResponse = ",
    "ytInitialPlayerResponse = ",
    "window[\"ytInitialPlayerResponse\"] = ",
    "window['ytInitialPlayerResponse'] = "
  ];

  for (const script of document.scripts) {
    const content = script.textContent || "";
    if (!content) {
      continue;
    }
    for (const marker of markers) {
      const jsonText = extractBalancedJson(content, marker);
      if (!jsonText) {
        continue;
      }
      try {
        return JSON.parse(jsonText);
      } catch (error) {
        console.debug("Hermes extension: failed to parse ytInitialPlayerResponse", error);
      }
    }
  }

  return null;
}

function chooseTranscriptTrack(tracks) {
  if (!Array.isArray(tracks) || !tracks.length) {
    return null;
  }

  const languageHints = [
    document.documentElement.lang,
    navigator.language
  ]
    .filter(Boolean)
    .map((value) => value.toLowerCase());

  const scoreTrack = (track) => {
    const lang = String(track.languageCode || "").toLowerCase();
    let score = 0;
    if (languageHints.some((hint) => hint === lang)) {
      score += 4;
    }
    if (languageHints.some((hint) => hint.startsWith(lang) || lang.startsWith(hint))) {
      score += 2;
    }
    if (lang.startsWith("en")) {
      score += 1;
    }
    if (!track.kind) {
      score += 1;
    }
    return score;
  };

  return [...tracks].sort((left, right) => scoreTrack(right) - scoreTrack(left))[0];
}

function isElementVisible(element) {
  if (!element) {
    return false;
  }
  const rect = element.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}

function collectTranscriptLinesFromDom() {
  const lines = [];
  const seen = new Set();

  const segmentSelectors = [
    "ytd-transcript-segment-renderer #segment-text",
    "ytd-transcript-segment-renderer .segment-text",
    "ytd-transcript-segment-renderer [id='text']"
  ];

  for (const selector of segmentSelectors) {
    const nodes = document.querySelectorAll(selector);
    for (const node of nodes) {
      const line = normalizeTranscriptLine(node?.textContent || "");
      if (!line || seen.has(line)) {
        continue;
      }
      seen.add(line);
      lines.push(line);
    }
    if (lines.length) {
      break;
    }
  }

  return lines;
}

async function fetchTranscriptFromPanelDom() {
  let lines = collectTranscriptLinesFromDom();
  if (lines.length) {
    return lines.join("\n");
  }

  const transcriptButtons = Array.from(
    document.querySelectorAll(
      "button[aria-label], yt-button-shape button[aria-label], yt-formatted-string"
    )
  ).filter((element) => {
    const label = (element.getAttribute?.("aria-label") || "").toLowerCase();
    const text = (element.textContent || "").toLowerCase();
    return (
      label.includes("show transcript") ||
      label === "transcript" ||
      text.includes("show transcript")
    );
  });

  const transcriptButton = transcriptButtons.find((element) => isElementVisible(element));
  if (!transcriptButton) {
    return "";
  }

  transcriptButton.click();

  for (let attempt = 0; attempt < 8; attempt += 1) {
    await new Promise((r) => setTimeout(r, 400));
    lines = collectTranscriptLinesFromDom();
    if (lines.length) {
      return lines.join("\n");
    }
  }

  return "";
}

function decodeHtmlEntities(text) {
  const source = String(text || "");
  if (!source) {
    return "";
  }
  const textarea = document.createElement("textarea");
  textarea.innerHTML = source;
  return textarea.value;
}

function normalizeTranscriptLine(text) {
  return collapseWhitespace(
    decodeHtmlEntities(String(text || "").replace(/<br\s*\/?>/gi, "\n"))
  );
}

function dedupeSequentialTranscriptLines(lines) {
  const deduped = [];
  for (const rawLine of Array.isArray(lines) ? lines : []) {
    const line = normalizeTranscriptLine(rawLine);
    if (!line) {
      continue;
    }
    if (deduped[deduped.length - 1] === line) {
      continue;
    }
    deduped.push(line);
  }
  return deduped;
}

function parseXmlTranscriptText(source) {
  try {
    const doc = new DOMParser().parseFromString(source, "text/xml");
    if (doc.querySelector("parsererror")) {
      return "";
    }

    const textNodes = Array.from(doc.querySelectorAll("text"));
    if (textNodes.length) {
      return dedupeSequentialTranscriptLines(
        textNodes.map((node) => node.textContent || "")
      ).join("\n");
    }

    const paragraphNodes = Array.from(doc.querySelectorAll("p"));
    if (paragraphNodes.length) {
      const paragraphLines = paragraphNodes.map((node) => {
        const segmentNodes = Array.from(node.querySelectorAll("s, span"));
        if (segmentNodes.length) {
          return segmentNodes.map((segment) => segment.textContent || "").join("");
        }
        return node.textContent || "";
      });
      return dedupeSequentialTranscriptLines(paragraphLines).join("\n");
    }
  } catch (_error) {
    return "";
  }

  return "";
}

function parsePlainTranscriptText(source) {
  const normalizedSource = String(source || "").replace(/\r\n/g, "\n");
  if (!normalizedSource.trim()) {
    return "";
  }

  const isTimestampLine = (line) =>
    /^\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?\s+-->\s+\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?/.test(line);

  const lines = [];
  let buffer = [];

  const flushBuffer = () => {
    if (!buffer.length) {
      return;
    }
    lines.push(buffer.join(" "));
    buffer = [];
  };

  for (const rawLine of normalizedSource.split("\n")) {
    const trimmed = rawLine.trim();
    if (!trimmed) {
      flushBuffer();
      continue;
    }
    if (
      /^WEBVTT\b/i.test(trimmed) ||
      /^NOTE\b/i.test(trimmed) ||
      /^\d+$/.test(trimmed) ||
      isTimestampLine(trimmed)
    ) {
      flushBuffer();
      continue;
    }

    const withoutTags = trimmed.replace(/<\/?[^>]+>/g, " ");
    const line = normalizeTranscriptLine(withoutTags);
    if (!line) {
      flushBuffer();
      continue;
    }
    buffer.push(line);
  }

  flushBuffer();
  return dedupeSequentialTranscriptLines(lines).join("\n");
}

function parseYouTubeTranscriptText(rawText) {
  const source = String(rawText || "");
  if (!source.trim()) {
    return "";
  }

  const extractJson3Lines = (jsonValue) => {
    const lines = [];
    for (const event of jsonValue?.events || []) {
      const line = collapseWhitespace(
        (event.segs || [])
          .map((segment) => segment.utf8 || "")
          .join("")
      );
      if (line) {
        lines.push(line);
      }
    }
    return lines;
  };

  try {
    const parsed = JSON.parse(source);
    const lines = extractJson3Lines(parsed);
    if (lines.length) {
      return dedupeSequentialTranscriptLines(lines).join("\n");
    }
  } catch (_error) {
    // Fall through to non-JSON parsing.
  }

  const xmlTranscript = parseXmlTranscriptText(source);
  if (xmlTranscript) {
    return xmlTranscript;
  }

  return parsePlainTranscriptText(source);
}

function getRunText(value) {
  if (!value) {
    return "";
  }
  if (typeof value === "string") {
    return value;
  }
  if (typeof value.simpleText === "string") {
    return value.simpleText;
  }
  if (Array.isArray(value.runs)) {
    return value.runs.map((run) => run?.text || "").join("");
  }
  return "";
}

function extractTranscriptLinesFromYoutubeiPayload(payload) {
  const lines = [];
  const seen = new Set();

  const addLine = (rawText) => {
    const line = normalizeTranscriptLine(rawText || "");
    if (!line || seen.has(line)) {
      return;
    }
    seen.add(line);
    lines.push(line);
  };

  const visit = (node) => {
    if (!node || typeof node !== "object") {
      return;
    }
    if (Array.isArray(node)) {
      for (const item of node) {
        visit(item);
      }
      return;
    }

    if (node.transcriptCueRenderer?.cue) {
      addLine(getRunText(node.transcriptCueRenderer.cue));
    }
    if (node.transcriptSegmentRenderer?.snippet) {
      addLine(getRunText(node.transcriptSegmentRenderer.snippet));
    }
    if (node.transcriptSegmentListRenderer?.initialSegments) {
      visit(node.transcriptSegmentListRenderer.initialSegments);
    }
    if (node.transcriptSearchPanelRenderer?.body) {
      visit(node.transcriptSearchPanelRenderer.body);
    }
    if (node.transcriptBodyRenderer?.cueGroups) {
      visit(node.transcriptBodyRenderer.cueGroups);
    }
    if (node.transcriptCueGroupRenderer?.cues) {
      visit(node.transcriptCueGroupRenderer.cues);
    }
    if (node.cueGroupRenderer?.cues) {
      visit(node.cueGroupRenderer.cues);
    }

    for (const value of Object.values(node)) {
      if (value && typeof value === "object") {
        visit(value);
      }
    }
  };

  visit(payload);
  return lines;
}

function findTranscriptEndpointParams(node) {
  if (!node || typeof node !== "object") {
    return "";
  }
  if (Array.isArray(node)) {
    for (const item of node) {
      const nested = findTranscriptEndpointParams(item);
      if (nested) {
        return nested;
      }
    }
    return "";
  }
  if (node.getTranscriptEndpoint?.params) {
    return String(node.getTranscriptEndpoint.params);
  }
  for (const value of Object.values(node)) {
    const nested = findTranscriptEndpointParams(value);
    if (nested) {
      return nested;
    }
  }
  return "";
}

async function fetchYouTubeTranscriptFromYoutubei() {
  const params = findTranscriptEndpointParams(window.ytInitialData || {});
  if (!params) {
    return "";
  }

  const ytcfgData = window.ytcfg?.data_ || {};
  const context =
    ytcfgData.INNERTUBE_CONTEXT ||
    {
      client: {
        clientName: ytcfgData.INNERTUBE_CLIENT_NAME || "WEB",
        clientVersion: ytcfgData.INNERTUBE_CLIENT_VERSION || ""
      }
    };
  const apiKey = String(ytcfgData.INNERTUBE_API_KEY || "").trim();
  const visitorData = String(
    ytcfgData.VISITOR_DATA ||
    context?.client?.visitorData ||
    ""
  ).trim();
  const clientName = String(ytcfgData.INNERTUBE_CONTEXT_CLIENT_NAME || 1);
  const clientVersion = String(
    ytcfgData.INNERTUBE_CONTEXT_CLIENT_VERSION ||
    ytcfgData.INNERTUBE_CLIENT_VERSION ||
    context?.client?.clientVersion ||
    ""
  );

  const endpoints = [];
  if (apiKey) {
    endpoints.push(`/youtubei/v1/get_transcript?key=${encodeURIComponent(apiKey)}&prettyPrint=false`);
  }
  endpoints.push("/youtubei/v1/get_transcript?prettyPrint=false");

  const body = JSON.stringify({
    context,
    params
  });

  for (const endpoint of endpoints) {
    try {
      const response = await fetch(endpoint, {
        method: "POST",
        credentials: "include",
        cache: "no-store",
        headers: {
          "content-type": "application/json",
          "x-youtube-client-name": clientName,
          "x-youtube-client-version": clientVersion,
          "x-goog-visitor-id": visitorData,
          "x-origin": window.location.origin
        },
        body
      });
      if (!response.ok) {
        continue;
      }

      const rawText = await response.text();
      if (!rawText) {
        continue;
      }
      let payload = null;
      try {
        payload = JSON.parse(rawText);
      } catch (_error) {
        payload = null;
      }
      if (!payload) {
        continue;
      }

      const lines = extractTranscriptLinesFromYoutubeiPayload(payload);
      if (lines.length) {
        return lines.join("\n");
      }
    } catch (_error) {
      // Try the next endpoint.
    }
  }

  return "";
}

async function fetchYouTubeTranscript(includeText) {
  const url = new URL(window.location.href);
  const videoId = url.searchParams.get("v") || "";
  const transcriptBase = {
    available: false,
    key: videoId ? `youtube:${videoId}` : "",
    source: "youtube-captions",
    videoId
  };

  let playerResponse = findYouTubePlayerResponse();
  if (includeText && !playerResponse?.captions?.playerCaptionsTracklistRenderer?.captionTracks?.length) {
    for (let i = 0; i < 6; i += 1) {
      await new Promise((r) => setTimeout(r, 500));
      playerResponse = findYouTubePlayerResponse();
      if (playerResponse?.captions?.playerCaptionsTracklistRenderer?.captionTracks?.length) {
        break;
      }
    }
  }

  const tracks =
    playerResponse?.captions?.playerCaptionsTracklistRenderer?.captionTracks || [];

  if (!tracks.length) {
    return transcriptBase;
  }

  const track = chooseTranscriptTrack(tracks);
  if (!track?.baseUrl) {
    return transcriptBase;
  }

  const transcript = {
    ...transcriptBase,
    available: true,
    language: track.languageCode || "",
    baseUrl: track.baseUrl || "",
    source: "youtube-captions-json3"
  };

  if (!includeText) {
    return transcript;
  }

  let transcriptUrl = track.baseUrl;
  try {
    const parsedTranscriptUrl = new URL(track.baseUrl, window.location.origin);
    parsedTranscriptUrl.searchParams.set("fmt", "json3");
    transcriptUrl = parsedTranscriptUrl.toString();
  } catch (_error) {
    const separator = track.baseUrl.includes("?") ? "&" : "?";
    transcriptUrl = new URL(
      `${track.baseUrl}${separator}fmt=json3`,
      window.location.origin
    ).toString();
  }

  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      const response = await fetch(transcriptUrl, {
        credentials: "same-origin",
        cache: "no-store"
      });
      if (!response.ok) {
        if (attempt < 2) {
          await new Promise((r) => setTimeout(r, 300 * (attempt + 1)));
        }
        continue;
      }

      const rawText = await response.text();
      const transcriptText = parseYouTubeTranscriptText(rawText);
      if (transcriptText) {
        return {
          ...transcript,
          text: clamp(transcriptText, 30000)
        };
      }
    } catch (error) {
      if (attempt === 2) {
        console.debug("Hermes extension: failed to fetch YouTube transcript", error);
      }
    }

    if (attempt < 2) {
      await new Promise((r) => setTimeout(r, 300 * (attempt + 1)));
    }
  }

  const youtubeiTranscriptText = await fetchYouTubeTranscriptFromYoutubei();
  if (youtubeiTranscriptText) {
    return {
      ...transcript,
      text: clamp(youtubeiTranscriptText, 30000),
      source: "youtube-transcript-api"
    };
  }

  const panelTranscriptText = await fetchTranscriptFromPanelDom();
  if (panelTranscriptText) {
    return {
      ...transcript,
      text: clamp(panelTranscriptText, 30000),
      source: "youtube-transcript-panel-dom"
    };
  }

  return {
    ...transcript,
    text: ""
  };
}

async function collectPageContext(includeTranscriptText, waitForHydration = false) {
  if (waitForHydration) {
    const hydrationTimeoutMs = isXOrTwitterHost() ? 9000 : 5000;
    await waitForHydratedCapture(hydrationTimeoutMs);
  }

  const url = getCurrentUrl();
  const title = document.title || "";
  const description = getPageDescription();
  const canonicalUrl = getCanonicalUrl();
  const siteName = getSiteName();
  const selection = getSelectedText();
  let pageText = await getVisiblePageTextWithRetry();
  let contentKind = "web-page";
  const embeddedPdf = getEmbeddedPdfInfo();
  if (isYouTubeWatchPage()) {
    contentKind = "youtube-watch";
  } else if (isXOrTwitterHost()) {
    contentKind = "x-feed";
  } else if (isPdfDocumentPage()) {
    contentKind = "pdf-document";
  } else if (embeddedPdf) {
    contentKind = "pdf-embed";
  }

  const metadata = {
    author: getMetaValue([
      "meta[name='author']",
      "meta[property='article:author']",
      "meta[itemprop='author']"
    ]),
    byline: collapseWhitespace(
      document.querySelector("[rel='author'], .byline, [itemprop='author']")?.textContent || ""
    )
  };

  let transcript = {
    available: false,
    shared: false,
    sharedPreviously: false,
    source: "",
    key: ""
  };

  if (isYouTubeWatchPage()) {
    metadata.videoId = url?.searchParams?.get("v") || "";
    metadata.channelName = collapseWhitespace(
      document.querySelector("ytd-channel-name a, #channel-name a, [itemprop='author']")?.textContent || ""
    );
    metadata.publishedTime = collapseWhitespace(
      document.querySelector("#info-strings, #title + yt-formatted-string, #description-inline-expander #info")?.textContent || ""
    );
    transcript = await fetchYouTubeTranscript(includeTranscriptText);
  } else if (isXOrTwitterHost()) {
    metadata.timelineItems = document.querySelectorAll("article").length;
  } else if (embeddedPdf) {
    metadata.embeddedPdfUrl = embeddedPdf.url;
    metadata.embeddedPdfTag = embeddedPdf.tagName;
  } else if (contentKind === "pdf-document") {
    metadata.pdfUrl = window.location.href;
    metadata.pdfTitle = clamp(document.title || "", 512);
  }

  if (contentKind === "pdf-document" && !(pageText || "").trim()) {
    pageText = `Direct PDF document detected.\nPDF URL: ${window.location.href}`;
    metadata.pageTextSource = metadata.pageTextSource || "pdf-url-fallback";
  } else if (contentKind === "pdf-embed" && !(pageText || "").trim() && embeddedPdf?.url) {
    pageText = `Embedded PDF detected.\nEmbedded PDF URL: ${embeddedPdf.url}`;
    metadata.pageTextSource = metadata.pageTextSource || "pdf-embed-url-fallback";
  }

  // X can render sparse/virtualized text nodes while selection still contains rich text.
  // If selection is significantly larger, promote it so the bridge sends useful context.
  if ((pageText || "").length < 500 && (selection || "").length > (pageText || "").length + 300) {
    pageText = selection;
    metadata.pageTextSource = "selection-fallback";
  }

  return {
    url: window.location.href,
    title: clamp(title, 512),
    description,
    canonicalUrl,
    siteName,
    selection,
    pageText,
    contentKind,
    metadata,
    transcript
  };
}

try {
  installRouteWatchers();

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message.type !== "hermes:collect-page-context") {
      return false;
    }

    collectPageContext(
      Boolean(message.includeTranscriptText),
      Boolean(message.waitForHydration)
    )
      .then((result) => sendResponse(result))
      .catch((error) => {
        sendResponse({
          error: error instanceof Error ? error.message : String(error)
        });
      });

    return true;
  });
} catch (error) {
  if (!isExtensionContextInvalidated(error)) {
    throw error;
  }
  console.debug("Hermes extension: skipped content listener registration after reload.");
}
