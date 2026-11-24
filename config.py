"""Read and atomically save the Studio configuration as JSON in a text file."""
import json
import os
from pathlib import Path
import tempfile

from agent import LIMITS
from emulator import AVD_NAME, SKIN_SIZE
from llm import DEFAULT, validate_config


DEFAULT_DATA = "~/.android-ai-test-studio/studio.sqlite"
SERVER = {"host": "127.0.0.1", "port": 8765, "open_browser": False}
BOUNDS = {"actions": 1000, "retries": 20, "recoveries": 50, "seconds": 86400, "repeated": 50}


def validate_adb_path(adb):
    if not isinstance(adb, str) or len(adb) > 1024:
        raise ValueError("Invalid ADB path")
    if adb and (not Path(adb).is_absolute() or Path(adb).name.lower() not in ("adb", "adb.exe")
                or not Path(adb).is_file() or not os.access(adb, os.X_OK)):
        raise ValueError("Choose an absolute path to the executable adb or adb.exe")
    return adb


def validate_settings(data):
    if not isinstance(data, dict):
        raise ValueError("settings must be an object")
    limits = data.get("limits", LIMITS)
    if not isinstance(limits, dict) or any(
        type(limits.get(key)) is not int or not (0 if key in ("retries", "recoveries") else 1)
        <= limits[key] <= maximum for key, maximum in BOUNDS.items()
    ):
        raise ValueError("Invalid execution limits")
    screenshots = data.get("report_screenshots", True)
    if type(screenshots) is not bool:
        raise ValueError("report_screenshots must be a boolean")
    return {"adb": validate_adb_path(data.get("adb", "")),
            "llm": validate_config(data.get("llm", DEFAULT)),
            "limits": {key: limits[key] for key in LIMITS}, "report_screenshots": screenshots}


class Config:
    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()
        raw = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        if not isinstance(raw, dict) or raw.get("schema_version", 1) != 1:
            raise ValueError("Expected a version 1 Studio configuration object")
        server = raw.get("server", {})
        if not isinstance(server, dict):
            raise ValueError("server must be an object")
        server = {**SERVER, **server}
        if server["host"] != "127.0.0.1":
            raise ValueError("This Studio version requires server.host to be 127.0.0.1")
        if type(server["port"]) is not int or not 0 <= server["port"] <= 65535:
            raise ValueError("server.port must be an integer from 0 to 65535")
        if type(server["open_browser"]) is not bool:
            raise ValueError("server.open_browser must be a boolean")
        data = raw.get("database_path", DEFAULT_DATA)
        if not isinstance(data, str) or not data.strip():
            raise ValueError("database_path must be a nonempty string")
        emulator = raw.get("emulator_path", "")
        if not isinstance(emulator, str) or (emulator and not Path(emulator).is_absolute()):
            raise ValueError("emulator_path must be an absolute executable path or an empty string")
        headless = raw.get("emulator_headless", True)
        if type(headless) is not bool:
            raise ValueError("emulator_headless must be a boolean")
        preview_interval = raw.get("emulator_preview_interval_ms", 300)
        if type(preview_interval) is not int or not 100 <= preview_interval <= 5000:
            raise ValueError("emulator_preview_interval_ms must be between 100 and 5000")
        skins = raw.get("emulator_skin_overrides", {})
        if not isinstance(skins, dict) or any(
            not AVD_NAME.fullmatch(name) or not isinstance(size, str) or not SKIN_SIZE.fullmatch(size)
            for name, size in skins.items()
        ):
            raise ValueError("emulator_skin_overrides must map AVD names to WIDTHxHEIGHT")
        self.document = {**raw, "emulator_path": emulator, "emulator_headless": headless,
                         "emulator_preview_interval_ms": preview_interval, "emulator_skin_overrides": skins, "schema_version": 1, "server": server, "database_path": data}
        self.database_path = self.path.parent / Path(data).expanduser()

    def initialize(self, store):
        if "settings" not in self.document:
            legacy = store.read("settings", "active") if not self.path.exists() else None
            self.save(validate_settings(legacy or {}))
        return validate_settings(self.document["settings"])

    def save(self, settings):
        document = {**self.document, "settings": settings}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(document, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self.document = document
