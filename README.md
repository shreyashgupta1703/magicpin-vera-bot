# magicpin Vera — Merchant AI Assistant

## What this implements

A stateful FastAPI bot for the magicpin Vera challenge.

It implements the required five HTTP endpoints:

- `POST /v1/context`
- `POST /v1/tick`
- `POST /v1/reply`
- `GET /v1/healthz`
- `GET /v1/metadata`

It also implements optional `POST /v1/teardown`.

The architecture follows the challenge's four-context model: Category, Merchant, Trigger and optional Customer context. The bot keeps context across requests, performs trigger-level suppression, creates merchant/customer messages, and handles the replay behaviors emphasized by the challenge: canned auto-replies, intent transitions, hostility/opt-out, language adaptation, and avoiding repetitive messages.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn bot:app --host 0.0.0.0 --port 8080
```

Then:

```bash
curl http://localhost:8080/v1/healthz
curl http://localhost:8080/v1/metadata
```

## Why the implementation is intentionally deterministic

The challenge asks for deterministic behavior for the same inputs and a sub-30-second response budget. This version uses a deterministic composition/routing layer rather than making an LLM call on every request. That removes API latency and quota risk during judging.

The composition logic is organized by trigger family so an LLM can be added later as a bounded rewriter/validator without changing the HTTP contract.

## Message strategy

The composer prioritizes:

1. Verifiable trigger facts.
2. Actual merchant data and active offers.
3. Category-specific voice.
4. One low-friction CTA.
5. Curiosity, social proof, loss aversion, or effort externalization where supported by the input.
6. No invented competitors, studies, metrics, prices, slots, or other facts.
7. Customer outreach only when a customer context and consent scope are present.

## Replay handling

- Repeated/canned auto-replies: stop instead of burning turns.
- "Let's do it"/"go ahead"/"what's next": switch immediately into action mode.
- Explicit stop/not-interested: end.
- Off-topic/unclear messages: stay on mission instead of restarting the entire pitch.
- Mixed Hindi-English signals: adapt phrasing.

## Submission JSONL

The uploaded materials did not include the actual `dataset/` directory, so a real 30-line `submission.jsonl` cannot be truthfully generated from unseen test pairs. Generate it once magicpin provides the dataset using:

```bash
python generate_submission.py dataset/
```

This preserves the required test-pair data instead of inventing test IDs or contexts.

## Deployment

For a simple public deployment:

```bash
uvicorn bot:app --host 0.0.0.0 --port 8080
```

Use a public HTTPS host such as Render/Railway/Fly/AWS/GCP, or an HTTPS tunnel for local testing.

## Important challenge constraints

The challenge requires context persistence, idempotent version handling, response-time discipline, and no external transmission of merchant/customer payloads except permitted LLM APIs. This implementation does not call external services.

Before final submission, run the supplied `judge_simulator.py` against this bot with the provided dataset directory and fix any dataset-specific edge cases exposed by the judge.
