"""直接读取 SQLite 中 status=pending 的岗位并发送 greeting.txt。"""

import argparse
import json
import random
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import requests

CHAT_SELECTOR = (
    'a[redirect-url*="/web/geek/chat"],a[data-url*="/friend/add"],'
    'a.btn-startchat,[ka="job_detail_chat"],[ka^="go_chat"],'
    '[ka*="gochat"],.op-btn-chat,.btn-startchat-wrap'
)

SESSION = requests.Session()
SESSION.trust_env = False
SETTINGS = None


def request_json(method: str, path: str, *, params=None, body=None, timeout=40):
    try:
        response = SESSION.request(
            method, f"{SETTINGS.runtime_url}{path}", params=params,
            data=body.encode("utf-8") if isinstance(body, str) else body,
            headers={"Content-Type": "text/plain; charset=utf-8"}, timeout=timeout,
        )
        data = response.json() if response.content else {}
        # /targets 返回数组，其余接口通常返回对象；两种都属于正常响应。
        has_error = isinstance(data, dict) and data.get("error")
        if response.status_code >= 400 or has_error:
            return None
        return data
    except (requests.RequestException, ValueError):
        return None


def evaluate(target: str, expression: str):
    data = request_json("POST", "/eval", params={"target": target}, body=expression)
    browser_pause()
    if not data:
        return None
    value = data.get("value")
    try:
        return json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError:
        return value


def new_tab(url: str):
    data = request_json("GET", "/new", params={"url": url, "background": "1"})
    browser_pause()
    return data.get("targetId") if data else None


def close_tab(target: str):
    request_json("GET", "/close", params={"target": target}, timeout=10)
    browser_pause()


def get_targets():
    data = request_json("GET", "/targets", timeout=10)
    browser_pause()
    return data if isinstance(data, list) else []


def navigate(target: str, url: str) -> bool:
    result = request_json("GET", "/navigate", params={"target": target, "url": url}) is not None
    browser_pause()
    return result


def click_at(target: str, selector_or_xy: str) -> bool:
    result = request_json("POST", "/clickAt", params={"target": target}, body=selector_or_xy) is not None
    browser_pause()
    return result


def press_key(target: str, key: str) -> bool:
    result = request_json("POST", "/key", params={"target": target}, body=key) is not None
    browser_pause()
    return result


def type_text(target: str, text: str) -> bool:
    result = request_json("POST", "/type", params={"target": target, "human": "1"}, body=text) is not None
    browser_pause()
    return result


def browser_pause():
    """每条浏览器指令完成后停顿，再允许下一条指令开始。"""
    time.sleep(random.uniform(SETTINGS.browser_pause_min, SETTINGS.browser_pause_max))


def require_runtime():
    data = request_json("GET", "/health", timeout=3)
    if not data or not data.get("connected"):
        sys.exit("CDP 桥未连接 Chrome。请先启动带调试端口的 Chrome，再运行 runtime/cdp-proxy.mjs")
    print(f"已连接 Chrome 调试端口 {data.get('chromePort')} ({data.get('browserName')})")


