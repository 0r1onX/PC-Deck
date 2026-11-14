#!/usr/bin/env python3
"""ESP32 PC Control Deck host bridge.

The bridge collects local PC telemetry and sends it to an Elecrow 2.8-inch
ESP32 display through the board's CH340 USB serial connection. It also maps
six touch buttons to locally configured actions.

Customised build: adds an "app" macro action (launch an installed Windows
application by path or by its Start menu name) and a weather page fed by the
free Open-Meteo API (no API key). The MUSIC page (Windows) shows a spectrum
of the sound the PC plays, the current song and the volume.

No Wi-Fi on the ESP32, Bluetooth, cloud account, API key, or browser
credential is needed.
"""
from __future__ import annotations

import argparse
import atexit
import glob
import json
import math
import os
import platform
import queue
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import psutil
import serial
from serial.tools import list_ports

try:
    import certifi
except Exception:
    certifi = None

try:
    from pynput.keyboard import Controller as KeyboardController, Key, KeyCode
except Exception:
    KeyboardController = None
    Key = None
    KeyCode = None

try:
    # NVIDIA's NVML bindings (pip package "nvidia-ml-py"). Reading the GPU this
    # way starts no nvidia-smi.exe process, so Windows shutdown can no longer
    # catch one half-started (Application Error 0xc0000142).
    import pynvml
except Exception:
    pynvml = None

APP_VERSION = "1.2.0"
PROTOCOL_PREFIX = "DD:"
PROTOCOL_VERSION = 3
CH340_VIDS = {0x1A86}
LOG_FILE: Path | None = None


_LOG_LOCK = threading.Lock()


