"""Small, opt-in Submagic API client for GAPCUT AI.

This module deliberately does not change the main editing pipeline. It is
used by the isolated Submagic API test spike until language and real-media
acceptance tests are complete.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
from pathlib import Path
from typing import Any

import httpx


DEFAULT_BASE_URL = "https://api.submagic.co/v1"
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024


class SubmagicError(RuntimeError):
    """Base error for safe, user-displayable Submagic integration failures."""


class SubmagicNotConfigured(SubmagicError):
    """Raised when a request is attempted without an API key."""


class SubmagicAPIError(SubmagicError):
    """Raised for HTTP/API failures without exposing credentials."""


class SubmagicClient:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 180.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_key = (api_key or os.getenv("SUBMAGIC_API_KEY", "")).strip()
        self.base_url = base_url.rstrip("/")
        headers = {"x-api-key": self.api_key} if self.api_key else {}
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=httpx.Timeout(timeout, connect=20.0),
            follow_redirects=False,
            transport=transport,
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    async def __aenter__(self) -> "SubmagicClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _require_key(self) -> None:
        if not self.is_configured:
            raise SubmagicNotConfigured(
                "Submagic API is not configured. Set SUBMAGIC_API_KEY on the backend."
            )

    @staticmethod
    def _decode_response(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise SubmagicAPIError(
                f"Submagic returned a non-JSON response (HTTP {response.status_code})."
            ) from exc

        if not isinstance(payload, dict):
            raise SubmagicAPIError("Submagic returned an unexpected response shape.")

        if response.is_error:
            error = payload.get("message") or payload.get("error") or "Request failed"
            # Never include request headers or API key in exceptions/log messages.
            raise SubmagicAPIError(
                f"Submagic API returned HTTP {response.status_code}: {str(error)[:300]}"
            )
        return payload

    async def get_languages(self) -> list[dict[str, str]]:
        """Return the live language codes advertised by the authenticated API."""
        self._require_key()
        response = await self._client.get("/languages")
        payload = self._decode_response(response)
        languages = payload.get("languages")
        if not isinstance(languages, list):
            raise SubmagicAPIError("Submagic languages response did not contain a list.")

        result: list[dict[str, str]] = []
        for item in languages:
            if not isinstance(item, dict):
                continue
            name, code = item.get("name"), item.get("code")
            if isinstance(name, str) and isinstance(code, str) and code.strip():
                result.append({"name": name.strip(), "code": code.strip().lower()})
        return result

    async def upload_project(
        self,
        video_path: str | Path,
        *,
        title: str,
        language: str,
        template_name: str | None = None,
        dictionary: list[str] | None = None,
        magic_zooms: bool = True,
        magic_brolls: bool = False,
        magic_brolls_percentage: int = 35,
    ) -> dict[str, Any]:
        """Upload one test clip; destructive silence/take cuts and music stay off."""
        self._require_key()
        path = Path(video_path)
        if not path.is_file():
            raise SubmagicError("The source video file does not exist.")
        size = path.stat().st_size
        if size <= 0 or size >= MAX_UPLOAD_BYTES:
            raise SubmagicError("The video must be non-empty and smaller than 2 GB.")
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 100:
            raise SubmagicError("Project title must contain 1–100 characters.")
        if not isinstance(language, str) or not re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})?", language):
            raise SubmagicError("A valid language code from Submagic's language endpoint is required.")
        if not 0 <= int(magic_brolls_percentage) <= 100:
            raise SubmagicError("B-roll percentage must be between 0 and 100.")

        fields: dict[str, str] = {
            "title": title.strip(),
            "language": language,
            "magicZooms": str(bool(magic_zooms)).lower(),
            "magicBrolls": str(bool(magic_brolls)).lower(),
            "magicBrollsPercentage": str(int(magic_brolls_percentage)),
            # Never let a test silently remove speech or add music.
            "removeBadTakes": "false",
            "cleanAudio": "false",
        }
        if template_name:
            fields["templateName"] = template_name
        if dictionary:
            fields["dictionary"] = json.dumps(
                [str(word)[:50] for word in dictionary[:100]],
                ensure_ascii=False,
            )

        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with path.open("rb") as stream:
            response = await self._client.post(
                "/projects/upload",
                data=fields,
                files={"file": (path.name, stream, mime_type)},
            )
        payload = self._decode_response(response)
        if not isinstance(payload.get("id"), str) or not payload["id"].strip():
            raise SubmagicAPIError("Submagic did not return a project ID.")
        return payload

    @staticmethod
    def _validate_project_id(project_id: str) -> str:
        if not isinstance(project_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{8,128}", project_id
        ):
            raise SubmagicError("Invalid Submagic project ID.")
        return project_id

    async def get_project(self, project_id: str) -> dict[str, Any]:
        self._require_key()
        safe_id = self._validate_project_id(project_id)
        response = await self._client.get(f"/projects/{safe_id}")
        return self._decode_response(response)

    async def export_project(
        self,
        project_id: str,
        *,
        width: int = 1080,
        height: int = 1920,
        fps: int = 30,
    ) -> dict[str, Any]:
        self._require_key()
        safe_id = self._validate_project_id(project_id)
        if not (1 <= int(width) <= 8192 and 1 <= int(height) <= 8192):
            raise SubmagicError("Export dimensions are outside the supported safety range.")
        if not 1 <= int(fps) <= 60:
            raise SubmagicError("Export FPS must be between 1 and 60.")
        response = await self._client.post(
            f"/projects/{safe_id}/export",
            json={"width": int(width), "height": int(height), "fps": int(fps)},
        )
        return self._decode_response(response)
