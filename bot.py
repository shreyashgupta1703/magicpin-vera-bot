import os, re, time, json, hashlib
from datetime import datetime
from typing import Any, Optional
from collections import defaultdict, deque

from fastapi import FastAPI
from pydantic import BaseModel, Field

APP_VERSION = "1.0.0"
START = time.time()
COMPOSER_VERSION = "composer_v1"

app = FastAPI(title="magicpin Vera Merchant AI", version=APP_VERSION)

# ---------------------------------------------------------------------------
# Stateful stores
# ---------------------------------------------------------------------------
contexts: dict[tuple[str, str], dict[str, Any]] = {}
conversations: dict[str, list[dict[str, Any]]] = defaultdict(list)
sent_by_conversation: dict[str, set[str]] = defaultdict(set)
suppression_seen: set[str] = set()

# ---------------------------------------------------------------------------
# API models
# ---------------------------------------------------------------------------
class ContextBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str

class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = Field(default_factory=list)

class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: str
    turn_number: int

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
VALID_SCOPES = {"category", "merchant", "customer", "trigger"}

def get_ctx(scope: str, cid: Optional[str]) -> Optional[dict]:
    if not cid:
        return None
    item = contexts.get((scope, cid))
    return item["payload"] if item else None

def category_for_merchant(merchant: dict) -> Optional[dict]:
    return get_ctx("category", merchant.get("category_slug"))

def norm(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()

def owner_name(merchant: dict) -> str:
    ident = merchant.get("identity", {})
    return ident.get("owner_first_name") or ident.get("name") or "there"

def merchant_name(merchant: dict) -> str:
    return merchant.get("identity", {}).get("name") or "your business"

def languages(merchant: dict) -> list[str]:
    return merchant.get("identity", {}).get("languages", []) or []

def active_offers(merchant: dict) -> list[dict]:
    return [o for o in merchant.get("offers", []) if o.get("status") == "active"]

def choose_offer(category: dict, merchant: dict) -> Optional[str]:
    offers = active_offers(merchant)
    if offers:
        return offers[0].get("title")
    catalog = category.get("offer_catalog", [])
    if catalog:
        first = catalog[0]
        return first.get("title") if isinstance(first, dict) else str(first)
    return None

def safe_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))

def hash_message(body: str) -> str:
    return hashlib.sha256(body.strip().lower().encode()).hexdigest()

def already_sent(conv_id: str, body: str) -> bool:
    return hash_message(body) in sent_by_conversation[conv_id]

def record_send(conv_id: str, body: str):
    sent_by_conversation[conv_id].add(hash_message(body))

def is_auto_reply(message: str, history: list[dict]) -> bool:
    msg = norm(message).lower()
    if not msg:
        return False
    same = sum(
        1 for t in history
        if t.get("from") in {"merchant", "customer"} and norm(t.get("msg")).lower() == msg
    )
    canned = any(x in msg for x in [
        "thank you for contacting", "thanks for contacting",
        "our team will respond", "we will get back to you",
        "thank you for reaching", "currently unavailable"
    ])
    return same >= 2 or (canned and same >= 1)

def detect_intent(message: str) -> str:
    m = norm(message).lower()
    if any(x in m for x in [
        "stop", "unsubscribe", "don't message", "do not message",
        "not interested", "no thanks", "leave me alone", "spam", "useless"
    ]):
        return "stop"
    if any(x in m for x in [
        "yes", "okay", "ok", "lets do it", "let's do it", "go ahead",
        "proceed", "sign me up", "i want to join", "do it", "whats next", "what's next"
    ]):
        return "commit"
    if any(x in m for x in ["how", "what", "why", "when", "where", "?"]):
        return "question"
    if len(m) < 8:
        return "unclear"
    return "general"

def language_style(merchant: dict, message: str = "") -> str:
    m = message.lower()
    if any(x in m for x in ["hai", "haan", "nahi", "karna", "chahiye", "batao", "bhejo", "aap"]):
        return "hi-en"
    langs = [str(x).lower() for x in languages(merchant)]
    if "hi" in langs and "en" in langs:
        return "hi-en"
    return "en"

def cta_for(trigger: dict, body: str) -> str:
    kind = trigger.get("kind", "")
    if kind in {"perf_spike", "perf_dip", "milestone_reached", "recall_due",
                "customer_lapsed_soft", "customer_lapsed_hard",
                "appointment_tomorrow", "unplanned_slot_open", "festival",
                "competitor_opened", "curious_ask_due"}:
        return "YES/STOP"
    return "open_ended"

