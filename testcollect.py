r"""独立采集脚本 — 抓 BOSS 直聘岗位，结果只打印到控制台，不落库。

================================ 整体架构 ================================

    本脚本 (Python 进程)
        │
        │  ① HTTP 请求（requests）  ——  /new /eval /scroll /navigate /close
        ▼
    cdp-proxy.mjs (Node 进程，常驻，监听 127.0.0.1:3456)
        │
        │  ② WebSocket，裸 CDP 协议  ——  Target.createTarget / Runtime.evaluate ...
        ▼
    Chrome (开着 --remote-debugging-port=9333，里面登录着你的直聘账号)

三个进程各司其职：Python 只发普通 HTTP，不碰 CDP 协议；Node 桥负责把
HTTP 翻译成 CDP 指令；Chrome 用你的登录态真实访问网站。

--------------------------- 为什么要多一层 Node 桥 -----------------------

直接用 Playwright 连 Chrome 会失败，页面加载 2 秒后自己跳 about:blank。
原因是直聘的反调试脚本：

    Playwright 会对每个 frame 调 CDP 的 Runtime.enable 常驻监听
        → 页面往 console 打一个带 getter 的对象
        → Runtime 域开着时，序列化会触发那个 getter
        → 页面立刻知道"有调试器附加"，调 window.close()，关不掉就跳 about:blank

cdp-proxy.mjs 绕开了这一点：
    - 只用无状态的 Runtime.evaluate（一次性调用，用完即走），从不开 Runtime.enable
    - 用 Fetch.enable 拦截页面对 127.0.0.1:9333 的探测请求，一律返回 ConnectionRefused

--------------------------- 取数策略：接口优先 ---------------------------

方案 A（默认）：在页面上下文里 fetch 搜索接口 /wapi/zpgeek/search/joblist.json
    - 同源请求，cookie 自动带上，不需要额外鉴权
    - salaryDesc 是干净的纯文本，不像 DOM 里的薪资要经过伪元素渲染
    - page 参数真的生效，能翻页
    - 顺带白拿 HR 姓名/职位、技能标签、securityId

方案 B（兜底）：接口不通时退回 DOM 提取
    - 薪资数字在 DOM 里是 CSS 伪元素 ::before 渲染的，textContent 读不到，
      所以用 deepText() 连伪元素一起读

================================ 执行流程 ================================

    collect()
      └─ require_runtime()           探 /health，确认 Node 桥活着
      └─ 遍历 城市 × 关键词
           └─ collect_keyword()
                ├─ new_tab(搜索页)   开一个后台标签页，整个关键词只开这一个
                ├─ 循环 MAX_PAGES 页
                │    ├─ fetch_by_api()    在这个标签页里 fetch 接口拿列表
                │    │    └─ 失败则 fetch_by_dom()  滚动 + 读 DOM
                │    ├─ 按 id 去重，本页全重复就停止翻页
                │    └─ 对每条新岗位：
                │         ├─ sleep 2~5 秒（限速）
                │         └─ fetch_detail()  单独开标签页读 JD，读完关掉
                └─ close_tab(搜索页)
      └─ dump()                      统一打印结果

================================ 跑之前 ==================================

  1. Chrome 开调试端口，user-data-dir 必须是非默认目录（新版 Chrome 的限制）：
       chrome.exe --remote-debugging-port=9333 --user-data-dir="C:\chrome-debug"
  2. 在这个 Chrome 窗口里登录 zhipin.com（登录态存在这个 user-data-dir 里）
  3. 起 Node 桥（另开一个终端，让它一直跑着）：
       $env:BOSSHUNTER_CHROME_PORTS="9333"
       node cdp-proxy.mjs
  4. pip install requests

运行：python testcollect.py
"""

import json
import random
import re
import sqlite3
import sys
import time
from pathlib import Path

import requests

# ================================ 配置 ================================

# Node 桥的地址。注意这里是 3456 不是 9333 ——
# Python 只跟桥说话，桥自己去连 Chrome 的 9333，我们碰不到那个端口。
RUNTIME_URL = "http://127.0.0.1:3456"
DB_PATH = str(Path(__file__).with_name("boss_jobs.db"))  # 与发送脚本共用
BROWSER_PAUSE_MIN = 1.0                 # 每条浏览器指令后的随机停顿（秒）
BROWSER_PAUSE_MAX = 3.0

