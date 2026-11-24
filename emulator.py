"""Discover and start locally installed Android Virtual Devices."""
import os
from pathlib import Path
import re
import shutil
import subprocess


AVD_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


SKIN_SIZE = re.compile(r"[1-9][0-9]{1,3}x[1-9][0-9]{1,3}")


class Emulators:
    def __init__(self, adb_path, executable=""):
        sdk_emulator = (Path(adb_path).resolve().parent.parent / "emulator" / "emulator"
                        if adb_path else None)
        if sdk_emulator and os.name == "nt":
            sdk_emulator = sdk_emulator.with_suffix(".exe")
        self.path = executable or (str(sdk_emulator) if sdk_emulator and sdk_emulator.is_file()
                                   else shutil.which("emulator"))

    def list(self):
        if not self.path:
            raise ValueError("Android Emulator executable missing. Install it through Android Studio or set emulator_path.")
        try:
            result = subprocess.run([self.path, "-list-avds"], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("Could not list Android Virtual Devices") from exc
        if result.returncode:
            raise ValueError("Android Emulator could not list virtual devices")
        return [name for name in result.stdout.splitlines() if AVD_NAME.fullmatch(name)]

    def launch(self, name, log_path, skin="", *, headless=True, skip_adb_auth=False):
        if not isinstance(name, str) or not AVD_NAME.fullmatch(name) or name not in self.list():
            raise ValueError("Select an installed Android Virtual Device")
        command = [self.path, "-avd", name]
        if headless:
            command.append("-no-window")
        if skip_adb_auth:
            command.extend(["-skip-adb-auth", "-no-snapshot-load"])
        if skin:
            if not SKIN_SIZE.fullmatch(skin):
                raise ValueError("Use a WIDTHxHEIGHT emulator skin override")
            command.extend(["-skin", skin])
        try:
            with log_path.open("wb") as log:
                return subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                        stdout=log, stderr=subprocess.STDOUT, start_new_session=headless)
        except OSError as exc:
            raise ValueError("Could not start Android Emulator") from exc


def exit_message(log_path):
    try:
        with log_path.open("rb") as log:
            log.seek(max(0, log_path.stat().st_size - 8192))
            tail = log.read(8192).decode(errors="replace").lower()
    except OSError:
        tail = ""
    if "running multiple emulators" in tail:
        return "Emulator exited. This AVD is already running. Refresh ADB to reconnect."
    if "unknown skin name" in tail:
        return "Emulator exited. Its display skin is missing. Check emulator_skin_overrides in studio-config.txt."
    if "failed to initialize hvf" in tail or "no accelerator found" in tail:
        return "Emulator exited. Hardware acceleration could not start. Run Studio from a regular macOS session."
    if "unable to connect to adb daemon" in tail:
        return "Emulator exited. It could not reach ADB. Run Repair Android ADB.command and try again."
    return "Emulator exited. See emulator.log beside the Studio database."
