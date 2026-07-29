"""豆瓣抓取：HTTP 直连 + DOUBAN_COOKIE 登录态（无 WAF / 无 Playwright，比雪球更简单）。

流程（沿用本地 douban_speaker_bot.py 验证过的朴素可靠法）：
  1. find_latest_post : 小组页定位楼主最新帖 → 拼 ?author=1 只看楼主模式
  2. fetch_posts      : 翻到末页抓楼主发言（末页+倒数第2页足够）
  3. parse_reply_blocks: 从 reply-doc 抽目标用户发言 + 图片 + 引用 + 时间
  4. normalize        : 去重 + 结构标准化（对齐 xueqiu-tracker.normalize）
"""
import re
import time
import datetime

import requests
from bs4 import BeautifulSoup

from config import (DOUBAN_COOKIE, DOUBAN_GROUP_URLS, DOUBAN_TARGET_USER,
                    DOUBAN_USER_STATUSES_URL, SCRAPE_MODE, PAGES)

_UAS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/119.0.0.0 Safari/537.36',
]
# 新模式拉取豆瓣话题评论所需移动端 UA（rexxar API 校验）
_MOBILE_UA = ('Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) '
              'AppleWebKit/605.1.15 Mobile/15E148')
SESSION = requests.Session()
SESSION.trust_env = False


def http_get(url, cookie="", timeout=20, retries=2):
    last = None
    for attempt, ua in enumerate([None] + _UAS):
        if attempt > 0:
            time.sleep(3)
        headers = {
            'User-Agent': ua if ua else _UAS[0],
            'Accept-Language': 'zh-CN,zh;q=0.9',
            'Referer': 'https://www.douban.com/',
        }
        if cookie:
            headers['Cookie'] = cookie
        try:
            r = SESSION.get(url, headers=headers, timeout=timeout,
                            allow_redirects=True, verify=False)
            if r.status_code == 200 and 'sec.douban.com' not in r.url and len(r.text) > 1000:
                return r
            last = r
        except Exception as e:
            last = e
    return last


def _parse_topic_page(html_text):
    soup = BeautifulSoup(html_text, 'lxml')
    h1 = soup.find('h1')
    if h1:
        return re.sub(r'豆瓣', '', h1.get_text(strip=True)).strip()
    title_tag = soup.find('title')
    if title_tag:
        return title_tag.get_text(strip=True).replace('豆瓣', '').strip()
    return "未知话题"


def find_latest_post(group_url, target_user):
    """小组页定位楼主最新帖，返回 author=1 URL 字典；失败返回 None。"""
    r = http_get(group_url, cookie=DOUBAN_COOKIE)
    if not hasattr(r, 'status_code') or r.status_code != 200 or '/group/topic/' not in r.text:
        print(f"  ⚠️ 小组页不可达: {getattr(r, 'status_code', 'ERR')} / sec风控={ 'sec.douban.com' in getattr(r,'url','') }")
        return None
    soup = BeautifulSoup(r.text, 'lxml')
    rows = []
    for tr in soup.select('tr'):
        a_topic = tr.find('a', href=re.compile(r'/group/topic/\d+'))
        a_author = tr.find('a', href=re.compile(r'/people/'))
        if not a_topic or not a_author:
            continue
        m = re.search(r'/group/topic/(\d+)', a_topic.get('href', ''))
        if not m:
            continue
        title = (a_topic.get('title') or a_topic.get_text(strip=True)).strip()
        if not title or title.endswith('回复') or re.fullmatch(r'\d+', title):
            continue
        rows.append({'tid': m.group(1), 'title': title, 'author': a_author.get_text(strip=True)})
    if not rows:
        print("  ❌ 未解析到任何话题行")
        return None
    latest = next((x for x in rows if x['author'] == target_user), None)
    if not latest:
        print(f"  ⚠️ 未找到 {target_user} 的帖，取全组最新兜底")
        latest = rows[0]
    topic_url = f"https://www.douban.com/group/topic/{latest['tid']}/"
    return {
        'title': latest['title'],
        'url': topic_url + "?author=1",
        'tid': latest['tid'],
        'author_confirmed': (latest['author'] == target_user),
    }