KEYWORDS = ["AI大模型"]                # 搜索关键词，可以多个
CITIES = ["深圳"]                        # 目标城市，可以多个
MAX_PAGES = 5                            # 每个"城市×关键词"抓几页
PAGE_SIZE = 20                           # 接口每页返回多少条 最多30条
STRIP_WATERMARKS = True                  # 是否清洗 JD 里的投毒词，设 False 可看原文

SEARCH_PAGE = "https://www.zhipin.com/web/geek/job"                      # 搜索页
SEARCH_API = "https://www.zhipin.com/wapi/zpgeek/search/joblist.json"    # 搜索接口
DETAIL_PAGE = "https://www.zhipin.com/job_detail/{job_id}.html"          # 详情页

CITY_CODES = {
    "北京": "101010100", "上海": "101020100", "深圳": "101280600",
    "广州": "101280100", "杭州": "101210100", "成都": "101270100",
    "武汉": "101200100", "南京": "101190100", "西安": "101110100",
}

# 平台会往 JD 正文里随机插这些词干扰爬虫，比如
# "岗位kanzhun职责"、"统招全日制本直聘科"、"熟boss悉pandas"。
# 按长度倒序排列很重要：先删长的，否则 "直聘" 会先把 "BOSS直聘" 咬掉一半。
WATERMARKS = ["来自BOSS直聘", "BOSS直聘", "kanzhun", "直聘", "boss"]


# ========================= 第一层：Node 桥客户端 =========================
# 这一层把桥的 HTTP 接口包成 Python 函数。全部只跟 127.0.0.1:3456 通信。

SESSION = requests.Session()
# 关键一行：不读系统代理环境变量。
# 否则装了 Clash / v2ray / 公司代理的机器上，发往 127.0.0.1 的请求会被代理接管，
# 返回 404 —— 这就是本项目最早连 9222 时报 "Unexpected status 404" 的原因。
SESSION.trust_env = False


def _get(path: str, params: dict | None = None, timeout: float = 30):
    """向桥发一个 GET，返回解析后的 JSON；任何异常都返回 None，让调用方决定怎么办。"""
    try:
        resp = SESSION.get(f"{RUNTIME_URL}{path}", params=params, timeout=timeout)
        return resp.json() if resp.content else None
    except (requests.RequestException, ValueError):
        return None


def _browser_pause() -> None:
    """浏览器动作之间随机停顿，降低连续机械操作的频率。"""
    time.sleep(random.uniform(BROWSER_PAUSE_MIN, BROWSER_PAUSE_MAX))


def new_tab(url: str) -> str | None:
    """开一个后台标签页，返回 targetId。

    桥收到后依次做：Target.createTarget(background) → Target.attachToTarget
    → Fetch.enable（装端口防护）→ 轮询 document.readyState 直到 complete。
    所以这个函数返回时页面已经加载完了，调用方不用自己等。

    targetId 是个字符串，是我们操作这个标签页的唯一凭据 —— Python 侧没有
    page 对象，后续每个操作都要把这串 id 带回给桥。
    """
    data = _get("/new", {"url": url, "background": "1"}, timeout=40)
    _browser_pause()
    return data.get("targetId") if isinstance(data, dict) else None


def navigate(target: str, url: str) -> None:
    """让已有标签页跳转到新 URL（复用标签页，不新开）。同样会等到加载完成。"""
    _get("/navigate", {"target": target, "url": url}, timeout=40)
    _browser_pause()


def close_tab(target: str) -> None:
    """关标签页。开了就要关，否则 Chrome 会越堆越多。"""
    _get("/close", {"target": target}, timeout=10)
    _browser_pause()


def scroll(target: str, y: int = 2000) -> None:
    """向下滚动 y 像素，触发懒加载。桥内部滚完会 sleep 800ms 等新内容渲染。"""
    _get("/scroll", {"target": target, "y": str(y)}, timeout=15)
    _browser_pause()


