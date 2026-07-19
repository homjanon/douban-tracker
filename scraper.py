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

from config import DOUBAN_COOKIE, DOUBAN_GROUP_URLS, DOUBAN_TARGET_USER, PAGES

_UAS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/119.0.0.0 Safari/537.36',
]
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


def scrape_user():
    """抓取目标楼主发言（多组支持，合并去重）。返回标准化 post 列表。"""
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
    return normalize(all_posts)
