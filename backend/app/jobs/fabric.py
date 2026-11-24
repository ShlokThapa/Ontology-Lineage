"""A small client for the Fabric REST API: find items, start a notebook / pipeline run, poll it, cancel it.

Written against the public reference (Job Scheduler and Items APIs). It has not been run against a real
workspace from here: ``FABRIC_API_URL`` lets tests (and a local stand-in server) swap the host.

Secrets stay inside this module: tokens are never logged and never appear in an error message.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import ssl
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from ..config import Settings

log = logging.getLogger(__name__)

SCOPE = "https://api.fabric.microsoft.com/.default"
TERMINAL = {"Completed", "Failed", "Cancelled", "Deduped"}
_GUID = re.compile(r"^[0-9a-fA-F-]{8,64}$")
_MAX_PAGES = 20
_MAX_RATE_LIMIT_WAITS = 3


class FabricError(RuntimeError):
    """A Fabric call failed. The message is safe to show: it never contains a token or a secret."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class FabricNotConfigured(FabricError):
    """No workspace or no way to sign in is set."""


def _tls_context() -> ssl.SSLContext | bool:
    """Verify against the operating system's certificates, so a proxy that re-signs TLS (common on company
    networks) works without switching verification off. Used for this client only, never injected globally."""
    try:
        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:  # noqa: BLE001 - fall back to the default bundle
        return True


def _jwt_expiry(token: str) -> float | None:
    """The ``exp`` claim of a bearer token (seconds since epoch), read without verifying it; None if unreadable."""
    try:
        payload = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return float(data["exp"])
    except Exception:  # noqa: BLE001
        return None


class TokenProvider:
    def __init__(self, settings: Settings, http: httpx.Client, clock: Callable[[], float] = time.time):
        self.s, self.http, self.clock = settings, http, clock
        self._cached: tuple[str, float] | None = None
        self._lock = threading.Lock()

    @property
    def mode(self) -> str:
        s = self.s
        if s.fabric_tenant_id and s.fabric_client_id and s.fabric_client_secret:
            return "service_principal"
        if s.fabric_access_token:
            return "token"
        return "none"

    def expires_in(self) -> int | None:
        """Seconds until the current token stops working, when that is known."""
        if self.mode == "token":
            exp = _jwt_expiry(self.s.fabric_access_token)
            return None if exp is None else int(exp - self.clock())
        if self._cached:
            return int(self._cached[1] - self.clock())
        return None

    def token(self) -> str:
        mode = self.mode
        if mode == "none":
            raise FabricNotConfigured(
                "No way to sign in to Fabric is set. Add FABRIC_TENANT_ID, FABRIC_CLIENT_ID and FABRIC_CLIENT_SECRET "
                "(a service principal) or FABRIC_ACCESS_TOKEN to .env."
            )
        if mode == "token":
            left = self.expires_in()
            if left is not None and left <= 0:
                raise FabricError("FABRIC_ACCESS_TOKEN has expired. Paste a fresh token into .env, or use a service principal.", 401)
            return self.s.fabric_access_token
        with self._lock:
            if self._cached and self._cached[1] - 60 > self.clock():
                return self._cached[0]
            try:
                resp = self.http.post(
                    f"{self.s.fabric_login_url.rstrip('/')}/{self.s.fabric_tenant_id}/oauth2/v2.0/token",
                    data={
                        "grant_type": "client_credentials", "client_id": self.s.fabric_client_id,
                        "client_secret": self.s.fabric_client_secret, "scope": SCOPE,
                    },
                )
            except httpx.HTTPError as exc:
                raise FabricError(f"Could not reach the sign-in service ({type(exc).__name__}).") from exc
            if resp.status_code != 200:
                detail = ""
                try:
                    detail = resp.json().get("error_description", "").splitlines()[0][:200]
                except Exception:  # noqa: BLE001
                    pass
                raise FabricError(f"Sign-in with the service principal failed (HTTP {resp.status_code}). {detail}".strip(), 401)
            body = resp.json()
            self._cached = (body["access_token"], self.clock() + float(body.get("expires_in", 3600)))
            return self._cached[0]


@dataclass
class JobHandle:
    instance_id: str
    retry_after: float | None = None


def build_parameters(params: dict[str, Any], style: str) -> dict[str, Any]:
    """The request body for a run with parameters. Two shapes exist; ``FABRIC_PARAM_STYLE`` picks one."""
    if not params:
        return {}
    if style == "execution_data":
        kinds = {bool: "bool", int: "int", float: "float"}
        return {"executionData": {"parameters": {k: {"value": v, "type": kinds.get(type(v), "string")} for k, v in params.items()}}}
    kinds = {bool: "Boolean", int: "Number", float: "Number"}
    return {"parameters": [{"name": k, "value": v, "type": kinds.get(type(v), "Text")} for k, v in params.items()]}