def parse_reply_blocks(soup, target_user):
    """从 BeautifulSoup 抽目标用户发言，含图片/引用/时间。"""
    posts = []
    for block in soup.find_all('div', class_='reply-doc'):
        author_elem = block.find('a', href=re.compile(r'people'))
        if not author_elem or author_elem.get_text(strip=True) != target_user:
            continue
        reply_id = block.get('id', '')
        content_div = block.find('div', class_='reply-content')
        if not content_div:
            continue
        content = content_div.get_text('\n', strip=True)
        content = re.sub(r'\n{3,}', '\n\n', content)
        content = re.sub(r'[ \t]+', ' ', content).strip()
        if not content or len(content) < 2:
            continue
        img_urls = []
        for img in content_div.find_all('img'):
            src = img.get('src', '').strip()
            if src and 'icon' not in src and 'avatar' not in src:
                img_urls.append(src)
        if img_urls:
            content += '\n\n' + '\n'.join(f'![图片]({u})' for u in img_urls)
        time_elem = block.find('span', class_='pubtime')
        post_time = time_elem.get_text(strip=True) if time_elem else "未知时间"
        quote_text = ""
        quote_div = block.find('div', class_=re.compile(r'^reply-quote'))
        if quote_div:
            al = quote_div.select_one('.pubdate a')
            qa = al.get_text(strip=True) if al else ""
            qc = ""
            for sel in ['.all.ref-content', '.short.ref-content']:
                el = quote_div.select_one(sel)
                if el and el.get_text(' ', strip=True):
                    qc = re.sub(r'[ \t\n]+', ' ', el.get_text(' ', strip=True)).strip()
                    break
            if qc:
                quote_text = f"（引用 @{qa}）{qc}" if qa else f"（引用）{qc}"
        now = datetime.datetime.now()
        if re.match(r'\d{4}-\d{2}-\d{2}', post_time):
            post_date = post_time[:10]
        elif re.match(r'\d{2}-\d{2}', post_time):
            post_date = f"{now.year}-{post_time[:5]}"
        else:
            post_date = now.strftime('%Y-%m-%d')
        tm = re.search(r'\d{2}:\d{2}(?::\d{2})?', post_time)
        st = tm.group() if tm else post_time[:8]
        if len(st) == 5:
            st += ':00'
        posts.append({
            'id': reply_id, 'author': target_user, 'content': content,
            'time': post_time, 'sortable_time': st, 'quote': quote_text, 'date': post_date,
        })
    return posts


def fetch_posts(topic_url, target_user, pages=PAGES):
    """翻到末页抓楼主发言，返回当天发言列表。"""
    r = http_get(topic_url, cookie=DOUBAN_COOKIE)
    if not hasattr(r, 'status_code') or r.status_code != 200:
        print(f"  ❌ HTTP {getattr(r,'status_code','ERR')}")
        return []
    if '没有访问权限' in r.text:
        print("  ❌ 没有访问权限")
        return []
    soup = BeautifulSoup(r.text, 'lxml')
    starts = set()
    for a in soup.select('.paginator a'):
        m = re.search(r'start=(\d+)', a.get('href', ''))
        if m:
            starts.add(int(m.group(1)))
    max_start = max(starts) if starts else 0
    total = max_start // 100 + 1
    print(f"   总页数: {total}")
    to_fetch = {max_start}
    if max_start >= 100:
        to_fetch.add(max_start - 100)
    all_posts = []
    for start in sorted(to_fetch):
        page_url = re.sub(r'start=\d+', f'start={start}', topic_url) if 'start=' in topic_url \
            else topic_url + ('&' if '?' in topic_url else '?') + f'start={start}'
        rr = http_get(page_url, cookie=DOUBAN_COOKIE)
        if hasattr(rr, 'status_code') and rr.status_code == 200 and '没有访问权限' not in rr.text:
            page_posts = parse_reply_blocks(BeautifulSoup(rr.text, 'lxml'), target_user)
            print(f"     第 {start//100+1}/{total} 页 → {len(page_posts)} 条")
            all_posts.extend(page_posts)
    all_posts.sort(key=lambda p: p['date'] + p['sortable_time'])
    return all_posts


def normalize(posts):
    """去重 + 标准化（对齐 xueqiu-tracker.normalize / 你本地缓存结构）。"""
    seen, uniq = set(), []
    for p in posts:
        key = (p.get('id') or p.get('content', '')[:50])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    return uniq


def find_latest_topic(statuses_url, target_user):
    """主页广播页定位最新一条广播(豆瓣话题)，返回话题信息字典。

    主页 statuses 页里的"广播"实为豆瓣话题(topic)动态：每条带 data-aid(话题id)
    与 data-uid(作者uid)。取第一条 status-item 的话题链接即可。
    """
    r = http_get(statuses_url, cookie=DOUBAN_COOKIE)
    if not hasattr(r, 'status_code') or r.status_code != 200 or '/statuses/' not in r.text:
        print(f"  ⚠️ 广播页不可达: {getattr(r,'status_code','ERR')} / sec风控={ 'sec.douban.com' in getattr(r,'url','') }")
        return None
    soup = BeautifulSoup(r.text, 'lxml')
    item = soup.find('div', class_='status-wrapper') or soup.find('div', class_='status-item')
    if not item:
        print("  ❌ 未解析到任何广播")
        return None
    aid = item.get('data-aid', '')
    uid = item.get('data-uid', '')
    title_a = item.find('a', href=re.compile(r'/topic/\d+'))
    title = title_a.get_text(strip=True) if title_a else ""
    if not aid and title_a:
        m = re.search(r'/topic/(\d+)', title_a.get('href', ''))
        aid = m.group(1) if m else ''
    if not aid:
        print("  ❌ 未解析到话题 id")
        return None
    return {'aid': aid, 'uid': uid, 'title': title,
            'url': f"https://www.douban.com/topic/{aid}/"}


