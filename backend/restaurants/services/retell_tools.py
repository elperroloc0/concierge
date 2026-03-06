"""
Retell LLM tool definitions — shared between admin actions and the portal view.
"""


def _sms_tool_definition(base_url: str) -> dict:
    return {
        "type": "custom",
        "name": "send_sms",
        "description": (
            "Send a text message to the caller with a link or useful info they requested. "
            "Only call this tool AFTER the caller explicitly says yes to receiving a text."
        ),
        "url": f"{base_url}/api/retell/tools/send-sms/",
        "speak_during_execution": True,
        "execution_message_description": "Perfect — I'm sending that to your number right now.",
        "execution_message_type": "static_text",
        "parameters": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": (
                        "The complete SMS to send. Keep under 160 characters. "
                        "Include the relevant link and a warm closing."
                    ),
                }
            },
            "required": ["message"],
        },
    }


def _save_caller_info_tool_definition(base_url: str) -> dict:
    return {
        "type": "custom",
        "name": "save_caller_info",
        "description": (
            "Save the caller's name as soon as you learn it. "
            "Call once silently — do NOT announce it or pause the conversation. "
            "Also set follow_up_needed=true if the caller explicitly asks to be called back "
            "or requests to speak with a human and could not be transferred."
        ),
        "url": f"{base_url}/api/retell/tools/save-caller-info/",
        "speak_during_execution": False,
        "parameters": {
            "type": "object",
            "properties": {
                "caller_name":      {"type": "string", "description": "Full name as introduced."},
                "caller_email":     {"type": "string", "description": "Email if provided. Omit otherwise."},
                "follow_up_needed": {
                    "type": "boolean",
                    "description": (
                        "Set to true ONLY if the caller explicitly asked to be called back "
                        "or requested a human and was not transferred. Default: false."
                    ),
                },
            },
            "required": ["caller_name"],
        },
    }


def _resolve_date_tool_definition(base_url: str) -> dict:
    return {
        "type": "custom",
        "name": "resolve_date",
        "description": (
            "Convert a relative or ambiguous date phrase into an actual calendar date. "
            "Call this as soon as the caller gives a date for a reservation. "
            "Use the spoken_es or spoken_en field from the response when confirming with the caller. "
            "If is_past=true, tell the caller the date has passed. "
            "If ambiguity is set, ask the caller to clarify."
        ),
        "url": f"{base_url}/api/retell/tools/resolve-date/",
        "speak_during_execution": False,
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": (
                        "The date phrase as the caller said it — e.g. 'this Friday', "
                        "'tomorrow', 'el sábado', 'March 15', 'next Monday'."
                    ),
                }
            },
            "required": ["text"],
        },
    }


def _get_info_tool_definition(base_url: str) -> dict:
    return {
        "type": "custom",
        "name": "get_info",
        "description": (
            "Look up specific restaurant information from the knowledge base. "
            "Call this BEFORE answering any factual question about the restaurant. "
            "Do not guess or recall from memory — always retrieve the data first."
        ),
        "url": f"{base_url}/api/retell/tools/get-info/",
        "speak_during_execution": False,
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "enum": [
                        "hours", "menu", "bar_menu", "happy_hour", "dietary",
                        "parking", "billing", "reservations", "private_events",
                        "ambience", "facilities", "special_events", "additional",
                    ],
                    "description": "The topic to look up.",
                }
            },
            "required": ["topic"],
        },
    }


def _escalation_tool_definition(transfer_number: str) -> dict:
    return {
        "type": "transfer_call",
        "name": "transfer_to_human",
        "description": (
            "Transfer the caller to a human agent when escalation conditions are met. "
            "Only call this after acknowledging the caller. Never call for routine questions."
        ),
        "transfer_destination": {
            "type": "number",
            "number": transfer_number,
            "description": "Human agent / restaurant manager",
        },
    }


def _end_call_tool_definition() -> dict:
    return {
        "type": "end_call",
        "name": "end_call",
        "description": (
            "End the call cleanly. "
            "Call this ONLY after a proper goodbye has been spoken. "
            "Never call mid-conversation."
        ),
    }


def build_tool_list(base_url: str, escalation_number: str | None = None) -> list:
    """
    Build the full Retell general_tools list.
    end_call is always included.
    transfer_to_human is included only when escalation_number is provided.
    """
    tools = [
        _sms_tool_definition(base_url),
        _save_caller_info_tool_definition(base_url),
        _get_info_tool_definition(base_url),
        _resolve_date_tool_definition(base_url),
        _end_call_tool_definition(),
    ]
    if escalation_number:
        tools.append(_escalation_tool_definition(escalation_number))
    return tools
