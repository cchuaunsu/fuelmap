"""Single-password gate for the whole site (pages + API).

Active only when FIE_ACCESS_PASSWORD is set — local development stays open.
Uses HTTP Basic auth so every browser (desktop and phone) shows its native
password prompt and remembers the credential; any username is accepted,
only the password is checked (constant-time).

The health endpoint stays open so hosting platforms can probe liveness.
"""

from __future__ import annotations

import base64
import secrets

_OPEN_PATHS = {"/api/v1/health"}


class PasswordGate:
    def __init__(self, app, password: str) -> None:
        self._app = app
        self._password = password

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["path"] in _OPEN_PATHS:
            await self._app(scope, receive, send)
            return

        if self._authorized(scope):
            await self._app(scope, receive, send)
            return

        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"www-authenticate", b'Basic realm="Fuel Prices NCR", charset="UTF-8"'),
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b"Password required."})

    def _authorized(self, scope) -> bool:
        for name, value in scope.get("headers", []):
            if name != b"authorization":
                continue
            try:
                kind, _, credentials = value.decode("latin-1").partition(" ")
                if kind.lower() != "basic":
                    return False
                decoded = base64.b64decode(credentials.strip()).decode("utf-8")
                _, _, password = decoded.partition(":")
            except Exception:
                return False
            return secrets.compare_digest(
                password.encode(), self._password.encode()
            )
        return False
