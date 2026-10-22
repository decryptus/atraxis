"""Curses presentation of the shared rule application, with explicit review.

No engine or policy logic lives here. Same standard terminal layer as the other
project TUIs; no Auton-specific presentation or GPL code is copied.
"""
import curses
import json
from pathlib import Path
import sys

from atraxis.rule_commands import _target, read_json, read_text

PAGE_SIZE = 20
MIN_ROWS = 15
MIN_COLS = 76
MAX_PROMPT = 4096


def safe_text(value):
    return ''.join(character for character in str(value) if character.isprintable())


from atraxis.rule_workbench import RuleWorkbench as WorkbenchModel


class RuleWorkbench(WorkbenchModel):









    def handle(self, key):
        if key in (curses.KEY_LEFT, curses.KEY_RIGHT):
            self.detail_offset = max(0, min(max(0, len(self.detail)-1),
                self.detail_offset + (5 if key == curses.KEY_RIGHT else -5)))
            return True
        if key in (ord('q'), 27):
            if self.pending:
                self.pending = None
                self.detail = []
                self.message = 'Cancelled.'
                return True
            return key != ord('q')
        if self.pending:
            if key in (10, 13):
                self.confirm()
            return True
        if key == curses.KEY_UP:
            self.index = max(0, self.index - 1)
        elif key == curses.KEY_DOWN:
            self.index = min(max(0, len(self.rows) - 1), self.index + 1)
        elif key in (curses.KEY_NPAGE, curses.KEY_PPAGE):
            self.offset = max(0, self.offset + (PAGE_SIZE if key == curses.KEY_NPAGE else -PAGE_SIZE))
            self.refresh()
        elif key == ord('r'):
            self.refresh()
        elif key == ord('t'):
            self.test_selected()
        elif key in (ord('A'), ord('U'), ord('D')):
            self.prepare({ord('A'): 'archive', ord('U'): 'unarchive', ord('D'): 'delete'}[key])
        elif key in (ord('a'), ord('b'), ord('d')):
            self.prepare({ord('a'): 'activate', ord('b'): 'rollback', ord('d'): 'deactivate'}[key])
        elif key in (ord('v'), ord('h'), ord('c')):
            self.show({ord('v'): 'revision', ord('h'): 'audit', ord('c'): 'catalog'}[key])
        return True


def _line(screen, row, value, attribute=0):
    height, width = screen.getmaxyx()
    if 0 <= row < height - 1 and width > 2:
        screen.addnstr(row, 1, safe_text(value), max(0, width - 2), attribute)


def _prompt(screen, label):
    value = ''
    while True:
        height, width = screen.getmaxyx()
        screen.move(height - 2, 0)
        screen.clrtoeol()
        _line(screen, height - 2, label + value[-max(1, width-len(label)-4):])
        screen.refresh()
        key = screen.getch()
        if key == 27:
            return None
        if key in (10, 13):
            return value
        if key in (curses.KEY_BACKSPACE, 127, 8):
            value = value[:-1]
        elif 32 <= key <= 126 and len(value) < MAX_PROMPT:
            value += chr(key)


def draw(screen, model):
    screen.erase()
    height, width = screen.getmaxyx()
    if height < MIN_ROWS or width < MIN_COLS:
        _line(screen, 0, 'Resize terminal to at least 76 x 15. q quits.')
        screen.refresh()
        return
    _line(screen, 0, 'ATRAXIS | Rule workbench | ' + model.name, curses.A_BOLD)
    active = model.state['revision'] if model.state else None
    _line(screen, 1, 'Active: ' + (active or 'inactive'))
    _line(screen, 2, 'Up/Down select | PgUp/PgDn pages | r refresh | v view | h audit | c catalog')
    _line(screen, 3, 't test | a activate | b rollback | d stop | i import | o observe | q quit')
    available = min(6, max(1, height // 3))
    start = max(0, model.index - available + 1)
    for position, item in enumerate(model.rows[start:start+available], start):
        prefix = '> ' if position == model.index else '  '
        marker = ' ACTIVE' if item['revision'] == active else ''
        _line(screen, 5 + position - start, prefix + item['revision'] + marker + ' | ' + item['author'],
              curses.A_REVERSE if position == model.index else 0)
    detail_start = 6 + available
    for index, line in enumerate(model.detail[model.detail_offset:model.detail_offset+max(0, height-detail_start-3)]):
        _line(screen, detail_start + index, line)
    _line(screen, height-2, model.message, curses.A_BOLD)
    screen.refresh()


def run(operator, name, cases=None):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError('terminal_required')
    model = RuleWorkbench(operator, name, cases)
    model.refresh()

    def loop(screen):
        screen.keypad(True)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        running = True
        while running:
            draw(screen, model)
            key = screen.getch()
            try:
                if key == ord('o') and model.pending is None:
                    target = _prompt(screen, 'Observe target (container:identity): ')
                    if target:
                        model.observe(_target(target))
                elif key == ord('i') and model.pending is None:
                    path = _prompt(screen, 'Import file (ESC cancels): ')
                    if path:
                        model.import_file(path)
                else:
                    running = model.handle(key)
            except Exception:
                model.pending = None
                model.message = 'Operation failed or denied. Refresh state; verify permissions, files and tests.'
    curses.wrapper(loop)
