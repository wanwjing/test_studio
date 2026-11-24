from android import validate_target


TYPES = ("element_visible", "element_not_visible", "text_equals", "element_enabled",
         "element_disabled", "screen_transition", "application_running", "application_not_foreground",
         "expected_ui_state")


def validate_assertion(assertion):
    if not isinstance(assertion, dict) or assertion.get("type") not in TYPES:
        raise ValueError("Unsupported test assertion")
    if assertion["type"] not in ("screen_transition", "application_running", "application_not_foreground"):
        validate_target(assertion.get("target"))
    if assertion["type"] == "text_equals" and not isinstance(assertion.get("value"), str):
        raise ValueError("Text equality requires a string value")


def evaluate(assertion, before, after, package):
    validate_assertion(assertion)
    kind = assertion["type"]
    if kind == "application_running":
        return after["package"] == package
    if kind == "application_not_foreground":
        return after["package"] != package if after["package"] else None
    if kind == "screen_transition":
        return before["fingerprint"] != after["fingerprint"]
    if after["package"] != package:
        return None
    target = assertion["target"]
    matches = []
    for node in after["nodes"]:
        if node["package"] != package or node[target["strategy"]] != target["value"]:
            continue
        if len(node["bounds"]) == 4:
            x1, y1, x2, y2 = node["bounds"]
            if max(0, x1) < min(after["width"], x2) and max(0, y1) < min(after["height"], y2):
                matches.append(node)
    if kind in ("element_visible", "expected_ui_state"):
        return bool(matches)
    if kind == "element_not_visible":
        return not matches
    if len(matches) != 1 or matches[0]["password"]:
        return None if matches else False
    if kind == "text_equals":
        return matches[0]["text"] == assertion["value"]
    return matches[0]["enabled"] == (kind == "element_enabled")
