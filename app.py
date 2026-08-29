#!/usr/bin/env python3
"""Small, local-only chat interface for Ollama and NeMo-Speech."""

import argparse
import asyncio
import json
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import ClientError, ClientSession, ClientTimeout, WSMsgType, web

ROOT = Path(__file__).parent.resolve()
STATIC = ROOT / "static"
MAX_BODY = 2 * 1024 * 1024
MAX_VOICE_SECONDS = 120
VOICE_SAMPLE_RATE = 16_000
MAX_VOICE_BYTES = MAX_VOICE_SECONDS * VOICE_SAMPLE_RATE * 2
ROLES = {"system", "user", "assistant", "tool"}
NO_STORE = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
VOICE_EVENTS = {
    "session.update",
    "input_audio_buffer.commit",
    "input_audio_buffer.clear",
    "response.cancel",
}
CLIENT_KEY = web.AppKey("client", ClientSession)
OLLAMA_URL_KEY = web.AppKey("ollama_url", str)
SPEECH_URL_KEY = web.AppKey("speech_url", str)


def service_url(value, name):
    parsed = urlparse(value)
    if parsed.scheme != "http" or not parsed.hostname:
        raise ValueError(f"{name} must be an http URL")
    return value.rstrip("/")


def websocket_url(base, path):
    parsed = urlparse(base)
    prefix = parsed.path.rstrip("/")
    return parsed._replace(scheme="ws", path=prefix + path).geturl()


def validate(payload):
    if not isinstance(payload, dict):
        raise ValueError("Request must be a JSON object")
    model, messages = payload.get("model"), payload.get("messages")
    if not isinstance(model, str) or not model.strip() or len(model) > 200:
        raise ValueError("A valid model is required")
    if not isinstance(messages, list) or not 0 < len(messages) <= 200:
        raise ValueError("At least one message is required")
    clean = []
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("Every message must be an object")
        role, content = message.get("role"), message.get("content")
        if role not in ROLES:
            raise ValueError("A message has an invalid role")
        if not isinstance(content, str) or len(content) > 200_000:
            raise ValueError("A message has invalid content")
        clean.append({"role": role, "content": content})
    return {"model": model.strip(), "messages": clean}


def json_response(payload, status=200):
    return web.json_response(payload, status=status, headers=NO_STORE)


async def read_chat_request(request):
    length = request.content_length
    if length is not None and not 0 < length <= MAX_BODY:
        raise ValueError("Request body is required or too large")
    body = await request.read()
    if not body or len(body) > MAX_BODY:
        raise ValueError("Request body is required or too large")
    try:
        return validate(json.loads(body))
    except json.JSONDecodeError as error:
        raise ValueError("Request body must be valid JSON") from error


async def models(request):
    try:
        async with request.app[CLIENT_KEY].get(
            request.app[OLLAMA_URL_KEY] + "/api/tags"
        ) as upstream:
            if upstream.status != 200:
                raise RuntimeError(f"Ollama returned HTTP {upstream.status}")
            data = await upstream.json()
        names = [item.get("name") for item in data.get("models", [])]
        return json_response(
            {"models": sorted(name for name in names if isinstance(name, str))}
        )
    except (ClientError, asyncio.TimeoutError) as error:
        return json_response({"error": f"Cannot reach Ollama: {error}"}, status=502)
    except (RuntimeError, json.JSONDecodeError, TypeError) as error:
        return json_response({"error": str(error)}, status=502)


async def chat(request):
    try:
        payload = await read_chat_request(request)
    except (ValueError, web.HTTPRequestEntityTooLarge) as error:
        return json_response({"error": str(error)}, status=400)

    try:
        upstream = await request.app[CLIENT_KEY].post(
            request.app[OLLAMA_URL_KEY] + "/api/chat",
            json={**payload, "stream": True},
            headers={"Accept": "application/x-ndjson"},
        )
    except (ClientError, asyncio.TimeoutError) as error:
        return json_response({"error": f"Cannot reach Ollama: {error}"}, status=502)

    if upstream.status != 200:
        detail = (await upstream.text(errors="replace"))[:500]
        upstream.release()
        return json_response(
            {"error": f"Ollama returned HTTP {upstream.status}: {detail}"}, status=502
        )

    response = web.StreamResponse(
        headers={
            **NO_STORE,
            "Content-Type": "application/x-ndjson; charset=utf-8",
        }
    )
    await response.prepare(request)
    try:
        async for chunk in upstream.content.iter_any():
            await response.write(chunk)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        upstream.close()
    with suppress(ConnectionResetError):
        await response.write_eof()
    return response


def voice_summary(data):
    payload = {"available": bool(data.get("ready"))}
    for key in ("device", "model", "models", "capabilities"):
        if key in data:
            payload[key] = data[key]
    return payload


