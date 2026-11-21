"""User-facing errors."""

from __future__ import annotations


class M2OError(Exception):
    """An error shown to the user in Russian and English. Must never contain secrets."""

    def __init__(self, ru: str, en: str) -> None:
        super().__init__(f"{ru}\n{en}")
        self.ru = ru
        self.en = en
