import asyncio
from io import StringIO
import json
from pathlib import Path
from types import SimpleNamespace

from rich.console import Console

from clinic_assist.interactive import Shell, RunStore
from clinic_assist.contracts import Message

ROOT = Path(__file__).resolve().parents[1]


class Backend:
    def __init__(self):
        self.calls = []

    async def process(self, message, on_event):
        self.calls.append(message)
        on_event({"name": "find_patient", "host": False, "ok": True})
        return {"result": {"id": message["id"], "patient_id": None, "urgency": "routine",
            "intents": ["admin"], "actions": ["ask_verification"], "routed_to": [],
            "tool_calls": ["find_patient"], "reply_draft": "Please confirm your date of birth."},
            "usage": {"input_tokens": 12, "output_tokens": 8}, "error": None, "trace": []}


def make_shell(tmp_path, answers=(), backend=None):
    stream = StringIO()
    iterator = iter(answers)

    async def read(prompt):
        return next(iterator)

    shell = Shell(data=ROOT / "data", output=tmp_path / "session", provider="gemini",
        model="test-model", backend=backend or Backend(),
        console=Console(file=stream, width=90, color_system=None), read=read)
    return shell, stream


def test_browse_samples_then_run_original_message_without_labels(tmp_path):
    async def scenario():
        shell, stream = make_shell(tmp_path)
        await shell.dispatch('/inbox')
        await shell.dispatch('/open M001')
        assert not shell.backend.calls
        await shell.dispatch('/run M001')
        expected = Message.model_validate(json.loads((ROOT / 'data/messages_sample.json').read_text())[0]).model_dump(mode='json')
        assert shell.backend.calls == [expected]
        assert 'Please confirm your date of birth.' in stream.getvalue()
        assert shell.last['result']['id'] == 'M001'
        assert (tmp_path / 'session/results.jsonl').exists()
        assert 'expected_actions' not in stream.getvalue()
    asyncio.run(scenario())


def test_plain_text_collects_sender_and_reuses_it_with_unique_ids(tmp_path):
    async def scenario():
        shell, _ = make_shell(tmp_path, ['Someone [red]', 'unknown', ''])
        await shell.dispatch('What time do you open?')
        await shell.dispatch('Are you open Monday?')
        first, second = shell.backend.calls
        assert first['from_name'] == 'Someone [red]'
        assert first['from_contact'] == 'unknown'
        assert first['stated_dob'] is None
        assert first['body'] == 'What time do you open?'
        assert first['id'] != second['id']
        assert first['received_at'].startswith('2026-11-09')
        assert shell.store.tokens == {'input_tokens': 24, 'output_tokens': 16}
    asyncio.run(scenario())


def test_unknown_ids_and_commands_never_process_a_message(tmp_path):
    async def scenario():
        shell, stream = make_shell(tmp_path)
        for command in ['/run M999', '/open M999', '/delete everything']:
            await shell.dispatch(command)
        assert not shell.backend.calls
        assert 'Unknown' in stream.getvalue()
        assert not (tmp_path / 'session').exists()
    asyncio.run(scenario())


def test_provider_failure_is_shown_without_claiming_success_and_can_return_to_prompt(tmp_path):
    class Broken:
        async def process(self, message, on_event):
            raise ValueError('GEMINI_API_KEY is not configured')

    async def scenario():
        shell, stream = make_shell(tmp_path, backend=Broken())
        await shell.dispatch('/run M001')
        assert shell.last is None
        assert 'GEMINI_API_KEY is not configured' in stream.getvalue()
        assert not (tmp_path / 'session/results.jsonl').exists()
        assert await shell.dispatch('/quit') is False
    asyncio.run(scenario())


def test_store_retains_partial_failure_and_never_overwrites_existing_session(tmp_path):
    store = RunStore(tmp_path / 'session', 'gemini', 'test-model')
    store.interrupted('M001', [{'name': 'prepare_refill_for_approval', 'ok': True}],
        {'input_tokens': 10, 'output_tokens': 5}, 'Connection lost')
    manifest = json.loads((tmp_path / 'session/run.json').read_text())
    assert manifest['status'] == 'interrupted'
    assert manifest['unprocessed_ids'] == ['M001']
    assert manifest['tokens']['input_tokens'] == 10
    import pytest
    with pytest.raises(FileExistsError):
        RunStore(tmp_path / 'session', 'gemini', 'test-model').start()


def test_no_argument_launch_browses_and_exits_without_key_or_output(tmp_path):
    import os
    import subprocess
    import sys
    environment = os.environ.copy()
    environment.pop('GEMINI_API_KEY', None)
    result = subprocess.run([sys.executable, '-m', 'clinic_assist', 'chat', '--no-env', '--output', str(tmp_path/'unused')],
        input='/help\n/open M001\n/quit\n', capture_output=True, text=True, cwd=ROOT, env=environment, timeout=30)
    assert result.returncode == 0, result.stderr
    assert 'CLINIC ASSIST' in result.stdout
    assert 'Robert Chen' in result.stdout
    assert not (tmp_path/'unused').exists()


