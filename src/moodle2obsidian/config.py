"""Configuration: config.yaml (yaml.safe_load) with M2O_* environment overrides."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import platformdirs
import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from .errors import M2OError
from .fsutil import secure_write_text

APP_NAME = "moodle2obsidian"


# Windows: MSIX-packaged apps (e.g. Claude Desktop's terminal) silently redirect new %LOCALAPPDATA% folders
# to a private per-app copy, so runs from different apps would see different state. Home is never redirected.
_WIN_HOME = Path.home() / f".{APP_NAME}"


def config_dir() -> Path:
    return _WIN_HOME if sys.platform == "win32" else Path(platformdirs.user_config_dir(APP_NAME, appauthor=False))


def data_dir() -> Path:
    return (
        _WIN_HOME / "data" if sys.platform == "win32" else Path(platformdirs.user_data_dir(APP_NAME, appauthor=False))
    )


def log_dir() -> Path:
    return _WIN_HOME / "logs" if sys.platform == "win32" else Path(platformdirs.user_log_dir(APP_NAME, appauthor=False))


def state_path() -> Path:
    return data_dir() / "state.db"


def default_config_path() -> Path:
    return Path(os.environ.get("M2O_CONFIG") or config_dir() / "config.yaml")


class MoodleConfig(BaseModel):
    url: str = "https://moodle.example.edu"
    service: str = "moodle_mobile_app"
    url_scheme: str = Field("moodlemobile", pattern=r"^[a-z][a-z0-9+.-]*$")
    rate_limit_per_sec: float = Field(1.5, gt=0, le=10)
    timeout_sec: float = Field(30, gt=0, le=600)
    max_retries: int = Field(3, ge=0, le=10)
    browser_channel: Literal["chrome", "msedge", "chromium"] = "chrome"

    @field_validator("url")
    @classmethod
    def _require_https(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.hostname or parts.query or parts.fragment:
            raise ValueError("moodle.url must be an https:// site root, e.g. https://moodle.example.edu")
        return value

    @property
    def host(self) -> str:
        return urlsplit(self.url).hostname or ""


class VaultConfig(BaseModel):
    path: Path = Path("~/Obsidian")
    subdir: str = Field("Moodle", pattern=r"^[^/\\:*?\"<>|]+$")

    @field_validator("path")
    @classmethod
    def _expand(cls, value: Path) -> Path:
        return value.expanduser()


class LLMConfig(BaseModel):
    backend: Literal["claude_code", "anthropic"] = "claude_code"
    model: str = "claude-sonnet-5-5"


class TranscriptionConfig(BaseModel):
    backend: Literal["groq", "local"] = "groq"
    device: Literal["auto", "cpu", "cuda"] = "auto"
    local_model: str = "large-v3-turbo"
    groq_model: str = "whisper-large-v3-turbo"


class CoursesConfig(BaseModel):
    include: list[str] = Field(default_factory=list)  # course ids or name substrings; empty = all
    exclude: list[str] = Field(default_factory=list)
    skip_hidden: bool = True  # courses removed from view ("archived") on the Moodle dashboard
    skip_past: bool = False  # courses whose end date has passed

    @field_validator("include", "exclude", mode="before")
    @classmethod
    def _as_strings(cls, value: Any) -> list[str]:
        return [str(v) for v in value or []]


class FetchConfig(BaseModel):
    downloads_dir: Path | None = None  # default: <data dir>/downloads
    max_file_mb: int = Field(1024, gt=0)
    download_timeout_sec: float = Field(600, gt=0)


class ExtractConfig(BaseModel):
    ocr: bool = True  # OCR pages without a text layer (needs the `tesseract` program)
    ocr_languages: str = Field("ukr+eng", pattern=r"^[a-z_]+(\+[a-z_]+)*$")
    timeout_sec: float = Field(120, gt=0)  # per file; a hung parser is killed, the sync goes on
    max_zip_mb: int = Field(200, gt=0)
    max_zip_members: int = Field(500, gt=0)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="M2O_", env_nested_delimiter="__", extra="forbid")

    moodle: MoodleConfig = Field(default_factory=MoodleConfig)
    vault: VaultConfig = Field(default_factory=VaultConfig)
    language: Literal["uk", "en"] = "uk"
    llm: LLMConfig = Field(default_factory=LLMConfig)
    transcription: TranscriptionConfig = Field(default_factory=TranscriptionConfig)
    courses: CoursesConfig = Field(default_factory=CoursesConfig)
    fetch: FetchConfig = Field(default_factory=FetchConfig)
    extract: ExtractConfig = Field(default_factory=ExtractConfig)

    @property
    def downloads_dir(self) -> Path:
        return (self.fetch.downloads_dir or data_dir() / "downloads").expanduser()

    @property
    def extracted_dir(self) -> Path:
        return data_dir() / "extracted"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # env vars beat config.yaml (passed as init kwargs); secrets are read only by auth.store
        return (env_settings, init_settings)


def load_settings(path: Path | None = None) -> Settings:
    path = path or default_config_path()
    if not path.is_file():
        raise M2OError(
            f"Конфиг не найден: {path}. Запустите `m2o init`.",
            f"Config not found: {path}. Run `m2o init`.",
        )
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise M2OError(f"Ошибка в YAML {path}: {exc}", f"Invalid YAML in {path}: {exc}") from None
    if not isinstance(data, dict):
        raise M2OError(f"Конфиг {path} должен быть YAML-словарём", f"Config {path} must be a YAML mapping")
    try:
        return Settings(**data)
    except ValidationError as exc:
        raise M2OError(f"Неверный конфиг {path}:\n{exc}", f"Invalid config {path}:\n{exc}") from None


def save_settings(settings: Settings, path: Path) -> None:
    body = yaml.safe_dump(settings.model_dump(mode="json"), allow_unicode=True, sort_keys=False)
    secure_write_text(path, "# moodle2obsidian config — see config.example.yaml\n" + body)