def trigger_fact(trigger: dict, category: dict) -> str:
    p = trigger.get("payload", {}) or {}
    kind = trigger.get("kind", "")
    if kind in {"research_digest", "research_digest_release", "category_research_digest_release"}:
        item = p.get("top_item") or {}
        title = item.get("title") or p.get("title")
        source = item.get("source") or p.get("source")
        if title:
            return f"{title}" + (f" — {source}" if source else "")
    if kind in {"perf_spike", "perf_dip"}:
        perf = p.get("performance") or p
        for key in ["views_pct", "calls_pct", "leads_pct", "ctr_pct"]:
            if key in perf:
                return f"{key.replace('_pct','').replace('_',' ')} changed {perf[key]}"
    if kind == "milestone_reached":
        return norm(p.get("milestone") or p.get("value") or "a milestone")
    if kind in {"festival", "festival_upcoming"}:
        return norm(p.get("name") or p.get("event") or "an upcoming festival")
    if kind == "competitor_opened":
        return norm(p.get("distance") or p.get("competitor") or "a nearby competitor")
    if kind == "review_theme_emerged":
        return norm(p.get("theme") or p.get("summary") or "a review pattern")
    if kind in {"recall_due", "customer_lapsed_soft", "customer_lapsed_hard"}:
        return norm(p.get("due_date") or p.get("last_visit") or "a customer recall window")
    return norm(p.get("headline") or p.get("title") or p.get("reason") or kind)

