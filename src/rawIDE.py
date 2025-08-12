'''
rawIDE - a simple terminal-based code editor / mini-IDE written in pure Python (stdlib only).

Features:
- Edit text files in a simple buffer (insert characters, backspace, Enter).
- Navigate with arrow keys.
- Command mode triggered by ':' at the bottom. Commands include:
    :w        - save current file
    :wq       - save and quit
    :q        - quit (will warn if unsaved changes)
    :r        - compile and run current file (behavior depends on file extension)
    :open F   - open file F
    :cd DIR   - change working directory
    :mkdir DIR- make directory
    :ls [DIR] - list directory (or current)
    :help     - show available commands
- Works best on Unix-like systems using curses. Includes a basic Windows fallback using msvcrt and ANSI sequences.
- No external libraries required (uses only Python standard library).

Limitations / notes:
- This is a minimal editor meant as a starting point. It is not a full-featured editor.
- Terminal must support ANSI escapes. On Windows, use Win10+ cmd or PowerShell for decent results.
- Running/compilation uses subprocess; it will execute commands on the host system. Use with care.

Usage:
    python3 rawIDE.py [optional-file-to-open]

Version: 1.1 alpha
Author: Itcor - https://github.com/TheItcor
'''


import os
import sys
import time
import shutil
import subprocess
import tempfile
from typing import List, Tuple

IS_POSIX = os.name == 'posix'

# Try to import curses on POSIX. If unavailable (or on Windows), we'll fall back.
USE_CURSES = False
try:
    if IS_POSIX:
        import curses
        USE_CURSES = True
    else:
        # on Windows, curses might not exist; try to import it anyway (often not present)
        import curses
        USE_CURSES = True
except Exception:
    USE_CURSES = False

if not USE_CURSES:
    # Windows fallback input utilities
    try:
        import msvcrt
    except Exception:
        msvcrt = None

