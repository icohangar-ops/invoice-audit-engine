"""Precoro API client.

Precoro enforces a route-based rate limit of ~1 request/minute, so the client
serializes requests per route and sleeps between calls. Sync jobs are designed
to be resumable: every page is persisted before the next request.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from auditengine.config import settings


class PrecoroError(RuntimeError):
    pass


@dataclass
class PrecoroClient:
    token: str = settings.precoro_token
    email: str = settings.precoro_email
    base_url: str = settings.precoro_base_url
    min_interval_s: float = settings.precoro_min_interval_s
    _last_call: dict[str, float] = field(default_factory=dict)

    @property
    def headers(self) -> dict[str, str]:
        # Precoro's edge blocks default Python user agents (403 before auth);
        # a curl-style UA is known to pass.
        return {
            "X-AUTH-TOKEN": self.token,
            "email": self.email,
            "User-Agent": "curl/8.4.0",
        }

    def _throttle(self, route: str) -> None:
        last = self._last_call.get(route)
        if last is not None:
            wait = self.min_interval_s - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_call[route] = time.monotonic()

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        route = path.strip("/").split("/")[0]
        for attempt in range(4):
            self._throttle(route)
            resp = httpx.get(
                f"{self.base_url}{path}", headers=self.headers, params=params, timeout=60
            )
            try:
                body = resp.json()
            except json.JSONDecodeError as exc:
                raise PrecoroError(f"Non-JSON response from {path}: {resp.text[:200]}") from exc
            if isinstance(body, dict) and "Too many requests" in str(body.get("error", "")):
                time.sleep(45 * (attempt + 1))
                continue
            if resp.status_code == 401:
                raise PrecoroError("Precoro auth failed: check PRECORO_TOKEN / PRECORO_EMAIL")
            if resp.status_code >= 400:
                raise PrecoroError(f"{path} -> HTTP {resp.status_code}: {resp.text[:200]}")
            return body
        raise PrecoroError(f"Rate-limited on {path} after retries")

    def iter_invoices(self, per_page: int = 100, max_pages: int = 30):
        """Yield invoice dicts, newest first."""
        page = 1
        while page <= max_pages:
            body = self.get("/invoices", {"page": page, "perPage": per_page})
            data = body.get("data", [])
            yield from data
            pagination = body.get("meta", {}).get("pagination", {})
            if not data or not pagination.get("has_next_page"):
                return
            page += 1

    def invoice_detail(self, idn: int | str) -> dict[str, Any]:
        """Fetch a single invoice with line items. Keyed by document number (idn), not id."""
        return self.get(f"/invoices/{idn}")
