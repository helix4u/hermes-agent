const bridgeUrlInput = document.getElementById("bridge-url");
const bridgeTokenInput = document.getElementById("bridge-token");
const sharePageByDefault = document.getElementById("share-page-by-default");
const includeTranscript = document.getElementById("include-transcript");
const enableMicrophoneButton = document.getElementById("enable-microphone-button");
const microphoneStatusText = document.getElementById("microphone-status-text");
const audioInputDeviceSelect = document.getElementById("audio-input-device-select");
const themeSelect = document.getElementById("theme-select");
const themeDescription = document.getElementById("theme-description");
const customThemeList = document.getElementById("custom-theme-list");
const addThemeButton = document.getElementById("add-theme-button");
const showQuickPrompts = document.getElementById("show-quick-prompts");
const showChallengeMode = document.getElementById("show-challenge-mode");
const challengeModeLabel = document.getElementById("challenge-mode-label");
const challengeModePrompt = document.getElementById("challenge-mode-prompt");
const quickPromptList = document.getElementById("quick-prompt-list");
const addQuickPromptButton = document.getElementById("add-quick-prompt-button");
const bridgeStatusText = document.getElementById("bridge-status-text");
const wikiBaseUrlInput = document.getElementById("wiki-base-url");
const sidecarActivityLogLevelSelect = document.getElementById("sidecar-activity-log-level");
const activityLogPanelOpenCheckbox = document.getElementById("activity-log-panel-open");
const openWikiButton = document.getElementById("open-wiki-button");
const openControlRoomButton = document.getElementById("open-control-room-button");
const localServicesFeedback = document.getElementById("local-services-feedback");
const statusText = document.getElementById("status-text");
const runtimeProviderSelect = document.getElementById("runtime-provider-select");
const runtimeModelSelect = document.getElementById("runtime-model-select");
const runtimeModelInput = document.getElementById("runtime-model-input");
const runtimeBaseUrlInput = document.getElementById("runtime-base-url");
const runtimeApiModeSelect = document.getElementById("runtime-api-mode");
const providerAuthStatus = document.getElementById("provider-auth-status");
const providerApiKeyInput = document.getElementById("provider-api-key-input");
const providerEnvBaseUrlInput = document.getElementById("provider-env-base-url-input");
const providerEnvHint = document.getElementById("provider-env-hint");
const ttsProviderSelect = document.getElementById("tts-provider-select");
const ttsEdgeVoiceInput = document.getElementById("tts-edge-voice");
const ttsOpenaiModelInput = document.getElementById("tts-openai-model");
const ttsOpenaiVoiceInput = document.getElementById("tts-openai-voice");
const ttsKokoroBaseUrlInput = document.getElementById("tts-kokoro-base-url");
const ttsKokoroVoiceInput = document.getElementById("tts-kokoro-voice");
const sttProviderSelect = document.getElementById("stt-provider-select");
const sttLocalModelInput = document.getElementById("stt-local-model");
const sttOpenaiModelInput = document.getElementById("stt-openai-model");
const sttEnabledCheckbox = document.getElementById("stt-enabled");
const terminalBackendSelect = document.getElementById("terminal-backend-select");
const terminalTimeoutInput = document.getElementById("terminal-timeout-input");
const terminalWindowsShellSelect = document.getElementById("terminal-windows-shell-select");
const terminalCwdInput = document.getElementById("terminal-cwd-input");
const terminalPersistentShellCheckbox = document.getElementById("terminal-persistent-shell");
const terminalDockerMountCwdCheckbox = document.getElementById("terminal-docker-mount-cwd");
const webBackendSelect = document.getElementById("web-backend-select");
const archiveFallbackEnabledCheckbox = document.getElementById("archive-fallback-enabled");
const archiveServiceSelect = document.getElementById("archive-service-select");
const archiveFallbackToOriginalCheckbox = document.getElementById("archive-fallback-to-original");
const archivePaywalledDomainsInput = document.getElementById("archive-paywalled-domains");
const delegationProviderInput = document.getElementById("delegation-provider-input");
const delegationModelInput = document.getElementById("delegation-model-input");
const delegationBaseUrlInput = document.getElementById("delegation-base-url-input");

const THEME_GROUP_ORDER = [
  "Monochrome dark",
  "Light themes",
  "Original",
  "Sepia",
  "Retro",
  "Custom themes"
];

let quickPromptDrafts = [];
let customThemeDrafts = [];
let currentThemeAccent = window.HermesTheme?.defaultCustomThemePrimary || "#8b5cf6";
let themePreviewSaveTimer = null;
let runtimeConfigState = null;

window.HermesTheme?.applyThemeToDocument({
  themeName: window.HermesTheme?.defaultThemeId || "obsidian"
});

function setStatus(message) {
  statusText.textContent = message;
}

function setLocalServicesFeedback(message, isError = false) {
  if (!localServicesFeedback) {
    return;
  }
  localServicesFeedback.textContent = message || "";
  localServicesFeedback.classList.toggle("has-error", Boolean(isError && message));
}

function openControlRoomTab() {
  const url = chrome.runtime.getURL("control-room.html");
  chrome.tabs.create({ url }, () => {
    const err = chrome.runtime.lastError;
    if (err) {
      const text = err.message || String(err);
      setLocalServicesFeedback(text, true);
      setStatus(text);
    } else {
      setLocalServicesFeedback("Control room opened in a new tab.");
    }
  });
}

function setBridgeStatus(message) {
  bridgeStatusText.textContent = message;
}

function setMicrophoneStatus(message) {
  if (microphoneStatusText) {
    microphoneStatusText.textContent = message;
  }
}

function getSelectedProviderInfo() {
  const providerId = String(runtimeProviderSelect?.value || "").trim();
  const providers = Array.isArray(runtimeConfigState?.providers) ? runtimeConfigState.providers : [];
  return providers.find((provider) => String(provider.id || "").trim() === providerId) || null;
}

