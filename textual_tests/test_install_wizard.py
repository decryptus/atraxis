"""Drive the real screens and preparation engine with a disposable HTTPS source."""
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest

from textual.widgets import Input, Static
from atraxis.installation.engine import Engine
from atraxis.installation.filesystem import DIRECTORY_FLAGS, Root
from atraxis.installation.validation import parse
from atraxis.installation.wizard import InstallWizard

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))
from test_installation import LOGIN, https_fixture, material, monit, plan, policy


class WizardTests(unittest.IsolatedAsyncioTestCase):
    async def test_keyboard_validation_review_back_and_confirm_real_preparation(self):
        with tempfile.TemporaryDirectory() as directory, https_fixture(payload=monit) as (url, ca, seen):
            root = Root(os.open(directory, DIRECTORY_FLAGS))
            self.addCleanup(root.close)
            document = plan(url)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                document['listen_port'] = sock.getsockname()[1]
            cert, key, _ = material()
            app = InstallWizard(Engine(policy(url), root), parse(document), cert, key, {'monit': ca})
            async with app.run_test(size=(100, 32)) as pilot:
                await pilot.click('#advanced')
                field = app.query_one('#port', Input)
                current = field.value
                field.value = '65536'
                app.query_one('#next').focus()
                await pilot.press('enter')
                self.assertEqual(app.step, 0)
                self.assertIn('invalid port', str(app.query_one('#error', Static).content))
                field.value = current
                await pilot.pause(0.4)
                await pilot.press('enter')
                self.assertEqual(app.step, 1)
                for field, value in [('login', LOGIN['username']), ('password', LOGIN['password']),
                                     ('user-0', LOGIN['username']), ('secret-0', LOGIN['password'])]:
                    app.query_one('#' + field, Input).value = value
                await pilot.click('#next')
                self.assertEqual(app.step, 2)
                self.assertNotIn(LOGIN['password'], str(app.query_one('#review', Static).content))
                await pilot.click('#back')
                self.assertEqual(app.query_one('#secret-0', Input).value, LOGIN['password'])
                await pilot.pause(0.4)
                await pilot.click('#next')
                self.assertEqual(app.step, 2)
                self.assertEqual(seen, [])
                self.assertEqual(list(Path(directory).iterdir()), [])
                await pilot.pause(0.4)
                await pilot.click('#next')
                await pilot.pause()
                self.assertTrue(app.prepared, (app.step, app.busy, app.query_one("#error", Static).content))
                self.assertTrue((Path(directory) / document['name'] / 'complete.json').is_file())
                self.assertEqual(len(seen), 1)
