import json
import urllib.request

BASE = "http://localhost:8080"

def req(method, path, body=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    with urllib.request.urlopen(r, timeout=5) as x:
        return json.loads(x.read())

cat = {
    "slug": "dentists",
    "voice": {"tone": "peer_clinical", "taboos": ["cure", "guaranteed"]},
    "offer_catalog": [{"title": "Dental Cleaning @ ₹299"}]
}
merchant = {
    "merchant_id": "m_001",
    "category_slug": "dentists",
    "identity": {"name": "Dr. Meera's Dental Clinic", "owner_first_name": "Meera",
                 "city": "Delhi", "locality": "Lajpat Nagar", "languages": ["en","hi"]},
    "offers": [{"id": "o1", "title": "Dental Cleaning @ ₹299", "status": "active"}],
    "performance": {"views": 2410, "calls": 18, "ctr": 0.021},
    "signals": ["stale_posts:22d", "high_risk_adult_cohort"]
}
trigger = {
    "id": "trg_1", "scope": "merchant", "kind": "research_digest_release",
    "source": "external", "merchant_id": "m_001", "customer_id": None,
    "payload": {"top_item": {"title": "3-mo fluoride recall cuts caries 38% better",
                             "source": "JIDA Oct 2026, p.14"}},
    "urgency": 2, "suppression_key": "research:dentists:2026-W17"
}

assert req("GET", "/v1/healthz")["status"] == "ok"
req("POST", "/v1/context", {"scope":"category","context_id":"dentists","version":1,"payload":cat,"delivered_at":"now"})
req("POST", "/v1/context", {"scope":"merchant","context_id":"m_001","version":1,"payload":merchant,"delivered_at":"now"})
req("POST", "/v1/context", {"scope":"trigger","context_id":"trg_1","version":1,"payload":trigger,"delivered_at":"now"})
out = req("POST", "/v1/tick", {"now":"2026-04-26T10:00:00Z","available_triggers":["trg_1"]})
assert out["actions"], out
print("PASS tick:", out["actions"][0]["body"])

r = req("POST", "/v1/reply", {
    "conversation_id":"conv_m_001_trg_1", "merchant_id":"m_001",
    "customer_id":None, "from_role":"merchant",
    "message":"Ok lets do it. Whats next?", "received_at":"now", "turn_number":2
})
assert r["action"] == "send", r
print("PASS intent:", r["body"])