function populateRuntimeProviderOptions(selectedProvider = "") {
  if (!runtimeProviderSelect) {
    return;
  }
  runtimeProviderSelect.textContent = "";
  const providers = Array.isArray(runtimeConfigState?.providers) ? runtimeConfigState.providers : [];
  for (const provider of providers) {
    const option = document.createElement("option");
    option.value = String(provider.id || "").trim();
    const authMarker = provider.authenticated ? "configured" : "not configured";
    option.textContent = `${provider.label || provider.id} (${authMarker})`;
    runtimeProviderSelect.appendChild(option);
  }
  const fallback = providers[0]?.id || "openrouter";
  runtimeProviderSelect.value = providers.some((provider) => provider.id === selectedProvider)
    ? selectedProvider
    : fallback;
}

function populateRuntimeModelOptions(selectedModel = "") {
  if (!runtimeModelSelect) {
    return;
  }
  runtimeModelSelect.textContent = "";
  const models = Array.isArray(runtimeConfigState?.provider_models) ? runtimeConfigState.provider_models : [];
  const defaultOption = document.createElement("option");
  defaultOption.value = "";
  defaultOption.textContent = "Use provider default / custom override";
  runtimeModelSelect.appendChild(defaultOption);
  for (const model of models) {
    const option = document.createElement("option");
    option.value = String(model.id || "").trim();
    option.textContent = String(model.id || "").trim();
    option.title = String(model.description || "").trim();
    runtimeModelSelect.appendChild(option);
  }
  const hasSelected = Array.from(runtimeModelSelect.options).some((option) => option.value === selectedModel);
  runtimeModelSelect.value = hasSelected ? selectedModel : "";
}

function updateProviderAuthUi() {
  const provider = getSelectedProviderInfo();
  if (!provider) {
    if (providerAuthStatus) {
      providerAuthStatus.textContent = "Provider metadata unavailable.";
    }
    if (providerEnvHint) {
      providerEnvHint.textContent = "";
    }
    return;
  }
  const status = provider.status || {};
  const authSummary = status.logged_in
    ? "Authenticated"
    : status.configured
      ? "Configured via API key"
      : "Not configured yet";
  if (providerAuthStatus) {
    providerAuthStatus.textContent = `${provider.label || provider.id}: ${authSummary}.`;
  }
  if (providerEnvHint) {
    const apiKeyVars = Array.isArray(provider.api_key_env_vars) && provider.api_key_env_vars.length
      ? provider.api_key_env_vars.join(", ")
      : "(no API key env var metadata)";
    const baseUrlVar = String(provider.base_url_env_var || "").trim();
    providerEnvHint.textContent = baseUrlVar
      ? `API key env: ${apiKeyVars}. Base URL env: ${baseUrlVar}.`
      : `API key env: ${apiKeyVars}.`;
  }
  if (providerEnvBaseUrlInput) {
    providerEnvBaseUrlInput.value = String(provider.base_url_value || "").trim();
    providerEnvBaseUrlInput.dataset.originalValue = String(provider.base_url_value || "").trim();
  }
}

async function loadRuntimeProviderModels(providerId, selectedModel = "") {
  if (!providerId) {
    runtimeConfigState = {
      ...(runtimeConfigState || {}),
      provider_models: []
    };
    populateRuntimeModelOptions(selectedModel);
    return;
  }
  const response = await sendRuntimeMessage({
    type: "hermes:get-runtime-provider-models",
    selectedProvider: providerId
  });
  runtimeConfigState = {
    ...(runtimeConfigState || {}),
    provider_models: response.result?.provider_models || []
  };
  populateRuntimeModelOptions(selectedModel);
}

async function loadRuntimeConfig(selectedProvider = "") {
  const response = await sendRuntimeMessage({
    type: "hermes:get-runtime-config",
    selectedProvider
  });
  runtimeConfigState = response.result || {};
  const config = runtimeConfigState.config || {};
  const modelConfig = config.model || {};
  const provider = String(modelConfig.provider || "auto").trim() || "auto";
  const model = String(modelConfig.default || "").trim();

  populateRuntimeProviderOptions(provider);
  await loadRuntimeProviderModels(runtimeProviderSelect?.value || provider, model);
  if (runtimeModelInput) {
    runtimeModelInput.value = model;
  }
  if (runtimeBaseUrlInput) {
    runtimeBaseUrlInput.value = String(modelConfig.base_url || "").trim();
  }
  if (runtimeApiModeSelect) {
    runtimeApiModeSelect.value = String(modelConfig.api_mode || "").trim();
  }

  const tts = config.tts || {};
  if (ttsProviderSelect) {
    ttsProviderSelect.value = String(tts.provider || "edge").trim() || "edge";
  }
  if (ttsEdgeVoiceInput) {
    ttsEdgeVoiceInput.value = String(tts.edge?.voice || "").trim();
  }
  if (ttsOpenaiModelInput) {
    ttsOpenaiModelInput.value = String(tts.openai?.model || "").trim();
  }
  if (ttsOpenaiVoiceInput) {
    ttsOpenaiVoiceInput.value = String(tts.openai?.voice || "").trim();
  }
  if (ttsKokoroBaseUrlInput) {
    ttsKokoroBaseUrlInput.value = String(tts.kokoro?.base_url || "").trim();
  }
  if (ttsKokoroVoiceInput) {
    ttsKokoroVoiceInput.value = String(tts.kokoro?.voice || "").trim();
  }

  const stt = config.stt || {};
  if (sttProviderSelect) {
    sttProviderSelect.value = String(stt.provider || "local").trim() || "local";
  }
  if (sttLocalModelInput) {
    sttLocalModelInput.value = String(stt.local?.model || "").trim();
  }
  if (sttOpenaiModelInput) {
    sttOpenaiModelInput.value = String(stt.openai?.model || "").trim();
  }
  if (sttEnabledCheckbox) {
    sttEnabledCheckbox.checked = stt.enabled !== false;
  }

  const terminal = config.terminal || {};
  if (terminalBackendSelect) {
    terminalBackendSelect.value = String(terminal.backend || "local").trim() || "local";
  }
  if (terminalTimeoutInput) {
    terminalTimeoutInput.value = String(terminal.timeout ?? 180);
  }
  if (terminalWindowsShellSelect) {
    const preferredShell = String(terminal.windows_shell || "auto").trim().toLowerCase();
    terminalWindowsShellSelect.value =
      preferredShell === "cmd" || preferredShell === "powershell" || preferredShell === "wsl"
        ? preferredShell
        : "auto";
  }
  if (terminalCwdInput) {
    terminalCwdInput.value = String(terminal.cwd || ".").trim() || ".";
  }
  if (terminalPersistentShellCheckbox) {
    terminalPersistentShellCheckbox.checked = terminal.persistent_shell !== false;
  }
  if (terminalDockerMountCwdCheckbox) {
    terminalDockerMountCwdCheckbox.checked = terminal.docker_mount_cwd_to_workspace === true;
  }

  const webConfig = config.web || {};
  if (webBackendSelect) {
    webBackendSelect.value = String(webConfig.backend || "").trim();
  }
  const archiveFallback = webConfig.archive_fallback || {};
  if (archiveFallbackEnabledCheckbox) {
    archiveFallbackEnabledCheckbox.checked = archiveFallback.enabled === true;
  }
  if (archiveServiceSelect) {
    archiveServiceSelect.value = String(archiveFallback.service || "archive.today").trim() || "archive.today";
  }
  if (archiveFallbackToOriginalCheckbox) {
    archiveFallbackToOriginalCheckbox.checked = archiveFallback.fallback_to_original !== false;
  }
  if (archivePaywalledDomainsInput) {
    const paywalledDomains = Array.isArray(archiveFallback.paywalled_domains)
      ? archiveFallback.paywalled_domains.join(", ")
      : "";
    archivePaywalledDomainsInput.value = paywalledDomains;
  }

  const delegation = config.delegation || {};
  if (delegationProviderInput) {
    delegationProviderInput.value = String(delegation.provider || "").trim();
  }
  if (delegationModelInput) {
    delegationModelInput.value = String(delegation.model || "").trim();
  }
  if (delegationBaseUrlInput) {
    delegationBaseUrlInput.value = String(delegation.base_url || "").trim();
  }
  updateProviderAuthUi();
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
    return "Hermes Sidecar was reloaded or updated. Reload the options page to reconnect.";
  }
  return String(error?.message || error || "Unknown extension error.");
}