def evaluate(target: str, expression: str):
    """在指定标签页里执行一段 JS，返回解析好的 Python 对象。整个脚本最核心的函数。

    数据要穿过两层 JSON，所以这里解两次：
        JS 里 return JSON.stringify(结果)     ← 第一层，JS 自己序列化
        桥包成 {"value": "<那个字符串>"}       ← 第二层，桥的响应体
        resp.json()          解掉第二层，拿到 {"value": "..."}
        json.loads(...value) 解掉第一层，拿到真正的数据

    桥那边走的是 CDP 的 Runtime.evaluate，带 returnByValue（值直接回传，不给
    远程对象引用）和 awaitPromise（所以 JS 可以写 async/await，比如去 fetch 接口）。
    """
    try:
        resp = SESSION.post(
            f"{RUNTIME_URL}/eval",
            params={"target": target},
            data=expression.encode("utf-8"),        # JS 原文直接当 body，不是 JSON
            headers={"Content-Type": "text/plain; charset=utf-8"},
            timeout=40,
        )
        data = resp.json()
    except (requests.RequestException, ValueError):
        _browser_pause()
        return None
    _browser_pause()

    # JS 里抛了异常，桥会返回 {"error": "..."} + HTTP 400
    if data.get("error"):
        print(f"  JS 报错: {str(data['error'])[:200]}")
        return None

    try:
        return json.loads(data.get("value") or "null")
    except (json.JSONDecodeError, TypeError):
        print(f"  返回的不是 JSON: {str(data.get('value'))[:120]}")
        return None


def require_runtime() -> None:
    """启动自检：桥没起就直接退出，并告诉用户怎么起。

    /health 是桥唯一不需要先连上 Chrome 就能响应的接口，
    所以它能同时告诉我们：桥活着吗、它连的是哪个 Chrome 端口、端口防护开没开。
    """
    info = _get("/health", timeout=3)
    if not info:
        sys.exit(f"连不上 Node 桥 {RUNTIME_URL}\n"
                 f'先起桥：$env:BOSSHUNTER_CHROME_PORTS="9333"; node cdp-proxy.mjs')
    print(f"Node 桥就绪：Chrome 端口 {info.get('chromePort')}，"
          f"浏览器 {info.get('browserName')}，端口防护 {info.get('portGuard')}")


# ======================= 第二层：在页面里跑的 JS =======================
# 下面这些字符串不在 Python 里执行，而是通过 evaluate() 送进 Chrome 的页面
# 上下文里跑。因为是在页面里，所以它们天然拥有该页面的 cookie、同源权限和 DOM。
# 每段都以 JSON.stringify(...) 结尾，把结果序列化后带回 Python。

# ---- 方案 A：调页面自己用的搜索接口 ----
# 这是页面翻页时自己会发的请求，我们只是在同一个页面里原样再发一次。
# credentials: 'include' 让浏览器带上登录 cookie；因为是同源，不会有跨域问题。
# __URL__ 是占位符，Python 侧用 build_url() 拼好完整地址后替换进来。
JS_API_LIST = r"""
(async () => {
    try {
        const resp = await fetch('__URL__', {
            credentials: 'include',
            headers: { 'accept': 'application/json, text/plain, */*' },
        });
        const raw = await resp.text();

        // 先拿文本再手动 JSON.parse：接口被风控时会返回 HTML 验证页，
        // 直接 resp.json() 会抛异常，拿不到"到底返回了什么"这个关键信息。
        let data;
        try { data = JSON.parse(raw); }
        catch (e) { return JSON.stringify({ ok: false, why: `非 JSON (HTTP ${resp.status}): ${raw.slice(0, 200)}` }); }

        // 直聘的约定：code 为 0 才是成功，非 0 常见是需要安全验证或登录态失效
        if (data.code !== 0) {
            return JSON.stringify({ ok: false, why: `code=${data.code} ${data.message || ''} ${raw.slice(0, 200)}` });
        }

        // 把接口字段映射成本脚本统一的字段名，跟方案 B 的输出保持一致
        return JSON.stringify({
            ok: true,
            jobs: (data.zpData?.jobList || []).map((j) => ({
                title: j.jobName || '',
                salary: j.salaryDesc || '',          // 干净的薪资文本，不用解伪元素
                experience: j.jobExperience || '',
                education: j.jobDegree || '',
                company: j.brandName || '',
                location: [j.cityName, j.areaDistrict, j.businessDistrict].filter(Boolean).join('·'),
                boss: [j.bossName, j.bossTitle].filter(Boolean).join(' '),   // 不用等详情页
                labels: (j.jobLabels || []).concat(j.skills || []).join(' / '),
                jobId: j.encryptJobId || '',
                securityId: j.securityId || '',      // 拼详情页 URL 必需，见 detail_url_of
            })),
        });
    } catch (e) {
        return JSON.stringify({ ok: false, why: String(e).slice(0, 200) });
    }
})()
"""

