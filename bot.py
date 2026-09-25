import os, re, time, json, hashlib
from datetime import datetime
from typing import Any, Optional
from collections import defaultdict, deque

from fastapi import FastAPI
from pydantic import BaseModel, Field

APP_VERSION = "2.0.0"
START = time.time()
COMPOSER_VERSION = "composer_v2"

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
    if kind == "regulation_change":
        item = _category_item_by_id(category, p.get("top_item_id") or p.get("digest_item_id"))
        title = _text_value(item) if item else "a regulation change"
        deadline = _first(p.get("deadline_iso"), p.get("deadline"))
        body = f"{prefix}, {title}"
        if deadline:
            body += f" — effective {deadline}"
        body += ". Want me to turn the change into a short audit checklist for your practice?"
        rationale_parts += ["compliance trigger", "source-grounded action", "effort externalization"]

    elif kind == "cde_opportunity":
        item = _category_item_by_id(category, p.get("digest_item_id"))
        title = _text_value(item) if item else "a relevant CDE opportunity"
        date = _first(item.get("date") if isinstance(item, dict) else None, p.get("date"))
        credits = _first(p.get("credits"), item.get("credits") if isinstance(item, dict) else None)
        fee = _first(p.get("fee"), item.get("fee") if isinstance(item, dict) else None,
                     item.get("actionable") if isinstance(item, dict) else None)
        body = f"{prefix}, {title}"
        if date:
            body += f" on {date}"
        if credits:
            body += f" ({credits} credits"
            if fee:
                body += f", {fee}"
            body += ")"
        body += ". Want me to pull the registration details?"
        rationale_parts += ["professional development relevance", "specific date/credit detail", "low-friction CTA"]

    elif kind == "renewal_due":
        days = _first(p.get("days_remaining"), merchant.get("subscription", {}).get("days_remaining"))
        plan = _first(p.get("plan"), merchant.get("subscription", {}).get("plan"))
        amount = _first(p.get("renewal_amount"), p.get("amount"))
        body = f"{prefix}, your {plan or 'subscription'} renewal is due"
        if days is not None:
            body += f" in {days} days"
        if amount is not None:
            body += f" (₹{amount} renewal)"
        body += ". Want me to walk you through the renewal step?"
        rationale_parts += ["subscription timing", "concrete commercial detail", "single next step"]

    elif kind == "winback_eligible":
        days = _first(p.get("days_since_expiry"))
        added = _first(p.get("lapsed_customers_added_since_expiry"))
        body = f"{prefix}, your subscription expired {days} days ago" if days is not None else f"{prefix}, your account is eligible for a win-back."
        if added:
            body += f" and {added} customers have entered the lapsed pool since then."
        body += " Want me to show the simplest reactivation step?"
        rationale_parts += ["merchant state", "loss aversion", "one-step CTA"]

    elif kind == "ipl_match_today":
        match = _first(p.get("match"), "today's match")
        venue = p.get("venue")
        body = f"{prefix}, {match} is on today"
        if venue:
            body += f" at {venue}"
        body += "."
        if offer:
            body += f" Your active offer is {offer}."
        body += " Want me to draft one match-night message for the local crowd?"
        rationale_parts += ["timely local event", "actual offer", "operator-focused CTA"]

    elif kind == "supply_alert":
        molecule = _first(p.get("molecule"), "the affected medicine")
        batches = p.get("affected_batches") or []
        manufacturer = _first(p.get("manufacturer"))
        body = f"{prefix}, supply alert on {molecule}"
        if manufacturer:
            body += f" from {manufacturer}"
        if batches:
            body += f" — affected batches: {', '.join(map(str, batches[:3]))}"
        body += ". Want me to turn this into a batch-check/customer-list action?"
        rationale_parts += ["high-urgency safety/supply trigger", "verifiable batch detail", "action CTA"]

    elif kind == "category_seasonal":
        season = _first(p.get("season"))
        trends = p.get("trends") or []
        body = f"{prefix}, the {str(season).replace('_',' ')} demand shift is visible."
        if trends:
            body += " " + ", ".join(str(x).replace("_", " ") for x in trends[:3]) + "."
        body += " Want me to turn the strongest demand signal into one shelf/action change?"
        rationale_parts += ["category trend", "concrete demand signals", "single operational action"]

    elif kind == "gbp_unverified":
        uplift = _first(p.get("estimated_uplift_pct"))
        body = f"{prefix}, your Google Business Profile is still unverified."
        if uplift is not None:
            n = _as_num(uplift)
            shown = n * 100 if n is not None and abs(n) <= 1 else n
            body += f" The context estimates up to {_fmt_pct(shown)} uplift from verification."
        body += " Want me to walk you through the verification path?"
        rationale_parts += ["specific account state", "estimated upside from context", "next-step CTA"]

    elif kind == "active_planning_intent":
        topic = _first(p.get("intent_topic"), "the idea you were planning")
        last_msg = _first(p.get("merchant_last_message"))
        body = f"{prefix}, on {str(topic).replace('_',' ')}"
        if last_msg:
            body += f" — you said, “{last_msg}.”"
        body += " I can turn that into the first concrete draft now. Want me to proceed?"
        rationale_parts += ["explicit merchant intent", "conversation continuity", "action handoff"]

    elif kind == "dormant_with_vera":
        days = _first(p.get("days_since_last_merchant_message"))
        body = f"{prefix}, it’s been {days} days since your last Vera conversation." if days is not None else f"{prefix}, it’s been a while since your last Vera conversation."
        topic = _first(p.get("last_topic"))
        if topic:
            body += f" Your last topic was {str(topic).replace('_',' ')}."
        body += " Want one useful update rather than a generic check-in?"
        rationale_parts += ["conversation-aware dormancy", "curiosity", "low-friction CTA"]

    elif kind in {"trial_followup", "wedding_package_followup", "chronic_refill_due"}:
        # Customer-specific triggers are handled above when customer context is
        # present. This fallback keeps the live judge useful if customer context
        # arrives slightly later than the trigger.
        fact = _trigger_fact(category, trigger)
        body = f"{prefix}, {fact}."
        body += " Want me to prepare the next customer-facing step?"
        rationale_parts += ["customer trigger preserved", "action handoff"]

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


