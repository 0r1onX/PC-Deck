# Changelog

All notable changes to this project are documented here.

## [1.2.0] - 2026-11-11 (custom build)

### Added

- CONTROLS page: 8 buttons in 2 rows of 4 (70x78 px): new 4 Telegram and 7 O-KAM PRO; PowerShell, Task Manager and Lock PC move to 5, 6 and 8. Firmware and bridge accept button ids 1-8 (`macros`, `power_button`, `--test-macro`).

### Changed

- CONTROLS buttons (style "B", chosen from a preview): dark neutral gradient, the icon in its real colours in the middle of the button, the number top left, no label (labels are still used by the simple fallback style when there is no memory for the button sprite).
- `tools/make_button_icons.py` stores full-colour icons (alpha + RGB565, 56 px box, glow trimmed at alpha 60 like the tab icons) instead of alpha + luminance; new icons `telegram`, `okam_pro`; new Android Studio icon (`Android Studio 01.png`).
- Page titles: "PC CONTROL DECK" -> "PC MONITOR" (first page), "DISKS" -> "STORAGE", "CONTROLS" -> "DECK"; the same names in the footer fallback text. Protocol page names (`page` frame) are unchanged.
- Footer: new CONTROLS icon (`Deck 01.png`, 8 coloured squares); the other four icons are byte-identical.
- Icon images in `icons/` renamed to short names (`Claude 01.svg`, `Monitor 01.png`, `Deck 01.png`, ...); both icon tools use the new names and produce byte-identical data. Unused images are left out of Git.
- `config.example.json`, README, `docs/MACROS.md` and `docs/PROTOCOL.md` describe the eight buttons (power menu on button 8); the example finds O-KAM by its exact Start menu name "O-KAM Pro" (a plain "O-KAM" also matched an older version).
- Firmware 1.2.0, host bridge 1.2.0. 1 new test, 4 updated for the new button numbers (85 in total). Firmware checked with g++ against mock headers (both board variants); final build in PlatformIO.

## [1.1.0] - 2026-11-06 (custom build)

### Added

- MUSIC page: 10 effects for the spectrum (chosen from mockups): 1 LED (as in 1.0.0), 2 solid bars cyan -> magenta with white peaks, 3 mirror, 4 dots with peaks, 5 wave (smooth line with filled area), 6 oscilloscope (waveform, Winamp style), 7 fire with peaks, 8 ocean (three blue layers), 9 circle (wedges, low notes at the bottom), 10 spikes (warm colours). A tap on the bars picks the next effect and shows its name for 1 s; the choice is kept in NVS. `media.effect` in config.json (1-10) is sent as `music_effect` in the config frame and applied when its value changes. Peaks hold for 0.4 s and then fall.
- Effects are drawn in a 310x94 sprite (58 KB) that is pushed in one go, at most ~30 frames a second; the circle uses a 16 KB lookup map. Both are freed when another page is drawn. Without memory for the sprite the page shows the LED bars.
- Bridge: the `spec` frame also carries 64 waveform points (`w`) for the oscilloscope, starting at a rising zero crossing with automatic gain.
- Black box: new step `music_fx` (effect chosen with a tap).

### Changed

- MUSIC page: title and artist are shown only while music plays; the area stays empty when paused, stopped, with no player or before the PC answers ("Waiting for the PC...", "Nothing playing" and "Music info not available" were removed; problems are still in the bridge log).
- Firmware 1.1.0, host bridge 1.1.0. 4 new tests, 2 extended (84 in total). Firmware checked with g++ against mock headers (both board variants); the effects were also run on the PC against a drawing mock to make the preview pictures. Final build in PlatformIO.

## [1.0.0] - 2026-11-06 (custom build)

### Added

