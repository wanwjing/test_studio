import base64
import hashlib
import re
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path


PACKAGE = re.compile(r"[A-Za-z][\w]*(?:\.[A-Za-z][\w]*)+", re.ASCII)
SERIAL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:%\[\]-]{0,127}")
SELECTORS = ("resource_id", "description", "text")
ACTIONS = ("tap", "long_press", "input_text", "clear_text", "swipe", "scroll", "press_back",
           "wait_for_element", "launch_app", "restart_app", "capture_screenshot", "inspect_screen")


def parse_devices(output):
    devices = []
    for line in output.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 2 and SERIAL.fullmatch(fields[0]):
            devices.append(dict(serial=fields[0], status=fields[1],
                                emulator=fields[0].startswith("emulator-"),
                                model=next((f[6:] for f in fields[2:] if f.startswith("model:")), "")))
    return devices


def parse_ui(data):
    if len(data) > 4_000_000 or b"<!" in data:
        raise ValueError("Unsupported UI hierarchy")
    start = data.find(b"<?xml")
    if start < 0:
        start = data.find(b"<hierarchy")
    end = data.rfind(b"</hierarchy>")
    if start < 0 or end < start:
        raise ValueError("Android returned no UI hierarchy")
    try:
        root = ET.fromstring(data[start:end + len(b"</hierarchy>")])
    except ET.ParseError as exc:
        raise ValueError("ADB returned malformed hierarchy XML") from exc
    nodes = []

    def visit(node, parent=None, depth=0):
        if depth > 100 or len(nodes) > 5000:
            raise ValueError("UI hierarchy exceeds supported size")
        for child in node.findall("node"):
            keys = ("resource-id", "content-desc", "text", "class", "package")
            element = dict(zip(("resource_id", "description", "text", "class", "package"),
                               (child.get(key, "") for key in keys)))
            element.update({key: child.get(key) == "true" for key in
                            ("clickable", "enabled", "scrollable", "focused", "password", "editable")})
            element["bounds"] = [int(n) for n in re.findall(r"-?\d+", child.get("bounds", ""))]
            element["parent"] = parent
            if element["password"]:
                element["text"] = element["description"] = ""
            index = len(nodes)
            nodes.append(element)
            visit(child, index, depth + 1)

    visit(root)
    return nodes


def png_size(data):
    if data[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" or len(data) < 33:
        raise ValueError("ADB did not return a PNG screenshot")
    width, height = struct.unpack(">II", data[16:24])
    if not (0 < width <= 16384 and 0 < height <= 16384):
        raise ValueError("Invalid screenshot dimensions")
    return width, height


def validate_target(target):
    if not isinstance(target, dict) or target.get("strategy") not in SELECTORS:
        raise ValueError("Expected a resource_id, description, or text selector")
    if not isinstance(target.get("value"), str) or not 0 < len(target["value"]) <= 512:
        raise ValueError("Invalid selector value")


def validate_action(action):
    if not isinstance(action, dict) or action.get("schema_version") != 1 or action.get("action") not in ACTIONS:
        raise ValueError("Expected a version 1 Android action")
    kind = action["action"]
    if "confidence" in action and (type(action["confidence"]) not in (int, float) or
                                   not 0.85 <= action["confidence"] <= 1):
        raise ValueError("Action confidence must be between 0.85 and 1")
    if kind in ("tap", "long_press", "wait_for_element") and "point" not in action:
        validate_target(action.get("target"))
    if "point" in action:
        if kind not in ("tap", "long_press") or not isinstance(action.get("fingerprint"), str):
            raise ValueError("Coordinate taps require a current screen fingerprint")
        validate_point(action["point"])
    if kind == "swipe":
        validate_point(action.get("start"))
        validate_point(action.get("end"))
    if kind == "scroll" and action.get("direction") not in ("up", "down"):
        raise ValueError("Scroll direction must be up or down")
    if kind == "input_text":
        value = action.get("text")
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9 ._@,+:/-]{1,256}", value):
            raise ValueError("Use 1–256 supported ASCII characters in test input")
    return action


