import json
import tempfile
import unittest
from pathlib import Path

import httpx

from submagic_client import (
    SubmagicAPIError,
    SubmagicClient,
    SubmagicNotConfigured,
)


class SubmagicClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_key_fails_before_network_request(self):
        async with SubmagicClient(api_key="") as client:
            self.assertFalse(client.is_configured)
            with self.assertRaises(SubmagicNotConfigured):
                await client.get_languages()

    async def test_get_languages_uses_authenticated_endpoint(self):
        def handler(request):
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.url.path, "/v1/languages")
            self.assertEqual(request.headers["x-api-key"], "sk-test-only")
            return httpx.Response(200, json={
                "languages": [
                    {"name": "Russian", "code": "ru"},
                    {"name": "English", "code": "en"},
                ]
            })

        async with SubmagicClient(
            "sk-test-only", transport=httpx.MockTransport(handler)
        ) as client:
            languages = await client.get_languages()
        self.assertEqual(languages, [
            {"name": "Russian", "code": "ru"},
            {"name": "English", "code": "en"},
        ])

    async def test_upload_keeps_music_and_destructive_cutting_off(self):
        def handler(request):
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.url.path, "/v1/projects/upload")
            self.assertIn("name=\"file\"", request.headers["content-type"])
            body = request.read().decode("latin-1")
            self.assertIn('name="magicZooms"\r\n\r\ntrue', body)
            self.assertIn('name="magicBrolls"\r\n\r\nfalse', body)
            self.assertIn('name="removeBadTakes"\r\n\r\nfalse', body)
            self.assertIn('name="cleanAudio"\r\n\r\nfalse', body)
            self.assertNotIn('name="music"', body)
            self.assertNotIn('name="removeSilencePace"', body)
            return httpx.Response(201, json={"id": "project-12345678", "status": "processing"})

        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "sample.mp4"
            video.write_bytes(b"fake-mp4-content")
            async with SubmagicClient(
                "sk-test-only", transport=httpx.MockTransport(handler)
            ) as client:
                project = await client.upload_project(
                    video, title="GAPCUT API test", language="ru"
                )
        self.assertEqual(project["status"], "processing")

    async def test_get_project_and_export_use_documented_routes(self):
        calls = []

        def handler(request):
            calls.append((request.method, request.url.path))
            if request.url.path.endswith("/export"):
                return httpx.Response(200, json={"status": "exporting"})
            return httpx.Response(200, json={"id": "project-12345678", "status": "completed"})

        async with SubmagicClient(
            "sk-test-only", transport=httpx.MockTransport(handler)
        ) as client:
            project = await client.get_project("project-12345678")
            export = await client.export_project("project-12345678")
        self.assertEqual(project["status"], "completed")
        self.assertEqual(export["status"], "exporting")
        self.assertEqual(calls, [
            ("GET", "/v1/projects/project-12345678"),
            ("POST", "/v1/projects/project-12345678/export"),
        ])

    async def test_api_error_does_not_expose_api_key(self):
        def handler(request):
            return httpx.Response(401, json={
                "error": "UNAUTHORIZED",
                "message": "Invalid or missing API key",
            })

        async with SubmagicClient(
            "sk-secret-value", transport=httpx.MockTransport(handler)
        ) as client:
            with self.assertRaises(SubmagicAPIError) as caught:
                await client.get_languages()
        self.assertNotIn("sk-secret-value", str(caught.exception))
        self.assertIn("401", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