def _first(*vals):
    for v in vals:
        if v is not None and str(v).strip() != "":
            return v
    return None

def _as_num(v):
    try:
        if isinstance(v, bool):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None

def _fmt_pct(v):
    n = _as_num(v)
    if n is None:
        return str(v)
    if n.is_integer():
        return f"{int(n)}%"
    return f"{n:.1f}%"

def _fmt_money(v):
    if v is None:
        return None
    s = str(v)
    if "₹" in s:
        return s
    n = _as_num(v)
    if n is not None:
        return f"₹{int(n) if n.is_integer() else n:g}"
    return s

def _text_value(v):
    if isinstance(v, dict):
        for k in ("title", "name", "label", "summary", "headline", "value", "text", "description"):
            if v.get(k):
                return str(v[k])
        return ""
    return str(v) if v is not None else ""

def _source_suffix(item):
    if not isinstance(item, dict):
        return ""
    source = _first(item.get("source"), item.get("publisher"), item.get("journal"))
    date = _first(item.get("date"), item.get("published_at"), item.get("issue"))
    page = _first(item.get("page"), item.get("page_no"))
    parts = [str(x) for x in (source, date, f"p.{page}" if page else None) if x]
    return " — " + " ".join(parts) if parts else ""

def _category_item_by_id(category, item_id):
    if not item_id:
        return None
    for item in category.get("digest", []) or []:
        if isinstance(item, dict) and item.get("id") == item_id:
            return item
    return None

def _digest_item(category, trigger):
    p = trigger.get("payload", {}) or {}
    item = p.get("top_item")
    if isinstance(item, dict):
        return item
    item = _category_item_by_id(category, p.get("top_item_id") or p.get("digest_item_id") or p.get("alert_id"))
    if item:
        return item
    for key in ("digest_item", "research", "item"):
        if isinstance(p.get(key), dict):
            return p[key]
    digest = category.get("digest", []) or []
    return digest[0] if digest else None

def _performance_snapshot(merchant, trigger):
    p = trigger.get("payload", {}) or {}
    perf = p.get("performance")
    if not isinstance(perf, dict):
        perf = {}
    # Trigger values win because they explain WHY NOW.
    merged = dict(merchant.get("performance", {}) or {})
    merged.update(perf)
    for k, v in p.items():
        if k in {"views", "calls", "ctr", "leads", "directions", "views_pct",
                  "calls_pct", "ctr_pct", "leads_pct", "change_pct",
                  "before", "after", "metric"}:
            merged[k] = v
    return merged

