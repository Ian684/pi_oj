"""Small, dependency-light access gate for a private Pi OJ instance.

This is intended for a single-owner deployment behind HTTPS. It is not a
multi-user identity provider or a distributed request quota service.
"""

import base64
import binascii
from collections import defaultdict, deque
import hmac
import os
import threading
import time

from fastapi import Request
from fastapi.responses import JSONResponse


class SlidingWindowLimiter:
    def __init__(self):
        self._lock = threading.Lock()
        self._hits = defaultdict(deque)

    def allow(self, key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and hits[0] <= now - window_seconds:
                hits.popleft()
            if len(hits) >= limit:
                retry_after = max(1, int(window_seconds - (now - hits[0])) + 1)
                return False, retry_after
            hits.append(now)
            return True, 0


def check_basic_authorization(header: str | None, username: str, password: str) -> bool:
    if not header or len(header) > 4096:
        return False
    scheme, sep, token = header.partition(" ")
    if not sep or scheme.lower() != "basic":
        return False
    try:
        decoded = base64.b64decode(token.strip(), validate=True).decode("utf-8")
    except (ValueError, UnicodeError, binascii.Error):
        return False
    if ":" not in decoded:
        return False
    supplied_user, supplied_password = decoded.split(":", 1)
    return (hmac.compare_digest(supplied_user, username)
            and hmac.compare_digest(supplied_password, password))


def install_access_control(app) -> None:
    username = os.getenv("PI_OJ_BASIC_USER", "").strip()
    password = os.getenv("PI_OJ_BASIC_PASSWORD", "")
    if not username or not password or ":" in username:
        raise RuntimeError(
            "Pi OJ authentication is not configured. Set PI_OJ_BASIC_USER and "
            "PI_OJ_BASIC_PASSWORD in .env."
        )

    limiter = SlidingWindowLimiter()
    # Each quota is per instance of the single shared login. A single web worker
    # is used in docker-compose.yml; use Redis for future horizontal scaling.
    quotas = {
        ("POST", "/api/run"): (30, 60),
        ("POST", "/api/ai/help"): (15, 3600),
        ("POST", "/api/uva/submit"): (10, 60),
        ("POST", "/api/tests/fetch"): (20, 60),
    }

    @app.middleware("http")
    async def pi_oj_access_gate(request: Request, call_next):
        # An unprivileged health response is needed by the deployment checks.
        if request.method == "GET" and request.url.path == "/api/health":
            return await call_next(request)

        if not check_basic_authorization(
            request.headers.get("authorization"), username, password
        ):
            # A small bound on failed logins, keyed by the trusted socket peer;
            # do not trust a client-supplied X-Forwarded-For header here.
            peer = request.client.host if request.client else "unknown"
            allowed, retry_after = limiter.allow("auth-failure:" + peer, 30, 60)
            if not allowed:
                return JSONResponse(
                    {"detail": "Too many login attempts"}, status_code=429,
                    headers={"Retry-After": str(retry_after), "Cache-Control": "no-store"},
                )
            return JSONResponse(
                {"detail": "Authentication required"},
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Pi OJ", charset="UTF-8"',
                         "Cache-Control": "no-store"},
            )

        quota = quotas.get((request.method, request.url.path))
        if quota:
            allowed, retry_after = limiter.allow(request.url.path, *quota)
            if not allowed:
                return JSONResponse(
                    {"detail": "Too many requests"},
                    status_code=429,
                    headers={"Retry-After": str(retry_after), "Cache-Control": "no-store"},
                )

        response = await call_next(request)
        # Don't cache authenticated API results in a shared browser/proxy cache.
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response
