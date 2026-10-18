"""Interactive clinic inbox workspace; all operations still go through Agent/MCP."""
import asyncio
from contextlib import AsyncExitStack
from datetime import datetime, timedelta
import json
import os
import re
from pathlib import Path
import sys
from uuid import uuid4

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .contracts import Message
from .data import ClinicData


COMMANDS = {
    '/help': 'Show commands', '/inbox': 'Browse the sample inbox',
    '/open': 'Read a message: /open M001', '/run': 'Process a message: /run M001',
    '/sender': 'Set sender details for typed messages', '/status': 'Show session status',
    '/last': 'Show the last result', '/trace': 'Show the last tool activity',
    '/clear': 'Clear the display', '/quit': 'Close the session',
}


def sanitize(value):
    # Strip terminal commands, not message content sent to the policy engine.
    value = str(value)
    value = re.sub(r'(?:\x1b\]|\x9d).*?(?:\x07|\x1b\\|\x9c|$)', '', value, flags=re.S)
    value = re.sub(r'(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]', '', value)
    value = re.sub(r'\x1b[ -/]*[@-Z\\-_]', '', value)
    return re.sub(r'[\x00-\x08\x0b-\x1f\x7f-\x9f]', '', value)


def safe_text(value='', *args, **kwargs):
    return Text(sanitize(value), *args, **kwargs)


def save_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


class RunStore:
    def __init__(self, path, provider, model):
        self.path, self.provider, self.model = Path(path), provider, model
        self.started = False
        self.ids = []
        self.errors = []
        self.tokens = {'input_tokens': 0, 'output_tokens': 0}
        self.fallback_models = [name.strip() for name in os.getenv('GEMINI_FALLBACK_MODELS', '').split(',') if name.strip()] if provider == 'gemini' else []
        self.models_attempted, self.models_used = [], []

    def start(self):
        if not self.started:
            self.path.mkdir(parents=True, exist_ok=False)
            self.started = True
            self.manifest('ready')

    def manifest(self, status, unprocessed=()):
        save_json(self.path / 'run.json', {'status': status, 'provider': self.provider,
            'model': self.model, 'processed_ids': self.ids, 'unprocessed_ids': list(unprocessed),
            'fallback_models': self.fallback_models, 'models_attempted': self.models_attempted, 'models_used': self.models_used,
            'tokens': self.tokens, 'errors': self.errors,
            'state_model': 'Shared sequential MCP session; fresh data on restart'})

    def append(self, name, value):
        with (self.path / name).open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(value, ensure_ascii=False) + '\n')

    def add_usage(self, usage):
        for key in self.tokens:
            self.tokens[key] += usage.get(key, 0)

    def record(self, outcome):
        self.start()
        self.append('results.jsonl', outcome['result'])
        self.append('traces.jsonl', outcome)
        self.ids.append(outcome['result']['id'])
        self.add_usage(outcome['usage'])
        self.record_requests(outcome.get('request_events', []))
        if outcome['error']:
            self.errors.append({'id': outcome['result']['id'], 'error': outcome['error']})
        self.manifest('failed' if self.errors else 'completed')

    def record_requests(self, events):
        for event in events:
            if event['model'] not in self.models_attempted:
                self.models_attempted.append(event['model'])
            if event['status'] == 'completed' and event['model'] not in self.models_used:
                self.models_used.append(event['model'])

    def interrupted(self, message_id, trace, usage, error, request_events=()):
        self.start()
        self.append('traces.jsonl', {'message_id': message_id, 'trace': trace, 'error': error, 'request_events': list(request_events)})
        self.record_requests(request_events)
        self.add_usage(usage)
        self.errors.append({'id': message_id, 'error': error})
        self.manifest('interrupted', [message_id])


class Backend:
    """Lazy connection permits local inbox browsing without any credentials."""
    def __init__(self, data, provider, model, interval, store):
        self.data, self.provider, self.model, self.interval = data, provider, model, interval
        self.store = store
        self.stack = AsyncExitStack()
        self.agent = None
        self.broken = False

    async def connect(self):
        if self.broken:
            raise RuntimeError('Connection interrupted. Restart the app before processing more messages.')
        if self.agent is not None:
            return
        key_name = 'GEMINI_API_KEY' if self.provider == 'gemini' else 'OPENAI_API_KEY'
        if not os.getenv(key_name):
            raise ValueError(f'{key_name} is not configured. Add it to .env locally and restart.')
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from .agent import Agent
        from .providers import model_client

        class VisibleAgent(Agent):
            on_event = None

            async def _call(self, name, args, host=False):
                try:
                    return await super()._call(name, args, host)
                finally:
                    if self.on_event and self.trace and self.trace[-1]['name'] == name:
                        self.on_event(self.trace[-1])

        self.store.start()
        try:
            client = await self.stack.enter_async_context(model_client(
                self.provider, os.environ[key_name], min_interval=self.interval))
            log = self.stack.enter_context((self.store.path / 'mcp.log').open('w', encoding='utf-8'))
            params = StdioServerParameters(command=sys.executable,
                args=['-m', 'clinic_assist.server', '--data', str(Path(self.data).resolve())])
            read, write = await self.stack.enter_async_context(stdio_client(params, errlog=log))
            session = await self.stack.enter_async_context(ClientSession(
                read, write, read_timeout_seconds=timedelta(seconds=30)))
            await session.initialize()
            self.agent = VisibleAgent(session, client, self.model)
        except BaseException:
            self.broken = True
            raise

    async def process(self, message, on_event):
        await self.connect()
        self.agent.on_event = on_event
        try:
            return await self.agent.process(message)
        except BaseException as exc:
            self.broken = True
            self.store.interrupted(message['id'], self.agent.trace, self.agent.usage, type(exc).__name__,
                request_events=getattr(self.agent, 'request_events', []))
            raise

    async def close(self):
        await self.stack.aclose()


