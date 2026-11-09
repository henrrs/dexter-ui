"""Small, version-aware HTTP adapter for the installed OpenCode Server API."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterator
from urllib.parse import quote

import httpx


class OpenCodeError(RuntimeError):
    def __init__(self, code: str, message: str, *, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True, slots=True)
class OpenCodeHealth:
    version: str
    healthy: bool


class OpenCodeAdapter:
    """Encapsulates the OpenCode HTTP API; never returns provider credentials."""

    def __init__(self, base_url: str, *, directory: str, username: str | None = None,
                 password: str | None = None, timeout: float = 12.0):
        self.base_url = base_url.rstrip("/")
        self.directory = directory
        self._client = httpx.Client(
            base_url=self.base_url,
            auth=(username, password) if username and password else None,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 5.0)),
            follow_redirects=False,
            trust_env=False,
        )

    def close(self) -> None:
        self._client.close()

    def health(self, *, timeout: float = 0.75) -> OpenCodeHealth:
        """Run a short readiness probe without inheriting request timeouts."""
        payload = self._json("GET", "/global/health", timeout=timeout)
        return OpenCodeHealth(version=str(payload.get("version") or "unknown"),
                              healthy=payload.get("healthy") is True)

    def agents(self, *, timeout: float = 12.0) -> list[dict[str, Any]]:
        payload = self._json("GET", "/agent", params=self._scope(), timeout=timeout)
        result = []
        for item in payload if isinstance(payload, list) else []:
            name = item.get("name")
            if not isinstance(name, str) or not name:
                continue
            result.append({"id": name, "description": str(item.get("description") or ""),
                           "mode": str(item.get("mode") or "primary"),
                           "native": bool(item.get("native"))})
        return result

    def models(self, *, timeout: float = 12.0) -> list[dict[str, Any]]:
        """Return safe models from connected providers only, dropping auth fields."""
        payload = self._json("GET", "/provider", params=self._scope(), timeout=timeout)
        providers = payload.get("all", [])
        connected = {str(item) for item in payload.get("connected", []) if isinstance(item, str)}
        result: list[dict[str, Any]] = []
        for provider in providers if isinstance(providers, list) else []:
            provider_id = provider.get("id")
            if not isinstance(provider_id, str) or provider_id not in connected:
                continue
            model_map = provider.get("models") or {}
            if not isinstance(model_map, dict):
                continue
            for model in model_map.values():
                model_id = model.get("id")
                if not isinstance(model_id, str) or not model_id:
                    continue
                variants = model.get("variants") or {}
                capabilities = model.get("capabilities") or {}
                result.append({
                    "provider_id": provider_id,
                    "provider_name": str(provider.get("name") or provider_id),
                    "id": model_id,
                    "name": str(model.get("name") or model_id),
                    "variants": sorted(str(key) for key in variants) if isinstance(variants, dict) else [],
                    "reasoning": bool(capabilities.get("reasoning")),
                })
        return sorted(result, key=lambda item: (item["provider_name"].casefold(), item["name"].casefold()))

    def create_session(self, *, title: str, agent: str, model: dict | None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "title": title,
            "agent": agent,
            "permission": [{"permission": "*", "pattern": "*", "action": "deny"}],
        }
        if model:
            body["model"] = {"providerID": model["provider_id"], "id": model["id"]}
            if model.get("variant"):
                body["model"]["variant"] = model["variant"]
        return self._json("POST", "/session", params=self._scope(), json=body)

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self._json("GET", f"/session/{quote(session_id, safe='')}", params=self._scope())

    def rename_session(self, session_id: str, title: str) -> None:
        self._json("PATCH", f"/session/{quote(session_id, safe='')}",
                   params=self._scope(), json={"title": title})

    def delete_session(self, session_id: str) -> None:
        self._request("DELETE", f"/session/{quote(session_id, safe='')}", params=self._scope())

    def messages(self, session_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
        payload = self._json("GET", f"/session/{quote(session_id, safe='')}/message",
                             params={**self._scope(), "limit": limit})
        result: list[dict[str, Any]] = []
        for row in payload if isinstance(payload, list) else []:
            info = row.get("info") or {}
            role = info.get("role")
            if role not in {"user", "assistant"}:
                continue
            parts = []
            for part in row.get("parts") or []:
                if part.get("type") != "text" or part.get("ignored"):
                    continue
                text = part.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
            text = "".join(parts)
            if not text and role != "assistant":
                continue
            model = info.get("model") or {}
            result.append({
                "id": str(info.get("id") or ""),
                "role": role,
                "text": text,
                "created_at": info.get("time", {}).get("created"),
                "complete": bool(info.get("time", {}).get("completed")),
                "agent": info.get("agent"),
                "model": {"provider_id": model.get("providerID"), "id": model.get("modelID")}
                    if model.get("providerID") and model.get("modelID") else None,
            })
        return result

    def prompt_async(self, session_id: str, *, text: str, system: str,
                     model: dict, agent: str = "build", message_id: str | None = None) -> None:
        body: dict[str, Any] = {
            "agent": agent,
            "model": {"providerID": model["provider_id"], "modelID": model["id"]},
            "tools": {"*": False},
            "system": system,
            "parts": [{"type": "text", "text": text}],
        }
        if model.get("variant"):
            body["variant"] = model["variant"]
        if message_id:
            body["messageID"] = message_id
        self._request("POST", f"/session/{quote(session_id, safe='')}/prompt_async",
                      params=self._scope(), json=body)

    def abort(self, session_id: str) -> None:
        self._request("POST", f"/session/{quote(session_id, safe='')}/abort", params=self._scope())

    def session_status(self) -> dict[str, Any]:
        payload = self._json("GET", "/session/status", params=self._scope())
        return payload if isinstance(payload, dict) else {}

    def events(self) -> Iterator[dict[str, Any]]:
        """Yield actual OpenCode SSE payloads until the consumer disconnects."""
        timeout = httpx.Timeout(None, connect=5.0, write=5.0, pool=5.0)
        try:
            with self._client.stream("GET", "/event", params=self._scope(), timeout=timeout) as response:
                self._raise(response)
                data_lines: list[str] = []
                for line in response.iter_lines():
                    if line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                    elif not line and data_lines:
                        encoded = "\n".join(data_lines)
                        data_lines.clear()
                        try:
                            payload = json.loads(encoded)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(payload, dict):
                            yield payload
        except httpx.TimeoutException as exc:
            raise OpenCodeError("OPENCODE_TIMEOUT", "OpenCode did not answer in time.") from exc
        except httpx.RequestError as exc:
            raise OpenCodeError("OPENCODE_UNAVAILABLE", "OpenCode connection was interrupted.") from exc

    def _scope(self) -> dict[str, str]:
        return {"directory": self.directory}

    def _json(self, method: str, path: str, **kwargs) -> dict[str, Any] | list:
        response = self._request(method, path, **kwargs)
        if response.status_code == 204 or not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise OpenCodeError("OPENCODE_INVALID_RESPONSE", "OpenCode returned an unreadable response.",
                                status=response.status_code) from exc

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise OpenCodeError("OPENCODE_TIMEOUT", "OpenCode did not answer in time.") from exc
        except httpx.RequestError as exc:
            raise OpenCodeError("OPENCODE_UNAVAILABLE", "OpenCode connection is unavailable.") from exc
        self._raise(response)
        return response

    @staticmethod
    def _raise(response: httpx.Response) -> None:
        if response.is_success:
            return
        status = response.status_code
        if status == 401:
            code, message = "OPENCODE_AUTH_REQUIRED", "OpenCode authentication failed. Check its local server credentials."
        elif status == 404:
            code, message = "OPENCODE_RESOURCE_MISSING", "The OpenCode session or endpoint is no longer available."
        elif status == 409:
            code, message = "OPENCODE_BUSY", "This OpenCode conversation is already processing a message."
        elif status >= 500:
            code, message = "OPENCODE_PROVIDER_FAILED", "OpenCode could not complete the request with its configured provider."
        else:
            code, message = "OPENCODE_REQUEST_REJECTED", "OpenCode rejected the request. Check the selected agent and model."
        raise OpenCodeError(code, message, status=status)
