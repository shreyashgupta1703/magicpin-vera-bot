import json, sys
from pathlib import Path
from bot import compose

def load_json(path):
    return json.loads(Path(path).read_text())

def load_collection(root: Path, singular: str, seed_name: str):
    """Load expanded collection when present; otherwise fall back to seed JSON."""
    out = {}
    folder = root / singular
    if folder.exists():
        for p in sorted(folder.glob("*.json")):
            d = load_json(p)
            if singular == "merchants":
                key = d.get("merchant_id")
            elif singular == "customers":
                key = d.get("customer_id")
            else:
                key = d.get("id")
            if key:
                out[key] = d
    seed = root / seed_name
    if seed.exists():
        d = load_json(seed)
        key_name = {"merchants_seed.json": "merchants",
                    "customers_seed.json": "customers",
                    "triggers_seed.json": "triggers"}[seed_name]
        for x in d.get(key_name, []):
            if key_name == "merchants":
                key = x.get("merchant_id")
            elif key_name == "customers":
                key = x.get("customer_id")
            else:
                key = x.get("id")
            if key:
                out.setdefault(key, x)
    return out

def main():
    if len(sys.argv) != 2:
        print("Usage: python generate_submission.py /path/to/dataset")
        raise SystemExit(2)

    root = Path(sys.argv[1])
    cats = {}
    for p in (root / "categories").glob("*.json"):
        d = load_json(p)
        cats[d.get("slug", p.stem)] = d

    merchants = load_collection(root, "merchants", "merchants_seed.json")
    customers = load_collection(root, "customers", "customers_seed.json")
    triggers = load_collection(root, "triggers", "triggers_seed.json")

    # Prefer the canonical 30 pairs when the expanded dataset provides them.
    pairs = []
    pair_file = root / "test_pairs.json"
    if pair_file.exists():
        raw = load_json(pair_file)
        pairs = raw if isinstance(raw, list) else (raw.get("pairs") or raw.get("test_pairs") or [])

    rows = []
    if pairs:
        for i, pair in enumerate(pairs, 1):
            tid = pair.get("trigger_id") or pair.get("id") or pair.get("trigger")
            mid = pair.get("merchant_id") or pair.get("merchant")
            trg = triggers.get(tid, {})
            merchant = merchants.get(mid)
            if not merchant and trg:
                merchant = merchants.get(trg.get("merchant_id"))
            if not merchant:
                continue
            category = cats.get(merchant.get("category_slug"), {})
            cid = pair.get("customer_id") or trg.get("customer_id")
            customer = customers.get(cid) if (trg.get("scope") == "customer" or cid) else None
            result = compose(category, merchant, trg, customer if trg.get("scope") == "customer" else None)
            rows.append({"test_id": pair.get("test_id", f"T{i:02d}"), **result})
    else:
        for i, (tid, trg) in enumerate(triggers.items(), 1):
            mid = trg.get("merchant_id")
            merchant = merchants.get(mid)
            if not merchant:
                continue
            category = cats.get(merchant.get("category_slug"), {})
            cid = trg.get("customer_id")
            customer = customers.get(cid) if trg.get("scope") == "customer" else None
            result = compose(category, merchant, trg, customer)
            rows.append({"test_id": f"T{i:02d}", **result})

    Path("submission.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + ("\n" if rows else "")
    )
    print(f"Wrote {len(rows)} rows to submission.jsonl")

if __name__ == "__main__":
    main()