function createPromptDraft(prompt = {}) {
  return {
    id: String(prompt.id || "").trim() || crypto.randomUUID(),
    label: String(prompt.label || "").trim(),
    template: String(prompt.template || "").trim(),
    includeTranscript: Boolean(prompt.includeTranscript)
  };
}

function createCustomThemeDraft(theme = {}, index = 0) {
  const normalized = window.HermesTheme?.normalizeCustomThemeDefinition?.(theme, index) || {
    id: String(theme.id || "").trim() || `custom-theme-${index + 1}`,
    label: String(theme.label || "").trim() || `Custom Theme ${index + 1}`,
    mode: String(theme.mode || "").trim().toLowerCase() === "light" ? "light" : "dark",
    primaryColor: window.HermesTheme?.normalizeHexColor(theme.primaryColor, "#8b5cf6") || "#8b5cf6",
    secondaryColor: window.HermesTheme?.normalizeHexColor(theme.secondaryColor, "#22d3ee") || "#22d3ee",
    textColor: window.HermesTheme?.normalizeHexColor(theme.textColor, "#f8fafc") || "#f8fafc",
    mutedTextColor: window.HermesTheme?.normalizeHexColor(theme.mutedTextColor, "#94a3b8") || "#94a3b8",
    surfaceColor: window.HermesTheme?.normalizeHexColor(theme.surfaceColor, "#1b1a25") || "#1b1a25",
    fieldColor: window.HermesTheme?.normalizeHexColor(theme.fieldColor, "#11131d") || "#11131d",
    fieldTextColor: window.HermesTheme?.normalizeHexColor(theme.fieldTextColor, "#f8fafc") || "#f8fafc"
  };
  return { ...normalized };
}

function getThemeSettingsForPreview(themeName = themeSelect.value) {
  return {
    themeName,
    customThemeAccent: currentThemeAccent,
    customThemes: customThemeDrafts
  };
}

function populateThemeOptions({ selectedThemeId = themeSelect.value } = {}) {
  const entries = window.HermesTheme?.getThemePresetEntries?.({
    customThemes: customThemeDrafts
  }) || [];

  const groups = new Map(
    THEME_GROUP_ORDER.map((groupLabel) => [groupLabel, { label: groupLabel, options: [] }])
  );

  entries.forEach((entry) => {
    if (!groups.has(entry.group)) {
      groups.set(entry.group, { label: entry.group, options: [] });
    }
    groups.get(entry.group).options.push(entry);
  });

  themeSelect.textContent = "";
  for (const group of groups.values()) {
    if (!group.options.length) {
      continue;
    }
    const optgroup = document.createElement("optgroup");
    optgroup.label = group.label;
    group.options.forEach((entry) => {
      const option = document.createElement("option");
      option.value = entry.id;
      option.textContent = entry.label;
      option.title = entry.description;
      optgroup.appendChild(option);
    });
    themeSelect.appendChild(optgroup);
  }

  const hasSelectedTheme =
    selectedThemeId &&
    Array.from(themeSelect.options).some((option) => option.value === selectedThemeId);
  themeSelect.value = hasSelectedTheme
    ? selectedThemeId
    : window.HermesTheme?.defaultThemeId || "obsidian";
}

function updateThemeDescription(themeName = themeSelect.value) {
  if (!themeDescription) {
    return;
  }
  const resolved = window.HermesTheme?.resolveThemePalette?.(
    getThemeSettingsForPreview(themeName)
  );
  if (!resolved) {
    themeDescription.textContent = "";
    return;
  }
  const modeLabel = resolved.mode === "light" ? "Light mode" : "Dark mode";
  const groupLabel = resolved.group ? `${resolved.group}. ` : "";
  themeDescription.textContent = `${modeLabel}. ${groupLabel}${resolved.description}`;
}

