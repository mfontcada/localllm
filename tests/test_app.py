import json
import unittest

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from app import create_app, service_url, validate, voice_event, voice_summary


class ValidateChatRequestTests(unittest.TestCase):
    def test_accepts_minimal_chat(self):
        result = validate(
            {"model": "qwen3:8b", "messages": [{"role": "user", "content": "Hi"}]}
        )
        self.assertEqual(result["model"], "qwen3:8b")
        self.assertEqual(result["messages"][0]["content"], "Hi")

    def test_drops_untrusted_message_fields(self):
        result = validate(
            {
                "model": "test",
                "messages": [
                    {"role": "user", "content": "Hi", "images": ["ignored"]}
                ],
                "stream": False,
            }
        )
        self.assertEqual(
            result,
            {"model": "test", "messages": [{"role": "user", "content": "Hi"}]},
        )

    def test_rejects_bad_role(self):
        with self.assertRaisesRegex(ValueError, "invalid role"):
            validate(
                {"model": "test", "messages": [{"role": "root", "content": "Hi"}]}
            )

    def test_rejects_empty_messages(self):
        with self.assertRaisesRegex(ValueError, "At least one"):
            validate({"model": "test", "messages": []})


class VoiceValidationTests(unittest.TestCase):
    def test_requires_local_http_service_url(self):
        self.assertEqual(service_url("http://127.0.0.1:8080/", "SPEECH"), "http://127.0.0.1:8080")
        with self.assertRaisesRegex(ValueError, "http URL"):
            service_url("https://speech.example", "SPEECH")

    def test_locks_audio_format_and_disables_extra_analysis(self):
        result = json.loads(
            voice_event(
                json.dumps(
                    {
                        "type": "session.update",
                        "session": {
                            "sample_rate": 48_000,
                            "language": "auto",
                            "speaker_diarization": True,
                        },
                    }
                )
            )
        )
        self.assertEqual(result["session"]["sample_rate"], 16_000)
        self.assertFalse(result["session"]["speaker_diarization"])
        self.assertEqual(result["session"]["language"], "auto")

    def test_rejects_unknown_voice_control_events(self):
        with self.assertRaisesRegex(ValueError, "invalid type"):
            voice_event('{"type":"response.create"}')

    def test_voice_summary_does_not_expose_unexpected_fields(self):
        result = voice_summary(
            {"ready": True, "device": "cuda:0", "model": "local", "secret": "x"}
        )
        self.assertEqual(
            result, {"available": True, "device": "cuda:0", "model": "local"}
        )


class AppIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.upstream_state = {"chat": None, "audio": bytearray(), "session": None}
        upstream = web.Application()
        upstream.router.add_get("/api/tags", self.fake_tags)
        upstream.router.add_post("/api/chat", self.fake_chat)
        upstream.router.add_get("/ready", self.fake_ready)
        upstream.router.add_get("/v1/realtime", self.fake_voice)
        self.upstream = TestServer(upstream)
        await self.upstream.start_server()
        base = str(self.upstream.make_url("")).rstrip("/")
        self.client = TestClient(TestServer(create_app(base, base)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.upstream.close()

    async def fake_tags(self, _request):
        return web.json_response({"models": [{"name": "zeta"}, {"name": "alpha"}]})

    async def fake_chat(self, request):
        self.upstream_state["chat"] = await request.json()
        response = web.StreamResponse(
            headers={"Content-Type": "application/x-ndjson"}
        )
        await response.prepare(request)
        await response.write(b'{"message":{"content":"Hi"}}\n')
        await response.write(b'{"message":{"content":" there"},"done":true}\n')
        await response.write_eof()
        return response

    async def fake_ready(self, _request):
        return web.json_response(
            {"ready": True, "device": "cuda:0", "model": "nemotron", "private": "hidden"}
        )

    async def fake_voice(self, request):
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        await socket.send_json({"type": "session.created"})
        async for message in socket:
            if message.type == WSMsgType.TEXT:
                event = json.loads(message.data)
                if event["type"] == "session.update":
                    self.upstream_state["session"] = event["session"]
                    await socket.send_json({"type": "session.updated"})
                elif event["type"] == "input_audio_buffer.commit":
                    await socket.send_json(
                        {
                            "type": "conversation.item.input_audio_transcription.completed",
                            "transcript": "Hello locally",
                        }
                    )
            elif message.type == WSMsgType.BINARY:
                self.upstream_state["audio"].extend(message.data)
                await socket.send_json(
                    {
                        "type": "conversation.item.input_audio_transcription.delta",
                        "delta": "Hello",
                    }
                )
        return socket

    async def test_models_are_sorted(self):
        response = await self.client.get("/api/models")
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {"models": ["alpha", "zeta"]})

    async def test_chat_validation_and_streaming_are_preserved(self):
        response = await self.client.post(
            "/api/chat",
            json={
                "model": "alpha",
                "messages": [{"role": "user", "content": "Hi", "ignored": True}],
                "stream": False,
            },
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(
            await response.text(),
            '{"message":{"content":"Hi"}}\n'
            '{"message":{"content":" there"},"done":true}\n',
        )
        self.assertEqual(
            self.upstream_state["chat"],
            {
                "model": "alpha",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": True,
            },
        )

    async def test_rejects_invalid_chat_json(self):
        response = await self.client.post(
            "/api/chat", data="not json", headers={"Content-Type": "application/json"}
        )
        self.assertEqual(response.status, 400)
        self.assertIn("valid JSON", (await response.json())["error"])

    async def test_voice_status_filters_upstream_details(self):
        response = await self.client.get("/api/voice/status")
        self.assertEqual(
            await response.json(),
            {"available": True, "device": "cuda:0", "model": "nemotron"},
        )

    async def test_voice_websocket_proxies_audio_and_events(self):
        socket = await self.client.ws_connect("/api/voice/realtime")
        self.assertEqual((await socket.receive_json())["type"], "session.created")
        await socket.send_json(
            {
                "type": "session.update",
                "session": {"sample_rate": 48_000, "language": "auto"},
            }
        )
        self.assertEqual((await socket.receive_json())["type"], "session.updated")
        await socket.send_bytes(b"\x01\x00\x02\x00")
        delta = await socket.receive_json()
        self.assertEqual(delta["delta"], "Hello")
        await socket.send_json({"type": "input_audio_buffer.commit"})
        completed = await socket.receive_json()
        self.assertEqual(completed["transcript"], "Hello locally")
        await socket.close()
        self.assertEqual(self.upstream_state["audio"], b"\x01\x00\x02\x00")
        self.assertEqual(self.upstream_state["session"]["sample_rate"], 16_000)

    async def test_static_path_cannot_escape_static_directory(self):
        response = await self.client.get("/%2e%2e/app.py")
        self.assertEqual(response.status, 404)

    async def test_chat_page_handles_mobile_keyboard_layout(self):
        page = await self.client.get("/")
        self.assertIn("interactive-widget=resizes-content", await page.text())

        styles = await self.client.get("/style.css")
        style_text = await styles.text()
        self.assertIn("position: sticky", style_text)
        self.assertIn("--app-height", style_text)
        self.assertIn("overflow-anchor: none", style_text)

        script = await self.client.get("/app.js")
        script_text = await script.text()
        self.assertIn("visualViewport", script_text)
        self.assertIn("scrollMessagesToEnd", script_text)
        self.assertIn("scheduleScrollMessagesToEnd", script_text)
        self.assertIn('addEventListener("focus", scheduleScrollMessagesToEnd)', script_text)


if __name__ == "__main__":
    unittest.main()