# --- Editor data structures ---
class Buffer:
    """A simple text buffer represented as list of lines, with undo/redo support.

    Undo/redo is implemented by storing snapshots of (lines, cx, cy).
    """
    def __init__(self, lines: List[str] = None, filename: str = None, undo_limit: int = 200):
        self.lines = lines or ['']
        self.filename = filename
        self.cx = 0  # cursor x (col)
        self.cy = 0  # cursor y (line)
        self.changed = False

        # Undo/redo stacks hold tuples: (lines_copy, cx, cy)
        self._undo_stack: List[Tuple[List[str], int, int]] = []
        self._redo_stack: List[Tuple[List[str], int, int]] = []
        self._undo_limit = undo_limit

        # initial state is not pushed to undo stack by default

    # --- Internal snapshot helpers ---
    def _snapshot(self) -> Tuple[List[str], int, int]:
        """Return a deep-ish copy snapshot of current state."""
        return (list(self.lines), self.cx, self.cy)

    def _restore_snapshot(self, snap: Tuple[List[str], int, int]):
        """Restore from a snapshot tuple."""
        lines, cx, cy = snap
        self.lines = list(lines)
        self.cx = cx
        self.cy = cy
        # When restoring an old snapshot, we should mark as changed (it may differ)
        self.changed = True

    def _push_undo(self):
        """Push current state to undo stack and cap its size."""
        self._undo_stack.append(self._snapshot())
        if len(self._undo_stack) > self._undo_limit:
            # drop oldest
            del self._undo_stack[0]

    def _clear_redo(self):
        """Clear redo stack (should be called on any new edit)."""
        self._redo_stack.clear()

    # --- Public undo/redo operations ---
    def undo(self) -> bool:
        """Undo last operation. Returns True if undone, False if nothing to undo."""
        if not self._undo_stack:
            return False
        # push current state to redo, then restore last undo
        self._redo_stack.append(self._snapshot())
        snap = self._undo_stack.pop()
        self._restore_snapshot(snap)
        return True

    def redo(self) -> bool:
        """Redo last undone operation. Returns True if redone, False if nothing to redo."""
        if not self._redo_stack:
            return False
        # push current state back to undo, then restore redo snapshot
        self._undo_stack.append(self._snapshot())
        snap = self._redo_stack.pop()
        self._restore_snapshot(snap)
        return True

    # --- Editing primitives (push undo before mutations) ---
    def insert_char(self, ch: str):
        """Insert characters at current cursor position."""
        self._push_undo()
        self._clear_redo()
        line = self.lines[self.cy]
        self.lines[self.cy] = line[:self.cx] + ch + line[self.cx:]
        self.cx += len(ch)
        self.changed = True

    def backspace(self):
        """Backspace: delete char before cursor or join with previous line."""
        # If nothing to delete and at start of buffer, do nothing
        if self.cx == 0 and self.cy == 0:
            return
        self._push_undo()
        self._clear_redo()
        if self.cx > 0:
            line = self.lines[self.cy]
            self.lines[self.cy] = line[:self.cx-1] + line[self.cx:]
            self.cx -= 1
            self.changed = True
        elif self.cy > 0:
            # join with previous line
            prev = self.lines[self.cy-1]
            cur = self.lines[self.cy]
            new_cx = len(prev)
            self.lines[self.cy-1] = prev + cur
            del self.lines[self.cy]
            self.cy -= 1
            self.cx = new_cx
            self.changed = True

    def newline(self):
        """Split the current line at cursor into two lines."""
        self._push_undo()
        self._clear_redo()
        line = self.lines[self.cy]
        left = line[:self.cx]
        right = line[self.cx:]
        self.lines[self.cy] = left
        self.lines.insert(self.cy+1, right)
        self.cy += 1
        self.cx = 0
        self.changed = True

    # Navigation operations do not modify buffer contents, so they don't affect undo/redo
    def move_left(self):
        if self.cx > 0:
            self.cx -= 1
        elif self.cy > 0:
            self.cy -= 1
            self.cx = len(self.lines[self.cy])

    def move_right(self):
        if self.cx < len(self.lines[self.cy]):
            self.cx += 1
        elif self.cy < len(self.lines)-1:
            self.cy += 1
            self.cx = 0

    def move_up(self):
        if self.cy > 0:
            self.cy -= 1
            self.cx = min(self.cx, len(self.lines[self.cy]))

    def move_down(self):
        if self.cy < len(self.lines)-1:
            self.cy += 1
            self.cx = min(self.cx, len(self.lines[self.cy]))

    def load_from_file(self, filename: str):
        """Load content from file. This is treated as a new state (push previous to undo)."""
        # push current state to undo so user can undo load
        self._push_undo()
        self._clear_redo()
        with open(filename, 'r', encoding='utf-8') as f:
            data = f.read().splitlines()
        if not data:
            data = ['']
        self.lines = data
        self.filename = filename
        self.cx = 0
        self.cy = 0
        self.changed = False

    def save(self, filename: str = None):
        """Save buffer to file. Saving does not affect undo/redo stacks themselves."""
        if filename is None:
            filename = self.filename
        if filename is None:
            raise ValueError('No filename specified')
        # Ensure directory exists
        os.makedirs(os.path.dirname(os.path.abspath(filename)) or '.', exist_ok=True)
        with open(filename, 'w', encoding='utf-8') as f:
            f.write('\n'.join(self.lines))
        self.filename = filename
        self.changed = False

# --- Utilities ---

def run_command_and_capture(cmd: List[str], cwd: str = None, timeout: int = 10) -> Tuple[int, str, str]:
    """Run a command (list) without shell and capture stdout/stderr.
    Returns (returncode, stdout, stderr).
    """
    try:
        proc = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as e:
        return -1, '', f'Timeout after {timeout} seconds'
    except FileNotFoundError:
        return -1, '', f'Command not found: {cmd[0]}'

# --- Platform rendering / input abstraction for fallback ---
class DumbTerminal:
    """A minimal terminal renderer + input handler for platforms without curses.
    Uses ANSI codes to clear and position the cursor, and msvcrt for key detection on Windows.

    Note: stdin.read based fallback can't reliably detect ctrl-key combos unless the terminal
    forwards them as characters. On Windows with msvcrt we can read single keys.
    """
    def __init__(self):
        self.cols, self.rows = shutil.get_terminal_size((80, 24))

    def clear(self):
        sys.stdout.write('\x1b[2J')
        sys.stdout.write('\x1b[H')
        sys.stdout.flush()

    def move_cursor(self, x: int, y: int):
        sys.stdout.write(f'\x1b[{y+1};{x+1}H')

    def hide_cursor(self):
        sys.stdout.write('\x1b[?25l')

    def show_cursor(self):
        sys.stdout.write('\x1b[?25h')

    def get_key(self):
        # returns a tuple (type, value) where type may be 'char' or 'arrow' or 'ctrl'
        if msvcrt:
            ch = msvcrt.getwch()
            # handle extended keys
            if ch == '\x00' or ch == '\xe0':
                code = msvcrt.getwch()
                mapping = {'H':'UP','P':'DOWN','K':'LEFT','M':'RIGHT'}
                return ('arrow', mapping.get(code, code))
            else:
                if ch == '\r':
                    return ('char', '\n')
                # msvcrt returns control key characters too (e.g. '\x1a' for Ctrl+Z)
                return ('char', ch)
        else:
            # fallback to blocking sys.stdin.read (user must press Enter) - very limited
            ch = sys.stdin.read(1)
            return ('char', ch)

