"""Backend input decoding: SGR mouse reports, Windows virtual keys,
Posix/VT escape sequences, bracketed paste, split UTF-8 reads.

``BackendParseTest`` drives ``core.tui.backend`` directly; the
``PosixDecoderRegressionTest`` block pins behaviours that once regressed
and feeds a real decoder end-to-end."""

from __future__ import annotations

import os
import sys
import threading
import time
import unittest

from core.tui.backend import Backend, Key, Mouse, Paste

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "."))
_tests_dir = os.path.dirname(os.path.abspath(__file__))
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)

from tui_harness import wait_until  # noqa: E402


class BackendParseTest(unittest.TestCase):
    """The terminal input parser must translate SGR mouse reports the way
    the selection logic expects, and the app must request the mouse mode
    that delivers drag reports at all."""

    def _parse(self, buf: str):
        backend = Backend()
        backend._parse_stream(buf, 0)
        return backend.events

    def test_sgr_drag_report_becomes_drag_event(self):
        # Button bit 32 marks motion with a button held; the selection
        # only starts on "drag" events.
        events = self._parse("\x1b[<32;6;7M")
        self.assertEqual(events.qsize(), 1)
        ev = events.get_nowait()
        self.assertIsInstance(ev, Mouse)
        self.assertEqual(ev.kind, "drag")
        self.assertEqual(ev.button, 0)
        self.assertEqual((ev.x, ev.y), (5, 6))

    def test_sgr_press_and_release(self):
        events = self._parse("\x1b[<0;6;7M\x1b[<0;6;7m")
        kinds = [events.get_nowait().kind for _ in range(events.qsize())]
        self.assertEqual(kinds, ["press", "release"])

    def test_sgr_drag_burst_consumes_every_report(self):
        # A fast drag packs several reports into one read; each must be
        # decoded, not just the one at the end of the buffer.
        events = self._parse("\x1b[<0;6;7M\x1b[<32;7;8M\x1b[<32;8;9M\x1b[<0;8;9m")
        kinds = [events.get_nowait().kind for _ in range(events.qsize())]
        self.assertEqual(kinds, ["press", "drag", "drag", "release"])

    def test_sgr_wheel_reports_decode_to_wheel_events(self):
        # Wheel reports are button codes 64 (up) and 65 (down): they must
        # surface as Mouse("wheel") events, never as key or press events,
        # or the composer eats them as history recall instead of the
        # transcript scrolling.
        events = self._parse("\x1b[<64;11;5M\x1b[<65;11;5M\x1b[<64;11;4M")
        decoded = [events.get_nowait() for _ in range(events.qsize())]
        self.assertEqual(len(decoded), 3)
        for ev in decoded:
            self.assertIsInstance(ev, Mouse)
            self.assertEqual(ev.kind, "wheel")
        self.assertEqual([ev.button for ev in decoded], [64, 65, 64])
        self.assertEqual((decoded[0].x, decoded[0].y), (10, 4))

    def test_sgr_wheel_with_modifiers_still_decodes_as_a_wheel(self):
        # A wheel notch with shift/alt/ctrl held carries the modifier bits
        # (64+4, 65+8, 65+16...). Those codes must still classify as a
        # wheel: falling through to the press path turned the notch into
        # a click wherever the cursor rested - which over the prompt box
        # moved the caret instead of scrolling the conversation.
        events = self._parse("\x1b[<68;11;5M\x1b[<73;11;5M\x1b[<81;11;5M")
        decoded = [events.get_nowait() for _ in range(events.qsize())]
        self.assertEqual([ev.kind for ev in decoded], ["wheel", "wheel", "wheel"])
        self.assertEqual([ev.button for ev in decoded], [64, 65, 65])

    def test_sgr_tilt_wheel_is_consumed_without_a_click(self):
        # Horizontal tilt (66/67) has no vertical action; the notch must
        # be swallowed whole rather than decoded as a press.
        events = self._parse("\x1b[<66;11;5M\x1b[<67;11;5M")
        self.assertEqual(events.qsize(), 0)

    def test_decomposed_wheel_flush_never_types_into_the_prompt(self):
        # Regression: when ConPTY decomposes an SGR wheel report into key
        # records that arrive too gappily to reassemble, the buffer is
        # flushed through the shared escape parser - the complete report
        # still becomes one wheel event, and a truncated one is dropped
        # instead of typing "[<64;55;15" into the composer.
        backend = Backend()
        events = backend._decode_win_sequence_buffer("\x1b[<64;55;15M")
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertIsInstance(ev, Mouse)
        self.assertEqual(ev.kind, "wheel")
        self.assertEqual(ev.button, 64)
        self.assertEqual((ev.x, ev.y), (54, 14))

        # Truncated report (final record never arrived): dropped whole,
        # no keystrokes synthesized from its parameter bytes.
        events = backend._decode_win_sequence_buffer("\x1b[<64;55")
        self.assertEqual(events, [])

        # A lone escape still replays as the escape action.
        events = backend._decode_win_sequence_buffer("\x1b")
        self.assertEqual(events, [Key("esc")])

    def test_decomposed_arrow_flush_is_one_up_key(self):
        # The same reassembly fix covers decomposed arrow keys: the CSI
        # becomes one "up" event, not esc + "[" + "A" typing.
        backend = Backend()
        events = backend._decode_win_sequence_buffer("\x1b[A")
        self.assertEqual(events, [Key("up")])

    def test_mouse_mode_requests_button_event_tracking(self):
        # Regression: only ?1000 (press/release) was enabled, so real
        # terminals never sent drag reports and selection could not start.
        self.assertIn("?1002h", Backend._MOUSE_ON)
        self.assertIn("?1006h", Backend._MOUSE_ON)

    def test_windows_arrow_keys_decode_by_virtual_key(self):
        # A real Windows KEY_EVENT for an arrow carries VK_UP etc. with a
        # NUL uChar. The NUL used to be read as Ctrl+Space, which dropped
        # every arrow key before the virtual-key lookup ran - so the
        # completion popup could not be navigated with the keyboard.
        from types import SimpleNamespace

        backend = Backend()

        def decode(vk, uchar="\x00", mods=frozenset()):
            key = SimpleNamespace(wVirtualKeyCode=vk, uChar=uchar, dwControlKeyState=0)
            return backend._win_key_to_event(key, mods)

        self.assertEqual(decode(0x26), [Key("up")])
        self.assertEqual(decode(0x28), [Key("down")])
        self.assertEqual(decode(0x25), [Key("left")])
        self.assertEqual(decode(0x27), [Key("right")])
        self.assertEqual(decode(0x21), [Key("pageup")])
        self.assertEqual(decode(0x22), [Key("pagedown")])
        # Ctrl+Space (NUL) keeps its control identity instead of a space.
        self.assertEqual(decode(0x20, mods=frozenset({"ctrl"})),
                         [Key("ctrl+space", frozenset({"ctrl"}))])


