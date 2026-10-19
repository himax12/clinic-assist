# Clinic Assist

A Python terminal assistant for a fictional clinic inbox. Gemini interprets messages; MCP tools verify senders, enforce clinic policies, manage appointments and prepare refill requests. Replies are drafted, never sent.

## Quick start

From the project folder on Windows, with [uv](https://docs.astral.sh/uv/) installed:

```powershell
uv sync --locked --extra dev --python 3.12
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

Add your `GEMINI_API_KEY` to `.env`, then launch:

```powershell
uv run clinic-assist
```

Browsing works without a key. Processing requires available Gemini quota. Keep `.env` local.

- `/inbox` â€” list samples; `/open M001` â€” read one.
- `/run M001` â€” process a sample, or type your own message and supply sender details.
- `/last` â€” view the reply; `/trace` â€” inspect tool calls; `/status` â€” view session details.
- `/sender` â€” change sender details; `/help` â€” all commands; `/quit` â€” exit.

**Tab** completes commands, **Up/Down** recalls input, **Alt+Enter** adds a line and **Enter** submits.

## Demo

https://github.com/user-attachments/assets/b9db70a0-891e-4fd0-aaad-afb8b0f0ce46

41 seconds of the actual `uv run clinic-assist` interface. Shows local browsing and command completion; no model API calls or live triage are demonstrated. The video follows this private repository's access permissions.

## Architecture

```mermaid
flowchart LR
    User[User] --> CLI[Interactive CLI or batch command]
    CLI --> Agent[Agent loop]
    Agent <-->|Model requests and tool proposals| Gemini[Gemini API]
    Agent <-->|MCP over stdio| Tools[MCP server]
    Tools --> Policy[Identity and clinic policy checks]
    Data[Synthetic JSON fixtures] --> Policy
    Policy <--> State[Run-scoped state and operation evidence]
    Policy --> Reply[Validated reply templates]
    Agent --> Output[Results, traces and run metrics]
    Output --> Eval[Separate evaluator]
    Labels[Expected labels and manual safety review] --> Eval
```

The model proposes tools; Python enforces permissions and policies. `agent.py` orchestrates, `providers.py` adapts Gemini, `server.py` exposes MCP tools, `clinic.py` owns operations, `replies.py` renders English/Spanish drafts and `evaluate.py` scores saved results. Labels stay outside inference.

Gemini uses the OpenAI SDK pointed at Google's endpoint. OpenAI is an explicit alternate via `--provider openai`; providers never switch automatically. Gemini can use the configured model fallback chain below.

## Message workflow

```mermaid
flowchart TD
    Input[Incoming message and sender metadata] --> Bind[Validate timestamp and bind original metadata]
    Bind --> Triage[Triage urgency, intents and response language]
    Triage --> Emergency{Emergency detected?}
    Emergency -->|Yes| Urgent[Record urgent handoff and lock mutations]
    Emergency -->|No| Identity[Verify sender before accessing patient records]
    Identity --> Operations[Check policies, clarify, execute permitted tools or hand off]
    Operations --> More{More tool work needed?}
    More -->|Yes| Operations
    More -->|No| Finish[finish_message: validate actions against evidence]
    Urgent --> Finish
    Finish --> Result[Build result and unsent reply without another model call]
    Result --> Save[Save result, actual tool trace and request metrics]
    Triage -.->|Processing failure| Recovery[Preserve completed operations and route for human review]
    Operations -.->|Processing failure| Recovery
    Recovery --> Finish
```

Emergency classification also applies on later triage calls. If a mutation already occurred, its evidence is retained and the run records a safety failure. Exhausted connectivity, credentials or quota errors stop the batch and list remaining messages as unprocessed.

## Safety and scope

- Sender identity comes from host-bound metadata; unverified senders cannot access patient records. Host-only MCP tools are hidden from the model.
- Refills create pending approval requests, never prescriptions. Scheduling checks ownership, hours, leave and overlaps; rescheduling is atomic.
- English/Spanish request guards check recognized provider, date, weekday, time, daypart and visit-type constraints. Explicitly withheld consent and recognized negative or conflicting references require clarification. This is a limited grammar, not general language understanding.
- Empty searches return real alternatives within the following 28 days. Only a query covering the original requested window establishes unavailability; another search cannot supply consent. Cancellation and rescheduling require a uniquely identified owned booking.
- Clinical completion records a clinician handoff. Visit-required refills need provider routing plus a real appointment offer, booking or front-desk follow-up. Host recovery preserves partial operations through a hidden tool.
- Final actions require supporting operation or policy evidence. Replies use constrained templates and emergency locks.
- Messages are independent: sender details and simulated clinic changes persist within a session, but conversation history does not. Follow-ups must restate context.
- Data is synthetic, with a fixed clinic date of **2026-11-09, America/Chicago**. Incoming timestamps must include a timezone.
- The MCP server trusts its local host. This is not ready for real patient data or public exposure.

## Validation status

**224 tests pass. The [latest unseen rehearsal](evaluation/unseen-live-v3/provenance.json) met all six review criteria using real Gemini. The latest revision's full 40-message benchmark is still running; it has not passed yet.**

The [previous-revision full benchmark](evaluation/benchmark-v7/evaluation.json) measured 95.0% urgency accuracy, 62.5% exact action sets, 68.6% action precision, 74.5% action recall, and all five required emergencies escalated. Later [behaviour failures](evaluation/benchmark-v9/operational-findings.json) prompted booking, calendar, triage and verification fixes. A [subsequent run](evaluation/benchmark-v10/evaluation.json) saved 21 successful results and one connection-failure recovery before stopping; M023–M040 remain unprocessed in that attempt. Every saved result has explicit review notes. M011/M027 keep null patient IDs for unverified senders despite conflicting labels.

New reports use `safety_completeness_passed` for the minimum checks and `all_label_checks_passed` for raw correctness. Neither replaces a human submission verdict; historical reports retain their original fields.

The three previous workflow failures are fixed and passed [targeted real Gemini checks](evaluation/workflow-smoke-v1/provenance.json): confirmation before an alternative-time mutation, actual alternatives after an empty search, and automatic handoff for urgent clinical triage. Earlier full baselines and the quota-blocked [19-result attempt](evaluation/benchmark-v6/evaluation.json) remain preserved. Six [unseen live cases on the earlier calendar-guard revision](evaluation/unseen-live-v1/provenance.json) also passed their review criteria.

Latest attempt: 294 model request attempts, 784,507 input / 5,054 output tokens, mean 88.0 seconds per saved result. Actual USD cost is unknown; [cost context](evaluation/benchmark-v7/cost-context.json) documents known usage and public pricing. Quota failures and model choices remain in the traces. See [provenance](evaluation/benchmark-v7/provenance.json).

## Tests and batch runs

```powershell
uv run pytest -q
uv run clinic-assist inspect
uv run clinic-assist run --limit 1 --output output/smoke-1
uv run clinic-assist run --output output/full-1
uv run clinic-assist evaluate --results output/full-1/results.jsonl --output output/full-1/evaluation.json
```

Use a fresh output directory each time. Runs save `results.jsonl`, `traces.jsonl` and `run.json`; processing errors return exit code 2. Scratch runs stay under ignored `output/`; retained evaluation evidence belongs in `evaluation/`.

For safety review, create `observations.json` mapping each reviewed message ID to observed label prohibition names; `[]` means explicitly reviewed clean. Pass it with `evaluate --observations path/to/observations.json`. Reviewed semantic errors, such as a booking on the wrong requested date, belong in `operational-findings.json` beside the results (or pass `--operational-findings`). These findings block readiness even when action labels match.

For your own JSON input, use `run --messages new-message.json --output output/new-1`. Each message needs `id`, timezone-aware `received_at`, `channel` (`sms`, `portal` or `email`), `from_name`, `from_contact` and `body`; `stated_dob` is optional.

Run the standalone MCP server with `uv run python -m clinic_assist.server --data data`. It speaks stdio; the CLI normally starts it automatically.

## Configuration

See [.env.example](.env.example) for provider, model and optional pricing settings. Defaults: Gemini, the configured `GEMINI_MODEL`, 12-second request pacing and a 60-second timeout. Timeout/connection and transient server failures each allow one retry of the same model request without replaying completed MCP operations. Quota retries are bounded; unknown usage and cost stay explicit.

`GEMINI_FALLBACK_MODELS=gemini-2.5-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite` enables an ordered fallback chain (maximum three alternatives). Leave it empty to disable fallback. After bounded retries, initial-request quota, missing-model, temporary server or connection failures can try the next model. Invalid credentials, permission errors, invalid requests and unusable responses stop without switching. Once the first model response succeeds, that model is pinned for the message; later failures preserve operations and hand off without restarting the message or migrating thought signatures.

Traces record every attempted model and fallback reason. Batch reports include configured fallbacks, attempted/used models and the actual model per message. Cost remains unknown when fallback is attempted because a single configured token rate cannot price multiple models reliably. For a fixed-model benchmark, leave `GEMINI_FALLBACK_MODELS` empty in `.env` and remove any inherited nonempty override. A valid Gemini key is required for every model in the chain.

## Built with and next steps

Reused: supplied synthetic fixtures, MCP and OpenAI Python SDKs, Pydantic, Rich, prompt-toolkit and pytest. Built here: policy tools, scoped verification, agent orchestration, evidence-backed replies, evaluation and terminal UI. AI coding assistance was used.

Next: improve exact action selection, explicit clarification and reply relevance using the recorded failures. No sample-ID-specific rules. Replies remain conservative; there is no durable queue, outbound messaging or conversation memory.

Real patient use would require authenticated identity, staff permissions, encrypted durable storage, monitored clinical handoffs, consent/retention controls and clinical/privacy/security review.
