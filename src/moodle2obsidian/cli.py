"""m2o — command line interface."""

from __future__ import annotations

import logging
import sys
import time
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from .auth import Tokens, private_token_key, token_key
from .auth.password import password_login
from .auth.sso import sso_login
from .auth.store import get_secret, set_secret
from .config import Settings, data_dir, default_config_path, load_settings, log_dir, save_settings, state_path
from .errors import M2OError
from .fetch.collect import Collector, available_functions, course_skip_reason, fetch_courses
from .fsutil import exclusive_lock
from .log import redact, setup_logging
from .moodle.client import MoodleClient, fetch_public_config, normalize_site_url
from .moodle.doctor import EXTERNAL_TOOLS, check_functions, check_tools
from .state import State

# pretty exceptions are off: they print local variables, which may hold tokens
app = typer.Typer(
    help="Moodle → Obsidian knowledge graph.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    add_completion=False,
)
console = Console()
log = logging.getLogger("moodle2obsidian")


def run() -> None:
    # Windows pipes/scheduled tasks default to a legacy codepage that can't print Cyrillic or ✓/✗
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        app()
    except M2OError as exc:
        console.print(Text("✗ " + redact(str(exc)), style="red"))
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception:
        log.debug("unexpected error", exc_info=True)  # redacted traceback goes to the log file
        console.print(Text(f"✗ Непредвиденная ошибка / Unexpected error. Лог / log: {log_dir()}", style="red"))
        sys.exit(1)