# --- Core UI / Editor loop for curses ---
class RawIDE:
    def __init__(self, stdscr=None, use_curses: bool = True):
        self.use_curses = use_curses and USE_CURSES
        self.stdscr = stdscr
        self.width = 80
        self.height = 24
        self.top_line = 0  # line index at top of screen
        self.left_col = 0
        self.status = ''
        self.message_time = 0.0
        self.buffer = Buffer()
        if not self.buffer.filename:
            self.buffer.filename = None
        # Modes: 'command' (normal) and 'editor' (insert)
        self.mode = 'command'  # start in command (normal) mode as requested

    # ---- High-level commands ----
    def open_file(self, filename: str):
        try:
            self.buffer.load_from_file(filename)
            self.status_message(f'Opened {filename}')
        except Exception as e:
            self.status_message(f'Error opening {filename}: {e}')

    def save_file(self, filename: str = None):
        try:
            self.buffer.save(filename)
            self.status_message(f'Saved {self.buffer.filename}')
        except Exception as e:
            self.status_message(f'Error saving: {e}')

    def quit(self, force: bool = False):
        if self.buffer.changed and not force:
            self.status_message('Unsaved changes. Use :q! to quit without saving or :w to save.')
            return False
        return True

    def compile_and_run(self):
        fname = self.buffer.filename
        if not fname:
            self.status_message('No filename. Save first with :w')
            return
        ext = os.path.splitext(fname)[1].lower()
        cwd = os.getcwd()
        # Save buffer to file before running
        try:
            self.buffer.save()
        except Exception as e:
            self.status_message(f'Failed to save: {e}')
            return
        # Determine how to run
        if ext in ['.py']:
            cmd = [sys.executable, fname]
            rc, out, err = run_command_and_capture(cmd, cwd=cwd, timeout=30)
            self.show_output(rc, out, err)
        elif ext in ['.c']:
            exe = tempfile.mktemp(prefix='rawide_', suffix='')
            cmd_compile = ['gcc', fname, '-o', exe]
            rc, out, err = run_command_and_capture(cmd_compile, cwd=cwd, timeout=30)
            if rc != 0:
                self.show_output(rc, out, err, compile_phase=True)
            else:
                rc2, out2, err2 = run_command_and_capture([exe], cwd=cwd, timeout=30)
                self.show_output(rc2, out2, err2)
                try:
                    os.remove(exe)
                except Exception:
                    pass
        elif ext in ['.cpp', '.cc', '.cxx']:
            exe = tempfile.mktemp(prefix='rawide_', suffix='')
            cmd_compile = ['g++', fname, '-o', exe]
            rc, out, err = run_command_and_capture(cmd_compile, cwd=cwd, timeout=30)
            if rc != 0:
                self.show_output(rc, out, err, compile_phase=True)
            else:
                rc2, out2, err2 = run_command_and_capture([exe], cwd=cwd, timeout=30)
                self.show_output(rc2, out2, err2)
                try:
                    os.remove(exe)
                except Exception:
                    pass
        elif ext in ['.rs']:
            # try rustc
            exe = tempfile.mktemp(prefix='rawide_', suffix='')
            cmd_compile = ['rustc', fname, '-o', exe]
            rc, out, err = run_command_and_capture(cmd_compile, cwd=cwd, timeout=30)
            if rc != 0:
                self.show_output(rc, out, err, compile_phase=True)
            else:
                rc2, out2, err2 = run_command_and_capture([exe], cwd=cwd, timeout=30)
                self.show_output(rc2, out2, err2)
                try:
                    os.remove(exe)
                except Exception:
                    pass
        else:
            self.status_message(f'Run/compile not supported for {ext}')

    def show_output(self, rc: int, out: str, err: str, compile_phase: bool = False):
        # show the output in a pager-like view; wait for user to press a key
        sep = '--- stdout ---\n' + out + '\n--- stderr ---\n' + err + f'\n(returncode={rc})\n'
        self.popup_text(sep)

    # ---- Mode helpers ----
    def set_mode(self, mode: str):
        assert mode in ('command', 'editor')
        self.mode = mode
        # persistent mode indicator in status (no timeout)
        self.status = f'MODE: {self.mode.upper()}'
        # do not set message_time so it remains visible

    # ---- UI helpers ----
    def status_message(self, msg: str, timeout: float = 3.0):
        # If a mode is set, show it alongside the transient message
        self.status = f'MODE: {self.mode.upper()} - {msg}'
        self.message_time = time.time() + timeout

    def popup_text(self, text: str):
        # show text and wait for keypress
        if self.use_curses:
            maxy, maxx = self.stdscr.getmaxyx()
            win = curses.newwin(maxy-2, maxx-2, 1, 1)
            win.clear()
            win.addstr(0, 0, text[:(maxy-3)*(maxx-1)])
            win.box()
            win.addstr(maxy-3, 1, 'Press any key to continue...')
            win.refresh()
            self.stdscr.getch()
        else:
            dt = DumbTerminal()
            dt.clear()
            print(text)
            input('Press Enter to continue...')

    # ---- Command handling ----
    def handle_command(self, cmdline: str) -> bool:
        """Handle a ':' command. Return True to continue, False to exit editor.
        """
        cmd = cmdline.strip()
        if not cmd:
            return True
        parts = cmd.split()
        main = parts[0]
        args = parts[1:]
        if main == 'w':
            if args:
                self.save_file(args[0])
            else:
                if not self.buffer.filename:
                    self.status_message('Specify filename: :w filename')
                else:
                    self.save_file()
            return True
        elif main == 'wq':
            if not self.buffer.filename:
                self.status_message('Specify filename: :wq filename')
                return True
            self.save_file()
            return False
        elif main == 'q':
            # support :q! to force quit
            if args and args[0] == '!':
                return False
            if self.buffer.changed:
                self.status_message('Unsaved changes. Use :q! to quit without saving.')
                return True
            return False
        elif main == 'r':
            self.compile_and_run()
            return True
        elif main == 'open':
            if not args:
                self.status_message('Usage: :open filename')
            else:
                self.open_file(args[0])
            return True
        elif main == 'cd':
            if not args:
                self.status_message('Usage: :cd directory')
            else:
                try:
                    os.chdir(args[0])
                    self.status_message(f'cwd: {os.getcwd()}')
                except Exception as e:
                    self.status_message(f'cd error: {e}')
            return True
        elif main == 'mkdir':
            if not args:
                self.status_message('Usage: :mkdir dirname')
            else:
                try:
                    os.makedirs(args[0], exist_ok=True)
                    self.status_message('mkdir ok')
                except Exception as e:
                    self.status_message(f'mkdir error: {e}')
            return True
        elif main == 'ls':
            target = args[0] if args else '.'
            try:
                items = os.listdir(target)
                out = '\n'.join(items)
                self.popup_text(out)
            except Exception as e:
                self.status_message(f'ls error: {e}')
            return True
        elif main == 'help':
            help_text = (
                ':w - save\n'
                ':wq - save and quit\n'
                ':q - quit (:q! to force)\n'
                ':r - compile & run current file\n'
                ':open filename - open file\n'
                ':cd dir - change directory\n'
                ':mkdir dir - create directory\n'
                ':ls [dir] - list directory\n'
                'Ctrl+Z - undo\n'
                'Ctrl+U - redo\n'
            )
            self.popup_text(help_text)
            return True
        else:
            self.status_message(f'Unknown command: {main}')
            return True

    # ---- Drawing / input loops ----
    def draw(self):
        if self.use_curses:
            self.stdscr.erase()
            maxy, maxx = self.stdscr.getmaxyx()
            self.height = maxy
            self.width = maxx
            # Draw text area (leave last line for status)
            text_h = maxy - 2
            for i in range(text_h):
                lineno = self.top_line + i
                if lineno >= len(self.buffer.lines):
                    break
                line = self.buffer.lines[lineno]
                # handle left_col scrolling
                visible = line[self.left_col:self.left_col+maxx-1]
                try:
                    self.stdscr.addstr(i, 0, visible)
                except curses.error:
                    pass
            # status bar
            status = f"rawIDE - {self.buffer.filename or '[no file]'} {'*' if self.buffer.changed else ''}  ln {self.buffer.cy+1}, col {self.buffer.cx+1}  {self.status}"
            try:
                self.stdscr.addstr(maxy-2, 0, status[:maxx-1], curses.A_REVERSE)
            except curses.error:
                pass
            # command line area
            try:
                self.stdscr.addstr(maxy-1, 0, ':')
            except curses.error:
                pass
            # position cursor
            cy = self.buffer.cy - self.top_line
            cx = self.buffer.cx - self.left_col
            if 0 <= cy < text_h and 0 <= cx < maxx:
                try:
                    self.stdscr.move(cy, cx)
                except curses.error:
                    pass
            self.stdscr.refresh()
        else:
            dt = DumbTerminal()
            dt.clear()
            cols, rows = dt.cols, dt.rows
            text_h = rows - 2
            for i in range(text_h):
                lineno = self.top_line + i
                if lineno >= len(self.buffer.lines):
                    print('')
                    continue
                line = self.buffer.lines[lineno]
                visible = line[self.left_col:self.left_col+cols-1]
                print(visible)
            # status bar
            status = f"rawIDE - {self.buffer.filename or '[no file]'} {'*' if self.buffer.changed else ''}  ln {self.buffer.cy+1}, col {self.buffer.cx+1}  {self.status}"
            print(status[:cols-1])
            print(':', end='', flush=True)

    def ensure_cursor_visible(self):
        # vertical
        if self.buffer.cy < self.top_line:
            self.top_line = self.buffer.cy
        elif self.buffer.cy >= self.top_line + (self.height - 2):
            self.top_line = self.buffer.cy - (self.height - 3)
        # horizontal
        if self.buffer.cx < self.left_col:
            self.left_col = self.buffer.cx
        elif self.buffer.cx >= self.left_col + (self.width - 1):
            self.left_col = self.buffer.cx - (self.width - 2)

    def run_curses(self):
        curses.raw()
        curses.noecho()
        self.stdscr.keypad(True)
        # ensure mode indicator shown
        self.set_mode(self.mode)
        try:
            maxy, maxx = self.stdscr.getmaxyx()
            self.height = maxy
            self.width = maxx
            while True:
                self.ensure_cursor_visible()
                self.draw()
                ch = self.stdscr.getch()
                # Mode switching: ESC -> command (normal), 'i' in command -> editor (insert)
                if ch == 27:  # ESC
                    self.set_mode('command')
                    continue

                # handle global ctrl keys (works in both modes)
                # Ctrl+Z = 26, Ctrl+U = 21
                if ch == 26:
                    if self.buffer.undo():
                        self.status_message('Undo')
                    else:
                        self.status_message('Nothing to undo')
                    continue
                if ch == 21:
                    if self.buffer.redo():
                        self.status_message('Redo')
                    else:
                        self.status_message('Nothing to redo')
                    continue

                if self.mode == 'command':
                    # Navigation allowed in command mode
                    if ch == curses.KEY_LEFT:
                        self.buffer.move_left()
                    elif ch == curses.KEY_RIGHT:
                        self.buffer.move_right()
                    elif ch == curses.KEY_UP:
                        self.buffer.move_up()
                    elif ch == curses.KEY_DOWN:
                        self.buffer.move_down()
                    elif ch == ord('i'):
                        # enter editor (insert) mode
                        self.set_mode('editor')
                    elif ch == ord(':'):
                        # enter command-line prompt (only from command mode)
                        curses.echo()
                        maxy, maxx = self.stdscr.getmaxyx()
                        self.stdscr.move(maxy-1, 0)
                        self.stdscr.clrtoeol()
                        self.stdscr.addstr(maxy-1, 0, ':')
                        cmd = self.stdscr.getstr(maxy-1, 1, 200).decode('utf-8')
                        curses.noecho()
                        cont = self.handle_command(cmd)
                        if not cont:
                            break
                    # ignore other printable keys in command mode
                else:  # editor mode
                    # handle editing keys
                    if ch == curses.KEY_LEFT:
                        self.buffer.move_left()
                    elif ch == curses.KEY_RIGHT:
                        self.buffer.move_right()
                    elif ch == curses.KEY_UP:
                        self.buffer.move_up()
                    elif ch == curses.KEY_DOWN:
                        self.buffer.move_down()
                    elif ch in (10, 13):  # Enter
                        self.buffer.newline()
                    elif ch in (127, curses.KEY_BACKSPACE):
                        self.buffer.backspace()
                    elif 0 <= ch <= 255:
                        try:
                            chs = chr(ch)
                        except Exception:
                            chs = ''
                        if chs:
                            if chs == '\t':
                                self.buffer.insert_char('    ')
                            else:
                                self.buffer.insert_char(chs)
                # clear transient status when expired
                if time.time() > self.message_time:
                    # keep the persistent mode indicator
                    self.status = f'MODE: {self.mode.upper()}'
        finally:
            curses.nocbreak()
            self.stdscr.keypad(False)
            curses.echo()

    def run_dumb(self):
        dt = DumbTerminal()
        dt.hide_cursor()
        # ensure mode indicator shown
        self.set_mode(self.mode)
        try:
            while True:
                self.height = dt.rows
                self.width = dt.cols
                self.ensure_cursor_visible()
                self.draw()
                key = dt.get_key()
                ktype, val = key
                # handle ESC
                if ktype == 'char' and val == '\x1b':
                    self.set_mode('command')
                    continue

                # detect Ctrl+Z and Ctrl+U in dumb mode as well (if terminal forwards them)
                # Ctrl+Z -> '\x1a' ; Ctrl+U -> '\x15'
                if ktype == 'char' and val in ('\x1a', '\x1A'):
                    if self.buffer.undo():
                        self.status_message('Undo')
                    else:
                        self.status_message('Nothing to undo')
                    continue
                if ktype == 'char' and val in ('\x15',):
                    if self.buffer.redo():
                        self.status_message('Redo')
                    else:
                        self.status_message('Nothing to redo')
                    continue

                if self.mode == 'command':
                    if ktype == 'arrow':
                        if val == 'LEFT':
                            self.buffer.move_left()
                        elif val == 'RIGHT':
                            self.buffer.move_right()
                        elif val == 'UP':
                            self.buffer.move_up()
                        elif val == 'DOWN':
                            self.buffer.move_down()
                    elif ktype == 'char':
                        if val == 'i':
                            self.set_mode('editor')
                        elif val == ':':
                            # read command from stdin (command mode only)
                            dt.move_cursor(0, dt.rows-1)
                            dt.show_cursor()
                            cmd = input(':')
                            dt.hide_cursor()
                            cont = self.handle_command(cmd)
                            if not cont:
                                break
                        # ignore other printable chars in command mode
                else:  # editor mode
                    if ktype == 'arrow':
                        if val == 'LEFT':
                            self.buffer.move_left()
                        elif val == 'RIGHT':
                            self.buffer.move_right()
                        elif val == 'UP':
                            self.buffer.move_up()
                        elif val == 'DOWN':
                            self.buffer.move_down()
                    elif ktype == 'char':
                        if val == '\n':
                            self.buffer.newline()
                        elif val == '\x08' or val == '\x7f':
                            self.buffer.backspace()
                        else:
                            if val == '\t':
                                self.buffer.insert_char('    ')
                            else:
                                # insert literal character (including ':') in editor mode
                                self.buffer.insert_char(val)
                if time.time() > self.message_time:
                    self.status = f'MODE: {self.mode.upper()}'
        finally:
            dt.show_cursor()

    def run(self):
        if self.use_curses and self.stdscr is not None:
            self.run_curses()
        else:
            self.run_dumb()

# --- Main entrypoint ---

def main(argv):
    filename = argv[1] if len(argv) > 1 else None
    if USE_CURSES and IS_POSIX:
        def curses_main(stdscr):
            editor = RawIDE(stdscr=stdscr, use_curses=True)
            if filename:
                try:
                    editor.open_file(filename)
                except Exception as e:
                    editor.status_message(f'Open error: {e}')
            # start in command (normal) mode
            editor.set_mode('command')
            editor.run()
        curses.wrapper(curses_main)
    else:
        editor = RawIDE(use_curses=False)
        if filename:
            try:
                editor.open_file(filename)
            except Exception as e:
                editor.status_message(f'Open error: {e}')
        editor.set_mode('command')
        editor.run()

if __name__ == '__main__':
    main(sys.argv)