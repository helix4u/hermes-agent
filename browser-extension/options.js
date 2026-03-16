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
const statusText = document.getElementById("status-text");

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

window.HermesTheme?.applyThemeToDocument({
  themeName: window.HermesTheme?.defaultThemeId || "obsidian"
});

function setStatus(message) {
  statusText.textContent = message;
}

function setBridgeStatus(message) {
  bridgeStatusText.textContent = message;
}

function setMicrophoneStatus(message) {
  if (microphoneStatusText) {
    microphoneStatusText.textContent = message;
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
      challengeModePrompt: challengeModePrompt.value.trim()
    }
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
  await sendRuntimeMessage({
    type: "hermes:save-settings",
    settings
  });

  await loadSettings();

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
  } catch (error) {
    const message = error.message || String(error);
    setStatus(message);
    setBridgeStatus(message);
  }
})();
