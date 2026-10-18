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
- Final actions require supporting operation or policy evidence. Replies use constrained templates and emergency locks.
- Messages are independent: sender details and simulated clinic changes persist within a session, but conversation history does not. Follow-ups must restate context.
- Data is synthetic, with a fixed clinic date of **2026-11-09, America/Chicago**. Incoming timestamps must include a timezone.
- The MCP server trusts its local host. This is not ready for real patient data or public exposure.

## Validation status

**111 offline tests pass. Live acceptance and the complete 40-case evaluation remain unfinished. A [completed two-case live run](evaluation/reliability-smoke-v5/provenance.json) escalated M003 and prepared M001’s refill, but M001 included an extra routing action and failed exact-action scoring.**

The retained initial 40-case benchmark stopped at its first model request: M001 received a human-review fallback and 39 messages were unprocessed. All five labelled emergencies remain missing. **Submission readiness is false; live model performance and cost are unknown.**

See the [benchmark report](evaluation/benchmark-v1/evaluation.json), [run metadata](evaluation/benchmark-v1/run.json), [traces](evaluation/benchmark-v1/traces.jsonl) and [evidence provenance](evaluation/provenance.json). Failed smoke runs are preserved in `evaluation/reliability-smoke-v1`, `evaluation/reliability-smoke-v2` and [the latest live retry](evaluation/reliability-smoke-v3/provenance.json). That retry received HTTP 429 on the first model request. A [later live fallback attempt](evaluation/reliability-smoke-v4/provenance.json) reached Gemini 2.5 Flash and prepared a refill before a later HTTP 503 caused a safe handoff. Both partial replies/traces were explicitly reviewed; neither attempt completed the live gate. A [four-case scripted rehearsal](evaluation/offline-rehearsal-v1/provenance.json) checks actual CLI/MCP execution, not live model accuracy.

The evaluator measures urgency, exact action sets, action precision/recall, identity, intents and emergency recall. Every reply and trace also needs an explicit review against labelled prohibited behaviours; absent observations do not mean safe. M011/M027 retain identity-label disagreements because unverified senders must have a null patient ID.

## Tests and batch runs

```powershell
uv run pytest -q
uv run clinic-assist inspect
uv run clinic-assist run --limit 1 --output output/smoke-1
uv run clinic-assist run --output output/full-1
uv run clinic-assist evaluate --results output/full-1/results.jsonl --output output/full-1/evaluation.json
```

Use a fresh output directory each time. Runs save `results.jsonl`, `traces.jsonl` and `run.json`; processing errors return exit code 2. Scratch runs stay under ignored `output/`; retained evaluation evidence belongs in `evaluation/`.

For safety review, create `observations.json` mapping each reviewed message ID to observed label prohibition names; `[]` means explicitly reviewed clean. Pass it with `evaluate --observations path/to/observations.json`.

For your own JSON input, use `run --messages new-message.json --output output/new-1`. Each message needs `id`, timezone-aware `received_at`, `channel` (`sms`, `portal` or `email`), `from_name`, `from_contact` and `body`; `stated_dob` is optional.

Run the standalone MCP server with `uv run python -m clinic_assist.server --data data`. It speaks stdio; the CLI normally starts it automatically.

## Configuration

See [.env.example](.env.example) for provider, model and optional pricing settings. Defaults: Gemini, the configured `GEMINI_MODEL`, 12-second request pacing and a 60-second timeout. Timeout/connection and transient server failures each allow one retry of the same model request without replaying completed MCP operations. Quota retries are bounded; unknown usage and cost stay explicit.

`GEMINI_FALLBACK_MODELS=gemini-2.5-flash,gemini-2.5-flash-lite` enables an ordered fallback chain (maximum three alternatives). Leave it empty to disable fallback. After bounded retries, initial-request quota, missing-model, temporary server or connection failures can try the next model. Invalid credentials, permission errors, invalid requests and unusable responses stop without switching. Once the first model response succeeds, that model is pinned for the message; later failures preserve operations and hand off without restarting the message or migrating thought signatures.

Traces record every attempted model and fallback reason. Batch reports include configured fallbacks, attempted/used models and the actual model per message. Cost remains unknown when fallback is attempted because a single configured token rate cannot price multiple models reliably. For a fixed-model benchmark, set `$env:GEMINI_FALLBACK_MODELS = ""` before running. A valid Gemini key is required for every model in the chain.

## Built with and next steps

Reused: supplied synthetic fixtures, MCP and OpenAI Python SDKs, Pydantic, Rich, prompt-toolkit and pytest. Built here: policy tools, scoped verification, agent orchestration, evidence-backed replies, evaluation and terminal UI. AI coding assistance was used.

Next: restore live quota, rerun smoke gates and all 40 cases, review every reply/trace and add independent adversarial cases. No sample-ID-specific rules. Replies remain conservative; there is no durable queue, outbound messaging or conversation memory.

Real patient use would require authenticated identity, staff permissions, encrypted durable storage, monitored clinical handoffs, consent/retention controls and clinical/privacy/security review.
