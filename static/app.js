const get = id => document.getElementById(id);
const ui = Object.fromEntries(["clear", "composer", "messages", "model", "prompt", "send", "status", "stop"].map(id => [id, get(id)]));
const MODEL_KEY = "local-llm-model";
let messages = [], request;

function busy(value) {
  ui.send.classList.toggle("hidden", value);
  ui.stop.classList.toggle("hidden", !value);
  [ui.model, ui.clear, ui.prompt].forEach(element => element.disabled = value);
}

function add(role, content = "") {
  get("messages").querySelector(".empty")?.remove();
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
    ui.send.disabled = false;
  } catch (error) {
    ui.status.textContent = error.message;
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
  busy(true);
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
    busy(false);
    ui.prompt.focus();
  }
}

ui.composer.addEventListener("submit", event => {
  event.preventDefault();
  const text = ui.prompt.value.trim();
  if (!text || request || !ui.model.value) return;
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
ui.prompt.addEventListener("input", () => {
  ui.prompt.style.height = "auto";
  ui.prompt.style.height = `${ui.prompt.scrollHeight}px`;
});
ui.stop.addEventListener("click", () => request?.abort());
ui.model.addEventListener("change", () => localStorage.setItem(MODEL_KEY, ui.model.value));
ui.clear.addEventListener("click", () => {
  messages = [];
  ui.messages.innerHTML = '<div class="empty"><h2>New conversation</h2><p>The previous context has been cleared.</p></div>';
  ui.prompt.focus();
});
ui.send.disabled = true;
loadModels();