class PosixDecoderRegressionTest(unittest.TestCase):
    """Byte-exact regressions for the streaming input decoder.

    The Windows record path is covered above; these pin the byte-stream
    path (used on POSIX hosts) so escape, DEL, C1 bytes, modifier
    arrows, and bracketed pastes keep their fixed behavior.
    """

    def _parse(self, buf: str):
        backend = Backend()
        backend._parse_stream(buf, 0)
        return backend.events

    def _feed(self, ch: str):
        backend = Backend()
        backend._feed_char(ch)
        return backend.events

    def _drain(self, events) -> list:
        out = []
        while events.qsize():
            out.append(events.get_nowait())
        return out

    def test_bare_escape_is_the_escape_action_not_text(self):
        # A lone escape replayed after the sequence timeout must surface
        # as the escape action, never as a printable byte in the prompt.
        events = self._feed("\x1b")
        self.assertEqual(events.qsize(), 1)
        self.assertEqual(events.get_nowait(), Key("esc"))

    def test_escape_before_control_byte_acts_alone(self):
        # ESC followed by a control byte: the escape acts by itself and
        # the control byte keeps its own identity (no raw \x1b Key).
        events = self._drain(self._parse("\x1b\x03"))
        self.assertEqual(events, [Key("esc"), Key("ctrl+c")])

    def test_esc_esc_yields_one_escape(self):
        # ESC ESC used to swallow both bytes; now the first escape fires
        # and the second waits (then replays as escape on timeout).
        events = self._drain(self._parse("\x1b\x1b"))
        self.assertEqual(events, [Key("esc")])

    def test_alt_chord_arrives_with_alt_modifier(self):
        # ESC + printable byte is an alt-chord, not two dropped bytes.
        events = self._drain(self._parse("\x1bb"))
        self.assertEqual(events, [Key("b", frozenset({"alt"}))])

    def test_del_edits_instead_of_typing(self):
        # DEL (0x7F) is backspace, never a literal character in the
        # buffer, on both the parse and the replay paths.
        events = self._drain(self._parse("ab\x7f"))
        self.assertEqual(events, [Key("a"), Key("b"), Key("backspace")])
        events = self._drain(self._feed("\x7f"))
        self.assertEqual(events, [Key("backspace")])

    def test_c1_bytes_are_dropped(self):
        # C1 controls (0x80-0x9F, e.g. the raw CSI byte 0x9B) must never
        # be inserted as printable input.
        events = self._drain(self._parse("a\x9bb"))
        self.assertEqual(events, [Key("a"), Key("b")])

    def test_modifier_arrows_decode_with_modifiers(self):
        # xterm "1;<n>" parameters carry modifiers; shifted arrows must
        # not decode as alt-arrows and ctrl-arrows must keep the composed
        # name the composer binds.
        cases = [
            ("\x1b[1;5D", Key("ctrl+left", frozenset({"ctrl"}))),
            ("\x1b[1;5C", Key("ctrl+right", frozenset({"ctrl"}))),
            ("\x1b[1;2C", Key("right", frozenset({"shift"}))),
            ("\x1b[1;3D", Key("left", frozenset({"alt"}))),
        ]
        for seq, expected in cases:
            with self.subTest(seq=seq):
                events = self._drain(self._parse(seq))
                self.assertEqual(events, [expected])

    def test_plain_arrows_unchanged_by_modifier_branch(self):
        # Guard: unmodified arrow sequences must still decode as before.
        for seq, expected in (
            ("\x1b[C", Key("right")),
            ("\x1b[D", Key("left")),
            ("\x1bOA", Key("up")),
        ):
            with self.subTest(seq=seq):
                events = self._drain(self._parse(seq))
                self.assertEqual(events, [expected])

    def test_bracketed_paste_delivered_whole(self):
        # The paste body (newlines included) is one Paste event; the tail
        # after the end marker returns to normal key decoding, and no
        # marker bytes leak into the event stream.
        events = self._drain(self._parse("\x1b[200~ab\ncd\x1b[201~tail"))
        self.assertEqual(len(events), 5)
        first = events[0]
        self.assertIsInstance(first, Paste)
        self.assertEqual(first.text, "ab\ncd")
        self.assertEqual([e.key for e in events[1:]], ["t", "a", "i", "l"])

    def test_unterminated_paste_is_cut_at_cap(self):
        # A paste whose end marker never arrives is cut off at the cap
        # and delivered, instead of buffering without bound.
        from core.tui.backend import _PASTE_CAP

        seq = "\x1b[200~" + "x" * (_PASTE_CAP + 50)
        events = self._drain(self._parse(seq))
        self.assertEqual(len(events), 1)
        self.assertIsInstance(events[0], Paste)
        self.assertEqual(events[0].text, "x" * _PASTE_CAP)

    def test_utf8_split_across_reads_becomes_one_character(self):
        # A multi-byte character split across two reads is reassembled by
        # the incremental decoder instead of decoding as replacement
        # characters (which would type garbage into the prompt).
        import os as _os

        backend = Backend()
        read_fd, write_fd = _os.pipe()
        backend._fd = read_fd
        backend._winch_r = -1
        thread = threading.Thread(
            target=backend._read_loop_posix, name="tui-test-input", daemon=True
        )
        thread.start()
        try:
            _os.write(write_fd, b"\xe6\xbc")  # first half of 漢
            time.sleep(0.05)
            _os.write(write_fd, b"\xa2")      # second half
            self.assertTrue(wait_until(lambda: backend.events.qsize() >= 1, timeout=5))
            event = backend.events.get_nowait()
            self.assertIsInstance(event, Key)
            self.assertEqual(event.key, "漢")
        finally:
            backend._stop.set()
            try:
                _os.write(write_fd, b" ")  # unblock a pending read
            except OSError:
                pass
            thread.join(timeout=2)
            _os.close(write_fd)




if __name__ == "__main__":  # pragma: no cover
    unittest.main()