# ---- 方案 B：DOM 兜底 ----
# 只在接口不通时用。这里的难点是薪资：直聘把数字放在 CSS 伪元素的 content 里
# 渲染，textContent 读不到（所以之前抓出来全是 "-K"，数字凭空消失）。
# deepText() 手动遍历节点树，把 ::before / ::after 的 content 也拼进去。
JS_DOM_LIST = r"""
(() => {
    // 读一个伪元素的 content，去掉 CSS 值外面的引号
    const pseudo = (node, which) => {
        const c = getComputedStyle(node, which).content;
        return (!c || c === 'none' || c === 'normal') ? '' : c.replace(/^["']|["']$/g, '');
    };

    // 深度遍历取文本，顺序：::before → 子节点（递归）→ ::after
    // nodeType 3 = 文本节点，1 = 元素节点
    const deepText = (el) => {
        if (!el) return '';
        let out = '';
        const walk = (node) => {
            if (node.nodeType === 3) { out += node.nodeValue; return; }
            if (node.nodeType !== 1) return;
            out += pseudo(node, '::before');
            node.childNodes.forEach(walk);
            out += pseudo(node, '::after');
        };
        walk(el);
        return out.replace(/\s+/g, ' ').trim();
    };

    const jobs = [];
    document.querySelectorAll('.job-card-wrap').forEach((wrap) => {
        const box = wrap.querySelector('.job-card-box') || wrap;
        const nameEl = box.querySelector('.job-name');
        const href = nameEl ? nameEl.getAttribute('href') : '';
        if (!nameEl || !href) return;                 // 没链接的卡片是广告位，跳过

        const tags = box.querySelectorAll('.tag-list li');   // [0]=经验 [1]=学历
        const salary = deepText(box.querySelector('.job-salary'));
        jobs.push({
            title: deepText(nameEl),
            salary: salary,
            // 连伪元素都读不出数字，说明是另一种反爬（字体映射：数字被换成私有区
            // 字符，靠自定义字体渲染）。带上码点回去，便于判断要不要解字体表。
            salaryCodes: /\d/.test(salary) ? '' : Array.from(salary).map((c) => c.codePointAt(0)).join(','),
            experience: deepText(tags[0]),
            education: deepText(tags[1]),
            company: deepText(box.querySelector('.boss-name') || box.querySelector('.company-name')),
            location: deepText(box.querySelector('.company-location')),
            url: href,
        });
    });
    return JSON.stringify(jobs);
})()
"""

# ---- 详情页提取 ----
# 选择器来自项目里的 src/bosshunter/browser/runtime/site-patterns/zhipin.com.md，
# 那份笔记是踩过坑记下来的（比如 .text-experiece 少个 n 是平台自己拼错的，
# .company-name 会匹配到无关公司名，要用 .sider-company 交叉验证）。
JS_DETAIL = r"""
(() => {
    const txt = (sel) => document.querySelector(sel)?.textContent?.trim() || '';
    // .sider-company 是右侧公司栏，按行拆开后第 2 行（下标 1）是公司名，
    // 第 1 行是"公司基本信息"这个标题
    const sider = txt('.sider-company').split('\n').map((s) => s.trim()).filter(Boolean);
    return JSON.stringify({
        jd: txt('.job-sec-text'),                       // JD 全文
        boss: txt('.job-boss-info').replace(/\s+/g, ' '),
        city: txt('.text-city'),
        degree: txt('.text-degree'),
        company: sider[1] || '',
    });
})()
"""


# ============================== 小工具 ==============================

def build_url(base: str, params: dict) -> str:
    """拼带查询参数的 URL。

    用 requests 自己的 URL 构造器，中文和特殊字符（securityId 里常有 / + =）
    的转义全部由它负责，省得手动调 quote 还可能漏。
    """
    return requests.Request("GET", base, params=params).prepare().url


