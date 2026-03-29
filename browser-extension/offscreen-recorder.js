let recorder = null;
let recorderStream = null;
let recorderChunks = [];
const voiceChannel = typeof BroadcastChannel !== "undefined"
  ? new BroadcastChannel("hermes-sidecar-voice-input")
  : null;

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

async function sendStatus(event, extra = {}) {
  if (!voiceChannel) {
    return;
  }
  voiceChannel.postMessage({
    type: String(event || ""),
    ...extra
  });
}

async function startRecording(selectedDeviceId = "", captureMode = "raw") {
  if (recorder && recorder.state === "recording") {
    return { ok: true, alreadyRecording: true };
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

  recorder.addEventListener("stop", async () => {
    const mimeType = recorder?.mimeType || preferredMimeType || "audio/webm";
    const blob = new Blob(recorderChunks, { type: mimeType });
    recorder = null;
    recorderChunks = [];
    stopStream();

    if (!blob.size) {
      await sendStatus("error", { error: "No recorded audio was captured." });
      return;
    }

    await sendStatus("transcribing");
    try {
      const audioBase64 = await blobToBase64(blob);
      await chrome.runtime.sendMessage({
        type: "hermes:voice-recording-audio",
        audioBase64,
        mimeType
      });
    } catch (error) {
      await sendStatus("error", { error: String(error?.message || error) });
    }
  }, { once: true });

  recorder.addEventListener("error", async () => {
    recorder = null;
    recorderChunks = [];
    stopStream();
    await sendStatus("error", { error: "Voice recording failed." });
  }, { once: true });

  recorder.start();
  await sendStatus("recording");
  return { ok: true, recording: true };
}

async function stopRecording() {
  if (!recorder || recorder.state !== "recording") {
    return { ok: true, recording: false };
  }
  recorder.stop();
  return { ok: true, recording: false };
}

if (voiceChannel) {
  voiceChannel.addEventListener("message", (event) => {
    const payload = event?.data && typeof event.data === "object" ? event.data : {};
    const type = String(payload.type || "");
    if (type === "hermes:start-recording") {
      startRecording(payload.deviceId || "", payload.captureMode || "raw").catch(async (error) => {
        await sendStatus("error", { error: String(error?.message || error) });
      });
      return;
    }
    if (type === "hermes:stop-recording") {
      stopRecording().catch(async (error) => {
        await sendStatus("error", { error: String(error?.message || error) });
      });
    }
  });
}
