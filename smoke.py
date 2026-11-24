"""Drive an authorized fixture app through the same executor and run engine as Studio."""
import argparse
import json
from pathlib import Path
import tempfile
import time

from agent import Run
from android import Android
from llm import DEFAULT, Model
from storage import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adb", default="")
    parser.add_argument("--serial", required=True)
    parser.add_argument("--package", required=True)
    parser.add_argument("--field", required=True, help="Resource ID of a non-password editable fixture field")
    parser.add_argument("--next", required=True, help="Resource ID of a button that navigates within the fixture app")
    parser.add_argument("--destination", required=True, help="Resource ID visible on the destination screen")
    parser.add_argument("--authorized", action="store_true", required=True)
    args = parser.parse_args()
    android = Android(args.adb)
    try:
        if not args.serial.startswith("emulator-") or not any(
            row["serial"] == args.serial and row["status"] == "device" for row in android.devices()
        ):
            raise ValueError("The requested emulator is unavailable")
        android.execute(args.serial, args.package, {"schema_version": 1, "action": "launch_app"})
        deadline = time.monotonic() + 20
        while android.foreground(args.serial) != args.package and time.monotonic() < deadline:
            time.sleep(0.25)
        screen = android.observe(args.serial)
        if screen["package"] != args.package or not screen["image"] or not screen["nodes"]:
            raise ValueError("Fixture app did not produce a screenshot and populated UI hierarchy")

        def target(value):
            return {"strategy": "resource_id", "value": value}

        plan = {"objective": "Type into the fixture app and navigate to its destination screen",
                "steps": [
                    {"action": {"schema_version": 1, "action": "tap", "target": target(args.field)},
                     "assertions": [{"type": "element_enabled", "target": target(args.field)}]},
                    {"action": {"schema_version": 1, "action": "input_text", "text": "StudioSmoke"},
                     "assertions": [{"type": "text_equals", "value": "StudioSmoke",
                                     "target": target(args.field)}]},
                    {"action": {"schema_version": 1, "action": "tap", "target": target(args.next)},
                     "assertions": [{"type": "element_visible", "target": target(args.destination)},
                                    {"type": "screen_transition"}]},
                ]}
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "smoke.sqlite")
            run = Run(android, Model(DEFAULT), store, args.serial, args.package, plan)
            run.run()
            result = store.read("run", run.data["id"])
            print(json.dumps({"state": result["state"], "metrics": result["metrics"],
                              "results": result["results"]}, indent=2))
            if result["state"] != "COMPLETED" or not result.get("analysis"):
                raise ValueError("Fixture run or report analysis did not complete")
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Smoke did not pass. {exc}\n")


if __name__ == "__main__":
    main()
