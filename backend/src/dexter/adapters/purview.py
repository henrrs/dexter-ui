"""Azure CLI based read-only Microsoft Purview data-plane validation."""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from urllib.parse import urlparse


def validate_configuration(endpoint: str, scope: str, api_path: str) -> dict | None:
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return {"status": "error", "category": "configuration", "detail": "Purview endpoint must be an HTTPS URL without credentials or query parameters."}
    if not api_path.startswith("/") or api_path.startswith("//"):
        return {"status": "error", "category": "configuration", "detail": "Purview API path must be an absolute path on the configured endpoint."}
    if not scope.startswith("https://") or not scope.endswith("/.default"):
        return {"status": "error", "category": "configuration", "detail": "Purview scope must be an HTTPS resource scope ending in /.default."}
    return None


def validate_endpoint(endpoint: str, tenant_id: str | None, scope: str, api_path: str) -> dict:
    configuration_error = validate_configuration(endpoint, scope, api_path)
    if configuration_error:
        return configuration_error

    try:
        from azure.identity import AzureCliCredential

        credential = AzureCliCredential(tenant_id=tenant_id or None)
        token = credential.get_token(scope)
    except ImportError:
        return {"status": "unavailable", "category": "integration", "detail": "Azure Identity SDK is not installed in this Dexter environment."}
    except Exception as exc:
        message = str(exc).lower()
        if any(term in message for term in ("timeout", "connection", "network", "proxy", "name or service", "dns")):
            return {"status": "unavailable", "category": "connectivity", "detail": "The Azure authentication endpoint could not be reached."}
        if any(term in message for term in ("executable not found", "azure cli not found", "not installed")):
            return {"status": "unavailable", "category": "authentication", "detail": "Azure CLI is not available in this runtime."}
        return {"status": "expired", "category": "authentication", "detail": "Azure CLI authentication is unavailable or expired. Sign in with az login and retry."}

    audience = _token_audience(token.token)
    expected = scope.removesuffix("/.default").rstrip("/")
    accepted = {expected}
    if expected == "https://purview.azure.net":
        accepted.add("https://purview.azure.net/")
    if audience is not None and audience not in accepted:
        return {"status": "error", "category": "authentication", "detail": "The Azure CLI token audience does not match the configured Purview API scope."}
    if audience is None:
        return {"status": "error", "category": "authentication", "detail": "The returned Azure access token audience could not be verified."}
    claims = _token_claims(token.token)
    if api_path.startswith("/datamap/api/atlas/") and claims and claims.get("scp"):
        return {
            "status": "error",
            "category": "authentication",
            "detail": "This Purview Atlas data-plane API requires an application identity; the current Azure CLI token is delegated. No alternate identity was attempted.",
        }

    request = urllib.request.Request(endpoint.rstrip("/") + api_path, headers={"Authorization": f"Bearer {token.token}", "Accept": "application/json"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            if 200 <= response.status < 300:
                return {"status": "connected", "category": None, "detail": "Read-only Purview API check succeeded."}
            return {"status": "error", "category": "connectivity", "detail": f"Purview returned HTTP {response.status}."}
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return {"status": "expired", "category": "authentication", "detail": "Purview rejected the Azure CLI token. Verify its audience and tenant."}
        if exc.code == 403:
            return {"status": "no_permission", "category": "authorization", "detail": "The signed-in user does not have permission for this Purview read-only API."}
        return {"status": "error", "category": "connectivity", "detail": f"Purview returned HTTP {exc.code}."}
    except (TimeoutError, OSError, urllib.error.URLError):
        return {"status": "unavailable", "category": "connectivity", "detail": "The configured Purview endpoint could not be reached."}


def discover_accounts() -> dict:
    """Find Purview accounts visible to the current Azure CLI identity via ARM."""
    try:
        from azure.identity import AzureCliCredential
        credential = AzureCliCredential()
        token = credential.get_token("https://management.azure.com/.default")
        claims = _token_claims(token.token)
        if not claims or claims.get("aud") not in {"https://management.azure.com/", "https://management.azure.com"}:
            return {"items": [], "status": "error", "detail": "Azure CLI returned a token for an unexpected Azure management audience."}
    except ImportError:
        return {"items": [], "status": "unavailable", "detail": "Azure Identity is not installed."}
    except Exception as exc:
        return {"items": [], "status": "unavailable", "detail": "Azure CLI authentication is unavailable. Sign in with az login and retry."}
    headers = {"Authorization": f"Bearer {token.token}", "Accept": "application/json"}

    def pages(url: str):
        result = []
        while url:
            parsed_url = urlparse(url)
            if parsed_url.scheme != "https" or parsed_url.hostname != "management.azure.com":
                raise ValueError("Azure Resource Manager returned an unexpected pagination endpoint")
            request = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict):
                break
            result.extend(payload.get("value", []))
            url = payload.get("nextLink")
        return result

    try:
        subscriptions = pages("https://management.azure.com/subscriptions?api-version=2022-12-01")
        accounts = []
        for subscription in subscriptions:
            sub_id = subscription.get("subscriptionId")
            if not isinstance(sub_id, str):
                continue
            url = f"https://management.azure.com/subscriptions/{sub_id}/providers/Microsoft.Purview/accounts?api-version=2021-12-01"
            for account in pages(url):
                properties = account.get("properties") if isinstance(account.get("properties"), dict) else {}
                endpoints = properties.get("endpoints") if isinstance(properties.get("endpoints"), dict) else {}
                endpoint = endpoints.get("catalog")
                parsed = urlparse(endpoint or "")
                if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                    continue
                if parsed.hostname != "api.purview-service.microsoft.com" and not parsed.hostname.endswith(".purview.azure.com"):
                    continue
                accounts.append({"id": account.get("id"), "name": account.get("name"), "tenant_id": claims.get("tid"), "endpoint": endpoint.rstrip("/"), "subscription_id": sub_id})
        # Keep only endpoints where the delegated identity can call the supported Unified Catalog API.
        purview_token = credential.get_token("https://purview.azure.net/.default")
        purview_claims = _token_claims(purview_token.token) or {}
        if purview_claims.get("aud") not in {"https://purview.azure.net", "https://purview.azure.net/"}:
            return {"items": [], "status": "error", "detail": "Azure CLI did not return a token with the Purview API audience."}
        data_headers = {"Authorization": f"Bearer {purview_token.token}", "Accept": "application/json"}
        supported = []
        for account in accounts:
            probe_url = account["endpoint"] + "/datagovernance/catalog/dataAssets?api-version=2026-03-20-preview&top=1"
            try:
                request = urllib.request.Request(probe_url, headers=data_headers, method="GET")
                with urllib.request.urlopen(request, timeout=8) as response:
                    if 200 <= response.status < 300:
                        supported.append(account)
            except urllib.error.HTTPError as exc:
                if exc.code == 403:
                    account["diagnostic"] = "The identity can discover the account but cannot read its Unified Catalog assets."
            except (OSError, urllib.error.URLError):
                continue
        return {"items": supported, "status": "connected" if supported else "unavailable", "detail": f"Found {len(supported)} Purview account(s) with delegated Unified Catalog access." if supported else "No accessible Purview account exposed the delegated Unified Catalog API."}
    except urllib.error.HTTPError as exc:
        category = "authorization" if exc.code == 403 else "connectivity"
        detail = "Azure CLI identity lacks permission to list Purview accounts in one or more subscriptions." if exc.code == 403 else f"Azure management returned HTTP {exc.code}."
        return {"items": [], "status": "no_permission" if exc.code == 403 else "error", "detail": detail, "category": category}
    except (OSError, urllib.error.URLError, ValueError, TypeError):
        return {"items": [], "status": "unavailable", "detail": "Could not list accessible Purview accounts through Azure Resource Manager."}


def _token_audience(token: str) -> str | None:
    claims = _token_claims(token)
    audience = claims.get("aud") if claims else None
    return audience if isinstance(audience, str) else None


def _token_claims(token: str) -> dict | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        value = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        return value if isinstance(value, dict) else None
    except (ValueError, UnicodeError, json.JSONDecodeError):
        return None
