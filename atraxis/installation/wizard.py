"""Interactive, host-confined installation preparation over a local terminal or SSH."""
import asyncio
from multiprocessing import resource_tracker

from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll, Vertical
from textual.widgets import Button, Checkbox, Input, Label, Static

from atraxis.installation.engine import serialize
from atraxis.installation.validation import InstallError, credentials, require


class InstallWizard(App):
    TITLE = 'Atraxis installation'
    CSS = """
    Screen { padding: 1 2; }
    #heading { color: $accent; text-style: bold; height: 2; }
    Vertical { height: auto; }
    Input { margin-bottom: 1; }
    #error { color: $error; height: auto; }
    #actions { height: 3; }
    Button { margin-right: 1; min-width: 10; }
    """
    steps = ('Site and access', 'Sources and credentials', 'Review preparation', 'Result')

    def __init__(self, engine, plan, certificate, private_key, cas):
        # Spawned TLS/probe workers need the POSIX tracker. Start it before
        # Textual replaces stderr with a capture object without a real FD.
        resource_tracker.ensure_running()
        super().__init__()
        self.engine, self.template = engine, plan
        self.certificate, self.private_key, self.cas = certificate, private_key, cas
        self.step = 0
        self.prepared = False
        self.busy = False

    def compose(self) -> ComposeResult:
        yield Label(id='heading')
        with VerticalScroll():
            with Vertical(id='page0'):
                yield Label('Installation name')
                yield Input(self.template.name, id='name', max_length=48)
                yield Label('Server name on the TLS certificate')
                yield Input(self.template.server_name, id='host', max_length=253)
                yield Checkbox('Advanced network settings', id='advanced')
                with Vertical(id='network'):
                    yield Label('Listen address')
                    yield Input(self.template.listen_address, id='address', max_length=45)
                    yield Label('Listen port')
                    yield Input(str(self.template.listen_port), id='port', max_length=5)
            with Vertical(id='page1'):
                yield Label('Installation login')
                yield Input(placeholder='Username', id='login', max_length=128)
                yield Input(placeholder='Password (16 or more characters)', password=True,
                            id='password', max_length=4096)
                for i, source in enumerate(self.template.sources):
                    yield Checkbox(f'{source.kind} · {source.origin}', value=True, id=f'use-{i}')
                    yield Input(placeholder=f'{source.source_id} username', id=f'user-{i}', max_length=128)
                    yield Input(placeholder=f'{source.source_id} password', password=True,
                                id=f'secret-{i}', max_length=4096)
            with Vertical(id='page2'):
                yield Static(id='review', markup=False)
            with Vertical(id='page3'):
                yield Static(id='result', markup=False)
        yield Static(id='error', markup=False)
        with Horizontal(id='actions'):
            yield Button('Back', id='back')
            yield Button('Next', id='next', variant='primary')

    def on_mount(self):
        self.query_one('#network').display = False
        self.show_step()

    def show_step(self):
        for index in range(len(self.steps)):
            self.query_one(f'#page{index}').display = index == self.step
        self.query_one('#heading', Label).update(f'ATRAXIS   {self.step + 1}/4   {self.steps[self.step]}')
        self.query_one('#back').display = self.step in (1, 2)
        self.query_one('#next', Button).label = ('Next', 'Review', 'Prepare', 'Close')[self.step]
        self.query_one('#error', Static).update('')
        self.query_one(('#name', '#login', '#back', '#next')[self.step]).focus()

    def on_checkbox_changed(self, event: Checkbox.Changed):
        if event.checkbox.id == 'advanced':
            self.query_one('#network').display = event.value
        elif event.checkbox.id.startswith('use-'):
            index = event.checkbox.id.split('-')[1]
            for prefix in ('user', 'secret'):
                self.query_one(f'#{prefix}-{index}').display = event.value

    def value(self, field):
        return self.query_one('#' + field, Input).value

    def selected(self):
        return [(i, s) for i, s in enumerate(self.template.sources)
                if self.query_one(f'#use-{i}', Checkbox).value]

    def plan_bytes(self, *, all_sources=False):
        number = self.value('port')
        require(number.isascii() and number.isdecimal(), 'invalid_port', 'listen_port')
        data = self.template.document()
        data.update(name=self.value('name'), server_name=self.value('host'),
                    listen_address=self.value('address'), listen_port=int(number))
        if not all_sources:
            require(self.selected(), 'invalid_plan', 'sources')
            data['sources'] = [data['sources'][i] for i, _ in self.selected()]
        return serialize(data)

    def inputs(self):
        login = credentials(dict(username=self.value('login'), password=self.value('password')), login=True)
        auth = {s.source_id: credentials(dict(username=self.value(f'user-{i}'), password=self.value(f'secret-{i}')))
                for i, s in self.selected()}
        return dict(login=login, source_credentials=auth, certificate=self.certificate,
                    private_key=self.private_key, source_cas={k: self.cas[k] for k in auth})

    async def on_button_pressed(self, event: Button.Pressed):
        if self.busy:
            return
        if event.button.id == 'back':
            self.step -= 1
        elif self.step == 3:
            self.exit(self.prepared)
            return
        else:
            try:
                if self.step == 0:
                    self.engine.preview(self.plan_bytes(all_sources=True))
                elif self.step == 1:
                    plan = self.engine.preview(self.plan_bytes())['plan']
                    self.inputs()
                    rows = [f"{s['source_id']}   {s['kind']}   {s['origin']}" for s in plan['sources']]
                    self.query_one('#review', Static).update('\n'.join([
                        f"Installation   {plan['name']}", f"Server   {plan['server_name']}",
                        f"Listener   {plan['listen_address']}:{plan['listen_port']}", '', *rows, '',
                        'Prepare verifies TLS and source logins, then writes a new private bundle.',
                        'Services are started separately with Docker Compose.']))
                else:
                    self.busy = True
                    self.query_one('#actions').disabled = True
                    self.query_one('#error', Static).update('Checking TLS, listener and sources…')
                    try:
                        await asyncio.to_thread(self.engine.prepare, self.plan_bytes(), **self.inputs())
                        self.prepared = True
                        self.query_one('#result', Static).update('Bundle prepared. Services have not been started.')
                    finally:
                        self.busy = False
                        self.query_one('#actions').disabled = False
            except InstallError as exc:
                self.query_one('#error', Static).update(f'{exc.field}: {exc.code.replace("_", " ")}')
                if exc.code in ('write_failed', 'publication_uncertain'):
                    self.step = 3
                    self.show_step()
                    self.query_one('#result', Static).update('Inspect the destination before retrying. ' + exc.code)
                return
            self.step += 1
        self.show_step()
