# USB serial protocol

ESP32 PC Control Deck v0.3 uses a small line-oriented protocol through the board's CH340 USB serial connection.

## Framing

Every application frame starts with `DD:` and ends with a newline:

```text
DD:{"type":"stats","cpu":18.2,"ram":61.7}\n
```

The prefix lets both sides ignore ESP32 ROM boot messages and unrelated serial output instead of attempting to parse them as JSON.

Default serial settings:

- 115200 baud
- 8 data bits
- no parity
- 1 stop bit

Protocol version: `3`.

The firmware still accepts a valid raw JSON line from earlier prototypes, but new clients should always use the `DD:` prefix.

## PC to ESP32

### `stats`

```json
{
  "type": "stats",
  "cpu": 18.2,
  "ram": 61.7,
  "ram_used_gb": 19.7,
  "ram_total_gb": 32.0,
  "gpu": 35.0,
  "vram": 48.0,
  "vram_used_gb": 5.8,
  "vram_total_gb": 12.0,
  "cpu_temp": 57.0,
  "gpu_temp": 52.0,
  "system_temp": 34.0,
  "disk_temp": 41.0,
  "disk_used": 71.0,
  "disk_read_mbs": 4.2,
  "disk_write_mbs": 1.3,
  "net_down_mbps": 23.4,
  "net_up_mbps": 2.1,
  "clock": "19:05",
  "clock_seconds": 68700,
  "top_cpu_name": "chrome",
  "top_cpu": 12.5,
  "top_ram_name": "Code",
  "top_ram_gb": 2.1
}
```

Unavailable sensor values are sent as JSON `null`. `top_cpu` is the share of the whole CPU used by the busiest program (processes grouped by name); `top_ram_*` is the program with the largest working set.

`clock_seconds` is the local number of seconds since midnight on the PC. The ESP32 stores the value and advances it locally using `millis()`. The older `clock` string remains as a fallback.

### `config`

Updates the eight touch-button labels (six before firmware 1.2.0):

```json
{
  "type": "config",
  "macros": [
    {"id": 1, "label": "ChatGPT"},
    {"id": 2, "label": "Ollama"},
    {"id": 3, "label": "Voice Server"}
  ]
}
```

`sleep_s` (optional) sets after how many seconds without data the display turns its backlight off (`0` = never).

`power_button` (optional, firmware 0.9.7+) is the button (1-8 since firmware 1.2.0, 1-6 before) whose long press (1.5 s) opens the power menu; `0` = off.

`music_effect` (optional, firmware 1.1.0+) is the MUSIC page effect (1-10, from `media.effect` in config.json). The display applies it only when the value differs from the last one it received, so an effect picked with a tap on the bars stays until `media.effect` is changed.

`icon` (optional) names a built-in button icon (see `MACROS.md`). Labels can contain up to 22 characters. The firmware wraps longer labels onto two lines.

### `weather`

Sent when new weather data is available (every `refresh_minutes`) and after the device connects:

```json
{
  "type": "weather",
  "updated": "19:05",
  "locations": [
    {"name": "ATHINA", "temp": 23.4, "feels": 23, "code": 2, "day": 1,
     "hum": 61, "wind": 13, "tmax": 27, "tmin": 18, "rain": 20}
  ]
}
```

Up to six locations. `code` is the WMO weather code returned by Open-Meteo, `day` is 1 during daylight, `wind` is km/h and `rain` is today's maximum precipitation probability in percent. Names are ASCII (max 14 characters).

### `disks`

Usage of every local drive (fixed and removable; CD/DVD and network drives are skipped):

```json
{
  "type": "disks",
  "items": [
    {"name": "C:", "label": "SYSTEM", "used": 44.3, "used_gb": 412.0, "total_gb": 930.5,
     "r": 12.4, "w": 0.3}
  ]
}
```

Sent with every `stats` frame. `r` / `w` are read/write MB/s of the physical disk that holds the drive (`null` when unknown). Up to 16 drives. Sizes are in GiB (as shown by Windows Explorer); the display switches to TB above 1000 GB. Labels are ASCII, max 10 characters.

### `claude`

Claude plan usage, sent when it changes (polled every `refresh_minutes`):

```json
{"type": "claude", "state": "ok", "s": 42, "s_reset": "15:30", "w": 18, "w_reset": "TUE"}
```