def validate_point(point):
    if not isinstance(point, list) or len(point) != 2 or any(type(n) is not int for n in point):
        raise ValueError("Expected an integer coordinate pair")


class Android:
    def __init__(self, path=""):
        self.path = path or shutil.which("adb")
        self.server_port = None

    def _run(self, *args, serial=None, timeout=15, show_error=False):
        if not self.path:
            raise ValueError("ADB is missing. Configure the platform-tools adb executable.")
        command = [self.path]
        if self.server_port is not None:
            try:
                with socket.create_connection(("127.0.0.1", self.server_port), timeout=2):
                    pass
            except OSError as exc:
                raise ValueError("ADB tunnel is unreachable. Check the SSH tunnel and local forwarded port.") from exc
            command += ["-H", "127.0.0.1", "-P", str(self.server_port)]
        if serial is not None:
            if not isinstance(serial, str) or not SERIAL.fullmatch(serial):
                raise ValueError("Invalid device serial")
            command += ["-s", serial]
        try:
            result = subprocess.run(command + list(args), capture_output=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("ADB could not start or timed out") from exc
        if result.returncode:
            if show_error:
                output = (result.stderr or result.stdout).decode(errors="replace").strip()
                if output:
                    raise ValueError(output[:1000])
            raise ValueError("ADB command failed. Check the daemon, device authorization, and USB connection.")
        return result.stdout

    def devices(self):
        return parse_devices(self._run("devices", "-l").decode(errors="replace"))

    def install_apk(self, serial, path):
        self._run("install", "-r", str(path), serial=serial, timeout=180, show_error=True)

    def foreground(self, serial):
        output = self._run("shell", "dumpsys", "activity", "activities", serial=serial).decode(errors="replace")
        for line in output.splitlines():
            if "ResumedActivity" in line:
                match = re.search(r"\s([\w.]+)/[\w.$]+", line)
                if match:
                    return match[1]
        return ""

    def app_version(self, serial, package):
        if not PACKAGE.fullmatch(package):
            raise ValueError("Invalid application package")
        output = self._run("shell", "dumpsys", "package", package, serial=serial).decode(errors="replace")
        version = re.search(r"^\s*versionName=(.+)$", output, re.MULTILINE)
        return version.group(1).strip() if version else ""

    def packages(self, serial):
        data = self._run("shell", "pm", "list", "packages", "-3", serial=serial).decode(errors="replace")
        packages = {line[8:] for line in data.splitlines()
                    if line.startswith("package:") and PACKAGE.fullmatch(line[8:])}
        try:
            launchers = self._run("shell", "cmd", "package", "query-activities", "--brief",
                                  "-a", "android.intent.action.MAIN", "-c",
                                  "android.intent.category.LAUNCHER", serial=serial).decode(errors="replace")
        except ValueError:
            return sorted(packages)
        for line in launchers.splitlines():
            package, separator, activity = line.strip().partition("/")
            if separator and activity and PACKAGE.fullmatch(package):
                packages.add(package)
        return sorted(packages)

    def is_emulator(self, serial):
        if re.fullmatch(r"emulator-[0-9]{4,5}", serial):
            return True
        return (serial == "127.0.0.1:5555"
                and self._run("shell", "getprop", "ro.kernel.qemu", serial=serial).strip() == b"1")

    def connect_local_emulator(self):
        serial = "127.0.0.1:5555"
        self._run("connect", serial)
        devices = self.devices()
        for target in ("emulator-5554", serial):
            if any(d["serial"] == target and d["status"] == "device" for d in devices):
                if self.is_emulator(target):
                    return target
        raise ValueError("The local emulator on port 5555 is not online in ADB yet. Wait for Android to start and retry.")

    def emulator_name(self, serial):
        for command in (("emu", "avd", "name"), ("shell", "getprop", "ro.boot.qemu.avd_name"),
                        ("shell", "getprop", "ro.kernel.qemu.avd_name")):
            try:
                result = self._run(*command, serial=serial).decode(errors="replace")
                name = next(iter(result.splitlines()), "").strip()
                if name != "OK" and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name):
                    return name
            except ValueError:
                continue
        return ""

    def end_emulator(self, serial):
        if not isinstance(serial, str) or not re.fullmatch(r"emulator-[0-9]{4,5}", serial):
            raise ValueError("Select an emulator detected by ADB")
        self._run("emu", "kill", serial=serial)

    def emulator_booted(self, serial):
        return self._run("shell", "getprop", "sys.boot_completed", serial=serial).strip() == b"1"

    def preview(self, serial):
        try:
            image = self._run("exec-out", "screencap", "-p", serial=serial)
            return image, png_size(image)
        except ValueError:
            if not self.console_preview_available(serial):
                raise
        with tempfile.TemporaryDirectory(prefix="studio-emulator-") as folder:
            screenshot = Path(folder) / "screen.png"
            self._console(serial, "screenrecord", "screenshot", str(screenshot))
            try:
                image = screenshot.read_bytes()
            except OSError as exc:
                raise ValueError("The emulator console did not return a screenshot") from exc
        return image, png_size(image)

    def console_preview_available(self, serial):
        return (self.server_port is None and re.fullmatch(r"emulator-[0-9]{4,5}", serial) is not None
                and any(device["serial"] == serial and device["status"] == "unauthorized"
                        for device in self.devices()))

    def _console(self, serial, *args):
        output = self._run("emu", *args, serial=serial)
        if any(line.startswith(b"KO") for line in output.splitlines()):
            raise ValueError("The local emulator console rejected the command")

    def control_emulator(self, serial, action, frame_size=None):
        if not isinstance(action, dict):
            raise ValueError("Choose an emulator action")
        kind = action.get("action")
        console = self.console_preview_available(serial)
        if kind in ("tap", "swipe"):
            points = [action.get("point")] if kind == "tap" else [action.get("start"), action.get("end")]
            size = action.get("size")
            validate_point(size)
            width, height = size
            for point in points:
                validate_point(point)
                if not (0 <= point[0] < width and 0 <= point[1] < height):
                    raise ValueError("Gesture is outside the emulator screen")
            actual = frame_size or self.preview(serial)[1]
            if tuple(size) != actual:
                raise ValueError("Emulator screen size changed. Wait for the next frame and tap again.")
            if console:
                start, end = points[0], points[-1]
                self._console(serial, "event", "mouse", *map(str, start), "0", "1")
                try:
                    if kind == "swipe":
                        for step in range(1, 5):
                            position = [a + (b - a) * step // 4 for a, b in zip(start, end)]
                            self._console(serial, "event", "mouse", *map(str, position), "0", "1")
                finally:
                    self._console(serial, "event", "mouse", *map(str, end), "0", "0")
                return
            coords = [str(value) for point in points for value in point]
            args = ("tap", *coords) if kind == "tap" else ("swipe", *coords, "350")
        elif kind in ("back", "home"):
            if console:
                key = "158" if kind == "back" else "102"
                self._console(serial, "event", "send", f"1:{key}:1", "0:0:0", f"1:{key}:0", "0:0:0")
                return
            args = ("keyevent", "KEYCODE_BACK" if kind == "back" else "KEYCODE_HOME")
        else:
            raise ValueError("Unsupported emulator action")
        self._run("shell", "input", *args, serial=serial)

    def observe(self, serial):
        before = self.foreground(serial)
        xml = self._run("shell", "uiautomator", "dump", "/dev/stdout", serial=serial)
        if b"<hierarchy" not in xml:
            path = f"/data/local/tmp/studio-ui-{uuid.uuid4().hex}.xml"
            try:
                self._run("shell", "uiautomator", "dump", path, serial=serial)
                xml = self._run("shell", "cat", path, serial=serial)
            finally:
                self._run("shell", "rm", "-f", path, serial=serial)
        nodes = parse_ui(xml)
        image = self._run("exec-out", "screencap", "-p", serial=serial)
        width, height = png_size(image)
        foreground = self.foreground(serial)
        if before != foreground:
            raise ValueError("Foreground changed during capture. Refresh the screen.")
        fingerprint = hashlib.sha256(repr((foreground, width, height, nodes)).encode()).hexdigest()
        sensitive = any(node["password"] for node in nodes)
        return dict(schema_version=1, serial=serial, package=foreground, nodes=nodes,
                    width=width, height=height, fingerprint=fingerprint, captured=time.time(),
                    image_fingerprint=hashlib.sha256(fingerprint.encode() + image).hexdigest(),
                    image="" if sensitive else base64.b64encode(image).decode())

    def logcat(self, serial):
        output = self._run("logcat", "-d", "-t", "200", "-v", "threadtime", serial=serial)
        return output[-128_000:].decode("utf-8", errors="replace")

    def info(self, serial):
        return {key: self._run("shell", "getprop", prop, serial=serial).decode().strip()
                for key, prop in (("android", "ro.build.version.release"), ("model", "ro.product.model"))}

    def execute(self, serial, package, action, screen=None):
        validate_action(action)
        if not isinstance(package, str) or not PACKAGE.fullmatch(package):
            raise ValueError("Select a valid application package")
        kind = action["action"]
        if kind in ("launch_app", "restart_app"):
            if package not in self.packages(serial):
                raise ValueError("Select an installed test application")
            if kind == "restart_app":
                self._run("shell", "am", "force-stop", package, serial=serial)
            self._run("shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER",
                      "1", serial=serial)
            return
        screen = screen or self.observe(serial)
        if (screen["serial"] != serial or screen["package"] != package or
                self.foreground(serial) != package or time.time() - screen["captured"] > 15):
            raise ValueError("Selected app is outside the foreground or the observation expired")
        if kind in ("inspect_screen", "capture_screenshot"):
            return

        def bounded(point):
            x, y = point
            if not (0 <= x < screen["width"] and 0 <= y < screen["height"]):
                raise ValueError("Coordinates are outside the screen")
            return str(x), str(y)

        if kind in ("tap", "long_press", "wait_for_element"):
            if "point" in action:
                if action["fingerprint"] != screen["image_fingerprint"]:
                    raise ValueError("Screen changed. Refresh before tapping.")
                point = action["point"]
            else:
                target = action["target"]
                matches = [node for node in screen["nodes"]
                           if node["package"] == package and node[target["strategy"]] == target["value"]
                           and node["enabled"] and len(node["bounds"]) == 4]
                if len(matches) != 1:
                    raise ValueError("Selector must match exactly one enabled element")
                x1, y1, x2, y2 = matches[0]["bounds"]
                if x2 <= x1 or y2 <= y1:
                    raise ValueError("Element has invalid bounds")
                point = [(x1 + x2) // 2, (y1 + y2) // 2]
            x, y = bounded(point)
            if kind == "wait_for_element":
                return
            args = ("tap", x, y) if kind == "tap" else ("swipe", x, y, x, y, "750")
        elif kind == "swipe":
            args = ("swipe", *bounded(action["start"]), *bounded(action["end"]), "400")
        elif kind == "scroll":
            x, h = screen["width"] // 2, screen["height"]
            ys = [h // 4, h * 3 // 4]
            if action["direction"] == "down":
                ys.reverse()
            args = ("swipe", *bounded([x, ys[0]]), *bounded([x, ys[1]]), "400")
        elif kind in ("input_text", "clear_text"):
            focused = [node for node in screen["nodes"] if node["focused"] and node["package"] == package]
            if (len(focused) != 1 or focused[0]["password"] or
                    not (focused[0]["editable"] or focused[0]["class"].endswith("EditText"))):
                raise ValueError("Focus a non-password editable field in the selected app first")
            if kind == "input_text":
                args = ("text", action["text"].replace(" ", "%s"))
            else:
                count = min(len(focused[0]["text"]), 512)
                if not count:
                    return
                args = ("keyevent", "KEYCODE_MOVE_END", *(["KEYCODE_DEL"] * count))
        else:
            args = ("keyevent", "KEYCODE_BACK")
        self._run("shell", "input", *args, serial=serial)
