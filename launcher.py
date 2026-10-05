"""Windows desktop entrypoint; the window and service exit together."""
from __future__ import annotations

import argparse
import ctypes
import logging
import os
import socket
import sys
import threading
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--headless', action='store_true', help='Local diagnostics without a window or WeChat.')
    parser.add_argument('--port', type=int, default=8731)
    args = parser.parse_args()
    from publisher.store import DATA, ROOT
    DATA.mkdir(parents=True, exist_ok=True)
    # Keep Python/Playwright tracebacks private in the user's own data directory.
    from logging.handlers import RotatingFileHandler
    logging.basicConfig(level=logging.INFO, handlers=[RotatingFileHandler(DATA/'runtime.log',maxBytes=2_000_000,backupCount=2,encoding='utf-8')],format='%(asctime)s %(levelname)s %(message)s')
    if sys.stdout is None:
        sys.stdout = open(DATA/'console.log','a',encoding='utf-8')
    if sys.stderr is None:
        sys.stderr = sys.stdout
    url = f'http://127.0.0.1:{args.port}'
    from publisher.instance import handle_existing, show_existing_window
    from publisher.version import VERSION
    if handle_existing(url, VERSION, headless=args.headless,
            confirm=lambda message:ctypes.windll.user32.MessageBoxW(None,message,'切换到新版墨笺',0x24)==6,
            show_error=lambda message:ctypes.windll.user32.MessageBoxW(None,message,'墨笺启动提示',0x10),
            show_existing=show_existing_window):
        return
    # Port binding happens before recovery, so launching a second EXE cannot
    # mark another running process's jobs as interrupted.
    sock = socket.socket(socket.AF_INET,socket.SOCK_STREAM)
    if hasattr(socket,'SO_EXCLUSIVEADDRUSE'):
        sock.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
    try:
        sock.bind(('127.0.0.1',args.port))
    except OSError:
        sock.close()
        if not args.headless:
            ctypes.windll.user32.MessageBoxW(None,f'本机端口 {args.port} 已被占用，请关闭占用该端口的程序后重试。','墨笺',0x10)
        return
    import uvicorn
    from publisher.app import create_app
    app = create_app()
    server = uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=args.port,loop='asyncio',log_config=None,access_log=False,timeout_graceful_shutdown=10))
    app.state.request_exit = lambda confirmed=False:setattr(server,'should_exit',True)
    if args.headless:
        try:
            server.run(sockets=[sock])
        finally:
            sock.close()
        return
    # A non-daemon thread finishes browser cleanup before the EXE exits.
    service = threading.Thread(target=lambda:server.run(sockets=[sock]),name='mojian-service')
    service.start()
    try:
        for _ in range(100):
            if server.started:
                break
            if not service.is_alive():
                raise RuntimeError('Local service did not start')
            time.sleep(0.1)
        if not server.started:
            raise RuntimeError('Local service startup timed out')
        from publisher.desktop import run_desktop
        run_desktop(app, server, url, ROOT)
    finally:
        server.should_exit=True
        service.join()
        sock.close()


if __name__=='__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    try:
        main()
    except Exception:
        logging.exception('Launcher failed')
        if os.name=='nt' and '--headless' not in sys.argv:
            ctypes.windll.user32.MessageBoxW(None,'程序未能启动，后台已停止。请查看本地数据目录中的 runtime.log。\n独立窗口需要 Microsoft Edge WebView2 运行时。','墨笺',0x10)
        raise
