import curses
import unittest
from unittest.mock import Mock, patch

from atraxis.rule_tui import RuleWorkbench, draw, safe_text


class RuleTerminalTests(unittest.TestCase):
    def setUp(self):
        self.operator=Mock()
        self.app=self.operator.application
        self.app.history.return_value=[{'revision':'first','author':'alice'},{'revision':'second','author':'bob'}]
        self.app.active.return_value={'revision':'first','generation':'g1'}
        self.app.read.return_value={'context':{'rule_kind':'intervention','targets':[['container','web']]}}
        self.model=RuleWorkbench(self.operator,'cpu','cases.json')
        self.model.refresh()
        self.model.reports['second']={'id':'test2','passed':True}
        self.model.index=1

    def test_activation_requires_visible_review_and_exact_confirmation(self):
        self.model.handle(ord('a'))
        self.app.activate.assert_not_called()
        self.assertTrue(any('web' in line for line in self.model.detail))
        self.assertEqual(self.model.pending['expected_generation'],'g1')
        self.model.handle(10)
        self.app.activate.assert_called_once_with('cpu','second',test_id='test2',expected_generation='g1')

    def test_escape_cancels_and_quit_does_not_activate(self):
        self.model.handle(ord('a'))
        self.model.handle(27)
        self.model.handle(10)
        self.assertFalse(self.model.handle(ord('q')))
        self.app.activate.assert_not_called()

    def test_failed_or_untested_revision_cannot_prepare(self):
        self.model.reports['second']['passed']=False
        with self.assertRaises(ValueError):
            self.model.handle(ord('a'))
        self.app.activate.assert_not_called()

    def test_failed_mutation_is_not_retried_by_enter(self):
        self.model.handle(ord('a'))
        self.app.activate.side_effect=ValueError('conflict')
        with self.assertRaises(ValueError):
            self.model.handle(10)
        self.model.handle(10)
        self.assertEqual(self.app.activate.call_count,1)

    def test_deactivation_reviews_active_not_selected_draft(self):
        self.model.handle(ord('d'))
        self.assertEqual(self.model.pending['revision'],'first')
        self.app.read.assert_called_with('cpu','first')
        self.model.handle(10)
        self.app.deactivate.assert_called_once_with('cpu',expected_generation='g1')

    def test_resize_and_terminal_controls_do_not_execute_or_crash(self):
        screen=Mock()
        screen.getmaxyx.return_value=(2,1)
        draw(screen,self.model)
        screen.getmaxyx.return_value=(24,100)
        draw(screen,self.model)
        self.assertNotIn('\x1b',safe_text('name\x1b[31m'))
        self.app.activate.assert_not_called()

    def test_review_scope_can_be_scrolled(self):
        self.app.read.return_value['context']['targets']=[['container','target'+str(i)] for i in range(30)]
        self.model.handle(ord('a'))
        self.model.handle(curses.KEY_RIGHT)
        self.assertEqual(self.model.detail_offset,5)
        self.assertIsNotNone(self.model.pending)
        self.app.activate.assert_not_called()

    def test_retirement_reviews_head_and_consumes_confirmation_once(self):
        self.app.draft.return_value={'revision':'c'*32}
        self.app.availability.return_value={'status':'available','generation':None}
        self.model.prepare('archive')
        self.app.archive.assert_not_called()
        self.assertEqual(self.model.pending['revision'],'c'*32)
        self.model.confirm();self.model.confirm()
        self.app.archive.assert_called_once_with('cpu',expected_revision='c'*32,expected_generation=None)

    def test_delete_keeps_audit_visible_and_does_not_refresh_removed_draft(self):
        self.app.draft.return_value={'revision':'c'*32}
        self.app.availability.return_value={'status':'available','generation':None}
        self.app.audit.return_value=[{'operation':'delete'}]
        self.model.prepare('delete');self.app.active.reset_mock()
        self.model.confirm()
        self.assertEqual(self.model.rows,[])
        self.app.active.assert_not_called()
        self.assertIn('Audit preserved',self.model.message)

    def test_cli_retirement_requires_confirmation_and_exact_availability_generation(self):
        import argparse
        from contextlib import redirect_stderr
        import io
        from atraxis.rule_commands import add_parser, run_command
        parser=argparse.ArgumentParser();add_parser(parser.add_subparsers(dest='command'))
        arguments=['rules','--operator-config','/private/operator.yml','archive','cpu',
                   '--expected-revision','c'*32,'--expected-generation','none']
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):parser.parse_args(arguments)
        options=parser.parse_args(arguments+['--confirm'])
        run_command(options,self.operator)
        self.app.archive.assert_called_once_with('cpu',expected_revision='c'*32,expected_generation=None)
