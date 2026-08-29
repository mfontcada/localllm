#!/usr/bin/env python3
"""Small, local-only chat interface for Ollama."""

import argparse
import http.client
import json
import mimetypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).parent.resolve()
STATIC = ROOT / "static"
MAX_BODY = 2 * 1024 * 1024
ROLES = {"system", "user", "assistant", "tool"}


class OllamaError(RuntimeError):
    pass


class Ollama:
    def __init__(self, url):
        parsed = urlparse(url)
        if parsed.scheme != "http" or not parsed.hostname:
            raise ValueError("OLLAMA_URL must be an http URL")
        self.host, self.port = parsed.hostname, parsed.port or 80
        self.prefix = parsed.path.rstrip("/")

    def request(self, method, path, body=None):
        connection = http.client.HTTPConnection(self.host, self.port, timeout=300)
        headers = {"Accept": "application/x-ndjson"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, self.prefix + path, body, headers)
            return connection, connection.getresponse()
        except OSError as error:
            connection.close()
            raise OllamaError(f"Cannot reach Ollama at {self.host}:{self.port}") from error


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalLLMChat/0.1"

    @property
    def ollama(self):
        return self.server.ollama

    def do_GET(self):
        path = urlparse(self.path).path
        self.models() if path == "/api/models" else self.static(path)

    def do_POST(self):
        if urlparse(self.path).path == "/api/chat":
            self.chat()
        else:
            self.respond(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def models(self):
        try:
            connection, response = self.ollama.request("GET", "/api/tags")
            data, status = response.read(), response.status
            connection.close()
            if status != HTTPStatus.OK:
                raise OllamaError(f"Ollama returned HTTP {status}")
            names = [item.get("name") for item in json.loads(data).get("models", [])]
            self.respond(HTTPStatus.OK, {"models": sorted(n for n in names if isinstance(n, str))})
        except (OllamaError, json.JSONDecodeError) as error:
            self.respond(HTTPStatus.BAD_GATEWAY, {"error": str(error)})

    def chat(self):
        try:
            request = validate(self.read_json())
        except ValueError as error:
            self.respond(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        try:
            body = json.dumps({**request, "stream": True}).encode()
            connection, response = self.ollama.request("POST", "/api/chat", body)
        except OllamaError as error:
            self.respond(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
            return
        if response.status != HTTPStatus.OK:
            detail = response.read().decode(errors="replace")[:500]
            connection.close()
            self.respond(HTTPStatus.BAD_GATEWAY, {"error": f"Ollama returned HTTP {response.status}: {detail}"})
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            while line := response.readline():
                self.wfile.write(line)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            connection.close()

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError as error:
            raise ValueError("Invalid Content-Length") from error
        if not 0 < length <= MAX_BODY:
            raise ValueError("Request body is required or too large")
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError as error:
            raise ValueError("Request body must be valid JSON") from error

    def static(self, path):
        file = (STATIC / ("index.html" if path == "/" else path.lstrip("/"))).resolve()
        if not file.is_file() or STATIC not in file.parents:
            self.respond(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        data = file.read_bytes()
        mime = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{mime}; charset=utf-8")
        self.send_header("Content-Length", len(data))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def respond(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(data))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        print(f"{self.address_string()} - {format % args}")


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3000)
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.ollama = Ollama(args.ollama_url)
    print(f"Local LLM Chat: http://{args.host}:{args.port}\nOllama: {args.ollama_url}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
