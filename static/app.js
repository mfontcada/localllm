const get = id => document.getElementById(id);
const ui = Object.fromEntries([
  "clear", "composer", "messages", "mic", "model", "prompt", "send", "status", "stop", "voice-status",
].map(id => [id.replace("-", "_"), get(id)]));
const MODEL_KEY = "local-llm-model";
const MAX_VOICE_MS = 120_000;
let messages = [], request, voice;
let modelsReady = false, voiceAvailable = false;

function resizePrompt() {
  ui.prompt.style.height = "auto";
  ui.prompt.style.height = `${ui.prompt.scrollHeight}px`;
}

function updateControls() {
  const chatting = Boolean(request), speaking = Boolean(voice);
  ui.send.classList.toggle("hidden", chatting);
  ui.stop.classList.toggle("hidden", !chatting);
  ui.send.disabled = !modelsReady || chatting || speaking;
  ui.model.disabled = chatting || speaking;
  ui.clear.disabled = chatting || speaking;
  ui.prompt.disabled = chatting;
  ui.prompt.readOnly = speaking;
  ui.mic.disabled = !voiceAvailable || chatting;
}

function setVoiceStatus(text, state = "") {
  ui.voice_status.textContent = text;
  ui.voice_status.dataset.state = state;
}

function add(role, content = "") {
  ui.messages.querySelector(".empty")?.remove();
  const article = document.createElement("article");
  const label = document.createElement("div");
  const body = document.createElement("div");
  article.className = `message ${role}`;
  label.className = "role";
  label.textContent = { user: "You", assistant: "Assistant" }[role] || "Error";
  body.textContent = content;
  article.append(label, body);
  ui.messages.append(article);
  ui.messages.scrollTop = ui.messages.scrollHeight;
  return body;
}

async function loadModels() {
  try {
    const response = await fetch("/api/models");
    const data = await response.json();
    if (!response.ok || !data.models.length) throw Error(data.error || "No Ollama models are installed");
    data.models.forEach(name => ui.model.add(new Option(name, name)));
    const saved = localStorage.getItem(MODEL_KEY);
    if (data.models.includes(saved)) ui.model.value = saved;
    ui.status.textContent = `${data.models.length} local model${data.models.length === 1 ? "" : "s"} available`;
    modelsReady = true;
  } catch (error) {
    ui.status.textContent = error.message;
  } finally {
    updateControls();
  }
}

async function loadVoice() {
  const supported = window.isSecureContext && navigator.mediaDevices?.getUserMedia &&
    window.AudioContext && window.AudioWorkletNode && window.WebSocket;
  if (!supported) {
    setVoiceStatus("");
    return;
  }
  try {
    const response = await fetch("/api/voice/status");
    const data = await response.json();
    voiceAvailable = response.ok && data.available;
    setVoiceStatus("");
  } catch {
    setVoiceStatus("");
  } finally {
    updateControls();
  }
}

function consume(line, state) {
  if (!line.trim()) return;
  const event = JSON.parse(line);
  if (event.error) throw Error(event.error);
  state.text += event.message?.content || "";
  state.body.textContent = state.text;
  ui.messages.scrollTop = ui.messages.scrollHeight;
}

async function chat(text) {
  messages.push({ role: "user", content: text });
  add("user", text);
  const state = { body: add("assistant"), text: "" };
  request = new AbortController();
  updateControls();
  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: ui.model.value, messages }),
      signal: request.signal,
    });
    if (!response.ok) {
      const data = await response.json();
      throw Error(data.error || `Chat failed with HTTP ${response.status}`);
    }
    const reader = response.body.getReader(), decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";
      lines.forEach(line => consume(line, state));
      if (done) break;
    }
    consume(buffer, state);
    if (state.text) messages.push({ role: "assistant", content: state.text });
  } catch (error) {
    if (error.name === "AbortError") {
      state.body.textContent = state.text || "Stopped.";
      if (state.text) messages.push({ role: "assistant", content: state.text });
    } else {
      state.body.parentElement.classList.add("error");
      state.body.textContent = error.message;
      messages.pop();
    }
  } finally {
    request = null;
    updateControls();
    ui.prompt.focus();
  }
}

function joinedVoiceText(state) {
  return [...state.finals, state.partial].filter(Boolean).join(" ").trim();
}

function withDraft(draft, transcript) {
  if (!draft || !transcript) return draft || transcript;
  return `${draft}${/\s$/.test(draft) ? "" : " "}${transcript}`;
}

function renderVoice(state) {
  ui.prompt.value = withDraft(state.draft, joinedVoiceText(state));
  resizePrompt();
}

