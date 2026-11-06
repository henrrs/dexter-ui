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
        return {"items": [], "accounts": [], "status": "unavailable", "category": "integration", "detail": "Azure Identity is not installed."}
    except Exception as exc:
        return {"items": [], "accounts": [], "status": "unavailable", "category": "authentication", "detail": "Azure CLI authentication is unavailable. Run az login and retry."}
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
                if not _is_supported_account_host(parsed.hostname):
                    continue
                accounts.append({"id": account.get("id"), "name": account.get("name"), "tenant_id": claims.get("tid"), "account_endpoint": endpoint.rstrip("/"), "subscription_id": sub_id})
        if not accounts:
            return {"items": [], "accounts": [], "status": "unavailable", "category": "discovery", "detail": "No Purview accounts were found in Azure subscriptions visible to the signed-in identity."}
        try:
            purview_token = credential.get_token("https://purview.azure.net/.default")
        except Exception:
            return {"items": [], "accounts": accounts, "status": "unavailable", "category": "authentication", "detail": "Purview accounts were discovered, but Azure CLI could not provide a Purview token. Run az login and retry."}
        purview_claims = _token_claims(purview_token.token) or {}
        if purview_claims.get("aud") not in {"https://purview.azure.net", "https://purview.azure.net/"}:
            return {"items": [], "accounts": accounts, "status": "error", "category": "authentication", "detail": "Azure CLI did not return a token with the Purview API audience."}
        data_headers = {"Authorization": f"Bearer {purview_token.token}", "Accept": "application/json"}
        supported = []
        diagnostics = []
        # Prefer a tenant-specific platform endpoint when ARM returns one. Otherwise
        # use the shared service root documented for Unified Catalog.
        routes = {}
        for account in accounts:
            base = resolve_api_endpoint(account["account_endpoint"], "/datagovernance/catalog/dataAssets")
            routes.setdefault(base, []).append(account)
        unified_error = None
        for api_endpoint, route_accounts in routes.items():
            probe_url = api_endpoint + "/datagovernance/catalog/dataAssets?api-version=2026-03-20-preview&includeExtendedProperties=true&top=1"
            try:
                request = urllib.request.Request(probe_url, headers=data_headers, method="GET")
                with urllib.request.urlopen(request, timeout=8) as response:
                    if 200 <= response.status < 300:
                        supported.extend({**account, "endpoint": api_endpoint, "api_surface": "unified_catalog", "status": "connected"} for account in route_accounts)
            except urllib.error.HTTPError as exc:
                diagnostic = _http_diagnostic(exc.code)
                unified_error = unified_error or diagnostic
                diagnostics.extend({"account": account.get("name"), **diagnostic, "http_status": exc.code, "api_surface": "unified_catalog"} for account in route_accounts)
            except (TimeoutError, OSError, urllib.error.URLError):
                diagnostic = {"status": "unavailable", "category": "connectivity", "detail": "The Unified Catalog service could not be reached."}
                unified_error = unified_error or diagnostic
                diagnostics.extend({"account": account.get("name"), **diagnostic, "api_surface": "unified_catalog"} for account in route_accounts)

        # Data Map uses the account endpoint with its /catalog suffix removed and
        # a read-only Discovery query. Probe each account because this API is scoped.
        if not supported:
            for account in accounts:
                endpoint = account["account_endpoint"]
                base = resolve_api_endpoint(endpoint, "/datamap/api/search/query")
                url = base + "/datamap/api/search/query?api-version=2023-09-01"
                headers = {**data_headers, "Content-Type": "application/json"}
                request = urllib.request.Request(url, data=json.dumps({"keywords": None, "limit": 1}).encode(), headers=headers, method="POST")
                try:
                    with urllib.request.urlopen(request, timeout=8) as response:
                        if 200 <= response.status < 300:
                            supported.append({**account, "endpoint": base, "api_surface": "data_map", "status": "connected"})
                except urllib.error.HTTPError as exc:
                    diag = _http_diagnostic(exc.code)
                    diagnostics.append({"account": account.get("name"), **diag, "http_status": exc.code})
                except (TimeoutError, OSError, urllib.error.URLError):
                    diagnostics.append({"account": account.get("name"), "status": "unavailable", "category": "connectivity", "detail": "The Data Map API could not be reached."})
        if supported:
            usable_by_id = {item.get("id"): item for item in supported}
            account_results = [{**account, **usable_by_id.get(account.get("id"), {})} for account in accounts]
            detail = f"Found {len(accounts)} Purview account(s); read-only metadata access succeeded through {supported[0]['api_surface'].replace('_', ' ').title()}."
            status, category = "connected", None
        else:
            failure = next((item for item in diagnostics if item.get("category") == "authorization"), None) or next((item for item in diagnostics if item.get("category") == "authentication"), None) or next((item for item in diagnostics if item.get("category") == "endpoint"), None) or (diagnostics[0] if diagnostics else unified_error)
            failure = failure or {"status": "unavailable", "category": "connectivity", "detail": "No supported read-only Purview API was reachable."}
            status, category = failure["status"], failure.get("category")
            detail = f"{len(accounts)} Purview account(s) discovered, but metadata access failed: {failure.get('detail', 'API access failed.')}"
            account_results = [{**account, "status": status, "category": category} for account in accounts]
        return {"items": supported, "accounts": account_results, "status": status, "category": category, "detail": detail, "diagnostics": diagnostics}
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return {"items": [], "accounts": [], "status": "expired", "category": "authentication", "detail": "Azure Resource Manager rejected the Azure CLI token. Run az login and retry."}
        if exc.code == 403:
            return {"items": [], "accounts": [], "status": "no_permission", "detail": "Azure CLI identity lacks permission to list Purview accounts in one or more subscriptions.", "category": "authorization"}
        return {"items": [], "accounts": [], "status": "error", "detail": f"Azure management returned HTTP {exc.code}.", "category": "connectivity"}
    except (OSError, urllib.error.URLError, ValueError, TypeError):
        return {"items": [], "status": "unavailable", "detail": "Could not list accessible Purview accounts through Azure Resource Manager."}


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
