import threading
import time
import uuid

from android import PACKAGE, SELECTORS, validate_action
from llm import public_screen
from verification import evaluate, validate_assertion


TRANSITIONS = {
    "IDLE": {"CONNECTING"}, "CONNECTING": {"OBSERVING", "EXPLORING"}, "OBSERVING": {"PLANNING"},
    "EXPLORING": {"EXPLORING", "OBSERVING"},
    "PLANNING": {"EXECUTING", "COMPLETED"}, "EXECUTING": {"VERIFYING", "RECOVERING"},
    "VERIFYING": {"OBSERVING", "RECOVERING", "COMPLETED"}, "RECOVERING": {"OBSERVING"},
}
TERMINAL = {"COMPLETED", "FAILED", "STOPPED"}
LIMITS = {"actions": 100, "retries": 3, "recoveries": 5, "seconds": 600, "repeated": 4}


def validate_plan(plan):
    if not isinstance(plan, dict) or not isinstance(plan.get("objective"), str) or not plan["objective"].strip():
        raise ValueError("A plan requires an objective")
    if "name" in plan and (not isinstance(plan["name"], str) or not 0 < len(plan["name"].strip()) <= 150):
        raise ValueError("Use a plan name of 1–150 characters")
    if "package" in plan and (not isinstance(plan["package"], str) or not PACKAGE.fullmatch(plan["package"])):
        raise ValueError("Invalid test plan package")
    for field in ("preconditions", "assumptions"):
        items = plan.get(field, [])
        if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
            raise ValueError(f"{field} must be a list of strings")
    steps = plan.get("steps")
    if not isinstance(steps, list) or len(steps) > 100:
        raise ValueError("Provide an array of at most 100 steps")
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError("Invalid test step")
        action = step.get("action")
        if action is not None:
            validate_action(action)
        elif not isinstance(step.get("instruction"), str) or not step["instruction"].strip():
            raise ValueError("Each step needs an action or an instruction")
        assertions = step.get("assertions")
        if not isinstance(assertions, list) or not assertions or len(assertions) > 20:
            raise ValueError("Each step needs 1–20 explicit assertions")
        for item in assertions:
            validate_assertion(item)
    return plan


def metrics(run):
    results = run["results"]
    return {
        "total": len(results),
        **{status.lower(): sum(row["status"] == status for row in results)
           for status in ("PASSED", "FAILED", "BLOCKED", "INCONCLUSIVE", "NOT_EXECUTED")},
        "assertions_passed": sum(value is True for row in results for value in row["assertions"]),
        "assertions_checked": sum(len(row["assertions"]) for row in results),
        "unique_screens": len(set(run["screens"])),
        "actions": run["actions"], "retries": len(run["improvements"]),
        "recoveries_verified": sum(item.get("verified") is True for item in run["improvements"]),
        "duration_seconds": round((run.get("finished") or time.time()) - run["started"], 2),
    }


class Stopped(Exception):
    pass


