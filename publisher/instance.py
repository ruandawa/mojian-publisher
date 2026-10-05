"""Local instance handover. Never silently run an older version."""
import json
import time
import urllib.error
import urllib.request


def get_json(url, **kwargs):
    with urllib.request.urlopen(urllib.request.Request(url, **kwargs), timeout=3) as response:
        return json.load(response)


def version_tuple(value):
    try:
        return tuple(int(part) for part in value.split('.'))
    except (ValueError, AttributeError):
        return (0,)


def show_existing_window(url):
    """Bring the existing WebView2 window forward without opening a browser tab."""
    try:
        token = get_json(url + '/api/bootstrap')['token']
        get_json(url + '/api/system/show', method='POST', data=b'',
                 headers={'X-Mojian-Token': token})
        return True
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError):
        return False


def handle_existing(url, version, *, headless=False, confirm=lambda message:False,
                    show_error=lambda message:None, show_existing=None,
                    open_page=lambda url:None):
    try:
        health = get_json(url+'/api/health')
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False
    if health.get('app')!='mojian-publisher':
        return False
    actual = health.get('version','未知')
    if headless:
        return True
    if version_tuple(actual)>=version_tuple(version):
        if not (show_existing and show_existing(url)):
            open_page(url)
        return True
    if not confirm(f'正在运行的是 v{actual}，你打开的是 v{version}。\n\n是否退出旧版并切换到新版？\n\n文章、设置和任务会保留。工具打开的微信窗口将关闭，请先保存微信页面上手动编辑的内容。'):
        return True
    try:
        token = get_json(url+'/api/bootstrap')['token']
        get_json(url+'/api/system/exit', method='POST',
                 data=b'{"confirmed":true}',
                 headers={'X-Mojian-Token':token,'Content-Type':'application/json'})
        deadline = time.monotonic()+20
        while time.monotonic()<deadline:
            try:
                get_json(url+'/api/health')
            except (urllib.error.URLError, TimeoutError, OSError):
                return False
            time.sleep(.25)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError):
        pass
    show_error('旧版未能退出，新版没有启动。\n\n如果关闭按钮和“退出墨笺”均无效：先保存能保存的内容，再按 Ctrl+Shift+Esc 打开任务管理器，结束“墨笺公众号助手”进程，然后重新打开新版 EXE。\n\n结束进程会丢失未保存输入；已保存的文章和设置保留。')
    return True
