const recordButton = document.getElementById("record-button");
const cancelButton = document.getElementById("cancel-button");
const voiceStatus = document.getElementById("voice-status");
const voiceChannel = typeof BroadcastChannel !== "undefined"
  ? new BroadcastChannel("hermes-sidecar-voice-input")
  : null;

let recorder = null;
let recorderStream = null;
let recorderChunks = [];
let transcriptionPending = false;
const urlParams = new URLSearchParams(window.location.search);
const autoStartRequested = urlParams.get("autostart") === "1";
const defaultDeviceId = String(urlParams.get("deviceId") || "").trim();
const defaultCaptureMode = String(urlParams.get("captureMode") || "").trim().toLowerCase() === "speech" ? "speech" : "raw";

function publish(type, extra = {}) {
  if (!voiceChannel) {
    return;
  }
  voiceChannel.postMessage({ type, ...extra });
}

function setStatus(message) {
  voiceStatus.textContent = String(message || "");
}

function updateUi() {
  const recording = Boolean(recorder && recorder.state === "recording");
  recordButton.disabled = transcriptionPending;
  recordButton.classList.toggle("is-recording", recording);
  recordButton.textContent = transcriptionPending
    ? "Transcribing..."
    : recording
      ? "Stop recording"
      : "Start recording";
}

function stopStream() {
  if (!recorderStream) {
    return;
  }
  for (const track of recorderStream.getTracks()) {
    try {
      track.stop();
    } catch (_error) {
      // Ignore cleanup failures.
    }
  }
  recorderStream = null;
}

function blobToBase64(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error || new Error("Failed to read recorded audio."));
    reader.onloadend = () => {
      const result = String(reader.result || "");
      const marker = "base64,";
      const index = result.indexOf(marker);
      if (index === -1) {
        reject(new Error("Could not encode recorded audio."));
        return;
      }
      resolve(result.slice(index + marker.length));
    };
    reader.readAsDataURL(blob);
  });
}

async function transcribeBlob(blob) {
  if (!(blob instanceof Blob) || blob.size <= 0) {
    throw new Error("No recorded audio was captured.");
  }
  transcriptionPending = true;
  updateUi();
  setStatus("Uploading voice note to Hermes for transcription...");
  publish("voice-recorder:transcribing");

  try {
    const audioBase64 = await blobToBase64(blob);
    const response = await chrome.runtime.sendMessage({
      type: "hermes:transcribe-chat-audio",
      audioBase64,
      mimeType: blob.type || "audio/webm"
    });
    if (!response?.ok) {
      throw new Error(response?.error || "Audio transcription failed.");
    }
    const transcript = String(response.result?.transcript || "").trim();
    if (!transcript) {
      throw new Error("Hermes returned an empty transcript.");
    }
    publish("voice-recorder:transcript", { transcript });
    window.close();
  } finally {
    transcriptionPending = false;
    updateUi();
  }
}

async function startRecording(selectedDeviceId = defaultDeviceId, captureMode = defaultCaptureMode) {
  if (transcriptionPending) {
    return;
  }
  if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
    throw new Error("Voice recording is not supported in this browser.");
  }

  const normalizedDeviceId = String(selectedDeviceId || "").trim();
  const normalizedMode = String(captureMode || "").trim().toLowerCase() === "speech" ? "speech" : "raw";
  const audioConstraints = normalizedMode === "speech"
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
  if (normalizedDeviceId) {
    audioConstraints.deviceId = { exact: normalizedDeviceId };
  }

  recorderStream = await navigator.mediaDevices.getUserMedia({ audio: audioConstraints });
  const preferredMimeType = MediaRecorder.isTypeSupported?.("audio/webm;codecs=opus")
    ? "audio/webm;codecs=opus"
    : MediaRecorder.isTypeSupported?.("audio/webm")
      ? "audio/webm"
      : "";
  recorder = preferredMimeType
    ? new MediaRecorder(recorderStream, { mimeType: preferredMimeType })
    : new MediaRecorder(recorderStream);
  recorderChunks = [];

  recorder.addEventListener("dataavailable", (event) => {
    if (event.data && event.data.size > 0) {
      recorderChunks.push(event.data);
    }
  });

  recorder.addEventListener("stop", () => {
    const blob = new Blob(recorderChunks, {
      type: recorder?.mimeType || preferredMimeType || "audio/webm"
    });
    recorder = null;
    recorderChunks = [];
    stopStream();
    updateUi();
    transcribeBlob(blob).catch((error) => {
      publish("voice-recorder:error", { error: String(error?.message || error) });
      setStatus(String(error?.message || error));
    });
  }, { once: true });

  recorder.addEventListener("error", () => {
    recorder = null;
    recorderChunks = [];
    stopStream();
    updateUi();
    const message = "Voice recording failed.";
    publish("voice-recorder:error", { error: message });
    setStatus(message);
  }, { once: true });

  recorder.start();
  publish("recording");
  updateUi();
  setStatus("Recording... click Stop recording when you're done.");
}

function stopRecording() {
  if (!recorder || recorder.state !== "recording") {
    return;
  }
  setStatus("Stopping recording...");
  recorder.stop();
}

async function toggleRecording() {
  if (recorder && recorder.state === "recording") {
    stopRecording();
    return;
  }
  await startRecording(defaultDeviceId, defaultCaptureMode);
}

recordButton.addEventListener("click", () => {
  toggleRecording().catch((error) => {
    publish("voice-recorder:error", { error: String(error?.message || error) });
    setStatus(String(error?.message || error));
    stopStream();
    recorder = null;
    recorderChunks = [];
    transcriptionPending = false;
    updateUi();
  });
});

cancelButton.addEventListener("click", () => {
  window.close();
});

if (voiceChannel) {
  voiceChannel.addEventListener("message", (event) => {
    const payload = event?.data && typeof event.data === "object" ? event.data : {};
    const type = String(payload.type || "");
    if (type === "hermes:start-recording") {
      startRecording(payload.deviceId || defaultDeviceId, payload.captureMode || defaultCaptureMode).catch((error) => {
        publish("voice-recorder:error", { error: String(error?.message || error) });
        setStatus(String(error?.message || error));
      });
      return;
    }
    if (type === "hermes:stop-recording") {
      stopRecording();
    }
  });
}

window.addEventListener("beforeunload", () => {
  stopStream();
  publish("voice-recorder:closed");
});

publish("voice-recorder:ready");
updateUi();

if (autoStartRequested) {
  setStatus("Opening microphone prompt...");
  startRecording(defaultDeviceId, defaultCaptureMode).catch((error) => {
    publish("voice-recorder:error", { error: String(error?.message || error) });
    setStatus(String(error?.message || error));
    stopStream();
    recorder = null;
    recorderChunks = [];
    transcriptionPending = false;
    updateUi();
  });
}