def compose(category: dict, merchant: dict, trigger: dict,
            customer: Optional[dict] = None,
            prior_message: str = "",
            conversation: Optional[list[dict]] = None) -> dict:
    """Deterministic rule-based composer. Optional LLM can be layered later."""
    kind = trigger.get("kind", "")
    ident = merchant.get("identity", {})
    name = ident.get("owner_first_name") or ident.get("name") or "there"
    business = ident.get("name") or "your business"
    locality = ident.get("locality")
    perf = merchant.get("performance", {}) or {}
    signals = merchant.get("signals", []) or []
    offer = choose_offer(category, merchant)
    voice = (category.get("voice", {}) or {}).get("tone", "")

    # Customer-facing path
    if customer is not None or trigger.get("scope") == "customer":
        cident = customer.get("identity", {}) if customer else {}
        cname = cident.get("name") or "there"
        consent = customer.get("consent", {}) if customer else {}
        if consent and not consent.get("scope"):
            return {
                "body": "I can't send this outreach without the customer's consent scope.",
                "cta": "none", "send_as": "merchant_on_behalf",
                "suppression_key": trigger.get("suppression_key", ""),
                "rationale": "Consent scope is missing; suppress customer outreach."
            }
        if kind == "recall_due":
            last = (customer.get("relationship", {}) or {}).get("last_visit")
            body = f"Hi {cname}, {business} here 🦷. Your next visit is due"
            if last:
                body += f" based on your last visit on {last}"
            if offer:
                body += f". {offer}."
            body += " Reply YES if you'd like us to help with a suitable slot, or STOP to opt out."
        elif kind in {"customer_lapsed_soft", "customer_lapsed_hard"}:
            body = f"Hi {cname}, {business} here. We noticed it has been a while since your last visit."
            if offer:
                body += f" We currently have {offer}."
            body += " Reply YES if you'd like details, or STOP to opt out."
        elif kind == "appointment_tomorrow":
            body = f"Hi {cname}, {business} here. Your appointment is tomorrow."
            body += " Reply YES to confirm, or STOP to opt out."
        else:
            body = f"Hi {cname}, {business} here. {trigger_fact(trigger, category)}."
            body += " Reply YES if you'd like details, or STOP to opt out."
        return {
            "body": body, "cta": "YES/STOP", "send_as": "merchant_on_behalf",
            "suppression_key": trigger.get("suppression_key", ""),
            "rationale": f"Customer-facing {kind} message uses customer relationship, merchant offer, trigger fact and consent."
        }

    # Merchant-facing path
    prefix = f"Dr. {name}" if "dentist" in category.get("slug","") and name and not str(name).lower().startswith("dr.") else str(name)
    if kind in {"research_digest", "research_digest_release", "category_research_digest_release"}:
        fact = trigger_fact(trigger, category)
        body = f"{prefix}, {fact}."
        if "high_risk" in safe_json(merchant).lower() or "high-risk" in safe_json(merchant).lower():
            body += " It looks relevant to your high-risk patient cohort."
        body += " Want me to pull the key points and turn them into a patient-ready WhatsApp?"
        rationale = "Research hook + merchant-specific cohort + effort externalization."
    elif kind == "perf_spike":
        pct = (trigger.get("payload") or {}).get("pct") or (trigger.get("payload") or {}).get("change_pct")
        body = f"{prefix}, your performance moved up"
        if pct is not None:
            body += f" {pct}%"
        body += " versus the comparison period."
        body += " Want me to break down what likely drove it?"
        rationale = "Performance spike anchored to a concrete change and low-friction curiosity CTA."
    elif kind == "perf_dip":
        pct = (trigger.get("payload") or {}).get("pct") or (trigger.get("payload") or {}).get("change_pct")
        body = f"{prefix}, I spotted a performance dip"
        if pct is not None:
            body += f" of {pct}%"
        body += ". I can isolate the biggest drop and suggest one fix."
        rationale = "Specific performance issue + effort externalization."
    elif kind == "milestone_reached":
        fact = trigger_fact(trigger, category)
        body = f"{prefix}, you just hit {fact}. Nice milestone."
        body += " Want me to suggest the next profile move to capitalize on it?"
        rationale = "Milestone recognition + next-best action."
    elif kind == "review_theme_emerged":
        theme = trigger_fact(trigger, category)
        body = f"{prefix}, a review pattern is emerging around {theme}."
        body += " Want the exact pattern and a draft response you can use?"
        rationale = "Review-theme specificity + drafted action."
    elif kind in {"festival", "festival_upcoming"}:
        fact = trigger_fact(trigger, category)
        body = f"{prefix}, {fact} is coming up."
        if offer:
            body += f" Your active offer is {offer}."
        body += " Want me to draft a service-first message for it?"
        rationale = "Timely external trigger + actual active offer + service-first copy."
    elif kind == "competitor_opened":
        fact = trigger_fact(trigger, category)
        body = f"{prefix}, I spotted {fact}."
        body += " Want me to show what changed locally and where your profile can differentiate?"
        rationale = "Local competitive curiosity without inventing competitor details."
    elif kind == "curious_ask_due":
        body = f"{prefix}, quick operator question: what service are customers asking you for most this week?"
        body += " Reply with the service name and I'll turn it into one useful growth idea."
        rationale = "Merchant-as-expert curiosity loop; explicitly asks the merchant."
    else:
        fact = trigger_fact(trigger, category)
        body = f"{prefix}, {fact}."
        body += " Want me to turn this into one concrete next step?"
        rationale = "Generic fallback still anchored to the trigger payload."

    # Language adaptation for mixed Hindi/English
    if language_style(merchant) == "hi-en" and kind in {"curious_ask_due", "perf_spike", "perf_dip"}:
        body = body.replace("quick operator question:", "ek quick operator question:")
        body = body.replace("Want me to", "Batao, kya main")
        body = body.replace("Reply with", "Bas")

    # Anti-repetition guard
    conv = conversation or []
    if any(norm(t.get("body")) == norm(body) for t in conv if t.get("from") == "vera"):
        body = body.rstrip(".") + " — I can make this more specific if useful."

    return {
        "body": body,
        "cta": cta_for(trigger, body),
        "send_as": "vera",
        "suppression_key": trigger.get("suppression_key", ""),
        "rationale": rationale
    }

