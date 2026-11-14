import importlib.util
import json
import re
import sys
import time
import unittest
import unittest.mock
from dataclasses import dataclass
from unittest.mock import patch
from typing import Optional

import types

if "serial" not in sys.modules:
    serial_stub = types.ModuleType("serial")
    serial_stub.SerialException = OSError
    serial_stub.Serial = object
    tools_stub = types.ModuleType("serial.tools")
    list_ports_stub = types.ModuleType("serial.tools.list_ports")
    list_ports_stub.comports = lambda: []
    tools_stub.list_ports = list_ports_stub
    serial_stub.tools = tools_stub
    sys.modules["serial"] = serial_stub
    sys.modules["serial.tools"] = tools_stub
    sys.modules["serial.tools.list_ports"] = list_ports_stub

from pathlib import Path

BRIDGE_PATH = Path(__file__).resolve().parents[1] / "control_deck_bridge.py"
SPEC = importlib.util.spec_from_file_location("control_deck_bridge", BRIDGE_PATH)
bridge = importlib.util.module_from_spec(SPEC)
sys.modules["control_deck_bridge"] = bridge
assert SPEC.loader is not None
SPEC.loader.exec_module(bridge)


class TemperatureParsingTests(unittest.TestCase):
    def setUp(self):
        self.sample = {
            "Text": "Sensor",
            "Children": [
                {
                    "Text": "AMD Ryzen 9 5900X",
                    "Children": [
                        {
                            "Text": "Temperatures",
                            "Children": [
                                {
                                    "Text": "CPU Package",
                                    "Value": "57.0 C",
                                    "SensorId": "/amdcpu/0/temperature/0",
                                    "Children": [],
                                },
                                {
                                    "Text": "Core Average",
                                    "Value": "52.0 C",
                                    "SensorId": "/amdcpu/0/temperature/1",
                                    "Children": [],
                                },
                            ],
                        }
                    ],
                },
                {
                    "Text": "NVIDIA GeForce",
                    "Children": [
                        {
                            "Text": "Temperatures",
                            "Children": [
                                {
                                    "Text": "GPU Core",
                                    "Value": "48.0 C",
                                    "SensorId": "/nvidiagpu/0/temperature/0",
                                    "Children": [],
                                }
                            ],
                        }
                    ],
                },
                {
                    "Text": "Samsung NVMe",
                    "Children": [
                        {
                            "Text": "Temperatures",
                            "Children": [
                                {
                                    "Text": "Composite",
                                    "Value": "41.0 C",
                                    "SensorId": "/storage/nvme/0/temperature/0",
                                    "Children": [],
                                }
                            ],
                        }
                    ],
                },
                {
                    "Text": "ASUS Mainboard",
                    "Children": [
                        {
                            "Text": "Temperatures",
                            "Children": [
                                {
                                    "Text": "Motherboard",
                                    "Value": "34.0 C",
                                    "SensorId": "/lpc/nct/0/temperature/0",
                                    "Children": [],
                                }
                            ],
                        }
                    ],
                },
            ],
        }

    def test_selects_expected_temperature_sources(self):
        sensors = bridge.LibreHardwareMonitorHttpReader._temperature_sensors(self.sample)
        expected = {
            "cpu": ("CPU Package", 57.0),
            "gpu": ("GPU Core", 48.0),
            "disk": ("Composite", 41.0),
            "system": ("Motherboard", 34.0),
        }
        for kind, (label, value) in expected.items():
            selected = bridge.LibreHardwareMonitorHttpReader._select(sensors, kind)
            self.assertIsNotNone(selected)
            self.assertEqual(label, selected.label)
            self.assertEqual(value, selected.value)


class ProtocolTests(unittest.TestCase):
    def test_config_packet_carries_icons(self):
        packet = bridge.macro_packet({"macros": [
            {"id": 1, "label": "A", "icon": "Claude"},
            {"id": 2, "label": "B"},
        ]})
        self.assertEqual({"id": 1, "label": "A", "icon": "claude"}, packet["macros"][0])
        self.assertNotIn("icon", packet["macros"][1])

    def test_config_icon_names_exist_in_firmware(self):
        header = (Path(__file__).resolve().parents[2] / "src" / "button_icons.h").read_text(encoding="utf-8")
        available = set(re.findall(r'\{"(\w+)", \d+, \d+, ICON_', header))
        config = json.loads((Path(__file__).resolve().parents[1] / "config.example.json").read_text(encoding="utf-8"))
        for item in config["macros"]:
            self.assertIn(item["icon"], available)

    def test_config_packet_has_eight_buttons(self):
        packet = bridge.macro_packet({"macros": [
            {"id": i, "label": f"B{i}"} for i in range(0, 10)
        ]})
        self.assertEqual(list(range(1, 9)), [item["id"] for item in packet["macros"]])

    def test_config_packet_carries_screen_sleep(self):
        self.assertEqual(120, bridge.macro_packet({})["sleep_s"])
        self.assertEqual(300, bridge.macro_packet({"screen_sleep_minutes": 5})["sleep_s"])
        self.assertEqual(0, bridge.macro_packet({"screen_sleep_minutes": 0})["sleep_s"])

    def test_frame_encoding(self):
        frame = bridge.encode_frame({"type": "stats", "clock_seconds": 3600})
        self.assertEqual(b'DD:{"type":"stats","clock_seconds":3600}\n', frame)

    def test_default_top_row(self):
        config_path = Path(__file__).resolve().parents[1] / "config.example.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        labels = [item["label"] for item in bridge.macro_packet(config)["macros"]]
        self.assertEqual(
            ["Claude", "Android Studio", "Proton VPN", "Telegram", "PowerShell", "Task Manager",
             "O-KAM PRO", "Lock PC"],
            labels,
        )


@dataclass
class FakePort:
    device: str
    description: str = ""
    manufacturer: str = ""
    vid: Optional[int] = None
    pid: Optional[int] = None
    product: str = ""
    interface: str = ""
    hwid: str = ""


