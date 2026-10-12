"""Tool declarations exposed to the model. Generic only: no task-specific shortcuts."""

S = {"type": "string"}


def _obj(props: dict, required: list[str]):
    return {"type": "object", "properties": props, "required": required}


TOOLS = [
    {"name": "set_plan", "description": "Record a short plan and the checkable success criteria for the task. "
     "Call once at the start (you may combine it with your first action).",
     "parameters": _obj({"steps": {"type": "array", "items": S},
                         "success_criteria": {"type": "array", "items": S}}, ["steps", "success_criteria"])},
    {"name": "open_page", "description": "Navigate to a path inside the company application, e.g. /vendors?q=V-101.",
     "parameters": _obj({"path": S}, ["path"])},
    {"name": "observe", "description": "Re-read the current page. Use page=2,3… to see more table rows/elements.",
     "parameters": _obj({"page": {"type": "integer"}}, [])},
    {"name": "click", "description": "Click a link or button by its ref from the LATEST observation (e.g. '7.12'). "
     "Clicking a form's submit button sends the form.",
     "parameters": _obj({"ref": S}, ["ref"])},
    {"name": "fill", "description": "Fill several inputs/selects of the current page without submitting. "
     "For selects give the option value or its visible text.",
     "parameters": _obj({"fields": {"type": "array", "items": _obj({"ref": S, "value": S}, ["ref", "value"])}},
                        ["fields"])},
    {"name": "sign_in", "description": "Sign in to the application with the company credential provider "
     "(use when the page says you are not signed in).", "parameters": _obj({}, [])},
    {"name": "list_files", "description": "List documents in the company's inbox folder.",
     "parameters": _obj({}, [])},
    {"name": "read_document", "description": "Read the text of a document from the inbox folder.",
     "parameters": _obj({"name": S}, ["name"])},
    {"name": "note", "description": "Remember a fact you verified, with its source (kept in your working memory).",
     "parameters": _obj({"case_id": S, "key": S, "value": S, "source": S}, ["key", "value", "source"])},
    {"name": "propose_decision", "description": "Propose the business action for ONE case before changing anything. "
     "Returns AUTHORIZED write instructions, a DENIAL, or a human decision. Never submit a form that saves "
     "data without an authorization.",
     "parameters": _obj({"case_id": S, "action": S,
                         "values": {"type": "array", "items": _obj({"name": S, "value": S}, ["name", "value"])},
                         "evidence": {"type": "array", "items": S}, "rationale": S},
                        ["case_id", "action", "values", "rationale"])},
    {"name": "ask_human", "description": "Ask the human operator a specific question when information is missing "
     "or ambiguous and no documented action covers it. Execution waits for the answer.",
     "parameters": _obj({"question": S, "case_id": S}, ["question"])},
    {"name": "request_completion", "description": "When every case in scope is handled, request independent "
     "verification of the whole task. Returns verified results or what is still outstanding.",
     "parameters": _obj({"summary": S}, ["summary"])},
]

TOOL_NAMES = {t["name"] for t in TOOLS}
# After these tools the page or the situation changes, so later calls in the same turn are not executed.
BARRIER_TOOLS = {"open_page", "click", "observe", "sign_in", "propose_decision", "ask_human", "request_completion"}