def test_real_mcp_backend_processes_and_closes_with_live_tool_events(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager
    from clinic_assist.interactive import Backend as RealBackend

    async def create(**kwargs):
        call = SimpleNamespace(type='function_call', name='triage_message',
            arguments=json.dumps({'urgency': 'emergency', 'intents': ['clinical']}),
            model_dump=lambda **_: {'type':'function_call'}, call_id='demo-call')
        return SimpleNamespace(output=[call], usage=None)

    @asynccontextmanager
    async def fake_client(*args, **kwargs):
        yield SimpleNamespace(responses=SimpleNamespace(create=create))

    monkeypatch.setenv('GEMINI_API_KEY', 'offline-test-not-sent')
    monkeypatch.setattr('clinic_assist.providers.model_client', fake_client)

    async def scenario():
        shell, _ = make_shell(tmp_path)
        shell.backend = RealBackend(ROOT/'data', 'gemini', 'test', 0, shell.store)
        events = []
        message = shell.new_message('Chest pain', {'from_name':'Robert Chen','from_contact':'555-0102','stated_dob':None})
        try:
            outcome = await shell.backend.process(message, events.append)
            shell.store.record(outcome)
            assert outcome['error'] is None
            assert outcome['result']['urgency'] == 'emergency'
            assert outcome['result']['routed_to'] == ['clinician_urgent']
            assert any(e['name']=='triage_message' and e['ok'] for e in events)
            # One live server is retained throughout the session.
            agent = shell.backend.agent
            await shell.backend.connect()
            assert shell.backend.agent is agent
        finally:
            await shell.backend.close()
    asyncio.run(scenario())


def test_untrusted_messages_cannot_emit_terminal_escape_sequences(tmp_path):
    async def scenario():
        shell, stream = make_shell(tmp_path)
        payload = 'Keep [red]literal[/red] text\x1b]52;c;YWJj\x1b\\\x1b[2J'
        shell.samples['M001']['body'] = payload
        await shell.dispatch('/open M001')
        await shell.dispatch('/run M001')
        assert shell.backend.calls[0]['body'] == payload
        output = stream.getvalue()
        assert '[red]literal[/red]' in output
        assert '\x1b' not in output
        assert '52;c;YWJj' not in output
    asyncio.run(scenario())


def test_storage_failure_keeps_result_and_blocks_further_mutations(tmp_path, monkeypatch):
    async def scenario():
        shell, stream = make_shell(tmp_path)

        def broken_record(outcome):
            raise OSError('Disk full')

        monkeypatch.setattr(shell.store, 'record', broken_record)
        await shell.dispatch('/run M001')
        assert shell.last is not None
        await shell.dispatch('/run M001')
        await shell.dispatch('/run M002')
        assert len(shell.backend.calls) == 1
        await shell.dispatch('/trace')
        assert 'Disk full' in stream.getvalue()
        assert 'Saved:' not in stream.getvalue()
    asyncio.run(scenario())


def test_prompt_completes_commands_and_supports_multiline_input():
    from clinic_assist.interactive import make_prompt
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    async def scenario():
        with create_pipe_input() as pipe:
            prompt = make_prompt(input=pipe, output=DummyOutput())
            pending = asyncio.create_task(prompt.prompt_async('clinic > '))
            pipe.send_text('/sta')
            await asyncio.sleep(0.1)
            pipe.send_text('\t')
            await asyncio.sleep(0.1)
            pipe.send_text('\r')
            assert await asyncio.wait_for(pending, 3) == '/status'
            pending = asyncio.create_task(prompt.prompt_async('clinic > '))
            pipe.send_text('first\x1b\rsecond\r')
            assert await asyncio.wait_for(pending, 3) == 'first\nsecond'
    asyncio.run(scenario())


def test_interrupted_processing_preserves_trace_and_disables_the_connection(tmp_path, monkeypatch):
    from clinic_assist.interactive import Backend as RealBackend

    class InterruptedAgent:
        trace = [{'name': 'prepare_refill_for_approval', 'ok': True}]
        usage = {'input_tokens': 10, 'output_tokens': 5}

        async def process(self, message):
            raise asyncio.CancelledError()

    async def scenario():
        shell, _ = make_shell(tmp_path)
        backend = RealBackend(ROOT/'data', 'gemini', 'test', 0, shell.store)
        backend.agent = InterruptedAgent()
        import pytest
        with pytest.raises(asyncio.CancelledError):
            await backend.process(shell.samples['M001'], lambda event: None)
        assert backend.broken
        manifest = json.loads((tmp_path/'session/run.json').read_text())
        assert manifest['unprocessed_ids'] == ['M001']
        assert 'prepare_refill_for_approval' in (tmp_path/'session/traces.jsonl').read_text()
        with pytest.raises(RuntimeError, match='Restart'):
            await backend.connect()
    asyncio.run(scenario())