class SerialPortSelectionTests(unittest.TestCase):
    def test_explicit_port_is_never_replaced(self):
        selected = bridge.choose_serial_port("/dev/cu.manual", [])
        self.assertEqual("/dev/cu.manual", selected)

    def test_macos_prefers_callout_device_over_tty_device(self):
        ports = [
            FakePort(
                "/dev/tty.wchusbserial1420",
                description="USB2.0-Serial",
                manufacturer="wch.cn",
                vid=0x1A86,
                pid=0x7523,
            ),
            FakePort(
                "/dev/cu.wchusbserial1420",
                description="USB2.0-Serial",
                manufacturer="wch.cn",
                vid=0x1A86,
                pid=0x7523,
            ),
        ]
        with patch.object(bridge.platform, "system", return_value="Darwin"):
            with patch.object(bridge, "log"):
                selected = bridge.choose_serial_port("auto", ports)
        self.assertEqual("/dev/cu.wchusbserial1420", selected)

    def test_macos_recognizes_usbserial_without_vid_metadata(self):
        ports = [
            FakePort("/dev/cu.Bluetooth-Incoming-Port", description="Bluetooth-Incoming-Port"),
            FakePort("/dev/cu.usbserial-110", description="USB Serial"),
        ]
        with patch.object(bridge.platform, "system", return_value="Darwin"):
            selected = bridge.choose_serial_port("auto", ports)
        self.assertEqual("/dev/cu.usbserial-110", selected)

    def test_linux_recognizes_ttyusb_without_vid_metadata(self):
        ports = [
            FakePort("/dev/ttyS0", description="serial"),
            FakePort("/dev/ttyUSB0", description="USB2.0-Serial"),
        ]
        with patch.object(bridge.platform, "system", return_value="Linux"):
            selected = bridge.choose_serial_port("auto", ports)
        self.assertEqual("/dev/ttyUSB0", selected)

    def test_windows_keeps_original_first_match_behavior(self):
        ports = [
            FakePort("COM7", description="USB-SERIAL CH340", vid=0x1A86),
            FakePort("COM9", description="USB-SERIAL CH340", vid=0x1A86),
        ]
        with patch.object(bridge.platform, "system", return_value="Windows"):
            with patch.object(bridge, "log"):
                selected = bridge.choose_serial_port("auto", ports)
        self.assertEqual("COM7", selected)


class PlatformConfigurationTests(unittest.TestCase):
    def setUp(self):
        config_path = Path(__file__).resolve().parents[1] / "config.example.json"
        self.config = json.loads(config_path.read_text(encoding="utf-8"))

    def test_librehardwaremonitor_stays_enabled_on_windows(self):
        with patch.object(bridge.platform, "system", return_value="Windows"):
            reader = bridge.build_lhm_reader(self.config)
        self.assertTrue(reader.enabled)

    def test_librehardwaremonitor_is_disabled_by_default_on_linux_and_macos(self):
        for system in ("Linux", "Darwin"):
            with self.subTest(system=system):
                with patch.object(bridge.platform, "system", return_value=system):
                    reader = bridge.build_lhm_reader(self.config)
                self.assertFalse(reader.enabled)

    def test_platform_specific_macro_values_are_resolved(self):
        monitor = next(item for item in self.config["macros"] if item["id"] == 6)
        with patch.object(bridge.platform, "system", return_value="Darwin"):
            self.assertEqual(
                ["open", "-a", "Activity Monitor"],
                bridge.resolve_platform_value(monitor["value"]),
            )
        with patch.object(bridge.platform, "system", return_value="Linux"):
            self.assertEqual(
                ["gnome-system-monitor"],
                bridge.resolve_platform_value(monitor["value"]),
            )

    def test_custom_buttons_use_app_action_on_windows(self):
        with patch.object(bridge.platform, "system", return_value="Windows"):
            for macro_id, start_menu in ((1, "Claude"), (2, "Android Studio"), (3, "Proton VPN"),
                                     (4, "Telegram")):
                item = next(m for m in self.config["macros"] if m["id"] == macro_id)
                self.assertEqual("app", item["action"])
                spec = bridge.resolve_platform_value(item["value"])
                self.assertEqual(start_menu, spec["start_menu"])
                self.assertTrue(spec["paths"])


    def test_windows_system_macro_commands_remain_unchanged(self):
        expected = {
            6: ["taskmgr.exe"],
            8: ["rundll32.exe", "user32.dll,LockWorkStation"],
        }
        with patch.object(bridge.platform, "system", return_value="Windows"):
            actual = {
                item["id"]: bridge.resolve_platform_value(item.get("value"))
                for item in self.config["macros"]
                if item["id"] in expected
            }
        self.assertEqual(expected, actual)

