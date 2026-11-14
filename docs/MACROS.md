# Touch controls and macros

The ESP32 sends one numeric macro ID to the host bridge. The bridge then runs the matching action from the local `host/config.json` file.

Commands, paths and URLs are not stored in the ESP32 firmware.

## Default layout

| ID | Label | Action |
| ---: | --- | --- |
| 1 | Claude | Start the Claude desktop app |
| 2 | Android Studio | Start Android Studio |
| 3 | Proton VPN | Start Proton VPN |
| 4 | PowerShell | Open PowerShell 7 (`pwsh`) or Windows PowerShell in your user folder |
| 5 | Task Manager | Open the system monitor |
| 6 | Lock PC | Lock the current session |

## Button icons

On the Deck page each button shows its number and its icon in full colour, without a label (the label is only used by a simple fallback style). The icon is chosen with `"icon"`:

```json
{"id": 1, "label": "Claude", "icon": "claude", "action": "app", "value": {...}}
```

Available icons (built into the firmware): `claude`, `android_studio`, `proton_vpn`, `telegram`, `powershell`, `task_manager`, `okam_pro`, `lock`. Use `""` for no icon. Changing which icon a button uses only needs a restart of the bridge.

To add or replace an icon image:

1. Put the PNG (transparent background) or SVG in `icons/`.
2. Map it to a name in `SOURCES` at the top of `tools/make_button_icons.py`.
3. Run `python tools/make_button_icons.py` (needs `pip install pillow cairosvg`); it rewrites `src/button_icons.h`.
4. Upload the firmware.

## Supported actions

### Start an installed application

```json
{
  "id": 2,
  "label": "Android Studio",
  "action": "app",
  "value": {
    "windows": {
      "paths": [
        "%ProgramFiles%\\Android\\Android Studio\\bin\\studio64.exe",
        "%LOCALAPPDATA%\\Programs\\Android Studio*\\bin\\studio64.exe"
      ],
      "start_menu": "Android Studio"
    }
  }
}
```

- `paths`: tried in order; `%VARIABLES%` and `*` wildcards are expanded, plain names such as `pwsh.exe` are looked up on `PATH`.
- `args`: optional arguments passed to the executable.
- `start_menu`: Windows only. When no path exists, the app is found by its Start menu name and started through `shell:AppsFolder` (works for Store/MSIX apps too).
- `new_console` and `working_directory` work as for `command`.
- A list value (`["open", "-a", "Claude"]`) is treated as executable + arguments.

Useful checks from the `host` folder:

```text
.venv\Scripts\python.exe control_deck_bridge.py --find-app "Proton VPN"
.venv\Scripts\python.exe control_deck_bridge.py --test-macro 3
```

### Open a URL

```json
{
  "id": 1,
  "label": "ChatGPT",
  "action": "url",
  "value": "https://chatgpt.com/"
}
```

### Start a command

A command can be a string or an argument array. Arrays avoid quoting problems and are recommended for Windows scripts.

```json
{
  "id": 3,
  "label": "Voice Server",
  "action": "command",
  "new_console": true,
  "value": {
    "windows": ["cmd.exe", "/c", "C:\\Path\\To\\START_VOICE_SERVER.cmd"]
  },
  "working_directory": {
    "windows": "C:\\Path\\To"
  }
}
```

Options:

- `hidden`: start without a visible console on Windows
- `new_console`: start in a separate console on Windows
- `working_directory`: process working directory

### Start a service only when it is unavailable

The `service` action checks `health_url` before starting the process.

```json
{
  "id": 2,
  "label": "Ollama",
  "action": "service",
  "health_url": "http://127.0.0.1:11434/api/tags",
  "hidden": true,
  "value": {
    "windows": ["ollama", "serve"],
    "linux": ["ollama", "serve"],
    "macos": ["ollama", "serve"]
  }
}
```

If the endpoint is already reachable, the bridge logs that the service is running and does not create a duplicate process.

### Send a hotkey

```json
{
  "id": 4,
  "label": "Mute",
  "action": "hotkey",
  "value": "volume_mute"
}
```

Examples:

- `ctrl+shift+esc`
- `win+r`
- `alt+f2`
- `volume_mute`
- `volume_up`
- `media_play_pause`

### Type text

```json
{
  "id": 5,
  "label": "Signature",
  "action": "text",
  "value": "Hello from ESP32 PC Control Deck"
}
```

## Platform-specific values

`value` and `working_directory` can contain per-platform entries:

```json
"value": {
  "windows": ["wt.exe"],
  "linux": ["x-terminal-emulator"],
  "macos": ["open", "-a", "Terminal"]
}
```

The public example keeps the Windows commands unchanged and provides separate Linux and macOS commands. Linux desktop environments use different terminal and system-monitor applications, so edit only the `linux` value when the default command is not installed.

On macOS, `hotkey` and `text` actions may require Accessibility permission for the terminal or Python host. URL and command actions do not use keyboard injection.

See `LINUX_MACOS.md` for the complete platform setup and troubleshooting guide.

## Security boundary

The display cannot submit an arbitrary command string. It can only submit an ID from 1 to 8. The bridge maps that ID to a command already present in the local ignored configuration file.

Treat `host/config.json` as executable local configuration and add only actions you trust.