class Shell:
    def __init__(self, *, data, output, provider, model, backend=None, console=None, read=None, interval=12):
        self.data = ClinicData.load(Path(data))
        self.samples = {item['id']: Message.model_validate(item).model_dump(mode='json')
            for item in json.loads((Path(data) / 'messages_sample.json').read_text(encoding='utf-8'))}
        self.console = console or Console(highlight=False)
        self.store = RunStore(output, provider, model)
        self.provider, self.model = provider, model
        self.backend = backend or Backend(data, provider, model, interval, self.store)
        self.read = read
        self.sender = None
        self.last = None
        self.unsaved = False
        self.consumed = set()

    def banner(self):
        body = safe_text('CLINIC ASSIST\n', style='bold cyan')
        body.append('Your clinic inbox, one message at a time.\n\n', style='white')
        body.append(sanitize(f'{self.provider} / {self.model}\n'), style='dim')
        body.append('Type a patient message, or /inbox to browse samples.\n', style='white')
        body.append('Tab completes commands  |  Alt+Enter adds a line  |  /help', style='dim')
        self.console.print(Panel(body, border_style='cyan', padding=(1, 2)))
        self.console.print(safe_text('Fictional clinic data. Replies are drafts; prescriptions require provider approval.', style='dim'))

    def status(self):
        key = 'GEMINI_API_KEY' if self.provider == 'gemini' else 'OPENAI_API_KEY'
        detail = (f'Provider  {self.provider}\nModel     {self.model}\n'
            f'Fallbacks {", ".join(self.store.fallback_models) or "disabled"}\n'
            f'API key   {"configured" if os.getenv(key) else "missing - browsing still available"}\n'
            f'Clock     {self.data.today} / {self.data.timezone.key}\n'
            f'Sender    {self.sender["from_name"] if self.sender else "not set"}\n'
            f'Processed {len(self.store.ids)} message(s)\n'
            f'Tokens    {self.store.tokens["input_tokens"]} in / {self.store.tokens["output_tokens"]} out\n'
            f'Saved to  {self.store.path}')
        self.console.print(Panel(safe_text(detail), title='Session', border_style='cyan'))

    async def set_sender(self):
        self.console.print(safe_text('Use the actual sender details. A name alone does not verify identity.', style='dim'))
        name = (await self.read('Sender name > ')).strip()
        contact = (await self.read('Phone or email > ')).strip()
        dob = (await self.read('Date of birth [YYYY-MM-DD, optional] > ')).strip()
        # Validate all fields before replacing the current sender.
        candidate = self.new_message('Sender setup', {'from_name': name, 'from_contact': contact,
            'stated_dob': dob or None})
        if not name or not contact:
            raise ValueError('Sender name and phone/email are required.')
        self.sender = {key: candidate[key] for key in ('from_name', 'from_contact', 'stated_dob')}
        self.console.print(safe_text(f'Sender set: {name}. Verification will be checked by the tools.', style='cyan'))

    def new_message(self, body, sender):
        received = datetime.combine(self.data.today, datetime.min.time().replace(hour=9), self.data.timezone)
        return Message.model_validate(dict(id='CHAT-' + uuid4().hex[:12], channel='sms', body=body,
            received_at=received.isoformat(), **sender)).model_dump(mode='json')

    def show_result(self, outcome):
        result = outcome['result']
        urgency = result['urgency']
        color = {'routine': 'cyan', 'urgent': 'yellow', 'emergency': 'red'}.get(urgency, 'white')
        self.console.print()
        self.console.print(Panel(safe_text(result['reply_draft']), title='Reply draft - not sent', border_style=color, padding=(1, 2)))
        self.console.print(safe_text(f'{result["id"]}  |  {urgency.upper()}  |  patient: {result["patient_id"] or "unverified"}', style=color))
        self.console.print(safe_text('Actions: ' + ', '.join(result['actions'])))
        if outcome.get('model'):
            self.console.print(safe_text('Model: ' + outcome['model'], style='dim'))
        if result['routed_to']:
            self.console.print(safe_text('Queues: ' + ', '.join(result['routed_to'])))
        if outcome['error']:
            self.console.print(safe_text('Processing failed; review the saved trace. ' + outcome['error'], style='bold yellow'))
        if self.unsaved:
            self.console.print(safe_text('Not saved. Result retained in memory; use /trace before closing.', style='bold yellow'))
        else:
            self.console.print(safe_text(f'Saved: {self.store.path}  |  /trace for tool details', style='dim'))

    async def process(self, message):
        if self.unsaved:
            self.console.print(safe_text('Saving failed. Further processing is blocked; inspect /last and /trace before closing.', style='bold yellow'))
            return
        if message['id'] in self.consumed:
            self.console.print(safe_text('Already processed in this session. Use /last or start a fresh session.', style='yellow'))
            return
        self.console.print(Panel(safe_text(message['body']), title=safe_text(f'Message / {message["from_name"]}'), border_style='bright_black'))

        def event(item):
            if not item.get('host'):
                mark = 'ok' if item.get('ok') else 'failed'
                self.console.print(safe_text(f'  {mark:6} {item["name"]}', style='dim' if mark == 'ok' else 'yellow'))

        with self.console.status('Connecting / waiting for model and tools...', spinner='dots'):
            outcome = await self.backend.process(message, event)
        self.last = outcome
        self.consumed.add(message['id'])
        try:
            self.store.record(outcome)
        except OSError as exc:
            self.unsaved = True
            self.show_result(outcome)
            self.console.print(safe_text(str(exc), style='bold yellow'))
            return
        self.show_result(outcome)

    async def dispatch(self, line):
        line = line.strip()
        if not line:
            return True
        try:
            if not line.startswith('/'):
                if self.sender is None:
                    await self.set_sender()
                await self.process(self.new_message(line, self.sender))
                return True
            command, _, argument = line.partition(' ')
            argument = argument.strip()
            if command in {'/quit', '/exit'}:
                return False
            if command == '/help':
                for key, description in COMMANDS.items():
                    self.console.print(safe_text(f'{key:10} {description}'))
                self.console.print(safe_text('Type ordinary text to process a new patient message. /sender changes its sender.', style='dim'))
            elif command == '/status':
                self.status()
            elif command == '/clear':
                self.console.clear()
                self.banner()
            elif command == '/sender':
                await self.set_sender()
            elif command == '/inbox':
                table = Table(box=None, padding=(0, 2), expand=True)
                table.add_column('ID', style='cyan', no_wrap=True)
                table.add_column('Sender')
                table.add_column('Message', ratio=3, overflow='ellipsis', no_wrap=True)
                for item in self.samples.values():
                    table.add_row(safe_text(item['id']), safe_text(item['from_name']), safe_text(item['body'].replace('\n', ' ')))
                self.console.print(table)
                self.console.print(safe_text('/open M001 to read  |  /run M001 to process through the model', style='dim'))
            elif command in {'/open', '/run'}:
                message = self.samples.get(argument.upper())
                if message is None:
                    self.console.print(safe_text('Unknown message ID. Use /inbox, then /open M001 or /run M001.', style='yellow'))
                elif command == '/open':
                    self.console.print(Panel(safe_text(message['body']), title=safe_text(message['id'] + ' / ' + message['from_name']), border_style='cyan'))
                else:
                    await self.process(message)
            elif command in {'/last', '/trace'}:
                if self.last is None:
                    self.console.print(safe_text('No result yet. Type a message or /run M001.', style='dim'))
                elif command == '/last':
                    self.show_result(self.last)
                else:
                    self.console.print_json(data=self.last['trace'])
            else:
                self.console.print(safe_text('Unknown command. Type /help or press Tab.', style='yellow'))
        except (ValueError, OSError, RuntimeError) as exc:
            self.console.print(safe_text(str(exc), style='bold yellow'))
        return True