def clean_text(text: str) -> str:
    """删掉平台插进正文的水印词，并把连续空白压成单个空格。

    注意副作用：裸的"直聘"和"boss"也会被删，公司名里真带这两个字的会被误伤。
    想看原文把 STRIP_WATERMARKS 设成 False。
    """
    if not text:
        return ""
    if STRIP_WATERMARKS:
        for word in WATERMARKS:
            text = re.sub(re.escape(word), "", text, flags=re.IGNORECASE)
    return " ".join(text.split())


def detail_url_of(job_id: str, security_id: str = "") -> str:
    """拼详情页 URL。

    站点笔记明确写了：URL 缺 securityId 会导致页面加载失败或被拦截。
    接口给了这个值就一定带上；DOM 兜底路径拿到的 href 里本来就含它。
    """
    base = DETAIL_PAGE.format(job_id=job_id)
    return build_url(base, {"lid": "", "securityId": security_id}) if security_id else base


# ==================== 第三层：列表抓取（两条路） ====================

def fetch_by_api(target: str, keyword: str, city_code: str, page_no: int) -> list[dict] | None:
    """方案 A：在搜索页的上下文里调接口拿列表。

    返回 []   → 接口通了但这页没数据（正常翻到底）
    返回 None → 接口这条路走不通，调用方应该回退到 DOM
    """
    api_url = build_url(SEARCH_API, {
        "scene": 1,                  # 1 = 求职者端搜索场景
        "query": keyword,
        "city": city_code,
        "page": page_no,
        "pageSize": PAGE_SIZE,
        "sortType": 2,               # 2 = 最新发布
    })
    result = evaluate(target, JS_API_LIST.replace("__URL__", api_url))

    if not result:                   # evaluate 层就失败了（网络、JS 异常）
        return None
    if not result.get("ok"):         # 接口返回了，但不是我们要的（风控/验证/登录失效）
        print(f"  接口不可用: {result.get('why')}")
        return None

    # 统一成本脚本内部的字段结构，跟 fetch_by_dom 的返回保持一致，
    # 这样上层 collect_keyword 不用关心数据是从哪条路来的
    return [{
        "id": j["jobId"],            # 去重用
        "title": j["title"],
        "salary": j["salary"],
        "experience": j["experience"],
        "education": j["education"],
        "company": j["company"],
        "location": j["location"],
        "boss": j["boss"],
        "labels": j["labels"],
        "url": detail_url_of(j["jobId"], j["securityId"]),
    } for j in result.get("jobs", []) if j.get("jobId")]


def fetch_by_dom(target: str) -> list[dict]:
    """方案 B：滚动页面触发懒加载，然后从 DOM 里抠。

    直聘的列表首屏只渲染一部分，不滚动只能拿到前几条，所以要先滚三屏。
    """
    time.sleep(2)                    # 等页面 JS 把首屏渲染完
    for _ in range(3):
        scroll(target)               # 每次滚 2000px，桥内部已 sleep 800ms

    rows = []
    for r in evaluate(target, JS_DOM_LIST) or []:
        if r["salaryCodes"]:
            print(f"  ⚠ 薪资读不到数字，码点: {r['salaryCodes']}（疑似字体映射反爬）")
        # href 形如 /job_detail/abc123.html?lid=xxx&securityId=yyy，取中间那段当 id
        m = re.search(r"/job_detail/([^.?]+)", r["url"])
        rows.append({
            "id": m.group(1) if m else r["url"],
            "title": r["title"], "salary": r["salary"],
            "experience": r["experience"], "education": r["education"],
            "company": r["company"], "location": r["location"],
            "boss": "", "labels": "",          # 这两个 DOM 列表页给不了，留空等详情页补
            "url": "https://www.zhipin.com" + r["url"],
        })
    return rows


def fetch_detail(url: str) -> dict:
    """开一个临时标签页读 JD，读完立刻关掉。

    JD 全文只有详情页有，接口不返回，所以每条岗位都得单独开一次页面 ——
    这也是整个采集最慢的部分。
    """
    target = new_tab(url)
    if not target:
        print(f"  详情页打不开: {url}")
        return {}
    time.sleep(2)                    # /new 虽然等了 load，但 JD 区域可能还在异步渲染
    detail = evaluate(target, JS_DETAIL) or {}
    close_tab(target)
    return detail