@app.callback()
def main(
    ctx: typer.Context,
    config: Annotated[
        Path | None, typer.Option("--config", "-c", help="Path to config.yaml", envvar="M2O_CONFIG")
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug output")] = False,
) -> None:
    setup_logging(log_dir(), verbose)
    ctx.obj = config or default_config_path()


@app.command()
def init(ctx: typer.Context) -> None:
    """Мастер настройки / Interactive setup."""
    path: Path = ctx.obj
    if path.exists() and not typer.confirm(f"{path} уже существует. Перезаписать? / Overwrite?", default=False):
        raise typer.Exit()

    url = normalize_site_url(typer.prompt("Адрес Moodle / Moodle URL"))
    with console.status("Проверяю сайт… / Checking site…"):
        public = fetch_public_config(url)
    wwwroot = normalize_site_url(str(public.get("wwwroot") or url))
    if urlsplit(wwwroot).hostname != urlsplit(url).hostname:
        raise M2OError(
            f"Сайт сообщает другой адрес: {wwwroot}. Укажите его.",
            f"The site reports a different address: {wwwroot}. Use that one.",
        )
    console.print(f"Сайт / site: [bold]{escape(str(public.get('sitename', '')))}[/bold] ({wwwroot})")
    providers = [str(p.get("name")) for p in public.get("identityproviders") or [] if isinstance(p, dict)]
    if providers:
        console.print(f"Вход через / sign-in via: {escape(', '.join(providers))} → `m2o login` откроет браузер")
    if not public.get("enablemobilewebservice"):
        console.print("[yellow]Мобильный веб-сервис выключен — будет режим cookies.[/yellow]")

    vault = Path(typer.prompt("Путь к Obsidian vault / vault path")).expanduser()
    if not vault.is_dir():
        raise M2OError(f"Папка не найдена: {vault}", f"Folder not found: {vault}")
    if not (vault / ".obsidian").is_dir():
        console.print("[yellow]В папке нет .obsidian — это точно vault?[/yellow]")

    language = _choose("Язык конспектов / notes language", ["uk", "en"])
    llm = _choose("LLM backend", ["claude_code", "anthropic"])
    transcription = _choose("Транскрибация / transcription", ["groq", "local"])
    device = _choose("Устройство / device", ["auto", "cpu", "cuda"]) if transcription == "local" else "auto"

    # model_validate (not Settings(...)) so env overrides are not baked into the saved file
    settings = Settings.model_validate(
        {
            "moodle": {"url": wwwroot},
            "vault": {"path": str(vault)},
            "language": language,
            "llm": {"backend": llm},
            "transcription": {"backend": transcription, "device": device},
        }
    )
    save_settings(settings, path)
    console.print(
        f"[green]✓[/green] Конфиг сохранён / saved: {escape(str(path))}\nДальше / next: [bold]m2o login[/bold]"
    )


def _choose(prompt: str, options: list[str]) -> str:
    """Prompt until one of `options` is entered; the first option is the default."""
    while True:
        value = str(typer.prompt(f"{prompt} [{'/'.join(options)}]", default=options[0])).strip()
        if value in options:
            return value
        console.print(f"[yellow]Выберите одно из / choose one of: {', '.join(options)}[/yellow]")


class LoginMethod(StrEnum):
    auto = "auto"
    sso = "sso"
    password = "password"  # noqa: S105 — enum label, not a credential
    token = "token"  # noqa: S105 — enum label, not a credential


@app.command()
def login(
    ctx: typer.Context,
    method: Annotated[
        LoginMethod,
        typer.Option("--method", "-m", help="auto | sso (Google и др.) | password | token (вставить вручную)"),
    ] = LoginMethod.auto,
) -> None:
    """Получить токен Moodle и сохранить в keyring / Get a Moodle token."""
    settings = load_settings(ctx.obj)
    cfg = settings.moodle
    if method is LoginMethod.auto:
        try:
            public = fetch_public_config(cfg.url, cfg.timeout_sec)
        except M2OError:
            public = {}
        method = LoginMethod.sso if public.get("identityproviders") else LoginMethod.password

    if method is LoginMethod.token:
        tokens = Tokens(typer.prompt("Токен (ввод скрыт) / token (hidden)", hide_input=True).strip())
    elif method is LoginMethod.password:
        username = typer.prompt("Логин / username")
        password = typer.prompt("Пароль / password", hide_input=True)
        tokens = password_login(cfg, username, password)
    else:
        console.print(
            "Откроется браузер: войдите (Google, 2FA). Если Moodle спросит про приложение — подтвердите.\n"
            "A browser opens: sign in (Google, 2FA). Confirm if Moodle asks about opening the app."
        )
        result = sso_login(cfg, data_dir() / "cookies.json")
        if result is None:
            console.print(
                "[yellow]Токен не выдан, но сессия сохранена (cookies) — синк пойдёт через скрапер.\n"
                "No token, but the session (cookies) was saved — sync will use the scraper.[/yellow]"
            )
            return
        tokens = result

    with MoodleClient(cfg, tokens.token) as client:
        info: dict[str, Any] = client.call("core_webservice_get_site_info")  # verify before storing
    where = set_secret(token_key(cfg.host), tokens.token)
    if tokens.privatetoken:
        set_secret(private_token_key(cfg.host), tokens.privatetoken)
    console.print(
        f"[green]✓[/green] {escape(str(info.get('fullname', '')))} @ {escape(str(info.get('sitename', '')))}\n"
        f"Токен сохранён / token stored: {escape(where)}"
    )


@app.command()
def doctor(ctx: typer.Context) -> None:
    """Проверить доступ и доступные функции API / Check access and API functions."""
    settings = load_settings(ctx.obj)
    with _client(settings) as client:
        info: dict[str, Any] = client.call("core_webservice_get_site_info")

    console.print(
        f"Сайт / site: [bold]{escape(str(info.get('sitename', '')))}[/bold]  "
        f"Moodle {escape(str(info.get('release', '?')))}  "
        f"пользователь / user: {escape(str(info.get('fullname', '')))}"
    )
    if not info.get("downloadfiles"):
        console.print("[yellow]Сайт запрещает скачивание файлов через API / file download disabled.[/yellow]")

    checks = check_functions(info)
    table = Table(title="Moodle Web Services API")
    table.add_column("Функция / function", no_wrap=True)
    table.add_column("Для чего / purpose")
    table.add_column("Статус / status")
    for check in checks:
        if check.available:
            status = "[green]✓[/green]"
        elif check.required:
            status = "[red]✗ обязательна / required[/red]"
        else:
            status = "[yellow]✗ будет пропущена / skipped[/yellow]"
        table.add_row(check.name, check.purpose, status)
    console.print(table)

    tools = Table(title="Внешние программы / external tools")
    tools.add_column("Программа / tool")
    tools.add_column("Для чего / purpose")
    tools.add_column("Путь / path")
    for name, found in check_tools().items():
        tools.add_row(name, EXTERNAL_TOOLS[name], escape(found) if found else "[yellow]не найдено / not found[/yellow]")
    console.print(tools)

    vault = settings.vault.path
    console.print(
        f"Vault: {escape(str(vault))} "
        + ("[green]✓[/green]" if vault.is_dir() else "[red]✗ не найден / not found[/red]")
    )
    if any(c.required and not c.available for c in checks):
        raise typer.Exit(1)


def _client(settings: Settings) -> MoodleClient:
    host = settings.moodle.host
    token = get_secret(token_key(host))
    if not token:
        raise M2OError(f"Нет токена для {host}. Выполните `m2o login`.", f"No token for {host}. Run `m2o login`.")
    return MoodleClient(settings.moodle, token)


@app.command()
def courses(ctx: typer.Context) -> None:
    """Список курсов и что будет синхронизировано / List courses and what will be synced."""
    settings = load_settings(ctx.obj)
    with _client(settings) as client:
        _, items = fetch_courses(client)
    now = time.time()
    table = Table(title="Курсы / courses")
    table.add_column("ID", justify="right")
    table.add_column("Короткое имя / short", no_wrap=True)
    table.add_column("Название / name")
    table.add_column("Синк / sync")
    for course in items:
        reason = course_skip_reason(course, settings.courses, now)
        table.add_row(
            str(course.get("id")),
            escape(str(course.get("shortname", ""))),
            escape(str(course.get("fullname", ""))),
            "[green]✓[/green]" if reason is None else f"[dim]— {escape(reason)}[/dim]",
        )
    console.print(table)


@app.command()
def sync(
    ctx: typer.Context,
    course: Annotated[
        list[int] | None, typer.Option("--course", help="Только эти ID курсов / only these course IDs")
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Показать, что будет скачано / plan only")] = False,
    no_video: Annotated[bool, typer.Option("--no-video", help="Не качать видеофайлы / skip video files")] = False,
) -> None:
    """Скачать новые и изменённые материалы / Fetch new and changed materials."""
    settings = load_settings(ctx.obj)
    with exclusive_lock(data_dir() / "sync.lock"), _client(settings) as client, State(state_path()) as state:
        info, items = fetch_courses(client)
        now = time.time()
        if course:
            selected = [c for c in items if int(c["id"]) in course]
        else:
            selected = [c for c in items if course_skip_reason(c, settings.courses, now) is None]
        collector = Collector(
            client,
            state,
            settings.fetch,
            settings.downloads_dir,
            available_functions(info),
            dry_run=dry_run,
            no_video=no_video,
            on_event=lambda message: console.print(Text(message)),
        )
        with console.status(f"Синхронизация {len(selected)} курсов… / syncing…"):
            report = collector.run(selected)

    if dry_run:
        for label in report.planned:
            console.print(Text(f"  + {label}"))
    console.print(
        f"Курсов / courses: {report.courses}  модулей / modules: {report.modules} "
        f"(изменено / changed: {report.changed_modules})\n"
        f"Скачано / downloaded: {report.downloaded} ({report.bytes / 1_048_576:.1f} MB)  "
        f"дубли / duplicates: {report.deduplicated}  без изменений / unchanged: {report.unchanged}  "
        f"пропущено / skipped: {report.skipped}"
        + (f"  к скачиванию / planned: {len(report.planned)}" if dry_run else "")
    )
    console.print(f"Папка / folder: {escape(str(settings.downloads_dir))}")
    if report.failed:
        console.print(f"[red]Ошибки / failed: {len(report.failed)}[/red]")
        for label, reason in report.failed:
            console.print(Text(f"  ✗ {label}: {reason}", style="red"))
        raise typer.Exit(1)


@app.command()
def status(ctx: typer.Context) -> None:
    """Что уже скачано / What has been synced so far."""
    settings = load_settings(ctx.obj)
    path = state_path()
    if not path.exists():
        console.print("Синхронизаций ещё не было — запустите `m2o sync` / no sync yet.")
        return
    with State(path, readonly=True) as state:
        stats = state.stats()
    last = datetime.fromtimestamp(stats["last_sync"]).strftime("%Y-%m-%d %H:%M") if stats["last_sync"] else "—"
    console.print(
        f"Курсов / courses: {stats['courses']}  модулей / modules: {stats['modules']}  "
        f"файлов / files: {stats['files']} ({stats['bytes'] / 1_048_576:.1f} MB)\n"
        f"Последний синк / last sync: {last}\n"
        f"Загрузки / downloads: {escape(str(settings.downloads_dir))}"
    )
