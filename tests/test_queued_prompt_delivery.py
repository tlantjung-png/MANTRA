"""Mid-turn prompts must survive, in order, until they run."""

import shutil
import tempfile
import unittest

from core import console as console_module
from core.scripted import final_response
from tests._helpers import make_session
from tests.tui_harness import _make_app


class _DispatchPatch:
    """Replace the module-level seam used by the TUI turn worker."""

    def __init__(self, fn):
        self.fn = fn
        self.original = None

    def __enter__(self):
        self.original = console_module.dispatch
        console_module.dispatch = self.fn
        return self

    def __exit__(self, *exc):
        console_module.dispatch = self.original


class QueuedPromptDeliveryTest(unittest.TestCase):
    def _app(self, dispatch_fn):
        app, _, _ = _make_app([])
        self.addCleanup(lambda: setattr(console_module, "dispatch", self._saved_dispatch))
        self._saved_dispatch = console_module.dispatch
        console_module.dispatch = dispatch_fn
        spawned = []
        app.run_detached = lambda fn: spawned.append(fn)
        return app, spawned

    def test_multiple_mid_turn_prompts_run_in_submission_order(self):
        seen = []

        def dispatch(_session, line):
            seen.append(line)
            return True

        app, spawned = self._app(dispatch)
        app.submit("A")
        app.busy = True
        app.submit("B")
        app.submit("C")
        self.assertEqual(list(app._prompt_queue), ["B", "C"])

        spawned.pop(0)()
        self.assertEqual(seen, ["A"])
        self.assertEqual(list(app._prompt_queue), ["C"])
        self.assertEqual(len(spawned), 1)

        spawned.pop(0)()
        spawned.pop(0)()
        self.assertEqual(seen, ["A", "B", "C"])

    def test_queued_prompt_survives_an_unexpected_turn_exception(self):
        seen = []

        def dispatch(_session, line):
            if line == "A":
                raise ValueError("unexpected failure")
            seen.append(line)
            return True

        app, spawned = self._app(dispatch)
        app.submit("A")
        app.submit("B")
        with self.assertRaises(ValueError):
            spawned.pop(0)()
        self.assertEqual(len(spawned), 1)
        spawned.pop(0)()
        self.assertEqual(seen, ["B"])

    def test_session_handle_does_not_clear_the_app_busy_flag(self):
        calls = []

        class StubUI:
            def set_busy(self, on, label=""):
                calls.append(on)

        workdir = tempfile.mkdtemp(prefix="mantra-queue-test-")
        self.addCleanup(shutil.rmtree, workdir, True)
        session = make_session(workdir, [final_response("done")])
        session.ui = StubUI()
        session.handle("hello")
        self.assertIn(True, calls)
        self.assertNotIn(False, calls)


if __name__ == "__main__":
    unittest.main()
