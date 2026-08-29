# Full stack setup

This guide provisions the complete local stack on Linux with an NVIDIA GPU:

```text
Browser ──HTTP/HTTPS──> Local LLM Chat :3000
                              ├──> Ollama :11434
                              └──> NeMo-Speech.cpp :8080
```

Only Local LLM Chat should be exposed to the browser. Ollama and NeMo-Speech
remain bound to loopback. The primary setup uses CUDA; a CPU speech fallback is
included for systems where GPU memory is more valuable than transcription
latency.

## 1. Prerequisites

The setup requires a Linux system with `systemd`, Git, curl, and a working
NVIDIA driver. It does not require a CUDA development toolkit when the
prebuilt NeMo-Speech CUDA package is available.

Verify the basics:

```bash
nvidia-smi
git --version
curl --version
```

Do not continue with the CUDA path until `nvidia-smi` lists the GPU without an
error. Follow the [Ollama Linux documentation](https://docs.ollama.com/linux)
and [NeMo-Speech installation guide](https://github.com/NVIDIA/NeMo-Speech.cpp/blob/main/docs/install.md)
for driver and platform-specific details.

### GPU and model sizing

The recommended chat model for a 16 GB GPU is `gemma4:12b`. Its default Ollama
artifact is approximately 7.6 GB, and Google positions it between the
edge-oriented E4B and the larger 26B workstation model. E4B remains useful when
lower latency matters more than model quality.

| GPU memory | Suggested starting model | Notes |
| --- | --- | --- |
| 8–12 GB | `gemma4:e2b` or `gemma4:e4b` | Prefer E2B when memory is tight and E4B for more quality. |
| 16 GB | `gemma4:12b` | Recommended baseline with a 16K context and CUDA ASR. |
| 24 GB | `gemma4:26b` | Run ASR on CPU if model or context memory becomes tight. |
| 32 GB or more | `gemma4:26b`; evaluate `gemma4:31b` | Leave headroom for ASR and context. |

Model artifact size is not total runtime memory. Context length and concurrent
requests also consume memory. Use `ollama ps` and `nvidia-smi` to confirm the
real allocation. Any text-chat model listed by `ollama list` can be used by the
app; Gemma 4 is a documented baseline, not a requirement. See the
[official Gemma 4 model page](https://ollama.com/library/gemma4) for available
quantizations and sizes and Google's
[Gemma 4 12B introduction](https://blog.google/innovation-and-ai/technology/developers-tools/introducing-gemma-4-12b/)
for its intended hardware tier.

## 2. Install Ollama and a chat model

Install Ollama using its official Linux installer:

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

The script can be inspected before execution:

```bash
curl -fsSL https://ollama.com/install.sh | less
```

This intentionally installs the current stable Ollama release instead of
pinning one version. Rerun the installer to upgrade, and use the version output
when reporting compatibility problems:

```bash
ollama --version
```

Enable and start the service:

```bash
sudo systemctl enable --now ollama
sudo systemctl status ollama
```

Ollama defaults to a 4K context on GPUs with less than 24 GiB of memory. Set a
16K starting context for this stack without opening an interactive editor:

```bash
printf '%s\n' \
  '[Service]' \
  'Environment="OLLAMA_CONTEXT_LENGTH=16384"' \
  | sudo systemctl edit --stdin ollama
```

If the installed systemd does not support `--stdin`, run
`sudo SYSTEMD_EDITOR=EDITOR systemctl edit ollama`, replace `EDITOR` with an
installed editor such as `vi` or `nvim`, and enter the same two lines.

Apply it:

```bash
sudo systemctl daemon-reload
sudo systemctl restart ollama
```

Larger contexts consume more memory. If `ollama ps` later reports CPU
offloading or the stack runs out of GPU memory, change the value to `8192` and
restart Ollama. When NeMo runs on CPU, higher values can be tested gradually.

Pull the recommended model:

```bash
ollama pull gemma4:12b
ollama list
```

If suitable models are already installed, skip the pull and keep using them.
Verify the local API:

```bash
curl http://127.0.0.1:11434/api/tags
```

The response must contain a `models` array. Run one small prompt before moving
on:

```bash
ollama run gemma4:12b "Reply with: Ollama is ready."
ollama ps
```

`ollama ps` reports the allocated context, VRAM use, and whether the model is
fully on the GPU.

## 3. Install NeMo-Speech.cpp

The application targets the realtime protocol in NeMo-Speech.cpp `v0.1.0`.
Install that pinned, prebuilt CUDA release so the wire protocol does not change
unexpectedly:

```bash
installer_dir="$(mktemp -d)"

git clone --depth 1 --branch v0.1.0 \
  https://github.com/NVIDIA/NeMo-Speech.cpp.git \
  "$installer_dir/NeMo-Speech.cpp"

cd "$installer_dir/NeMo-Speech.cpp"

./scripts/install.sh \
  --version 0.1.0 \
  --backend cuda \
  --binary-only
```

The installer verifies the release checksum and installs into the current user
account without `sudo`. Refresh the current shell and verify the runtime:

```bash
export PATH="$HOME/.local/bin:$PATH"
hash -r
nemo-speech --version
nemo-speech doctor
```

If `nemo-speech` works only after the `export`, open a new terminal or add
`$HOME/.local/bin` to the shell's normal `PATH` configuration.

Download the multilingual streaming ASR model once:

```bash
nemo-speech pull nemotron-3.5
nemo-speech model list
```

Nemotron 3.5 supports realtime transcription, automatic language detection,
and punctuation. Model files stay in NeMo-Speech's user cache. See the
[Nemotron 3.5 model card](https://huggingface.co/nvidia/nemotron-3.5-asr-streaming-0.6b)
and [NeMo-Speech server API](https://github.com/NVIDIA/NeMo-Speech.cpp/blob/main/docs/server.md).

## 4. Start and verify speech recognition

Start the ASR-only server and keep it running:

```bash
nemo-speech serve \
  --asr-model nemotron-3.5 \
  --gpu 0 \
  --host 127.0.0.1 \
  --port 8080 \
  --no-ui
```

In another terminal, verify readiness:

```bash
nemo-speech health --url http://127.0.0.1:8080/ready
curl http://127.0.0.1:8080/ready
```

The endpoint must return HTTP 200 with `"ready": true`. Leave NeMo bound to
`127.0.0.1`; the application provides the same-origin WebSocket proxy used by
the browser.

### CPU fallback

To preserve GPU memory for a larger Ollama model, use the same CUDA-capable
installation but run ASR on CPU:

```bash
nemo-speech serve \
  --asr-model nemotron-3.5 \
  --gpu -1 \
  --host 127.0.0.1 \
  --port 8080 \
  --no-ui
```

This reduces GPU use at the cost of transcription speed.

### Optional streaming-context tuning

The upstream `asr.streaming.rnnt_right_context` default is `1`. Keep that
default for initial setup. Setting it to `3` uses a 320 ms chunk and can trade
additional latency for accuracy, but should be enabled only after testing the
languages and microphones used by the deployment.

## 5. Install and run Local LLM Chat

Install `uv` if it is not already available:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

The installer can be inspected first with:

```bash
curl -LsSf https://astral.sh/uv/install.sh | less
```

Open a new shell if `uv` is not immediately on `PATH`. Then clone and prepare
the app:

```bash
git clone https://github.com/mfontcada/localllm.git
cd localllm
uv sync --frozen
```

`uv` will provide a compatible Python when necessary and install the locked
`aiohttp` runtime dependency from `uv.lock`. Start the application:

```bash
uv run --frozen python app.py \
  --ollama-url http://127.0.0.1:11434 \
  --speech-url http://127.0.0.1:8080
```

Verify both upstream integrations:

```bash
curl http://127.0.0.1:3000/api/models
curl http://127.0.0.1:3000/api/voice/status
```

The first response should list the installed Ollama models. The second should
contain `"available": true` when NeMo is ready.

Open <http://127.0.0.1:3000>, choose a model, and send a text message. Select
Mic, grant browser microphone permission, speak, and select Stop. The final
transcript is placed in the existing prompt draft and is not submitted
automatically.

If the page was loaded before NeMo became ready, reload it. An unavailable
speech service intentionally leaves Mic disabled without displaying an error;
text chat remains usable.

## 6. Keep the stack running

Ollama is managed by the system service. Run NeMo and the app as systemd user
services so they restart after failures and their logs remain available in the
journal.

Create the user-service directory:

```bash
mkdir -p "$HOME/.config/systemd/user"
```

Create `~/.config/systemd/user/nemo-speech.service` with the same GPU or CPU
option selected in step 4 (`--gpu -1` is the CPU fallback):

```ini
[Unit]
Description=NeMo Speech server
After=network-online.target

[Service]
ExecStart=%h/.local/bin/nemo-speech serve --asr-model nemotron-3.5 --gpu 0 --host 127.0.0.1 --port 8080 --no-ui
Environment="PATH=%h/.local/bin:/usr/local/bin:/usr/bin"
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

Create `~/.config/systemd/user/local-llm.service`, replacing
`/path/to/localllm` with the absolute path to this checkout:

```ini
[Unit]
Description=Local LLM Chat
After=network-online.target nemo-speech.service
Wants=nemo-speech.service

[Service]
WorkingDirectory=/path/to/localllm
ExecStart=%h/.local/bin/uv run --frozen python app.py --ollama-url http://127.0.0.1:11434 --speech-url http://127.0.0.1:8080
Environment="PATH=%h/.local/bin:/usr/local/bin:/usr/bin"
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

Enable and start both services:

```bash
systemctl --user daemon-reload
systemctl --user enable --now nemo-speech.service local-llm.service
```

`Wants=` starts NeMo when the app starts, but does not make speech a hard
requirement: text chat remains usable if NeMo is unavailable. The app starts
after the NeMo service has been launched; readiness can still take a few
seconds while the model loads.

Enable user lingering only if these services must start at boot without an
interactive login:

```bash
loginctl enable-linger "$USER"
```

Inspect, restart, or stop the services with:

```bash
systemctl --user status nemo-speech.service local-llm.service
journalctl --user -u nemo-speech.service
journalctl --user -u local-llm.service
systemctl --user restart nemo-speech.service local-llm.service
systemctl --user disable --now nemo-speech.service local-llm.service
```

## Private remote access

Microphone capture is allowed on localhost, but remote browser access requires
a secure HTTPS context. Tailscale Serve can expose only the application to
devices in the same tailnet:

```bash
sudo tailscale serve --bg http://127.0.0.1:3000
sudo tailscale serve status
```

Open the HTTPS URL reported by Tailscale and grant microphone permission there.
Ollama and NeMo remain on loopback and are reached through the app. Do not use
Tailscale Funnel: this application has no authentication and must not be made
public. Tailnet membership does not necessarily mean that only one user can
connect; restrict the destination and HTTPS port with
[Tailscale grants or ACLs](https://tailscale.com/docs/features/access-control/grants)
according to the users and devices that should have access.

## Troubleshooting

### Ollama models do not appear

```bash
sudo systemctl status ollama
journalctl -e -u ollama
curl http://127.0.0.1:11434/api/tags
ollama list
```

Confirm the app was not started with an incorrect `--ollama-url`.

### Mic remains disabled

Check each layer in order:

```bash
nemo-speech health --url http://127.0.0.1:8080/ready
curl http://127.0.0.1:8080/ready
curl http://127.0.0.1:3000/api/voice/status
```

Then reload the page. For remote access, confirm the browser URL uses HTTPS.
Review the site's browser permissions if microphone access was previously
denied.

### NeMo does not start

```bash
nemo-speech doctor
nemo-speech model list
nvidia-smi
```

Confirm that the installed runtime includes CUDA support, the model is present,
and port `8080` is not already occupied.

### GPU memory is exhausted

```bash
nvidia-smi
ollama ps
```

Use a smaller Ollama model, reduce its context length, stop unused Ollama
models with `ollama stop MODEL`, or run NeMo with `--gpu -1`. Avoid assuming
that a model's download size equals its complete runtime allocation.

### App dependencies or tests fail

From the repository checkout:

```bash
uv sync --frozen
uv run --frozen python -m unittest discover -s tests
```

The app requires Python 3.11 or newer and uses the dependency versions recorded
in `uv.lock`.