class Run:
    def __init__(self, android, model, store, serial, package, plan, limits=None):
        self.android, self.model, self.store = android, model, store
        self.plan = validate_plan(plan)
        if plan.get("package", package) != package:
            raise ValueError("Select the application this test plan was recorded for")
        self.limits = {**LIMITS, **(limits or {})}
        self.condition = threading.Condition()
        self.paused = self.stopped = False
        execution_mode = "model" if any(step.get("action") is None for step in plan["steps"]) else "plan"
        self.data = dict(schema_version=1, id=uuid.uuid4().hex, state="IDLE", plan=plan,
                         serial=serial, package=package, model=model.get_capabilities() if execution_mode == "model" else {},
                         execution_mode=execution_mode,
                         events=[], results=[dict(step=i, status="NOT_EXECUTED", assertions=[])
                                             for i in range(len(plan["steps"]))],
                         improvements=[], actions=0, screens=[], started=time.time())

    def persist(self):
        self.store.write("run", self.data["id"], self.data)

    def transition(self, state, **details):
        previous = self.data["state"]
        if previous in TERMINAL:
            raise RuntimeError("Run already ended")
        if (state not in TERMINAL | {"PAUSED"} and previous != "PAUSED"
                and state not in TRANSITIONS.get(previous, set())):
            raise RuntimeError(f"Invalid state transition {previous} → {state}")
        self.data["state"] = state
        self.data["events"].append(dict(time=time.time(), state=state, **details))
        self.persist()

    def control(self, command):
        if command not in ("pause", "resume", "stop", "emergency"):
            raise ValueError("Invalid run control")
        with self.condition:
            if command in ("stop", "emergency"):
                self.stopped = True
            elif not self.stopped:
                self.paused = command == "pause"
            self.condition.notify_all()

    def checkpoint(self):
        with self.condition:
            previous = self.data["state"]
            if self.paused and not self.stopped:
                self.transition("PAUSED")
                while self.paused and not self.stopped:
                    self.condition.wait(0.25)
                    if time.time() - self.data["started"] >= self.limits["seconds"]:
                        break
                if not self.stopped:
                    self.transition(previous)
            if self.stopped:
                raise Stopped()
        if time.time() - self.data["started"] >= self.limits["seconds"]:
            raise ValueError("Run time limit reached")

    def observe(self):
        observation = self.android.observe(self.data["serial"])
        self.data["screens"].append(observation["fingerprint"])
        return observation

    def prepare_plan(self, serial, package):
        pass

    def run(self):
        final = "FAILED"
        try:
            self.transition("CONNECTING")
            self.checkpoint()
            serial, package = self.data["serial"], self.data["package"]
            if not any(d["serial"] == serial and d["status"] == "device" for d in self.android.devices()):
                raise ValueError("Selected device is disconnected or unauthorized")
            self.data["device"] = self.android.info(serial)
            try:
                self.data["app_version"] = self.android.app_version(serial, package)
            except ValueError:
                self.data["app_version"] = ""
            self.prepare_plan(serial, package)
            for index, step in enumerate(self.plan["steps"]):
                if not self.run_step(index, step, package, serial):
                    break
            else:
                if self.plan["steps"]:
                    final = "COMPLETED"
        except Stopped:
            final = "STOPPED"
        except (ValueError, OSError) as exc:
            row = next((row for row in self.data["results"] if row["status"] == "NOT_EXECUTED"), None)
            if row is not None:
                row.update(status="BLOCKED", error=str(exc))
            self.data["error"] = str(exc)
        finally:
            self.data["finished"] = time.time()
            self.data["metrics"] = metrics(self.data)
            self.transition(final)
            if self.data["execution_mode"] == "model":
                self.add_model_report()

    def add_model_report(self):
        try:
            report = self.model.generate_report({"objective": self.plan["objective"],
                                                "metrics": self.data["metrics"]})
            analysis, recommendations = report.get("analysis"), report.get("recommendations")
            if isinstance(analysis, str) and isinstance(recommendations, list):
                self.data["analysis"] = analysis[:4000]
                self.data["recommendations"] = [item[:500] for item in recommendations[:10]
                                                if isinstance(item, str)]
        except ValueError:
            self.data["analysis"] = "Model report analysis unavailable."
        self.persist()

    def save_step_evidence(self, result, screen, package, timing):
        if self.data.get("report_screenshots", True) and screen["package"] == package and screen.get("image"):
            key = f"{self.data['id']}-{result['step']}"
            self.store.write("evidence", key, screen["image"])
            result.update(evidence=key, evidence_timing=timing)

    def run_step(self, index, step, package, serial):
        result = self.data["results"][index]
        step_action = step.get("action")
        action = step_action
        repeated = set()
        for retry in range(self.limits["retries"] + 1):
            self.checkpoint()
            self.transition("OBSERVING", step=index, retry=retry)
            before = self.observe()
            self.transition("PLANNING", step=index)
            if action is None:
                action = self.model.suggest_action(dict(instruction=step["instruction"],
                                                       screen=public_screen(before),
                                                       history=self.data["events"][-10:]))
                validate_action(action)
                if action["action"] not in ("tap", "long_press", "scroll", "wait_for_element",
                                           "inspect_screen", "capture_screenshot") or "point" in action:
                    raise ValueError("Model-planned steps require a selector or a read-only action")
            signature = (before["fingerprint"], repr(action))
            if signature in repeated or len(repeated) >= self.limits["repeated"]:
                result.update(status="BLOCKED", error="Repeated screen and action produced no progress")
                return False
            repeated.add(signature)
            if self.data["actions"] >= self.limits["actions"]:
                raise ValueError("Run action limit reached")
            self.checkpoint()
            self.data["actions"] += 1
            self.transition("EXECUTING", step=index, action=action["action"])
            try:
                self.android.execute(serial, package, action, before)
            except ValueError as exc:
                result.update(status="BLOCKED", error=str(exc))
                alternative = None if step_action is not None else self.recovery(action, before, retry, str(exc))
                if alternative is None:
                    self.save_step_evidence(result, before, package, "before")
                    return False
                action, improvement = alternative
                result.update(status="NOT_EXECUTED")
                continue
            self.checkpoint()
            self.transition("VERIFYING", step=index)
            after = self.observe()
            values = [evaluate(assertion, before, after, package) for assertion in step["assertions"]]
            status = "FAILED" if False in values else "INCONCLUSIVE" if None in values else "PASSED"
            result.update(status=status, assertions=values, fingerprint=after["fingerprint"], error="")
            self.save_step_evidence(result, after, package, "after")
            if retry:
                improvement["verified"] = status == "PASSED"
                if improvement["verified"]:
                    self.store.write("strategy", improvement["key"], improvement["target"])
            self.persist()
            return status == "PASSED"
        return False

    def recovery(self, action, screen, retry, reason):
        target = action.get("target")
        if (action["action"] not in ("tap", "wait_for_element") or not target
                or retry >= self.limits["retries"] or len(self.data["improvements"]) >= self.limits["recoveries"]
                or "Selector must match" not in reason):
            return None
        self.transition("RECOVERING", reason=reason)
        key = "|".join((self.data["package"], target["strategy"], target["value"]))
        candidate = self.store.read("strategy", key)
        if candidate is None:
            response = self.model.analyze_failure({"target": target, "screen": public_screen(screen),
                                                   "reason": reason})
            candidate = response.get("target")
        if not isinstance(candidate, dict) or candidate == target or candidate.get("strategy") not in SELECTORS:
            return None
        alternative = {**action, "target": candidate}
        validate_action(alternative)
        matches = [node for node in screen["nodes"] if node["package"] == self.data["package"]
                   and node[candidate["strategy"]] == candidate["value"]]
        if len(matches) != 1 or target["value"] not in [matches[0][field] for field in SELECTORS]:
            return None
        improvement = dict(key=key, target=candidate, original=target, verified=False)
        self.data["improvements"].append(improvement)
        return alternative, improvement