# ============================== 主流程 ==============================

def job_identity(job: dict) -> tuple[str, str]:
    """公司和职位都相同才算同一岗位；字段缺失时用岗位 ID 避免误合并。"""
    company = str(job.get("company") or "").strip()
    title = str(job.get("title") or "").strip()
    if company and title:
        return company, title
    return "__job_id__", str(job.get("id") or job.get("url") or "")


def load_existing_identities(db_path: str = DB_PATH) -> set[tuple[str, str]]:
    """读取数据库已有的公司+职位组合，采集时直接跳过历史重复岗位。"""
    if not Path(db_path).exists():
        return set()
    try:
        with sqlite3.connect(db_path) as db:
            rows = db.execute(
                "SELECT company, title FROM jobs WHERE trim(company) != '' AND trim(title) != ''"
            ).fetchall()
    except sqlite3.OperationalError:
        return set()
    return {(str(company).strip(), str(title).strip()) for company, title in rows}


def collect_keyword(city: str, city_code: str, keyword: str,
                    seen: set[tuple[str, str]], results: list[dict]) -> None:
    """抓一个"城市 × 关键词"组合的前 MAX_PAGES 页。

    seen 和 results 从上层传入并跨关键词共享；公司和职位都相同才去重。
    """
    search_params = {"query": keyword, "city": city_code, "sortType": 2}

    # 整个关键词只开这一个搜索标签页。方案 A 下所有页都在这个页面里 fetch，
    # 不需要真的翻页；方案 B 才需要 navigate 过去。
    target = new_tab(build_url(SEARCH_PAGE, search_params))
    if not target:
        print(f"[{city}/{keyword}] 打不开搜索页")
        return

    use_api = True                   # 接口失败一次就整轮放弃，不必每页重试
    try:
        for page_no in range(1, MAX_PAGES + 1):
            rows, source = None, "api"

            if use_api:
                rows = fetch_by_api(target, keyword, city_code, page_no)
                if rows is None:
                    use_api = False
                    print("  → 回退到 DOM 提取")

            if rows is None:         # 走到这里说明接口没戏，用 DOM
                if page_no > 1:      # DOM 模式下翻页得真的把页面导航过去
                    navigate(target, build_url(SEARCH_PAGE, {**search_params, "page": page_no}))
                rows, source = fetch_by_dom(target), "dom"

            fresh = []
            for row in rows:
                identity = job_identity(row)
                if identity in seen:
                    continue
                seen.add(identity)
                fresh.append(row)
            print(f"[{city}/{keyword}] 第 {page_no} 页({source}): {len(rows)} 条，新增 {len(fresh)} 条")

            if not rows:
                break                # 这页真没数据，翻到底了
            if not fresh:
                print("  本页岗位在数据库或本轮结果中均已存在，已跳过")
                # API 的 page 参数可靠，继续检查下一页是否有新岗位；DOM 回退模式
                # 无法可靠确认翻页是否生效，避免反复读取同一批数据。
                if source == "api":
                    time.sleep(random.uniform(3.0, 6.0))
                    continue
                break

            for row in fresh:
                time.sleep(random.uniform(2.0, 5.0))     # 详情页之间限速，别把账号刷进风控

                detail = fetch_detail(row["url"])
                # 列表数据打底，详情页数据补缺 —— 列表有值就用列表的，
                # 因为接口给的比 DOM 抠的可靠
                results.append({
                    **row,
                    "company": row["company"] or detail.get("company", ""),
                    "city": detail.get("city") or city,
                    "education": row["education"] or detail.get("degree", ""),
                    "boss": row["boss"] or clean_text(detail.get("boss", "")),
                    "jd": clean_text(detail.get("jd", "")),
                    "source": source,                    # 标记这条是 api 还是 dom 来的
                })
                print(f"  + {row['company']} | {row['title']} | {row['salary']}")

            time.sleep(random.uniform(3.0, 6.0))         # 翻页间隔
    finally:
        # 不管中途出什么错，搜索标签页都要关掉，不然 Chrome 里会残留
        close_tab(target)


