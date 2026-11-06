"""Azure CLI based read-only Microsoft Purview data-plane validation."""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
import uuid
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


def resolve_api_endpoint(endpoint: str, api_path: str) -> str:
    """Resolve the host from the selected Purview API surface, not a joined resource URL."""
    if api_path.startswith("/datagovernance/catalog/"):
        # Unified Catalog supports the tenant-specific platform endpoint returned by
        # ARM. Legacy account endpoints use the documented shared service root.
        parsed = urlparse(endpoint)
        if parsed.hostname and _is_tenant_service_host(parsed.hostname):
            base = endpoint.rstrip("/")
            return base[:-len("/catalog")] if base.casefold().endswith("/catalog") else base
        return "https://api.purview-service.microsoft.com"
    if api_path.startswith("/datamap/api/"):
        # Data Map uses the account data-plane host, never its /catalog suffix.
        return endpoint.rstrip("/")[:-len("/catalog")] if endpoint.rstrip("/").casefold().endswith("/catalog") else endpoint.rstrip("/")
    return endpoint.rstrip("/")


def _is_tenant_service_host(hostname: str) -> bool:
    suffix = "-api.purview-service.microsoft.com"
    if not hostname.casefold().endswith(suffix):
        return False
    tenant = hostname[:-len(suffix)]
    try:
        uuid.UUID(tenant)
        return True
    except (ValueError, AttributeError):
        return False


def _is_supported_account_host(hostname: str) -> bool:
    normalized = hostname.casefold()
    return normalized == "api.purview-service.microsoft.com" or _is_tenant_service_host(normalized) or normalized.endswith(".purview.azure.com")


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

    api_endpoint = resolve_api_endpoint(endpoint, api_path)
    data_map_search = api_path.startswith("/datamap/api/search/query")
    headers = {"Authorization": f"Bearer {token.token}", "Accept": "application/json"}
    if data_map_search:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(api_endpoint + api_path, data=json.dumps({"keywords": None, "limit": 1}).encode() if data_map_search else None, headers=headers, method="POST" if data_map_search else "GET")
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


def discover_accounts(scope: str = "https://purview.azure.net/.default", api_path: str = "/datagovernance/catalog/dataAssets?api-version=2026-03-20-preview&includeExtendedProperties=true&top=1") -> dict:
    """Use the current Azure CLI tenant token to probe its Unified Catalog endpoint."""
    try:
        from azure.identity import AzureCliCredential
        token = AzureCliCredential().get_token(scope)
    except ImportError:
        return {"status": "unavailable", "category": "integration", "detail": "Azure Identity is not installed in this Dexter environment."}
    except Exception as exc:
        message = str(exc).casefold()
        if any(term in message for term in ("timeout", "connection", "network", "proxy", "dns", "name resolution")):
            return {"status": "unavailable", "category": "connectivity", "detail": "Azure CLI could not reach the authentication service to obtain a Purview token."}
        if any(term in message for term in ("not installed", "executable not found", "command not found", "azure cli not found")):
            return {"status": "unavailable", "category": "integration", "detail": "Azure CLI is not available in this Dexter runtime."}
        return {"status": "expired", "category": "authentication", "detail": "Azure CLI has no valid Purview session. Run az login and retry."}

    claims = _token_claims(token.token)
    if not claims:
        return {"status": "expired", "category": "authentication", "detail": "Azure CLI returned an unreadable Purview token."}
    tenant_id = claims.get("tid")
    if not isinstance(tenant_id, str):
        return {"status": "expired", "category": "authentication", "detail": "The Purview token does not contain a tenant ID."}
    try:
        tenant_id = str(uuid.UUID(tenant_id))
    except ValueError:
        return {"status": "expired", "category": "authentication", "detail": "The Purview token contains an invalid tenant ID."}

    expected_audience = scope.removesuffix("/.default").rstrip("/")
    token_audience = claims.get("aud")
    accepted_audiences = {expected_audience, expected_audience + "/"}
    if not isinstance(token_audience, str) or token_audience not in accepted_audiences:
        return {"status": "expired", "category": "authentication", "tenant_id": tenant_id, "detail": "Azure CLI returned a token with an invalid Purview audience."}
    if not api_path.startswith("/datagovernance/catalog/"):
        return {"status": "error", "category": "configuration", "tenant_id": tenant_id, "detail": "Automatic Purview discovery requires a Unified Catalog read-only endpoint."}

    endpoint = f"https://{tenant_id}-api.purview-service.microsoft.com"
    url = endpoint + api_path
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token.token}", "Accept": "application/json"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            if 200 <= response.status < 300:
                return {"status": "connected", "category": None, "detail": "Tenant Purview Unified Catalog read-only request succeeded.", "tenant_id": tenant_id, "endpoint": endpoint, "api_surface": "unified_catalog"}
            return {"status": "error", "category": "connectivity", "detail": f"Purview returned unexpected HTTP {response.status}.", "tenant_id": tenant_id, "endpoint": endpoint, "api_surface": "unified_catalog"}
    except urllib.error.HTTPError as exc:
        diagnostic = _http_diagnostic(exc.code)
        return {**diagnostic, "tenant_id": tenant_id, "endpoint": endpoint, "api_surface": "unified_catalog"}
    except (TimeoutError, OSError, urllib.error.URLError):
        return {"status": "unavailable", "category": "connectivity", "detail": "The tenant-specific Purview endpoint could not be reached.", "tenant_id": tenant_id, "endpoint": endpoint, "api_surface": "unified_catalog"}


def _http_diagnostic(status_code: int) -> dict:
    if status_code == 401:
        return {"status": "expired", "category": "authentication", "detail": "Purview rejected the delegated Azure CLI token. Verify authentication, tenant, and token audience."}
    if status_code == 403:
        return {"status": "no_permission", "category": "authorization", "detail": "The signed-in Azure CLI identity lacks permission for this read-only metadata API."}
    if status_code == 404:
        return {"status": "error", "category": "endpoint", "detail": "The Purview API route was not found on this endpoint."}
    return {"status": "error", "category": "connectivity", "detail": f"Purview returned HTTP {status_code}."}


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
