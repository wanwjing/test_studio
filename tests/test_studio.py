from http.server import ThreadingHTTPServer
import io
import json
from pathlib import Path
import struct
import tempfile
import threading
from urllib.request import urlopen
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent import Run
from android import Android, parse_devices, parse_ui, png_size, validate_action
from config import Config
from emulator import Emulators, exit_message
from llm import DEFAULT, Model, validate_config
from storage import Store
from studio import Handler, Studio, is_studio_listener
from verification import evaluate


PACKAGE = "com.example.test"
XML = (b'<hierarchy><node package="com.example.test" resource-id="id/go" content-desc="Go" '
       b'text="Go" enabled="true" bounds="[0,0][20,20]"><node password="true" '
       b'text="secret"/></node></hierarchy>')
SELECTOR = {"strategy": "resource_id", "value": "id/go"}
ACTION = {"schema_version": 1, "action": "tap", "target": SELECTOR}
ASSERTION = {"type": "element_visible", "target": SELECTOR}


class FakeAndroid(Android):
    def __init__(self):
        self.path = "/local/adb"
        self.calls = []
        self.picture = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + struct.pack(">II", 100, 200) + bytes(9)

    def _run(self, *args, serial=None):
        self.calls.append((args, serial))
        if args == ("devices", "-l"):
            return b"List of devices attached\nemulator-5554 device model:Test\n"
        if args == ("shell", "dumpsys", "package", PACKAGE):
            return b"    versionName=1.2.3\n"
        if args[:2] == ("shell", "dumpsys"):
            return b"mResumedActivity: ActivityRecord{abc u0 com.example.test/.Main t1}"
        if args[:2] == ("shell", "uiautomator"):
            return XML
        if args[:2] == ("exec-out", "screencap"):
            return self.picture
        if args[:2] == ("shell", "getprop"):
            return b"Test"
        return b""


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "test.sqlite")
        self.android = FakeAndroid()
        self.studio = Studio(self.store)

    def request(self, path, data=None, token=None, host="127.0.0.1:8765", origin=None):
        headers = [f"Host: {host}", "Connection: close"]
        if origin:
            headers.append(f"Origin: {origin}")
        body = b"" if data is None else json.dumps(data).encode()
        if body:
            headers.extend(["Content-Type: application/json", f"Content-Length: {len(body)}"])
        if token:
            headers.append(f"X-Studio-Token: {token}")
        method = "POST" if body else "GET"
        raw = (f"{method} {path} HTTP/1.1\r\n" + "\r\n".join(headers) + "\r\n\r\n").encode() + body

        class InProcessHandler(Handler):
            def setup(inner):
                inner.rfile = io.BytesIO(raw)
                inner.wfile = io.BytesIO()

            def finish(inner):
                pass

        interaction = InProcessHandler(None, ("127.0.0.1", 9999),
                                       SimpleNamespace(studio=self.studio, server_port=8765))
        return interaction.wfile.getvalue()

    def test_existing_studio_listener_identification(self):
        with patch("studio.HTTPConnection") as client:
            response = client.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = b"<title>Android AI Test Studio</title>"
            self.assertTrue(is_studio_listener("127.0.0.1", 8765))
            response.read.return_value = b"<title>Another app</title>"
            self.assertFalse(is_studio_listener("127.0.0.1", 8765))
            client.return_value.close.assert_called()

    def test_http_handler_and_assets(self):
        self.assertIn(b"200 OK", self.request("/api/health"))
        self.assertIn(b"Android AI Test Studio", self.request("/"))
        self.assertIn(b"404 Not Found", self.request("/../studio.py"))
        self.assertIn(b"403 Forbidden", self.request("/api/session", host="example.org"))
        self.assertIn(b"403 Forbidden", self.request("/api/session", origin="https://example.org"))
        self.assertIn(b"403 Forbidden", self.request("/api/model/test", {"llm": DEFAULT}))
        self.assertIn(b"connected", self.request("/api/model/test", {"llm": DEFAULT}, self.studio.token))

    def test_live_loopback_health_when_available(self):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        except PermissionError:
            self.skipTest("sandbox prohibits loopback bind")
        with server:
            server.studio = self.studio
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                with urlopen(f"http://127.0.0.1:{server.server_port}/api/health", timeout=3) as response:
                    self.assertEqual(json.load(response)["status"], "ok")
            finally:
                server.shutdown()
                thread.join()

    def test_settings_persist_and_failed_apply_preserves_settings(self):
        data = self.studio.settings | {"report_screenshots": False}
        self.assertIn(b"200 OK", self.request("/api/settings", data, self.studio.token))
        self.assertIn(b"400 Bad Request", self.request(
            "/api/settings", {**data, "llm": {**DEFAULT, "provider": "unknown"}}, self.studio.token))
        self.assertFalse(Studio(self.store).settings["report_screenshots"])

    def test_adb_path_can_be_saved_without_model_inference(self):
        adb = Path(self.temp.name) / "adb"
        adb.touch(mode=0o700)
        with patch.object(Model, "test_connection", side_effect=AssertionError("Unexpected model call")):
            self.assertIn(b"200 OK", self.request("/api/adb/settings", {"adb": str(adb)}, self.studio.token))
            self.assertIn(b"400 Bad Request", self.request("/api/adb/settings", {"adb": "adb"}, self.studio.token))
        self.assertEqual(Studio(self.store).settings["adb"], str(adb))

    def test_text_config_imports_legacy_settings_and_keeps_server_group(self):
        path = Path(self.temp.name) / "deployment.txt"
        self.store.write("settings", "active", {"report_screenshots": False})
        config = Config(path)
        studio = Studio(self.store, config=config)
        self.assertFalse(studio.settings["report_screenshots"])
        document = json.loads(path.read_text())
        document["server"]["port"] = 9876
        path.write_text(json.dumps(document))
        config = Config(path)
        studio = Studio(self.store, config=config)
        studio.dispatch("POST", "/api/adb/settings", {"adb": ""})
        self.assertEqual(Config(path).document["server"]["port"], 9876)
        self.assertFalse(Config(path).document["settings"]["report_screenshots"])

    def test_emulator_launch_and_preview(self):
        manager = Emulators(None, "/sdk/emulator")
        destination = Path(self.temp.name) / "emulator.log"
        with patch.object(manager, "list", return_value=["Fixture_API_29"]):
            with patch("emulator.subprocess.Popen") as start:
                manager.launch("Fixture_API_29", destination, "2208x1840")
                self.assertEqual(start.call_args.args[0],
                                 ["/sdk/emulator", "-avd", "Fixture_API_29", "-no-window", "-skin", "2208x1840"])
                self.assertTrue(start.call_args.kwargs["start_new_session"])
                manager.launch("Fixture_API_29", destination, skip_adb_auth=True)
                self.assertIn("-skip-adb-auth", start.call_args.args[0])
                self.assertIn("-no-snapshot-load", start.call_args.args[0])
                with self.assertRaises(ValueError):
                    manager.launch("Unlisted", destination)
        with patch("studio.Android", return_value=self.android):
            result = self.studio.dispatch("POST", "/api/emulators/preview", {"serial": "emulator-5554"})
        self.assertEqual((result["width"], result["height"]), (100, 200))

    def test_direct_local_emulator_connection(self):
        expected = "127.0.0.1:5555"
        def fake_run(*args, serial=None):
            if args == ("connect", expected):
                return b"connected"
            if args == ("devices", "-l"):
                return b"List of devices attached\n127.0.0.1:5555 device"
            if args == ("shell", "getprop", "ro.kernel.qemu") and serial == expected:
                return b"1"
            self.fail((args, serial))

        with patch.object(self.android, "_run", side_effect=fake_run), \
                patch("studio.Android", return_value=self.android):
            self.assertEqual(self.studio.dispatch("POST", "/api/emulators/connect-local", {}),
                             {"serial": expected})

    def test_emulator_start_reuses_running_avd(self):
        with patch("studio.Android", return_value=self.android), \
                patch.object(self.android, "emulator_name", return_value="Fixture_API_29"), \
                patch.object(self.android, "emulator_booted", return_value=True), \
                patch("studio.Emulators.launch") as launch:
            outcome = self.studio.dispatch("POST", "/api/emulators/start", {"name": "Fixture_API_29"})
            self.assertTrue(outcome["reused"])
            launch.assert_not_called()
            self.assertEqual(self.studio.dispatch("GET", "/api/emulators/status", {}),
                             {"serial": "emulator-5554"})

    def test_start_reuses_emulator_waiting_for_authorization(self):
        devices = [{"serial": "emulator-5554", "status": "unauthorized", "emulator": True}]
        with patch("studio.Android", return_value=self.android), \
                patch.object(self.android, "devices", return_value=devices), \
                patch.object(self.android, "emulator_name", return_value="Fixture_API_29"), \
                patch.object(self.android, "connect_local_emulator", side_effect=ValueError), \
                patch("studio.Emulators.launch") as launch:
            payload = {"name": "Fixture_API_29", "show_window": True}
            result = self.studio.dispatch("POST", "/api/emulators/start", payload)
            self.assertTrue(result["reused"])
            launch.assert_not_called()
            self.assertEqual(self.studio.dispatch("GET", "/api/emulators/status", {}),
                             {"serial": "", "needs_authorization": True, "preview_serial": "emulator-5554"})
            with self.assertRaisesRegex(ValueError, "End this emulator"):
                self.studio.dispatch("POST", "/api/emulators/start", {**payload, "skip_adb_auth": True})
            self.assertEqual(self.studio.dispatch("POST", "/api/emulators/end", payload)["serial"],
                             "emulator-5554")

    def test_end_emulator_targets_the_selected_avd(self):
        with patch("studio.Android", return_value=self.android), \
                patch.object(self.android, "emulator_name", return_value="Fixture_API_29"):
            with self.assertRaisesRegex(ValueError, "No running emulator"):
                self.studio.dispatch("POST", "/api/emulators/end", {"name": "Other"})
            result = self.studio.dispatch("POST", "/api/emulators/end", {"name": "Fixture_API_29"})
        self.assertEqual(result["serial"], "emulator-5554")
        self.assertEqual(self.android.calls[-1], (("emu", "kill"), "emulator-5554"))

    def test_live_emulator_controls_validate_taps_and_authorization(self):
        payload = {"serial": "emulator-5554", "action": {"action": "tap", "point": [20, 30],
                                                        "size": [100, 200]}}
        with patch("studio.Android", return_value=self.android):
            with self.assertRaisesRegex(ValueError, "Enable controls"):
                self.studio.dispatch("POST", "/api/emulators/action", payload)
            payload["authorized"] = True
            self.studio.dispatch("POST", "/api/emulators/action", payload)
            self.assertEqual(self.android.calls[-1], (("shell", "input", "tap", "20", "30"),
                                                    "emulator-5554"))
            tap = payload["action"]
            payload["action"] = {"action": "swipe", "start": [20, 30], "end": [75, 80], "size": [100, 200]}
            self.studio.dispatch("POST", "/api/emulators/action", payload)
            self.assertEqual(self.android.calls[-1], (("shell", "input", "swipe", "20", "30", "75", "80", "350"),
                                                    "emulator-5554"))
            payload["action"] = tap
            payload["action"]["size"] = [200, 100]
            with self.assertRaisesRegex(ValueError, "size changed"):
                self.studio.dispatch("POST", "/api/emulators/action", payload)
            payload["action"] = {"action": "home"}
            self.studio.dispatch("POST", "/api/emulators/action", payload)
            self.assertEqual(self.android.calls[-1], (("shell", "input", "keyevent", "KEYCODE_HOME"),
                                                    "emulator-5554"))

    def test_binary_preview_and_tap_reuse_the_latest_dimensions(self):
        serial = "emulator-5554"
        with patch("studio.Android", return_value=self.android):
            response = self.request("/api/emulators/preview", {"serial": serial, "format": "png"},
                                    self.studio.token)
            self.assertIn(b"Content-Type: image/png", response)
            self.assertIn(b"X-Studio-Screen-Width: 100", response)
            self.assertTrue(response.endswith(self.android.picture))
            self.studio.dispatch("POST", "/api/emulators/action", {
                "serial": serial, "authorized": True,
                "action": {"action": "tap", "point": [50, 100], "size": [100, 200]},
            })
        self.assertEqual(self.android.calls, [
            (("exec-out", "screencap", "-p"), serial),
            (("devices", "-l"), None),
            (("shell", "input", "tap", "50", "100"), serial),
        ])

    def test_unauthorized_emulator_preview_and_tap_use_local_console(self):
        serial = "emulator-5554"
        calls = []

        def adb(*args, serial=None):
            calls.append((args, serial))
            if args == ("devices", "-l"):
                return b"List of devices attached\nemulator-5554 unauthorized transport_id:1\n"
            if args[0] == "exec-out":
                raise ValueError("ADB authorization pending")
            if args[:3] == ("emu", "screenrecord", "screenshot"):
                Path(args[3]).write_bytes(self.android.picture)
            return b"OK\r\n"

        with patch.object(self.android, "_run", side_effect=adb), patch("studio.Android", return_value=self.android):
            image, headers = self.studio.dispatch("POST", "/api/emulators/preview", {
                "serial": serial, "format": "png",
            })
            self.assertEqual(image, self.android.picture)
            self.assertEqual(headers["X-Studio-Screen-Width"], "100")
            self.studio.dispatch("POST", "/api/emulators/action", {
                "serial": serial, "authorized": True,
                "action": {"action": "tap", "point": [99, 199], "size": [100, 200]},
            })
        self.assertEqual(calls[-2:], [
            (("emu", "event", "mouse", "99", "199", "0", "1"), serial),
            (("emu", "event", "mouse", "99", "199", "0", "0"), serial),
        ])

    def test_logcat_uses_a_bounded_read_on_the_selected_device(self):
        serial = "emulator-5554"
        with patch("studio.Android", return_value=self.android):
            result = self.studio.dispatch("POST", "/api/logcat", {"serial": serial})
        self.assertEqual(result["lines"], [])
        self.assertEqual(self.android.calls[-1], (("logcat", "-d", "-t", "200", "-v", "threadtime"), serial))

    def test_boot_status_retries_through_the_local_emulator_port(self):
        serial = "127.0.0.1:5555"
        self.studio.emulator_name = "Fixture_API_29"
        device = {"serial": serial, "status": "device", "emulator": False}
        with patch("studio.Android", return_value=self.android), \
                patch.object(self.android, "devices", side_effect=[[], [device]]), \
                patch.object(self.android, "connect_local_emulator", return_value=serial) as connect, \
                patch.object(self.android, "is_emulator", return_value=True), \
                patch.object(self.android, "emulator_name", return_value="Fixture_API_29"), \
                patch.object(self.android, "emulator_booted", return_value=True):
            self.assertEqual(self.studio.dispatch("GET", "/api/emulators/status", {}), {"serial": serial})
            connect.assert_called_once()

    def test_emulator_exit_message_identifies_acceleration_error(self):
        path = Path(self.temp.name) / "emulator.log"
        path.write_text("No accelerator found.\n")
        self.assertIn("Hardware acceleration", exit_message(path))

    def test_model_http_contract(self):
        config = {**DEFAULT, "provider": "openai-compatible", "model": "configured-model",
                  "endpoint_url": "http://127.0.0.1:9999/v1/chat/completions"}
        response = {"choices": [{"message": {"content": '{"ok": true}'}}]}
        path = Path(self.temp.name) / "plugin.json"
        path.write_text(json.dumps(config))
        with patch("llm.urllib.request.build_opener") as factory:
            factory.return_value.open.return_value = io.BytesIO(json.dumps(response).encode())
            Studio(self.store, path)
            request = factory.return_value.open.call_args.args[0]
            self.assertEqual(json.loads(request.data)["model"], "configured-model")
        self.assertEqual(Studio(self.store).settings["llm"]["model"], "configured-model")
        with self.assertRaises(ValueError):
            validate_config({**config, "endpoint_url": "https://example.com/chat"})

    def test_android_parsing_and_coordinate_validation(self):
        self.assertEqual(parse_devices("List of devices attached\nabc unauthorized")[0]["status"], "unauthorized")
        nodes = parse_ui(XML)
        self.assertEqual(nodes[1]["parent"], 0)
        self.assertEqual(nodes[1]["text"], "")
        self.assertEqual(png_size(self.android.picture), (100, 200))
        screen = self.android.observe("emulator-5554")
        action = {"schema_version": 1, "action": "tap", "point": [100, 0],
                  "fingerprint": screen["image_fingerprint"]}
        with self.assertRaisesRegex(ValueError, "outside the screen"):
            self.android.execute("emulator-5554", PACKAGE, action, screen)
        action.update(point=[99, 199], fingerprint="stale")
        with self.assertRaisesRegex(ValueError, "Screen changed"):
            self.android.execute("emulator-5554", PACKAGE, action, screen)
        self.android.execute("emulator-5554", PACKAGE, ACTION, screen)
        self.assertEqual(self.android.calls[-1], (("shell", "input", "tap", "10", "10"), "emulator-5554"))

    def test_command_is_serial_scoped_and_text_is_validated(self):
        with patch("android.subprocess.run") as call:
            call.return_value = SimpleNamespace(returncode=0, stdout=b"ok")
            Android("/opt/adb").foreground("emulator-5554")
            self.assertEqual(call.call_args.args[0][:3], ["/opt/adb", "-s", "emulator-5554"])
        with self.assertRaises(ValueError):
            validate_action({"schema_version": 1, "action": "input_text", "text": "a; id"})
        with self.assertRaises(ValueError):
            validate_action({"schema_version": 2, "action": "tap", "target": SELECTOR})

    def create_run(self, assertion=ASSERTION, limits=None, model=None):
        plan = dict(objective="Verify the button is visible",
                    steps=[dict(action=ACTION, assertions=[assertion])])
        return Run(self.android, model or Model(DEFAULT), self.store, "emulator-5554", PACKAGE, plan, limits)

    def test_multistep_execution_and_persistence(self):
        run = self.create_run()
        run.plan["steps"].append(run.plan["steps"][0])
        run.data["results"].append(dict(step=1, status="NOT_EXECUTED", assertions=[]))
        with self.assertRaises(RuntimeError):
            run.transition("EXECUTING")
        run.run()
        saved = self.store.read("run", run.data["id"])
        self.assertEqual(saved["state"], "COMPLETED")
        self.assertEqual(saved["metrics"]["passed"], 2)
        self.assertEqual(saved["metrics"]["unique_screens"], 1)

    def test_adb_tunnel_switch_checks_connection_and_preserves_previous_on_failure(self):
        adb = Android("/local/adb")
        response = SimpleNamespace(returncode=0, stdout=b"List of devices attached\nemulator-5554 device\n")
        with patch("studio.Android", return_value=adb), patch(
            "android.socket.create_connection"
        ) as connect, patch("android.subprocess.run", return_value=response) as command:
            selected = self.studio.dispatch("POST", "/api/adb/connection", {"port": 15037})
            self.assertEqual(selected, {"port": 15037})
            devices = self.studio.dispatch("GET", "/api/devices", {})
            self.assertEqual(devices[0]["serial"], "emulator-5554")
            self.assertEqual(command.call_args.args[0],
                             ["/local/adb", "-H", "127.0.0.1", "-P", "15037", "devices", "-l"])
            connect.side_effect = OSError("connection refused")
            with self.assertRaisesRegex(ValueError, "tunnel is unreachable"):
                self.studio.dispatch("POST", "/api/adb/connection", {"port": 15038})
            self.assertEqual(self.studio.adb_tunnel_port, 15037)
            self.studio.dispatch("POST", "/api/adb/connection", {"port": None})
            self.studio.dispatch("GET", "/api/devices", {})
            self.assertEqual(command.call_args.args[0], ["/local/adb", "devices", "-l"])

    def test_deleting_saved_plan_preserves_run_history(self):
        plan = {"objective": "Check fixture", "steps": [{"action": ACTION, "assertions": [ASSERTION]}]}
        saved = self.studio.dispatch("POST", "/api/plan/save", {"plan": plan})
        self.store.write("run", "fixture-run", {"plan_id": saved["id"], "plan": plan})
        self.assertEqual(self.studio.dispatch("POST", "/api/plan/delete", {"id": saved["id"]}),
                         {"deleted": saved["id"]})
        self.assertEqual(self.studio.dispatch("GET", "/api/plans", {}), [])
        self.assertEqual(self.store.read("run", "fixture-run")["plan"], plan)

    def test_batch_runs_saved_plans_in_requested_order(self):
        first = self.studio.dispatch("POST", "/api/plan/save", {"plan": {
            "name": "Fixture transition", "objective": "First check", "package": PACKAGE,
            "preconditions": ["Open the fixture"],
            "steps": [{"action": ACTION, "assertions": [{"type": "screen_transition"}]}],
        }})
        second = self.studio.dispatch("POST", "/api/plan/save", {"plan": {
            "objective": "Second check", "package": PACKAGE,
            "steps": [{"action": ACTION, "assertions": [ASSERTION]}],
        }})
        ids = [first["id"], second["id"]]
        payload = {"serial": "emulator-5554", "authorized": True, "plan_ids": ids}
        with patch("studio.Android", return_value=self.android), patch.object(
            self.android, "packages", return_value=[PACKAGE]
        ):
            with self.assertRaisesRegex(ValueError, "Confirm the batch starting conditions"):
                self.studio.dispatch("POST", "/api/batch/start", payload)
            self.studio.dispatch("POST", "/api/batch/start", {
                **payload, "preconditions_confirmed": ids,
            })
            self.studio.worker.join(5)
            batch = self.studio.dispatch("GET", "/api/batch", {})
        self.assertFalse(batch["active"])
        self.assertEqual(batch["entries"][0]["name"], "Fixture transition")
        self.assertIsNone(batch["entries"][1]["name"])
        self.assertEqual([entry["state"] for entry in batch["entries"]], ["FAILED", "COMPLETED"])
        saved_ids = [self.store.read("run", entry["id"])["plan_id"] for entry in batch["entries"]]
        self.assertEqual(saved_ids, ids)

    def test_start_requires_preconditions_and_records_app_version(self):
        target = {"serial": "emulator-5554", "package": PACKAGE}
        payload = {**target, "authorized": True, "plan": {
            "objective": "Open the fixture",
            "preconditions": ["Complete onboarding"],
            "steps": [{"action": ACTION, "assertions": [ASSERTION]}],
        }}
        with patch("studio.Android", return_value=self.android):
            version = self.studio.dispatch("POST", "/api/app-version", target)
            self.assertEqual(version, {"version": "1.2.3"})
            with self.assertRaisesRegex(ValueError, "Confirm the plan starting conditions"):
                self.studio.dispatch("POST", "/api/start", payload)
            saved = self.studio.dispatch("POST", "/api/plan/save", {"plan": payload["plan"]})
            result = self.studio.dispatch("POST", "/api/start", {
                **payload, "preconditions_confirmed": True, "plan_id": saved["id"],
            })
            self.studio.worker.join(5)
            history = self.studio.dispatch("GET", "/api/runs", {})
        self.assertEqual(history[0]["plan_id"], saved["id"])
        run = self.store.read("run", result["id"])
        self.assertEqual(run["state"], "COMPLETED")
        self.assertTrue(run["preconditions_confirmed"])
        self.assertEqual(run["app_version"], version["version"])

    def test_explore_endpoint_records_and_replays_observed_path(self):
        self.studio.settings["llm"] = {
            **DEFAULT, "provider": "openai-compatible", "endpoint_url": "http://localhost/chat",
            "vision": True,
        }
        base_run = self.android._run
        detail = False

        def adb(*args, serial=None):
            nonlocal detail
            if args[:3] == ("shell", "pm", "list"):
                return f"package:{PACKAGE}\n".encode()
            if args[:3] == ("shell", "am", "force-stop"):
                detail = False
            if args[:3] == ("shell", "input", "tap"):
                detail = True
            if args[:2] == ("shell", "uiautomator"):
                xml = XML.replace(b'<node password="true" text="secret"/>', b'')
                return xml.replace(b'id/go', b'id/detail') if detail else xml
            return base_run(*args, serial=serial)

        self.android._run = adb
        responses = [
            {"action": ACTION},
            {"action": None},
            {"analysis": "", "recommendations": []},
        ]
        with patch("studio.Android", return_value=self.android), patch(
            "llm.urllib.request.build_opener"
        ) as factory:
            opener = factory.return_value
            opener.open.side_effect = [
                io.BytesIO(json.dumps({"choices": [{"message": {"content": json.dumps(result)}}]}).encode())
                for result in responses
            ]
            result = self.studio.dispatch("POST", "/api/explore", {
                "serial": "emulator-5554", "package": PACKAGE, "authorized": True, "objective": "Open detail",
            })
            self.studio.worker.join(5)
            request = json.loads(opener.open.call_args_list[0].args[0].data)
            self.assertEqual(request["messages"][1]["content"][1]["type"], "image_url")
        self.assertFalse(self.studio.worker.is_alive())
        run = self.store.read("run", result["id"])
        self.assertEqual(run["state"], "COMPLETED")
        self.assertEqual(run["metrics"]["passed"], 2)
        self.assertEqual(run["actions"], 4)
        saved = self.store.read("plan", run["generated_plan_id"])
        self.assertEqual(saved["plan"]["steps"][1]["assertions"][-1], {
            "type": "element_visible", "target": {"strategy": "resource_id", "value": "id/detail"},
        })

    def test_replay_blocks_coordinate_when_recorded_image_differs(self):
        run = self.create_run()
        run.plan["steps"][0]["action"] = {
            "schema_version": 1, "action": "tap", "point": [10, 10],
            "fingerprint": "0" * 64,
        }
        run.run()
        self.assertEqual(run.data["results"][0]["status"], "BLOCKED")
        self.assertIn("Screen changed", run.data["results"][0]["error"])
        self.assertFalse(any(args[:3] == ("shell", "input", "tap") for args, _ in self.android.calls))

    def test_failed_assertion_is_not_retried(self):
        run = self.create_run({"type": "screen_transition"})
        run.run()
        self.assertEqual(run.data["state"], "FAILED")
        self.assertEqual(run.data["results"][0]["status"], "FAILED")
        self.assertEqual(run.data["actions"], 1)
        screen = self.android.observe("emulator-5554")
        self.assertIsNone(evaluate(ASSERTION, screen, {**screen, "package": "com.other"}, PACKAGE))

    def test_recovery_and_retry_limits(self):
        self.android._run = self.duplicate_ui(self.android._run)
        model = Model(DEFAULT)
        plan = {"objective": "Verify the button", "steps": [
            {"instruction": "Tap Go", "assertions": [ASSERTION]},
        ]}
        with patch.object(model, "suggest_action", return_value=ACTION), patch.object(
            model, "analyze_failure", return_value={"target": {"strategy": "text", "value": "Go"}}
        ):
            run = Run(self.android, model, self.store, "emulator-5554", PACKAGE, plan)
            run.run()
            self.assertEqual(run.data["state"], "COMPLETED")
            self.assertEqual(run.data["metrics"]["recoveries_verified"], 1)
            for limits in ({"retries": 0}, {"repeated": 1}):
                run = Run(self.android, model, self.store, "emulator-5554", PACKAGE, plan, limits)
                run.run()
                self.assertEqual(run.data["state"], "FAILED")
                self.assertEqual(run.data["actions"], 1)

    def test_explicit_plan_skips_model_and_selector_recovery(self):
        self.android._run = self.duplicate_ui(self.android._run)
        model = Model(DEFAULT)
        alternative = {"strategy": "text", "value": "Go"}
        self.store.write("strategy", f"{PACKAGE}|resource_id|id/go", alternative)
        with patch.object(model, "call", side_effect=AssertionError("Unexpected model call")) as call:
            run = self.create_run(model=model)
            run.run()
        call.assert_not_called()
        self.assertEqual(run.data["execution_mode"], "plan")
        self.assertEqual(run.data["results"][0]["status"], "BLOCKED")
        self.assertEqual(run.data["actions"], 1)
        self.assertEqual(run.data["improvements"], [])

    def test_blocked_step_preserves_pre_action_screenshot(self):
        self.android._run = self.duplicate_ui(self.android._run)
        observe = self.android.observe

        def observe_with_evidence(serial):
            return {**observe(serial), "image": "fixture-screenshot"}

        with patch.object(self.android, "observe", side_effect=observe_with_evidence):
            run = self.create_run()
            run.run()
        result = self.store.read("run", run.data["id"])["results"][0]
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["evidence_timing"], "before")
        self.assertEqual(self.store.read("evidence", result["evidence"]), "fixture-screenshot")

    @staticmethod
    def duplicate_ui(original):
        def run(*args, **kwargs):
            output = original(*args, **kwargs)
            if output == XML:
                return output.replace(b"</hierarchy>", b'<node package="com.example.test" resource-id="id/go" '
                                      b'text="Other" enabled="true" bounds="[40,40][50,50]"/></hierarchy>')
            return output
        return run

    def test_stop_prevents_action(self):
        run = self.create_run()
        run.control("emergency")
        run.run()
        self.assertEqual(run.data["state"], "STOPPED")
        self.assertEqual(run.data["actions"], 0)

    def test_dashboard_accessibility_structure(self):
        from html.parser import HTMLParser

        class Markup(HTMLParser):
            def __init__(self):
                super().__init__()
                self.tags = []
            def handle_starttag(self, tag, attrs):
                self.tags.append((tag, dict(attrs)))

        parser = Markup()
        parser.feed(Path("static/index.html").read_text())
        labels = {attrs.get("for") for tag, attrs in parser.tags if tag == "label"}
        self.assertTrue(any(tag == "main" for tag, _ in parser.tags))
        self.assertTrue(any(tag == "img" and attrs.get("alt") for tag, attrs in parser.tags))
        for tag, attrs in parser.tags:
            if tag in ("select", "input", "textarea") and attrs.get("type") != "checkbox":
                self.assertIn(attrs["id"], labels)
        self.assertIn(":focus-visible", Path("static/style.css").read_text())


if __name__ == "__main__":
    unittest.main()
