"""Terminal backend: lifecycle, raw input decoding, event queue.

One reader thread owns the terminal input and translates it into data
events; the UI loop consumes the queue. Nothing here ever draws.

Events:
    Key(key, mods)      - "enter", "esc", "tab", "backspace", "delete",
                          "up"/"down"/"left"/"right", "home", "end",
                          "pageup", "pagedown", "ctrl+left", "ctrl+right",
                          "newline" (insert line break), "ctrl+<letter>",
                          or a single printable character.
    Mouse(kind, button, x, y, mods) - kind: "press"|"drag"|"release"|"wheel";
                          button: 0 left, 1 middle, 2 right, 64/65 wheel
                          up/down; x/y are 0-based screen cells.
    Paste(text)         - bracketed paste body (newlines preserved).
    Resize(cols, rows)  - terminal geometry changed.
"""

from __future__ import annotations

import os
import queue
import re
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, ClassVar

from core.term import term_size

# How long the reader waits between size polls / stop checks.
_POLL = 0.02

_WHEEL_UP = 64
_WHEEL_DOWN = 65


@dataclass
class Key:
    key: str
    mods: frozenset = field(default_factory=frozenset)


@dataclass
class Mouse:
    kind: str  # press | drag | release | wheel
    button: int
    x: int
    y: int
    mods: frozenset = field(default_factory=frozenset)


@dataclass
class Paste:
    text: str


@dataclass
class Resize:
    cols: int
    rows: int


Event = Any

# POSIX CSI final-byte tables (shared shape with the historical editor).
_SPECIALS = {
    "A": "up",
    "B": "down",
    "C": "right",
    "D": "left",
    "H": "home",
    "F": "end",
    "Z": "shift+tab",
}
# CSI "1;<n>" modifier parameter -> key modifier set (xterm encoding).
_MODIFIERS = {
    2: frozenset({"shift"}),
    3: frozenset({"alt"}),
    4: frozenset({"shift", "alt"}),
    5: frozenset({"ctrl"}),
    6: frozenset({"ctrl", "shift"}),
    7: frozenset({"ctrl", "alt"}),
    8: frozenset({"ctrl", "shift", "alt"}),
}

_TILDES = {
    "1": "home",
    "2": "insert",
    "3": "delete",
    "4": "end",
    "5": "pageup",
    "6": "pagedown",
    "7": "home",
    "8": "end",
}
_CTRL_NAMES = {
    0: "ctrl+space", 3: "ctrl+c", 4: "ctrl+d", 5: "ctrl+e", 6: "ctrl+f",
    7: "ctrl+g", 8: "backspace", 9: "tab", 10: "newline", 11: "ctrl+k",
    12: "ctrl+l", 13: "enter", 14: "ctrl+n", 15: "ctrl+o", 16: "ctrl+p",
    17: "ctrl+q", 18: "ctrl+r", 19: "ctrl+s", 20: "ctrl+t", 21: "ctrl+u",
    22: "ctrl+v", 23: "ctrl+w", 24: "ctrl+x", 25: "ctrl+y", 26: "ctrl+z",
}

_SGR_MOUSE = re.compile(r"^<(-?\d+);(\d+);(\d+)([Mm])")

# Bracketed paste markers. On Windows the console delivers a paste as
# key records rather than a byte stream, but a host that wraps pastes
# (e.g. Windows Terminal after ``?2004h``) sends the markers too; the
# reader reassembles them into one Paste event so newlines inside the
# paste cannot submit the composer mid-paste.
_PASTE_START = "\x1b[200~"
_PASTE_END = "\x1b[201~"
_PASTE_CAP = 200000