- MUSIC page (5th tab, Windows): 24 spectrum bars of the sound the PC plays (style "A" chosen from mockups: segmented LED bars green -> yellow -> red), song title and artist from the Windows media controls (Spotify, YouTube in a browser, etc.), play state, the system volume (`VOL 45%`, `MUTE`) and five buttons: volume down, previous, play/pause, next, volume up. The buttons act on touch like the CONTROLS buttons; play/pause shows the new state at once. The page does not open by itself (no "auto").
- Greek titles: an anti-aliased font with Greek, Latin-1 and typographic characters (`src/media_font.h`, 267 glyphs, 31 KB, DejaVu Sans 15 px) made by the new `tools/make_smooth_font.py` in TFT_eSPI's .vlw format. It is loaded only while the title is drawn (`SMOOTH_FONT` build flag added to `platformio.ini`). Long titles end with "...". The bridge keeps only characters the font has (`clean_media_text`).
- Footer: five tabs of 64 px; new music icon added by `tools/make_tab_icons.py` (the other four icons are byte-identical).
- Bridge: `MediaService` runs a separate worker process (`--media-worker`, like TOP APP, so the FFT cannot slow down the buttons). The worker records the default output device (WASAPI loopback, PyAudioWPatch), makes 24 logarithmic bands (40 Hz-16 kHz, 4096-point FFT, +3 dB/octave, automatic level, smooth fall) about 20 times a second, reads the song with `winrt-Windows.Media.Control` and the volume with `pycaw`. It records only while the MUSIC page is on screen and the screen is awake (new `page` frame from the display); it reopens the recording when the output device changes. Previous / play-pause / next are media keys (pynput). New `media` section in config.json (`enabled`, `fps` 5-30, `volume_step`), new `--test-media` (prints song, volume and text bars for a few seconds).
- Protocol: `spec`, `media` (PC -> ESP32), `page`, `media_key` (ESP32 -> PC); frames are now UTF-8 instead of `\u` escapes. Serial writes are serialised with a lock (main loop + music thread).
- First test on Windows: the worker hung while importing numpy when numpy was loaded after the winrt/pycaw libraries and the stdin thread (traceback from `faulthandler`). numpy is now imported first, with `OPENBLAS_NUM_THREADS=1`; then recording started in 0.1 s. `--test-media` shows each step with its time, the recording device, text bars with the loudest level in dB, and a traceback if something hangs for 12 s. The loopback is recorded before the Windows volume (bars stay full at 2 % volume).
- New Python packages (Windows only, in requirements.txt): numpy, PyAudioWPatch, pycaw, winrt-Windows.Media.Control, winrt-Windows.Foundation. Without them the page still works with what is available and the log says what is missing.
- Firmware 1.0.0, host bridge 1.0.0. 15 new tests (80 in total). Firmware checked with g++ against mock headers (both board variants); final build in PlatformIO.

## [0.9.8] - 2026-11-06 (custom build)

### Fixed

- Automatic renewal of the Claude Code login often failed ("automatic renewal failed", then "login expired" for 10-30 minutes). The log showed a clear pattern: all 19 attempts made 20 minutes before the expiry failed after the 45 s timeout, and every attempt made after the token had expired worked in ~5 s. Claude Code only renews an access token that has expired (or is about to), so starting it 20 minutes early did nothing. The bridge now keeps using the token until it expires, wakes up 10 s after the expiry and renews then (`renew_before_minutes` now defaults to 0). A failed attempt is retried after 2 minutes instead of 30 (2, 2, 5, 15, then 30 minutes after repeated failures).
- Host bridge 0.9.8 (firmware unchanged, 0.9.7). 3 new tests, one updated (65 in total).

## [0.9.7] - 2026-11-06 (custom build)

### Added

- Power menu: hold the Lock PC button for 1.5 s (2 s at first; shortened after testing) (it fills red from below, "HOLD" in the corner) to open a window over the darkened CONTROLS page with Shut down / Restart / Sleep / Cancel (style "B" chosen from mockups). Shut down and Restart count down 5 s with a large CANCEL button, then the firmware sends `{"type":"power","action":...}`; Sleep is sent at once. The menu closes by itself after 10 s; the "Shutting down..." window after a tap or 15 s. A short tap on Lock PC still locks the PC, now on release (a 120 ms grace covers touch drop-outs). After the menu opens, touches are ignored until the finger is lifted, so the finger that held the button cannot press a menu button.
- Bridge: `power_menu_button` (1-6, 0 = off; sent to the display as `power_button` in the config frame) and `power_menu_test_mode` (log only, do nothing). Windows commands: `shutdown.exe /s /t 0` and `/r /t 0` without `/f` (programs with unsaved work can still stop it; any `/t` above 0 implies `/f`), sleep through .NET `SetSuspendState('Suspend')` in a hidden PowerShell (`rundll32 powrprof.dll,SetSuspendState` hibernates when hibernation is on). Linux: `systemctl poweroff/reboot/suspend`; macOS: System Events / `pmset sleepnow`. Power frames are handled on the serial reader thread, like buttons.
- `tools/make_power_icons.py` -> `src/power_icons.h` (power, restart, moon line icons, 30x30 alpha).
- Firmware 0.9.7, host bridge 0.9.7. 7 new tests (62 in total). Firmware checked with g++ against mock headers (both board variants); final build in PlatformIO.