def dump(results: list[dict]) -> None:
    """把结果完整打印到控制台。采集过程中已经逐条打过简报，这里是详细版。"""
    print("\n" + "=" * 78)
    print(f"共采集 {len(results)} 条岗位")
    print("=" * 78)
    for i, job in enumerate(results, 1):
        print(f"\n[{i}] {job['title']}  |  {job['salary']}")
        print(f"    公司   : {job['company']}")
        print(f"    城市   : {job['city']}  {job['location']}")
        print(f"    要求   : {job['experience']} / {job['education']}")
        print(f"    HR     : {job['boss']}")
        if job["labels"]:
            print(f"    标签   : {job['labels']}")
        print(f"    来源   : {job['source']}")
        print(f"    链接   : {job['url']}")
        if job["jd"]:
            print(f"    JD     : {job['jd'][:300]}{'...' if len(job['jd']) > 300 else ''}")
    print("\n" + "=" * 78)


def save_to_sqlite(results: list[dict], db_path: str = DB_PATH) -> None:
    """把采集结果写入 SQLite；重复采集只刷新岗位详情，不覆盖消息和发送状态。"""
    with sqlite3.connect(db_path) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                company TEXT NOT NULL DEFAULT '',
                salary TEXT NOT NULL DEFAULT '',
                city TEXT NOT NULL DEFAULT '',
                location TEXT NOT NULL DEFAULT '',
                experience TEXT NOT NULL DEFAULT '',
                education TEXT NOT NULL DEFAULT '',
                boss TEXT NOT NULL DEFAULT '',
                labels TEXT NOT NULL DEFAULT '',
                jd TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'api',
                greeting TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                collected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                sent_at TEXT
            )
        """)
        db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_company_title
            ON jobs(company, title)
            WHERE trim(company) != '' AND trim(title) != ''
        """)
        # 兼容上一版脚本写入的旧状态。
        db.execute("UPDATE jobs SET status='pending' WHERE status='collected'")
        for job in results:
            existing = db.execute(
                "SELECT id FROM jobs WHERE company = ? AND title = ? LIMIT 1",
                (job["company"].strip(), job["title"].strip()),
            ).fetchone()
            if existing:
                # 保留原记录 ID、pending/filter/sent 状态和发送历史，只刷新岗位详情。
                db.execute("""
                    UPDATE jobs SET
                        salary=?, city=?, location=?, experience=?, education=?,
                        boss=?, labels=?, jd=?, url=?, source=?, collected_at=CURRENT_TIMESTAMP
                    WHERE id=?
                """, (
                    job["salary"], job["city"], job["location"], job["experience"],
                    job["education"], job["boss"], job["labels"], job["jd"],
                    job["url"], job["source"], existing[0],
                ))
                continue
            db.execute("""
                INSERT INTO jobs (
                    id, title, company, salary, city, location, experience,
                    education, boss, labels, jd, url, source, status, collected_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title, company=excluded.company,
                    salary=excluded.salary, city=excluded.city,
                    location=excluded.location, experience=excluded.experience,
                    education=excluded.education, boss=excluded.boss,
                    labels=excluded.labels, jd=excluded.jd, url=excluded.url,
                    source=excluded.source, collected_at=CURRENT_TIMESTAMP
            """, (
                job["id"], job["title"], job["company"], job["salary"],
                job["city"], job["location"], job["experience"],
                job["education"], job["boss"], job["labels"], job["jd"],
                job["url"], job["source"],
            ))
    print(f"采集结果已保存到 {db_path}（{len(results)} 条）")


def collect() -> None:
    """入口：自检 → 遍历所有城市×关键词 → 打印结果。"""
    require_runtime()
    print(f"开始采集（结果将保存到 {DB_PATH}）...")

    seen = load_existing_identities()  # 数据库历史 + 本轮共享的公司/职位组合
    results: list[dict] = []         # 所有抓到的岗位，最后统一打印
    print(f"数据库已有 {len(seen)} 个公司+职位组合，将自动跳过重复岗位")

    for city in CITIES:
        code = CITY_CODES.get(city)
        if not code:
            print(f"⚠ 未识别的城市: {city}，已跳过")
            continue
        for keyword in KEYWORDS:
            collect_keyword(city, code, keyword, seen, results)

    save_to_sqlite(results)
    dump(results)


if __name__ == "__main__":
    collect()