def log(message: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
    # Called from the main loop and from the serial reader thread.
    with _LOG_LOCK:
        print(line, flush=True)
        if LOG_FILE is not None:
            try:
                with LOG_FILE.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                pass


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("The configuration root must be a JSON object")
    return data


def platform_key() -> str:
    return {
        "Windows": "windows",
        "Linux": "linux",
        "Darwin": "macos",
    }.get(platform.system(), "default")


def resolve_platform_value(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    return value.get(platform_key(), value.get("default"))


def expand_text(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return os.path.expanduser(os.path.expandvars(value))


def _port_text(port: Any) -> str:
    values = (
        getattr(port, "description", ""),
        getattr(port, "manufacturer", ""),
        getattr(port, "product", ""),
        getattr(port, "interface", ""),
        getattr(port, "hwid", ""),
    )
    return " ".join(str(value or "") for value in values).upper()


def _choose_windows_serial_port(ports: list[Any]) -> str | None:
    """Keep the existing Windows auto-detection behavior unchanged."""
    preferred: list[str] = []
    for port in ports:
        description = (getattr(port, "description", "") or "").upper()
        manufacturer = (getattr(port, "manufacturer", "") or "").upper()
        if (
            getattr(port, "vid", None) in CH340_VIDS
            or "CH340" in description
            or "CH340" in manufacturer
            or "USB-SERIAL" in description
        ):
            preferred.append(str(port.device))

    if len(preferred) == 1:
        return preferred[0]
    if preferred:
        log("Multiple CH340-like serial ports found: " + ", ".join(preferred))
        return preferred[0]
    return None


def _posix_serial_score(port: Any, system: str) -> int:
    """Score likely Control Deck ports on Linux and macOS."""
    device = str(getattr(port, "device", "") or "")
    device_lower = device.lower()
    text = _port_text(port)
    score = 0

    if getattr(port, "vid", None) in CH340_VIDS:
        score += 120
    if "CH340" in text or "CH341" in text:
        score += 100
    if "QINHENG" in text or "WCH" in text:
        score += 80
    if any(token in text for token in ("USB-SERIAL", "USB SERIAL", "USB2.0-SERIAL")):
        score += 60

    if system == "Darwin":
        if "wchusbserial" in device_lower:
            score += 80
        elif "usbserial" in device_lower:
            score += 50
        if score > 0:
            if device_lower.startswith("/dev/cu."):
                score += 35
            elif device_lower.startswith("/dev/tty."):
                score += 5
    elif system == "Linux":
        if device_lower.startswith("/dev/ttyusb"):
            score += 60
        elif device_lower.startswith("/dev/ttyacm"):
            score += 20

    return score


def choose_serial_port(configured: str, ports: list[Any] | None = None) -> str | None:
    if configured and configured.lower() != "auto":
        return configured

    ports = list(list_ports.comports()) if ports is None else list(ports)
    if not ports:
        return None

    system = platform.system()
    if system == "Windows":
        return _choose_windows_serial_port(ports)

    scored = [
        (_posix_serial_score(port, system), str(port.device))
        for port in ports
    ]
    candidates = sorted(
        ((score, device) for score, device in scored if score > 0),
        key=lambda item: (-item[0], item[1]),
    )
    if not candidates:
        return None

    best_score = candidates[0][0]
    best_devices = [device for score, device in candidates if score == best_score]
    if len(candidates) > 1:
        log(
            "Serial candidates: "
            + ", ".join(f"{device} ({score})" for score, device in candidates)
        )
    return best_devices[0]


def _format_vid_pid(port: Any) -> str:
    vid = getattr(port, "vid", None)
    pid = getattr(port, "pid", None)
    if isinstance(vid, int) and isinstance(pid, int):
        return f"{vid:04X}:{pid:04X}"
    return "unknown"


def print_serial_ports(configured: str) -> int:
    ports = list(list_ports.comports())
    if not ports:
        print("No serial ports are currently visible.")
        if platform.system() == "Linux":
            print("Connect the board and check USB permissions for /dev/ttyUSB*.")
        return 1

    print("Visible serial ports:\n")
    for port in ports:
        description = str(getattr(port, "description", "") or "unknown")
        manufacturer = str(getattr(port, "manufacturer", "") or "unknown")
        print(f"- {port.device}")
        print(f"  description: {description}")
        print(f"  manufacturer: {manufacturer}")
        print(f"  VID:PID: {_format_vid_pid(port)}")

    selected = choose_serial_port(configured, ports)
    print()
    if selected:
        print(f"Selected port: {selected}")
        return 0

    print("No CH340-like Control Deck port was selected automatically.")
    print("Set serial_port manually in host/config.json.")
    return 1


def safe_number(value: float | None, digits: int = 2) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
        if math.isnan(numeric) or math.isinf(numeric):
            return None
    except (TypeError, ValueError):
        return None
    return round(numeric, digits)


def disk_path_from_config(config: dict[str, Any]) -> str:
    configured = str(config.get("disk_path", "auto"))
    if configured.lower() != "auto":
        return configured
    if platform.system() == "Windows":
        return os.environ.get("SystemDrive", "C:") + "\\"
    return "/"


def read_cpu_temp_psutil() -> float | None:
    """Read a CPU temperature where psutil exposes sensors (mainly POSIX)."""
    function = getattr(psutil, "sensors_temperatures", None)
    if function is None:
        return None
    try:
        sensors = function(fahrenheit=False) or {}
    except Exception:
        return None

    preferred: list[float] = []
    fallback: list[float] = []
    for entries in sensors.values():
        for entry in entries:
            current = getattr(entry, "current", None)
            if current is None:
                continue
            try:
                current = float(current)
            except (TypeError, ValueError):
                continue
            if not 0 < current < 130:
                continue
            label = (getattr(entry, "label", "") or "").lower()
            if any(token in label for token in ("package", "tctl", "tdie", "cpu")):
                preferred.append(current)
            else:
                fallback.append(current)
    values = preferred or fallback
    return max(values) if values else None


def parse_numeric(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return safe_number(float(value), 2)
    if value is None:
        return None
    match = re.search(r"-?\d+(?:[.,]\d+)?", str(value))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", "."))
    except ValueError:
        return None


@dataclass(frozen=True)
class TemperatureSensor:
    value: float
    label: str
    sensor_id: str
    context: str


class LibreHardwareMonitorHttpReader:
    """Read temperatures from LibreHardwareMonitor's local data.json tree."""

    def __init__(self, enabled: bool, url: str, timeout: float = 0.5):
        self.enabled = enabled
        self.url = url
        self.timeout = timeout
        self._reachable: bool | None = None
        self._logged_sources: dict[str, str] = {}
        self._logged_identities: dict[str, set[str]] = {}

    @staticmethod
    def _walk(node: Any, path: tuple[str, ...] = ()) -> Iterable[tuple[dict[str, Any], tuple[str, ...]]]:
        if not isinstance(node, dict):
            return
        text = str(node.get("Text", "") or "")
        next_path = path + ((text,) if text else ())
        yield node, next_path
        for child in node.get("Children") or []:
            yield from LibreHardwareMonitorHttpReader._walk(child, next_path)

    @staticmethod
    def _is_temperature_node(node: dict[str, Any], context: str) -> bool:
        node_type = str(node.get("Type", "") or "").lower()
        sensor_id = str(node.get("SensorId", "") or "").lower()
        text = str(node.get("Text", "") or "").lower()
        return (
            node_type == "temperature"
            or "/temperature/" in sensor_id
            or "temperatures" in context
            or "temperature" in text
        )

    @staticmethod
    def _temperature_sensors(data: Any) -> list[TemperatureSensor]:
        sensors: list[TemperatureSensor] = []
        for node, path in LibreHardwareMonitorHttpReader._walk(data):
            if "Value" not in node:
                continue
            context = " ".join(path).lower()
            if not LibreHardwareMonitorHttpReader._is_temperature_node(node, context):
                continue

            value = parse_numeric(node.get("Value"))
            if value is None or not 0 < value < 130:
                continue

            sensors.append(
                TemperatureSensor(
                    value=value,
                    label=str(node.get("Text", "Temperature") or "Temperature"),
                    sensor_id=str(node.get("SensorId", "") or ""),
                    context=" > ".join(path),
                )
            )
        return sensors

    @staticmethod
    def _score(sensor: TemperatureSensor, kind: str) -> int | None:
        sensor_id = sensor.sensor_id.lower()
        label = sensor.label.lower()
        context = sensor.context.lower()
        haystack = " ".join((sensor_id, label, context))

        if kind == "cpu":
            identity = any(
                token in haystack
                for token in (
                    "amdcpu",
                    "intelcpu",
                    "/cpu/",
                    "amd ryzen",
                    "intel core",
                    "processor",
                )
            )
            if not identity:
                return None
            score = 20
            if "cpu package" in label or "package" in label:
                score += 100
            elif "tctl" in label or "tdie" in label:
                score += 90
            elif "core average" in label:
                score += 80
            elif "core max" in label or "maximum" in label:
                score += 60
            elif "core" in label:
                score += 20
            return score

        if kind == "gpu":
            if not any(
                token in haystack
                for token in ("nvidiagpu", "atigpu", "intelgpu", "/gpu/", "graphics")
            ):
                return None
            score = 20
            if "gpu core" in label or label == "core":
                score += 70
            if "gpu package" in label or "package" in label:
                score += 60
            if "hot spot" in label or "hotspot" in label:
                score += 30
            return score

        if kind == "disk":
            if not any(
                token in haystack
                for token in ("/hdd/", "/storage/", "nvme", "ssd", "disk", "drive")
            ):
                return None
            score = 20
            if "composite" in label:
                score += 60
            if label in {"temperature", "drive temperature"}:
                score += 50
            return score

        if kind == "system":
            if not any(
                token in haystack
                for token in ("/lpc/", "motherboard", "mainboard", "super i/o", "superio", "chipset")
            ):
                return None
            score = 10
            if "motherboard" in label or "mainboard" in label or "system" in label:
                score += 60
            if "ambient" in label:
                score += 45
            if "pch" in label or "chipset" in label:
                score += 25
            if "vrm" in label:
                score += 15
            return score

        return None

    @classmethod
    def _select(cls, sensors: list[TemperatureSensor], kind: str) -> TemperatureSensor | None:
        candidates: list[tuple[int, TemperatureSensor]] = []
        for sensor in sensors:
            score = cls._score(sensor, kind)
            if score is not None:
                candidates.append((score, sensor))
        if not candidates:
            return None
        best_score = max(score for score, _ in candidates)
        best = [sensor for score, sensor in candidates if score == best_score]
        return max(best, key=lambda item: item.value)

    def _fetch(self) -> Any:
        request = urllib.request.Request(
            self.url,
            headers={"User-Agent": f"ESP32-PC-Control-Deck/{APP_VERSION}"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))

    def _log_source(self, kind: str, sensor: TemperatureSensor | None) -> None:
        identity = "none" if sensor is None else f"{sensor.label} [{sensor.sensor_id}]"
        seen = self._logged_identities.setdefault(kind, set())
        if identity in seen:
            return  # avoid flooding the log when two sensors alternate
        seen.add(identity)
        self._logged_sources[kind] = identity
        if sensor is not None:
            log(f"Temperature source {kind}: {identity}")

    def read(self) -> dict[str, float | None]:
        result: dict[str, float | None] = {
            "cpu_temp": None,
            "gpu_temp": None,
            "disk_temp": None,
            "system_temp": None,
        }
        if not self.enabled:
            return result

        try:
            data = self._fetch()
            if self._reachable is not True:
                log("LibreHardwareMonitor web server detected")
            self._reachable = True

            sensors = self._temperature_sensors(data)
            for kind, result_key in (
                ("cpu", "cpu_temp"),
                ("gpu", "gpu_temp"),
                ("disk", "disk_temp"),
                ("system", "system_temp"),
            ):
                selected = self._select(sensors, kind)
                self._log_source(kind, selected)
                if selected is not None:
                    result[result_key] = selected.value
        except (OSError, urllib.error.URLError, json.JSONDecodeError, TimeoutError):
            if self._reachable is None:
                if platform.system() == "Windows":
                    log(
                        "Additional temperatures unavailable. On Windows, run "
                        "LibreHardwareMonitor and enable Remote Web Server > Run."
                    )
                else:
                    log(f"Configured temperature endpoint is unavailable: {self.url}")
            self._reachable = False
        except Exception as exc:
            if self._reachable is not False:
                log(f"LibreHardwareMonitor read error: {exc}")
            self._reachable = False
        return result

    def print_temperature_sensors(self) -> int:
        if not self.enabled:
            print("LibreHardwareMonitor integration is disabled in config.json")
            return 2
        try:
            sensors = self._temperature_sensors(self._fetch())
        except Exception as exc:
            print(f"Could not read {self.url}: {exc}", file=sys.stderr)
            return 2

        if not sensors:
            print("No temperature sensors were found in the LibreHardwareMonitor JSON tree.")
            return 1

        print(f"Temperature sensors from {self.url}:\n")
        for sensor in sensors:
            print(f"{sensor.value:6.1f} C  {sensor.label}")
            print(f"          id: {sensor.sensor_id or '(not provided)'}")
            print(f"        path: {sensor.context}\n")
        return 0


NVML_RETRY_SECONDS = 30.0
_NVML_LOCK = threading.Lock()
_nvml_handle: Any = None
_nvml_retry_at = 0.0
_gpu_source_logged = False


def _empty_gpu_result() -> dict[str, float | None]:
    return {
        "gpu": None,
        "vram": None,
        "gpu_temp": None,
        "vram_used_gb": None,
        "vram_total_gb": None,
    }


def _fill_gpu_result(
    result: dict[str, float | None], gpu: float, used_mib: float, total_mib: float, temperature: float
) -> None:
    result["gpu"] = gpu
    result["vram"] = used_mib * 100.0 / total_mib if total_mib > 0 else None
    result["gpu_temp"] = temperature
    result["vram_used_gb"] = used_mib / 1024.0
    result["vram_total_gb"] = total_mib / 1024.0


def read_nvidia_gpu() -> dict[str, float | None]:
    """First NVIDIA GPU: NVML when nvidia-ml-py is installed, else nvidia-smi."""
    global _gpu_source_logged
    if not _gpu_source_logged:
        _gpu_source_logged = True
        if pynvml is not None:
            log("GPU stats: NVML (no nvidia-smi process)")
        else:
            log("GPU stats: nvidia-smi (install nvidia-ml-py to avoid it)")
    if pynvml is not None:
        return read_nvidia_gpu_nvml()
    return read_nvidia_gpu_smi()


def _nvml_device() -> Any:
    """Initialise NVML once and return GPU 0. The caller holds _NVML_LOCK."""
    global _nvml_handle, _nvml_retry_at
    if _nvml_handle is not None:
        return _nvml_handle
    now = time.monotonic()
    if now < _nvml_retry_at:
        return None
    _nvml_retry_at = now + NVML_RETRY_SECONDS
    try:
        pynvml.nvmlInit()
    except Exception:
        return None
    try:
        _nvml_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
    except Exception:
        _nvml_handle = None
        try:
            pynvml.nvmlShutdown()
        except Exception:
            pass
    return _nvml_handle


def read_nvidia_gpu_nvml() -> dict[str, float | None]:
    global _nvml_handle
    result = _empty_gpu_result()
    with _NVML_LOCK:
        handle = _nvml_device()
        if handle is None:
            return result
        try:
            gpu = float(pynvml.nvmlDeviceGetUtilizationRates(handle).gpu)
            try:
                # v2 reports "used" the same way current nvidia-smi does.
                memory = pynvml.nvmlDeviceGetMemoryInfo(handle, version=pynvml.nvmlMemory_v2)
            except Exception:
                memory = pynvml.nvmlDeviceGetMemoryInfo(handle)
            temperature = float(pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU))
        except Exception:
            # E.g. after a driver update: look the GPU up again on a later call.
            _nvml_handle = None
            return result
    _fill_gpu_result(result, gpu, memory.used / 1048576.0, memory.total / 1048576.0, temperature)
    return result


def read_nvidia_gpu_smi() -> dict[str, float | None]:
    result = _empty_gpu_result()
    executable = shutil.which("nvidia-smi")
    if not executable:
        return result

    command = [
        executable,
        "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
        "--format=csv,noheader,nounits",
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0,
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            return result

        fields = [item.strip() for item in completed.stdout.strip().splitlines()[0].split(",")]
        if len(fields) < 4:
            return result

        _fill_gpu_result(result, float(fields[0]), float(fields[1]), float(fields[2]), float(fields[3]))
    except Exception:
        pass
    return result


def process_display_name(name: str, max_length: int = 18) -> str:
    """ASCII-only process name without the .exe suffix."""
    text = re.sub(r"\.exe$", "", str(name or ""), flags=re.IGNORECASE)
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if 32 <= ord(ch) < 127).strip()
    return text[:max_length] or "?"


class ProcessMonitor:
    """Finds the programs using the most CPU and RAM.

    Processes are grouped by name (all chrome.exe processes count as one),
    like the Task Manager app list. CPU is a share of the whole CPU (0-100).
    """

    IGNORED = {"system idle process", "idle", "memory compression", "registry"}

    def __init__(self) -> None:
        self.cpu_count = max(1, psutil.cpu_count() or 1)

    def sample(self) -> dict[str, Any]:
        cpu_by_name: dict[str, float] = {}
        ram_by_name: dict[str, float] = {}
        try:
            processes = list(psutil.process_iter(["name", "memory_info"]))
        except Exception:
            processes = []
        for process in processes:
            try:
                name = str((process.info or {}).get("name") or "")
                if not name or name.lower() in self.IGNORED or process.pid == 0:
                    continue
                cpu = float(process.cpu_percent(None) or 0.0) / self.cpu_count
                memory = (process.info or {}).get("memory_info")
                rss = float(getattr(memory, "rss", 0) or 0)
            except Exception:
                continue
            cpu_by_name[name] = cpu_by_name.get(name, 0.0) + cpu
            ram_by_name[name] = ram_by_name.get(name, 0.0) + rss

        result: dict[str, Any] = {
            "top_cpu_name": None,
            "top_cpu": None,
            "top_ram_name": None,
            "top_ram_gb": None,
        }
        if cpu_by_name:
            name = max(cpu_by_name, key=lambda key: cpu_by_name[key])
            result["top_cpu_name"] = process_display_name(name)
            result["top_cpu"] = safe_number(min(100.0, cpu_by_name[name]), 1)
        if ram_by_name:
            name = max(ram_by_name, key=lambda key: ram_by_name[key])
            result["top_ram_name"] = process_display_name(name)
            result["top_ram_gb"] = safe_number(ram_by_name[name] / 1024.0 ** 3, 2)
        return result


def run_top_apps_worker(interval: float, parent_pid: int) -> None:
    """Child process: samples the top programs and prints one JSON line each time.

    Runs in its own process so that its heavy psutil work (~1.4 s per sample on
    Windows, measured) never holds up the bridge, where it slowed down button
    presses. Exits when the bridge is gone or its pipe is closed.
    """
    monitor = ProcessMonitor()
    monitor.sample()  # first call only primes the CPU counters
    time.sleep(min(2.0, interval))
    while True:
        if parent_pid and not psutil.pid_exists(parent_pid):
            return
        result = monitor.sample()
        try:
            sys.stdout.write(json.dumps(result) + "\n")
            sys.stdout.flush()
        except (OSError, ValueError, AttributeError):
            return
        time.sleep(interval)


class TopAppsService:
    """TOP APP values from a separate worker process (see run_top_apps_worker)."""

    RESTART_DELAY_SECONDS = 30.0

    def __init__(self, config: dict[str, Any], command: list[str] | None = None) -> None:
        try:
            interval = float(config.get("top_apps_interval_seconds", 10))
        except (TypeError, ValueError):
            interval = 10.0
        self.interval = max(2.0, interval)
        self._command = command
        self._latest: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._process: subprocess.Popen[Any] | None = None
        self._stopping = False

    def default_command(self) -> list[str]:
        executable = Path(sys.executable)
        if platform.system() == "Windows":
            # python.exe (not pythonw.exe) so the worker surely has a stdout;
            # CREATE_NO_WINDOW keeps it without a console window.
            console = executable.with_name("python.exe")
            if console.is_file():
                executable = console
        return [
            str(executable),
            str(Path(__file__).resolve()),
            "--top-apps-worker",
            str(self.interval),
            "--parent-pid",
            str(os.getpid()),
        ]

    def start(self) -> None:
        atexit.register(self.stop)
        threading.Thread(target=self._run, name="top-apps", daemon=True).start()

    def stop(self) -> None:
        self._stopping = True
        process = self._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    def latest(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._latest)

    def _handle_line(self, line: str) -> None:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return
        if isinstance(data, dict):
            with self._lock:
                self._latest = data

    def _run(self) -> None:
        while not self._stopping:
            command = self._command or self.default_command()
            creationflags = 0
            if platform.system() == "Windows":
                creationflags = subprocess.CREATE_NO_WINDOW
            try:
                self._process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    encoding="ascii",
                    errors="replace",
                    creationflags=creationflags,
                )
            except OSError as exc:
                log(f"Top apps worker could not start: {exc}")
            else:
                log(f"Top apps worker started (PID {self._process.pid}, every {self.interval:g} s)")
                assert self._process.stdout is not None
                for line in self._process.stdout:
                    self._handle_line(line)
                code = self._process.wait()
                if self._stopping:
                    return
                log(f"Top apps worker stopped (exit code {code})")
            time.sleep(self.RESTART_DELAY_SECONDS)



# ------------------------------------------------------------------- music
# MUSIC page: spectrum of the sound the PC plays (WASAPI loopback + FFT),
# song title / artist / play state from the Windows media controls and the
# system volume. All of it runs in a separate worker process (like TOP APP),
# so the FFT never slows down button presses. Windows only.
MEDIA_BANDS = 24
MEDIA_MAX_LEVEL = 31
MEDIA_DIGITS = "0123456789ABCDEFGHIJKLMNOPQRSTUV"  # one base-32 digit per bar
MEDIA_FFT_SIZE = 4096
# Oscilloscope effect: 64 points of the waveform, one digit each (0..30,
# 15 = silence), from WAVE_SPAN samples that start at a rising zero crossing
# so the picture stands still for a steady tone.
MEDIA_WAVE_POINTS = 64
MEDIA_WAVE_SPAN = 1024
MEDIA_WAVE_CENTRE = 15
MEDIA_WAVE_SILENT = MEDIA_DIGITS[MEDIA_WAVE_CENTRE] * MEDIA_WAVE_POINTS
MEDIA_EFFECTS = 10  # effects on the MUSIC page (chosen on the display)
MEDIA_KEYS = {"prev": "media_previous", "next": "media_next", "play_pause": "media_play_pause"}
MEDIA_TEXT_MAX_CHARS = 60


def media_font_codes() -> list[int]:
    """Characters in src/media_font.h (keep in step with tools/make_smooth_font.py)."""
    codes = list(range(0x21, 0x7F))
    codes += range(0xA1, 0x100)
    codes += [0x0384, 0x0385, 0x0386, 0x0388, 0x0389, 0x038A, 0x038C]
    codes += range(0x038E, 0x03A2)
    codes += range(0x03A3, 0x03CF)
    codes += [0x2013, 0x2014, 0x2018, 0x2019, 0x201C, 0x201D, 0x2022, 0x2026]
    return sorted(set(codes) - {0xAD})


MEDIA_FONT_CHARS = frozenset(chr(code) for code in media_font_codes())


def clean_media_text(text: Any, max_chars: int = MEDIA_TEXT_MAX_CHARS) -> str:
    """Keeps only characters the display font has (Greek, Latin, punctuation).

    Others are replaced by their plain letter when there is one (e.g. a Latin
    letter with an unusual accent) or dropped (emoji, other scripts).
    """
    result: list[str] = []
    for ch in unicodedata.normalize("NFC", str(text or "")):
        if ch in MEDIA_FONT_CHARS:
            result.append(ch)
        elif ch.isspace():
            result.append(" ")
        else:
            plain = unicodedata.normalize("NFKD", ch)
            result.append("".join(c for c in plain if c in MEDIA_FONT_CHARS or c == " "))
    cleaned = " ".join("".join(result).split())
    return cleaned[:max_chars].rstrip()


def media_band_edges(rate: float, size: int = MEDIA_FFT_SIZE, bands: int = MEDIA_BANDS,
                     low: float = 40.0, high: float = 16000.0) -> list[tuple[int, int]]:
    """FFT bin ranges [start, end) of logarithmically spaced bands."""
    high = min(high, rate / 2 * 0.95)
    bin_hz = rate / size
    ranges = []
    for i in range(bands):
        f0 = low * (high / low) ** (i / bands)
        f1 = low * (high / low) ** ((i + 1) / bands)
        start = max(1, int(f0 / bin_hz))
        end = max(start + 1, int(math.ceil(f1 / bin_hz)))
        ranges.append((start, end))
    return ranges


class SpectrumAnalyzer:
    """Turns the last MEDIA_FFT_SIZE samples into 24 bar levels (0..31).

    Levels are in dB with +3 dB per octave (music has less energy in the
    highs) and an automatic reference that follows the loudest band at once
    and drops slowly, so quiet and loud music both fill the bars. Bars fall
    smoothly instead of jumping down.
    """

    RANGE_DB = 50.0
    SILENCE_DB = -70.0

    def __init__(self, rate: float, fps: float):
        import numpy as np

        self.np = np
        self.window = np.hanning(MEDIA_FFT_SIZE)
        self.ranges = media_band_edges(rate)
        centres = [math.sqrt(a * b) * rate / MEDIA_FFT_SIZE for a, b in self.ranges]
        self.tilt = np.array([3.0 * math.log2(max(c, 1.0) / 1000.0) for c in centres])
        self.reference = -30.0
        self.ref_drop = 1.5 / fps  # dB per frame
        self.fall = 1.6 / fps  # bar height (0..1) per frame
        self.levels = np.zeros(MEDIA_BANDS)
        self.last_db = -200.0  # loudest band of the last frame (for --test-media)
        self.wave_ref = 0.05  # loudest recent waveform peak (automatic gain)
        self.wave_drop = 0.5 ** (1.0 / fps)  # halves in a second

    def update(self, samples: Any) -> str:
        np = self.np
        spectrum = np.abs(np.fft.rfft(samples * self.window)) / (MEDIA_FFT_SIZE / 4)
        peaks = np.array([spectrum[a:b].max() if b <= len(spectrum) else 0.0 for a, b in self.ranges])
        db = 20.0 * np.log10(np.maximum(peaks, 1e-9)) + self.tilt
        loudest = float(db.max())
        self.last_db = loudest
        if loudest < self.SILENCE_DB:
            target = np.zeros(MEDIA_BANDS)
        else:
            self.reference = max(loudest, self.reference - self.ref_drop, -50.0)
            top = self.reference + 4.0
            target = np.clip((db - (top - self.RANGE_DB)) / self.RANGE_DB, 0.0, 1.0)
        self.levels = np.maximum(target, self.levels - self.fall)
        return self.encode(self.levels)

    def silence(self) -> str:
        self.levels = self.np.maximum(self.levels - self.fall, 0.0)
        return self.encode(self.levels)

    def waveform(self, samples: Any) -> str:
        """64 waveform points for the oscilloscope effect (see MEDIA_WAVE_*)."""
        np = self.np
        samples = np.asarray(samples, dtype=np.float32)
        span = MEDIA_WAVE_SPAN
        start = len(samples) - span
        # Rising zero crossing in the span before the last one (if any).
        search = samples[max(1, len(samples) - 2 * span): len(samples) - span + 1]
        if len(search) > 1:
            rising = np.nonzero((search[:-1] < 0.0) & (search[1:] >= 0.0))[0]
            if len(rising):
                # Half a point earlier: the first point is centred on the crossing.
                start = max(1, len(samples) - 2 * span) + int(rising[0]) + 1
                start = max(0, start - span // MEDIA_WAVE_POINTS // 2)
        segment = samples[start:start + span]
        if len(segment) < span:
            return MEDIA_WAVE_SILENT
        points = segment.reshape(MEDIA_WAVE_POINTS, -1).mean(axis=1)
        peak = float(np.abs(segment).max())
        self.wave_ref = max(peak, self.wave_ref * self.wave_drop, 0.01)
        if peak < 1e-4:
            return MEDIA_WAVE_SILENT
        return self.encode_wave(points / self.wave_ref * 0.95)

    @staticmethod
    def encode_wave(values: Iterable[float]) -> str:
        top = 2 * MEDIA_WAVE_CENTRE
        return "".join(
            MEDIA_DIGITS[max(0, min(top, int(round((float(v) + 1.0) * MEDIA_WAVE_CENTRE))))]
            for v in values
        )

    @staticmethod
    def encode(levels: Iterable[float]) -> str:
        return "".join(
            MEDIA_DIGITS[max(0, min(MEDIA_MAX_LEVEL, int(round(v * MEDIA_MAX_LEVEL))))]
            for v in levels
        )


class LoopbackCapture:
    """Records what the default output device plays (WASAPI loopback)."""

    def __init__(self, step: Any = None) -> None:
        step = step or (lambda message: None)
        import numpy as np
        import pyaudiowpatch as pyaudio  # pip package PyAudioWPatch

        self.np = np
        self.pyaudio = pyaudio
        step("starting PortAudio")
        self.pa = pyaudio.PyAudio()
        step("finding the loopback of the default output device")
        self._lock = threading.Lock()
        self._buffer = np.zeros(MEDIA_FFT_SIZE, dtype=np.float32)
        self.last_audio = 0.0
        device = self.pa.get_default_wasapi_loopback()
        self.name = str(device.get("name", "?"))
        self.rate = float(device["defaultSampleRate"])
        channels = max(1, int(device["maxInputChannels"]))
        step(f"device {self.name}, {self.rate:g} Hz, {channels} channels")

        def callback(in_data: bytes, frame_count: int, time_info: Any, status: int):
            data = np.frombuffer(in_data, dtype=np.int16).astype(np.float32) / 32768.0
            if channels > 1:
                data = data[: len(data) - len(data) % channels].reshape(-1, channels).mean(axis=1)
            with self._lock:
                if len(data) >= MEDIA_FFT_SIZE:
                    self._buffer = data[-MEDIA_FFT_SIZE:].copy()
                else:
                    self._buffer = np.concatenate((self._buffer[len(data):], data))
            self.last_audio = time.monotonic()
            return (None, pyaudio.paContinue)

        self.stream = self.pa.open(
            format=pyaudio.paInt16,
            channels=channels,
            rate=int(self.rate),
            input=True,
            input_device_index=int(device["index"]),
            frames_per_buffer=1024,
            stream_callback=callback,
        )
        step("recording stream open")

    def samples(self) -> Any:
        with self._lock:
            return self._buffer.copy()

    def alive(self) -> bool:
        try:
            return bool(self.stream.is_active())
        except Exception:
            return False

    def close(self) -> None:
        for action in (self.stream.stop_stream, self.stream.close, self.pa.terminate):
            try:
                action()
            except Exception:
                pass


class MediaInfoReader:
    """Song, play state (Windows media controls) and volume (Core Audio)."""

    STATES = {0: "none", 1: "stopped", 2: "playing", 3: "stopped", 4: "playing", 5: "paused"}

    def __init__(self, warn: Any) -> None:
        import asyncio

        self.loop = asyncio.new_event_loop()
        self.asyncio = asyncio
        self.manager: Any = None
        self.media_control: Any = None
        self.volume_api: Any = None
        try:
            # winrt before comtypes, both multithreaded COM (avoids a clash).
            from winrt.windows.media import control as media_control

            self.media_control = media_control
        except Exception as exc:
            warn(f"song info unavailable (pip install winrt-Windows.Media.Control): {exc}")
        try:
            sys.coinit_flags = 0  # type: ignore[attr-defined]  # COINIT_MULTITHREADED
            from pycaw.pycaw import AudioUtilities

            self.volume_api = AudioUtilities
        except Exception as exc:
            warn(f"volume unavailable (pip install pycaw): {exc}")

    async def _session(self) -> tuple[str, str, str]:
        control = self.media_control
        if self.manager is None:
            self.manager = await control.GlobalSystemMediaTransportControlsSessionManager.request_async()
        session = self.manager.get_current_session()
        if session is None:
            return "none", "", ""
        state = self.STATES.get(int(session.get_playback_info().playback_status), "none")
        props = await session.try_get_media_properties_async()
        title = str(props.title or "") if props is not None else ""
        artist = str(props.artist or "") if props is not None else ""
        return state, title, artist

    def song(self) -> tuple[str, str, str]:
        if self.media_control is None:
            return "unavailable", "", ""
        try:
            return self.loop.run_until_complete(self.asyncio.wait_for(self._session(), 2.0))
        except Exception:
            self.manager = None
            return "none", "", ""

    def _endpoint(self) -> tuple[Any, str]:
        device = self.volume_api.GetSpeakers()
        volume = getattr(device, "EndpointVolume", None)
        name = str(getattr(device, "FriendlyName", "") or "")
        if volume is None:  # older pycaw: GetSpeakers() returns the raw IMMDevice
            from ctypes import POINTER, cast

            from comtypes import CLSCTX_ALL
            from pycaw.pycaw import IAudioEndpointVolume

            interface = device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            volume = cast(interface, POINTER(IAudioEndpointVolume))
        return volume, name

    def volume(self) -> tuple[int, bool, str]:
        """(percent or -1, muted, output device name)."""
        if self.volume_api is None:
            return -1, False, ""
        try:
            endpoint, name = self._endpoint()
            return (int(round(endpoint.GetMasterVolumeLevelScalar() * 100)),
                    bool(endpoint.GetMute()), name)
        except Exception:
            return -1, False, ""

    def change_volume(self, direction: int, step: int) -> None:
        if self.volume_api is None:
            return
        try:
            endpoint, _ = self._endpoint()
            percent = int(round(endpoint.GetMasterVolumeLevelScalar() * 100))
            if direction > 0:
                target = (percent // step + 1) * step
                if endpoint.GetMute():
                    endpoint.SetMute(0, None)
            else:
                target = ((percent + step - 1) // step - 1) * step
            endpoint.SetMasterVolumeLevelScalar(max(0, min(100, target)) / 100.0, None)
        except Exception:
            pass


def run_media_worker(fps: float, parent_pid: int, volume_step: int) -> None:
    """Child process for the MUSIC page; JSON lines on stdout, commands on stdin.

    stdin: "active 1|0" (MUSIC page on screen), "vol 1|-1", "info", "quit".
    stdout: {"k":"spec","b":"<24 digits>","w":"<64 digits>"} about `fps` times a second while
    active, {"k":"info",...} when something changes (and every 10 s),
    {"k":"warn","msg":...} for problems the bridge should log.
    """
    out_lock = threading.Lock()
    debug = os.environ.get("CONTROL_DECK_MEDIA_DEBUG") == "1"  # set by --test-media
    started = time.monotonic()
    if debug:
        import faulthandler

        # If something hangs, print where every thread is (goes to --test-media).
        faulthandler.dump_traceback_later(12, exit=False)

    def out(obj: dict[str, Any]) -> bool:
        try:
            with out_lock:
                sys.stdout.write(json.dumps(obj) + "\n")
                sys.stdout.flush()
            return True
        except (OSError, ValueError, AttributeError):
            return False

    warned: set[str] = set()

    def warn(message: str) -> None:
        if message not in warned:
            warned.add(message)
            out({"k": "warn", "msg": message})

    def step(message: str) -> None:
        if debug:
            out({"k": "step", "msg": f"{time.monotonic() - started:5.2f} s  {message}"})

    commands: queue.Queue[str] = queue.Queue()

    def read_stdin() -> None:
        for line in sys.stdin:
            commands.put(line.strip())
        commands.put("quit")

    # numpy first, before winrt/pycaw load and before other threads start:
    # imported later (after them) it hung on one PC while loading its DLLs.
    # One BLAS thread is enough (the FFT does not use BLAS).
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    step("loading numpy")
    try:
        import numpy  # noqa: F401
    except Exception as exc:
        warn(f"numpy unavailable (pip install numpy): {exc}")
    step("numpy loaded")
    step("loading song/volume libraries")
    reader = MediaInfoReader(warn)
    step("libraries loaded")
    threading.Thread(target=read_stdin, name="media-stdin", daemon=True).start()
    period = 1.0 / fps
    active = False
    capture: LoopbackCapture | None = None
    analyzer: SpectrumAnalyzer | None = None
    next_capture_try = 0.0
    next_spec = 0.0
    next_info = 0.0
    next_parent_check = 0.0
    last_info: dict[str, Any] | None = None
    last_info_sent = 0.0
    last_bars = ""
    last_bars_sent = 0.0
    device_name = ""

    def close_capture() -> None:
        nonlocal capture, analyzer
        if capture is not None:
            capture.close()
        capture = None
        analyzer = None

    while True:
        now = time.monotonic()
        while True:
            try:
                command = commands.get_nowait()
            except queue.Empty:
                break
            parts = command.split()
            if not parts:
                continue
            if parts[0] == "quit":
                close_capture()
                return
            if parts[0] == "active" and len(parts) > 1:
                active = parts[1] == "1"
                step(f"active {active}")
                next_info = now
                if not active:
                    close_capture()
            elif parts[0] == "vol" and len(parts) > 1:
                reader.change_volume(1 if parts[1] == "1" else -1, volume_step)
                next_info = now
            elif parts[0] == "info":
                next_info = min(next_info, now + 0.3)  # the player needs a moment

        if now >= next_parent_check:
            next_parent_check = now + 2.0
            if parent_pid and not psutil.pid_exists(parent_pid):
                close_capture()
                return

        if now >= next_info:
            next_info = now + 1.0
            step("reading song and volume")
            state, title, artist = reader.song()
            volume, mute, name = reader.volume()
            step(f"song {state}, volume {volume}, device {name!r}")
            info = {"k": "info", "state": state, "title": title, "artist": artist,
                    "vol": volume, "mute": mute}
            if info != last_info or now - last_info_sent >= 10.0:
                if not out(info):
                    close_capture()
                    return
                last_info = info
                last_info_sent = now
            if name and device_name and name != device_name and capture is not None:
                close_capture()  # output device changed (e.g. headphones): reopen
                next_capture_try = now
            device_name = name or device_name

        if active and capture is None and now >= next_capture_try:
            try:
                step("opening sound recording")
                capture = LoopbackCapture(step)
                analyzer = SpectrumAnalyzer(capture.rate, fps)
                out({"k": "capture", "device": capture.name, "rate": capture.rate})
                if debug:
                    import faulthandler

                    faulthandler.cancel_dump_traceback_later()  # started fine
                next_spec = now
            except Exception as exc:
                close_capture()
                warn(f"sound capture failed (pip install PyAudioWPatch numpy): {type(exc).__name__}: {exc}")
                next_capture_try = now + 10.0
        if capture is not None and not capture.alive():
            close_capture()
            next_capture_try = now + 2.0

        if active and capture is not None and analyzer is not None and now >= next_spec:
            next_spec = max(next_spec + period, now - period)
            audio = now - capture.last_audio < 0.25
            if audio:
                samples = capture.samples()
                bars = analyzer.update(samples)
                wave = analyzer.waveform(samples)
            else:
                bars = analyzer.silence()  # nothing playing: no audio arrives
                wave = MEDIA_WAVE_SILENT
            silent = bars.strip("0") == ""
            if not (silent and last_bars.strip("0") == "" and now - last_bars_sent < 1.0):
                # "audio"/"db" are only for --test-media; the bridge ignores them.
                if not out({"k": "spec", "b": bars, "w": wave, "audio": audio,
                            "db": round(analyzer.last_db, 1)}):
                    close_capture()
                    return
                last_bars = bars
                last_bars_sent = now

        wake = next_info
        if active and capture is not None:
            wake = min(wake, next_spec)
        time.sleep(max(0.002, min(0.05, wake - time.monotonic())))


class MediaService:
    """Runs the MUSIC worker (run_media_worker) and passes its frames on."""

    RESTART_DELAY_SECONDS = 30.0

    def __init__(self, config: dict[str, Any], command: list[str] | None = None) -> None:
        settings = config.get("media") or {}
        self.enabled = bool(settings.get("enabled", True)) and (
            command is not None or platform.system() == "Windows"
        )
        try:
            self.fps = max(5.0, min(30.0, float(settings.get("fps", 20))))
        except (TypeError, ValueError):
            self.fps = 20.0
        try:
            self.volume_step = max(1, min(20, int(settings.get("volume_step", 5))))
        except (TypeError, ValueError):
            self.volume_step = 5
        self._command = command
        self._process: subprocess.Popen[Any] | None = None
        self._stopping = False
        self._active = False
        self._info: dict[str, Any] | None = None
        self._sink: Any = None
        self._lock = threading.Lock()
        self._stdin_lock = threading.Lock()

    def default_command(self) -> list[str]:
        executable = Path(sys.executable)
        if platform.system() == "Windows":
            console = executable.with_name("python.exe")
            if console.is_file():
                executable = console
        return [
            str(executable),
            str(Path(__file__).resolve()),
            "--media-worker",
            str(self.fps),
            "--volume-step",
            str(self.volume_step),
            "--parent-pid",
            str(os.getpid()),
        ]

    def start(self) -> None:
        if not self.enabled:
            return
        atexit.register(self.stop)
        threading.Thread(target=self._run, name="media", daemon=True).start()

    def stop(self) -> None:
        self._stopping = True
        process = self._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    # -- display side ----------------------------------------------------
    def attach(self, sink: Any) -> None:
        """sink(frame) sends a frame to the display (None = disconnected)."""
        with self._lock:
            self._sink = sink
            info = self._info
        if sink is not None and info is not None:
            self._emit(info)

    def resend(self) -> None:
        with self._lock:
            info = self._info
        if info is not None:
            self._emit(info)

    def set_page(self, page: str, awake: bool = True) -> None:
        active = page == "music" and awake
        if active != self._active:
            self._active = active
            self._write(f"active {1 if active else 0}")
        if active:
            self.resend()

    def media_key(self, key: str) -> None:
        log(f"Media button: {key}")
        if key in MEDIA_KEYS:
            try:
                send_hotkey(MEDIA_KEYS[key])
            except Exception as exc:
                log(f"Media button failed: {type(exc).__name__}: {exc}")
            self._write("info")
        elif key in {"vol_up", "vol_down"}:
            self._write("vol 1" if key == "vol_up" else "vol -1")
        else:
            log(f"Unknown media button: {key}")

    # -- worker side -----------------------------------------------------
    def _emit(self, frame: dict[str, Any]) -> None:
        with self._lock:
            sink = self._sink
        if sink is None:
            return
        try:
            sink(frame)
        except Exception:
            pass  # serial port gone; the main loop reconnects

    def _write(self, line: str) -> None:
        process = self._process
        if process is None or process.stdin is None:
            return
        try:
            with self._stdin_lock:
                process.stdin.write(line + "\n")
                process.stdin.flush()
        except (OSError, ValueError):
            pass

    def _handle_line(self, line: str) -> None:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(data, dict):
            return
        kind = data.get("k")
        if kind == "spec":
            bars = str(data.get("b", ""))
            if len(bars) == MEDIA_BANDS and all(c in MEDIA_DIGITS for c in bars):
                frame = {"type": "spec", "b": bars}
                wave = str(data.get("w", ""))
                if len(wave) == MEDIA_WAVE_POINTS and all(
                        c in MEDIA_DIGITS[: 2 * MEDIA_WAVE_CENTRE + 1] for c in wave):
                    frame["w"] = wave  # oscilloscope effect
                self._emit(frame)
        elif kind == "info":
            state = str(data.get("state", "none"))
            if state not in {"playing", "paused", "stopped", "none", "unavailable"}:
                state = "none"
            try:
                volume = int(data.get("vol", -1))
            except (TypeError, ValueError):
                volume = -1
            frame = {
                "type": "media",
                "state": state,
                "title": clean_media_text(data.get("title")),
                "artist": clean_media_text(data.get("artist")),
                "vol": max(-1, min(100, volume)),
                "mute": bool(data.get("mute")),
            }
            with self._lock:
                self._info = frame
            self._emit(frame)
        elif kind == "warn":
            log(f"Music page: {data.get('msg', '')}")

    def _run(self) -> None:
        while not self._stopping:
            command = self._command or self.default_command()
            creationflags = subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
            try:
                self._process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=creationflags,
                )
            except OSError as exc:
                log(f"Music worker could not start: {exc}")
            else:
                log(f"Music worker started (PID {self._process.pid})")
                if self._active:
                    self._write("active 1")
                assert self._process.stdout is not None
                for line in self._process.stdout:
                    self._handle_line(line)
                code = self._process.wait()
                if self._stopping:
                    return
                log(f"Music worker stopped (exit code {code})")
            time.sleep(self.RESTART_DELAY_SECONDS)


def test_media(config: dict[str, Any], seconds: float = 15.0) -> int:
    """--test-media: run the worker for a few seconds and print what it sends."""
    service = MediaService(config)
    command = service.default_command()
    print("Starting the music worker... (play some music)", flush=True)
    env = dict(os.environ, CONTROL_DECK_MEDIA_DEBUG="1")
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                               errors="replace", env=env)
    assert process.stdin is not None and process.stdout is not None
    process.stdin.write("active 1\n")
    process.stdin.flush()
    lines: queue.Queue[str] = queue.Queue()
    threading.Thread(target=lambda: [lines.put(line) for line in process.stdout],
                     daemon=True).start()
    deadline = time.monotonic() + seconds
    received = 0
    shown = 0
    try:
        while time.monotonic() < deadline:
            try:
                line = lines.get(timeout=0.2)
            except queue.Empty:
                if process.poll() is not None:
                    print(f"The music worker stopped (exit code {process.returncode}).")
                    break
                continue
            received += 1
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                print(line.rstrip())  # Python error text from the worker
                continue
            if data.get("k") == "info":
                print(f"state {data.get('state')}, title {data.get('title')!r}, "
                      f"artist {data.get('artist')!r}, volume {data.get('vol')}%"
                      f"{' (muted)' if data.get('mute') else ''}", flush=True)
            elif data.get("k") == "warn":
                print(f"WARNING: {data.get('msg')}", flush=True)
            elif data.get("k") == "step":
                print(f"  [{data.get('msg')}]", flush=True)
            elif data.get("k") == "capture":
                print(f"recording: {data.get('device')} ({data.get('rate')} Hz)", flush=True)
            elif data.get("k") == "spec":
                shown += 1
                if shown % 5 == 1:
                    print("bars: |" + "".join(" .:-=+*#%@"[min(9, MEDIA_DIGITS.index(c) * 10 // 32)]
                                              for c in data.get("b", ""))
                          + f"|  sound {'yes' if data.get('audio') else 'NO'}, loudest {data.get('db')} dB",
                          flush=True)
    finally:
        process.terminate()
    if received == 0:
        print("Nothing came from the music worker.")
    print("Done.")
    return 0


@dataclass
class Counters:
    time: float
    net_sent: int
    net_recv: int
    disk_read: int
    disk_write: int


def current_counters() -> Counters:
    network = psutil.net_io_counters()
    disk = psutil.disk_io_counters()
    return Counters(
        time=time.monotonic(),
        net_sent=int(network.bytes_sent if network else 0),
        net_recv=int(network.bytes_recv if network else 0),
        disk_read=int(disk.read_bytes if disk else 0),
        disk_write=int(disk.write_bytes if disk else 0),
    )


def get_stats(
    previous: Counters,
    disk_path: str,
    lhm: LibreHardwareMonitorHttpReader,
    processes: ProcessMonitor | None = None,
) -> tuple[dict[str, Any], Counters]:
    current = current_counters()
    elapsed = max(0.001, current.time - previous.time)

    try:
        disk_used = psutil.disk_usage(disk_path).percent
    except Exception:
        disk_used = None

    memory = psutil.virtual_memory()
    gib = 1024.0 ** 3
    ram_total_gb = memory.total / gib
    ram_used_gb = (memory.total - memory.available) / gib

    nvidia = read_nvidia_gpu()
    lhm_stats = lhm.read()
    psutil_cpu_temp = read_cpu_temp_psutil()
    cpu_temp = psutil_cpu_temp if psutil_cpu_temp is not None else lhm_stats.get("cpu_temp")
    gpu_temp = nvidia.get("gpu_temp")
    if gpu_temp is None:
        gpu_temp = lhm_stats.get("gpu_temp")

    now = datetime.now()
    clock_seconds = now.hour * 3600 + now.minute * 60 + now.second

    payload = {
        "type": "stats",
        "cpu": safe_number(psutil.cpu_percent(interval=None), 1),
        "ram": safe_number(memory.percent, 1),
        "ram_used_gb": safe_number(ram_used_gb, 1),
        "ram_total_gb": safe_number(ram_total_gb, 1),
        "gpu": safe_number(nvidia.get("gpu"), 1),
        "vram": safe_number(nvidia.get("vram"), 1),
        "vram_used_gb": safe_number(nvidia.get("vram_used_gb"), 1),
        "vram_total_gb": safe_number(nvidia.get("vram_total_gb"), 1),
        "cpu_temp": safe_number(cpu_temp, 1),
        "gpu_temp": safe_number(gpu_temp, 1),
        "system_temp": safe_number(lhm_stats.get("system_temp"), 1),
        "disk_temp": safe_number(lhm_stats.get("disk_temp"), 1),
        "disk_used": safe_number(disk_used, 1),
        "disk_read_mbs": safe_number(
            max(0, current.disk_read - previous.disk_read) / elapsed / 1_000_000.0,
            2,
        ),
        "disk_write_mbs": safe_number(
            max(0, current.disk_write - previous.disk_write) / elapsed / 1_000_000.0,
            2,
        ),
        "net_down_mbps": safe_number(
            max(0, current.net_recv - previous.net_recv) * 8.0 / elapsed / 1_000_000.0,
            2,
        ),
        "net_up_mbps": safe_number(
            max(0, current.net_sent - previous.net_sent) * 8.0 / elapsed / 1_000_000.0,
            2,
        ),
        "clock": now.strftime("%H:%M"),
        "clock_seconds": clock_seconds,
    }
    if processes is not None:
        payload.update(processes.sample())
    return payload, current


# Buttons on the CONTROLS page (2 rows of 4 since 1.2.0).
MACRO_COUNT = 8


def macro_packet(config: dict[str, Any]) -> dict[str, Any]:
    macros: list[dict[str, Any]] = []
    for item in config.get("macros", []):
        try:
            macro_id = int(item.get("id"))
        except Exception:
            continue
        if 1 <= macro_id <= MACRO_COUNT:
            entry = {
                "id": macro_id,
                "label": str(item.get("label", f"MACRO {macro_id}"))[:22],
            }
            if "icon" in item:
                # Icon on the CONTROLS page (see src/button_icons.h).
                entry["icon"] = str(item.get("icon") or "")[:24].lower()
            macros.append(entry)
    packet: dict[str, Any] = {"type": "config", "macros": macros}
    # Display turns its backlight off after this many seconds without data
    # (PC off or asleep). 0 = never.
    try:
        minutes = float(config.get("screen_sleep_minutes", 2))
    except (TypeError, ValueError):
        minutes = 2.0
    packet["sleep_s"] = max(0, int(round(minutes * 60)))
    # Long press on this button (1-8) opens the power menu on the display.
    packet["power_button"] = power_menu_button(config)
    # MUSIC page effect (1-10). The display applies it when this value
    # changes; a tap on the bars picks another one (kept on the display).
    effect = music_effect(config)
    if effect:
        packet["music_effect"] = effect
    return packet


def music_effect(config: dict[str, Any]) -> int:
    """media.effect from config.json (1-10), 0 = not set or not valid."""
    settings = config.get("media")
    if not isinstance(settings, dict) or "effect" not in settings:
        return 0
    try:
        effect = int(settings.get("effect"))
    except (TypeError, ValueError):
        return 0
    return effect if 1 <= effect <= MEDIA_EFFECTS else 0


def power_menu_button(config: dict[str, Any]) -> int:
    try:
        button = int(config.get("power_menu_button", 0))
    except (TypeError, ValueError):
        return 0
    return button if 1 <= button <= MACRO_COUNT else 0


POWER_ACTIONS = ("shutdown", "restart", "sleep")

# Windows: "shutdown /t 0" without /f, so programs with unsaved work can still
# stop it (any /t above 0 would imply /f). Sleep goes through .NET, which
# enables the shutdown privilege itself; "rundll32 powrprof.dll,SetSuspendState"
# would hibernate instead when hibernation is on.
_WINDOWS_SLEEP = (
    "Add-Type -AssemblyName System.Windows.Forms; "
    "[void][System.Windows.Forms.Application]::SetSuspendState('Suspend', $false, $false)"
)


def power_command(action: str, system: str | None = None) -> list[str]:
    system = system or platform.system()
    if system == "Windows":
        if action == "shutdown":
            return ["shutdown.exe", "/s", "/t", "0"]
        if action == "restart":
            return ["shutdown.exe", "/r", "/t", "0"]
        return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_SLEEP]
    if system == "Darwin":
        if action == "shutdown":
            return ["osascript", "-e", 'tell application "System Events" to shut down']
        if action == "restart":
            return ["osascript", "-e", 'tell application "System Events" to restart']
        return ["pmset", "sleepnow"]
    return ["systemctl", {"shutdown": "poweroff", "restart": "reboot", "sleep": "suspend"}[action]]


def execute_power(config: dict[str, Any], action: str) -> None:
    """Shut down / restart / sleep, asked for from the power menu on the display."""
    if action not in POWER_ACTIONS:
        log(f"Power: unknown action {action!r}")
        return
    if power_menu_button(config) == 0:
        log(f"Power: {action} ignored (power_menu_button is off in config.json)")
        return
    if bool(config.get("power_menu_test_mode", False)):
        log(f"Power: {action} requested - test mode, nothing done "
            "(power_menu_test_mode in config.json)")
        return
    command = power_command(action)
    log(f"Power: {action} requested from the display: {' '.join(command[:4])}")
    try:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0,
        )
    except OSError as exc:
        log(f"Power: {action} failed: {exc}")


def keyboard_key(token: str):
    if Key is None or KeyCode is None:
        raise RuntimeError("pynput is not available")
    token = token.strip().lower()
    mapping = {
        "ctrl": Key.ctrl,
        "control": Key.ctrl,
        "alt": Key.alt,
        "shift": Key.shift,
        "cmd": Key.cmd,
        "win": Key.cmd,
        "windows": Key.cmd,
        "enter": Key.enter,
        "return": Key.enter,
        "esc": Key.esc,
        "escape": Key.esc,
        "tab": Key.tab,
        "space": Key.space,
        "backspace": Key.backspace,
        "delete": Key.delete,
        "up": Key.up,
        "down": Key.down,
        "left": Key.left,
        "right": Key.right,
        "home": Key.home,
        "end": Key.end,
        "page_up": Key.page_up,
        "page_down": Key.page_down,
        "volume_up": Key.media_volume_up,
        "volume_down": Key.media_volume_down,
        "volume_mute": Key.media_volume_mute,
        "media_play_pause": Key.media_play_pause,
        "media_next": Key.media_next,
        "media_previous": Key.media_previous,
    }
    if token in mapping:
        return mapping[token]
    if token.startswith("f") and token[1:].isdigit() and hasattr(Key, token):
        return getattr(Key, token)
    if len(token) == 1:
        return KeyCode.from_char(token)
    raise ValueError(f"Unsupported key token: {token}")


def send_hotkey(specification: str) -> None:
    if KeyboardController is None:
        raise RuntimeError("pynput is not installed or available")
    controller = KeyboardController()
    keys = [keyboard_key(item) for item in specification.split("+") if item.strip()]
    for key in keys:
        controller.press(key)
    for key in reversed(keys):
        controller.release(key)


def type_text(text: str) -> None:
    if KeyboardController is None:
        raise RuntimeError("pynput is not installed or available")
    KeyboardController().type(text)


def url_reachable(url: str, timeout: float = 0.6) -> bool:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": f"ControlDeck/{APP_VERSION}"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= int(response.status) < 500
    except Exception:
        return False


ERROR_ELEVATION_REQUIRED = 740


def shell_execute(command: list[str], cwd: str | None = None) -> None:
    """Start a program through ShellExecute, like a double-click.

    Unlike CreateProcess this honours the program's manifest, so apps that
    need administrator rights (e.g. Task Manager for admin users) show the
    normal UAC prompt instead of failing with WinError 740.
    """
    import ctypes

    parameters = subprocess.list2cmdline(command[1:]) if len(command) > 1 else None
    result = ctypes.windll.shell32.ShellExecuteW(
        None, "open", command[0], parameters, cwd, 1
    )
    if int(result) <= 32:
        raise OSError(f"ShellExecute failed with code {int(result)}")


def popen_or_elevate(command: Any, **kwargs: Any) -> subprocess.Popen[Any] | None:
    """Popen, falling back to ShellExecute when Windows requires elevation."""
    try:
        return subprocess.Popen(command, **kwargs)
    except OSError as exc:
        if getattr(exc, "winerror", None) != ERROR_ELEVATION_REQUIRED or not isinstance(
            command, list
        ):
            raise
        log("Program requires administrator rights; requesting elevation (UAC)")
        shell_execute([str(item) for item in command], kwargs.get("cwd"))
        return None


def start_process(
    command_value: Any,
    working_directory: Any = None,
    hidden: bool = False,
    new_console: bool = False,
) -> subprocess.Popen[Any] | None:
    command_value = resolve_platform_value(command_value)
    working_directory = resolve_platform_value(working_directory)
    if not command_value:
        raise ValueError("No command is configured for this operating system")

    cwd = expand_text(working_directory) if working_directory else None
    if cwd and not Path(str(cwd)).exists():
        raise FileNotFoundError(f"Working directory does not exist: {cwd}")

    shell = isinstance(command_value, str)
    if isinstance(command_value, list):
        command: Any = [str(expand_text(item)) for item in command_value]
    else:
        command = str(expand_text(command_value))

    creationflags = 0
    startupinfo = None
    stdout: Any = None
    stderr: Any = None
    if platform.system() == "Windows":
        if hidden:
            creationflags |= subprocess.CREATE_NO_WINDOW
            stdout = subprocess.DEVNULL
            stderr = subprocess.DEVNULL
        elif new_console:
            creationflags |= subprocess.CREATE_NEW_CONSOLE
        if hidden:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0

    return popen_or_elevate(
        command,
        shell=shell,
        cwd=str(cwd) if cwd else None,
        creationflags=creationflags,
        startupinfo=startupinfo,
        stdout=stdout,
        stderr=stderr,
    )


_START_APP_CACHE: dict[str, str] = {}


def windows_start_apps(name: str) -> list[tuple[str, str]]:
    """Return (name, AppID) pairs from the Windows Start menu that contain name.

    Uses PowerShell's Get-StartApps, which lists both classic desktop apps and
    Microsoft Store / MSIX apps. The search text is passed through an
    environment variable so it is never interpreted as PowerShell code.
    """
    if platform.system() != "Windows":
        return []
    script = (
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
        "$needle = $env:CONTROL_DECK_APP_NAME; "
        "Get-StartApps | Where-Object { $_.Name -like ('*' + $needle + '*') } | "
        "ForEach-Object { $_.Name + [char]9 + $_.AppID }"
    )
    environment = dict(os.environ)
    environment["CONTROL_DECK_APP_NAME"] = name
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            timeout=15,
            check=False,
            env=environment,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception as exc:
        log(f"Start menu lookup failed: {exc}")
        return []
    output = completed.stdout.decode("utf-8", errors="replace")
    pairs: list[tuple[str, str]] = []
    for line in output.splitlines():
        app_name, separator, app_id = line.partition("\t")
        if separator and app_id.strip():
            pairs.append((app_name.strip(), app_id.strip()))
    return pairs


def choose_start_app(name: str, pairs: list[tuple[str, str]]) -> str | None:
    """Prefer an exact (case-insensitive) name match, then the shortest name."""
    if not pairs:
        return None
    wanted = name.strip().lower()
    exact = [app_id for app_name, app_id in pairs if app_name.lower() == wanted]
    if exact:
        return exact[0]
    return min(pairs, key=lambda item: len(item[0]))[1]


def find_start_menu_app(name: str) -> str | None:
    key = name.strip().lower()
    if key in _START_APP_CACHE:
        return _START_APP_CACHE[key]
    app_id = choose_start_app(name, windows_start_apps(name))
    if app_id:
        _START_APP_CACHE[key] = app_id
    return app_id


def resolve_app_path(candidate: Any) -> str | None:
    """Expand %VARS% and wildcards, then return an existing file or PATH hit."""
    expanded = str(expand_text(str(candidate)))
    if any(token in expanded for token in "*?["):
        matches = sorted(glob.glob(expanded))
        files = [item for item in matches if Path(item).is_file()]
        return files[-1] if files else None
    if Path(expanded).is_file():
        return expanded
    return shutil.which(expanded)


def launch_app(item: dict[str, Any]) -> str:
    """Start an installed application.

    value (per platform) may be:
      - a string (one path) or a list (executable + arguments), or
      - {"paths": [...], "args": [...], "start_menu": "Name"}

    The first existing path wins. On Windows, when no path exists, the app is
    looked up by its Start menu name and started through shell:AppsFolder,
    which also works for Microsoft Store / MSIX installations.
    """
    spec = resolve_platform_value(item.get("value"))
    if not spec:
        raise ValueError("No app is configured for this operating system")
    if isinstance(spec, str):
        spec = {"paths": [spec]}
    elif isinstance(spec, list):
        # A list is a command: executable followed by its arguments.
        spec = {"paths": spec[:1], "args": spec[1:]}
    if not isinstance(spec, dict):
        raise ValueError("Invalid app specification")

    paths = spec.get("paths") or []
    if isinstance(paths, str):
        paths = [paths]
    args = [str(expand_text(arg)) for arg in spec.get("args") or []]

    working_directory = resolve_platform_value(item.get("working_directory"))
    cwd = expand_text(working_directory) if working_directory else None
    if cwd and not Path(str(cwd)).is_dir():
        cwd = None

    creationflags = 0
    if platform.system() == "Windows":
        if item.get("new_console"):
            creationflags |= subprocess.CREATE_NEW_CONSOLE
        else:
            creationflags |= subprocess.DETACHED_PROCESS
        creationflags |= subprocess.CREATE_NEW_PROCESS_GROUP

    for candidate in paths:
        resolved = resolve_app_path(candidate)
        if resolved:
            popen_or_elevate(
                [resolved, *args],
                cwd=str(cwd) if cwd else None,
                creationflags=creationflags,
                close_fds=True,
            )
            return resolved

    start_menu_name = str(spec.get("start_menu", "") or "").strip()
    if start_menu_name and platform.system() == "Windows":
        app_id = find_start_menu_app(start_menu_name)
        if app_id:
            subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app_id}"])
            return f"Start menu '{start_menu_name}' ({app_id})"

    tried = ", ".join(str(path) for path in paths) or "(no paths)"
    raise FileNotFoundError(
        f"Application not found. Tried: {tried}"
        + (f"; Start menu name: {start_menu_name}" if start_menu_name else "")
    )


def execute_macro(config: dict[str, Any], macro_id: int) -> None:
    item = next(
        (macro for macro in config.get("macros", []) if int(macro.get("id", -1)) == macro_id),
        None,
    )
    if item is None:
        log(f"Macro {macro_id}: not configured")
        return

    action = str(item.get("action", "none")).lower()
    label = str(item.get("label", f"Macro {macro_id}"))
    log(f"Macro {macro_id} pressed: {label}")

    try:
        if action == "none":
            return
        if action == "url":
            value = resolve_platform_value(item.get("value"))
            if not value:
                raise ValueError("No URL configured")
            webbrowser.open(str(value))
            return
        if action == "hotkey":
            send_hotkey(str(resolve_platform_value(item.get("value"))))
            return
        if action == "text":
            type_text(str(resolve_platform_value(item.get("value"))))
            return
        if action == "app":
            target = launch_app(item)
            log(f"Started {label}: {target}")
            return
        if action in {"command", "service"}:
            health_url = str(item.get("health_url", "") or "").strip()
            if action == "service" and health_url and url_reachable(health_url):
                log(f"{label} is already running: {health_url}")
                return
            process = start_process(
                item.get("value"),
                working_directory=item.get("working_directory"),
                hidden=bool(item.get("hidden", False)),
                new_console=bool(item.get("new_console", False)),
            )
            if process is not None:
                log(f"Started {label} (PID {process.pid})")
            else:
                log(f"Started {label} with elevation")
            return
        raise ValueError(f"Unsupported macro action: {action}")
    except Exception as exc:
        log(f"Macro {macro_id} failed: {exc}")


# Greek -> Latin transliteration so place names render with the ASCII fonts
# of the ESP32 display (e.g. "Θεσσαλονίκη" -> "THESSALONIKI").
_GREEK_DIGRAPHS = {
    "ου": "ou", "ού": "ou", "γγ": "ng",
}
_GREEK_LETTERS = dict(zip(
    "αβγδεζηθικλμνξοπρσςτυφχψω",
    ["a", "v", "g", "d", "e", "z", "i", "th", "i", "k", "l", "m", "n", "x",
     "o", "p", "r", "s", "s", "t", "y", "f", "ch", "ps", "o"],
))


def display_label(text: str, max_length: int = 14) -> str:
    lowered = unicodedata.normalize("NFC", str(text)).lower()
    out: list[str] = []
    index = 0
    while index < len(lowered):
        pair = lowered[index : index + 2]
        if pair in _GREEK_DIGRAPHS:
            out.append(_GREEK_DIGRAPHS[pair])
            index += 2
            continue
        char = lowered[index]
        base = unicodedata.normalize("NFD", char)[0]
        out.append(_GREEK_LETTERS.get(base, base))
        index += 1
    ascii_text = unicodedata.normalize("NFD", "".join(out))
    ascii_text = "".join(ch for ch in ascii_text if 32 <= ord(ch) < 127)
    ascii_text = re.sub(r"\s+", " ", ascii_text).strip().upper()
    return ascii_text[:max_length] or "?"


def https_context() -> ssl.SSLContext:
    """TLS context for internet requests.

    Uses the certifi CA bundle when available. Python on Windows otherwise
    relies on the Windows root store, which can be outdated and then fails
    with "certificate has expired" for Let's Encrypt sites.
    """
    if certifi is not None:
        try:
            return ssl.create_default_context(cafile=certifi.where())
        except Exception:
            pass
    return ssl.create_default_context()


@dataclass(frozen=True)
class WeatherLocation:
    label: str
    latitude: float
    longitude: float


def parse_open_meteo(label: str, data: dict[str, Any]) -> dict[str, Any]:
    """Convert an Open-Meteo forecast response into a compact display entry."""
    current = data.get("current") or {}
    daily = data.get("daily") or {}

    def first(key: str) -> Any:
        values = daily.get(key) or []
        return values[0] if values else None

    def as_int(value: Any) -> int | None:
        number = safe_number(value, 0)
        return None if number is None else int(number)

    code = as_int(current.get("weather_code"))
    if code is None:
        code = as_int(first("weather_code"))
    return {
        "name": label,
        "temp": safe_number(current.get("temperature_2m"), 1),
        "feels": safe_number(current.get("apparent_temperature"), 0),
        "code": code if code is not None else -1,
        "day": as_int(current.get("is_day")) if current.get("is_day") is not None else 1,
        "hum": as_int(current.get("relative_humidity_2m")),
        "wind": as_int(current.get("wind_speed_10m")),
        "tmax": safe_number(first("temperature_2m_max"), 0),
        "tmin": safe_number(first("temperature_2m_min"), 0),
        "rain": as_int(first("precipitation_probability_max")),
    }


class WeatherService:
    """Fetch weather for configured places in a background thread."""

    FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
    GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
    MAX_LOCATIONS = 6

    def __init__(self, config: dict[str, Any]):
        weather = config.get("weather") or {}
        self.locations_config = list(weather.get("locations") or [])[: self.MAX_LOCATIONS]
        self.enabled = bool(weather.get("enabled", False)) and bool(self.locations_config)
        self.refresh_seconds = max(5.0, float(weather.get("refresh_minutes", 15))) * 60.0
        self.retry_seconds = 60.0
        self.timeout = float(weather.get("timeout_seconds", 8))
        self.language = str(weather.get("language", "el"))
        # Open-Meteo weather model; "best_match" lets Open-Meteo choose.
        self.model = str(weather.get("model", "best_match") or "best_match")
        self._resolved: dict[int, WeatherLocation] = {}
        self._entries: dict[int, dict[str, Any]] = {}
        self._payload: dict[str, Any] | None = None
        self.version = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ssl_context = https_context()

    def _get_json(self, url: str, params: dict[str, Any]) -> Any:
        query = urllib.parse.urlencode(params)
        request = urllib.request.Request(
            f"{url}?{query}",
            headers={"User-Agent": f"ESP32-PC-Control-Deck/{APP_VERSION}"},
        )
        with urllib.request.urlopen(
            request, timeout=self.timeout, context=self._ssl_context
        ) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))

    def resolve(self, index: int, item: dict[str, Any]) -> WeatherLocation:
        if index in self._resolved:
            return self._resolved[index]
        name = str(item.get("name", "") or "").strip()
        latitude = item.get("latitude")
        longitude = item.get("longitude")
        if latitude is None or longitude is None:
            if not name:
                raise ValueError("Weather location needs a name or latitude/longitude")
            params: dict[str, Any] = {
                "name": name,
                "count": 1,
                "language": self.language,
                "format": "json",
            }
            if item.get("country_code"):
                params["countryCode"] = str(item["country_code"])
            results = (self._get_json(self.GEOCODING_URL, params) or {}).get("results") or []
            if not results:
                raise LookupError(f"Place not found: {name}")
            latitude = results[0]["latitude"]
            longitude = results[0]["longitude"]
            log(f"Weather: '{name}' -> {results[0].get('name')} ({latitude}, {longitude})")
        label = display_label(item.get("label") or name or f"{latitude},{longitude}")
        location = WeatherLocation(label, float(latitude), float(longitude))
        self._resolved[index] = location
        return location

    # Models offered by --compare-weather (Open-Meteo names).
    COMPARE_MODELS = (
        ("best_match", "Auto (best match)"),
        ("ecmwf_ifs025", "ECMWF IFS (Europe)"),
        ("ecmwf_aifs025_single", "ECMWF AIFS (AI)"),
        ("icon_eu", "DWD ICON-EU (Germany)"),
        ("icon_global", "DWD ICON global"),
        ("meteofrance_seamless", "Meteo-France"),
        ("ukmo_seamless", "UK Met Office"),
        ("gfs_seamless", "NOAA GFS (USA)"),
        ("gem_seamless", "Canada GEM"),
    )

    def fetch_location(self, location: WeatherLocation, model: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {
            "latitude": location.latitude,
            "longitude": location.longitude,
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,"
            "is_day,weather_code,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
            "precipitation_probability_max",
            "timezone": "auto",
            "forecast_days": 1,
            "wind_speed_unit": "kmh",
        }
        model = model or self.model
        if model and model != "best_match":
            params["models"] = model
        data = self._get_json(self.FORECAST_URL, params)
        return parse_open_meteo(location.label, data)

    def compare_models(self) -> str:
        """Table of the current forecast from several models, per place."""

        def cell(value: Any, suffix: str = "") -> str:
            return "--" if value is None else f"{value:g}{suffix}"

        lines = [
            f"Weather models compared at {datetime.now().strftime('%d/%m %H:%M')}",
            f"Current setting: \"model\": \"{self.model}\"",
            "",
        ]
        header = f"{'model':<22}{'now':>7}{'feels':>7}{'max':>6}{'min':>6}{'rain':>6}{'wind':>8}{'hum':>6}"
        for index, item in enumerate(self.locations_config):
            try:
                location = self.resolve(index, item)
            except Exception as exc:
                lines.append(f"{item.get('label') or item.get('name')}: {exc}")
                continue
            lines.append(f"== {location.label} ({location.latitude}, {location.longitude})")
            lines.append(header)
            for model, title in self.COMPARE_MODELS:
                try:
                    e = self.fetch_location(location, model)
                    lines.append(
                        f"{title:<22}{cell(e['temp'], 'C'):>7}{cell(e['feels'], 'C'):>7}"
                        f"{cell(e['tmax']):>6}{cell(e['tmin']):>6}{cell(e['rain'], '%'):>6}"
                        f"{cell(e['wind'], 'km/h'):>8}{cell(e['hum'], '%'):>6}"
                        f"   ({model})"
                    )
                except Exception as exc:
                    lines.append(f"{title:<22}  not available ({type(exc).__name__})   ({model})")
            lines.append("")
        lines.append('Set the closest model in host/config.json -> "weather": {"model": "<name>"}')
        return "\n".join(lines)

    def refresh(self) -> bool:
        """Fetch every location once. Returns True when all succeeded."""
        all_ok = True
        for index, item in enumerate(self.locations_config):
            try:
                location = self.resolve(index, item)
                self._entries[index] = self.fetch_location(location)
            except Exception as exc:
                all_ok = False
                log(f"Weather update failed for {item.get('label') or item.get('name')}: {exc}")
        entries = [self._entries[i] for i in sorted(self._entries)]
        if entries:
            payload = {
                "type": "weather",
                "updated": datetime.now().strftime("%H:%M"),
                "locations": entries,
            }
            with self._lock:
                self._payload = payload
                self.version += 1
        return all_ok

    def latest(self) -> tuple[int, dict[str, Any] | None]:
        with self._lock:
            return self.version, self._payload

    def _loop(self) -> None:
        while not self._stop.is_set():
            ok = self.refresh()
            self._stop.wait(self.refresh_seconds if ok else self.retry_seconds)

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        log(f"Weather enabled for {len(self.locations_config)} location(s)")
        self._thread = threading.Thread(target=self._loop, name="weather", daemon=True)
        self._thread.start()


# ------------------------------------------------------------ claude usage
WEEKDAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


def parse_iso_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone()  # local time zone
    except Exception:
        return None


def parse_claude_usage(data: dict[str, Any]) -> dict[str, Any]:
    """Map the usage response to the compact `claude` frame."""

    def window(key: str) -> tuple[float | None, datetime | None]:
        item = data.get(key) or {}
        if not isinstance(item, dict):
            return None, None
        return safe_number(item.get("utilization"), 0), parse_iso_time(item.get("resets_at"))

    session, session_reset = window("five_hour")
    weekly, weekly_reset = window("seven_day")
    return {
        "type": "claude",
        "state": "ok",
        "s": session,
        "s_reset": session_reset.strftime("%H:%M") if session_reset else "",
        "w": weekly,
        "w_reset": WEEKDAYS[weekly_reset.weekday()] if weekly_reset else "",
    }


class ClaudeUsageService:
    """Claude plan usage (5-hour session and weekly limit).

    Reads the login that Claude Code stores in ~/.claude/.credentials.json
    and asks the same usage endpoint that Claude Code's /usage uses. The
    endpoint is not an official public API and may change. The token is only
    read locally and only sent to api.anthropic.com; it is never logged.
    """

    USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

    def __init__(self, config: dict[str, Any]):
        settings = config.get("claude_usage") or {}
        self.enabled = bool(settings.get("enabled", False))
        default_path = os.path.join("~", ".claude", ".credentials.json")
        self.credentials_path = Path(
            str(expand_text(settings.get("credentials_path") or default_path))
        )
        self.refresh_seconds = max(60.0, float(settings.get("refresh_minutes", 5)) * 60.0)
        self.timeout = float(settings.get("timeout_seconds", 10))
        self._ssl_context = https_context()
        self._lock = threading.Lock()
        self._payload: dict[str, Any] | None = None
        self.version = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_state = ""
        # Automatic renewal: briefly start Claude Code (hidden) in a trusted,
        # empty folder; Claude Code renews its login when it starts - but only
        # once the access token has expired (or is about to). The log showed
        # that every attempt 20 minutes early failed and every attempt after
        # the expiry worked, so by default the bridge wakes up right at the
        # expiry and renews then (renew_before_minutes = 0).
        self.auto_renew = bool(settings.get("auto_renew", True))
        self.renew_folder = Path(
            str(expand_text(settings.get("renew_folder") or os.path.join("~", "claude-login")))
        )
        self.renew_before_seconds = max(0.0, float(settings.get("renew_before_minutes", 0)) * 60.0)
        self.renew_timeout = float(settings.get("renew_timeout_seconds", 45))
        self._last_renew_attempt = -1e9
        self._renew_failures = 0

    # -- helpers -------------------------------------------------------
    def read_expiry(self) -> float | None:
        """Expiry of the stored access token as a UNIX time (seconds)."""
        try:
            data = json.loads(self.credentials_path.read_text(encoding="utf-8"))
            expires_at = ((data or {}).get("claudeAiOauth") or {}).get("expiresAt")
            if isinstance(expires_at, (int, float)) and expires_at > 0:
                return expires_at / 1000.0
        except Exception:
            pass
        return None

    @staticmethod
    def claude_executable() -> str | None:
        candidates = [shutil.which("claude")]
        home_bin = Path(os.path.expanduser("~")) / ".local" / "bin"
        candidates += [str(home_bin / "claude.exe"), str(home_bin / "claude")]
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                return candidate
        return None

    def renew_login(self) -> bool:
        """Let Claude Code renew its access token. Returns True on success.

        Step 1 asks `claude auth status`. If the expiry did not move, step 2
        starts the interactive `claude` hidden in `renew_folder` (which must be
        trusted in Claude Code once), waits until the credentials file shows a
        new expiry and then closes it. No prompt is ever sent to Claude.
        """
        if not self.auto_renew:
            return False
        now = time.monotonic()
        if now - self._last_renew_attempt < self.renew_cooldown():
            return False  # 2 minutes apart, longer after repeated failures
        self._last_renew_attempt = now

        executable = self.claude_executable()
        if executable is None:
            log("Claude usage: Claude Code not found, cannot renew the login")
            return False
        before = self.read_expiry() or 0.0
        windows = platform.system() == "Windows"

        def renewed() -> bool:
            after = self.read_expiry()
            return after is not None and after > max(before, time.time() + 60)

        try:
            subprocess.run(
                [executable, "auth", "status"],
                capture_output=True,
                timeout=30,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW if windows else 0,
            )
        except Exception:
            pass
        if renewed():
            log("Claude usage: login renewed")
            return True

        if not self.renew_folder.is_dir():
            log(
                f"Claude usage: cannot renew automatically - folder {self.renew_folder} "
                "is missing (create it and run `claude` there once)"
            )
            return False

        log("Claude usage: renewing login by starting Claude Code in the background")
        kwargs: dict[str, Any] = {"cwd": str(self.renew_folder)}
        if windows:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0  # SW_HIDE
            kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
            kwargs["startupinfo"] = startupinfo
        try:
            process = subprocess.Popen([executable], **kwargs)
        except Exception as exc:
            log(f"Claude usage: could not start Claude Code ({type(exc).__name__})")
            return False

        ok = False
        deadline = time.monotonic() + self.renew_timeout
        try:
            while time.monotonic() < deadline:
                if renewed():
                    ok = True
                    time.sleep(3.0)  # let Claude Code finish writing its files
                    break
                if process.poll() is not None:
                    break
                time.sleep(1.0)
        finally:
            self._stop_process_tree(process)
        log("Claude usage: login renewed" if ok else "Claude usage: automatic renewal failed")
        self._renew_failures = 0 if ok else self._renew_failures + 1
        return ok

    RENEW_RETRY_SECONDS = (120.0, 120.0, 300.0, 900.0, 1800.0)

    def renew_cooldown(self) -> float:
        """Minimum time between two renewal attempts: 2 minutes, growing to
        30 minutes after repeated failures so a broken login does not start
        Claude Code over and over."""
        steps = self.RENEW_RETRY_SECONDS
        return steps[min(self._renew_failures, len(steps) - 1)]

    def next_delay(self, base: float) -> float:
        """Wait `base` seconds, but wake up right after the token expires (or
        `renew_before_minutes` earlier) so it is renewed without a gap."""
        expiry = self.read_expiry()
        if expiry is not None and self.auto_renew:
            until = expiry - self.renew_before_seconds - time.time()
            if 0 < until < base:
                return max(5.0, until + 10.0)
        return base

    @staticmethod
    def _stop_process_tree(process: subprocess.Popen[Any]) -> None:
        if process.poll() is not None:
            return
        if platform.system() == "Windows":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            process.terminate()
        try:
            process.wait(timeout=5)
        except Exception:
            pass

    def read_token(self) -> tuple[str | None, bool]:
        """Returns (access_token, expired)."""
        try:
            data = json.loads(self.credentials_path.read_text(encoding="utf-8"))
        except Exception:
            return None, False
        oauth = (data or {}).get("claudeAiOauth") or {}
        token = oauth.get("accessToken")
        expires_at = oauth.get("expiresAt")
        expired = False
        if isinstance(expires_at, (int, float)) and expires_at > 0:
            expired = expires_at / 1000.0 <= time.time()
        return (str(token) if token else None), expired

    def fetch_usage(self, token: str) -> tuple[int, Any, float]:
        """Returns (status, json_or_None, retry_after_seconds)."""
        request = urllib.request.Request(
            self.USAGE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
                "User-Agent": f"ESP32-PC-Control-Deck/{APP_VERSION}",
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=self._ssl_context
            ) as response:
                return int(response.status), json.loads(response.read().decode("utf-8")), 0.0
        except urllib.error.HTTPError as exc:
            retry_after = 0.0
            try:
                retry_after = float(exc.headers.get("Retry-After") or 0)
            except Exception:
                pass
            return int(exc.code), None, retry_after

    def _publish(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._payload = payload
            self.version += 1
        state = payload.get("state", "")
        if state != self._last_state:
            messages = {
                "ok": "Claude usage: connected",
                "login": "Claude usage: login expired - run `claude` once in a terminal",
                "nologin": f"Claude usage: no Claude Code login found at {self.credentials_path}",
                "error": "Claude usage: request failed (the endpoint is unofficial and may have changed)",
            }
            log(messages.get(state, f"Claude usage: {state}"))
            self._last_state = state

    def refresh(self) -> float:
        """One update. Returns the number of seconds to wait before the next."""
        token, expired = self.read_token()
        if token is None:
            self._publish({"type": "claude", "state": "nologin"})
            return self.refresh_seconds
        expiry = self.read_expiry()
        expiring_soon = expiry is not None and expiry - time.time() < self.renew_before_seconds
        if expired or expiring_soon:
            if self.renew_login():
                token, expired = self.read_token()
        if token is None or expired:
            self._publish({"type": "claude", "state": "login"})
            if self.auto_renew:
                # Try again as soon as the retry pause allows.
                wait = self.renew_cooldown() - (time.monotonic() - self._last_renew_attempt)
                return min(self.refresh_seconds, max(5.0, wait + 1.0))
            return self.refresh_seconds

        try:
            status, data, retry_after = self.fetch_usage(token)
            if status == 401:
                self.renew_login()
                new_token, _ = self.read_token()
                if new_token and new_token != token:
                    status, data, retry_after = self.fetch_usage(new_token)
            if status == 401:
                self._publish({"type": "claude", "state": "login"})
                return self.refresh_seconds
            if status == 429:
                return max(self.refresh_seconds, retry_after)
            if status != 200 or not isinstance(data, dict):
                self._publish({"type": "claude", "state": "error"})
                return self.refresh_seconds
            self._publish(parse_claude_usage(data))
        except Exception as exc:
            log(f"Claude usage update failed: {type(exc).__name__}")
            if self._payload is None:
                self._publish({"type": "claude", "state": "error"})
            return self.next_delay(60.0)
        return self.next_delay(self.refresh_seconds)

    def latest(self) -> tuple[int, dict[str, Any] | None]:
        with self._lock:
            return self.version, self._payload

    def _loop(self) -> None:
        while not self._stop.is_set():
            delay = self.refresh()
            self._stop.wait(delay)

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="claude-usage", daemon=True)
        self._thread.start()


# ------------------------------------------------------------------- disks
MAX_DISKS = 16


def windows_volume_label(mountpoint: str) -> str:
    if platform.system() != "Windows":
        return ""
    try:
        import ctypes

        buffer = ctypes.create_unicode_buffer(261)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(mountpoint), buffer, len(buffer),
            None, None, None, None, 0,
        )
        return buffer.value if ok else ""
    except Exception:
        return ""


