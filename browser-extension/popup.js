const pageTitle = document.getElementById("page-title");
const pageUrl = document.getElementById("page-url");
const contentKind = document.getElementById("content-kind");
const selectionLength = document.getElementById("selection-length");
const transcriptStatus = document.getElementById("transcript-status");
const noteInput = document.getElementById("note-input");
const includeTranscript = document.getElementById("include-transcript");
const bridgeUrlInput = document.getElementById("bridge-url");
const bridgeTokenInput = document.getElementById("bridge-token");
const statusText = document.getElementById("status-text");

let activeTabId = null;

window.HermesTheme?.applyThemeToDocument({
  themeName: window.HermesTheme?.defaultThemeId || "obsidian"
});

function setStatus(message) {
  statusText.textContent = message;
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
    return "Hermes Sidecar was reloaded or updated. Reopen the popup and try again.";
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

async function getActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id) {
    throw new Error("No active tab found.");
  }
  activeTabId = tab.id;
  return tab;
}

function renderPreview(result) {
  pageTitle.textContent = result.title || "Untitled page";
  pageUrl.textContent = result.url || "";
  contentKind.textContent = result.contentKind || "web-page";
  selectionLength.textContent = `${result.selectionLength || 0} chars`;

  if (result.transcriptAvailable) {
    if (result.transcriptAlreadyShared) {
      transcriptStatus.textContent = "Already sent";
    } else {
      transcriptStatus.textContent = result.transcriptLanguage
        ? `Ready (${result.transcriptLanguage})`
        : "Ready";
    }
  } else {
    transcriptStatus.textContent = "Not available";
  }
}

async function loadSettings() {
  const response = await sendRuntimeMessage({ type: "hermes:get-settings" });
  const settings = response.settings || {};
  bridgeUrlInput.value = settings.bridgeUrl || "";
  bridgeTokenInput.value = settings.bridgeToken || "";
  includeTranscript.checked = Boolean(settings.includeTranscriptByDefault);
  window.HermesTheme?.applyThemeToDocument(settings);
}

async function saveSettings() {
  await sendRuntimeMessage({
    type: "hermes:save-settings",
    settings: {
      bridgeUrl: bridgeUrlInput.value.trim(),
      bridgeToken: bridgeTokenInput.value.trim(),
      includeTranscriptByDefault: includeTranscript.checked
    }
  });
  await loadSettings();
  setStatus("Settings saved.");
}

async function refreshPreview() {
  const tab = await getActiveTab();
  setStatus("Reading page context...");
  const response = await sendRuntimeMessage({
    type: "hermes:preview-page-context",
    tabId: tab.id
  });
  renderPreview(response.result || {});
  setStatus("Page context is ready to send.");
}

async function sendToHermes() {
  if (!activeTabId) {
    await getActiveTab();
  }
  setStatus("Sending page context to Hermes...");
  const response = await sendRuntimeMessage({
    type: "hermes:inject-page-context",
    tabId: activeTabId,
    note: noteInput.value.trim(),
    includeTranscript: includeTranscript.checked
  });

  const result = response.result || {};
  const lines = ["Context delivered to Hermes."];
  if (result.session_id) {
    lines.push(`Session: ${result.session_id}`);
  }
  if (result.transcript_shared) {
    lines.push("The YouTube transcript was included.");
  } else if (result.transcript_shared_previously) {
    lines.push("The transcript was skipped because it was already shared earlier.");
  }
  if (result.response) {
    lines.push("");
    lines.push("Hermes response:");
    lines.push(result.response);
  }
  setStatus(lines.join("\n"));

  await refreshPreview();
}

async function checkBridge() {
  setStatus("Checking the local bridge...");
  const response = await sendRuntimeMessage({ type: "hermes:check-bridge-health" });
  const result = response.result || {};
  if (result.ok) {
    setStatus(`Bridge is reachable on port ${result.port}.`);
  } else {
    setStatus("Bridge health check returned an unexpected response.");
  }
}

document.getElementById("refresh-button").addEventListener("click", () => {
  refreshPreview().catch((error) => setStatus(error.message));
});

document.getElementById("save-settings-button").addEventListener("click", () => {
  saveSettings().catch((error) => setStatus(error.message));
});

document.getElementById("send-button").addEventListener("click", () => {
  sendToHermes().catch((error) => setStatus(error.message));
});

document.getElementById("health-button").addEventListener("click", () => {
  checkBridge().catch((error) => setStatus(error.message));
});

window.addEventListener("error", (event) => {
  if (!isExtensionContextInvalidated(event?.error || event?.message)) {
    return;
  }
  setStatus(explainExtensionError(event.error || event.message));
  event.preventDefault();
});

window.addEventListener("unhandledrejection", (event) => {
  if (!isExtensionContextInvalidated(event?.reason)) {
    return;
  }
  setStatus(explainExtensionError(event.reason));
  event.preventDefault();
});

(async () => {
  try {
    await loadSettings();
    await refreshPreview();
  } catch (error) {
    setStatus(error.message || String(error));
  }
})();
