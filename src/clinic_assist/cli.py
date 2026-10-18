import argparse
import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import sys

from dotenv import load_dotenv

from .contracts import Message, MessageResult
from .data import ClinicData
from .evaluate import evaluate


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


async def run(args) -> int:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from .agent import Agent
    from .providers import model_client

    raw = read_json(args.messages)
    messages = raw if isinstance(raw, list) else [raw]
    messages = [Message.model_validate(m).model_dump(mode="json") for m in messages]
    if len({m["id"] for m in messages}) != len(messages):
        raise ValueError("Duplicate input message IDs")
    if args.limit:
        messages = messages[:args.limit]
    # Never silently overwrite evidence from an earlier run.
    args.output.mkdir(parents=True, exist_ok=False)
    tokens = {"input_tokens": 0, "output_tokens": 0}
    failures = []
    processed_ids = []
    write_json(args.output / "run.json", {"status": "running", "provider": args.provider, "model": args.model,
        "requested_ids": [m["id"] for m in messages], "unprocessed_ids": [m["id"] for m in messages]})
    params = StdioServerParameters(command=sys.executable,
        args=["-m", "clinic_assist.server", "--data", str(args.data.resolve())])
    key_name = "GEMINI_API_KEY" if args.provider == "gemini" else "OPENAI_API_KEY"
    async with model_client(args.provider, os.environ[key_name], min_interval=args.request_interval) as client:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=30)) as session:
                await session.initialize()
                agent = Agent(session, client, args.model)
                with (args.output / "results.jsonl").open("w", encoding="utf-8") as result_file, \
                     (args.output / "traces.jsonl").open("w", encoding="utf-8") as trace_file:
                    for message in messages:
                        try:
                            outcome = await agent.process(message)
                        except Exception as exc:
                            # Transport/preflight/recovery failures cannot honestly
                            # yield a successful handoff or complete MessageResult.
                            failures.append({"id": message["id"], "error": type(exc).__name__, "detail": "Processing interrupted; inspect partial trace"})
                            trace_file.write(json.dumps({"message_id": message["id"], "error": type(exc).__name__, "trace": agent.trace}, ensure_ascii=False) + "\n")
                            trace_file.flush()
                            for key in tokens:
                                tokens[key] += agent.usage[key]
                            break
                        result_file.write(json.dumps(outcome["result"], ensure_ascii=False) + "\n")
                        trace_file.write(json.dumps(outcome, ensure_ascii=False) + "\n")
                        result_file.flush()
                        trace_file.flush()
                        processed_ids.append(message["id"])
                        for key in tokens:
                            tokens[key] += outcome["usage"][key]
                        if outcome["error"]:
                            failures.append({"id": message["id"], "error": outcome["error"]})
                        print(message["id"] + (" -> human review (processing error)" if outcome["error"] else " -> processed"), file=sys.stderr)
                        if outcome.get("provider_error_status") in {400, 401, 402, 403, 404, 422, 429}:
                            print("Provider access or quota unavailable; stopping remaining messages.", file=sys.stderr)
                            break
    input_rate, output_rate = os.getenv("INPUT_USD_PER_MILLION"), os.getenv("OUTPUT_USD_PER_MILLION")
    estimate = ((tokens["input_tokens"] * float(input_rate) + tokens["output_tokens"] * float(output_rate)) / 1_000_000
                if input_rate and output_rate else None)
    write_json(args.output / "run.json", {"status": "failed" if failures else "completed", "provider": args.provider, "model": args.model,
        "messages": len(messages), "processed_ids": processed_ids,
        "unprocessed_ids": [m["id"] for m in messages if m["id"] not in processed_ids], "tokens": tokens,
        "estimated_cost_usd": estimate, "cost_basis": "Configured standard rates; ignores cache discounts" if estimate is not None else "Rates not configured",
        "errors": failures, "state_model": "Shared sequential MCP server; fresh source data at process start"})
    print(str(args.output / "results.jsonl"))
    return 2 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Synthetic clinic inbox agent and independent evaluation")
    sub = parser.add_subparsers(dest="command", required=True)
    chat_cmd = sub.add_parser("chat", help="Open the interactive clinic inbox workspace (default)")
    chat_cmd.add_argument("--data", type=Path, default=Path("data"))
    chat_cmd.add_argument("--output", type=Path)
    chat_cmd.add_argument("--model")
    chat_cmd.add_argument("--provider", choices=["gemini", "openai"])
    chat_cmd.add_argument("--request-interval", type=float)
    chat_cmd.add_argument("--no-env", action="store_true")
    inspect_cmd = sub.add_parser("inspect", help="Validate input files without API calls")
    inspect_cmd.add_argument("--data", type=Path, default=Path("data"))
    run_cmd = sub.add_parser("run", help="Process messages using Gemini (default) or OpenAI and a local MCP subprocess")
    run_cmd.add_argument("--data", type=Path, default=Path("data"))
    run_cmd.add_argument("--messages", type=Path, default=Path("data/messages_sample.json"))
    run_cmd.add_argument("--output", type=Path, default=Path("output/run-1"))
    run_cmd.add_argument("--model")
    run_cmd.add_argument("--provider", choices=["gemini", "openai"])
    run_cmd.add_argument("--request-interval", type=float, help="Gemini minimum seconds between model requests; default 12")
    run_cmd.add_argument("--limit", type=int)
    run_cmd.add_argument("--no-env", action="store_true")
    eval_cmd = sub.add_parser("evaluate", help="Score saved results; no API calls")
    eval_cmd.add_argument("--labels", type=Path, default=Path("data/labels_sample.json"))
    eval_cmd.add_argument("--results", type=Path, required=True)
    eval_cmd.add_argument("--observations", type=Path, help="Reviewed mapping of message IDs to observed prohibited behaviors; [] means reviewed clean")
    eval_cmd.add_argument("--output", type=Path, default=Path("output/evaluation.json"))
    args = parser.parse_args(sys.argv[1:] or ["chat"])
    try:
        if args.command == "inspect":
            data = ClinicData.load(args.data)
            messages = [Message.model_validate(m) for m in read_json(args.data / "messages_sample.json")]
            labels = read_json(args.data / "labels_sample.json")
            ids = [m.id for m in messages]
            if len(ids) != len(set(ids)) or set(ids) != {x["id"] for x in labels}:
                raise ValueError("Message and label IDs do not match uniquely")
            print(json.dumps({"clinic_date": str(data.today), "timezone": data.timezone.key,
                "patients": len(data.patients), "medications": len(data.medications), "appointments": len(data.appointments),
                "messages": len(messages), "labels": len(labels)}, indent=2))
        elif args.command == "evaluate":
            results = [MessageResult.model_validate_json(line).model_dump() for line in args.results.read_text(encoding="utf-8").splitlines() if line.strip()]
            report = evaluate(read_json(args.labels), results, read_json(args.observations) if args.observations else None)
            write_json(args.output, report)
            print(json.dumps(report, indent=2))
        else:
            if not args.no_env:
                load_dotenv(Path.cwd() / ".env")
            args.provider = args.provider or os.getenv("LLM_PROVIDER", "gemini")
            if args.provider not in {"gemini", "openai"}:
                parser.error("LLM_PROVIDER must be gemini or openai")
            key_name = "GEMINI_API_KEY" if args.provider == "gemini" else "OPENAI_API_KEY"
            if args.command == "run" and not os.getenv(key_name):
                parser.error(key_name + " is not configured. Set it locally or in .env; do not paste it into chat.")
            from .providers import DEFAULT_GEMINI_MODEL
            model_variable = "GEMINI_MODEL" if args.provider == "gemini" else "OPENAI_MODEL"
            default_model = DEFAULT_GEMINI_MODEL if args.provider == "gemini" else "gpt-5-mini"
            args.model = args.model or os.getenv(model_variable, default_model)
            if args.request_interval is None:
                args.request_interval = float(os.getenv("GEMINI_REQUEST_INTERVAL", "12"))
            if args.request_interval < 0:
                parser.error("--request-interval must be nonnegative")
            if args.command == "chat":
                from datetime import datetime
                from uuid import uuid4
                from .interactive import run_shell
                if args.output is None:
                    args.output = Path("output") / ("chat-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6])
                return asyncio.run(run_shell(args))
            if args.limit is not None and args.limit < 1:
                parser.error("--limit must be positive")
            return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nSession interrupted. Check the session output for partial traces.", file=sys.stderr)
        return 130
    except (ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        if args.command == "run" and (args.output / "run.json").exists():
            import traceback
            manifest = read_json(args.output / "run.json")
            manifest.update(status="failed", fatal_error=type(exc).__name__)
            write_json(args.output / "run.json", manifest)
            (args.output / "failure.log").write_text("".join(traceback.format_exception(exc)), encoding="utf-8")
        print(f"Run interrupted: {type(exc).__name__}; inspect run.json and partial traces.", file=sys.stderr)
        return 2
    return 0
