# iPhone Backup Viewer

Browse the messages, photos and videos inside old iPhone backups (iTunes, Finder or the Apple Devices app),
without restoring them to a phone.

- **Read-only.** It never modifies a backup.
- **Offline.** No network access, no telemetry.
- **Old backups welcome.** It aims to support iOS 4 through current.

> **Status: pre-alpha.** Iteration 1 (backup list + photo/video gallery) is in progress. See
> [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#iterations) for the roadmap.

## Run from source (Windows)

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m iphone_backup_viewer
```

The app looks for backups in the standard locations automatically:

- `%APPDATA%\Apple Computer\MobileSync\Backup`
- `%USERPROFILE%\Apple\MobileSync\Backup`

Use **File → Open folder…** for backups copied anywhere else.

## Development

```powershell
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\python -m tests.fixtures.fake_backup demo-backups   # synthetic backups to click through
```

Never commit real backup data. `local/` and `demo-backups/` are git-ignored for that reason.

## License

MIT. See [LICENSE](LICENSE).
