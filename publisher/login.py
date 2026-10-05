"""Classify visible WeChat login evidence; never substitute a configured name."""
from urllib.parse import parse_qs, urlsplit
import time
from datetime import datetime, timezone


ACCOUNT_PROBE = r'''() => {
    const visible = e => !!e && !!e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
    const text = e => (e.innerText || e.textContent || '').replace(/\s+/g, ' ').trim();
    const names = [];
    const selectors = '.weui-desktop-account__nickname, .weui-desktop-account__name, .account_nickname, [class*="nick_name"], [class*="nickname"], [class*="user_name"], [class*="user-name"], [class*="username"]';
    for (const el of document.querySelectorAll(selectors)) {
        if (visible(el)) {
            const value = text(el);
            if (value && value.length <= 120) names.push(value);
        }
    }
    // The newer home page has a profile card containing the avatar, name,
    // original-content count and follower count, rather than a header nickname.
    // Restrict this fallback to the home profile card, never article body text.
    if (location.pathname === '/cgi-bin/home') {
        for (const img of document.querySelectorAll('img')) {
            if (!visible(img)) continue;
            let card = img.parentElement;
            for (let depth = 0; card && depth < 5; depth++, card = card.parentElement) {
                const value = text(card);
                if (value.length > 500) break;
                if (/原创内容/.test(value) && /总用户数/.test(value)) {
                    const name = value.split('原创内容')[0].trim();
                    if (name && name.length <= 120 && !/首页|内容管理|互动管理/.test(name)) names.push(name);
                    break;
                }
            }
        }
    }
    const alerts = [...document.querySelectorAll('[role="alert"], .weui-desktop-toast, .weui-desktop-msg__title, .login_err, .login__type__container')]
        .filter(visible).map(text).filter(v => v.length < 300);
    const loginForm = [...document.querySelectorAll('.login_box, .login_qrcode, .login__type__container__scan, .login__type__container, #js_qrcode')].some(visible);
    return {names:[...new Set(names)], expired:alerts.some(v=>/登录已过期|登录超时|请重新登录|登录失效/.test(v)), login_form:loginForm};
}'''


def classify_login(url, probe, configured='', ever_logged=False, opened=True):
    base = {'open':opened, 'logged_in':False, 'account_name':'', 'configured_account':configured,
            'account_matches':None, 'state':'closed', 'label':'微信窗口未打开',
            'message':'打开微信窗口后，工具会自动检测登录状态。'}
    if not opened:
        return base
    parts=urlsplit(url)
    at_wechat=parts.hostname=='mp.weixin.qq.com'
    has_token=at_wechat and bool(parse_qs(parts.query).get('token'))
    names=list(dict.fromkeys(n.strip() for n in probe.get('names',[]) if isinstance(n,str) and n.strip()))
    if probe.get('expired'):
        return {**base,'state':'expired','label':'登录已失效','message':'微信登录已失效，请在微信窗口重新扫码登录。'}
    if probe.get('error'):
        return {**base,'state':'unknown','label':'暂时无法检测','message':'暂时无法读取微信页面，请稍后刷新登录状态。'}
    if not at_wechat:
        return {**base,'state':'unknown','label':'当前不在微信后台','message':'请打开微信后台，再刷新登录状态。'}
    if ever_logged and (probe.get('login_form') or not has_token):
        return {**base,'state':'expired','label':'登录已失效','message':'微信登录已失效，请在微信窗口重新扫码登录。'}
    if probe.get('login_form') or not has_token:
        return {**base,'state':'waiting_scan','label':'等待扫码登录','message':'请在微信窗口扫码，并在手机上确认登录。'}
    if len(names)!=1:
        return {**base,'state':'unverified','label':'已进入后台，名称待核对','message':'已进入微信后台，暂未识别到唯一公众号名称。请点击“刷新登录状态”。'}
    actual=names[0]
    matches=actual==configured if configured else None
    return {**base,'logged_in':True,'account_name':actual,'account_matches':matches,
            'state':'mismatch' if matches is False else 'logged_in',
            'label':'已登录，账号不一致' if matches is False else '已登录',
            'message':f'当前登录：{actual}；设置中为：{configured}。请切换公众号或修改名称后保存。' if matches is False else
                      '扫码登录成功，公众号名称与设置一致。' if matches else '扫码登录成功，请把当前公众号名称填入下方并保存设置。'}


class SessionIdentity:
    """Remember a recently verified identity only within the same login session.

    The cache is for status display. A task's fresh verification never uses it.
    Session tokens remain in memory and are never returned to the workbench.
    """
    def __init__(self, clock=time.monotonic, max_age=600):
        self.clock = clock
        self.max_age = max_age
        self.clear()

    def clear(self):
        self.session = ''
        self.account = ''
        self.confirmed_at = ''
        self.confirmed_clock = 0

    def resolve(self, url, probe, configured='', ever_logged=False, opened=True, fresh=False):
        result = classify_login(url, probe, configured, ever_logged, opened)
        parts = urlsplit(url)
        session = parse_qs(parts.query).get('token',[''])[0] if parts.hostname=='mp.weixin.qq.com' else ''
        if result['logged_in']:
            self.session, self.account = session, result['account_name']
            self.confirmed_clock = self.clock()
            self.confirmed_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
            return {**result, 'identity_source':'page', 'verified_at':self.confirmed_at}
        if not opened or result['state'] in {'expired','waiting_scan'} or session != self.session:
            self.clear()
        if (not fresh and result['state']=='unverified' and not probe.get('names')
                and session and session==self.session and self.account
                and self.clock()-self.confirmed_clock <= self.max_age):
            remembered = classify_login(url, {'names':[self.account]}, configured, ever_logged, opened)
            if remembered['account_matches'] is not False:
                remembered.update(state='connected', label='登录会话已核对',
                    message='当前在微信编辑页或其他后台页面，沿用本次登录会话已核对的公众号。执行任务前会重新核对账号。')
            return {**remembered, 'identity_source':'session', 'verified_at':self.confirmed_at}
        return result
