"""Username/password login via /login/token.php (sites without SSO)."""

from __future__ import annotations

import httpx

from ..config import MoodleConfig
from ..errors import M2OError
from ..log import register_secret
from ..moodle.client import network_error, new_http_client, parse_json
from . import Tokens


def password_login(cfg: MoodleConfig, username: str, password: str) -> Tokens:
    register_secret(password)
    with new_http_client(cfg.timeout_sec) as http:
        try:
            response = http.post(
                f"{cfg.url}/login/token.php",
                data={"username": username, "password": password, "service": cfg.service},
            )
        except httpx.TransportError as exc:
            raise network_error(exc) from None
    if response.status_code != 200:
        raise M2OError(
            f"login/token.php ответил HTTP {response.status_code}",
            f"login/token.php returned HTTP {response.status_code}",
        )
    data = parse_json(response)
    token = data.get("token") if isinstance(data, dict) else None
    if not isinstance(token, str) or not token:
        reason = data.get("error", "unknown") if isinstance(data, dict) else "unknown"
        raise M2OError(
            f"Moodle отказал во входе: {reason}. Если вы входите через Google/SSO — `m2o login --method sso`.",
            f"Moodle rejected the login: {reason}. For Google/SSO accounts use `m2o login --method sso`.",
        )
    register_secret(token)
    private = data.get("privatetoken")
    return Tokens(token, private if isinstance(private, str) and private else None)