def windows_physical_drive(mountpoint: str) -> str | None:
    """Map a drive letter (C:) to its psutil I/O key (PhysicalDrive0).

    Uses IOCTL_STORAGE_GET_DEVICE_NUMBER, which needs no administrator
    rights. Returns None for volumes that span several disks.
    """
    if platform.system() != "Windows":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class StorageDeviceNumber(ctypes.Structure):
            _fields_ = [
                ("DeviceType", wintypes.DWORD),
                ("DeviceNumber", wintypes.DWORD),
                ("PartitionNumber", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        kernel32.DeviceIoControl.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
            wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
        ]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        path = "\\\\.\\" + mountpoint.rstrip("\\/")
        handle = kernel32.CreateFileW(path, 0, 3, None, 3, 0, None)
        if not handle or handle == ctypes.c_void_p(-1).value:
            return None
        try:
            number = StorageDeviceNumber()
            returned = wintypes.DWORD()
            ok = kernel32.DeviceIoControl(
                handle, 0x2D1080, None, 0, ctypes.byref(number),
                ctypes.sizeof(number), ctypes.byref(returned), None,
            )
        finally:
            kernel32.CloseHandle(handle)
        return f"PhysicalDrive{number.DeviceNumber}" if ok else None
    except Exception:
        return None


def disk_entries(
    config: dict[str, Any],
    partitions: list[Any] | None = None,
    label_fn: Any = None,
    io_fn: Any = None,
) -> list[dict[str, Any]]:
    """Usage of every local drive, e.g. C:, D: (fixed and removable).

    io_fn(partition) may return (read_MBs, write_MBs) for the drive.
    """
    label_fn = label_fn or windows_volume_label
    disks_config = config.get("disks") or {}
    include_removable = bool(disks_config.get("include_removable", True))
    exclude = {str(item).upper().rstrip("\\/") for item in disks_config.get("exclude") or []}

    if partitions is None:
        try:
            partitions = psutil.disk_partitions(all=False)
        except Exception:
            return []

    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    gib = 1024.0 ** 3
    windows = platform.system() == "Windows"
    for part in partitions:
        mountpoint = str(getattr(part, "mountpoint", "") or "")
        options = str(getattr(part, "opts", "") or "").lower()
        if not mountpoint or "cdrom" in options or "remote" in options:
            continue
        if "removable" in options and not include_removable:
            continue
        if not getattr(part, "fstype", ""):
            continue
        name = mountpoint.rstrip("\\/") if windows else mountpoint
        name = name or mountpoint
        if name.upper() in exclude or name in seen:
            continue
        try:
            usage = psutil.disk_usage(mountpoint)
        except Exception:
            continue
        if usage.total <= 0:
            continue
        seen.add(name)
        label = label_fn(mountpoint) if windows else ""
        rates = io_fn(part) if io_fn is not None else None
        read_mbs, write_mbs = rates if rates else (None, None)
        entries.append(
            {
                "name": display_label(name, 10) if not windows else name.upper()[:10],
                "label": display_label(label, 10) if label else "",
                "used": safe_number(usage.percent, 1),
                "used_gb": safe_number(usage.used / gib, 1),
                "total_gb": safe_number(usage.total / gib, 1),
                "r": safe_number(read_mbs, 1),
                "w": safe_number(write_mbs, 1),
            }
        )
        if len(entries) >= MAX_DISKS:
            break
    return entries


class DiskMonitor:
    """Builds the `disks` frame with per-drive read/write speed."""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self._labels: dict[str, str] = {}
        self._io_keys: dict[str, str | None] = {}
        self._previous: dict[str, Any] | None = None
        self._previous_time = 0.0

    def _label(self, mountpoint: str) -> str:
        if mountpoint not in self._labels:
            self._labels[mountpoint] = windows_volume_label(mountpoint)
        return self._labels[mountpoint]

    def _io_key(self, part: Any) -> str | None:
        mountpoint = str(getattr(part, "mountpoint", "") or "")
        if mountpoint not in self._io_keys:
            if platform.system() == "Windows":
                key = windows_physical_drive(mountpoint)
            else:
                key = os.path.basename(str(getattr(part, "device", "") or "")) or None
            self._io_keys[mountpoint] = key
        return self._io_keys[mountpoint]

    def _sample_rates(self) -> dict[str, tuple[float, float]]:
        try:
            counters = psutil.disk_io_counters(perdisk=True) or {}
        except Exception:
            counters = {}
        now = time.monotonic()
        rates: dict[str, tuple[float, float]] = {}
        if self._previous is not None:
            elapsed = max(0.001, now - self._previous_time)
            for key, current in counters.items():
                before = self._previous.get(key)
                if before is None:
                    continue
                read = max(0, current.read_bytes - before.read_bytes) / elapsed / 1_000_000.0
                write = max(0, current.write_bytes - before.write_bytes) / elapsed / 1_000_000.0
                rates[key] = (read, write)
        self._previous = counters
        self._previous_time = now
        return rates

    def packet(self) -> dict[str, Any]:
        rates = self._sample_rates()

        def io_fn(part: Any) -> tuple[float, float] | None:
            key = self._io_key(part)
            return rates.get(key) if key else None

        return {
            "type": "disks",
            "items": disk_entries(self.config, label_fn=self._label, io_fn=io_fn),
        }


def disks_packet(config: dict[str, Any]) -> dict[str, Any]:
    return {"type": "disks", "items": disk_entries(config)}


def encode_frame(obj: dict[str, Any]) -> bytes:
    # UTF-8 (not \u escapes): Greek song titles stay half as long.
    payload = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
    return f"{PROTOCOL_PREFIX}{payload}\n".encode("utf-8")


# The main loop and the music worker's thread both write to the port.
SERIAL_WRITE_LOCK = threading.Lock()


def send_json(serial_port: serial.Serial, obj: dict[str, Any]) -> None:
    frame = encode_frame(obj)
    with SERIAL_WRITE_LOCK:
        serial_port.write(frame)
        serial_port.flush()


def parse_frame(text: str) -> dict[str, Any] | None:
    if text.startswith(PROTOCOL_PREFIX):
        payload = text[len(PROTOCOL_PREFIX) :]
    elif text.startswith("{"):
        payload = text
    else:
        return None
    try:
        message = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return message if isinstance(message, dict) else None


def build_lhm_reader(config: dict[str, Any]) -> LibreHardwareMonitorHttpReader:
    lhm_config = config.get("librehardwaremonitor", {}) or {}
    enabled = resolve_platform_value(lhm_config.get("enabled", True))
    return LibreHardwareMonitorHttpReader(
        enabled=bool(enabled),
        url=str(lhm_config.get("url", "http://127.0.0.1:8085/data.json")),
        timeout=float(lhm_config.get("timeout_seconds", 0.5)),
    )


class SerialReader(threading.Thread):
    """Reads the serial port on its own thread.

    Button presses (macro frames) are executed here straight away, so they no
    longer wait for the main loop, which spends most of every cycle collecting
    stats (measured ~1.4 s of every 2 s). Every other line is handed to the
    main loop through a queue and handled there exactly as before.
    """

    def __init__(self, serial_port: Any, config: dict[str, Any],
                 media: MediaService | None = None) -> None:
        super().__init__(name="serial-reader", daemon=True)
        self._port = serial_port
        self._config = config
        self._media = media
        self._queue: queue.Queue[str] = queue.Queue()
        # Not "_stop": threading.Thread already has a private _stop().
        self._stop_event = threading.Event()
        self.error: BaseException | None = None

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        buffer = bytearray()
        try:
            while not self._stop_event.is_set():
                # Returns as soon as a byte is there (or after the 50 ms timeout).
                chunk = self._port.read(self._port.in_waiting or 1)
                if not chunk:
                    continue
                buffer.extend(chunk)
                while b"\n" in buffer:
                    raw, _, remainder = buffer.partition(b"\n")
                    buffer = bytearray(remainder)
                    text = raw.decode("utf-8", errors="replace").strip()
                    if not text:
                        continue
                    message = parse_frame(text)
                    if message is not None and message.get("type") == "page":
                        if self._media is not None:
                            self._media.set_page(str(message.get("page", "")),
                                                 bool(message.get("awake", True)))
                        continue
                    if message is not None and message.get("type") == "media_key":
                        if self._media is not None:
                            self._media.media_key(str(message.get("key", "")))
                        continue
                    if message is not None and message.get("type") == "power":
                        execute_power(self._config, str(message.get("action", "")))
                        continue
                    if message is not None and message.get("type") == "macro":
                        try:
                            macro_id = int(message.get("id", 0))
                        except (TypeError, ValueError):
                            log(f"ESP: invalid macro frame {message}")
                            continue
                        execute_macro(self._config, macro_id)
                        continue
                    self._queue.put(text)
        except Exception as exc:  # port unplugged or closed
            if not self._stop_event.is_set():
                self.error = exc

    def _raise_if_failed(self) -> None:
        error = self.error
        if error is None:
            return
        if isinstance(error, (serial.SerialException, OSError)):
            raise error
        raise serial.SerialException(f"serial reader stopped: {error}")

    def lines(self, timeout: float) -> list[str]:
        """Non-macro lines received so far; waits up to `timeout` for the first.

        Raises the reader's error so the main loop reconnects as before.
        """
        self._raise_if_failed()
        items: list[str] = []
        try:
            items.append(self._queue.get(timeout=timeout))
            while True:
                items.append(self._queue.get_nowait())
        except queue.Empty:
            pass
        if not items:
            self._raise_if_failed()
        return items


def run(config: dict[str, Any]) -> None:
    baud = int(config.get("baud", 115200))
    interval = max(0.5, float(config.get("interval_seconds", 2.0)))
    disk_path = disk_path_from_config(config)
    lhm = build_lhm_reader(config)
    weather = WeatherService(config)
    weather.start()
    disk_monitor = DiskMonitor(config)
    claude_usage = ClaudeUsageService(config)
    claude_usage.start()
    top_apps = TopAppsService(config)
    top_apps.start()
    media = MediaService(config)
    media.start()

    psutil.cpu_percent(interval=None)
    previous = current_counters()
    configured_port = str(config.get("serial_port", "auto"))
    next_posix_port_message = 0.0
    last_posix_serial_error: tuple[str, str] | None = None

    while True:
        if configured_port and configured_port.lower() != "auto":
            ports: list[Any] = []
            port = configured_port
        else:
            ports = list(list_ports.comports())
            port = choose_serial_port(configured_port, ports)
        if not port:
            if platform.system() == "Windows":
                log("Control Deck CH340 not found. Retrying...")
            elif time.monotonic() >= next_posix_port_message:
                log("Control Deck CH340 not found. Retrying...")
                if ports:
                    log(
                        "Visible serial ports: "
                        + ", ".join(str(item.device) for item in ports)
                    )
                else:
                    log("No serial ports are currently visible.")
                log("Run the launcher with --list-ports for diagnostics.")
                next_posix_port_message = time.monotonic() + 10.0
            time.sleep(2.0)
            continue

        try:
            log(f"Opening {port} at {baud} baud")
            raw_log_window = 0.0
            raw_log_count = 0
            with serial.Serial(port, baud, timeout=0.05, write_timeout=1) as serial_port:
                last_posix_serial_error = None
                time.sleep(2.0)
                serial_port.reset_input_buffer()
                serial_port.reset_output_buffer()

                send_json(serial_port, macro_packet(config))
                last_send = 0.0
                sent_weather_version = -1
                sent_claude_version = -1
                last_weather_send = -1e9
                last_claude_send = -1e9
                reader = SerialReader(serial_port, config, media)
                reader.start()
                media.attach(lambda frame: send_json(serial_port, frame))

                try:
                    while True:
                        now = time.monotonic()
                        if now - last_send >= interval:
                            payload, previous = get_stats(previous, disk_path, lhm)
                            payload.update(top_apps.latest())
                            send_json(serial_port, payload)
                            time.sleep(0.05)
                            send_json(serial_port, disk_monitor.packet())
                            last_send = now

                        # Slow-changing frames go out between stats bursts so the
                        # ESP32 serial buffer is not flooded, and are repeated every
                        # minute in case one was lost (e.g. while the display was
                        # busy redrawing).
                        if 0.6 <= now - last_send <= interval - 0.3:
                            claude_version, claude_payload = claude_usage.latest()
                            if claude_payload is not None and (
                                claude_version != sent_claude_version
                                or now - last_claude_send >= 60.0
                            ):
                                send_json(serial_port, claude_payload)
                                sent_claude_version = claude_version
                                last_claude_send = now
                            else:
                                weather_version, weather_payload = weather.latest()
                                if weather_payload is not None and (
                                    weather_version != sent_weather_version
                                    or now - last_weather_send >= 60.0
                                ):
                                    send_json(serial_port, weather_payload)
                                    sent_weather_version = weather_version
                                    last_weather_send = now

                        for text in reader.lines(timeout=0.05):
                            message = parse_frame(text)
                            if message is None:
                                # The firmware only sends frames, so any other
                                # line is boot or crash output from the ESP32
                                # (reset reason, Guru Meditation, Backtrace).
                                now_raw = time.monotonic()
                                if now_raw - raw_log_window >= 60.0:
                                    raw_log_window = now_raw
                                    raw_log_count = 0
                                if raw_log_count < 60:
                                    raw_log_count += 1
                                    log(f"ESP output: {text[:300]}")
                                continue

                            message_type = message.get("type")
                            if message_type == "macro":
                                execute_macro(config, int(message.get("id", 0)))
                            elif message_type == "hello":
                                protocol = message.get("protocol", "?")
                                firmware = message.get("firmware", "?")
                                log(
                                    f"Connected to {message.get('device', 'ESP32')} "
                                    f"firmware {firmware}, protocol {protocol}"
                                )
                                if protocol != PROTOCOL_VERSION:
                                    log(
                                        f"Warning: bridge protocol {PROTOCOL_VERSION} "
                                        f"!= device protocol {protocol}"
                                    )
                                send_json(serial_port, macro_packet(config))
                                media.resend()
                                sent_weather_version = -1
                                sent_claude_version = -1
                                last_weather_send = -1e9
                                last_claude_send = -1e9
                            elif message_type == "reset_report":
                                trail = message.get("trail")
                                report = f"ESP last restart: {message.get('reason', '?')}"
                                if isinstance(trail, list) and trail:
                                    report += (
                                        f" after {message.get('uptime_s', '?')} s running;"
                                        " last steps (oldest first): "
                                        + " > ".join(str(step) for step in trail)
                                    )
                                log(report)
                                log(
                                    "ESP memory: psram now "
                                    f"{message.get('psram_now', '?')}, before restart "
                                    f"{message.get('psram_then', '?')}; free heap "
                                    f"{message.get('heap_free', '?')} bytes, largest block "
                                    f"{message.get('heap_largest', '?')} bytes"
                                )
                                crash = message.get("crash")
                                if isinstance(crash, dict):
                                    backtrace = crash.get("bt")
                                    if not isinstance(backtrace, list):
                                        backtrace = []
                                    log(
                                        f"ESP crash: task {crash.get('task', '?')}, "
                                        f"cause {crash.get('cause', '?')}, "
                                        f"address {crash.get('vaddr', '?')}, "
                                        f"pc {crash.get('pc', '?')}, "
                                        f"firmware elf {crash.get('elf', '?')}, "
                                        f"backtrace{' (corrupted)' if crash.get('bt_corrupted') else ''}: "
                                        + " ".join(str(item) for item in backtrace)
                                    )
                            elif message_type == "error":
                                log(
                                    f"ESP protocol error: {message.get('code', 'unknown')} "
                                    f"({message.get('detail', '')})"
                                )
                            elif message_type not in {"pong", "rebooting"}:
                                log(f"ESP: {message}")

                        time.sleep(0.01)
                finally:
                    media.attach(None)
                    reader.stop()
                    reader.join(timeout=1.0)
        except (serial.SerialException, OSError) as exc:
            if platform.system() == "Windows":
                log(f"Serial disconnected: {exc}")
            else:
                error_key = (port, str(exc))
                if error_key != last_posix_serial_error:
                    log(f"Serial disconnected: {exc}")
                    message = str(exc).lower()
                    if platform.system() == "Linux" and (
                        "permission denied" in message or "errno 13" in message
                    ):
                        log(
                            "Linux permission hint: check the device group with "
                            f"'ls -l {port}' and add your user to that group, commonly dialout."
                        )
                    elif platform.system() == "Darwin" and port.startswith("/dev/tty."):
                        log(
                            "macOS hint: prefer the matching /dev/cu.* port in config.json."
                        )
                    last_posix_serial_error = error_key
            time.sleep(2.0)


def main() -> None:
    parser = argparse.ArgumentParser(description="ESP32 PC Control Deck host bridge")
    parser.add_argument(
        "--config",
        default=str(Path(__file__).with_name("config.json")),
        help="Path to config.json",
    )
    parser.add_argument(
        "--list-temperatures",
        action="store_true",
        help="List temperature sensors exposed by LibreHardwareMonitor and exit",
    )
    parser.add_argument(
        "--list-ports",
        action="store_true",
        help="List serial ports, show the selected Control Deck port and exit",
    )
    parser.add_argument(
        "--test-macro",
        type=int,
        metavar="ID",
        help="Run one button action (1-8) from config.json without the display and exit",
    )
    parser.add_argument(
        "--find-app",
        metavar="NAME",
        help="Search the Windows Start menu for an app name and print its AppID",
    )
    parser.add_argument(
        "--list-disks",
        action="store_true",
        help="Print the drives that the DISKS page will show and exit",
    )
    parser.add_argument(
        "--test-claude-usage",
        action="store_true",
        help="Fetch Claude plan usage once, print it and exit",
    )
    parser.add_argument(
        "--compare-weather",
        action="store_true",
        help="Show the current weather from several forecast models side by side and exit",
    )
    parser.add_argument(
        "--test-weather",
        action="store_true",
        help="Fetch weather once for the configured locations, print it and exit",
    )
    parser.add_argument(
        "--test-media",
        action="store_true",
        help="Windows: show the current song, volume and sound bars for a few seconds and exit",
    )
    parser.add_argument("--top-apps-worker", type=float, metavar="SECONDS", help=argparse.SUPPRESS)
    parser.add_argument("--media-worker", type=float, metavar="FPS", help=argparse.SUPPRESS)
    parser.add_argument("--volume-step", type=int, default=5, help=argparse.SUPPRESS)
    parser.add_argument("--parent-pid", type=int, default=0, help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.top_apps_worker is not None:
        run_top_apps_worker(max(2.0, args.top_apps_worker), args.parent_pid)
        raise SystemExit(0)

    if args.media_worker is not None:
        run_media_worker(max(5.0, min(30.0, args.media_worker)), args.parent_pid,
                         max(1, min(20, args.volume_step)))
        raise SystemExit(0)

    if args.find_app:
        pairs = windows_start_apps(args.find_app)
        if not pairs:
            print("No Start menu entries matched.")
            raise SystemExit(1)
        for app_name, app_id in pairs:
            print(f"{app_name}\t{app_id}")
        raise SystemExit(0)

    config_path = Path(args.config).resolve()
    if not config_path.exists():
        example = Path(__file__).with_name("config.example.json")
        print(
            f"Config not found: {config_path}\nCopy {example.name} to config.json and edit it.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    config = load_config(config_path)

    global LOG_FILE
    log_name = str(config.get("log_file", "control-deck.log") or "").strip()
    if log_name:
        LOG_FILE = (config_path.parent / log_name).resolve()

    if args.list_temperatures:
        raise SystemExit(build_lhm_reader(config).print_temperature_sensors())
    if args.list_ports:
        raise SystemExit(print_serial_ports(str(config.get("serial_port", "auto"))))
    if args.test_media:
        raise SystemExit(test_media(config))
    if args.test_claude_usage:
        service = ClaudeUsageService(config)
        service.refresh()
        print(json.dumps(service.latest()[1], ensure_ascii=False, indent=2))
        raise SystemExit(0)
    if args.list_disks:
        monitor = DiskMonitor(config)
        monitor.packet()
        time.sleep(1.0)
        print(json.dumps(monitor.packet(), ensure_ascii=False, indent=2))
        raise SystemExit(0)
    if args.test_macro is not None:
        execute_macro(config, args.test_macro)
        time.sleep(1.0)
        raise SystemExit(0)
    if args.compare_weather:
        print(WeatherService(config).compare_models())
        raise SystemExit(0)
    if args.test_weather:
        service = WeatherService(config)
        if not service.enabled:
            print("Weather is disabled or has no locations in config.json")
            raise SystemExit(2)
        ok = service.refresh()
        print(json.dumps(service.latest()[1], ensure_ascii=False, indent=2))
        raise SystemExit(0 if ok else 1)

    log(f"PC Control Deck Bridge {APP_VERSION} on {platform.system()} {platform.release()}")
    run(config)


if __name__ == "__main__":
    main()