async function releaseVoice(state, error = "") {
  if (voice !== state) return;
  voice = null;
  clearTimeout(state.limitTimer);
  clearTimeout(state.finishTimer);
  state.node?.disconnect();
  state.source?.disconnect();
  state.stream?.getTracks().forEach(track => track.stop());
  if (state.context && state.context.state !== "closed") await state.context.close();
  if (state.socket?.readyState === WebSocket.OPEN) state.socket.close();
  ui.mic.classList.remove("recording");
  ui.mic.textContent = "Mic";
  ui.mic.setAttribute("aria-label", "Start voice input");
  ui.mic.setAttribute("aria-pressed", "false");
  setVoiceStatus(error, error ? "error" : "");
  updateControls();
  ui.prompt.focus();
}

function voiceError(event) {
  return event?.error?.message || "Voice transcription failed";
}

function handleVoiceEvent(state, event) {
  if (voice !== state) return;
  if (event.type === "conversation.item.input_audio_transcription.delta") {
    state.partial += event.delta || "";
    renderVoice(state);
  } else if (event.type === "conversation.item.input_audio_transcription.completed") {
    const transcript = (event.transcript || state.partial).trim();
    if (transcript) state.finals.push(transcript);
    state.partial = "";
    renderVoice(state);
    if (state.stopping) releaseVoice(state);
  } else if (event.type === "error") {
    releaseVoice(state, voiceError(event));
  }
}

async function startVoice() {
  const state = {
    draft: ui.prompt.value,
    finals: [],
    partial: "",
    stopping: false,
  };
  voice = state;
  updateControls();
  setVoiceStatus("Requesting microphone…", "active");
  try {
    state.stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    });
    if (voice !== state || state.stopping) {
      state.stream.getTracks().forEach(track => track.stop());
      return;
    }
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    state.socket = new WebSocket(`${scheme}://${location.host}/api/voice/realtime`);
    await new Promise((resolve, reject) => {
      state.socket.addEventListener("open", resolve, { once: true });
      state.socket.addEventListener("error", () => reject(Error("Cannot connect to local voice service")), { once: true });
    });
    if (voice !== state || state.stopping) {
      state.socket.close();
      state.stream.getTracks().forEach(track => track.stop());
      return;
    }
    state.socket.addEventListener("message", message => {
      try {
        handleVoiceEvent(state, JSON.parse(message.data));
      } catch {
        releaseVoice(state, "Local voice service returned an invalid response");
      }
    });
    state.socket.addEventListener("close", () => {
      if (voice === state) releaseVoice(state, "Voice connection closed before transcription finished");
    });
    state.socket.send(JSON.stringify({
      type: "session.update",
      session: { sample_rate: 16000, language: "auto", automatic_punctuation: true },
    }));

    state.context = new AudioContext({ sampleRate: 16000 });
    await state.context.audioWorklet.addModule("/pcm-worklet.js");
    state.source = state.context.createMediaStreamSource(state.stream);
    state.node = new AudioWorkletNode(state.context, "pcm-capture");
    const silent = state.context.createGain();
    silent.gain.value = 0;
    state.node.port.onmessage = message => {
      if (state.socket.readyState === WebSocket.OPEN && !state.stopping) {
        state.socket.send(message.data);
      }
    };
    state.source.connect(state.node).connect(silent).connect(state.context.destination);
    state.limitTimer = setTimeout(() => stopVoice(), MAX_VOICE_MS);
    ui.mic.classList.add("recording");
    ui.mic.textContent = "Stop";
    ui.mic.setAttribute("aria-label", "Stop voice input");
    ui.mic.setAttribute("aria-pressed", "true");
    setVoiceStatus("Listening…", "active");
  } catch (error) {
    await releaseVoice(state, error.message || "Voice input could not start");
  }
}

function stopVoice() {
  const state = voice;
  if (!state || state.stopping) return;
  state.stopping = true;
  clearTimeout(state.limitTimer);
  state.node?.disconnect();
  state.stream?.getTracks().forEach(track => track.stop());
  setVoiceStatus("Finishing transcription…", "active");
  if (state.socket?.readyState === WebSocket.OPEN) {
    state.socket.send(JSON.stringify({ type: "input_audio_buffer.commit" }));
    state.finishTimer = setTimeout(() => releaseVoice(state), 5000);
  } else {
    releaseVoice(state, "Voice connection closed before transcription finished");
  }
}

ui.composer.addEventListener("submit", event => {
  event.preventDefault();
  const text = ui.prompt.value.trim();
  if (!text || request || voice || !ui.model.value) return;
  ui.prompt.value = "";
  ui.prompt.style.height = "auto";
  chat(text);
});
ui.prompt.addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    ui.composer.requestSubmit();
  }
});
ui.prompt.addEventListener("input", resizePrompt);
ui.stop.addEventListener("click", () => request?.abort());
ui.mic.addEventListener("click", () => voice ? stopVoice() : startVoice());
ui.model.addEventListener("change", () => localStorage.setItem(MODEL_KEY, ui.model.value));
ui.clear.addEventListener("click", () => {
  messages = [];
  ui.messages.innerHTML = '<div class="empty"><h2>New conversation</h2><p>The previous context has been cleared.</p></div>';
  ui.prompt.focus();
});
updateControls();
loadModels();
loadVoice();