def fetch_topic_comments(topic, target_user):
    """调 rexxar API 拉全部评论，按作者 uid 过滤(即只看作者)，返回发言字典列表。

    豆瓣话题的回应是 AJAX 动态加载，静态 HTML 无 reply-doc；真实接口为
    m.douban.com/rexxar/api/v2/group/topic/{aid}/comments。接口的 user_id/only_author
    参数服务端不生效，需客户端按作者 uid 过滤。
    """
    aid = topic['aid']
    uid = topic.get('uid', '')
    api = f"https://m.douban.com/rexxar/api/v2/group/topic/{aid}/comments"
    H = {'User-Agent': _MOBILE_UA, 'Cookie': DOUBAN_COOKIE,
         'Referer': f"https://m.douban.com/topic/{aid}/", 'Accept': 'application/json'}
    posts, start, pages = [], 0, 0
    MAX_PAGES = 200  # 安全上限：防止接口异常导致死循环
    while pages < MAX_PAGES:
        pages += 1
        rr = SESSION.get(api, params={'start': start, 'count': 100,
                           'status': 'open', 'order_by': 'create_time'},
                           headers=H, timeout=20, verify=False)
        if rr.status_code != 200 or 'json' not in rr.headers.get('content-type', ''):
            print(f"  ⚠️ API {rr.status_code} / sec风控={ 'sec.douban.com' in getattr(rr,'url','') }")
            break
        cm = rr.json().get('comments') or []
        if not cm:
            break
        for c in cm:
            a = c.get('author', {})
            if a.get('uid') != uid and a.get('name') != target_user:
                continue  # 只看作者
            ct = c.get('create_time', '')
            txt = (c.get('text') or c.get('content') or '').strip()
            photos = c.get('photos') or []
            if photos:
                for p in photos:
                    u = (p.get('image', {}) or {}).get('large') or (p.get('image', {}) or {}).get('normal') or {}
                    u = u.get('url', '') if isinstance(u, dict) else ''
                    if u:
                        txt += f"\n\n![图片]({u})"
            ref = c.get('ref_comment') or c.get('quote')
            quote = ""
            if isinstance(ref, dict) and ref.get('text'):
                quote = f"（引用 @{ref.get('author', {}).get('name', '')}）{ref.get('text', '')[:120]}"
            tm = re.search(r'\d{2}:\d{2}(?::\d{2})?', ct)
            st = (tm.group() if tm else ct[:8])
            if len(st) == 5:
                st += ':00'
            posts.append({'id': str(c.get('id', '')), 'author': target_user,
                          'content': txt, 'time': ct, 'sortable_time': st,
                          'quote': quote, 'date': ct[:10]})
        start += len(cm)  # 按接口实际返回量推进翻页（count 可能被截断）
    posts.sort(key=lambda p: p['date'] + p['sortable_time'])
    return posts


def scrape_user():
    """抓取目标楼主发言，按 SCRAPE_MODE 分派：
       - topic（默认）：用户主页广播(豆瓣话题) → 最新一条广播 → 只看作者(按uid过滤)
       - group：原小组话题模式（保留，切回时启用）
    返回标准化 post 列表。
    """
    if SCRAPE_MODE == "group":
        # —— 旧模式（原逻辑整段保留，仅当切回时运行）——
        today = datetime.datetime.now().strftime('%Y-%m-%d')
        all_posts = []
        for gu in DOUBAN_GROUP_URLS:
            print(f"\n=== 小组 {gu} ===")
            latest = find_latest_post(gu, DOUBAN_TARGET_USER)
            if not latest:
                continue
            print(f"  最新帖: {latest['title']} (tid={latest['tid']}, 楼主确认={latest['author_confirmed']})")
            posts = fetch_posts(latest['url'], DOUBAN_TARGET_USER)
            print(f"  [抓取] 去重后 {len(posts)} 条")
            all_posts.extend(posts)
        # 方案 A：严格只保留"当天"发言（末两页可能混入历史发言，此处按日期过滤）
        day_posts = [p for p in all_posts if p.get('date') == today]
        print(f"  [当日过滤] 仅留 {today} 发言：{len(day_posts)} 条（丢弃历史 {len(all_posts)-len(day_posts)} 条）")
        return normalize(day_posts)
    else:
        # —— 新模式（豆瓣话题 + 只看作者）：每次取最新一条广播的全部发言 ——
        latest = find_latest_topic(DOUBAN_USER_STATUSES_URL, DOUBAN_TARGET_USER)
        if not latest:
            return []
        print(f"  最新话题: {latest['title']} (#{latest['aid']}, 作者uid={latest['uid']})")
        posts = fetch_topic_comments(latest, DOUBAN_TARGET_USER)
        print(f"  [抓取] 作者发言 {len(posts)} 条（该最新广播全部，不限当日）")
        return normalize(posts)