def _change_phrase(trigger):
    p = trigger.get("payload", {}) or {}
    perf = _performance_snapshot({}, trigger)
    metric = _first(p.get("metric"), perf.get("metric"))
    pct = _first(p.get("change_pct"), p.get("delta_pct"), p.get("pct"))
    if pct is None:
        for k in ("views_pct", "calls_pct", "ctr_pct", "leads_pct"):
            if k in p:
                metric = metric or k.replace("_pct", "")
                pct = p[k]
                break
    n = _as_num(pct)
    if metric and n is not None:
        # Trigger payloads use either decimal deltas (0.15 = 15%) or
        # percentage points (15 = 15%). Normalize for merchant-facing copy.
        shown = n * 100 if abs(n) <= 1 else n
        direction = "up" if n > 0 else "down" if n < 0 else "flat"
        label = metric.replace("_", " ")
        verb = "is" if label.lower() in {"ctr", "conversion rate", "rating"} else "are"
        return f"{label} {verb} {direction} {_fmt_pct(abs(shown))}"
    if metric and _first(p.get("before"), p.get("after")) is not None:
        return f"{metric.replace('_',' ')} moved from {_first(p.get('before'))} to {_first(p.get('after'))}"
    return None

def _merchant_state_hook(category, merchant, trigger):
    perf = merchant.get("performance", {}) or {}
    peer = category.get("peer_stats", {}) or {}
    signals = merchant.get("signals", []) or []
    hooks = []

    ctr = _as_num(perf.get("ctr"))
    peer_ctr = _as_num(_first(peer.get("avg_ctr"), peer.get("typical_ctr"), peer.get("median_ctr")))
    if ctr is not None and peer_ctr is not None:
        if ctr < peer_ctr:
            hooks.append(f"CTR {ctr:.1%} vs {peer_ctr:.1%} peer benchmark")
        elif ctr > peer_ctr:
            hooks.append(f"CTR {ctr:.1%} vs {peer_ctr:.1%} peer benchmark")

    if isinstance(signals, list):
        for s in signals[:4]:
            text = _text_value(s).replace("_", " ").strip()
            if text and text.lower() not in {"none", "null"}:
                hooks.append(text)

    agg = merchant.get("customer_aggregate", {}) or {}
    for key, label in (
        ("lapsed_count", "lapsed customers"),
        ("active_count", "active customers"),
        ("retention_rate", "retention"),
    ):
        if agg.get(key) is not None:
            value = agg[key]
            if key == "retention_rate" and _as_num(value) is not None:
                value = f"{_as_num(value):.0%}" if _as_num(value) <= 1 else _fmt_pct(value)
            hooks.append(f"{value} {label}")
    return hooks

def _category_voice(category):
    voice = category.get("voice", {}) or {}
    return str(_first(voice.get("tone"), voice.get("style"), "")).lower()

def _merchant_language(merchant, conversation=None):
    langs = [str(x).lower() for x in languages(merchant)]
    if any(x in {"hi-en", "hinglish", "hindi"} for x in langs):
        return "hi-en"
    return "en"

def _customer_language(customer):
    ident = customer.get("identity", {}) or {}
    pref = str(_first(ident.get("language_pref"), ident.get("language"), "")).lower()
    return "hi-en" if "hi" in pref or "hinglish" in pref else "en"

def _customer_slot(customer):
    prefs = customer.get("preferences", {}) or {}
    for key in ("preferred_slot", "preferred_time", "preferred_slot_time"):
        if prefs.get(key):
            return str(prefs[key])
    return None

def _customer_offer(merchant, category):
    offers = active_offers(merchant)
    if offers:
        # Prefer service+price offers over generic percentage discounts.
        ranked = sorted(offers, key=lambda o: (
            0 if ("₹" in str(o.get("title","")) or o.get("price") is not None) else 1,
            len(str(o.get("title","")))
        ))
        return ranked[0].get("title")
    return choose_offer(category, merchant)

def _cta(body):
    # Merchant-facing CTAs are intentionally single and open-ended.
    return "open_ended"