## [0.9.6] - 2026-11-06 (custom build)

### Changed

- TOP APP is computed in a separate worker process (`--top-apps-worker`, started by `TopAppsService` with `CREATE_NO_WINDOW`) and refreshed every 10 s instead of every 2 s (`top_apps_interval_seconds` in config.json, minimum 2). A second timing log showed that the process list took ~1.4 s of every 2 s cycle (psutil re-reads the whole system process list for each protected Windows process) and, while it ran, a button press handled on the reader thread took 180-550 ms instead of ~10 ms (Python runs one thread at a time). Every other stat took ~30 ms in total. The worker exits when the bridge is gone and is restarted after 30 s if it stops. The timing log was removed.
- Host bridge 0.9.6 (firmware unchanged, 0.9.4). 5 new tests (55 in total).

## [0.9.5] - 2026-11-06 (custom build)

### Fixed

- Buttons sometimes opened their program up to ~1.5 s late. A temporary timing log showed that the bridge spends ~1.4 s of every 2 s cycle collecting stats, and the same loop read the serial port, so a press that arrived meanwhile waited (two presses in that window even opened together). The serial port is now read on its own thread (`SerialReader`): button presses run immediately, every other line still goes to the main loop through a queue. A serial error in the reader is raised in the main loop, which reconnects as before. `log()` is now thread-safe. The timing log was removed.
- Host bridge 0.9.5 (firmware unchanged, 0.9.4). 5 new tests (50 in total).

## [0.9.4] - 2026-10-31 (custom build)

### Fixed

- Crash (or watchdog reset) when opening CONTROLS right after the board got power (USB plugged in, PC restart) — it looked like the screen flickering and returning to the previous page. Cause, from the core dump: `LoadProhibited` in `TFT_eSPI::textWidth` < `drawString` < `drawMacroButton`, address `0xcdbd519a`. TFT_eSPI 2.5.31 does not initialise `gfxFont` in its constructor, so the button sprite created with `new` started with leftover heap contents: garbage after a power-on (crash), but usually the valid pointer of the previous run after a warm reset (no crash), which is why it only happened after a power-on. The first `drawString(..., font 1)` for the button number then read a font through that pointer. The sprite now calls `setTextFont(1)` right after it is created. The bug was already in 0.9.0; the tab icons were not involved.
- Firmware 0.9.4 (host bridge unchanged, 0.9.3). Black box and crash report stay in place.

## [0.9.3] - 2026-10-31 (custom build)

### Added

- Crash details in the reset report: after a crash the framework stores a core dump in the `coredump` flash partition (enabled in the Arduino-ESP32 2.0.14 sdkconfig, partition present in `huge_app.csv`). At boot the firmware reads its summary (task, exception cause and address, PC, backtrace, ELF SHA prefix), erases it and adds it to `reset_report` as `crash`. Also reports `psram_now` / `psram_then` (psramFound) and free heap / largest free block. The bridge logs them as `ESP crash: ...` and `ESP memory: ...`.
- Finding so far: the reset happens while drawing the first CONTROLS button right after the board gets power (USB plugged in or PC restart), before the bridge connects. Firmware 0.9.0 (without the tab icons) does the same, so it is not caused by the icons.
- Firmware 0.9.3, host bridge 0.9.3.

## [0.9.2] - 2026-10-31 (custom build)

### Added

- Black box for unexpected resets: the firmware keeps its last 32 steps (frames received, touches, page and button drawing, footer icons, screen sleep/wake) in RTC memory, which survives a watchdog or crash reset. After every `config` frame it sends a `reset_report` frame with the reset reason and, after a watchdog/crash reset, those steps and the uptime before the reset. The bridge writes it to the log as `ESP last restart: ...`.
- Investigating: the board reset with `rst:0x8 (TG1WDT_SYS_RESET)` (interrupt watchdog) a few seconds after the bridge connected, with no crash message.
- Firmware 0.9.2, host bridge 0.9.2.

## [0.9.1] - 2026-10-31 (custom build)

### Changed