function applyThemePreview(themeName = themeSelect.value) {
  const selectedCustomTheme = customThemeDrafts.find((theme) => theme.id === themeName);
  if (selectedCustomTheme) {
    currentThemeAccent = selectedCustomTheme.primaryColor;
  }
  updateThemeDescription(themeName);
  window.HermesTheme?.applyThemeToDocument(getThemeSettingsForPreview(themeName));
}

function scheduleThemePreviewSave(savedPrefix = "Custom theme saved.") {
  if (themePreviewSaveTimer) {
    clearTimeout(themePreviewSaveTimer);
  }
  themePreviewSaveTimer = setTimeout(() => {
    themePreviewSaveTimer = null;
    const { settings } = buildSettingsPayload();
    sendRuntimeMessage({
      type: "hermes:save-settings",
      settings: {
        themeName: settings.themeName,
        customThemeAccent: settings.customThemeAccent,
        customThemes: settings.customThemes
      }
    })
      .then(() => setStatus(savedPrefix))
      .catch((error) => setStatus(error.message || String(error)));
  }, 120);
}

function renderQuickPromptList() {
  quickPromptList.textContent = "";

  if (!quickPromptDrafts.length) {
    const emptyState = document.createElement("p");
    emptyState.className = "empty-list";
    emptyState.textContent = "No quick prompts yet. Add one to create a reusable sidecar button.";
    quickPromptList.appendChild(emptyState);
    return;
  }

  quickPromptDrafts.forEach((prompt, index) => {
    const card = document.createElement("section");
    card.className = "prompt-card";

    const head = document.createElement("div");
    head.className = "prompt-card-head";

    const title = document.createElement("p");
    title.className = "prompt-card-title";
    title.textContent = prompt.label || `Prompt ${index + 1}`;
    head.appendChild(title);

    const actions = document.createElement("div");
    actions.className = "prompt-card-actions";

    const moveUpButton = document.createElement("button");
    moveUpButton.className = "ghost-button small-button";
    moveUpButton.type = "button";
    moveUpButton.textContent = "Move up";
    moveUpButton.disabled = index === 0;
    moveUpButton.addEventListener("click", () => {
      const previous = quickPromptDrafts[index - 1];
      quickPromptDrafts[index - 1] = quickPromptDrafts[index];
      quickPromptDrafts[index] = previous;
      renderQuickPromptList();
    });
    actions.appendChild(moveUpButton);

    const moveDownButton = document.createElement("button");
    moveDownButton.className = "ghost-button small-button";
    moveDownButton.type = "button";
    moveDownButton.textContent = "Move down";
    moveDownButton.disabled = index === quickPromptDrafts.length - 1;
    moveDownButton.addEventListener("click", () => {
      const next = quickPromptDrafts[index + 1];
      quickPromptDrafts[index + 1] = quickPromptDrafts[index];
      quickPromptDrafts[index] = next;
      renderQuickPromptList();
    });
    actions.appendChild(moveDownButton);

    const removeButton = document.createElement("button");
    removeButton.className = "ghost-button small-button danger-button";
    removeButton.type = "button";
    removeButton.textContent = "Remove";
    removeButton.addEventListener("click", () => {
      quickPromptDrafts.splice(index, 1);
      renderQuickPromptList();
    });
    actions.appendChild(removeButton);

    head.appendChild(actions);
    card.appendChild(head);

    const labelField = document.createElement("label");
    labelField.className = "field";
    const labelSpan = document.createElement("span");
    labelSpan.className = "field-label";
    labelSpan.textContent = "Button label";
    const labelInput = document.createElement("input");
    labelInput.type = "text";
    labelInput.maxLength = 60;
    labelInput.spellcheck = false;
    labelInput.value = prompt.label;
    labelInput.addEventListener("input", () => {
      quickPromptDrafts[index].label = labelInput.value;
      title.textContent = labelInput.value.trim() || `Prompt ${index + 1}`;
    });
    labelField.appendChild(labelSpan);
    labelField.appendChild(labelInput);
    card.appendChild(labelField);

    const promptField = document.createElement("label");
    promptField.className = "field";
    const promptSpan = document.createElement("span");
    promptSpan.className = "field-label";
    promptSpan.textContent = "Prompt text";
    const promptInput = document.createElement("textarea");
    promptInput.rows = 4;
    promptInput.value = prompt.template;
    promptInput.addEventListener("input", () => {
      quickPromptDrafts[index].template = promptInput.value;
    });
    promptField.appendChild(promptSpan);
    promptField.appendChild(promptInput);
    card.appendChild(promptField);

    const transcriptRow = document.createElement("label");
    transcriptRow.className = "checkbox-row tight-row";
    const transcriptInput = document.createElement("input");
    transcriptInput.type = "checkbox";
    transcriptInput.checked = prompt.includeTranscript;
    transcriptInput.addEventListener("change", () => {
      quickPromptDrafts[index].includeTranscript = transcriptInput.checked;
    });
    const transcriptCopy = document.createElement("span");
    transcriptCopy.textContent = "Force-include the transcript when this prompt is used";
    transcriptRow.appendChild(transcriptInput);
    transcriptRow.appendChild(transcriptCopy);
    card.appendChild(transcriptRow);

    quickPromptList.appendChild(card);
  });
}

