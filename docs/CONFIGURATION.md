# Configuration

Everything is set in your local, git-ignored `host/config.json` (created from `config.example.json` on first run). After a change, restart the bridge; the firmware does not need to be uploaded again.

## General

If several CH340 serial devices are connected, set the port manually:

```json
"serial_port": "COM7"
```

Screen sleep (`0` = never):

```json
"screen_sleep_minutes": 2
```

## Storage

```json
"disks": {"include_removable": true, "exclude": ["E:"]}
```

Check with `control_deck_bridge.py --list-disks`.

## Weather

```json
"weather": {
  "enabled": true,
  "refresh_minutes": 15,
  "locations": [
    {"name": "Αθήνα", "label": "ATHINA", "latitude": 37.9838, "longitude": 23.7275},
    {"name": "Βραχάτι", "label": "VRACHATI", "latitude": 37.95, "longitude": 22.8},
    {"name": "Νάξος", "label": "NAXOS", "latitude": 37.1036, "longitude": 25.3766}
  ]
}
```

Default places: Athens, Vrachati and Naxos. A place can be given with coordinates or just by `name` (e.g. `{"name": "Πάτρα"}`, looked up once with Open-Meteo geocoding; add `"country_code": "GR"` to narrow it). `label` is what the display shows; Greek names are transliterated automatically. Test with `control_deck_bridge.py --test-weather`. Compare forecast models with `control_deck_bridge.py --compare-weather` and pick one with `"model"` in the `weather` section.

## Buttons

The app buttons use the `app` action: the bridge tries the listed paths first and, if none exists, finds the program by its Windows Start menu name. If a button does not start its app:

```text
cd host
.venv\Scripts\python.exe control_deck_bridge.py --find-app "Claude"
.venv\Scripts\python.exe control_deck_bridge.py --test-macro 1
```

`--find-app` shows the exact Start menu name to put in `start_menu`; `--test-macro` runs a button without the display and logs the result. See [`MACROS.md`](MACROS.md) for every action type (`app`, `command`, `service`, `url`, `hotkey`, `text`).

## Power menu

```json
"power_menu_button": 8,
"power_menu_test_mode": false
```

`power_menu_button` is the button (1-8) that opens the menu, `0` = off. With `"power_menu_test_mode": true` the bridge only writes the request to its log.

## Music

```json
"media": {"enabled": true, "fps": 20, "volume_step": 5, "effect": 1}
```

`effect` (1-10) sets the spectrum effect when that value changes; a tap on the bars still picks another one. Test with `control_deck_bridge.py --test-media` while music is playing.