def make_prompt(**kwargs):
    from prompt_toolkit import PromptSession
    from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.styles import Style
    bindings = KeyBindings()

    @bindings.add('escape', 'enter')
    def newline(event):
        event.current_buffer.insert_text('\n')

    # Keep patient message input in memory, not a plaintext history file.
    return PromptSession(history=InMemoryHistory(), auto_suggest=AutoSuggestFromHistory(),
        completer=WordCompleter(list(COMMANDS), sentence=True), complete_while_typing=True,
        key_bindings=bindings, style=Style.from_dict({'prompt': '#62daca bold'}), **kwargs)


async def run_shell(args):
    shell = Shell(data=args.data, output=args.output, provider=args.provider,
        model=args.model, interval=args.request_interval)
    prompt = make_prompt(bottom_toolbar=lambda: sanitize(
        f' {shell.provider} / {shell.model}  |  {len(shell.store.ids)} processed  |  /help  |  Ctrl+D exit '
    )) if sys.stdin.isatty() and sys.stdout.isatty() else None

    async def read(label):
        if prompt:
            return await prompt.prompt_async([('class:prompt', label)])
        return await asyncio.to_thread(input, label)

    shell.read = read
    shell.banner()
    try:
        while True:
            try:
                line = await read('\nclinic > ')
                if not await shell.dispatch(line):
                    break
            except KeyboardInterrupt:
                shell.console.print(safe_text('Input cancelled. /quit to exit.', style='dim'))
            except EOFError:
                break
    finally:
        await shell.backend.close()
    shell.console.print(safe_text('Session closed. Saved results remain in output/.', style='dim'))
    return 0
