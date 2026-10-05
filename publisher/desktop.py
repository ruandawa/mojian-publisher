"""A visible desktop window owns the local service's lifetime."""
import ctypes

from .version import VERSION


class WindowLifecycle:
    def __init__(self, window, server, app, confirm=None):
        self.window = window
        self.server = server
        self.app = app
        self.exit_approved = False
        self.confirm = confirm or (lambda message: ctypes.windll.user32.MessageBoxW(
            None, message, '退出墨笺', 0x24) == 6)

    def closing(self):
        if self.exit_approved:
            return True
        # Never evaluate JavaScript here: WinForms is inside FormClosing and
        # blocking its UI thread while waiting for WebView2 would deadlock.
        message = ('确定退出墨笺公众号助手吗？\n\n'
                   '未保存的编辑会丢失；正在执行的任务会中断。\n'
                   '退出后本机服务、任务执行和工具打开的微信窗口都会停止。')
        if not self.confirm(message):
            return False  # Event.set() converts a False handler result to cancellation.
        return True

    def closed(self):
        self.server.should_exit = True

    def show(self):
        self.window.restore()
        self.window.show()

    def exit(self, confirmed=False):
        # Only the closed event stops the service: cancelling a close must
        # leave the whole app operational. WinForms handles UI thread dispatch.
        self.exit_approved = confirmed
        self.window.destroy()


def run_desktop(app, server, url, root):
    import webview
    webview.settings['ALLOW_DOWNLOADS'] = True
    webview.settings['ALLOW_FILE_URLS'] = False
    window = webview.create_window(
        f'墨笺公众号助手 · v{VERSION}', url, width=1360, height=900,
        min_size=(960, 640), background_color='#f5f7f3', text_select=True,
        maximized=True,
    )
    lifecycle = WindowLifecycle(window, server, app)
    window.events.closing += lifecycle.closing
    window.events.closed += lifecycle.closed
    app.state.request_exit = lifecycle.exit
    app.state.show_window = lifecycle.show
    app.state.desktop_window = True
    icon = root / 'mojian.ico'
    if not icon.is_file():
        icon = root / 'build-assets' / 'mojian.ico'
    webview.start(
        gui='edgechromium', private_mode=False,
        storage_path=str(app.state.store.root / 'workbench-profile'),
        icon=str(icon),
    )
