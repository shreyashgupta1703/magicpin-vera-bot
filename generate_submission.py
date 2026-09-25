import json, sys
from pathlib import Path
from bot import compose

def load_json(path):
    return json.loads(Path(path).read_text())

def main():
    if len(sys.argv) != 2:
        print("Usage: python generate_submission.py /path/to/dataset")
        raise SystemExit(2)

    root = Path(sys.argv[1])
    cats = {}
    merchants = {}
    triggers = {}

    for p in (root / "categories").glob("*.json"):
        d = load_json(p)
        cats[d.get("slug", p.stem)] = d

    mp = root / "merchants_seed.json"
    if mp.exists():
        d = load_json(mp)
        for x in d.get("merchants", []):
            merchants[x["merchant_id"]] = x

    tp = root / "triggers_seed.json"
    if tp.exists():
        d = load_json(tp)
        for x in d.get("triggers", []):
            triggers[x["id"]] = x

    # The challenge's final 30 test-pair file is not supplied in this starter package.
    # This generator creates one row per trigger available in the local dataset.
    rows = []
    for i, (tid, trg) in enumerate(triggers.items(), 1):
        mid = trg.get("merchant_id")
        merchant = merchants.get(mid)
        if not merchant:
            continue
        category = cats.get(merchant.get("category_slug"), {})
        result = compose(category, merchant, trg, {})
        rows.append({
            "test_id": f"T{i:02d}",
            **result
        })

    Path("submission.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + ("\n" if rows else "")
    )
    print(f"Wrote {len(rows)} rows to submission.jsonl")

if __name__ == "__main__":
    main()