def compose(category: dict, merchant: dict, trigger: dict,
            customer: Optional[dict] = None,
            prior_message: str = "",
            conversation: Optional[list[dict]] = None) -> dict:
    """
    Deterministic, context-first composer.
    The strategy is: select the strongest WHY-NOW signal, add 1–2
    merchant-specific facts, use the category voice, and finish with
    one low-friction CTA. No facts are invented.
    """
    kind = str(trigger.get("kind", "")).lower()
    p = trigger.get("payload", {}) or {}
    ident = merchant.get("identity", {}) or {}
    owner = ident.get("owner_first_name") or ident.get("name") or "there"
    business = ident.get("name") or "your business"
    locality = ident.get("locality") or ident.get("city")
    offer = _customer_offer(merchant, category)
    conv = conversation or []

    # -------------------- customer-facing --------------------
    if customer is not None and trigger.get("scope") == "customer":
        cident = customer.get("identity", {}) or {}
        cname = cident.get("name") or "there"
        consent = customer.get("consent", {}) or {}
        if consent and consent.get("opt_in") is False:
            return {"body": "I can't send this outreach because the customer is not opted in.",
                    "cta": "none", "send_as": "merchant_on_behalf",
                    "suppression_key": trigger.get("suppression_key", ""),
                    "rationale": "Suppressed because customer opt-in is explicitly false."}

        lang = _merchant_language(merchant)
        last = (customer.get("relationship", {}) or {}).get("last_visit")
        slot = _customer_slot(customer)

        if kind == "recall_due":
            due = _first(p.get("due_date"), p.get("appointment_date"))
            service = _first(p.get("service_due"), p.get("service"), "your next visit")
            parts = [f"Hi {cname}, {business} here."]
            if due:
                parts.append(f"Your {str(service).replace('_', ' ')} is due on {due}.")
            elif last:
                parts.append(f"Your last visit was {last}, so your next {str(service).replace('_', ' ')} is due.")
            else:
                parts.append(f"Your {str(service).replace('_', ' ')} is due.")
            slots = p.get("available_slots") or []
            labels = [str(x.get("label")) for x in slots[:2] if isinstance(x, dict) and x.get("label")]
            if labels:
                parts.append("I have " + " or ".join(labels) + " available.")
            elif offer:
                parts.append(f"We currently have {offer}.")
            if slot:
                parts.append(f"Your usual preference is {slot}.")
            parts.append("Reply YES with the slot you prefer, or STOP to opt out.")
            body = " ".join(parts)
        elif kind in {"customer_lapsed_soft", "customer_lapsed_hard"}:
            state = "It's been a while since your last visit."
            if p.get("days_since_last_visit"):
                state = f"It's been {p.get('days_since_last_visit')} days since your last visit."
            elif last:
                state = f"Your last visit was {last}."
            services = (customer.get("relationship", {}) or {}).get("services_received") or []
            body = f"Hi {cname}, {business} here. {state}"
            if services:
                body += f" We last saw you for {str(services[-1]).replace('_', ' ')}."
            if offer:
                body += f" We currently have {offer}."
            body += " Reply YES if you'd like the details, or STOP to opt out."
        elif kind == "appointment_tomorrow":
            appt = _first(p.get("appointment_time"), p.get("time"), p.get("appointment_date"))
            body = f"Hi {cname}, {business} here. Your appointment is tomorrow"
            if appt:
                body += f" at {appt}"
            body += ". Reply YES to confirm, or STOP to opt out."
        elif kind == "wedding_package_followup":
            wedding = _first(p.get("wedding_date"))
            window = _first(p.get("next_step_window_open"), "the next prep step")
            body = f"Hi {cname}, {business} here. Your wedding date is {wedding}." if wedding else f"Hi {cname}, {business} here."
            body += f" Your next step is {str(window).replace('_',' ')}."
            if offer:
                body += f" We currently have {offer}."
            body += " Reply YES if you'd like us to map the next step, or STOP to opt out."
        elif kind == "trial_followup":
            trial = _first(p.get("trial_date"))
            options = p.get("next_session_options") or []
            labels = [str(x.get("label")) for x in options[:2] if isinstance(x, dict) and x.get("label")]
            body = f"Hi {cname}, {business} here. Your trial was on {trial}." if trial else f"Hi {cname}, {business} here."
            if labels:
                body += " Next session options: " + " or ".join(labels) + "."
            body += " Reply YES if you'd like to continue, or STOP to opt out."
        elif kind == "chronic_refill_due":
            molecules = p.get("molecule_list") or []
            stockout = _first(p.get("stock_runs_out_iso"))
            body = f"Hi {cname}, {business} here. Your refill is due"
            if stockout:
                body += f" before {stockout}"
            if molecules:
                body += f" for {', '.join(map(str, molecules[:3]))}"
            body += ". Reply YES if you'd like us to arrange the refill, or STOP to opt out."
        elif kind == "customer_lapsed_hard":
            days = _first(p.get("days_since_last_visit"))
            focus = _first(p.get("previous_focus"))
            body = f"Hi {cname}, {business} here. It's been {days} days since your last visit" if days else f"Hi {cname}, {business} here. It's been a while since your last visit"
            if focus:
                body += f", after your {str(focus).replace('_',' ')} focus."
            if offer:
                body += f" We currently have {offer}."
            body += " Reply YES if you'd like the details, or STOP to opt out."
        elif kind == "unplanned_slot_open":
            slot_open = _first(p.get("slot"), p.get("time"), p.get("slot_time"))
            body = f"Hi {cname}, {business} here. A slot just opened"
            if slot_open:
                body += f" at {slot_open}"
            body += ". Reply YES if you'd like us to hold it, or STOP to opt out."
        else:
            fact = _trigger_fact(category, trigger)
            body = f"Hi {cname}, {business} here. {fact}"
            body += " Reply YES if you'd like the details, or STOP to opt out."

        if _customer_language(customer) == "hi-en" and kind in {"recall_due", "customer_lapsed_soft", "customer_lapsed_hard"}:
            body = body.replace("Reply YES", "Agar useful lage, YES reply karein")

        return {
            "body": body,
            "cta": "YES/STOP",
            "send_as": "merchant_on_behalf",
            "suppression_key": trigger.get("suppression_key", ""),
            "rationale": f"Customer path: {kind} uses customer relationship/preferences, merchant context, and a single opt-in-safe CTA."
        }

    # -------------------- merchant-facing --------------------
    prefix = f"Dr. {owner}" if "dentist" in str(category.get("slug","")).lower() and not str(owner).lower().startswith("dr.") else str(owner)
    rationale_parts = []

    if kind in {"research_digest", "research_digest_release", "category_research_digest_release"}:
        item = _digest_item(category, trigger)
        title = _text_value(item) if item else _first(p.get("title"), p.get("headline"))
        body = f"{prefix}, {title or 'this week’s category research digest has landed'}{_source_suffix(item)}."
        cohort = _first(p.get("relevant_cohort"), p.get("patient_cohort"),
                        merchant.get("customer_aggregate", {}).get("high_risk_adult_count"))
        if not cohort:
            for signal in merchant.get("signals", []) or []:
                st = str(signal).lower().replace("_", " ")
                if "high risk" in st:
                    cohort = "your high-risk patient cohort"
                    break
        if cohort:
            body += f" It is especially relevant to {cohort}."
        body += " Want me to pull the key point and draft one ready-to-share message?"
        rationale_parts += ["research source", "merchant-specific relevance", "effort externalization"]

    elif kind == "regulation_change":
        item = _category_item_by_id(category, p.get("top_item_id") or p.get("digest_item_id"))
        title = _text_value(item) if item else "a regulation change"
        deadline = _first(p.get("deadline_iso"), p.get("deadline"))
        body = f"{prefix}, {title}"
        if deadline:
            body += f" — effective {deadline}"
        body += ". Want me to turn the change into a short audit checklist for your practice?"
        rationale_parts += ["compliance trigger", "source-grounded action", "effort externalization"]

    elif kind == "cde_opportunity":
        item = _category_item_by_id(category, p.get("digest_item_id"))
        title = _text_value(item) if item else "a relevant CDE opportunity"
        date = _first(item.get("date") if isinstance(item, dict) else None, p.get("date"))
        credits = _first(p.get("credits"), item.get("credits") if isinstance(item, dict) else None)
        fee = _first(p.get("fee"), item.get("fee") if isinstance(item, dict) else None)
        body = f"{prefix}, {title}"
        if date:
            body += f" on {date}"
        if credits:
            body += f" ({credits} credits"
            if fee:
                body += f", {fee}"
            body += ")"
        body += ". Want me to pull the registration details?"
        rationale_parts += ["professional development relevance", "specific date/credit detail", "low-friction CTA"]

    elif kind in {"perf_dip", "seasonal_perf_dip"}:
        change = _change_phrase(trigger)
        body = f"{prefix}, I spotted {change or 'a performance dip in your latest snapshot'}."
        hooks = _merchant_state_hook(category, merchant, trigger)
        if hooks:
            body += f" Your {hooks[0]} is the useful comparison point."
        body += " Want me to isolate the biggest driver and suggest one fix?"
        rationale_parts += ["loss-aware performance signal", "merchant benchmark", "one concrete next step"]

    elif kind == "perf_spike":
        change = _change_phrase(trigger)
        body = f"{prefix}, {change or 'your latest performance snapshot is up'}."
        hooks = _merchant_state_hook(category, merchant, trigger)
        if hooks:
            body += f" {hooks[0]}."
        body += " Want me to identify what likely drove the lift so you can repeat it?"
        rationale_parts += ["positive performance signal", "repeatable learning CTA"]

    elif kind == "milestone_reached":
        milestone = _first(p.get("milestone"), p.get("milestone_value"), p.get("value_now"),
                           p.get("value"), p.get("count"), p.get("target"))
        body = f"{prefix}, you just hit {milestone or 'a new milestone'}."
        body += " Want one next-step idea to capitalize on it?"
        rationale_parts += ["milestone recognition", "next-best-action"]

    elif kind == "review_theme_emerged":
        theme = _first(p.get("theme"), p.get("review_theme"), p.get("summary"))
        count = _first(p.get("review_count"), p.get("occurrences_30d"), p.get("count"), p.get("mentions"))
        body = f"{prefix}, recent reviews are clustering around {theme or 'one theme'}"
        if count:
            body += f" ({count} mentions)"
        body += ". Want the exact pattern plus a ready-to-send response?"
        rationale_parts += ["review evidence", "effort externalization"]

    elif kind in {"festival", "festival_upcoming"}:
        event = _first(p.get("name"), p.get("event"), p.get("festival"), p.get("title"))
        date = _first(p.get("date"), p.get("event_date"))
        body = f"{prefix}, {event or 'an upcoming festival'} is coming up"
        if date:
            body += f" on {date}"
        body += "."
        if offer:
            body += f" Your active offer is {offer}."
        body += " Want me to turn the timing into one service-first message?"
        rationale_parts += ["timely event", "real active offer", "category-correct service framing"]

    elif kind == "competitor_opened":
        comp = _first(p.get("competitor"), p.get("competitor_name"), p.get("name"))
        distance = _first(p.get("distance"), p.get("distance_km"))
        locality_text = _first(p.get("locality"), p.get("area"))
        their_offer = p.get("their_offer")
        body = f"{prefix}, {comp or 'a nearby competitor'} just opened"
        if distance:
            body += f" {distance} km away" if _as_num(distance) is not None else f" {distance} away"
        if locality_text:
            body += f" in {locality_text}"
        if their_offer:
            body += f" with {their_offer}"
        body += ". Want me to compare the concrete profile differences that matter to customers?"
        rationale_parts += ["local competitive trigger", "specific competitor context", "curiosity"]

    elif kind == "curious_ask_due":
        service = _first(p.get("service"), p.get("topic"), p.get("question_topic"))
        body = (f"{prefix}, quick operator question: are customers asking you more about {service} this week?"
                if service else
                f"{prefix}, quick operator question: what service are customers asking you for most this week?")
        body += " Reply with the service name and I'll turn it into one useful growth idea."
        rationale_parts += ["asking the merchant", "curiosity", "low-friction response"]

    elif kind == "renewal_due":
        days = _first(p.get("days_remaining"), merchant.get("subscription", {}).get("days_remaining"))
        plan = _first(p.get("plan"), merchant.get("subscription", {}).get("plan"))
        amount = _first(p.get("renewal_amount"), p.get("amount"))
        body = f"{prefix}, your {plan or 'subscription'} renewal is due"
        if days is not None:
            body += f" in {days} days"
        if amount is not None:
            body += f" (₹{amount} renewal)"
        body += ". Want me to walk you through the renewal step?"
        rationale_parts += ["subscription timing", "concrete commercial detail", "single next step"]

    elif kind == "winback_eligible":
        days = _first(p.get("days_since_expiry"))
        added = _first(p.get("lapsed_customers_added_since_expiry"))
        body = f"{prefix}, your subscription expired {days} days ago" if days is not None else f"{prefix}, your account is eligible for a win-back."
        if added:
            body += f" and {added} customers have entered the lapsed pool since then."
        body += " Want me to show the simplest reactivation step?"
        rationale_parts += ["merchant state", "loss aversion", "one-step CTA"]

    elif kind == "ipl_match_today":
        match = _first(p.get("match"), "today's match")
        venue = p.get("venue")
        body = f"{prefix}, {match} is on today"
        if venue:
            body += f" at {venue}"
        body += "."
        if offer:
            body += f" Your active offer is {offer}."
        body += " Want me to draft one match-night message for the local crowd?"
        rationale_parts += ["timely local event", "actual offer", "operator-focused CTA"]

    elif kind == "supply_alert":
        molecule = _first(p.get("molecule"), "the affected medicine")
        batches = p.get("affected_batches") or []
        manufacturer = _first(p.get("manufacturer"))
        body = f"{prefix}, supply alert on {molecule}"
        if manufacturer:
            body += f" from {manufacturer}"
        if batches:
            body += f" — affected batches: {', '.join(map(str, batches[:3]))}"
        body += ". Want me to turn this into a batch-check/customer-list action?"
        rationale_parts += ["high-urgency supply trigger", "verifiable batch detail", "action CTA"]

    elif kind == "category_seasonal":
        season = _first(p.get("season"), "the current season")
        trends = p.get("trends") or []
        body = f"{prefix}, the {str(season).replace('_',' ')} demand shift is visible."
        if trends:
            body += " " + ", ".join(str(x).replace("_", " ") for x in trends[:3]) + "."
        body += " Want me to turn the strongest demand signal into one shelf/action change?"
        rationale_parts += ["category trend", "concrete demand signals", "single operational action"]

    elif kind == "gbp_unverified":
        uplift = _first(p.get("estimated_uplift_pct"))
        body = f"{prefix}, your Google Business Profile is still unverified."
        if uplift is not None:
            n = _as_num(uplift)
            shown = n * 100 if n is not None and abs(n) <= 1 else n
            body += f" The context estimates up to {_fmt_pct(shown)} uplift from verification."
        body += " Want me to walk you through the verification path?"
        rationale_parts += ["specific account state", "estimated upside from context", "next-step CTA"]

    elif kind == "active_planning_intent":
        topic = _first(p.get("intent_topic"), "the idea you were planning")
        last_msg = _first(p.get("merchant_last_message"))
        body = f"{prefix}, on {str(topic).replace('_',' ')}"
        if last_msg:
            body += f" — you said, “{last_msg}.”"
        body += " I can turn that into the first concrete draft now. Want me to proceed?"
        rationale_parts += ["explicit merchant intent", "conversation continuity", "action handoff"]

    elif kind == "dormant_with_vera":
        days = _first(p.get("days_since_last_merchant_message"))
        body = f"{prefix}, it’s been {days} days since your last Vera conversation." if days is not None else f"{prefix}, it’s been a while since your last Vera conversation."
        topic = _first(p.get("last_topic"))
        if topic:
            body += f" Your last topic was {str(topic).replace('_',' ')}."
        body += " Want one useful update rather than a generic check-in?"
        rationale_parts += ["conversation-aware dormancy", "curiosity", "low-friction CTA"]

    elif kind in {"trial_followup", "wedding_package_followup", "chronic_refill_due"}:
        fact = _trigger_fact(category, trigger)
        body = f"{prefix}, {fact}. Want me to prepare the next customer-facing step?"
        rationale_parts += ["customer trigger preserved", "action handoff"]

    else:
        fact = _trigger_fact(category, trigger)
        body = f"{prefix}, {fact}."
        hooks = _merchant_state_hook(category, merchant, trigger)
        if hooks:
            body += f" Your {hooks[0]} is the relevant context here."
        body += " Want me to turn this into one concrete next step?"
        rationale_parts += ["trigger-grounded fallback", "merchant context"]

    # Language preference: only adapt where we can do it without harming category voice.
    if _merchant_language(merchant) == "hi-en" and kind in {
        "curious_ask_due", "perf_dip", "perf_spike", "dormant_with_vera"
    }:
        body = body.replace("quick operator question:", "ek quick operator question:")
        body = body.replace("Want me to", "Batao, kya main")
        body = body.replace("Reply with", "Bas")

    # Category-specific guardrails: keep clinical categories peer-like, not promotional.
    if "dentist" in str(category.get("slug","")).lower():
        body = body.replace("growth idea", "practice idea")
        body = body.replace("AMAZING", "").replace("deal", "offer")

    # Anti-repetition: change the angle rather than appending generic filler.
    if any(norm(t.get("body") or t.get("msg")) == norm(body)
           for t in conv if t.get("from") == "vera"):
        body = body.rstrip(".") + " I can focus on the most useful detail if you'd like."

    return {
        "body": body,
        "cta": _cta(body),
        "send_as": "vera",
        "suppression_key": trigger.get("suppression_key", ""),
        "rationale": " + ".join(rationale_parts) or "Context-grounded trigger routing."
    }

