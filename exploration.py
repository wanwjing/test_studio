import uuid

from agent import Run, validate_plan
from android import SELECTORS, validate_action
from verification import evaluate


class Exploration(Run):
    def __init__(self, android, model, store, serial, package, objective, limits):
        plan = {
            "objective": objective,
            "package": package,
            "preconditions": ["Use the same emulator and fixture data as the recording."],
            "assumptions": ["Assertions describe observed behavior and need review."],
            "steps": [],
        }
        super().__init__(android, model, store, serial, package, plan, limits)
        self.data["execution_mode"] = "exploration"
        self.data["model"] = model.get_capabilities()
        self.data["exploration"] = True

    def prepare_plan(self, serial, package):
        self.transition("EXPLORING", phase="Launching selected APK")
        launch = {"schema_version": 1, "action": "restart_app"}
        self.data["actions"] += 1
        self.android.execute(serial, package, launch)
        screen = self.observe()
        self.require_selected_app(screen, package)
        steps = [{"action": launch, "assertions": [{"type": "application_running"}]}]
        history = []
        visited = set()
        reason = "Exploration action limit reached"
        # Reserve replay capacity for the launch and each recorded action.
        for _ in range(min(12, (self.limits["actions"] - 2) // 2)):
            self.checkpoint()
            try:
                response = self.model.explore_screen(screen, self.plan["objective"], history)
                proposal = response.get("action", {})
                if proposal is None:
                    reason = "Model finished exploring"
                    break
                action = self.prepare_action(proposal, screen)
                signature = (screen["fingerprint"], repr(action))
                if signature in visited:
                    reason = "The model repeated an action on the same screen"
                    break
                visited.add(signature)
                self.checkpoint()
                fresh = self.observe()
                self.require_selected_app(fresh, package)
                if fresh["fingerprint"] != screen["fingerprint"]:
                    raise ValueError("The screen changed while the model chose an action")
                if "point" in action and fresh["image_fingerprint"] != action["fingerprint"]:
                    raise ValueError("The screenshot changed while the model chose a coordinate")
                self.checkpoint()
                self.transition("EXPLORING", action=action["action"], step=len(steps))
                self.data["actions"] += 1
                self.android.execute(serial, package, action, fresh)
                after = self.observe()
                self.require_selected_app(after, package)
            except (ValueError, OSError) as exc:
                reason = str(exc)
                break
            steps.append({"action": action, "assertions": observed_assertions(fresh, after, package)})
            history.append({"screen": fresh["fingerprint"], "action": action,
                            "result": after["fingerprint"]})
            self.transition("EXPLORING", step=len(steps) - 1, observed=history[-1])
            screen = after

        if len(steps) == 1:
            raise ValueError(f"No in-app actions recorded. {reason}")
        # Save the observed path even if its subsequent replay fails.
        self.plan["steps"] = steps
        validate_plan(self.plan)
        plan_id = uuid.uuid4().hex
        self.store.write("plan", plan_id, {"id": plan_id, "plan": self.plan})
        self.data["generated_plan_id"] = plan_id
        self.data["results"] = [
            {"step": index, "status": "NOT_EXECUTED", "assertions": []}
            for index in range(len(steps))
        ]
        self.transition("EXPLORING", phase="Saved test case. Starting replay", reason=reason)

    @staticmethod
    def require_selected_app(screen, package):
        if screen["package"] != package:
            raise ValueError("The selected app left the foreground")

    def prepare_action(self, proposal, screen):
        if not isinstance(proposal, dict) or proposal.get("action") not in ("tap", "scroll", "press_back"):
            raise ValueError("Exploration accepts tap, scroll, and Back actions")
        fields = ("schema_version", "action", "target", "point", "direction")
        action = {field: proposal[field] for field in fields if field in proposal}
        if "point" in action:
            if not self.model.get_capabilities()["vision"] or not screen.get("image"):
                raise ValueError("Coordinate exploration requires a screenshot and vision")
            action["fingerprint"] = screen["image_fingerprint"]
        return validate_action(action)


def observed_assertions(before, after, package):
    assertions = [{"type": "application_running"}]
    if before["fingerprint"] != after["fingerprint"]:
        assertions.append({"type": "screen_transition"})
    for strategy in SELECTORS:
        for node in after["nodes"]:
            value = node[strategy]
            if node["package"] != package or node["password"] or not 0 < len(value) <= 512:
                continue
            assertion = {"type": "element_visible", "target": {"strategy": strategy, "value": value}}
            if evaluate(assertion, after, after, package) and not evaluate(assertion, before, before, package):
                return [*assertions, assertion]
    return assertions
