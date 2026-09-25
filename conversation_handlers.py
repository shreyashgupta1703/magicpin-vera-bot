from dataclasses import dataclass, field
from typing import Any

@dataclass
class ConversationState:
    conversation_id: str
    merchant_id: str | None = None
    customer_id: str | None = None
    turns: list[dict[str, Any]] = field(default_factory=list)

def detect_intent(message: str) -> str:
    m = message.lower().strip()
    if any(x in m for x in ["stop", "unsubscribe", "not interested", "leave me alone", "spam"]):
        return "stop"
    if any(x in m for x in ["yes", "okay", "ok", "let's do it", "lets do it", "go ahead", "proceed", "sign me up", "what's next", "whats next"]):
        return "commit"
    if "?" in m or any(x in m for x in ["how", "what", "why", "when", "where"]):
        return "question"
    return "general"

def respond(state: ConversationState, merchant_message: str) -> dict:
    """Optional standalone multi-turn handler matching the challenge's intent."""
    state.turns.append({"from": "merchant", "message": merchant_message})
    intent = detect_intent(merchant_message)

    if intent == "stop":
        return {"action": "end", "rationale": "Explicit opt-out/not-interested signal."}

    if intent == "commit":
        return {
            "action": "send",
            "body": "Done — switching to action mode. I’ll take the next step from here and only ask for anything required to complete it.",
            "cta": "open_ended",
            "rationale": "Commitment detected; no further qualification."
        }

    if intent == "question":
        return {
            "action": "send",
            "body": "Yes — I can help. Send me the specific detail you want to clarify and I’ll keep the next step focused.",
            "cta": "open_ended",
            "rationale": "Focused question handling."
        }

    return {
        "action": "send",
        "body": "Got it. I can keep this simple and prepare the concrete next step rather than asking more qualifying questions.",
        "cta": "open_ended",
        "rationale": "Low-friction continuation."
    }
