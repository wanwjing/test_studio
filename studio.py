import argparse
import base64
import errno
import json
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import threading
import time
import webbrowser
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from agent import Run, validate_plan
from android import Android, PACKAGE, validate_action
from config import Config, validate_adb_path, validate_settings
from emulator import Emulators, exit_message
from exploration import Exploration
from llm import Model, public_screen, validate_config
from storage import Store


ROOT = Path(__file__).resolve().parent
MAX_APK_SIZE = 1024 ** 3
ASSETS = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
          "/style.css": ("style.css", "text/css"),
          "/studio-icon.png": ("studio-icon.png", "image/png"),
          "/google-maps-test-plan.json": ("google-maps-test-plan.json", "application/json")}


class Studio:
    def __init__(self, store, config_path=None, *, config=None):
        self.store = store
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.run = self.worker = None
        self.batch = None
        self.batch_stop = False
        self.emulator = None
        self.emulator_name = ""
        self.emulator_log = Path(store.path).parent / "emulator.log"
        self.next_emulator_connect = 0
        self.preview_sizes = {}
        self.adb_tunnel_port = None
        self.config = config or Config(Path(store.path).with_suffix(".txt"))
        self.settings = self.config.initialize(store)
        if config_path:
            candidate = validate_config(json.loads(Path(config_path).read_text()))
            Model(candidate).test_connection()
            self.settings = {**self.settings, "llm": candidate}
            self.config.save(self.settings)

    @staticmethod
    def find_emulator(android, name, *, include_unauthorized=False):
        for device in android.devices():
            serial = device["serial"]
            if (include_unauthorized and device["status"] == "unauthorized" and device["emulator"]
                    and android.emulator_name(serial) == name):
                return serial
            if device["status"] == "device" and (device["emulator"] or serial == "127.0.0.1:5555"):
                try:
                    if android.is_emulator(serial) and (
                        android.emulator_name(serial) == name or
                        (serial == "127.0.0.1:5555" and android.emulator_name("emulator-5554") == name)
                    ):
                        return serial
                except ValueError:
                    continue
        return ""

    def android(self):
        android = Android(self.settings["adb"])
        android.server_port = self.adb_tunnel_port
        return android

    def install_apk(self, serial, path):
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise ValueError("Stop the active test run before installing APKs")
            android = self.android()
            if not any(device["serial"] == serial and device["status"] == "device"
                       for device in android.devices()):
                raise ValueError("Select an online, authorized Android device")
            android.install_apk(serial, path)

    def run_batch(self, runs):
        try:
            for index, run in enumerate(runs):
                with self.lock:
                    if self.batch_stop:
                        break
                    self.run = run
                    self.batch["entries"][index].update(state="RUNNING")
                run.data["started"] = time.time()
                run.run()
                with self.lock:
                    self.batch["entries"][index].update(state=run.data["state"], id=run.data["id"],
                                                         metrics=run.data["metrics"])
        finally:
            with self.lock:
                for entry in self.batch["entries"]:
                    if entry["state"] in ("WAITING", "RUNNING"):
                        entry["state"] = "NOT_EXECUTED"
                self.batch["active"] = False

    def start_batch(self, android, model, serial, data):
        ids = data.get("plan_ids")
        if not isinstance(ids, list) or not ids or any(not isinstance(key, str) for key in ids):
            raise ValueError("Select saved test plans")
        if len(set(ids)) != len(ids):
            raise ValueError("Select each plan only once")
        installed = set(android.packages(serial))
        runs = []
        entries = []
        for key in ids:
            record = self.store.read("plan", key)
            if not record:
                raise ValueError("A selected plan no longer exists")
            plan = validate_plan(record["plan"])
            package = plan.get("package") or data.get("package")
            if not isinstance(package, str) or package not in installed:
                raise ValueError(f"Select or install the application for {plan['objective']}")
            if not plan["steps"]:
                raise ValueError(f"Add steps to {plan['objective']}")
            confirmed = data.get("preconditions_confirmed") == ids
            if plan.get("preconditions") and not confirmed:
                raise ValueError("Confirm the batch starting conditions")
            run = Run(android, model, self.store, serial, package, plan, self.settings["limits"])
            run.data.update(plan_id=key, preconditions_confirmed=confirmed,
                            report_screenshots=self.settings.get("report_screenshots", True))
            runs.append(run)
            entries.append({"objective": plan["objective"], "name": plan.get("name"), "state": "WAITING"})
        self.batch = {"active": True, "entries": entries}
        self.batch_stop = False
        self.run = None
        self.worker = threading.Thread(target=self.run_batch, args=(runs,), daemon=True)
        self.worker.start()
        return {"accepted": True}

    def dispatch(self, method, path, data):
        android = self.android()
        if method == "POST" and path == "/api/adb/connection":
            port = data.get("port")
            if port is not None and (type(port) is not int or not 1024 <= port <= 65535 or port == 5037):
                raise ValueError("Enter a forwarded port from 1024 to 65535 other than 5037")
            with self.lock:
                if self.worker and self.worker.is_alive():
                    raise ValueError("Wait until the current test run finishes to switch ADB connections")
                if port is not None:
                    android.server_port = port
                    android.devices()
                self.adb_tunnel_port = port
                self.preview_sizes.clear()
                return {"port": port}
        if self.adb_tunnel_port is not None and path in (
            "/api/emulators/start", "/api/emulators/end", "/api/emulators/connect-local"
        ):
            raise ValueError("Manage the emulator on its host computer while using an ADB tunnel")
        model = Model(self.settings["llm"])
        if method == "GET":
            if path == "/api/batch":
                with self.lock:
                    if self.batch:
                        current = None
                        if self.batch["active"] and self.run:
                            current = {
                                "completed_steps": sum(row["status"] != "NOT_EXECUTED"
                                                       for row in self.run.data["results"]),
                                "total_steps": len(self.run.data["results"]),
                            }
                        return {"active": self.batch["active"], "current": current,
                                "entries": [dict(entry) for entry in self.batch["entries"]]}
                    return {"active": False, "entries": []}
            if path == "/api/health":
                return {"status": "ok", "schema_version": 1}
            if path == "/api/session":
                document = self.config.document
                startup = {key: document[key] for key in (
                    "schema_version", "database_path", "emulator_path", "emulator_headless",
                    "emulator_preview_interval_ms", "emulator_skin_overrides",
                )}
                startup["server"] = {key: document["server"][key] for key in ("host", "port", "open_browser")}
                return {"token": self.token, "settings": self.settings, "startup_settings": startup,
                        "adb_tunnel_port": self.adb_tunnel_port,
                        "active_run_id": self.run.data["id"] if self.run and self.worker and self.worker.is_alive()
                        and not (self.batch and self.batch["active"])
                        and self.run.data["state"] not in ("COMPLETED", "FAILED", "STOPPED") else None,
                        "emulator_preview_interval_ms": self.config.document["emulator_preview_interval_ms"]}
            if path == "/api/emulators":
                if self.adb_tunnel_port is not None:
                    return []
                return Emulators(android.path, self.config.document["emulator_path"]).list()
            if path == "/api/emulators/status":
                if self.adb_tunnel_port is not None:
                    return {"serial": ""}
                name = self.emulator_name
                exited = self.emulator and self.emulator.poll() is not None
                try:
                    running = self.find_emulator(android, name) if name else ""
                except ValueError:
                    if exited:
                        raise ValueError(exit_message(self.emulator_log)) from None
                    raise
                if not running and name and not exited and time.monotonic() >= self.next_emulator_connect:
                    self.next_emulator_connect = time.monotonic() + 5
                    try:
                        android.connect_local_emulator()
                        running = self.find_emulator(android, name)
                    except ValueError:
                        pass
                if running:
                    return {"serial": running if android.emulator_booted(running) else ""}
                if name and (pending := self.find_emulator(android, name, include_unauthorized=True)):
                    return {"serial": "", "needs_authorization": True, "preview_serial": pending}
                if exited:
                    raise ValueError(exit_message(self.emulator_log))
                return {"serial": ""}
            if path == "/api/devices":
                return android.devices()
            if path == "/api/plans":
                return self.store.read("plan")
            if path == "/api/runs":
                history = []
                for row in self.store.read("run"):
                    issue = next((result for result in row.get("results", [])
                                  if result.get("status") in ("FAILED", "BLOCKED", "INCONCLUSIVE")), None)
                    history.append(dict(
                        id=row["id"], state=row["state"], objective=row["plan"]["objective"],
                        name=row["plan"].get("name"), started=row.get("started"), metrics=row.get("metrics"),
                        plan_id=row.get("plan_id") or row.get("generated_plan_id"),
                        package=row.get("package"), app_version=row.get("app_version"),
                        device=(row.get("device") or {}).get("model"), serial=row.get("serial"),
                        issue=dict(step=issue.get("step"), status=issue["status"]) if issue else None,
                    ))
                return history
            if path.startswith("/api/run/"):
                return self.store.read("run", path.removeprefix("/api/run/"))
            if path.startswith("/api/evidence/"):
                return self.store.read("evidence", path.removeprefix("/api/evidence/"))
            raise LookupError()
        if path == "/api/emulators/connect-local":
            return {"serial": android.connect_local_emulator()}
        if path == "/api/emulators/end":
            with self.lock:
                name = data.get("name")
                if not isinstance(name, str) or not name:
                    raise ValueError("Select an Android Virtual Device")
                owned = self.emulator if name == self.emulator_name else None
                if self.worker and self.worker.is_alive():
                    raise ValueError("Stop the active test run before ending an emulator")
                serial = ""
                try:
                    serial = self.find_emulator(android, name, include_unauthorized=True)
                    direct = "127.0.0.1:5555"
                    if serial:
                        android.end_emulator("emulator-5554" if serial == direct else serial)
                        return {"name": name, "serial": serial}
                except ValueError:
                    if not owned or owned.poll() is not None:
                        raise
                if owned and owned.poll() is None:
                    try:
                        owned.terminate()
                    except ProcessLookupError:
                        pass
                    return {"name": name, "serial": serial}
                raise ValueError("No running emulator matches " + name)
        if path == "/api/emulators/start":
            with self.lock:
                name = data.get("name")
                if not isinstance(name, str) or not name:
                    raise ValueError("Select an installed Android Virtual Device")
                show_window = data.get("show_window", False)
                if not isinstance(show_window, bool):
                    raise ValueError("Show emulator window must be a boolean")
                skip_adb_auth = data.get("skip_adb_auth", False)
                if not isinstance(skip_adb_auth, bool):
                    raise ValueError("Skip local emulator ADB prompt must be a boolean")
                if skip_adb_auth and not self.find_emulator(android, name) and (
                    (self.emulator and self.emulator.poll() is None and self.emulator_name == name)
                    or self.find_emulator(android, name, include_unauthorized=True)
                ):
                    raise ValueError("End this emulator, then launch it with Skip local emulator ADB prompt checked")
                if self.emulator and self.emulator.poll() is None:
                    if self.emulator_name == name:
                        return {"name": name, "reused": True}
                    raise ValueError("Studio already started " + self.emulator_name)
                if self.find_emulator(android, name, include_unauthorized=True):
                    self.emulator_name = name
                    self.emulator = None
                    return {"name": name, "reused": True}
                manager = Emulators(android.path, self.config.document["emulator_path"])
                skin = self.config.document["emulator_skin_overrides"].get(name, "")
                self.emulator = manager.launch(name, self.emulator_log, skin,
                                               headless=self.config.document["emulator_headless"] and not show_window,
                                               skip_adb_auth=skip_adb_auth)
                self.emulator_name = name
                self.next_emulator_connect = 0
                return {"name": name}
        if path == "/api/adb/settings":
            adb = validate_adb_path(data.get("adb", ""))
            with self.lock:
                settings = {**self.settings, "adb": adb}
                self.config.save(settings)
                self.settings = settings
                return {"adb": adb}
        if path == "/api/settings":
            with self.lock:
                settings = validate_settings(data)
                connection = Model(settings["llm"]).test_connection()
                self.config.save(settings)
                self.settings = settings
                return {"connection": connection, "settings": settings}
        if path == "/api/model/test":
            return Model(data.get("llm", self.settings["llm"])).test_connection()
        if path == "/api/plan/generate":
            objective = data.get("objective")
            if not isinstance(objective, str) or not 0 < len(objective.strip()) <= 5000:
                raise ValueError("Enter a test objective of 1–5000 characters")
            return validate_plan(model.generate_plan(objective))
        if path == "/api/plan/validate":
            return validate_plan(data.get("plan"))
        if path == "/api/plan/delete":
            key = data.get("id")
            if not isinstance(key, str) or not 0 < len(key) <= 64:
                raise ValueError("Select a saved plan")
            if not self.store.delete("plan", key):
                raise ValueError("This plan has already been removed")
            return {"deleted": key}
        if path == "/api/plan/save":
            plan = validate_plan(data.get("plan"))
            key = data.get("id") or secrets.token_hex(16)
            if not isinstance(key, str) or len(key) > 64:
                raise ValueError("Invalid plan identifier")
            record = {"id": key, "plan": plan}
            self.store.write("plan", key, record)
            return record
        if path == "/api/batch/stop":
            with self.lock:
                if self.batch and self.batch["active"]:
                    self.batch_stop = True
                    if self.run and self.run.data["state"] not in ("COMPLETED", "FAILED", "STOPPED"):
                        self.run.control("stop")
            return {"accepted": True}
        if path == "/api/control":
            if not self.run or data.get("id") != self.run.data["id"]:
                raise ValueError("Select the current run")
            self.run.control(data.get("command"))
            return {"accepted": True}
        serial, package = data.get("serial"), data.get("package")
        if path in ("/api/emulators/preview", "/api/emulators/action"):
            if not isinstance(serial, str):
                raise ValueError("Select an Android emulator")
            key = (android.path, serial)
            cached = self.preview_sizes.get(key)
            fresh = cached and time.monotonic() - cached[1] < 2
            if (not fresh or path == "/api/emulators/action") and not android.is_emulator(serial):
                raise ValueError("Select an Android emulator")
            if path == "/api/emulators/preview":
                image, dimensions = android.preview(serial)
                self.preview_sizes[key] = (dimensions, time.monotonic())
                width, height = dimensions
                if data.get("format") == "png":
                    return image, {"X-Studio-Screen-Width": str(width), "X-Studio-Screen-Height": str(height)}
                return {"width": width, "height": height, "image": base64.b64encode(image).decode()}
            if data.get("authorized") is not True:
                raise ValueError("Enable controls for the selected emulator")
            with self.lock:
                if self.worker and self.worker.is_alive():
                    raise ValueError("Stop the active test run before controlling the emulator")
                android.control_emulator(serial, data.get("action"), cached[0] if fresh else None)
                return {"status": "executed"}
        if path not in ("/api/logcat", "/api/packages", "/api/observe", "/api/analyze", "/api/suggest",
                        "/api/action", "/api/start", "/api/explore", "/api/app-version", "/api/batch/start"):
            raise LookupError()
        if not isinstance(serial, str) or not any(
            d["serial"] == serial and d["status"] == "device" for d in android.devices()
        ):
            raise ValueError("Select an online, authorized Android device")
        if path == "/api/logcat":
            return {"lines": android.logcat(serial).splitlines()}
        if path == "/api/batch/start":
            with self.lock:
                if self.worker and self.worker.is_alive():
                    raise ValueError("An execution is already active")
                if data.get("authorized") is not True:
                    raise ValueError("Confirm authorization to test all selected applications")
                return self.start_batch(android, model, serial, data)
        if path == "/api/packages":
            return {"packages": android.packages(serial), "device": android.info(serial)}
        if not isinstance(package, str) or not package:
            raise ValueError("Select your authorized test application")
        if path == "/api/app-version":
            return {"version": android.app_version(serial, package)}
        if path in ("/api/observe", "/api/analyze", "/api/suggest"):
            if data.get("authorized") is not True:
                raise ValueError("Confirm authorization to inspect this device and application")
            screen = android.observe(serial)
            if screen["package"] != package:
                raise ValueError("Launch the selected app before inspecting its screen")
            if path == "/api/suggest":
                objective = data.get("objective")
                if not isinstance(objective, str) or not 0 < len(objective.strip()) <= 5000:
                    raise ValueError("Enter an objective before requesting an action")
                proposal = model.suggest_action({"instruction": objective, "screen": public_screen(screen)})
                return validate_action(proposal)
            return screen if path == "/api/observe" else model.analyze_screen(screen)
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise ValueError("An execution is already active")
            if data.get("authorized") is not True:
                raise ValueError("Confirm authorization for this device and application")
            if path == "/api/action":
                android.execute(serial, package, data.get("action"))
                return {"schema_version": 1, "status": "executed"}
            if path == "/api/explore":
                objective = data.get("objective", "")
                if not isinstance(objective, str) or len(objective) > 5000:
                    raise ValueError("Enter an objective of up to 5000 characters")
                if not PACKAGE.fullmatch(package) or not android.is_emulator(serial):
                    raise ValueError("Select an emulator and an installed test application for exploration")
                if self.settings["llm"]["provider"] == "mock":
                    raise ValueError("Connect a model in Settings before exploring")
                if self.settings["limits"]["actions"] < 4:
                    raise ValueError("Exploration and replay require an action limit of at least four")
                self.run = Exploration(android, model, self.store, serial, package,
                                       objective.strip() or f"Explore {package}", self.settings["limits"])
            else:
                plan = validate_plan(data.get("plan"))
                if not plan["steps"]:
                    raise ValueError("Add reviewed test steps before running")
                confirmed = data.get("preconditions_confirmed") is True
                if plan.get("preconditions") and not confirmed:
                    raise ValueError("Confirm the plan starting conditions before running")
                self.run = Run(android, model, self.store, serial, package, plan, self.settings["limits"])
                self.run.data["preconditions_confirmed"] = confirmed
                plan_id = data.get("plan_id")
                if (isinstance(plan_id, str) and 0 < len(plan_id) <= 64
                        and self.store.read("plan", plan_id)):
                    self.run.data["plan_id"] = plan_id
            self.run.data["report_screenshots"] = self.settings.get("report_screenshots", True)
            self.run.persist()
            self.worker = threading.Thread(target=self.run.run, daemon=True)
            self.worker.start()
            return {"id": self.run.data["id"]}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, status, body, content_type, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type if content_type == "image/png" else content_type + "; charset=utf-8")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data: blob:; "
                         "object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def receive_apk(self, studio):
        if self.headers.get_content_type() != "application/octet-stream":
            raise ValueError("Expected an APK upload")
        remaining = int(self.headers.get("Content-Length", "0"))
        if not 0 < remaining <= MAX_APK_SIZE:
            raise ValueError("APK must be between 1 byte and 1 GiB")
        serial = self.headers.get("X-Studio-Device")
        if not serial:
            raise ValueError("Select an online, authorized Android device")
        with tempfile.TemporaryDirectory(prefix="studio-apk-") as folder:
            path = Path(folder) / "upload.apk"
            with path.open("wb") as output:
                while remaining:
                    block = self.rfile.read(min(remaining, 1024 * 1024))
                    if not block:
                        raise ValueError("APK upload was interrupted")
                    output.write(block)
                    remaining -= len(block)
            studio.install_apk(serial, path)
        self.send(200, b'{"status":"installed"}', "application/json")

    def process(self, method):
        studio = self.server.studio
        host = f"127.0.0.1:{self.server.server_port}"
        data = {}
        try:
            if (self.headers.get("Host") != host
                or self.headers.get("Origin") not in (None, "http://" + host)
                or self.headers.get("Sec-Fetch-Site") not in (None, "none", "same-origin")):
                raise PermissionError()
            path = urlsplit(self.path).path
            if method == "POST":
                if not secrets.compare_digest(self.headers.get("X-Studio-Token", "").encode(), studio.token.encode()):
                    raise PermissionError()
                if path == "/api/install-apk":
                    self.receive_apk(studio)
                    return
                if self.headers.get_content_type() != "application/json":
                    raise ValueError("Expected application/json")
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 1_000_000:
                    raise ValueError("Invalid request size")
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise ValueError("Expected a JSON object")
            if method == "GET" and path in ASSETS:
                filename, kind = ASSETS[path]
                try:
                    content = (ROOT / "static" / filename).read_bytes()
                except FileNotFoundError as exc:
                    raise LookupError() from exc
                self.send(200, content, kind)
                return
            result = studio.dispatch(method, path, data)
            if result is None:
                raise LookupError()
            if isinstance(result, tuple):
                image, headers = result
                self.send(200, image, "image/png", headers)
            else:
                self.send(200, json.dumps(result).encode(), "application/json")
        except PermissionError:
            self.send(403, b'{"error":"Request denied"}', "application/json")
        except LookupError:
            self.send(404, b'{"error":"Resource not found"}', "application/json")
        except (ValueError, OSError) as exc:
            message = str(exc) if isinstance(exc, ValueError) else "Local resource could not be accessed"
            self.send(400, json.dumps({"error": message}).encode(), "application/json")

    def do_GET(self):
        self.process("GET")

    def do_POST(self):
        self.process("POST")


