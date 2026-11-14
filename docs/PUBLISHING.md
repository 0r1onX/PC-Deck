# Publishing guide

How to put this customised build on GitHub. Replace `<user>` and `<repo>` with your GitHub user name and the repository name.

## What stays out of Git

`.gitignore` keeps these files out of the repository:

- local configuration and logs: `host/config.json`, `host/control-deck.log`, `*.log`
- build and tool folders: `.pio/`, `.vscode/`, `host/.venv/`, `__pycache__/`
- third-party files that may not be redistributed: `CH341SER.EXE` (USB driver, linked in the README) and `3D printed case/` (MakerWorld case files, linked in the README)
- `backup/` (local copies of earlier versions)

Check the list before the first commit:

```bash
git status --short
```

## First push

1. Create an empty repository `<repo>` on GitHub (without README, license or .gitignore).
2. From the project folder:

```bash
git init
git add .
git status --short
git commit -m "PC Control Deck (CYD) 1.2.0"
git branch -M main
git remote add origin https://github.com/<user>/<repo>.git
git push -u origin main
```

## Release tag

```bash
git tag -a v1.2.0 -m "PC Control Deck (CYD) 1.2.0"
git push origin v1.2.0
```

Release notes: copy the `1.2.0` section of `CHANGELOG.md`.

## Suggested description and topics

```text
USB-connected ESP32 Cheap Yellow Display: PC monitor, weather, app buttons and a music visualizer.
```

```text
esp32 esp32-2432s028 cheap-yellow-display platformio tft-espi ili9341 xpt2046 pc-monitor system-monitor macropad music-visualizer python pyserial psutil librehardwaremonitor
```