`s` / `w` are the 5-hour session and weekly utilisation in percent, `s_reset` is the local reset time of the session and `w_reset` the weekday of the weekly reset. `state` is `ok`, `login` (Claude Code login expired), `nologin` (no Claude Code login on the PC) or `error`.

### `spec`

Firmware 1.0.0+, MUSIC page. Sound spectrum, about 20 frames per second while the MUSIC page is on screen (see `page` below):

```json
{"type":"spec","b":"9DHKLJGFDB98765432100000","w":"FHJLMNNMLJHFDBA9..."}
```

`b` has one base-32 digit per bar (`0`-`9`, `A`-`V` = 0..31), 24 bars from low to high notes (40 Hz to 16 kHz, logarithmic). When no frame arrives for 0.4 s the bars fall to zero on their own.

`w` (bridge 1.1.0+, for the oscilloscope effect) has 64 digits `0`-`9`, `A`-`U` (0..30) of the waveform; `F` (15) is silence. It starts at a rising zero crossing so a steady tone stands still, with automatic gain. Without `w` the oscilloscope shows a flat line.

### `media`

Firmware 1.0.0+. The current song (Windows media controls) and the system volume, sent when something changes, every 10 s and after the device connects:

```json
{"type":"media","state":"playing","title":"Συννεφιασμένη Κυριακή","artist":"Βασίλης Τσιτσάνης","vol":45,"mute":false}
```

`state` is `playing`, `paused`, `stopped`, `none` (no player) or `unavailable` (the bridge cannot read song info). `vol` is the system volume in percent (`-1` = unknown). Title and artist are UTF-8 (frames are UTF-8 since bridge 1.0.0), at most 60 characters, and contain only characters of the display font (`src/media_font.h`: ASCII, Latin-1, Greek, dashes, quotes, ellipsis); the bridge replaces or drops the others.

### `ping`

The ESP32 replies with `pong`.

### `recalibrate`

Deletes the saved touch calibration and restarts the ESP32.

## ESP32 to PC

### `hello`

Sent after boot:

```json
{
  "type": "hello",
  "device": "ESP32 PC Control Deck",
  "firmware": "0.3.3",
  "protocol": 3
}
```

### `macro`

Sent when a control button is pressed:

```json
{"type":"macro","id":3}
```

The ESP32 sends only the numeric ID. The trusted action remains in the local `host/config.json` file.

### `power`

Sent by firmware 0.9.7+ from the power menu (after the 5 s countdown for `shutdown` and `restart`, at once for `sleep`):

```json
{"type":"power","action":"shutdown"}
```

`action` is `shutdown`, `restart` or `sleep`. The bridge acts only when `power_menu_button` is set in `host/config.json`, and only logs the request when `power_menu_test_mode` is `true`.

### `page`

Firmware 1.0.0+. Sent when the page changes, when the screen goes to sleep or wakes up, and after every `config` frame:

```json
{"type":"page","page":"music","awake":true}
```

`page` is `monitor`, `disks`, `weather`, `controls` or `music`. The bridge records sound and sends `spec` frames only while `page` is `music` and `awake` is `true`.

### `media_key`

Firmware 1.0.0+, a button on the MUSIC page:

```json
{"type":"media_key","key":"play_pause"}
```

`key` is `prev`, `play_pause`, `next` (sent as media keys) or `vol_down` / `vol_up` (system volume in steps of `media.volume_step`, default 5%).

### `reset_report`

Sent after every `config` frame (the bridge sends one whenever it connects). Says why the firmware last restarted. After a watchdog or crash reset it also lists the last steps the firmware went through before the reset (kept in RTC memory), oldest first, and how long it had been running:

```json
{
  "type": "reset_report",
  "reason": "interrupt watchdog",
  "code": 5,
  "uptime_s": 123,
  "trail": ["touch", "tab:CONTROLS", "render:CONTROLS", "button:1", "button_done:1"]
}
```

`trail` and `uptime_s` are missing after a power-on reset. Since firmware 0.9.3 the report also has `psram_now`, `psram_then`, `heap_free`, `heap_largest` and, after a crash, a `crash` object read from the core dump (`task`, `pc`, `cause`, `vaddr`, `elf`, `bt_corrupted`, `bt`). The bridge writes the report to its log.

### `error`

Used for malformed framed JSON. Unrelated serial text is ignored without returning an error.
