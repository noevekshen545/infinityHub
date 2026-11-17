# Contributing to Infinity Search

[English](README.en.md) · [Русский](README.md) · [Documentation](docs/en/README.md)

Thanks for wanting to help. This project is a local-first workspace with a large
surface area, so small, focused changes are easier to review than large rewrites.

## Before you start

- Read [`docs/en/SYSTEM.md`](docs/en/SYSTEM.md) for the big picture.
- Read [`docs/en/EXTENSIONS.md`](docs/en/EXTENSIONS.md) if you are adding an extension.
- Check existing issues and pull requests.
- For security problems, do **not** open a public issue — see [`SECURITY.md`](SECURITY.md).

## Development setup

```bat
git clone https://github.com/noevekshen545/infinityHub.git
cd infinityHub
setup.bat
```

or manually:

```bash
python -m venv .venv
.venv/Scripts/activate
pip install -r requirements.txt
run-dev.bat
```

## Checks before a pull request

```bash
cd app
python -m compileall -q app modules tools
python -m pytest tests -q
```

Some tests need ComfyUI, Calibre, Hermes or a live server; they are skipped when the
component is absent. Do not weaken a test to make it pass.

## Code style

- Python 3.12, UTF-8, 4 spaces.
- Keep modules small and explicit; avoid new global state.
- Optional dependencies use `try/except ImportError` with a clear message.
- Do not add user data, secrets, tokens or personal paths.
- Do not commit `data/`, `models/`, `engine/`, `defaultapps/`, `ComfyUI_windows_portable/`,
  `InfinitySearch.exe`, `_internal/`, `python/`, `ms-playwright/` or `calibry/`.
- Large binaries go to GitHub Releases, not Git.

## Adding a module

1. Put the logic in `app/modules/` or a route in `app/routes/`.
2. Register it in `app/server/`.
3. Add tests in `app/tests/`.
4. Document the feature in `docs/MODULES.md` and `docs/en/MODULES.md`.
5. If it needs an external component, document it in `docs/EXTERNAL.md` and
   `docs/en/EXTERNAL.md`.

## Adding an extension

Follow [`docs/en/EXTENSIONS.md`](docs/en/EXTENSIONS.md):

- manifest `extension.json`;
- Python `main.py` with `on_load` and hooks;
- JavaScript `frontend.js` with `ExtensionAPI` or `ExtensionRuntime`;
- minimal permissions;
- sandbox for third-party code.

## Documentation

Russian is the primary language; English translations live in `docs/en/`. When you
change a feature, update both versions if possible. Screenshots go to
`docs/screenshots/` and should not contain private data.

## Release assets

- `scripts/pack-assets.ps1` builds `engine.zip` and `hermes.zip`.
- The full portable archive is built locally; it is not committed.
- Always attach a `.sha256` checksum.

## Pull request checklist

- [ ] The change is focused and described.
- [ ] `compileall` passes.
- [ ] Tests pass or are skipped for a documented reason.
- [ ] No user data, secrets or personal paths.
- [ ] Docs updated (Russian and English where possible).
- [ ] Screenshots or logs attached for UI changes.
- [ ] No large binaries in Git.

## License

By contributing, you agree that your changes are distributed under the MIT license.