- Footer tabs show a full-colour icon instead of the text (MONITOR = monitor with pulse, DISKS = hard disk, WEATHER = sun behind cloud, CONTROLS = sliders). Icons are the user's files in `icons/`, trimmed of their outer glow and scaled to 26 px wide by `tools/make_tab_icons.py` into `src/tab_icons.h` (alpha + RGB565 per pixel).
- Selected tab: purple background (RGB 80, 30, 120) with a magenta border (RGB 200, 80, 255) instead of the cyan fill. Dividers next to the selected tab are left out.
- Tab touch areas are unchanged.
- Bridge: any line from the ESP32 that is not a protocol frame (boot messages, reset reason, crash output such as Guru Meditation / Backtrace) is written to the log as `ESP output: ...` (at most 60 lines per minute), to diagnose unexpected resets.
- `platformio.ini`: `monitor_filters = esp32_exception_decoder`, so the PlatformIO Serial Monitor shows crash backtraces as function names and lines.
- Firmware 0.9.1, host bridge 0.9.1.

## [0.9.0] - 2026-10-25 (custom build)

### Added

- New look for the CONTROLS page: dark blue gradient background, blue gradient buttons with a cyan border, a large faint blue icon behind each label and FreeSans labels in mixed case with a soft shadow. Buttons are composed in a sprite, so they draw without flicker. Falls back to the previous style if the sprite cannot be allocated.
- Button icons come from the user's image files in `icons/` and are converted by `tools/make_button_icons.py` into `src/button_icons.h` (alpha + luminance per pixel; the firmware tints them blue). Icons: `claude`, `android_studio`, `proton_vpn`, `powershell`, `task_manager`, `lock` (blue lock).
- `"icon"` per button in `host/config.json` and in the `config` frame; an empty or unknown name shows no icon.
- Firmware 0.9.0, host bridge 0.9.0.

## [0.8.2] - 2026-10-25 (custom build)

### Added

- `weather.model` setting to pick an Open-Meteo forecast model (`best_match` by default, e.g. `ecmwf_ifs025`, `icon_eu`, `meteofrance_seamless`).
- `--compare-weather` command: prints the current temperature, feels-like, today's max/min, rain probability, wind and humidity from nine models side by side for every configured place, to choose the one closest to reality.
- Naxos (NAXOS, 37.1036 / 25.3766) added to the weather places.
- Host bridge 0.8.2 (firmware unchanged).

## [0.8.1] - 2026-10-25 (custom build)

### Fixed

- CLAUDE box showed `LOGIN` after a few hours: the Claude Code access token lasts about 8 hours and is renewed only when Claude Code starts; `claude auth status` does not renew it. The bridge now renews the login automatically about 20 minutes before it expires (and right away at start-up if it already expired) by starting `claude` hidden in an empty, trusted folder (`~/claude-login` by default), waiting until the credentials file shows a new expiry and closing it again. No prompt is sent to Claude. At most one attempt every 30 minutes.
- New `claude_usage` options: `auto_renew`, `renew_folder`, `renew_before_minutes`, `renew_timeout_seconds`.
- Host bridge 0.8.1 (firmware unchanged, 0.8.0).

## [0.8.0] - 2026-10-25 (custom build)

### Added

- Screen sleep: when no data has arrived from the PC for `screen_sleep_minutes` (default 2; `0` = never), the display turns its backlight off. It wakes up as soon as the bridge sends data again (PC switched on) or for 30 seconds when the screen is tapped; a tap on the dark screen never presses a button. Useful when the motherboard keeps USB powered while the PC is off.
- `sleep_s` field in the `config` frame.
- Firmware 0.8.0, host bridge 0.8.0.

## [0.7.1] - 2026-10-25 (custom build)

### Fixed

- Weather page could stay on "WAITING FOR WEATHER": with stats, disks and Claude frames sent back to back, the ESP32's 256-byte serial receive buffer overflowed while the display was redrawing and the one-time weather frame was lost. The firmware now uses a 4 KB receive buffer, the bridge sends weather/Claude frames between stats bursts and repeats them every minute.
- The log no longer fills up when LibreHardwareMonitor's best "system" sensor alternates between two sensors.
- Firmware 0.7.1, host bridge 0.7.1.

## [0.7.0] - 2026-10-25 (custom build)

### Added

- **CLAUDE** box on the MONITOR page (between NETWORK and TOP APP): Claude plan usage of the 5-hour session (with reset time) and the weekly limit (with reset day), refreshed every 5 minutes.
- Uses the Claude Code login in `%USERPROFILE%\.claude\.credentials.json` and the same usage endpoint that Claude Code's `/usage` reads (`api.anthropic.com/api/oauth/usage`). **This endpoint is not an official public API and may change.** The token is read locally, sent only to api.anthropic.com and never logged.
- When the stored access token has expired, the bridge runs `claude auth status` (no model request) and re-reads the login; if it is still expired the box shows `LOGIN / run claude`.
- `claude_usage` section in `config.json` (`enabled`, `refresh_minutes`, optional `credentials_path`) and the `--test-claude-usage` command.
- `claude` frame in the USB protocol.