class AppLaunchTests(unittest.TestCase):
    def test_choose_start_app_prefers_exact_name(self):
        pairs = [("Claude Code Helper", "x!helper"), ("Claude", "Claude_abc!Claude")]
        self.assertEqual("Claude_abc!Claude", bridge.choose_start_app("claude", pairs))

    def test_choose_start_app_falls_back_to_shortest(self):
        pairs = [("Proton VPN Beta Channel", "beta"), ("Proton VPN Client", "client")]
        self.assertEqual("client", bridge.choose_start_app("Proton", pairs))
        self.assertIsNone(bridge.choose_start_app("x", []))

    def test_first_existing_path_is_started_with_args(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as folder:
            exe = Path(folder) / "v2" / "app.exe"
            exe.parent.mkdir()
            exe.write_text("")
            item = {
                "action": "app",
                "value": {"default": {
                    "paths": [str(Path(folder) / "missing.exe"), str(Path(folder) / "v*" / "app.exe")],
                    "args": ["-NoLogo"],
                }},
            }
            with patch.object(bridge.platform, "system", return_value="Plan9"), \
                    patch.object(bridge.subprocess, "Popen") as popen:
                target = bridge.launch_app(item)
            self.assertEqual(str(exe), target)
            self.assertEqual([str(exe), "-NoLogo"], popen.call_args[0][0])

    def test_list_value_is_command_with_arguments(self):
        item = {"action": "app", "value": {"default": ["python3", "-c", "pass"]}}
        with patch.object(bridge.platform, "system", return_value="Plan9"), \
                patch.object(bridge.shutil, "which", return_value="/usr/bin/python3"), \
                patch.object(bridge.subprocess, "Popen") as popen:
            bridge.launch_app(item)
        self.assertEqual(["/usr/bin/python3", "-c", "pass"], popen.call_args[0][0])

    def test_elevation_required_falls_back_to_shell_execute(self):
        class ElevationError(OSError):
            winerror = 740

        with patch.object(bridge.subprocess, "Popen", side_effect=ElevationError("740")), \
                patch.object(bridge, "shell_execute") as shell_execute, \
                patch.object(bridge, "log"):
            result = bridge.popen_or_elevate(["taskmgr.exe"], cwd=None)
        self.assertIsNone(result)
        shell_execute.assert_called_once_with(["taskmgr.exe"], None)

    def test_other_os_errors_are_not_swallowed(self):
        with patch.object(bridge.subprocess, "Popen", side_effect=FileNotFoundError("x")):
            with self.assertRaises(FileNotFoundError):
                bridge.popen_or_elevate(["missing.exe"])

    def test_missing_app_raises(self):
        item = {"action": "app", "value": {"default": {"paths": ["/nope/none.exe"]}}}
        with patch.object(bridge.platform, "system", return_value="Plan9"):
            with self.assertRaises(FileNotFoundError):
                bridge.launch_app(item)


class WeatherTests(unittest.TestCase):
    SAMPLE = {
        "current": {
            "temperature_2m": 23.44,
            "relative_humidity_2m": 61,
            "apparent_temperature": 22.6,
            "is_day": 1,
            "weather_code": 2,
            "wind_speed_10m": 12.7,
        },
        "daily": {
            "weather_code": [3],
            "temperature_2m_max": [27.2],
            "temperature_2m_min": [17.8],
            "precipitation_probability_max": [20],
        },
    }

    def test_greek_names_are_transliterated(self):
        self.assertEqual("ATHINA", bridge.display_label("Αθήνα"))
        self.assertEqual("THESSALONIKI", bridge.display_label("Θεσσαλονίκη"))
        self.assertEqual("IRAKLEIO", bridge.display_label("Ηράκλειο"))
        self.assertEqual("PATRA", bridge.display_label("Πάτρα"))
        self.assertEqual("ZURICH", bridge.display_label("Zürich"))

    def test_parse_open_meteo(self):
        entry = bridge.parse_open_meteo("ATHINA", self.SAMPLE)
        self.assertEqual(
            {"name": "ATHINA", "temp": 23.4, "feels": 23.0, "code": 2, "day": 1,
             "hum": 61, "wind": 13, "tmax": 27.0, "tmin": 18.0, "rain": 20},
            entry,
        )

    def test_refresh_builds_frame_and_geocodes_names(self):
        config = {"weather": {"enabled": True, "locations": [
            {"name": "Πάτρα"},
            {"label": "Home", "latitude": 1, "longitude": 2},
        ]}}
        service = bridge.WeatherService(config)
        calls = []

        def fake_get(url, params):
            calls.append(url)
            if url == service.GEOCODING_URL:
                return {"results": [{"name": "Πάτρα", "latitude": 38.2, "longitude": 21.7}]}
            return self.SAMPLE

        with patch.object(service, "_get_json", side_effect=fake_get), patch.object(bridge, "log"):
            self.assertTrue(service.refresh())
            version, payload = service.latest()
            self.assertEqual(1, version)
            self.assertEqual("weather", payload["type"])
            self.assertEqual(["PATRA", "HOME"], [e["name"] for e in payload["locations"]])
            frame = bridge.encode_frame(payload)
            self.assertLess(len(frame), 2048)
            # Geocoding result is cached on the second refresh.
            service.refresh()
        self.assertEqual(1, calls.count(service.GEOCODING_URL))

    def test_failed_location_keeps_previous_entry(self):
        config = {"weather": {"enabled": True, "locations": [{"label": "A", "latitude": 1, "longitude": 1}]}}
        service = bridge.WeatherService(config)
        with patch.object(bridge, "log"):
            with patch.object(service, "_get_json", return_value=self.SAMPLE):
                service.refresh()
            with patch.object(service, "_get_json", side_effect=OSError("offline")):
                self.assertFalse(service.refresh())
        self.assertEqual("A", service.latest()[1]["locations"][0]["name"])

    def test_model_parameter_and_comparison(self):
        config = {"weather": {"enabled": True, "model": "icon_eu",
                              "locations": [{"label": "A", "latitude": 1, "longitude": 2}]}}
        service = bridge.WeatherService(config)
        seen = []

        def fake_get(url, params):
            seen.append(params.get("models"))
            if params.get("models") == "gem_seamless":
                raise OSError("400")
            return self.SAMPLE

        with patch.object(service, "_get_json", side_effect=fake_get):
            service.refresh()
            self.assertEqual(["icon_eu"], seen)
            table = service.compare_models()
        self.assertIn(None, seen)  # best_match sends no "models" parameter
        self.assertIn("ECMWF IFS (Europe)", table)
        self.assertIn("23.4C", table)
        self.assertIn("not available", table)

    def test_weather_disabled_without_locations(self):
        self.assertFalse(bridge.WeatherService({"weather": {"enabled": True}}).enabled)
        self.assertFalse(bridge.WeatherService({}).enabled)



@dataclass
class FakePartition:
    device: str
    mountpoint: str
    fstype: str = "NTFS"
    opts: str = "rw,fixed"


@dataclass
class FakeUsage:
    total: int
    used: int
    free: int
    percent: float


class DiskTests(unittest.TestCase):
    GIB = 1024 ** 3

    def usage(self, mountpoint):
        sizes = {"C:\\": (931, 412), "D:\\": (1863, 1500), "E:\\": (58, 3)}
        total, used = sizes[mountpoint]
        return FakeUsage(total * self.GIB, used * self.GIB, (total - used) * self.GIB,
                         round(used * 100 / total, 1))

    def partitions(self):
        return [
            FakePartition("C:\\", "C:\\"),
            FakePartition("D:\\", "D:\\"),
            FakePartition("E:\\", "E:\\", "FAT32", "rw,removable"),
            FakePartition("F:\\", "F:\\", "", "cdrom"),
            FakePartition("Z:\\", "Z:\\", "NTFS", "rw,remote"),
        ]

    def entries(self, config):
        with patch.object(bridge.platform, "system", return_value="Windows"), \
                patch.object(bridge.psutil, "disk_usage", side_effect=self.usage), \
                patch.object(bridge, "windows_volume_label", side_effect=lambda m: {"C:\\": "Σύστημα", "D:\\": "Data"}.get(m, "")):
            return bridge.disk_entries(config, self.partitions())

    def test_lists_local_drives_and_skips_cdrom_and_network(self):
        entries = self.entries({})
        self.assertEqual(["C:", "D:", "E:"], [e["name"] for e in entries])
        self.assertEqual("SYSTIMA", entries[0]["label"])
        self.assertEqual({"name": "D:", "label": "DATA", "used": 80.5, "used_gb": 1500.0,
                          "total_gb": 1863.0, "r": None, "w": None}, entries[1])

    def test_exclude_and_removable_options(self):
        entries = self.entries({"disks": {"exclude": ["d:"], "include_removable": False}})
        self.assertEqual(["C:"], [e["name"] for e in entries])

    def test_per_drive_read_write_rates(self):
        @dataclass
        class Io:
            read_bytes: int
            write_bytes: int

        monitor = bridge.DiskMonitor({})
        samples = [
            {"PhysicalDrive0": Io(0, 0), "PhysicalDrive1": Io(0, 0)},
            {"PhysicalDrive0": Io(20_000_000, 2_000_000), "PhysicalDrive1": Io(0, 500_000)},
        ]
        times = [100.0, 102.0]
        drives = {"C:\\": "PhysicalDrive0", "D:\\": "PhysicalDrive1", "E:\\": None}
        with patch.object(bridge.platform, "system", return_value="Windows"), \
                patch.object(bridge.psutil, "disk_io_counters", side_effect=samples), \
                patch.object(bridge.psutil, "disk_partitions", return_value=self.partitions()), \
                patch.object(bridge.psutil, "disk_usage", side_effect=self.usage), \
                patch.object(bridge.time, "monotonic", side_effect=times), \
                patch.object(bridge, "windows_volume_label", return_value=""), \
                patch.object(bridge, "windows_physical_drive", side_effect=lambda m: drives[m]):
            first = monitor.packet()
            second = monitor.packet()
        self.assertIsNone(first["items"][0]["r"])
        by_name = {item["name"]: item for item in second["items"]}
        self.assertEqual((10.0, 1.0), (by_name["C:"]["r"], by_name["C:"]["w"]))
        self.assertEqual((0.0, 0.2), (by_name["D:"]["r"], by_name["D:"]["w"]))
        self.assertIsNone(by_name["E:"]["r"])

    def test_packet_fits_serial_line(self):
        with patch.object(bridge, "disk_entries", return_value=[
            {"name": "C:", "label": "ABCDEFGHIJ", "used": 99.9, "used_gb": 9999.9,
             "total_gb": 9999.9, "r": 9999.9, "w": 9999.9}
        ] * bridge.MAX_DISKS):
            frame = bridge.encode_frame(bridge.disks_packet({}))
        self.assertLess(len(frame), 2048)



class ProcessTests(unittest.TestCase):
    class FakeProcess:
        def __init__(self, pid, name, cpu, rss_gb):
            self.pid = pid
            self.info = {"name": name, "memory_info": types.SimpleNamespace(rss=int(rss_gb * 1024 ** 3))}
            self._cpu = cpu

        def cpu_percent(self, interval=None):
            return self._cpu

    def test_groups_by_name_and_normalises_cpu(self):
        procs = [
            self.FakeProcess(0, "System Idle Process", 700, 0),
            self.FakeProcess(10, "chrome.exe", 80, 0.5),
            self.FakeProcess(11, "chrome.exe", 40, 0.75),
            self.FakeProcess(12, "Code.exe", 100, 2.0),
            self.FakeProcess(13, "studio64.exe", 20, 1.0),
        ]
        with patch.object(bridge.psutil, "cpu_count", return_value=8):
            monitor = bridge.ProcessMonitor()
        with patch.object(bridge.psutil, "process_iter", return_value=procs):
            result = monitor.sample()
        self.assertEqual("chrome", result["top_cpu_name"])
        self.assertEqual(15.0, result["top_cpu"])
        self.assertEqual("Code", result["top_ram_name"])
        self.assertEqual(2.0, result["top_ram_gb"])

    def test_process_name_is_ascii_without_exe(self):
        self.assertEqual("Proton VPN", bridge.process_display_name("Proton VPN.EXE"))
        self.assertEqual("cafe", bridge.process_display_name("café.exe"))


class GpuTests(unittest.TestCase):
    EXPECTED = {"gpu": 37.0, "vram": 25.0, "gpu_temp": 55.0, "vram_used_gb": 2.0, "vram_total_gb": 8.0}

    def setUp(self):
        bridge._nvml_handle = None
        bridge._nvml_retry_at = 0.0

    def fake_nvml(self):
        mib = 1024 ** 2
        return types.SimpleNamespace(
            NVML_TEMPERATURE_GPU=0,
            nvmlMemory_v2=object(),
            nvmlInit=lambda: None,
            nvmlShutdown=lambda: None,
            nvmlDeviceGetHandleByIndex=lambda index: "gpu0",
            nvmlDeviceGetUtilizationRates=lambda handle: types.SimpleNamespace(gpu=37),
            nvmlDeviceGetMemoryInfo=lambda handle, version=None: types.SimpleNamespace(
                used=2048 * mib, total=8192 * mib),
            nvmlDeviceGetTemperature=lambda handle, sensor: 55,
        )

    def test_nvml_reads_gpu_without_starting_nvidia_smi(self):
        with patch.object(bridge, "pynvml", self.fake_nvml()), \
                patch.object(bridge.subprocess, "run") as run, patch.object(bridge, "log"):
            result = bridge.read_nvidia_gpu()
        run.assert_not_called()
        self.assertEqual(self.EXPECTED, result)

    def test_nvml_failure_returns_empty_without_nvidia_smi(self):
        nvml = self.fake_nvml()

        def fail():
            raise RuntimeError("no driver")

        nvml.nvmlInit = fail
        with patch.object(bridge, "pynvml", nvml), \
                patch.object(bridge.subprocess, "run") as run, patch.object(bridge, "log"):
            result = bridge.read_nvidia_gpu()
        run.assert_not_called()
        self.assertEqual(bridge._empty_gpu_result(), result)

    def test_falls_back_to_nvidia_smi_without_library(self):
        completed = types.SimpleNamespace(returncode=0, stdout="37, 2048, 8192, 55\n")
        with patch.object(bridge, "pynvml", None), \
                patch.object(bridge.shutil, "which", return_value="nvidia-smi"), \
                patch.object(bridge.subprocess, "run", return_value=completed) as run, \
                patch.object(bridge, "log"):
            result = bridge.read_nvidia_gpu()
        run.assert_called_once()
        self.assertEqual(self.EXPECTED, result)



class ClaudeUsageTests(unittest.TestCase):
    RESPONSE = {
        "five_hour": {"utilization": 42.4, "resets_at": "2026-10-25T12:30:00+00:00"},
        "seven_day": {"utilization": 18.0, "resets_at": "2026-10-27T08:00:00+00:00"},
    }

    def service(self, folder, expires_ms):
        path = Path(folder) / ".credentials.json"
        path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok", "expiresAt": expires_ms}}),
                        encoding="utf-8")
        return bridge.ClaudeUsageService({"claude_usage": {"enabled": True, "credentials_path": str(path)}})

    def test_parse_usage(self):
        frame = bridge.parse_claude_usage(self.RESPONSE)
        self.assertEqual("claude", frame["type"])
        self.assertEqual(("ok", 42.0, 18.0), (frame["state"], frame["s"], frame["w"]))
        self.assertRegex(frame["s_reset"], r"^[0-9]{2}:[0-9]{2}$")
        self.assertIn(frame["w_reset"], bridge.WEEKDAYS)

    def test_refresh_ok(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            service = self.service(folder, (time.time() + 3600) * 1000)
            with patch.object(service, "fetch_usage", return_value=(200, self.RESPONSE, 0)) as fetch, \
                    patch.object(bridge, "log"):
                service.refresh()
            fetch.assert_called_once_with("tok")
            self.assertEqual(42.0, service.latest()[1]["s"])

    def test_expired_login_reports_login_state(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            service = self.service(folder, (time.time() - 60) * 1000)
            with patch.object(service, "renew_login", return_value=False) as nudge, \
                    patch.object(service, "fetch_usage") as fetch, patch.object(bridge, "log"):
                service.refresh()
            nudge.assert_called_once()
            fetch.assert_not_called()
            self.assertEqual("login", service.latest()[1]["state"])

    def test_token_expiring_soon_waits_for_expiry(self):
        # Claude Code only renews an expired token, so the bridge uses the
        # token until it expires and wakes up right after that.
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            service = self.service(folder, (time.time() + 120) * 1000)  # 2 minutes left
            with patch.object(service, "renew_login", return_value=True) as renew, \
                    patch.object(service, "fetch_usage", return_value=(200, self.RESPONSE, 0)), \
                    patch.object(bridge, "log"):
                delay = service.refresh()
            renew.assert_not_called()
            self.assertEqual("ok", service.latest()[1]["state"])
            self.assertAlmostEqual(130.0, delay, delta=3.0)

    def test_renew_before_minutes_is_still_honoured(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".credentials.json"
            path.write_text(json.dumps({"claudeAiOauth": {
                "accessToken": "tok", "expiresAt": (time.time() + 120) * 1000}}), encoding="utf-8")
            service = bridge.ClaudeUsageService({"claude_usage": {
                "enabled": True, "credentials_path": str(path), "renew_before_minutes": 3}})
            with patch.object(service, "renew_login", return_value=True) as renew, \
                    patch.object(service, "fetch_usage", return_value=(200, self.RESPONSE, 0)), \
                    patch.object(bridge, "log"):
                service.refresh()
            renew.assert_called_once()

    def test_failed_renewal_retries_after_two_minutes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            service = self.service(folder, (time.time() - 60) * 1000)
            fake = unittest.mock.MagicMock()
            fake.poll.return_value = 0  # Claude Code exits without renewing
            with patch.object(service, "claude_executable", return_value="claude"), \
                    patch.object(service, "renew_folder", Path(folder)), \
                    patch.object(bridge.subprocess, "run"), \
                    patch.object(bridge.subprocess, "Popen", return_value=fake), \
                    patch.object(bridge, "log"):
                delay = service.refresh()
            self.assertEqual("login", service.latest()[1]["state"])
            self.assertAlmostEqual(121.0, delay, delta=3.0)
            self.assertEqual(1, service._renew_failures)

    def test_renewal_retry_pause_grows_after_failures(self):
        service = bridge.ClaudeUsageService({"claude_usage": {"enabled": True}})
        pauses = []
        for failures in range(7):
            service._renew_failures = failures
            pauses.append(service.renew_cooldown())
        self.assertEqual([120, 120, 300, 900, 1800, 1800, 1800], pauses)

    def test_renewal_is_throttled(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            service = self.service(folder, (time.time() - 60) * 1000)
            with patch.object(service, "claude_executable", return_value=None), \
                    patch.object(bridge, "log"):
                self.assertFalse(service.renew_login())
            with patch.object(service, "claude_executable") as find:
                self.assertFalse(service.renew_login())
            find.assert_not_called()

    def test_missing_credentials(self):
        service = bridge.ClaudeUsageService(
            {"claude_usage": {"enabled": True, "credentials_path": "/nope/.credentials.json"}})
        with patch.object(bridge, "log"):
            service.refresh()
        self.assertEqual("nologin", service.latest()[1]["state"])

    def test_rate_limit_keeps_previous_value(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            service = self.service(folder, (time.time() + 3600) * 1000)
            with patch.object(bridge, "log"):
                with patch.object(service, "fetch_usage", return_value=(200, self.RESPONSE, 0)):
                    service.refresh()
                with patch.object(service, "fetch_usage", return_value=(429, None, 900)):
                    delay = service.refresh()
            self.assertEqual(900, delay)
            self.assertEqual("ok", service.latest()[1]["state"])


if __name__ == "__main__":
    unittest.main()


class FakeSerialPort:
    """Returns the given chunks one by one, then nothing (or raises)."""

    def __init__(self, chunks, error=None):
        self._chunks = list(chunks)
        self._error = error

    @property
    def in_waiting(self):
        return len(self._chunks[0]) if self._chunks else 0

    def read(self, size=1):
        if self._chunks:
            return self._chunks.pop(0)
        if self._error is not None:
            raise self._error
        time.sleep(0.005)
        return b""


class SerialReaderTests(unittest.TestCase):
    def _run_reader(self, port, config=None):
        reader = bridge.SerialReader(port, config or {"macros": []})
        reader.start()
        time.sleep(0.15)
        return reader

    def test_macro_runs_on_reader_thread_and_other_lines_are_queued(self):
        port = FakeSerialPort([
            b'DD:{"type":"hel',
            b'lo","protocol":3}\nDD:{"type":"macro","id":4}\n',
            b"Guru Meditation\n\n",
        ])
        with patch.object(bridge, "execute_macro") as execute:
            reader = self._run_reader(port, {"macros": [], "x": 1})
            lines = reader.lines(timeout=0.1)
            reader.stop()
            reader.join(timeout=1.0)
        execute.assert_called_once_with({"macros": [], "x": 1}, 4)
        self.assertEqual(lines, ['DD:{"type":"hello","protocol":3}', "Guru Meditation"])
        self.assertFalse(reader.is_alive())

    def test_invalid_macro_id_does_not_stop_the_reader(self):
        port = FakeSerialPort([b'DD:{"type":"macro","id":"x"}\nDD:{"type":"pong"}\n'])
        with patch.object(bridge, "execute_macro") as execute, patch.object(bridge, "log"):
            reader = self._run_reader(port)
            lines = reader.lines(timeout=0.1)
            reader.stop()
            reader.join(timeout=1.0)
        execute.assert_not_called()
        self.assertEqual(lines, ['DD:{"type":"pong"}'])

    def test_serial_error_is_raised_in_main_loop(self):
        port = FakeSerialPort([b'DD:{"type":"pong"}\n'], error=OSError("unplugged"))
        reader = self._run_reader(port)
        with self.assertRaises(OSError):
            reader.lines(timeout=0.1)
            reader.lines(timeout=0.1)
        self.assertFalse(reader.is_alive())

    def test_other_error_becomes_serial_exception(self):
        port = FakeSerialPort([], error=RuntimeError("boom"))
        reader = self._run_reader(port)
        with self.assertRaises(bridge.serial.SerialException):
            reader.lines(timeout=0.1)

    def test_stop_ends_thread_quietly(self):
        reader = self._run_reader(FakeSerialPort([]))
        reader.stop()
        reader.join(timeout=1.0)
        self.assertFalse(reader.is_alive())
        self.assertEqual(reader.lines(timeout=0.01), [])


class TopAppsServiceTests(unittest.TestCase):
    def test_service_reads_worker_lines(self):
        script = (
            "import json, sys, time\n"
            "print('not json', flush=True)\n"
            "print(json.dumps({'top_cpu_name': 'Chrome', 'top_cpu': 12.5}), flush=True)\n"
            "time.sleep(5)\n"
        )
        service = bridge.TopAppsService({}, command=[sys.executable, "-c", script])
        with patch.object(bridge, "log"):
            service.start()
            deadline = time.time() + 5
            while not service.latest() and time.time() < deadline:
                time.sleep(0.05)
            service.stop()
        self.assertEqual(service.latest(), {"top_cpu_name": "Chrome", "top_cpu": 12.5})

    def test_latest_is_empty_before_first_sample(self):
        self.assertEqual(bridge.TopAppsService({}).latest(), {})

    def test_interval_from_config_with_minimum(self):
        self.assertEqual(bridge.TopAppsService({}).interval, 10.0)
        self.assertEqual(bridge.TopAppsService({"top_apps_interval_seconds": 30}).interval, 30.0)
        self.assertEqual(bridge.TopAppsService({"top_apps_interval_seconds": 0.5}).interval, 2.0)
        self.assertEqual(bridge.TopAppsService({"top_apps_interval_seconds": "x"}).interval, 10.0)

    def test_default_command_runs_worker_mode(self):
        command = bridge.TopAppsService({}).default_command()
        self.assertIn("--top-apps-worker", command)
        self.assertEqual(command[command.index("--parent-pid") + 1], str(bridge.os.getpid()))
        self.assertTrue(command[1].endswith("control_deck_bridge.py"))

    def test_worker_prints_json_and_stops_when_parent_is_gone(self):
        class FakeMonitor:
            def sample(self):
                return {"top_ram_name": "Code", "top_ram_gb": 1.5}

        class Out:
            def __init__(self):
                self.text = ""
            def write(self, value):
                self.text += value
            def flush(self):
                pass

        out = Out()
        alive = iter([True, False])
        with patch.object(bridge, "ProcessMonitor", FakeMonitor), \
                patch.object(bridge.psutil, "pid_exists", lambda pid: next(alive)), \
                patch.object(bridge.time, "sleep"), \
                patch.object(bridge.sys, "stdout", out):
            bridge.run_top_apps_worker(10.0, 1234)
        self.assertEqual(out.text.splitlines(), ['{"top_ram_name": "Code", "top_ram_gb": 1.5}'])


class PowerMenuTests(unittest.TestCase):
    def test_config_packet_carries_power_button(self):
        self.assertEqual(0, bridge.macro_packet({})["power_button"])
        self.assertEqual(6, bridge.macro_packet({"power_menu_button": 6})["power_button"])
        self.assertEqual(8, bridge.macro_packet({"power_menu_button": 8})["power_button"])
        self.assertEqual(0, bridge.macro_packet({"power_menu_button": 9})["power_button"])
        self.assertEqual(0, bridge.macro_packet({"power_menu_button": "x"})["power_button"])

    def test_music_effect_is_sent_only_when_set(self):
        self.assertNotIn("music_effect", bridge.macro_packet({}))
        self.assertNotIn("music_effect", bridge.macro_packet({"media": {"enabled": True}}))
        self.assertEqual(6, bridge.macro_packet({"media": {"effect": 6}})["music_effect"])
        self.assertEqual(10, bridge.macro_packet({"media": {"effect": "10"}})["music_effect"])
        for bad in (0, 11, "x", None):
            self.assertNotIn("music_effect", bridge.macro_packet({"media": {"effect": bad}}))

    def test_power_commands(self):
        self.assertEqual(["shutdown.exe", "/s", "/t", "0"], bridge.power_command("shutdown", "Windows"))
        self.assertEqual(["shutdown.exe", "/r", "/t", "0"], bridge.power_command("restart", "Windows"))
        sleep = bridge.power_command("sleep", "Windows")
        self.assertEqual("powershell.exe", sleep[0])
        self.assertIn("SetSuspendState('Suspend', $false, $false)", sleep[-1])
        self.assertNotIn("/f", bridge.power_command("shutdown", "Windows"))
        self.assertEqual(["systemctl", "suspend"], bridge.power_command("sleep", "Linux"))
        self.assertEqual(["pmset", "sleepnow"], bridge.power_command("sleep", "Darwin"))

    def test_test_mode_does_nothing(self):
        config = {"power_menu_button": 6, "power_menu_test_mode": True}
        with patch.object(bridge.subprocess, "Popen") as popen, patch.object(bridge, "log") as log:
            bridge.execute_power(config, "shutdown")
        popen.assert_not_called()
        self.assertIn("test mode", log.call_args[0][0])

    def test_power_menu_off_does_nothing(self):
        with patch.object(bridge.subprocess, "Popen") as popen, patch.object(bridge, "log"):
            bridge.execute_power({}, "shutdown")
        popen.assert_not_called()

    def test_unknown_action_does_nothing(self):
        with patch.object(bridge.subprocess, "Popen") as popen, patch.object(bridge, "log"):
            bridge.execute_power({"power_menu_button": 6}, "format")
        popen.assert_not_called()

    def test_runs_command(self):
        with patch.object(bridge.subprocess, "Popen") as popen, patch.object(bridge, "log"), \
                patch.object(bridge.platform, "system", return_value="Windows"), \
                patch.object(bridge.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True):
            bridge.execute_power({"power_menu_button": 6}, "restart")
        self.assertEqual(["shutdown.exe", "/r", "/t", "0"], popen.call_args[0][0])
        self.assertEqual(0x08000000, popen.call_args[1]["creationflags"])

    def test_reader_runs_power_frames_immediately(self):
        port = FakeSerialPort([b'DD:{"type":"power","action":"sleep"}\nDD:{"type":"pong"}\n'])
        with patch.object(bridge, "execute_power") as execute:
            reader = bridge.SerialReader(port, {"power_menu_button": 6})
            reader.start()
            time.sleep(0.15)
            lines = reader.lines(timeout=0.1)
            reader.stop()
            reader.join(timeout=1.0)
        execute.assert_called_once_with({"power_menu_button": 6}, "sleep")
        self.assertEqual(['DD:{"type":"pong"}'], lines)


class MediaTextTests(unittest.TestCase):
    def test_greek_and_latin_are_kept(self):
        self.assertEqual("Συννεφιασμένη Κυριακή", bridge.clean_media_text("Συννεφιασμένη  Κυριακή"))
        self.assertEqual("Café – Été", bridge.clean_media_text("Café – Été"))

    def test_unknown_characters_are_simplified_or_dropped(self):
        self.assertEqual("Song", bridge.clean_media_text("Song 🎵"))
        self.assertEqual("Erdos", bridge.clean_media_text("Erdős"))  # ő -> o
        self.assertEqual("a b", bridge.clean_media_text("a\tb\n"))
        self.assertEqual("ά", bridge.clean_media_text("ά"))  # Greek oxia -> tonos

    def test_length_is_limited(self):
        self.assertEqual(60, len(bridge.clean_media_text("x" * 200)))

    def test_font_characters_match_the_firmware_font(self):
        header = (Path(__file__).resolve().parents[2] / "src" / "media_font.h").read_text(encoding="ascii")
        data = bytes(int(x, 16) for x in re.findall(r"0x([0-9a-f]{2})", header.split("PROGMEM = {", 1)[1]))
        count = int.from_bytes(data[0:4], "big")
        codes = [int.from_bytes(data[24 + 28 * i: 28 + 28 * i], "big") for i in range(count)]
        self.assertEqual(bridge.media_font_codes(), codes)


class SpectrumTests(unittest.TestCase):
    def test_band_edges_are_increasing_and_inside_the_fft(self):
        ranges = bridge.media_band_edges(48000)
        self.assertEqual(24, len(ranges))
        for (a, b), (c, _) in zip(ranges, ranges[1:]):
            self.assertLess(a, b)
            self.assertLessEqual(a, c)
        self.assertLessEqual(ranges[-1][1], bridge.MEDIA_FFT_SIZE // 2 + 1)

    def test_encode_levels(self):
        self.assertEqual("0GV", bridge.SpectrumAnalyzer.encode([0.0, 0.5, 1.0]))
        self.assertEqual("0V", bridge.SpectrumAnalyzer.encode([-1.0, 2.0]))

    @unittest.skipUnless(importlib.util.find_spec("numpy"), "numpy not installed")
    def test_sine_lights_its_band_and_silence_falls(self):
        import numpy as np
        rate = 48000
        analyzer = bridge.SpectrumAnalyzer(rate, 20)
        t = np.arange(bridge.MEDIA_FFT_SIZE) / rate
        bars = analyzer.update(0.3 * np.sin(2 * np.pi * 1000 * t))
        levels = [bridge.MEDIA_DIGITS.index(c) for c in bars]
        loudest = max(range(24), key=lambda i: levels[i])
        a, b = analyzer.ranges[loudest]
        self.assertTrue(a * rate / bridge.MEDIA_FFT_SIZE <= 1000 <= b * rate / bridge.MEDIA_FFT_SIZE)
        self.assertGreaterEqual(levels[loudest], 25)
        self.assertLessEqual(levels[0], 5)
        for _ in range(20):
            bars = analyzer.update(np.zeros(bridge.MEDIA_FFT_SIZE))
        self.assertEqual("0" * 24, bars)

    def test_encode_wave(self):
        self.assertEqual("0FU", bridge.SpectrumAnalyzer.encode_wave([-1.0, 0.0, 1.0]))
        self.assertEqual("0U", bridge.SpectrumAnalyzer.encode_wave([-3.0, 3.0]))
        self.assertEqual(64, len(bridge.MEDIA_WAVE_SILENT))
        self.assertEqual({"F"}, set(bridge.MEDIA_WAVE_SILENT))

    @unittest.skipUnless(importlib.util.find_spec("numpy"), "numpy not installed")
    def test_waveform_starts_at_a_rising_zero_crossing(self):
        import numpy as np
        rate = 48000
        analyzer = bridge.SpectrumAnalyzer(rate, 20)
        t = np.arange(bridge.MEDIA_FFT_SIZE) / rate
        first = None
        for phase in (0.0, 1.0, 2.5):  # the picture does not move with the phase
            wave = analyzer.waveform(0.2 * np.sin(2 * np.pi * 375 * t + phase))
            self.assertEqual(64, len(wave))
            values = [bridge.MEDIA_DIGITS.index(c) for c in wave]
            self.assertTrue(all(0 <= v <= 30 for v in values))
            self.assertGreaterEqual(max(values), 26)  # automatic gain fills the height
            self.assertLessEqual(min(values), 4)
            self.assertLessEqual(values[0], 17)  # starts near the centre line, going up
            self.assertGreater(values[2], values[0])
            if first is None:
                first = values
            else:
                self.assertLessEqual(max(abs(a - b) for a, b in zip(first, values)), 2)
        self.assertEqual(bridge.MEDIA_WAVE_SILENT, analyzer.waveform(np.zeros(bridge.MEDIA_FFT_SIZE)))


class MediaServiceTests(unittest.TestCase):
    def service(self):
        frames = []
        service = bridge.MediaService({}, command=["unused"])
        service.attach(frames.append)
        return service, frames

    def test_spectrum_and_info_are_forwarded(self):
        service, frames = self.service()
        service._handle_line('{"k":"spec","b":"0123456789ABCDEFGHIJKLMN"}')
        service._handle_line('{"k":"spec","b":"short"}')
        service._handle_line(json.dumps({"k": "info", "state": "playing", "title": "Τίτλος 🎵",
                                         "artist": "X", "vol": 45, "mute": False}))
        self.assertEqual({"type": "spec", "b": "0123456789ABCDEFGHIJKLMN"}, frames[0])
        self.assertEqual({"type": "media", "state": "playing", "title": "Τίτλος", "artist": "X",
                          "vol": 45, "mute": False}, frames[1])
        self.assertEqual(2, len(frames))

    def test_waveform_is_forwarded_only_when_valid(self):
        service, frames = self.service()
        bars = "0123456789ABCDEFGHIJKLMN"
        wave = "0FU" + "F" * 61
        service._handle_line(json.dumps({"k": "spec", "b": bars, "w": wave}))
        service._handle_line(json.dumps({"k": "spec", "b": bars, "w": "F" * 10}))
        service._handle_line(json.dumps({"k": "spec", "b": bars, "w": "V" * 64}))  # 31 > 30
        self.assertEqual({"type": "spec", "b": bars, "w": wave}, frames[0])
        self.assertEqual({"type": "spec", "b": bars}, frames[1])
        self.assertEqual({"type": "spec", "b": bars}, frames[2])

    def test_info_is_resent_when_the_display_attaches(self):
        service, frames = self.service()
        service._handle_line('{"k":"info","state":"weird","vol":"x"}')
        self.assertEqual("none", frames[0]["state"])
        self.assertEqual(-1, frames[0]["vol"])
        later = []
        service.attach(later.append)
        self.assertEqual(frames[0], later[0])

    def test_page_switches_the_worker_on_and_off(self):
        service, _ = self.service()
        with patch.object(service, "_write") as write:
            service.set_page("music", True)
            service.set_page("music", True)
            service.set_page("music", False)
            service.set_page("monitor", True)
        self.assertEqual([unittest.mock.call("active 1"), unittest.mock.call("active 0")],
                         write.call_args_list)

    def test_media_keys(self):
        service, _ = self.service()
        with patch.object(bridge, "send_hotkey") as hotkey, patch.object(service, "_write") as write, \
                patch.object(bridge, "log"):
            service.media_key("prev")
            service.media_key("play_pause")
            service.media_key("vol_up")
            service.media_key("vol_down")
            service.media_key("bogus")
        self.assertEqual([unittest.mock.call("media_previous"), unittest.mock.call("media_play_pause")],
                         hotkey.call_args_list)
        self.assertIn(unittest.mock.call("vol 1"), write.call_args_list)
        self.assertIn(unittest.mock.call("vol -1"), write.call_args_list)

    def test_disabled_off_windows_without_a_command(self):
        with patch.object(bridge.platform, "system", return_value="Linux"):
            self.assertFalse(bridge.MediaService({}).enabled)
        self.assertFalse(bridge.MediaService({"media": {"enabled": False}}, command=["x"]).enabled)

    def test_utf8_frames(self):
        frame = bridge.encode_frame({"type": "media", "title": "Άλφα"})
        self.assertEqual('DD:{"type":"media","title":"Άλφα"}\n'.encode("utf-8"), frame)

    def test_reader_passes_page_and_media_key_frames(self):
        port = FakeSerialPort([b'DD:{"type":"page","page":"music","awake":true}\n'
                               b'DD:{"type":"media_key","key":"next"}\nDD:{"type":"pong"}\n'])
        media = unittest.mock.MagicMock()
        reader = bridge.SerialReader(port, {"macros": []}, media)
        reader.start()
        time.sleep(0.15)
        lines = reader.lines(timeout=0.1)
        reader.stop()
        reader.join(timeout=1.0)
        media.set_page.assert_called_once_with("music", True)
        media.media_key.assert_called_once_with("next")
        self.assertEqual(['DD:{"type":"pong"}'], lines)


@unittest.skipUnless(importlib.util.find_spec("numpy"), "numpy not installed")
class MediaWorkerTests(unittest.TestCase):
    def test_worker_sends_info_and_spectrum_and_handles_commands(self):
        import io
        import os
        import threading
        import numpy as np

        class FakeReader:
            volume_changes = []

            def __init__(self, warn):
                warn("test warning")

            def song(self):
                return "playing", "Τίτλος", "Artist"

            def volume(self):
                return 40, False, "Speakers"

            def change_volume(self, direction, step):
                self.volume_changes.append((direction, step))

        class FakeCapture:
            rate = 48000.0
            name = "Speakers [Loopback]"

            def __init__(self, step=None):
                self.last_audio = time.monotonic()

            def samples(self):
                self.last_audio = time.monotonic()
                t = np.arange(bridge.MEDIA_FFT_SIZE) / self.rate
                return 0.3 * np.sin(2 * np.pi * 440 * t)

            def alive(self):
                return True

            def close(self):
                pass

        read_fd, write_fd = os.pipe()
        stdin = os.fdopen(read_fd, "r")
        stdout = io.StringIO()
        with patch.object(bridge, "MediaInfoReader", FakeReader), \
                patch.object(bridge, "LoopbackCapture", FakeCapture), \
                patch.object(bridge.sys, "stdin", stdin), patch.object(bridge.sys, "stdout", stdout):
            worker = threading.Thread(target=bridge.run_media_worker, args=(20.0, 0, 5))
            worker.start()
            with os.fdopen(write_fd, "w") as commands:
                commands.write("active 1\n")
                commands.flush()
                time.sleep(0.6)
                commands.write("vol 1\n")
                commands.flush()
                time.sleep(0.2)
            worker.join(timeout=3.0)  # stdin closed -> quit
        self.assertFalse(worker.is_alive())
        lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
        kinds = [line["k"] for line in lines]
        self.assertEqual("warn", kinds[0])
        self.assertIn("info", kinds)
        self.assertIn("capture", kinds)
        specs = [line["b"] for line in lines if line["k"] == "spec"]
        self.assertGreaterEqual(len(specs), 6)  # ~20 per second
        self.assertTrue(all(len(b) == 24 for b in specs))
        waves = [line["w"] for line in lines if line["k"] == "spec"]
        self.assertTrue(all(len(w) == 64 for w in waves))
        self.assertNotEqual(bridge.MEDIA_WAVE_SILENT, waves[-1])
        self.assertNotEqual("0" * 24, specs[-1])
        self.assertEqual([(1, 5)], FakeReader.volume_changes)