class FabricClient:
    def __init__(self, settings: Settings, http: httpx.Client | None = None, sleep: Callable[[float], None] = time.sleep):
        self.s = settings
        self.http = http or httpx.Client(verify=_tls_context(), timeout=30)
        self.tokens = TokenProvider(settings, self.http)
        self._sleep = sleep
        self._workspace_id = settings.fabric_workspace_id.strip() or None
        self.base = settings.fabric_api_url.rstrip("/")

    # -- plumbing ---------------------------------------------------------------------------------

    @property
    def configured(self) -> bool:
        return bool(self.s.fabric_workspace_id.strip() or self.s.fabric_workspace_name.strip()) and self.tokens.mode != "none"

    def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        url = path if path.startswith("http") else f"{self.base}{path}"
        for attempt in range(_MAX_RATE_LIMIT_WAITS + 1):
            try:
                resp = self.http.request(method, url, headers={"Authorization": f"Bearer {self.tokens.token()}"}, **kw)
            except httpx.HTTPError as exc:
                raise FabricError(f"Could not reach Fabric ({type(exc).__name__}).") from exc
            if resp.status_code == 429 and attempt < _MAX_RATE_LIMIT_WAITS:
                try:
                    wait = float(resp.headers.get("retry-after", 5))
                except ValueError:
                    wait = 5.0
                log.warning("Fabric rate limit; waiting %.0fs", wait)
                self._sleep(min(wait, 60.0))
                continue
            if resp.status_code >= 400:
                raise self._error(resp)
            return resp
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _error(resp: httpx.Response) -> FabricError:
        code, message = "", ""
        try:
            body = resp.json()
            code, message = str(body.get("errorCode", "")), str(body.get("message", ""))
        except Exception:  # noqa: BLE001
            pass
        hint = {
            401: "Fabric rejected the sign-in (token missing, expired, or for another tenant).",
            403: "The signed-in identity is not allowed to do this in the workspace (it needs Contributor or higher).",
            404: "Fabric could not find it.",
            429: "Fabric is rate limiting this app; try again shortly.",
        }.get(resp.status_code, f"Fabric returned HTTP {resp.status_code}.")
        detail = " ".join(x for x in (code, message[:300]) if x)
        return FabricError(f"{hint} {detail}".strip(), resp.status_code)

    # -- workspace and items ----------------------------------------------------------------------

    def workspace_id(self) -> str:
        if self._workspace_id:
            return self._workspace_id
        name = self.s.fabric_workspace_name.strip()
        if not name:
            raise FabricNotConfigured("Set FABRIC_WORKSPACE_ID or FABRIC_WORKSPACE_NAME in .env.")
        found = [w for w in self._pages("/v1/workspaces") if str(w.get("displayName", "")).lower() == name.lower()]
        if not found:
            raise FabricError(f"No workspace named '{name}' is visible to the signed-in identity. Set FABRIC_WORKSPACE_ID, or add it to the workspace.")
        if len(found) > 1:
            raise FabricError(f"More than one workspace is named '{name}'. Set FABRIC_WORKSPACE_ID.")
        self._workspace_id = str(found[0]["id"])
        return self._workspace_id

    def _pages(self, path: str, params: dict[str, str] | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        token = None
        for _ in range(_MAX_PAGES):
            q = {**(params or {}), **({"continuationToken": token} if token else {})}
            body = self._request("GET", path, params=q).json()
            out.extend(body.get("value") or [])
            token = body.get("continuationToken")
            if not token:
                break
        return out

    def list_items(self, item_type: str) -> list[dict[str, Any]]:
        return self._pages(f"/v1/workspaces/{self.workspace_id()}/items", {"type": item_type})

    # -- running ----------------------------------------------------------------------------------

    def run(self, item_id: str, job_type: str, parameters: dict[str, Any] | None = None) -> JobHandle:
        resp = self._request(
            "POST", f"/v1/workspaces/{self.workspace_id()}/items/{item_id}/jobs/instances",
            params={"jobType": job_type}, json=build_parameters(parameters or {}, self.s.fabric_param_style) or None,
        )
        # Only the id is taken from the Location header; the status URL is rebuilt here, so the bearer token is
        # never sent to an address the server chose.
        location = resp.headers.get("location", "")
        instance = location.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
        if not instance or not _GUID.match(instance):
            raise FabricError("Fabric accepted the run but returned no job instance to follow.")
        try:
            retry = float(resp.headers.get("retry-after", ""))
        except ValueError:
            retry = None
        return JobHandle(instance, retry)

    def status(self, item_id: str, instance_id: str) -> dict[str, Any]:
        body = self._request("GET", f"/v1/workspaces/{self.workspace_id()}/items/{item_id}/jobs/instances/{instance_id}").json()
        failure = body.get("failureReason") or {}
        return {
            "status": str(body.get("status") or "Unknown"),
            "started": body.get("startTimeUtc"),
            "ended": body.get("endTimeUtc"),
            "failure": (failure.get("message") or failure.get("errorCode") or "")[:400] if isinstance(failure, dict) else str(failure)[:400],
            "exit_value": body.get("exitValue"),
        }

    def cancel(self, item_id: str, instance_id: str) -> None:
        self._request("POST", f"/v1/workspaces/{self.workspace_id()}/items/{item_id}/jobs/instances/{instance_id}/cancel")