def _trigger_fact(trigger: dict, category: dict) -> str:
    p = trigger.get("payload", {}) or {}
    kind = str(trigger.get("kind", "")).lower()
    if kind in {"research_digest", "research_digest_release", "category_research_digest_release"}:
        item = _digest_item(category, trigger)
        title = _text_value(item) if item else _first(p.get("title"), p.get("headline"))
        return f"{title or 'A new category research item landed'}{_source_suffix(item)}"
    if kind in {"perf_spike", "perf_dip", "seasonal_perf_dip"}:
        change = _change_phrase(trigger)
        return change or "your latest performance snapshot changed"
    if kind == "milestone_reached":
        return str(_first(p.get("milestone"), p.get("milestone_value"), p.get("value_now"), p.get("value"), p.get("count"), "a milestone"))
    if kind in {"festival", "festival_upcoming"}:
        return str(_first(p.get("name"), p.get("event"), p.get("festival"), "an upcoming festival"))
    if kind == "competitor_opened":
        return str(_first(p.get("distance"), p.get("distance_km"), p.get("competitor"), p.get("competitor_name"), "a nearby competitor"))
    if kind == "review_theme_emerged":
        return str(_first(p.get("theme"), p.get("summary"), "a review pattern"))
    if kind in {"recall_due", "customer_lapsed_soft", "customer_lapsed_hard"}:
        return str(_first(p.get("due_date"), p.get("last_visit"), "a customer recall window"))
    if kind == "regulation_change":
        item = _category_item_by_id(category, p.get("top_item_id") or p.get("digest_item_id"))
        return _text_value(item) if item else "a regulation change"
    if kind == "supply_alert":
        return f"{_first(p.get('molecule'), 'a medicine')} supply alert"
    if kind == "category_seasonal":
        return str(_first(p.get("season"), "a seasonal demand shift")).replace("_", " ")
    return str(_first(p.get("headline"), p.get("title"), p.get("reason"), kind or "a relevant signal"))


