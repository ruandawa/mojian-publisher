import types
import unittest
from unittest.mock import Mock
from webview.event import Event

from publisher.desktop import WindowLifecycle


class DesktopLifecycleTests(unittest.TestCase):
    def make(self, answer):
        window = Mock()
        server = types.SimpleNamespace(should_exit=False)
        app = types.SimpleNamespace(state=types.SimpleNamespace())
        return WindowLifecycle(window, server, app, confirm=lambda _: answer), window, server

    def test_cancelled_native_close_keeps_window_and_service(self):
        lifecycle, window, server = self.make(False)
        closing = Event(window, should_lock=True)
        closing += lifecycle.closing
        # WinForms assigns Event.set()'s result to FormClosingEventArgs.Cancel.
        self.assertTrue(closing.set())
        window.destroy.assert_not_called()
        self.assertFalse(server.should_exit)

    def test_confirmed_native_close_allows_window_and_stops_service(self):
        lifecycle, window, server = self.make(True)
        closing = Event(window, should_lock=True)
        closing += lifecycle.closing
        self.assertFalse(closing.set())
        lifecycle.closed()
        self.assertTrue(server.should_exit)

    def test_explicit_app_exit_bypasses_second_native_prompt(self):
        lifecycle, window, server = self.make(False)
        closing = Event(window, should_lock=True)
        closing += lifecycle.closing
        lifecycle.exit(confirmed=True)
        window.destroy.assert_called_once()
        self.assertFalse(closing.set())