### Changed

- The bottom row of the MONITOR page now has three 96 px boxes: NETWORK, CLAUDE, TOP APP.
- Firmware 0.7.0, host bridge 0.7.0.

## [0.6.1] - 2026-10-25 (custom build)

### Changed

- MONITOR page: the DISK box (now redundant with the DISKS page) is replaced by **TOP PROCESS**: the program using the most CPU (name and % of the whole CPU) and the program using the most RAM. Processes are grouped by name, like the Task Manager app list.
- `stats` frame: new fields `top_cpu_name`, `top_cpu`, `top_ram_name`, `top_ram_gb`.
- Firmware 0.6.1, host bridge 0.6.1.

## [0.6.0] - 2026-10-25 (custom build)

### Added

- **DISKS** page (new footer tab: MONITOR | DISKS | WEATHER | CONTROLS) listing every local drive with its letter, volume label, usage bar, percentage and used/total size. Up to 16 drives, 8 per screen (tap the list for the next ones). Rows update in place without flicker.
- `disks` frame in the USB protocol, sent every 15 s when usage changes (at least once a minute).
- `disks` section in `config.json` (`include_removable`, `exclude`) and the `--list-disks` bridge command.

- Per-drive read/write speed under each drive on the DISKS page (Windows: drive letter mapped to its physical disk with `IOCTL_STORAGE_GET_DEVICE_NUMBER`, no admin rights needed; partitions on the same physical disk show the same speed). 5 drives per screen; the `disks` frame is now sent with every stats update.

### Changed

- Firmware 0.6.0, host bridge 0.6.0. Footer tabs shrink their font automatically when a label does not fit.

## [0.5.1] - 2026-10-25 (custom build)

### Added

- Support for the ESP32-2432S028 "Cheap Yellow Display" (CYD). New PlatformIO environments: `cyd2usb` (USB-C + micro USB, default) and `cyd` (micro USB only). The original Elecrow board stays available as `esp32_pc_control_deck`.
- Direct XPT2046 touch driver for the CYD's separate touch SPI pins (CLK 25, MISO 39, MOSI 32, CS 33) with a 3-point calibration that detects swapped/inverted axes. Stored in NVS as `cydcal`; `recalibrate` clears it.

### Fixed

- Backlight pin now follows `TFT_BL` from the build flags instead of a hard-coded GPIO 27 (the CYD uses GPIO 21; the screen stayed dark).
- One action per tap: holding a finger on a button no longer repeats it.
- Buttons for programs that need administrator rights (e.g. Task Manager) failed with `WinError 740`. The bridge now falls back to ShellExecute, which shows the normal UAC prompt.
- Weather requests failed on Windows with `CERTIFICATE_VERIFY_FAILED: certificate has expired` (outdated Windows root store). The bridge now uses the `certifi` CA bundle; `run_windows.bat` installs new or changed requirements automatically. Host bridge 0.5.1.

## [0.5.0] - 2026-10-25 (custom build)

### Added

- Weather page on the display (new middle tab **WEATHER**). The bridge fetches current conditions and today's high/low, rain probability, humidity and wind from the free Open-Meteo API (no API key) for up to six places configured in `host/config.json`. Three places are shown per screen; tap the list to see the next group.
- `weather` frame in the USB protocol (backwards compatible; protocol stays version 3).
- `app` macro action: starts an installed application from a list of candidate paths (environment variables and `*` wildcards allowed) and, on Windows, falls back to the Start menu name (`Get-StartApps` + `shell:AppsFolder`), which also works for Microsoft Store / MSIX apps.
- Bridge commands `--test-macro ID`, `--find-app NAME` and `--test-weather`.
- Greek place names are transliterated to Latin capitals for the display fonts.

### Changed

- Default buttons: Claude, Android Studio, Proton VPN, PowerShell, Task Manager, Lock PC.
- Default weather places: Athens (ATHINA) and Vrachati (VRACHATI).
- Footer now has three tabs: MONITOR, WEATHER, CONTROLS.
- Firmware 0.5.0, host bridge 0.5.0.

## 0.4.0 and earlier

Versions up to 0.4.0 belong to the original [ESP32 PC Control Deck](https://github.com/lepczynski-cloud/ESP32-PC-Control-Deck) by Wojciech Lepczynski; see its changelog. This custom build starts at 0.5.0.
