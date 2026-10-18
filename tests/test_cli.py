import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_inspect_data_without_credentials():
    run = subprocess.run([sys.executable, "-m", "clinic_assist", "inspect"], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)["messages"] == 40


def test_live_run_without_key_fails_before_creating_fake_results(tmp_path):
    env = os.environ.copy()
    env.pop("OPENAI_API_KEY", None)
    run = subprocess.run([sys.executable, "-m", "clinic_assist", "run", "--provider", "openai", "--no-env", "--output", str(tmp_path / "run")],
                         cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
    assert run.returncode != 0
    assert "OPENAI_API_KEY" in run.stderr
    assert not (tmp_path / "run" / "results.jsonl").exists()


def test_evaluate_command_reports_missing_results(tmp_path):
    source = tmp_path / "results.jsonl"
    source.write_text("")
    target = tmp_path / "evaluation.json"
    run = subprocess.run([sys.executable, "-m", "clinic_assist", "evaluate", "--results", str(source), "--output", str(target)],
                         cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    report = json.loads(target.read_text())
    assert len(report["missing_results"]) == 40
    assert report["safety_review_complete"] is False


def test_transport_failure_preserves_explicit_failed_and_unprocessed_ids(tmp_path):
    from argparse import Namespace
    import asyncio
    from unittest.mock import patch
    from clinic_assist.cli import run

    output = tmp_path / "interrupted"
    args = Namespace(messages=ROOT / "data/messages_sample.json", data=ROOT / "data", output=output, limit=2, model="test-model", provider="openai", request_interval=0)

    async def broken_process(self, message):
        raise RuntimeError("MCP connection closed")

    async def scenario():
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key-no-requests"}), patch("clinic_assist.agent.Agent.process", broken_process):
            return await run(args)

    assert asyncio.run(scenario()) == 2
    manifest = json.loads((output / "run.json").read_text())
    assert manifest["status"] == "failed"
    assert manifest["errors"][0]["id"] == "M001"
    assert manifest["unprocessed_ids"] == ["M001", "M002"]
    assert manifest["usage_complete"] is False
    assert "request_events" in json.loads((output / "traces.jsonl").read_text())
    assert (output / "results.jsonl").read_text() == ""


def test_default_gemini_requires_its_own_key_even_if_openai_key_exists(tmp_path):
    env = os.environ.copy()
    env.pop("GEMINI_API_KEY", None)
    env["OPENAI_API_KEY"] = "other-provider-key-not-to-use"
    run = subprocess.run([sys.executable, "-m", "clinic_assist", "run", "--no-env", "--output", str(tmp_path / "unused")],
                         cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
    assert run.returncode != 0
    assert "GEMINI_API_KEY" in run.stderr
    assert not (tmp_path / "unused").exists()


@pytest.mark.parametrize("status", [400, 402, None])
def test_gemini_access_failure_stops_before_second_message(tmp_path, status):
    from argparse import Namespace
    import asyncio
    from unittest.mock import patch
    from clinic_assist.cli import run

    calls = []
    output = tmp_path / "access-failure"
    args = Namespace(messages=ROOT / "data/messages_sample.json", data=ROOT / "data", output=output,
                     limit=2, model="gemini-test", provider="gemini", request_interval=0)

    async def failed_process(self, message):
        calls.append(message["id"])
        return {"result": {"id":message["id"]}, "trace":[], "audit":{}, "usage":{"input_tokens":0,"output_tokens":0},
                "error":"Provider access failed", "provider_error_status":status, "provider_unavailable": status is None}

    async def scenario():
        with patch.dict(os.environ, {"GEMINI_API_KEY":"test-not-sent"}), patch("clinic_assist.agent.Agent.process", failed_process):
            return await run(args)

    assert asyncio.run(scenario()) == 2
    assert calls == ["M001"]
    manifest = json.loads((output / "run.json").read_text())
    assert manifest["unprocessed_ids"] == ["M002"]