function renderCustomThemeList() {
  customThemeList.textContent = "";

  if (!customThemeDrafts.length) {
    const emptyState = document.createElement("p");
    emptyState.className = "empty-list";
    emptyState.textContent = "No custom themes yet. Add one to create a named palette you can select from the dropdown.";
    customThemeList.appendChild(emptyState);
    return;
  }

  customThemeDrafts.forEach((theme, index) => {
    const card = document.createElement("section");
    card.className = "prompt-card";

    const head = document.createElement("div");
    head.className = "prompt-card-head";

    const title = document.createElement("p");
    title.className = "prompt-card-title";
    title.textContent = theme.label;
    head.appendChild(title);

    const actions = document.createElement("div");
    actions.className = "prompt-card-actions";

    const useButton = document.createElement("button");
    useButton.className = "ghost-button small-button";
    useButton.type = "button";
    useButton.textContent = themeSelect.value === theme.id ? "Selected" : "Use theme";
    useButton.disabled = themeSelect.value === theme.id;
    useButton.addEventListener("click", () => {
      populateThemeOptions({ selectedThemeId: theme.id });
      applyThemePreview(theme.id);
    });
    actions.appendChild(useButton);

    const removeButton = document.createElement("button");
    removeButton.className = "ghost-button small-button danger-button";
    removeButton.type = "button";
    removeButton.textContent = "Remove";
    removeButton.addEventListener("click", () => {
      const wasSelected = themeSelect.value === theme.id;
      customThemeDrafts.splice(index, 1);
      populateThemeOptions({
        selectedThemeId: wasSelected ? window.HermesTheme?.defaultThemeId || "obsidian" : themeSelect.value
      });
      renderCustomThemeList();
      applyThemePreview(themeSelect.value);
    });
    actions.appendChild(removeButton);

    head.appendChild(actions);
    card.appendChild(head);

    const nameField = document.createElement("label");
    nameField.className = "field";
    const nameLabel = document.createElement("span");
    nameLabel.className = "field-label";
    nameLabel.textContent = "Theme name";
    const nameInput = document.createElement("input");
    nameInput.type = "text";
    nameInput.maxLength = 60;
    nameInput.spellcheck = false;
    nameInput.value = theme.label;
    nameInput.addEventListener("input", () => {
      customThemeDrafts[index].label = nameInput.value;
      title.textContent = nameInput.value.trim() || `Custom Theme ${index + 1}`;
    });
    nameInput.addEventListener("change", () => {
      const previousId = customThemeDrafts[index].id;
      customThemeDrafts[index] = createCustomThemeDraft(customThemeDrafts[index], index);
      const selectedThemeId = themeSelect.value === previousId ? customThemeDrafts[index].id : themeSelect.value;
      populateThemeOptions({ selectedThemeId });
      renderCustomThemeList();
      applyThemePreview(themeSelect.value);
    });
    nameField.appendChild(nameLabel);
    nameField.appendChild(nameInput);
    card.appendChild(nameField);

    const modeField = document.createElement("label");
    modeField.className = "field";
    const modeLabel = document.createElement("span");
    modeLabel.className = "field-label";
    modeLabel.textContent = "Mode";
    const modeSelect = document.createElement("select");
    const darkOption = document.createElement("option");
    darkOption.value = "dark";
    darkOption.textContent = "Dark";
    const lightOption = document.createElement("option");
    lightOption.value = "light";
    lightOption.textContent = "Light";
    modeSelect.appendChild(darkOption);
    modeSelect.appendChild(lightOption);
    modeSelect.value = theme.mode;
    modeSelect.addEventListener("change", () => {
      customThemeDrafts[index] = createCustomThemeDraft({
        ...customThemeDrafts[index],
        mode: modeSelect.value
      }, index);
      populateThemeOptions({ selectedThemeId: themeSelect.value });
      renderCustomThemeList();
      if (themeSelect.value === customThemeDrafts[index].id) {
        applyThemePreview(themeSelect.value);
      }
    });
    modeField.appendChild(modeLabel);
    modeField.appendChild(modeSelect);
    card.appendChild(modeField);

    const colorGrid = document.createElement("div");
    colorGrid.className = "theme-color-grid";

    const createColorEditor = (labelText, colorKey, fallback) => {
      const wrapper = document.createElement("label");
      wrapper.className = "field color-field";
      const label = document.createElement("span");
      label.className = "field-label";
      label.textContent = labelText;

      const row = document.createElement("div");
      row.className = "color-input-row";

      const picker = document.createElement("input");
      picker.type = "color";
      picker.value = theme[colorKey];

      const hexInput = document.createElement("input");
      hexInput.type = "text";
      hexInput.maxLength = 7;
      hexInput.spellcheck = false;
      hexInput.value = theme[colorKey];

      const applyColor = (rawValue, { persist = false } = {}) => {
        const normalized =
          window.HermesTheme?.normalizeHexColor(rawValue, fallback) || fallback;
        picker.value = normalized;
        hexInput.value = normalized;
        customThemeDrafts[index][colorKey] = normalized;
        if (themeSelect.value === customThemeDrafts[index].id) {
          currentThemeAccent = customThemeDrafts[index].primaryColor;
          applyThemePreview(themeSelect.value);
          if (persist) {
            scheduleThemePreviewSave();
          }
        }
      };

      picker.addEventListener("input", () => applyColor(picker.value, { persist: true }));
      picker.addEventListener("change", () => applyColor(picker.value, { persist: true }));
      hexInput.addEventListener("input", () => applyColor(hexInput.value, { persist: true }));
      hexInput.addEventListener("change", () => applyColor(hexInput.value, { persist: true }));

      row.appendChild(picker);
      row.appendChild(hexInput);
      wrapper.appendChild(label);
      wrapper.appendChild(row);
      return wrapper;
    };

    colorGrid.appendChild(
      createColorEditor(
        "Primary color",
        "primaryColor",
        window.HermesTheme?.defaultCustomThemePrimary || "#8b5cf6"
      )
    );
    colorGrid.appendChild(
      createColorEditor(
        "Secondary color",
        "secondaryColor",
        window.HermesTheme?.defaultCustomThemeSecondary || "#22d3ee"
      )
    );
    colorGrid.appendChild(
      createColorEditor(
        "Text color",
        "textColor",
        theme.mode === "light" ? "#111827" : "#f8fafc"
      )
    );
    colorGrid.appendChild(
      createColorEditor(
        "Muted text",
        "mutedTextColor",
        theme.mode === "light" ? "#475569" : "#94a3b8"
      )
    );
    colorGrid.appendChild(
      createColorEditor(
        "Surface color",
        "surfaceColor",
        theme.mode === "light" ? "#ffffff" : "#1b1a25"
      )
    );
    colorGrid.appendChild(
      createColorEditor(
        "Field color",
        "fieldColor",
        theme.mode === "light" ? "#ffffff" : "#11131d"
      )
    );
    colorGrid.appendChild(
      createColorEditor(
        "Field text",
        "fieldTextColor",
        theme.mode === "light" ? "#111827" : "#f8fafc"
      )
    );

    card.appendChild(colorGrid);
    customThemeList.appendChild(card);
  });
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

async function loadSettings() {
  const response = await sendRuntimeMessage({ type: "hermes:get-settings" });
  const settings = response.settings || {};
  const themeSettings = window.HermesTheme?.normalizeThemeSettings(settings) || {
    themeName: window.HermesTheme?.defaultThemeId || "obsidian",
    customThemeAccent: window.HermesTheme?.defaultCustomThemePrimary || "#8b5cf6",
    customThemes: []
  };
  bridgeUrlInput.value = settings.bridgeUrl || "";
  bridgeTokenInput.value = settings.bridgeToken || "";
  await loadAudioInputDevices(settings.audioInputDeviceId || "");
  sharePageByDefault.checked = settings.sharePageByDefault !== false;
  includeTranscript.checked = settings.includeTranscriptByDefault !== false;
  customThemeDrafts = Array.isArray(themeSettings.customThemes)
    ? themeSettings.customThemes.map((theme, index) => createCustomThemeDraft(theme, index))
    : [];
  populateThemeOptions({ selectedThemeId: themeSettings.themeName });
  currentThemeAccent = themeSettings.customThemeAccent;
  showQuickPrompts.checked = settings.showQuickPrompts === true;
  showChallengeMode.checked = settings.showChallengeMode === true;
  challengeModeLabel.value = settings.challengeModeLabel || "";
  challengeModePrompt.value = settings.challengeModePrompt || "";
  if (wikiBaseUrlInput) {
    wikiBaseUrlInput.value = settings.wikiBaseUrl || "";
  }
  if (sidecarActivityLogLevelSelect) {
    const level = String(settings.sidecarActivityLogLevel || "normal").toLowerCase();
    sidecarActivityLogLevelSelect.value =
      level === "minimal" || level === "verbose" ? level : "normal";
  }
  if (activityLogPanelOpenCheckbox) {
    activityLogPanelOpenCheckbox.checked = settings.activityLogPanelOpen === true;
  }
  quickPromptDrafts = Array.isArray(settings.quickPrompts)
    ? settings.quickPrompts.map((prompt) => createPromptDraft(prompt))
    : [];
  renderQuickPromptList();
  renderCustomThemeList();
  applyThemePreview(themeSelect.value);
}

async function checkBridge() {
  const response = await sendRuntimeMessage({ type: "hermes:check-bridge-health" });
  const result = response.result || {};
  if (result.ok) {
    return `Bridge is reachable on port ${result.port}.`;
  }
  return "Bridge health check returned an unexpected response.";
}

async function requestMicrophoneAccess() {
  if (!navigator.mediaDevices?.getUserMedia) {
    throw new Error("This browser does not support microphone capture from extension pages.");
  }
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  for (const track of stream.getTracks()) {
    track.stop();
  }
}

async function loadAudioInputDevices(selectedDeviceId = "") {
  if (!audioInputDeviceSelect || !navigator.mediaDevices?.enumerateDevices) {
    return;
  }
  const devices = await navigator.mediaDevices.enumerateDevices();
  const audioInputs = devices.filter((device) => device.kind === "audioinput");
  audioInputDeviceSelect.textContent = "";

  const defaultOption = document.createElement("option");
  defaultOption.value = "";
  defaultOption.textContent = "System default microphone";
  audioInputDeviceSelect.appendChild(defaultOption);

  audioInputs.forEach((device, index) => {
    const option = document.createElement("option");
    option.value = device.deviceId || "";
    option.textContent = device.label || `Microphone ${index + 1}`;
    audioInputDeviceSelect.appendChild(option);
  });

  const hasSelectedValue =
    selectedDeviceId &&
    Array.from(audioInputDeviceSelect.options).some((option) => option.value === selectedDeviceId);
  audioInputDeviceSelect.value = hasSelectedValue ? selectedDeviceId : "";
}

function collectQuickPromptPayload() {
  const prompts = [];
  let skippedCount = 0;

  for (const draft of quickPromptDrafts) {
    const label = String(draft.label || "").trim();
    const template = String(draft.template || "").trim();
    if (!label && !template) {
      skippedCount += 1;
      continue;
    }
    if (!label || !template) {
      skippedCount += 1;
      continue;
    }
    prompts.push({
      id: String(draft.id || "").trim() || crypto.randomUUID(),
      label,
      template,
      includeTranscript: Boolean(draft.includeTranscript)
    });
  }

  return { prompts, skippedCount };
}

function buildSettingsPayload() {
  const { prompts, skippedCount: skippedPromptCount } = collectQuickPromptPayload();
  const normalizedCustomThemes = window.HermesTheme?.normalizeCustomThemes?.(customThemeDrafts) ||
    customThemeDrafts.map((theme, index) => createCustomThemeDraft(theme, index));
  const themeSettings = window.HermesTheme?.normalizeThemeSettings({
    themeName: themeSelect.value,
    customThemeAccent: currentThemeAccent,
    customThemes: normalizedCustomThemes
  }) || {
    themeName: themeSelect.value || window.HermesTheme?.defaultThemeId || "obsidian",
    customThemeAccent: currentThemeAccent || "#8b5cf6",
    customThemes: normalizedCustomThemes
  };
  return {
    skippedPromptCount,
    settings: {
      bridgeUrl: bridgeUrlInput.value.trim(),
      bridgeToken: bridgeTokenInput.value.trim(),
      audioInputDeviceId: audioInputDeviceSelect?.value || "",
      includeTranscriptByDefault: includeTranscript.checked,
      sharePageByDefault: sharePageByDefault.checked,
      themeName: themeSettings.themeName,
      customThemeAccent: themeSettings.customThemeAccent,
      customThemes: themeSettings.customThemes,
      showQuickPrompts: showQuickPrompts.checked,
      showChallengeMode: showChallengeMode.checked,
      quickPrompts: prompts,
      challengeModeLabel: challengeModeLabel.value.trim(),
      challengeModePrompt: challengeModePrompt.value.trim(),
      wikiBaseUrl: wikiBaseUrlInput ? wikiBaseUrlInput.value.trim() : "",
      sidecarActivityLogLevel: sidecarActivityLogLevelSelect
        ? sidecarActivityLogLevelSelect.value
        : "normal",
      activityLogPanelOpen: Boolean(activityLogPanelOpenCheckbox?.checked)
    }
  };
}

function buildRuntimeConfigPayload() {
  const selectedProvider = String(runtimeProviderSelect?.value || "auto").trim() || "auto";
  const selectedModel = String(runtimeModelInput?.value || "").trim() || String(runtimeModelSelect?.value || "").trim();
  const configPatch = {
    model: {
      default: selectedModel,
      provider: selectedProvider,
      base_url: String(runtimeBaseUrlInput?.value || "").trim(),
      api_mode: String(runtimeApiModeSelect?.value || "").trim(),
    },
    tts: {
      provider: String(ttsProviderSelect?.value || "edge").trim() || "edge",
      edge: {
        voice: String(ttsEdgeVoiceInput?.value || "").trim(),
      },
      openai: {
        model: String(ttsOpenaiModelInput?.value || "").trim(),
        voice: String(ttsOpenaiVoiceInput?.value || "").trim(),
      },
      kokoro: {
        base_url: String(ttsKokoroBaseUrlInput?.value || "").trim(),
        voice: String(ttsKokoroVoiceInput?.value || "").trim(),
      },
    },
    stt: {
      enabled: Boolean(sttEnabledCheckbox?.checked),
      provider: String(sttProviderSelect?.value || "local").trim() || "local",
      local: {
        model: String(sttLocalModelInput?.value || "").trim(),
      },
      openai: {
        model: String(sttOpenaiModelInput?.value || "").trim(),
      },
    },
    terminal: {
      backend: String(terminalBackendSelect?.value || "local").trim() || "local",
      timeout: Math.max(1, Number.parseInt(String(terminalTimeoutInput?.value || "180"), 10) || 180),
      windows_shell: String(terminalWindowsShellSelect?.value || "auto").trim().toLowerCase() || "auto",
      cwd: String(terminalCwdInput?.value || ".").trim() || ".",
      persistent_shell: terminalPersistentShellCheckbox?.checked !== false,
      docker_mount_cwd_to_workspace: Boolean(terminalDockerMountCwdCheckbox?.checked),
    },
    web: {
      backend: String(webBackendSelect?.value || "").trim(),
      archive_fallback: {
        enabled: Boolean(archiveFallbackEnabledCheckbox?.checked),
        service: String(archiveServiceSelect?.value || "archive.today").trim() || "archive.today",
        fallback_to_original: archiveFallbackToOriginalCheckbox?.checked !== false,
        paywalled_domains: String(archivePaywalledDomainsInput?.value || "")
          .split(",")
          .map((item) => item.trim())
          .filter(Boolean),
      },
    },
    delegation: {
      provider: String(delegationProviderInput?.value || "").trim(),
      model: String(delegationModelInput?.value || "").trim(),
      base_url: String(delegationBaseUrlInput?.value || "").trim(),
    },
  };

  const envUpdates = {};
  const provider = getSelectedProviderInfo();
  const apiKeyValue = String(providerApiKeyInput?.value || "").trim();
  if (provider && apiKeyValue && Array.isArray(provider.api_key_env_vars) && provider.api_key_env_vars.length) {
    envUpdates[provider.api_key_env_vars[0]] = apiKeyValue;
  }
  const envBaseUrlValue = String(providerEnvBaseUrlInput?.value || "").trim();
  const originalEnvBaseUrlValue = String(providerEnvBaseUrlInput?.dataset.originalValue || "").trim();
  if (provider && String(provider.base_url_env_var || "").trim()) {
    if (envBaseUrlValue !== originalEnvBaseUrlValue) {
      envUpdates[provider.base_url_env_var] = envBaseUrlValue;
    }
  }

  return {
    selectedProvider,
    configPatch,
    envUpdates
  };
}

async function saveSidecarToolVisibility() {
  await sendRuntimeMessage({
    type: "hermes:save-settings",
    settings: {
      showQuickPrompts: showQuickPrompts.checked,
      showChallengeMode: showChallengeMode.checked
    }
  });
  setStatus("Sidecar tool visibility saved.");
}

async function saveSettings({
  checkBridgeAfterSave = true,
  savedPrefix = "Settings saved."
} = {}) {
  const { settings, skippedPromptCount } = buildSettingsPayload();
  const runtimePayload = buildRuntimeConfigPayload();
  await sendRuntimeMessage({
    type: "hermes:save-settings",
    settings
  });
  await sendRuntimeMessage({
    type: "hermes:save-runtime-config",
    selectedProvider: runtimePayload.selectedProvider,
    configPatch: runtimePayload.configPatch,
    envUpdates: runtimePayload.envUpdates
  });

  await loadSettings();
  await loadRuntimeConfig(runtimePayload.selectedProvider);
  if (providerApiKeyInput) {
    providerApiKeyInput.value = "";
  }

  const skippedMessages = [];
  if (skippedPromptCount) {
    skippedMessages.push(
      `Skipped ${skippedPromptCount} incomplete quick prompt${skippedPromptCount === 1 ? "" : "s"}.`
    );
  }
  const skippedMessage = skippedMessages.length ? ` ${skippedMessages.join(" ")}` : "";

  if (!checkBridgeAfterSave) {
    setStatus(`${savedPrefix}${skippedMessage}`);
    return;
  }

  setStatus(`${savedPrefix}${skippedMessage} Checking the local bridge...`);
  setBridgeStatus("Checking bridge...");
  try {
    const bridgeMessage = await checkBridge();
    setBridgeStatus(bridgeMessage);
    setStatus(`${savedPrefix}${skippedMessage} ${bridgeMessage}`);
  } catch (error) {
    setBridgeStatus(`Bridge check failed: ${error.message || String(error)}`);
    setStatus(`${savedPrefix}${skippedMessage} Bridge check failed: ${error.message || String(error)}`);
  }
}

document.getElementById("health-button").addEventListener("click", () => {
  setBridgeStatus("Checking bridge...");
  checkBridge()
    .then((message) => setBridgeStatus(message))
    .catch((error) => setBridgeStatus(error.message || String(error)));
});

if (enableMicrophoneButton) {
  enableMicrophoneButton.addEventListener("click", () => {
    setMicrophoneStatus("Requesting microphone access...");
    requestMicrophoneAccess()
      .then(() => {
        return loadAudioInputDevices(audioInputDeviceSelect?.value || "").then(() => {
          setMicrophoneStatus("Microphone enabled for Hermes voice input.");
          setStatus("Microphone access granted.");
        });
      })
      .catch((error) => {
        const message = error.message || String(error);
        setMicrophoneStatus(message);
        setStatus(message);
      });
  });
}

if (audioInputDeviceSelect) {
  audioInputDeviceSelect.addEventListener("change", () => {
    saveSettings({
      checkBridgeAfterSave: false,
      savedPrefix: "Audio input device saved."
    }).catch((error) => setStatus(error.message || String(error)));
  });
}

addThemeButton.addEventListener("click", () => {
  const nextTheme = createCustomThemeDraft({}, customThemeDrafts.length);
  customThemeDrafts.push(nextTheme);
  populateThemeOptions({ selectedThemeId: nextTheme.id });
  renderCustomThemeList();
  applyThemePreview(nextTheme.id);
});

addQuickPromptButton.addEventListener("click", () => {
  quickPromptDrafts.push(createPromptDraft({ label: "", template: "", includeTranscript: false }));
  renderQuickPromptList();
});

themeSelect.addEventListener("input", () => {
  applyThemePreview(themeSelect.value);
});

themeSelect.addEventListener("change", () => {
  saveSettings({
    checkBridgeAfterSave: false,
    savedPrefix: "Theme saved."
  }).catch((error) => setStatus(error.message || String(error)));
});

showQuickPrompts.addEventListener("change", () => {
  saveSidecarToolVisibility().catch((error) => setStatus(error.message || String(error)));
});

showChallengeMode.addEventListener("change", () => {
  saveSidecarToolVisibility().catch((error) => setStatus(error.message || String(error)));
});

document.getElementById("save-settings-button").addEventListener("click", () => {
  saveSettings().catch((error) => setStatus(error.message || String(error)));
});

const DEFAULT_WIKI_FALLBACK = "http://127.0.0.1:8000/knowledge_wiki.html";

function normalizeSidecarOpenUrl(raw, fallback) {
  const trimmed = String(raw || "").trim();
  if (trimmed) {
    try {
      return new URL(trimmed).toString();
    } catch (_error) {
      return fallback;
    }
  }
  return fallback;
}

if (openWikiButton) {
  openWikiButton.addEventListener("click", () => {
    setLocalServicesFeedback("");
    const candidate = wikiBaseUrlInput?.value || "";
    const url = normalizeSidecarOpenUrl(candidate, DEFAULT_WIKI_FALLBACK);
    chrome.tabs.create({ url }, () => {
      const err = chrome.runtime.lastError;
      if (err) {
        setLocalServicesFeedback(err.message || "Could not open tab.", true);
        setStatus(err.message || "Could not open tab.");
      }
    });
  });
}

if (openControlRoomButton) {
  openControlRoomButton.addEventListener("click", () => {
    setLocalServicesFeedback("Opening control room in a new tab…");
    openControlRoomTab();
  });
}

if (runtimeProviderSelect) {
  runtimeProviderSelect.addEventListener("change", () => {
    const providerId = String(runtimeProviderSelect.value || "").trim();
    loadRuntimeProviderModels(providerId, "")
      .then(() => {
        updateProviderAuthUi();
        const placeholder = runtimeModelSelect?.value || "";
        if (runtimeModelInput && !runtimeModelInput.value.trim() && placeholder) {
          runtimeModelInput.value = placeholder;
        }
      })
      .catch((error) => setStatus(error.message || String(error)));
  });
}

if (runtimeModelSelect) {
  runtimeModelSelect.addEventListener("change", () => {
    if (runtimeModelInput && !runtimeModelInput.value.trim()) {
      runtimeModelInput.value = String(runtimeModelSelect.value || "").trim();
    }
  });
}

if (sidecarActivityLogLevelSelect) {
  sidecarActivityLogLevelSelect.addEventListener("change", () => {
    saveSettings({
      checkBridgeAfterSave: false,
      savedPrefix: "Activity log level saved."
    }).catch((error) => setStatus(error.message || String(error)));
  });
}

if (activityLogPanelOpenCheckbox) {
  activityLogPanelOpenCheckbox.addEventListener("change", () => {
    saveSettings({
      checkBridgeAfterSave: false,
      savedPrefix: "Activity log panel default saved."
    }).catch((error) => setStatus(error.message || String(error)));
  });
}

window.addEventListener("error", (event) => {
  if (!isExtensionContextInvalidated(event?.error || event?.message)) {
    return;
  }
  const message = explainExtensionError(event.error || event.message);
  setStatus(message);
  setBridgeStatus(message);
  event.preventDefault();
});

window.addEventListener("unhandledrejection", (event) => {
  if (!isExtensionContextInvalidated(event?.reason)) {
    return;
  }
  const message = explainExtensionError(event.reason);
  setStatus(message);
  setBridgeStatus(message);
  event.preventDefault();
});

(async () => {
  try {
    await loadSettings();
    await loadRuntimeConfig();
  } catch (error) {
    const message = error.message || String(error);
    setStatus(message);
    setBridgeStatus(message);
  }
})();
