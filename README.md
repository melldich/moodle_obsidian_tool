# moodle2obsidian

> 🚧 Work in progress.

Downloads materials from your Moodle courses (files, pages, links, videos, assignments, forums),
transcribes videos and builds an Obsidian knowledge graph of lecture notes linked through `[[wikilinks]]`.
Runs weekly and appends new material without touching your own edits.

Завантажує матеріали ваших курсів Moodle, розшифровує відео та будує в Obsidian конспект-граф знань.

## Quick start (current stage: login, doctor, fetching materials)

Requires [uv](https://docs.astral.sh/uv/) and Chrome or Edge (for SSO login).

```bash
git clone https://github.com/melldich/moodle_obsidian_tool && cd moodle_obsidian_tool
uv sync
uv run python -m moodle2obsidian init     # site URL, vault path, language, backends
uv run python -m moodle2obsidian login    # SSO (Google…) opens a browser; or --method password|token
uv run python -m moodle2obsidian doctor   # which Moodle API functions this site allows
uv run python -m moodle2obsidian courses  # enrolled courses and which ones will be synced
uv run python -m moodle2obsidian sync --dry-run   # what would be downloaded
uv run python -m moodle2obsidian sync [--course ID] [--no-video]
uv run python -m moodle2obsidian status
```

Downloads go to the per-user data dir (`%LOCALAPPDATA%\moodle2obsidian\downloads` on Windows), never into the repo.
Re-running `sync` only fetches new or changed files; an interrupted run resumes where it stopped.

`uv run m2o …` works too; on Windows with Smart App Control the generated `m2o.exe` may be blocked,
so `python -m moodle2obsidian` is the reliable form.

Secrets: the token goes to the system keyring (Windows Credential Manager / macOS Keychain / Secret Service),
falling back to a `0600` `.env` in the config dir. Nothing secret is ever written to the repo or logs.

### Development

```bash
uv run python -m pytest && uv run ruff check . && uv run python -m mypy src tests
uv run python -m pre_commit install   # detect-secrets + ruff + forbidden-files guard on every commit
```

## Disclaimer

This tool is for **personal use with your own account only**. You are responsible for complying with
your university's rules and the copyright on course materials. **Do not publish downloaded materials.**

## License

[MIT](LICENSE)
