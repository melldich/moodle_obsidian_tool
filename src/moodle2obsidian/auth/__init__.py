"""Authentication: Moodle tokens via password, SSO (Playwright) or manual paste."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, repr=False)
class Tokens:
    token: str
    privatetoken: str | None = None

    def __repr__(self) -> str:
        return "Tokens(***)"


def token_key(host: str) -> str:
    # bound to the host so a token is never sent to a different site after a config change
    return f"moodle_token@{host}"


def private_token_key(host: str) -> str:
    return f"moodle_privatetoken@{host}"