def response_for_reply(body: ReplyBody) -> dict:
    hist = conversations[body.conversation_id]
    msg = norm(body.message)
    auto = is_auto_reply(msg, hist)
    intent = detect_intent(msg)

    hist.append({"from": body.from_role, "msg": msg, "turn": body.turn_number})

    if auto:
        return {"action": "end", "rationale": "Repeated/canned WhatsApp Business auto-reply detected; stopping instead of burning turns."}

    if intent == "stop":
        return {"action": "end", "rationale": "Merchant explicitly requested no further messaging."}

    if intent == "commit":
        return {
            "action": "send",
            "body": "Done — switching to action mode. I’ll take the next step from here and only ask you for anything I actually need to complete it.",
            "cta": "open_ended",
            "rationale": "Detected commitment and moved directly from qualification to action."
        }

    if intent == "question":
        return {
            "action": "send",
            "body": "Yes — I can help with that. Tell me the one detail you want to clarify, and I’ll keep the next step focused.",
            "cta": "open_ended",
            "rationale": "Answered/advanced the question without restarting qualification."
        }

    # Avoid endless follow-ups after several unanswered/low-signal turns.
    merchant_turns = sum(1 for t in hist if t.get("from") == body.from_role)
    if merchant_turns >= 3:
        return {"action": "wait", "wait_seconds": 1800, "rationale": "Backing off after multiple low-signal turns."}

    return {
        "action": "send",
        "body": "Got it. I can keep this simple — if you want, I’ll prepare the concrete next step rather than asking you more qualifying questions.",
        "cta": "open_ended",
        "rationale": "Low-friction continuation while avoiding repetitive qualification."
    }

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/v1/healthz")
async def healthz():
    counts = {s: 0 for s in VALID_SCOPES}
    for (scope, _), _v in contexts.items():
        if scope in counts:
            counts[scope] += 1
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START),
        "contexts_loaded": counts
    }

@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": os.getenv("TEAM_NAME", "Vera Builder"),
        "team_members": [x for x in os.getenv("TEAM_MEMBERS", "Shreyash").split(",") if x],
        "model": os.getenv("MODEL_NAME", "deterministic-composer-v1"),
        "approach": "4-context deterministic composer + stateful conversation routing",
        "contact_email": os.getenv("CONTACT_EMAIL", ""),
        "version": APP_VERSION,
        "submitted_at": datetime.utcnow().isoformat() + "Z"
    }

@app.post("/v1/context")
async def push_context(body: ContextBody):
    if body.scope not in VALID_SCOPES:
        return {"accepted": False, "reason": "invalid_scope", "details": body.scope}
    key = (body.scope, body.context_id)
    current = contexts.get(key)
    if current and current["version"] >= body.version:
        return {"accepted": False, "reason": "stale_version", "current_version": current["version"]}
    contexts[key] = {"version": body.version, "payload": body.payload}
    return {
        "accepted": True,
        "ack_id": f"ack_{body.scope}_{body.context_id}_v{body.version}",
        "stored_at": datetime.utcnow().isoformat() + "Z"
    }

@app.post("/v1/tick")
async def tick(body: TickBody):
    actions = []
    for trigger_id in body.available_triggers:
        trigger = get_ctx("trigger", trigger_id)
        if not trigger:
            continue
        merchant_id = trigger.get("merchant_id")
        customer_id = trigger.get("customer_id")
        merchant = get_ctx("merchant", merchant_id)
        if not merchant:
            continue
        category = category_for_merchant(merchant)
        if not category:
            continue
        customer = get_ctx("customer", customer_id) if customer_id else None

        conv_id = f"conv_{merchant_id}_{trigger_id}"
        result = compose(category, merchant, trigger, customer,
                         conversation=conversations.get(conv_id, []))

        # Deduplicate a trigger globally.
        suppression = result.get("suppression_key") or trigger_id
        if suppression in suppression_seen:
            continue
        suppression_seen.add(suppression)

        action = {
            "conversation_id": conv_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": result["send_as"],
            "trigger_id": trigger_id,
            "template_name": "vera_generic_v1",
            "template_params": [merchant_name(merchant), trigger_fact(trigger, category)],
            "body": result["body"],
            "cta": result["cta"],
            "suppression_key": suppression,
            "rationale": result["rationale"]
        }
        record_send(conv_id, result["body"])
        conversations[conv_id].append({"from": "vera", "body": result["body"], "trigger_id": trigger_id})
        actions.append(action)
        if len(actions) >= 20:
            break
    return {"actions": actions}

@app.post("/v1/reply")
async def reply(body: ReplyBody):
    result = response_for_reply(body)
    if result.get("action") == "send":
        record_send(body.conversation_id, result["body"])
        conversations[body.conversation_id].append({"from": "vera", "body": result["body"]})
    return result

@app.post("/v1/teardown")
async def teardown():
    contexts.clear()
    conversations.clear()
    sent_by_conversation.clear()
    suppression_seen.clear()
    return {"ok": True}