def ensure_db(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', company TEXT NOT NULL DEFAULT '',
            salary TEXT NOT NULL DEFAULT '', city TEXT NOT NULL DEFAULT '', location TEXT NOT NULL DEFAULT '',
            experience TEXT NOT NULL DEFAULT '', education TEXT NOT NULL DEFAULT '', boss TEXT NOT NULL DEFAULT '',
            labels TEXT NOT NULL DEFAULT '', jd TEXT NOT NULL DEFAULT '', url TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'api', greeting TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '', collected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, sent_at TEXT
        )
    """)
    db.execute("UPDATE jobs SET status='pending' WHERE status='collected'")


def rows_for_send(db, job_ids, limit):
    sql = "SELECT * FROM jobs WHERE status = 'pending'"
    params = []
    if job_ids:
        sql += f" AND id IN ({','.join('?' for _ in job_ids)})"
        params.extend(job_ids)
    sql += " ORDER BY collected_at DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return db.execute(sql, params).fetchall()


CLICK_CHAT_JS = r"""
(() => {
  const visible = el => { const r=el.getBoundingClientRect(),s=getComputedStyle(el);
    return !!(r.width&&r.height&&s.display!=='none'&&s.visibility!=='hidden'&&s.pointerEvents!=='none'); };
  const items = Array.from(document.querySelectorAll('__SELECTOR__')).filter(visible);
  const score = el => {
    const t=(el.innerText||el.textContent||'').trim(), r=el.getAttribute('redirect-url')||'',
      d=el.getAttribute('data-url')||'', k=el.getAttribute('ka')||'';
    return (t.includes('沟通')?80:0)+(r.includes('/web/geek/chat')?300:0)+
      (d.includes('/friend/add')?250:0)+(k.includes('chat')||k.includes('go')?60:0);
  };
  items.sort((a,b)=>score(b)-score(a)); const btn=items[0];
  if(!btn) return JSON.stringify({success:false,error:'no_chat_button'});
  btn.scrollIntoView({block:'center'}); const rect=btn.getBoundingClientRect(); btn.click();
  return JSON.stringify({success:true,x:rect.x+rect.width/2,y:rect.y+rect.height/2,
    redirectUrl:btn.getAttribute('redirect-url')||'',text:(btn.innerText||btn.textContent||'').trim()});
})()
""".replace("__SELECTOR__", CHAT_SELECTOR.replace("'", "\\'"))


def chat_matches(target: str, job) -> bool:
    expected = json.dumps({
        "id": job["id"],
        "company": str(job["company"] or "").replace("...", "").replace("…", ""),
        "title": job["title"],
        "boss": str(job["boss"] or "").split()[0] if job["boss"] else "",
    }, ensure_ascii=False)
    state = evaluate(target, f"""
    (() => {{ const e={expected}, n=s=>String(s||'').replace(/\\s+/g,'').toLowerCase();
      const roots=Array.from(new Set([
        document.querySelector('.friend-content.selected'),
        document.querySelector('.chat-conversation'),
        document.querySelector('.chat-header'),
        document.querySelector('.chat-record')
      ].filter(Boolean)));
      const text=n(roots.map(x=>x.innerText||x.textContent||'').join(' '));
      const html=roots.map(x=>x.outerHTML||'').join(' ');
      const ok=location.pathname.includes('/web/geek/chat') &&
        ((e.id&&(location.href.includes(e.id)||html.includes(e.id))) ||
         (e.boss&&text.includes(n(e.boss))) ||
         (e.company&&text.includes(n(e.company))) ||
         (e.title&&text.includes(n(e.title))));
      return JSON.stringify({{ok}}); }})()
    """) or {}
    return bool(state.get("ok"))


def wait_chat(target: str, job, greeting: str, attempts=24):
    for _ in range(attempts):
        if chat_matches(target, job) or message_state(target, greeting) in {
            "pending", "delivered", "failed"
        }:
            return target
        for candidate in get_targets():
            candidate_id = str(candidate.get("targetId") or "")
            candidate_url = str(candidate.get("url") or "")
            if candidate_id and candidate_id != target and "/web/geek/chat" in candidate_url:
                # BOSS 经常复用运行前已存在的聊天标签页，不能排除旧 targetId。
                if chat_matches(candidate_id, job) or message_state(candidate_id, greeting) in {
                    "pending", "delivered", "failed"
                }:
                    close_tab(target)
                    return candidate_id
        time.sleep(0.5)
    return None


def handle_first_contact(target: str, greeting: str):
    state = evaluate(target, """
    (()=>{const v=e=>e&&!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length);
      const d=Array.from(document.querySelectorAll('.dialog-wrap.startchat-dialog')).find(v);
      const p=Array.from(document.querySelectorAll('.greet-boss-pop,.greet-pop')).find(v);
      return JSON.stringify({start:!!d,preset:!!p});})()
    """) or {}
    if state.get("start"):
        if not click_at(target, ".dialog-wrap.startchat-dialog textarea.input-area"):
            return False, "首次沟通输入框不可用"
        if not press_key(target, "SelectAll") or not press_key(target, "Backspace") or not type_text(target, greeting):
            return False, "首次沟通消息输入失败"
        button = '.dialog-wrap.startchat-dialog .send-message,.dialog-wrap.startchat-dialog [ka="dialog_confirm"],.dialog-wrap.startchat-dialog .btn-sure,.dialog-wrap.startchat-dialog .btn-send'
        if not click_at(target, button):
            return False, "首次沟通发送按钮不可用"
        return True, "submitted"
    if state.get("preset"):
        if not click_at(target, '.greet-boss-pop [ka="dialog_confirm"],.greet-boss-pop .btn-sure,.greet-pop [ka="dialog_confirm"],.greet-pop .btn-sure'):
            return False, "预设招呼语确认按钮不可用"
    return True, "continue"


def message_state(target: str, greeting: str):
    escaped = json.dumps(greeting, ensure_ascii=False)
    return evaluate(target, f"""
    (()=>{{const n=s=>String(s||'').replace(/[\\u200b-\\u200f\\ufeff]/g,'').replace(/\\s+/g,' ').trim();
      const expected=n({escaped}); const nodes=Array.from(document.querySelectorAll('.message-content,.item-myself,.message-item,.chat-message'));
      const hit=nodes.find(x=>n(x.innerText||x.textContent).includes(expected));
      if(!hit)return 'missing'; const t=n(hit.innerText||hit.textContent);
      return /发送失败|重试|重新发送/.test(t)?'failed':/发送中/.test(t)?'pending':'delivered';}})()
    """)


def send_one(job):
    target = new_tab(job["url"])
    if not target:
        return False, "岗位页打开失败"
    try:
        time.sleep(random.uniform(3, 6))
        page = evaluate(target, "document.body ? document.body.innerText.slice(0,5000) : ''") or ""
        if "访问的页面不存在" in page or "Oops!" in page:
            return False, "岗位不存在或已下架"
        result = evaluate(target, CLICK_CHAT_JS) or {}
        if not result.get("success"):
            return False, result.get("error", "找不到沟通按钮")
        time.sleep(3)
        popup_ok, action = handle_first_contact(target, job["greeting"])
        if not popup_ok:
            return False, action
        redirect = result.get("redirectUrl", "")
        if action == "continue" and redirect.startswith("/web/geek/chat"):
            navigate(target, urljoin("https://www.zhipin.com", redirect))
        chat_target = wait_chat(target, job, job["greeting"])
        if not chat_target:
            return False, "未能确认进入目标岗位聊天，请人工检查避免重复发送"
        target = chat_target
        if action != "submitted":
            if message_state(target, job["greeting"]) == "delivered":
                return True, "消息已存在"
            if not click_at(target, "#chat-input"):
                return False, "找不到聊天输入框"
            if not press_key(target, "SelectAll") or not press_key(target, "Backspace") or not type_text(target, job["greeting"]):
                return False, "输入消息失败"
            if not click_at(target, ".btn-send:not(.disabled)"):
                return False, "发送按钮不可用"
        for _ in range(20):
            time.sleep(0.5)
            state = message_state(target, job["greeting"])
            if state == "failed":
                return False, "BOSS 显示消息发送失败"
            if state == "delivered":
                time.sleep(1.5)
                return (True, "发送成功") if message_state(target, job["greeting"]) == "delivered" else (False, "消息未稳定保留")
        return False, "发送结果无法验证，请人工检查避免重复发送"
    finally:
        close_tab(target)


def load_message(path: Path) -> str:
    if not path.exists():
        sys.exit(f"消息模板文件不存在：{path}")
    try:
        message = path.read_text(encoding="utf-8-sig").strip()
    except OSError as exc:
        sys.exit(f"读取消息模板失败：{exc}")
    if not message:
        sys.exit(f"消息模板文件为空：{path}")
    return message


def send_pending_jobs(db, args):
    rows = rows_for_send(db, None, args.limit)
    if not rows:
        print("没有 status=pending 的待发送岗位")
        return
    greeting = load_message(args.message_file)
    require_runtime()
    print(f"准备向 {len(rows)} 个 pending 岗位发送 {args.message_file.name} 中的消息")
    for index, row in enumerate(rows, 1):
        job = dict(row)
        job["greeting"] = greeting
        print(f"[{index}/{len(rows)}] {job['company']} | {job['title']}")
        ok, detail = send_one(job)
        if ok:
            db.execute("UPDATE jobs SET status='sent', attempts=attempts+1, last_error='', sent_at=CURRENT_TIMESTAMP WHERE id=?", (job["id"],))
            print(f"  ✓ {detail}")
        else:
            # 保持 pending，排除不确定状态前允许下次人工确认后重试。
            db.execute("UPDATE jobs SET attempts=attempts+1, last_error=? WHERE id=?", (detail, job["id"]))
            print(f"  ✗ {detail}")
        db.commit()
        if index < len(rows):
            time.sleep(random.uniform(args.interval_min, args.interval_max))


def build_parser():
    """所有常用参数集中在这里；修改 default 后可直接运行本文件。"""
    parser = argparse.ArgumentParser(description="向数据库中 pending 状态的 BOSS 岗位发送招呼语")
    parser.add_argument("--db", type=Path, default=Path(__file__).with_name("boss_jobs.db"))
    parser.add_argument("--message-file", type=Path, default=Path(__file__).with_name("greeting.txt"))
    parser.add_argument("--limit", type=int, default=5, help="最多发送数量；把 default 改为 None 表示全部")
    parser.add_argument("--interval-min", type=float, default=10.0, help="岗位之间最短等待秒数")
    parser.add_argument("--interval-max", type=float, default=50.0, help="岗位之间最长等待秒数")
    parser.add_argument("--browser-pause-min", type=float, default=1.0, help="浏览器指令后最短等待秒数")
    parser.add_argument("--browser-pause-max", type=float, default=3.0, help="浏览器指令后最长等待秒数")
    parser.add_argument("--runtime-url", default="http://127.0.0.1:3456", help="CDP 桥地址")
    return parser


def main():
    global SETTINGS
    SETTINGS = build_parser().parse_args()
    if SETTINGS.limit is not None and SETTINGS.limit <= 0:
        sys.exit("--limit 必须是正整数，或把 build_parser 中的 default 改为 None")
    if SETTINGS.interval_min < 0 or SETTINGS.interval_min > SETTINGS.interval_max:
        sys.exit("发送间隔配置不正确")
    if SETTINGS.browser_pause_min < 0 or SETTINGS.browser_pause_min > SETTINGS.browser_pause_max:
        sys.exit("浏览器指令间隔配置不正确")
    SETTINGS.db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(SETTINGS.db) as db:
        db.row_factory = sqlite3.Row
        ensure_db(db)
        send_pending_jobs(db, SETTINGS)


if __name__ == "__main__":
    main()