def response_for_reply(body: ReplyBody) -> dict:
    hist = conversations[body.conversation_id]
    msg = norm(body.message)
    auto = is_auto_reply(msg, hist)
    intent = detect_intent(msg)

    # Keep the raw merchant/customer turn for future routing.
    hist.append({"from": body.from_role, "msg": msg, "turn": body.turn_number})

    if auto:
        return {
            "action": "end",
            "rationale": "Repeated/canned WhatsApp Business auto-reply detected; stopping instead of burning turns."
        }

    if intent == "stop":
        return {"action": "end", "rationale": "Merchant explicitly requested no further messaging."}

    last_vera = next((t for t in reversed(hist[:-1]) if t.get("from") == "vera"), {})
    last_body = norm(last_vera.get("body", "")).lower()

    if intent == "commit":
        if any(x in last_body for x in ["research", "abstract", "digest"]):
            reply = "Done — I’ll pull the source material and prepare the short, merchant-ready takeaway plus the patient-facing draft."
            rationale = "Commitment after a research/content hook routed directly to execution."
        elif any(x in last_body for x in ["performance", "driver", "dip", "lift"]):
            reply = "Done — I’ll isolate the main performance driver and turn it into one concrete fix rather than another list of suggestions."
            rationale = "Commitment after a performance hook routed to the promised analysis."
        elif any(x in last_body for x in ["review", "response"]):
            reply = "Done — I’ll turn the review pattern into a concise response draft you can use."
            rationale = "Commitment after a review-theme hook routed to the promised draft."
        elif any(x in last_body for x in ["competitor", "profile differences"]):
            reply = "Done — I’ll compare the concrete profile differences and focus only on the changes that matter locally."
            rationale = "Commitment after a competitor hook routed to the promised comparison."
        elif any(x in last_body for x in ["offer", "festival", "service-first"]):
            reply = "Done — I’ll draft the service-first version using your active offer and the timing already in context."
            rationale = "Commitment after an offer/event hook routed to execution."
        else:
            reply = "Done — I’m moving to action mode and will take the next step from the context already provided, only asking for a detail if it is genuinely required."
            rationale = "Detected explicit commitment and moved directly from pitch/qualification to action."
        return {"action": "send", "body": reply, "cta": "open_ended", "rationale": rationale}

    if intent == "question":
        # Don't ask another broad qualification question. Acknowledge the
        # question and narrow only to the missing detail.
        return {
            "action": "send",
            "body": "Yes — I can help with that. I’ll use the context already available and keep the next step focused; if one detail is missing, I’ll ask only for that.",
            "cta": "open_ended",
            "rationale": "Answered/advanced the question without restarting qualification."
        }

    merchant_turns = sum(1 for t in hist if t.get("from") == body.from_role)
    if merchant_turns >= 3:
        return {"action": "wait", "wait_seconds": 1800,
                "rationale": "Backing off after multiple low-signal turns."}

    return {
        "action": "send",
        "body": "Got it. I’ll keep this focused and prepare the concrete next step rather than asking you a chain of qualifying questions.",
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
        "model": os.getenv("MODEL_NAME", "deterministic-composer-v2"),
        "approach": "context-first trigger routing + merchant-state hooks + customer-aware composition + stateful conversation routing",
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