class Backend:
    """Owns the terminal: raw mode, event decoding, frame writes."""

    def __init__(self) -> None:
        self.events: "queue.Queue[Event]" = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._size: tuple[int, int] = (0, 0)
        # POSIX self-pipe: a SIGWINCH handler writes one byte so the
        # reader's select() wakes up and emits a Resize event.
        self._winch_r = self._winch_w = -1
        # Windows console mode to restore on exit.
        self._saved_in_mode: int | None = None
        self._saved_out_mode: int | None = None
        self._stopped = False

    # ── lifecycle ─────────────────────────────────────────────

    # Mouse tracking (SGR) so reports arrive as structured records
    # instead of being decomposed into keystrokes, plus bracketed paste
    # for multi-line input. Mode 1002 (button-event tracking) is what
    # delivers motion while a button is held - without it a terminal
    # reports only press/release and drag selection can never start.
    _MOUSE_ON = "\033[?1000h\033[?1002h\033[?1006h"
    _MOUSE_OFF = "\033[?1000l\033[?1002l\033[?1003l\033[?1006l"
    _PASTE_OFF = "\033[?2004l"

    def start(self) -> None:
        self._stopped = False
        self._stop.clear()
        self._size = term_size()
        self._enter()
        # Clear any tracking modes a crashed previous session may have
        # left armed, then claim the modes this application services.
        # Without the claim, mouse reports arrive decomposed into raw
        # keystrokes and get typed into the prompt as literal text.
        self.write(
            "\033[?1000l\033[?1002l\033[?1003l\033[?1006l\033[?2004l\033[?1004l"
            + self._MOUSE_ON
            + "\033[?2004h"
        )
        # Alternate screen: the application owns the whole surface and
        # restores the shell's screen on exit.
        self.write("\033[?1049h\033[2J\033[H\033[?25h")
        # Crash safety: a traceback must still restore the terminal.
        import atexit

        self._atexit = atexit.register(self.stop)
        self._thread = threading.Thread(
            target=self._read_loop, name="tui-input", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.5)
        self.write(self._MOUSE_OFF + self._PASTE_OFF + "\033[?25h\033[r\033[?1049l")
        self._leave()
        try:
            import atexit

            atexit.unregister(self.stop)
        except (ValueError, TypeError):
            pass  # already unregistered or interpreter shutting down

    @property
    def size(self) -> tuple[int, int]:
        return self._size

    def write(self, text: str) -> None:
        if os.name == "nt":
            # Frames are pure escape sequences: if anything in the process
            # tree cleared virtual-terminal processing, the next frame
            # would paint as literal text. Verify on every frame (one
            # syscall) and restore immediately when lost.
            self._ensure_vt()
        with self._write_lock:
            try:
                sys.stdout.write(text)
                sys.stdout.flush()
            except UnicodeEncodeError:
                enc = getattr(sys.stdout, "encoding", None) or "utf-8"
                sys.stdout.write(
                    text.encode(enc, errors="replace").decode(enc, errors="replace")
                )
                sys.stdout.flush()

    def _ensure_vt(self) -> None:
        import ctypes
        from ctypes import wintypes

        try:
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            mode = wintypes.DWORD()
            if (
                handle in (None, 0, -1)
                or not kernel32.GetConsoleMode(handle, ctypes.byref(mode))
            ):
                return
            if not mode.value & 0x0004:  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            # ctypes/Win32 surface: a missing kernel32 or non-console
            # handle simply means there is nothing to restore; frames
            # still write (the UnicodeEncodeError fallback covers output).
            pass

    def current_size(self) -> tuple[int, int]:
        """Poll the true size now (used for resize verification)."""
        return term_size()

    def emit_resize_if_changed(self) -> None:
        cols, rows = self.current_size()
        if (cols, rows) != self._size and rows > 0 and cols > 0:
            self._size = (cols, rows)
            self.events.put(Resize(cols, rows))

    # ── platform setup ────────────────────────────────────────

    def _enter(self) -> None:
        if os.name == "nt":
            self._enter_windows()
        else:
            self._enter_posix()

    def _leave(self) -> None:
        if os.name == "nt":
            self._leave_windows()
        else:
            self._leave_posix()

    def _enter_windows(self) -> None:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        self._stdin_handle = kernel32.GetStdHandle(-10)
        self._stdout_handle = kernel32.GetStdHandle(-11)
        # VT output so escape sequences are interpreted.
        mode = wintypes.DWORD()
        if kernel32.GetConsoleMode(self._stdout_handle, ctypes.byref(mode)):
            self._saved_out_mode = mode.value
            kernel32.SetConsoleMode(self._stdout_handle, mode.value | 0x0004)
        # Input: mouse + window (resize) events; quick-edit selection is
        # disabled because the application draws its own. VT-input mode
        # (0x0200) is deliberately NOT enabled: it makes the pseudoconsole
        # deliver the whole input as a raw VT byte stream — arrow keys
        # decompose into escape garbage and backspace arrives as an
        # invisible DEL character. Without it every key arrives as a
        # proper virtual-key record, which is exactly what the decoder
        # below consumes.
        mode = wintypes.DWORD()
        if kernel32.GetConsoleMode(self._stdin_handle, ctypes.byref(mode)):
            self._saved_in_mode = mode.value
            new = mode.value | 0x0010 | 0x0008 | 0x0080
            new &= ~0x0040  # ENABLE_QUICK_EDIT_MODE
            new &= ~0x0200  # ENABLE_VIRTUAL_TERMINAL_INPUT
            kernel32.SetConsoleMode(self._stdin_handle, new)

    def _leave_windows(self) -> None:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        if self._saved_out_mode is not None:
            kernel32.SetConsoleMode(self._stdout_handle, self._saved_out_mode)
        if self._saved_in_mode is not None:
            kernel32.SetConsoleMode(self._stdin_handle, self._saved_in_mode)

    def _enter_posix(self) -> None:
        import termios
        import tty

        try:
            self._fd = sys.stdin.fileno()
            self._saved_attr = termios.tcgetattr(self._fd)
            tty.setraw(self._fd)
        except (OSError, ValueError, termios.error):  # type: ignore[attr-defined]
            self._fd = -1
            self._saved_attr = None
        # Self-pipe for SIGWINCH so resize wakes the reader immediately.
        try:
            self._winch_r, self._winch_w = os.pipe()
            signal.signal(signal.SIGWINCH, self._on_winch)
        except (OSError, ValueError):
            self._winch_r = self._winch_w = -1

    def _leave_posix(self) -> None:
        import termios

        if getattr(self, "_saved_attr", None) is not None:
            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved_attr)
            except termios.error:
                pass  # fd closed or not a tty anymore; nothing to restore
        try:
            signal.signal(signal.SIGWINCH, signal.SIG_DFL)
        except (OSError, ValueError):
            pass
        for fd in (self._winch_r, self._winch_w):
            if fd is not None and fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._winch_r = self._winch_w = -1

    def _on_winch(self, signum: int, frame: Any) -> None:
        try:
            if self._winch_w >= 0:
                os.write(self._winch_w, b"w")
        except OSError:
            pass

    # ── reader thread ─────────────────────────────────────────

    def _read_loop(self) -> None:
        if os.name == "nt":
            self._read_loop_windows()
        else:
            self._read_loop_posix()

    # ── Windows: console input records ────────────────────────

    def _read_loop_windows(self) -> None:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        handle = self._stdin_handle

        class COORD(ctypes.Structure):
            _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

        class KEY_RECORD(ctypes.Structure):
            _fields_ = [
                ("bKeyDown", wintypes.BOOL),
                ("wRepeatCount", wintypes.WORD),
                ("wVirtualKeyCode", wintypes.WORD),
                ("wVirtualScanCode", wintypes.WORD),
                ("uChar", ctypes.c_wchar),
                ("dwControlKeyState", wintypes.DWORD),
            ]

        class MOUSE_RECORD(ctypes.Structure):
            _fields_ = [
                ("dwMousePosition", COORD),
                ("dwButtonState", wintypes.DWORD),
                ("dwControlKeyState", wintypes.DWORD),
                ("dwEventFlags", wintypes.DWORD),
            ]

        class WINDOW_RECORD(ctypes.Structure):
            _fields_ = [("dwSize", COORD)]

        class EVENT_UNION(ctypes.Union):
            _fields_: ClassVar[list[tuple[str, Any]]] = [("KeyEvent", KEY_RECORD), ("MouseEvent", MOUSE_RECORD), ("WindowEvent", WINDOW_RECORD)]

        class INPUT_RECORD(ctypes.Structure):
            _fields_ = [("EventType", wintypes.WORD), ("Event", EVENT_UNION)]

        KEY_EVENT = 0x0001
        MOUSE_EVENT = 0x0002
        WINDOW_BUFFER_SIZE_EVENT = 0x0004
        SHIFT = 0x0010
        LEFT_CTRL = 0x0008
        RIGHT_CTRL = 0x0004
        LEFT_ALT = 0x0002
        RIGHT_ALT = 0x0001

        def mods_of(state: int) -> frozenset:
            mods = set()
            if state & SHIFT:
                mods.add("shift")
            if (state & LEFT_CTRL) or (state & RIGHT_CTRL):
                mods.add("ctrl")
            if (state & LEFT_ALT) or (state & RIGHT_ALT):
                mods.add("alt")
            return frozenset(mods)

        last_buttons = 0
        # ConPTY sometimes decomposes terminal control sequences (most
        # importantly SGR mouse reports) into individual KEY_EVENTs. A
        # sequence started by escape is accumulated here and parsed by
        # the same escape decoder the POSIX byte stream uses; when it
        # turns out to be a plain escape press, that is handed through
        # as one.
        pending = ""
        pending_at = 0.0
        last_size_poll = 0.0

        def flush_pending() -> None:
            nonlocal pending
            if not pending:
                return
            if pending.startswith(_PASTE_START):
                # A paste that grew past the cap is delivered as one Paste
                # event so its newlines stay intact and no marker text
                # leaks into the composer as literal input.
                body = pending[len(_PASTE_START):]
                for k in range(min(len(_PASTE_END) - 1, len(body)), 0, -1):
                    if _PASTE_END.startswith(body[-k:]):
                        body = body[:-k]
                        break
                self.events.put(Paste(body))
            else:
                # Decode every complete escape sequence in the buffer
                # through the shared parser: a decomposed SGR wheel
                # report, arrow, or PageUp becomes its real event here,
                # not a shower of "[<64;55;15M" keystrokes. Whatever the
                # parser cannot consume is dropped, never typed: the
                # fragments of a truncated report would otherwise land
                # in the prompt box as literal text (exactly the bug
                # where wheel motion "typed" into the composer while
                # the transcript never scrolled).
                for event in self._decode_win_sequence_buffer(pending):
                    self.events.put(event)
            pending = ""

        def pending_is_incomplete() -> bool:
            """True while ``pending`` may still grow into a real event.

            Asks the shared escape parser: an escape-prefix read means
            the sequence can complete with future records, so more
            time is granted instead of flushing mid-sequence. A lone
            escape byte counts as complete: after the grace window it
            is the escape action, never the start of a sequence.
            """
            if not pending or pending.startswith(_PASTE_START):
                return False
            if not pending.startswith("\x1b"):
                return False
            if len(pending) == 1:
                return False
            consumed, _, _ = self._parse_escape(pending, 0)
            return consumed == 0

        while not self._stop.is_set():
            # Resizes are not reliably delivered as records through a
            # pseudoconsole, so the size is polled on a timer regardless
            # of how busy the input stream is.
            now = time.monotonic()
            if now - last_size_poll >= 0.2:
                last_size_poll = now
                self.emit_resize_if_changed()
            # A decomposed sequence whose records arrive gappily gets a
            # generous grace window: flushing early would spray the
            # report's parameter bytes into the composer as typing. A
            # real key press never needs this long, and a still-growing
            # sequence resets the timer on every new record, so the
            # worst case is one delayed event, not a lost one.
            if pending and now - pending_at > 0.3 and not pending_is_incomplete():
                flush_pending()
            count = wintypes.DWORD()
            if not kernel32.GetNumberOfConsoleInputEvents(handle, ctypes.byref(count)):
                time.sleep(_POLL)
                continue
            if not count.value:
                # Nothing queued: still watch for resizes that arrive
                # without input records.
                self.emit_resize_if_changed()
                time.sleep(_POLL)
                continue
            records = (INPUT_RECORD * min(count.value, 64))()
            read = wintypes.DWORD()
            if not kernel32.ReadConsoleInputW(handle, records, len(records), ctypes.byref(read)):
                time.sleep(_POLL)
                continue
            # A burst of key-downs in one read is a paste, not typing:
            # newlines inside it must land in the composer as newlines
            # instead of submitting the prompt mid-paste.
            batch_downs = sum(
                1
                for j in range(read.value)
                if records[j].EventType == KEY_EVENT and records[j].Event.KeyEvent.bKeyDown
            )
            pasted = batch_downs > 1
            for i in range(read.value):
                rec = records[i]
                etype = rec.EventType
                if etype == WINDOW_BUFFER_SIZE_EVENT:
                    self.emit_resize_if_changed()
                    continue
                if etype == KEY_EVENT:
                    key = rec.Event.KeyEvent
                    if not key.bKeyDown:
                        continue
                    mods = mods_of(key.dwControlKeyState)
                    ch = key.uChar
                    # Sequence reassembly: an escape character opens a
                    # possible control sequence; a completed one is
                    # decoded below into its real event (prefix-matched
                    # so a burst of reports in one buffer is consumed
                    # one report at a time).
                    if pending:
                        # Repeats arrive as one record with wRepeatCount
                        # (held key or paste of identical chars); expand
                        # them while collecting a bracketed paste so the
                        # body is not shortened.
                        if key.wRepeatCount > 1 and pending.startswith(_PASTE_START):
                            pending += ch * key.wRepeatCount
                        else:
                            pending += ch
                        if pending.startswith(_PASTE_START):
                            end = pending.find(_PASTE_END)
                            if end >= 0:
                                body = pending[len(_PASTE_START):end]
                                self.events.put(Paste(body))
                                pending = ""
                            elif len(pending) - len(_PASTE_START) > _PASTE_CAP:
                                flush_pending()
                            pending_at = time.monotonic()
                            continue
                        # Decode complete sequences immediately through
                        # the shared parser (SGR wheel reports, arrows,
                        # PageUp...): a decomposed report fires its real
                        # event the moment its final record lands, and a
                        # burst of reports in one buffer is consumed one
                        # at a time.
                        while pending.startswith("\x1b"):
                            consumed, event, _ = self._parse_escape(pending, 0)
                            if consumed == 0:
                                break  # still incomplete; keep collecting
                            if event is not None:
                                self.events.put(event)
                            pending = pending[consumed:]
                        if pending and not pending.startswith("\x1b") and not pending.startswith(_PASTE_START):
                            # Printable debris between sequences (a
                            # mangled report's tail) is dropped, never
                            # typed into the composer.
                            pending = ""
                        elif len(pending) > 64:
                            flush_pending()
                        pending_at = time.monotonic()
                        continue
                    if ch == "\x1b":
                        if pending:
                            flush_pending()
                        pending = ch
                        pending_at = time.monotonic()
                        continue
                    for ev in self._win_key_events(key, mods, pasted):
                        self.events.put(ev)
                    continue
                if etype == MOUSE_EVENT:
                    m = rec.Event.MouseEvent
                    x = max(0, int(m.dwMousePosition.X))
                    y = max(0, int(m.dwMousePosition.Y))
                    flags = m.dwEventFlags
                    buttons = m.dwButtonState & 0xFFFF
                    if flags & 0x0004:  # wheel
                        delta = (m.dwButtonState >> 16) & 0xFFFF
                        direction = -1 if delta & 0x8000 else 1
                        if delta == 0:
                            continue
                        self.events.put(
                            Mouse(
                                "wheel",
                                _WHEEL_UP if direction > 0 else _WHEEL_DOWN,
                                x,
                                y,
                                mods_of(m.dwControlKeyState),
                            )
                        )
                        continue
                    if flags & 0x0001:  # move
                        if buttons:
                            self.events.put(Mouse("drag", self._primary_button(buttons, last_buttons), x, y, mods_of(m.dwControlKeyState)))
                        continue
                    # Click press/release (flags == 0 or double-click flag)
                    changed = buttons ^ last_buttons
                    last_buttons = buttons
                    if changed & 0x0001:
                        kind = "press" if buttons & 0x0001 else "release"
                        self.events.put(Mouse(kind, 0, x, y, mods_of(m.dwControlKeyState)))
                    if changed & 0x0002 and buttons & 0x0002:
                        self.events.put(Mouse("press", 2, x, y, mods_of(m.dwControlKeyState)))
                    continue

    def _win_key_events(self, key: Any, mods: frozenset, pasted: bool) -> list[Event]:
        """Decode one Windows key record, keeping pastes out of submit.

        A lone Enter submits the prompt, but an Enter inside a burst of
        key-downs is a pasted newline and must insert instead — otherwise
        the first line of a paste submits before the rest arrives.
        """
        out: list[Event] = []
        for ev in self._win_key_to_event(key, mods):
            if (
                pasted
                and isinstance(ev, Key)
                and ev.key == "enter"
                and "shift" not in ev.mods
            ):
                ev = Key("newline", ev.mods)
            out.append(ev)
        return out

    @staticmethod
    def _primary_button(current: int, previous: int) -> int:
        for bit, name in ((0x0001, 0), (0x0002, 2), (0x0004, 1)):
            if current & bit and previous & bit:
                return name
        return 0

    def _win_key_to_event(self, key: Any, mods: frozenset) -> list[Event]:
        vk = key.wVirtualKeyCode
        ch = key.uChar
        ctrl = "ctrl" in mods
        # Escape and a few others are identified by virtual key: their
        # character form is ambiguous with control codes.
        if vk == 0x1B:
            return [Key("esc", mods)]
        # Control characters arrive as their control code in uChar. A NUL
        # uChar means the key carries no character at all (arrows, function
        # keys): it must fall through to the virtual-key lookup below
        # instead of being misread as Ctrl+Space.
        if ch and ord(ch) < 32 and ch != "\x00":
            name = _CTRL_NAMES.get(ord(ch))
            if name == "enter" and "shift" in mods:
                return [Key("newline", mods)]
            return [Key(name or ch, mods)] if name else []
        if ch and ord(ch) >= 32:
            if ctrl and ch.isalpha():
                return [Key("ctrl+" + ch.lower(), mods)]
            if "shift" in mods and vk == 0x0D:
                return [Key("newline", mods)]
            if vk == 0x0D:
                return [Key("enter", mods)]
            if vk == 0x09:
                return [Key("shift+tab" if "shift" in mods else "tab", mods)]
            return [Key(ch, mods)]
        named = {
            0x21: "pageup", 0x22: "pagedown", 0x23: "end", 0x24: "home",
            0x25: "left", 0x26: "up", 0x27: "right", 0x28: "down",
            0x2D: "insert", 0x2E: "delete", 0x0D: "enter", 0x09: "tab",
            0x08: "backspace", 0x20: " ",
        }
        name = named.get(vk)
        if name is None:
            return []
        if ctrl and vk == 0x20:  # Ctrl+Space (NUL) keeps its control identity
            return [Key("ctrl+space", mods)]
        if ctrl and name in ("left", "right"):
            return [Key("ctrl+" + name, mods)]
        if name == "enter" and "shift" in mods:
            return [Key("newline", mods)]
        return [Key(name, mods)]

    # ── POSIX: byte stream parser ─────────────────────────────

    def _read_loop_posix(self) -> None:
        import select

        fd = getattr(self, "_fd", -1)
        if fd < 0:
            return
        import codecs

        # Incremental UTF-8 decoder: a multi-byte character split across
        # two reads is buffered inside the decoder instead of becoming
        # replacement characters.
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        pending = ""
        last_buttons = 0
        while not self._stop.is_set():
            timeout = 0.05 if pending else 0.2
            watch = [fd]
            if self._winch_r >= 0:
                watch.append(self._winch_r)
            try:
                ready, _, _ = select.select(watch, [], [], timeout)
            except (OSError, ValueError):
                ready = [fd]
            if self._winch_r >= 0 and self._winch_r in ready:
                try:
                    os.read(self._winch_r, 64)
                except OSError:
                    pass
                self.emit_resize_if_changed()
                continue
            if fd not in ready:
                if pending and time.monotonic() - getattr(self, "_pending_at", 0.0) > 0.05:
                    # An unterminated sequence: hand the escape back.
                    for ch in pending:
                        self._feed_char(ch)
                    pending = ""
                continue
            try:
                data = os.read(fd, 4096)
            except OSError:
                continue
            if not data:
                continue
            pending += decoder.decode(data)
            pending, last_buttons = self._parse_stream(pending, last_buttons)
            if pending:
                self._pending_at = time.monotonic()

    def _parse_stream(self, buf: str, last_buttons: int) -> tuple[str, int]:
        """Consume complete events from ``buf``; return the remainder."""
        i = 0
        n = len(buf)
        while i < n:
            ch = buf[i]
            if ch == "\x1b":
                consumed, event, last_buttons = self._parse_escape(
                    buf[i:], last_buttons
                )
                if consumed == 0:
                    break  # incomplete; wait for more bytes
                if event is not None:
                    self.events.put(event)
                i += consumed
                continue
            o = ord(ch)
            if o < 32:
                name = _CTRL_NAMES.get(o)
                if name is not None:
                    self.events.put(Key(name))
                i += 1
                continue
            if o == 127:
                self.events.put(Key("backspace"))
                i += 1
                continue
            if 128 <= o <= 159:
                i += 1  # C1 control byte: never printable input
                continue
            self.events.put(Key(ch))
            i += 1
        return buf[i:], last_buttons

    def _decode_win_sequence_buffer(self, buf: str) -> list[Event]:
        """Decode a flushed Windows reassembly buffer into real events.

        ConPTY decomposes control sequences into individual key records;
        when they arrive too gappily to reassemble, this decodes every
        complete sequence the buffer holds instead of spraying its
        parameter bytes as typing. Printable debris is dropped, never
        typed: the fragments of a truncated SGR report would otherwise
        land in the prompt box as literal text (exactly the bug where
        wheel motion "typed" into the composer while the transcript
        never scrolled).
        """
        out: list[Event] = []
        while buf:
            if buf[0] != "\x1b":
                ch, buf = buf[0], buf[1:]
                if ch >= " " and not (0x7F <= ord(ch) <= 0x9F):
                    out.append(Key(ch))
                continue
            consumed, event, _ = self._parse_escape(buf, 0)
            if consumed == 0:
                # Unterminated sequence: a bare "\x1b" left after a
                # timeout replays as the escape action; anything longer
                # is a mangled report and is discarded.
                if len(buf) == 1:
                    out.append(Key("esc"))
                break
            if event is not None:
                out.append(event)
            buf = buf[consumed:]
        return out

    def _parse_escape(self, seq: str, last_buttons: int) -> tuple[int, Event | None, int]:
        """Parse one escape sequence at the start of ``seq``.

        Returns (bytes consumed, event or None, new button state).
        Zero consumption means the sequence is incomplete.
        """
        if len(seq) < 2:
            return 0, None, last_buttons
        if seq[1] == "[":
            body = seq[2:]
            if not body:
                return 0, None, last_buttons
            # Bracketed paste start: 200~ ... 201~
            if body.startswith("200"):
                idx = seq.find(_PASTE_END)
                if idx < 0:
                    # Bound memory: a paste whose end marker never shows
                    # is cut off at the cap and delivered as-is.
                    if len(seq) > _PASTE_CAP + len(_PASTE_START):
                        cut = _PASTE_CAP + len(_PASTE_START)
                        return len(seq), Paste(seq[len(_PASTE_START):cut]), last_buttons
                    return 0, None, last_buttons
                text = seq[len(_PASTE_START):idx]
                return idx + len(_PASTE_END), Paste(text), last_buttons
            # SGR mouse: < button ; col ; row M|m
            if body.startswith("<"):
                m = _SGR_MOUSE.match(body)
                if not m:
                    return (1, None, last_buttons) if len(body) >= 32 else (0, None, last_buttons)
                button = int(m.group(1))
                col = max(0, int(m.group(2)) - 1)
                row = max(0, int(m.group(3)) - 1)
                pressed = m.group(4) == "M"
                # Wheel codes carry the modifier bits (shift +4, alt +8,
                # ctrl +16), and the spec adds 66/67 for horizontal tilt.
                # Classify with the modifiers stripped: an unstripped
                # shift+wheel (68) fell through to the press path below
                # and became a click wherever the cursor rested.
                base = button & ~0x1C
                if base in (_WHEEL_UP, _WHEEL_DOWN):
                    return len(m.group(0)) + 2, Mouse("wheel", base, col, row), last_buttons
                if base in (66, 67):
                    # Horizontal tilt: the transcript has no horizontal
                    # axis, so swallow the notch rather than misread it.
                    return len(m.group(0)) + 2, None, last_buttons
                if button & 32:  # motion bit: drag with a button held
                    return len(m.group(0)) + 2, Mouse("drag", 0, col, row), last_buttons
                kind = "press" if pressed else "release"
                return len(m.group(0)) + 2, Mouse(kind, 0, col, row), last_buttons
            # Ordinary CSI: parameters then final byte.
            m = re.match(r"^([0-9;]*)([A-Za-z~])", body)
            if not m:
                if len(body) > 32:
                    return 2, None, last_buttons
                return 0, None, last_buttons
            params, final = m.group(1), m.group(2)
            consumed = 2 + len(m.group(0))
            if final == "~":
                name = _TILDES.get(params or final)
                return consumed, (Key(name) if name else None), last_buttons
            mod_match = re.match(r"^1;([2-8])$", params)
            if mod_match and final in ("A", "B", "C", "D"):
                mods = _MODIFIERS.get(int(mod_match.group(1)), frozenset())
                base = {"A": "up", "B": "down", "C": "right", "D": "left"}[final]
                # Ctrl+left/right keep the composed name the Windows path
                # and the composer already understand; every other combo
                # carries its modifiers in ``mods``.
                if "ctrl" in mods and final in ("C", "D"):
                    return consumed, Key("ctrl+" + base, mods), last_buttons
                return consumed, Key(base, mods), last_buttons
            if final in ("u",) and params in ("13;2", "13"):
                return consumed, Key("newline" if params == "13;2" else "enter"), last_buttons
            name = _SPECIALS.get(final)
            if name:
                return consumed, Key(name), last_buttons
            return consumed, None, last_buttons
        if seq[1] == "O":
            if len(seq) < 3:
                return 0, None, last_buttons
            name = _SPECIALS.get(seq[2])
            return 3, (Key(name) if name else None), last_buttons
        # Alt+chord (ESC followed by a printable byte), ESC ESC, or a
        # bare escape followed by a control byte (the escape acts alone).
        if seq[1] == "\x1b":
            return 1, Key("esc"), last_buttons
        if seq[1] >= " ":
            return 2, Key(seq[1], frozenset({"alt"})), last_buttons
        return 1, Key("esc"), last_buttons

    def _feed_char(self, ch: str) -> None:
        if ch == "\x1b":
            # A lone escape replayed after a sequence timeout is the escape
            # action, never a printable character.
            self.events.put(Key("esc"))
            return
        o = ord(ch)
        if o == 127:
            self.events.put(Key("backspace"))
            return
        if 128 <= o <= 159:
            return
        name = _CTRL_NAMES.get(o) if o < 32 else None
        self.events.put(Key(name or ch))