async def voice_status(request):
    try:
        async with request.app[CLIENT_KEY].get(
            request.app[SPEECH_URL_KEY] + "/ready"
        ) as upstream:
            data = await upstream.json(content_type=None)
            if upstream.status != 200:
                return json_response(
                    {"available": False, "error": "Local speech service is not ready"}
                )
        return json_response(voice_summary(data))
    except (ClientError, asyncio.TimeoutError, json.JSONDecodeError, TypeError):
        return json_response(
            {"available": False, "error": "Local speech service is unavailable"}
        )


def voice_event(message):
    if len(message) > 16_384:
        raise ValueError("Voice control message is too large")
    try:
        event = json.loads(message)
    except json.JSONDecodeError as error:
        raise ValueError("Voice control message must be valid JSON") from error
    if not isinstance(event, dict) or event.get("type") not in VOICE_EVENTS:
        raise ValueError("Voice control message has an invalid type")
    if event["type"] == "session.update":
        session = event.get("session")
        if not isinstance(session, dict):
            raise ValueError("Voice session settings must be an object")
        event["session"] = {
            **session,
            "sample_rate": VOICE_SAMPLE_RATE,
            "word_timestamps": False,
            "speaker_diarization": False,
        }
    return json.dumps(event, separators=(",", ":"))


async def voice_realtime(request):
    try:
        upstream = await request.app[CLIENT_KEY].ws_connect(
            websocket_url(request.app[SPEECH_URL_KEY], "/v1/realtime"),
            max_msg_size=MAX_BODY,
        )
    except (ClientError, asyncio.TimeoutError):
        return json_response(
            {"error": "Local speech service is unavailable"}, status=503
        )

    downstream = web.WebSocketResponse(max_msg_size=MAX_VOICE_BYTES)
    await downstream.prepare(request)

    async def to_speech():
        audio_bytes = 0
        async for message in downstream:
            if message.type == WSMsgType.BINARY:
                audio_bytes += len(message.data)
                if audio_bytes > MAX_VOICE_BYTES:
                    await downstream.close(code=1009, message=b"Voice session is too long")
                    return
                await upstream.send_bytes(message.data)
            elif message.type == WSMsgType.TEXT:
                try:
                    event = voice_event(message.data)
                except ValueError as error:
                    await downstream.send_json(
                        {"type": "error", "error": {"message": str(error)}}
                    )
                    await downstream.close(code=1008)
                    return
                await upstream.send_str(event)
            elif message.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                return

    async def to_browser():
        async for message in upstream:
            if message.type == WSMsgType.TEXT:
                await downstream.send_str(message.data)
            elif message.type == WSMsgType.BINARY:
                await downstream.send_bytes(message.data)
            elif message.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                return

    tasks = {asyncio.create_task(to_speech()), asyncio.create_task(to_browser())}
    try:
        done, pending = await asyncio.wait(
            tasks,
            timeout=MAX_VOICE_SECONDS + 5,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done and not downstream.closed:
            await downstream.send_json(
                {"type": "error", "error": {"message": "Voice session is too long"}}
            )
            await downstream.close(code=1009)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    finally:
        await upstream.close()
        if not downstream.closed:
            await downstream.close()
    return downstream


async def static_file(request):
    path = request.match_info.get("path", "")
    file = (STATIC / (path or "index.html")).resolve()
    if not file.is_file() or STATIC not in file.parents:
        return json_response({"error": "Not found"}, status=404)
    return web.FileResponse(file, headers=NO_STORE)


async def client_session(app):
    timeout = ClientTimeout(total=None, connect=5, sock_read=300)
    app[CLIENT_KEY] = ClientSession(timeout=timeout)
    yield
    await app[CLIENT_KEY].close()


def create_app(
    ollama_url="http://127.0.0.1:11434",
    speech_url="http://127.0.0.1:8080",
):
    app = web.Application(client_max_size=MAX_BODY)
    app[OLLAMA_URL_KEY] = service_url(ollama_url, "OLLAMA_URL")
    app[SPEECH_URL_KEY] = service_url(speech_url, "SPEECH_URL")
    app.cleanup_ctx.append(client_session)
    app.router.add_get("/api/models", models)
    app.router.add_post("/api/chat", chat)
    app.router.add_get("/api/voice/status", voice_status)
    app.router.add_get("/api/voice/realtime", voice_realtime)
    app.router.add_get("/", static_file)
    app.router.add_get("/{path:.*}", static_file)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3000)
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--speech-url", default="http://127.0.0.1:8080")
    args = parser.parse_args()
    app = create_app(args.ollama_url, args.speech_url)
    print(
        f"Local LLM Chat: http://{args.host}:{args.port}\n"
        f"Ollama: {args.ollama_url}\nSpeech: {args.speech_url}"
    )
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