def is_studio_listener(host, port):
    connection = HTTPConnection(host, port, timeout=2)
    try:
        connection.request("GET", "/")
        response = connection.getresponse()
        return response.status == 200 and b"<title>Android AI Test Studio</title>" in response.read(8192)
    except (OSError, HTTPException):
        return False
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=ROOT / "studio-config.txt")
    parser.add_argument("--port", type=int)
    parser.add_argument("--data", type=Path)
    parser.add_argument("--llm-config")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--diagnostics", action="store_true")
    modes.add_argument("--repair-adb", action="store_true", help="Restart the local ADB daemon using the configured executable")
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args()
    try:
        config = Config(args.config)
        server_settings = config.document["server"]
        port = server_settings["port"] if args.port is None else args.port
        if not 0 <= port <= 65535:
            raise ValueError("--port must be from 0 to 65535")
        studio = Studio(Store(args.data or config.database_path), args.llm_config, config=config)
        if args.repair_adb:
            adb = Android(studio.settings["adb"]).path
            if not adb:
                raise ValueError("ADB executable missing. Set the Android SDK ADB path in studio-config.txt.")
            print(f"Restarting the local ADB daemon with {adb}", flush=True)
            subprocess.run([adb, "kill-server"], timeout=15)
            subprocess.run([adb, "start-server"], timeout=20, check=True)
            subprocess.run([adb, "devices", "-l"], timeout=20, check=True)
            return
        if args.diagnostics:
            with socket.socket() as probe:
                try:
                    probe.bind((server_settings["host"], 0))
                    print("Loopback bind available")
                except OSError as exc:
                    print("Loopback bind unavailable:", exc)
            print("ADB executable:", Android(studio.settings["adb"]).path)
            print(json.dumps(Android(studio.settings["adb"]).devices(), indent=2))
            return
        host = server_settings["host"]
        open_browser = args.open_browser or server_settings["open_browser"]
        try:
            server = ThreadingHTTPServer((host, port), Handler)
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
            url = f"http://{host}:{port}"
            if not is_studio_listener(host, port):
                raise ValueError(f"Port {port} is in use. Change server.port in {config.path}.") from exc
            print(f"Studio is already running at {url}", flush=True)
            if open_browser:
                webbrowser.open(url)
            return
        server.studio = studio
        url = f"http://127.0.0.1:{server.server_port}"
        print(f"Open {url}", flush=True)
        with server:
            if open_browser:
                threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
            server.serve_forever()
    except KeyboardInterrupt:
        return
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Studio could not start or complete diagnostics. {exc}\n")


if __name__ == "__main__":
    main()
