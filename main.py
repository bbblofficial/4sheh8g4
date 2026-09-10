import os
import time
import asyncio
import logging
import re
from urllib.parse import quote, unquote, urlparse
import html as html_parser
from concurrent.futures import ThreadPoolExecutor

import requests
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
import httpx
from bs4 import BeautifulSoup
import yt_dlp
from cachetools import TTLCache

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

extraction_cache = TTLCache(maxsize=2000, ttl=7200)
search_cache = TTLCache(maxsize=1000, ttl=1800)
thread_pool = ThreadPoolExecutor(max_workers=50)

# پراکسی وارپ صرفاً برای یوتیوب
WARP_PROXY = os.getenv("WARP_PROXY", "socks5h://127.0.0.1:40000")

app = FastAPI(title="Media Extraction Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_dynamic_headers(target: str, request_headers: dict = None) -> dict:
    target_lower = target.lower()
    if "youtube.com" in target_lower or "googlevideo.com" in target_lower or "youtu.be" in target_lower:
        ref = "https://www.youtube.com/"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Referer": ref,
            "Origin": ref.rstrip('/'),
        }
    elif "pornhub.com" in target_lower or "phncdn.com" in target_lower:
        ref = "https://www.pornhub.com/"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Referer": ref,
            "Origin": ref.rstrip('/'),
            "Cookie": "has_accepted_cookie=1; age_verified=1; platform=pc; bs=1; accessAgeDisclaimerPH=1; accessPH=1;"
        }
    elif "xhamster" in target_lower or "xhcdn" in target_lower:
        ref = "https://xhamster.com/"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Referer": ref,
            "Origin": ref.rstrip('/'),
            "Cookie": "has_accepted_cookie=1; age_verified=1; platform=pc;"
        }
    elif "xnxx" in target_lower:
        ref = "https://www.xnxx.com/"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Referer": ref,
            "Origin": ref.rstrip('/'),
            "Cookie": "has_accepted_cookie=1; age_verified=1; platform=pc;"
        }
    elif "xvideos" in target_lower:
        ref = "https://www.xvideos.com/"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Referer": ref,
            "Origin": ref.rstrip('/'),
            "Cookie": "has_accepted_cookie=1; age_verified=1; platform=pc;"
        }
    elif "redtube" in target_lower:
        ref = "https://www.redtube.com/"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Referer": ref,
            "Origin": ref.rstrip('/'),
            "Cookie": "has_accepted_cookie=1; age_verified=1; platform=pc;"
        }
    elif "youporn" in target_lower:
        ref = "https://www.youporn.com/"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Referer": ref,
            "Origin": ref.rstrip('/'),
            "Cookie": "has_accepted_cookie=1; age_verified=1; platform=pc;"
        }
    else:
        ref = "https://www.pornhub.com/"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Referer": ref,
            "Origin": ref.rstrip('/'),
            "Cookie": "has_accepted_cookie=1; age_verified=1; platform=pc; bs=1; accessAgeDisclaimerPH=1; accessPH=1;"
        }
        
    if request_headers and "range" in request_headers:
        headers["Range"] = request_headers["range"]
    return headers

def clean_thumbnail_url(raw_url: str) -> str:
    if not raw_url:
        return ""
    if ',' in raw_url and ('http://' in raw_url or 'https://' in raw_url):
        parts = re.split(r',\s+', raw_url)
        candidates = []
        for p in parts:
            url_part = p.strip().split(' ')[0]
            if url_part.startswith('http'):
                candidates.append(url_part)
        if candidates:
            raw_url = candidates[-1]

    raw_url = raw_url.strip()
    if raw_url.startswith('//'):
        raw_url = "https:" + raw_url
    return raw_url

def is_invalid_title(t: str) -> bool:
    if not t:
        return True
    t_clean = t.strip()
    if t_clean.isdigit() or len(t_clean) <= 2:
        return True
    if re.search(r'\d{1,2}:\d{2}', t_clean) and len(t_clean) <= 12:
        return True
    return False

def search_youtube_innertube(q: str) -> list:
    session = requests.Session()
    if WARP_PROXY:
        session.proxies = {"http": WARP_PROXY, "https": WARP_PROXY}

    api_url = "https://www.youtube.com/youtubei/v1/search"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Content-Type": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "X-YouTube-Client-Name": "1",
        "X-YouTube-Client-Version": "2.20240901.01.00",
    }
    payload = {
        "context": {
            "client": {
                "clientName": "WEB",
                "clientVersion": "2.20240901.01.00",
                "hl": "en",
                "gl": "US"
            }
        },
        "query": q
    }
    try:
        resp = session.post(api_url, json=payload, headers=headers, timeout=12)
        if resp.status_code != 200:
            return []
        data = resp.json()
        videos = []

        def walk(node):
            if isinstance(node, dict):
                if "videoRenderer" in node:
                    v = node["videoRenderer"]
                    vid = v.get("videoId")
                    if vid:
                        title = ""
                        if "title" in v and "runs" in v["title"]:
                            title = "".join(r.get("text", "") for r in v["title"]["runs"])
                        elif "title" in v and "simpleText" in v["title"]:
                            title = v["title"]["simpleText"]

                        duration_sec = 0
                        dur_text = v.get("lengthText", {}).get("simpleText", "")
                        if dur_text:
                            parts = dur_text.split(":")
                            if len(parts) == 2:
                                duration_sec = int(parts[0]) * 60 + int(parts[1])
                            elif len(parts) == 3:
                                duration_sec = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])

                        uploader = ""
                        owner = v.get("ownerText", {}) or v.get("shortBylineText", {})
                        if "runs" in owner and len(owner["runs"]) > 0:
                            uploader = owner["runs"][0].get("text", "")

                        thumb = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
                        if "thumbnail" in v and "thumbnails" in v["thumbnail"] and len(v["thumbnail"]["thumbnails"]) > 0:
                            thumb = v["thumbnail"]["thumbnails"][-1].get("url", thumb)

                        if title and not is_invalid_title(title):
                            videos.append({
                                "vkey": vid,
                                "title": html_parser.unescape(title),
                                "thumbnail": clean_thumbnail_url(thumb),
                                "url": f"https://www.youtube.com/watch?v={vid}",
                                "duration": duration_sec,
                                "uploader": uploader,
                                "provider": "youtube"
                            })
                for val in node.values():
                    walk(val)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(data)
        seen = set()
        deduped = []
        for v in videos:
            if v["vkey"] not in seen:
                seen.add(v["vkey"])
                deduped.append(v)
        return deduped[:48]
    except Exception as e:
        logger.error(f"InnerTube search error: {e}")
        return []

def search_pornhub_with_ytdlp(q: str, page: int) -> list:
    videos = []
    search_term = f"phsearch48:{q}"
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': 'in_playlist',
        'skip_download': True,
        'nocheckcertificate': True,
        'age_limit': 21,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_term, download=False)
            entries = info.get('entries', []) if info else []
            for entry in entries:
                if not entry:
                    continue
                vkey = entry.get('id') or ''
                url = entry.get('url') or f"https://www.pornhub.com/view_video.php?viewkey={vkey}"
                title = html_parser.unescape(entry.get('title', 'Unknown Video'))
                if any(bad in url.lower() or bad in title.lower() for bad in ['/join', 'sponsor', 'promo', 'ad/']):
                    continue
                if is_invalid_title(title):
                    continue
                videos.append({
                    "vkey": vkey,
                    "title": title,
                    "thumbnail": clean_thumbnail_url(entry.get('thumbnail', '')),
                    "url": url,
                    "provider": "pornhub"
                })
    except Exception as e:
        logger.error(f"yt-dlp fallback search error for Pornhub: {e}")
    return videos

def search_youporn_with_ytdlp(q: str, page: int) -> list:
    videos = []
    search_term = f"ypsearch48:{q}"
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': 'in_playlist',
        'skip_download': True,
        'nocheckcertificate': True,
        'age_limit': 21,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_term, download=False)
            entries = info.get('entries', []) if info else []
            for entry in entries:
                if not entry:
                    continue
                vkey = entry.get('id') or ''
                url = entry.get('url') or f"https://www.youporn.com/watch/{vkey}"
                title = html_parser.unescape(entry.get('title', 'Unknown Video'))
                if any(bad in url.lower() or bad in title.lower() for bad in ['/join', 'sponsor', 'promo', 'ad/']):
                    continue
                if is_invalid_title(title):
                    continue
                videos.append({
                    "vkey": vkey,
                    "title": title,
                    "thumbnail": clean_thumbnail_url(entry.get('thumbnail', '')),
                    "url": url,
                    "provider": "youporn"
                })
    except Exception as e:
        logger.error(f"yt-dlp fallback search error for YouPorn: {e}")
    return videos

def search_xhamster_with_ytdlp(q: str, page: int) -> list:
    videos = []
    search_url = f"https://xhamster.com/search/{quote(q)}" if page <= 1 else f"https://xhamster.com/search/{quote(q)}/{page}"
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': 'in_playlist',
        'skip_download': True,
        'nocheckcertificate': True,
        'age_limit': 21,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_url, download=False)
            entries = info.get('entries', []) if info else []
            for entry in entries:
                if not entry:
                    continue
                url = entry.get('url', '')
                vkey = entry.get('id', '')
                if not vkey and url:
                    parts = [p for p in url.split('/') if p]
                    vkey = parts[-1] if parts else ""
                if not vkey or '/videos/' not in url:
                    continue
                title = html_parser.unescape(entry.get('title', f"Video {vkey}"))
                if any(bad in url.lower() or bad in title.lower() for bad in ['/join', 'sponsor', 'promo', 'ad/']):
                    continue
                if is_invalid_title(title):
                    continue
                thumb = entry.get('thumbnail', '')
                if not thumb and entry.get('thumbnails'):
                    thumb = entry.get('thumbnails')[0].get('url', '')
                videos.append({
                    "vkey": vkey,
                    "title": title,
                    "thumbnail": clean_thumbnail_url(thumb),
                    "url": url if url.startswith('http') else f"https://xhamster.com/videos/{vkey}",
                    "provider": "xhamster"
                })
    except Exception as e:
        logger.error(f"yt-dlp fallback search error for xHamster: {e}")
    return videos

def search_redtube_with_ytdlp(q: str, page: int) -> list:
    videos = []
    search_url = f"https://www.redtube.com/?search={quote(q)}" if page <= 1 else f"https://www.redtube.com/?search={quote(q)}&page={page}"
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': 'in_playlist',
        'skip_download': True,
        'nocheckcertificate': True,
        'age_limit': 21,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_url, download=False)
            entries = info.get('entries', []) if info else []
            for entry in entries:
                if not entry:
                    continue
                url = entry.get('url', '')
                vkey = entry.get('id', '')
                if not vkey and url:
                    parts = [p for p in url.split('/') if p]
                    vkey = parts[-1] if parts else ""
                if not vkey or not vkey.isdigit():
                    continue
                title = html_parser.unescape(entry.get('title', f"Video {vkey}"))
                if any(bad in url.lower() or bad in title.lower() for bad in ['/join', 'sponsor', 'promo', 'ad/', '/channels/', '/pornstar/', '/amateur/', 'help.pornhub.com', 'adtng.com', 'trafficjunky.net']):
                    continue
                if is_invalid_title(title) or 'information/' in url:
                    continue
                thumb = entry.get('thumbnail', '')
                videos.append({
                    "vkey": vkey,
                    "title": title,
                    "thumbnail": clean_thumbnail_url(thumb),
                    "url": url if url.startswith('http') else f"https://www.redtube.com/{vkey}",
                    "provider": "redtube"
                })
    except Exception as e:
        logger.error(f"yt-dlp fallback search error for RedTube: {e}")
    return videos

def parse_metadata_fallback(url: str, provider: str) -> dict:
    if provider == "youtube":
        return {"view_count": 0, "upload_date": "", "thumbnail": "", "title": ""}
        
    base_domain = "https://www.pornhub.com"
    if "xhamster.com" in url:
        base_domain = "https://xhamster.com"
    elif "xnxx.com" in url:
        base_domain = "https://www.xnxx.com"
    elif "xvideos.com" in url:
        base_domain = "https://www.xvideos.com"
    elif "redtube.com" in url:
        base_domain = "https://www.redtube.com"
    elif "youporn.com" in url:
        base_domain = "https://www.youporn.com"

    url = re.sub(r'https?://[a-zA-Z0-9-]+\.' + provider + r'\.com', base_domain, url)
    headers = get_dynamic_headers(url)
    try:
        resp = requests.get(url, headers=headers, timeout=3.5)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, 'html.parser')
            view_count = 0
            upload_date = ""
            poster_url = ""
            scraped_title = ""

            og_title = soup.find('meta', property='og:title')
            if og_title and og_title.get('content'):
                scraped_title = og_title.get('content').replace('&amp;', '&').strip()

            if not scraped_title:
                title_tag = soup.find('title')
                if title_tag:
                    scraped_title = title_tag.get_text().replace('&amp;', '&').split('- RedTube')[0].split('- YouPorn')[0].split('- xHamster')[0].strip()

            og_img = soup.find('meta', property='og:image')
            if og_img and og_img.get('content'):
                poster_url = clean_thumbnail_url(og_img.get('content').replace('&amp;', '&'))

            if not poster_url:
                img_json = re.search(r'"image_url"\s*:\s*"([^"]+)"', resp.text)
                if img_json:
                    poster_url = clean_thumbnail_url(img_json.group(1).replace('\\/', '/'))

            view_match = re.search(r'([\d,\.]+)\s*(?:Views|views|Vistas|M views|k views)', resp.text)
            if view_match:
                raw_views = view_match.group(1).replace(',', '').replace('.', '')
                if 'k' in view_match.group(0).lower():
                    view_count = int(float(raw_views.replace('k', '')) * 1000)
                elif 'm' in view_match.group(0).lower():
                    view_count = int(float(raw_views.replace('m', '')) * 1000000)
                else:
                    view_count = int(raw_views) if raw_views.isdigit() else 0

            date_match = re.search(r'(\d{4}-\d{2}-\d{2})|(\d{1,2}\s+[a-zA-Z]+\s+\d{4})', resp.text)
            if date_match:
                upload_date = date_match.group(0)

            return {"view_count": view_count, "upload_date": upload_date, "thumbnail": poster_url, "title": scraped_title}
    except Exception:
        pass
    return {"view_count": 0, "upload_date": "", "thumbnail": "", "title": ""}

def search_provider_robust(provider: str, q: str, page: int):
    cache_key = f"{provider}:{q}:{page}"
    if cache_key in search_cache:
        return search_cache[cache_key]

    if provider == "youtube":
        videos = search_youtube_innertube(q)
        if videos:
            search_cache[cache_key] = videos
        return videos

    videos = []
    dummy_url = f"https://www.{provider}.com/" if provider != "xhamster" else "https://xhamster.com/"
    headers = get_dynamic_headers(dummy_url)

    if provider == "youporn":
        search_url = f"https://www.youporn.com/search/?query={quote(q)}&page={page}"
    elif provider == "xhamster":
        search_url = f"https://xhamster.com/search/{quote(q)}" if page <= 1 else f"https://xhamster.com/search/{quote(q)}/{page}"
    elif provider == "redtube":
        search_url = f"https://www.redtube.com/?search={quote(q)}" if page <= 1 else f"https://www.redtube.com/?search={quote(q)}&page={page}"
    elif provider == "xnxx":
        search_url = f"https://www.xnxx.com/search/{quote(q)}" if page <= 1 else f"https://www.xnxx.com/search/{quote(q)}/{page}"
    elif provider == "xvideos":
        p_val = page - 1 if page > 1 else 0
        search_url = f"https://www.xvideos.com/?k={quote(q)}" if p_val == 0 else f"https://www.xvideos.com/?k={quote(q)}&p={p_val}"
    else:
        search_url = f"https://www.pornhub.com/video/search?search={quote(q)}&page={page}"

    for attempt in range(3):
        try:
            resp = requests.get(search_url, headers=headers, timeout=10)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, 'html.parser')
                seen = set()

                if provider == "youporn":
                    items = soup.select('div.video-box, div.pb-card, div.list-item, li.video-tile, div.videoBox, div.videoListItem, div[class*="video"], div.video-tile, a[href*="/watch/"]')
                    for item in items:
                        full_url, title, thumb = "", "", ""
                        if item.name == 'a':
                            href = item.get('href', '')
                            if not href or '/watch/' not in href: continue
                            full_url = href if href.startswith('http') else f"https://www.youporn.com{href}"
                            title = item.get('title') or item.get('alt') or item.get_text(strip=True)
                            img_tag = item.select_one('img')
                            if img_tag:
                                if is_invalid_title(title):
                                    title = img_tag.get('alt') or title
                                thumb = clean_thumbnail_url(img_tag.get('data-src') or img_tag.get('src') or img_tag.get('data-lazy-src') or img_tag.get('data-image') or img_tag.get('data-thumb') or img_tag.get('data-poster') or "")
                        else:
                            a_tag = item.select_one('a[href*="/watch/"]')
                            if not a_tag: continue
                            href = a_tag.get('href', '')
                            full_url = href if href.startswith('http') else f"https://www.youporn.com{href}"
                            title_tag = item.select_one('a[href*="/watch/"] [title], p.title, span.title, a, div.title, h3, h4')
                            title = title_tag.get('title') or title_tag.get_text(strip=True) if title_tag else a_tag.get('title', '')
                            img_tag = item.select_one('img')
                            if img_tag:
                                if is_invalid_title(title):
                                    title = img_tag.get('alt') or title
                                thumb = clean_thumbnail_url(img_tag.get('data-src') or img_tag.get('src') or img_tag.get('data-lazy-src') or img_tag.get('data-image') or img_tag.get('data-thumb') or img_tag.get('data-poster') or "")

                        full_url = full_url.split('?')[0].rstrip('/')
                        if not full_url or full_url in seen: continue
                        if any(bad in full_url.lower() or bad in title.lower() for bad in ['/join', 'sponsor', 'promo', 'ad/']): continue
                        if is_invalid_title(title): continue
                        seen.add(full_url)

                        match_id = re.search(r'/watch/(\d+)', full_url)
                        if not match_id: continue
                        vkey = match_id.group(1)

                        videos.append({
                            "vkey": vkey,
                            "title": html_parser.unescape(title),
                            "thumbnail": thumb,
                            "url": full_url,
                            "provider": "youporn"
                        })
                        if len(videos) >= 48: break

                elif provider == "pornhub":
                    items = soup.select('li.videoblock, li.pcVideoListItem, li.js-pop, li.videoBox, ul#videoSearchResult li, div.search-video-list li')
                    for item in items:
                        vkey = item.get("data-video-vkey")
                        if not vkey:
                            a_tag = item.select_one('a[href*="viewkey="], a[href*="/view_video.php"], a[href*="/video/"]')
                            if a_tag:
                                h = a_tag.get('href', '')
                                if "viewkey=" in h:
                                    try: vkey = h.split("viewkey=")[1].split("&")[0]
                                    except: pass
                                elif "/video/" in h:
                                    parts = [p for p in h.split('/') if p]
                                    if parts: vkey = parts[-1]
                        if not vkey: continue
                        full_url = f"https://www.pornhub.com/view_video.php?viewkey={vkey}"
                        if full_url in seen: continue
                        
                        title_tag = item.select_one('.title a, a.title, img[alt]')
                        title = title_tag.get('alt') or title_tag.get_text(strip=True) if title_tag else "Unknown Video"
                        
                        if any(bad in full_url.lower() or bad in title.lower() for bad in ['/join', 'sponsor', 'promo', 'ad/']): continue
                        if is_invalid_title(title): continue
                        seen.add(full_url)

                        img_tag = item.select_one('img')
                        thumb = clean_thumbnail_url(img_tag.get('data-thumb_url') or img_tag.get('data-mediumthumb') or img_tag.get('data-image') or img_tag.get('data-src') or img_tag.get('src') or img_tag.get('data-lazy-src') or "") if img_tag else ""

                        videos.append({"vkey": vkey, "title": html_parser.unescape(title), "thumbnail": thumb, "url": full_url, "provider": "pornhub"})
                        if len(videos) >= 48: break

                elif provider == "xhamster":
                    items = soup.select('div.thumb-list__item, article.video-thumb, div.video-container, div[class*="video-thumb"], div.video-box, div[class*="video-card"]')
                    if not items:
                        items = soup.select('a[href*="/videos/"]')
                    for item in items:
                        a_tag = item if item.name == 'a' else item.select_one('a[href*="/videos/"]')
                        if not a_tag: continue
                        href = a_tag.get('href', '')
                        if not href or '/videos/' not in href: continue
                        full_url = href if href.startswith('http') else f"https://xhamster.com{href}"
                        full_url = full_url.split('?')[0].rstrip('/')
                        
                        if any(bad in full_url.lower() for bad in ['/pornstars/', '/channels/', '/creators/', '/categories/', '/tags/', '/join', 'sponsor', 'promo']): continue
                        if full_url in seen: continue
                        
                        vid_parts = [p for p in full_url.split('/') if p]
                        vid_id = vid_parts[-1] if vid_parts else "unknown"

                        title_tag = item.select_one('a.video-thumb__title, span.video-thumb__title-text, [title], h4, p') if item.name != 'a' else item
                        title = ""
                        if title_tag:
                            title = title_tag.get('title') or title_tag.get_text(strip=True)
                        if not title or title == vid_id or 'xhamster' in title.lower():
                            title = a_tag.get('title') or vid_id.replace('-', ' ').title()

                        if any(bad in title.lower() for bad in ['sponsor', 'promo', 'ad/']): continue
                        if is_invalid_title(title): continue
                        seen.add(full_url)

                        thumb = ""
                        container = item if item.name != 'a' else (item.parent.parent if item.parent else item)
                        
                        for noscript in container.select('noscript'):
                            ns_match = re.search(r'https?://[^\s<>"\']+\.(?:xhcdn|phncdn)\.com[^\s<>"\']+(?:\.jpg|\.jpeg|\.png|\.webp)', noscript.text)
                            if ns_match:
                                thumb = clean_thumbnail_url(ns_match.group(0))
                                break

                        if not thumb:
                            for tag in container.select('img, source'):
                                for attr in ['data-srcset', 'srcset', 'data-src', 'data-lazy-src', 'data-original', 'data-thumb', 'data-image', 'src']:
                                    val = tag.get(attr, '')
                                    if val and ('xhcdn' in val or 'phncdn' in val):
                                        cleaned = clean_thumbnail_url(val)
                                        if cleaned.startswith('http'):
                                            thumb = cleaned
                                            break
                                if thumb: break

                        if not thumb:
                            for attr in ['data-thumb', 'data-preview', 'data-poster', 'data-image', 'data-src']:
                                val = container.get(attr, '')
                                if val and val.startswith('http'):
                                    thumb = clean_thumbnail_url(val)
                                    break

                        if not thumb:
                            container_html = str(container)
                            matches = re.findall(r'https?://[^\s<>"\']+(?:xhcdn|phncdn)\.com[^\s<>"\']+', container_html)
                            for candidate in matches:
                                if any(ext in candidate.lower() for ext in ['.jpg', '.jpeg', '.png', '.webp']) and \
                                   not any(bad in candidate.lower() for bad in ['logo', 'svg', 'avatar', 'pixel', 'icon']):
                                    thumb = candidate.split('"')[0].split("'")[0].split(' ')[0]
                                    break

                        videos.append({"vkey": vid_id, "title": html_parser.unescape(title), "thumbnail": thumb, "url": full_url, "provider": "xhamster"})
                        if len(videos) >= 48: break

                    missing_thumbs = [v for v in videos if not v.get("thumbnail")]
                    if missing_thumbs:
                        def resolve_thumb(v):
                            try:
                                meta = parse_metadata_fallback(v["url"], "xhamster")
                                if meta.get("thumbnail"):
                                    v["thumbnail"] = meta["thumbnail"]
                                if meta.get("title") and not is_invalid_title(meta["title"]):
                                    v["title"] = meta["title"]
                            except Exception:
                                pass

                        with ThreadPoolExecutor(max_workers=min(len(missing_thumbs), 30)) as pool:
                            list(pool.map(resolve_thumb, missing_thumbs))
                    
                    videos = [v for v in videos if not is_invalid_title(v["title"])]

                elif provider == "redtube":
                    items = soup.select('div.videoBox, li.videoblock, div.video-item, div.pb-card, div.video-tile, div[class*="video"], a[href*="/"]')
                    for item in items:
                        full_url, title, thumb = "", "", ""
                        if item.name == 'a':
                            href = item.get('href', '')
                            if not href or any(b in href for b in ['search=', '/hot', '/join', '/channels/', '/pornstar/', '/amateur/', '/information/', 'help.pornhub', 'adtng.com', 'trafficjunky.net']): continue
                            parts = [p for p in href.split('/') if p]
                            if not parts or not parts[-1].isdigit(): continue
                            full_url = href if href.startswith('http') else f"https://www.redtube.com{href}"
                            title = item.get('title') or item.get('alt') or item.get_text(strip=True)
                            img_tag = item.select_one('img')
                            if img_tag:
                                if is_invalid_title(title):
                                    title = img_tag.get('alt') or title
                                thumb = clean_thumbnail_url(img_tag.get('data-src') or img_tag.get('src') or img_tag.get('data-lazy-src') or img_tag.get('data-image') or img_tag.get('data-thumb') or "")
                        else:
                            a_tag = item.select_one('a[href]')
                            if not a_tag: continue
                            href = a_tag.get('href', '')
                            if not href or any(b in href for b in ['search=', '/hot', '/join', '/channels/', '/pornstar/', '/amateur/', '/information/', 'help.pornhub', 'adtng.com', 'trafficjunky.net']): continue
                            parts = [p for p in href.split('/') if p]
                            if not parts or not parts[-1].isdigit(): continue
                            full_url = href if href.startswith('http') else f"https://www.redtube.com{href}"
                            
                            title_tag = item.select_one('.video-title-text, a[title], span.title, a, p, h3, h4')
                            if title_tag:
                                title = title_tag.get('title') or title_tag.get_text(strip=True)
                            if is_invalid_title(title):
                                title = a_tag.get('title', '')
                            
                            img_tag = item.select_one('img')
                            if img_tag:
                                if is_invalid_title(title):
                                    title = img_tag.get('alt') or title
                                thumb = clean_thumbnail_url(img_tag.get('data-src') or img_tag.get('src') or img_tag.get('data-lazy-src') or img_tag.get('data-image') or img_tag.get('data-thumb') or "")

                        full_url = full_url.split('?')[0].rstrip('/')
                        if not full_url or full_url in seen: continue
                        seen.add(full_url)

                        vid_parts = [p for p in full_url.split('/') if p]
                        vid_id = vid_parts[-1] if vid_parts else "unknown"

                        if not vid_id.isdigit(): continue
                        if any(bad in full_url.lower() or bad in title.lower() for bad in ['/join', 'sponsor', 'promo', 'ad/', 'adtng', 'trafficjunky']): continue

                        videos.append({"vkey": vid_id, "title": html_parser.unescape(title), "thumbnail": thumb, "url": full_url, "provider": "redtube"})
                        if len(videos) >= 48: break

                    def resolve_redtube(v):
                        try:
                            meta = parse_metadata_fallback(v["url"], "redtube")
                            if meta.get("thumbnail") and (not v["thumbnail"] or 'rdtcdn.com' not in v["thumbnail"]):
                                v["thumbnail"] = meta["thumbnail"]
                            if meta.get("title") and not is_invalid_title(meta["title"]):
                                v["title"] = meta["title"]
                        except Exception:
                            pass

                    missing_rdt = [v for v in videos if not v.get("thumbnail") or is_invalid_title(v["title"])]
                    if missing_rdt:
                        with ThreadPoolExecutor(max_workers=min(len(missing_rdt), 30)) as pool:
                            list(pool.map(resolve_redtube, missing_rdt))
                    
                    videos = [v for v in videos if not is_invalid_title(v["title"])]

                elif provider == "xnxx":
                    items = soup.select('div.mozaique div.thumb-block')
                    for item in items:
                        a_tag = item.select_one('a[href*="/video-"], a[href*="/video."]')
                        if not a_tag: continue
                        href = a_tag.get('href', '')
                        full_url = f"https://www.xnxx.com{href}" if href.startswith('/') else href
                        
                        vid_id = href.split('/')[1] if len(href.split('/')) > 1 else href
                        title_tag = item.select_one('div.thumb-under a[title], div.thumb-under a')
                        title = title_tag.get('title') or title_tag.get_text(strip=True) if title_tag else "Unknown Video"

                        if any(bad in full_url.lower() or bad in title.lower() for bad in ['/join', 'sponsor', 'promo', 'ad/']): continue
                        if is_invalid_title(title): continue
                        if full_url in seen: continue
                        seen.add(full_url)

                        img_tag = item.select_one('img')
                        thumb = clean_thumbnail_url(img_tag.get('data-src') or img_tag.get('src') or "") if img_tag else ""

                        videos.append({"vkey": vid_id, "title": html_parser.unescape(title), "thumbnail": thumb, "url": full_url, "provider": "xnxx"})
                        if len(videos) >= 48: break

                elif provider == "xvideos":
                    items = soup.select('div.mozaique div.thumb-block')
                    for item in items:
                        a_tag = item.select_one('p.title a, a[href*="/video."]')
                        if not a_tag: continue
                        href = a_tag.get('href', '')
                        vid_id = href.split('/')[1] if len(href.split('/')) > 1 else href
                        clean_href = href.rstrip('/')
                        if clean_href.endswith('_') or clean_href.endswith('/_') or len(clean_href.split('/')) < 3:
                            clean_href = f"/{vid_id}/video_stream"
                        full_url = f"https://www.xvideos.com{clean_href}" if clean_href.startswith('/') else clean_href
                        
                        title_tag = item.select_one('p.title a')
                        title = title_tag.get('title') or title_tag.get_text(strip=True) if title_tag else "Unknown Video"

                        if any(bad in full_url.lower() or bad in title.lower() for bad in ['/join', 'sponsor', 'promo', 'ad/']): continue
                        if is_invalid_title(title): continue
                        if full_url in seen: continue
                        seen.add(full_url)

                        img_tag = item.select_one('img')
                        thumb = clean_thumbnail_url(img_tag.get('data-src') or img_tag.get('src') or "") if img_tag else ""

                        videos.append({"vkey": vid_id, "title": html_parser.unescape(title), "thumbnail": thumb, "url": full_url, "provider": "xvideos"})
                        if len(videos) >= 48: break

                if len(videos) > 0:
                    search_cache[cache_key] = videos
                    return videos
        except Exception as e:
            logger.error(f"Search provider {provider} attempt {attempt+1} error: {e}")
            time.sleep(1.0)

    if provider == "pornhub":
        videos = search_pornhub_with_ytdlp(q, page)
    elif provider == "youporn":
        videos = search_youporn_with_ytdlp(q, page)
    elif provider == "xhamster":
        videos = search_xhamster_with_ytdlp(q, page)
    elif provider == "redtube":
        videos = search_redtube_with_ytdlp(q, page)

    search_cache[cache_key] = videos
    return videos

# فال‌بک اختصاصی پورن‌هاب در صورت دریافت خطای 410 از yt-dlp
def extract_pornhub_direct_fallback(url: str) -> dict | None:
    try:
        headers = get_dynamic_headers(url)
        resp = requests.get(url, headers=headers, timeout=8.0)
        if resp.status_code != 200:
            return None

        text = resp.text
        flashvars_match = re.search(r'flashvars_\d+\s*=\s*({.+?});', text)
        if not flashvars_match:
            return None

        import json
        f_data = json.loads(flashvars_match.group(1))
        media_defs = f_data.get('mediaDefinitions', [])

        qualities = []
        for item in media_defs:
            v_url = item.get('videoUrl')
            if not v_url or not isinstance(v_url, str) or not v_url.startswith('http'):
                continue
            q_val = item.get('quality')
            if not q_val:
                continue
            is_hls = item.get('format') == 'hls' or '.m3u8' in v_url
            q_label = f"{q_val}p" if str(q_val).isdigit() else str(q_val)

            qualities.append({
                "quality": q_label,
                "url": v_url,
                "type": "hls" if is_hls else "mp4"
            })

        if not qualities:
            return None

        # فیلتر کیفیت‌های HLS در صورت وجود
        has_hls = any(q['type'] == 'hls' for q in qualities)
        if has_hls:
            qualities = [q for q in qualities if q['type'] == 'hls']

        meta = parse_metadata_fallback(url, "pornhub")
        title = f_data.get('video_title') or meta.get('title') or "Pornhub Video"

        return {
            "status": "success",
            "title": html_parser.unescape(title),
            "thumbnail": clean_thumbnail_url(f_data.get('image_url') or meta.get('thumbnail', '')),
            "thumbnails": [clean_thumbnail_url(f_data.get('image_url') or meta.get('thumbnail', ''))],
            "duration": int(f_data.get('video_duration', 0)),
            "upload_date": meta.get('upload_date', ''),
            "view_count": meta.get('view_count', 0),
            "streams": {"qualities": qualities},
            "url": url,
            "provider": "pornhub"
        }
    except Exception as e:
        logger.error(f"Pornhub direct fallback extraction error: {e}")
        return None

def extract_with_ytdlp(url: str) -> dict:
    is_pornhub = "pornhub.com" in url
    is_youtube = any(y in url.lower() for y in ["youtube.com", "youtu.be"])

    if not is_pornhub and not is_youtube and url in extraction_cache:
        return extraction_cache[url]

    provider = "pornhub"
    if is_youtube:
        provider = "youtube"
    elif "xhamster.com" in url:
        provider = "xhamster"
    elif "xnxx.com" in url:
        provider = "xnxx"
    elif "xvideos.com" in url:
        provider = "xvideos"
    elif "redtube.com" in url:
        provider = "redtube"
    elif "youporn.com" in url:
        provider = "youporn"

    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'nocheckcertificate': True,
        'http_headers': get_dynamic_headers(url)
    }

    if is_youtube:
        if WARP_PROXY:
            ydl_opts['proxy'] = WARP_PROXY
        ydl_opts['extractor_args'] = {
            'youtube': {
                'player_client': ['tv', 'ios', 'mweb', 'web_safari'],
            }
        }
    else:
        ydl_opts['format'] = 'bestvideo+bestaudio/best'
        ydl_opts['age_limit'] = 21

    max_retries = 3
    last_error = "Unknown Error"

    for attempt in range(max_retries):
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)

            title = html_parser.unescape(info.get('title', ''))
            duration = info.get('duration', 0)
            upload_date = info.get('upload_date', '') 
            view_count = info.get('view_count', 0)

            extra_meta = parse_metadata_fallback(url, provider) if not is_youtube else {}

            if not is_pornhub and not is_youtube and (not title or is_invalid_title(title) or (len(title) <= 8 and title.isalnum())):
                if extra_meta.get("title") and not is_invalid_title(extra_meta.get("title")):
                    title = extra_meta.get("title")
            if not title or is_invalid_title(title):
                title = "Unknown Video"

            view_count = view_count or extra_meta.get("view_count", 0)
            upload_date = upload_date or extra_meta.get("upload_date", "")

            all_thumbs = []
            safe_thumb = extra_meta.get("thumbnail", "")
            if safe_thumb and not safe_thumb.startswith("data:image"):
                all_thumbs.append(clean_thumbnail_url(safe_thumb))

            if info.get('thumbnail') and info.get('thumbnail') not in all_thumbs:
                all_thumbs.append(clean_thumbnail_url(info.get('thumbnail')))

            for t in info.get('thumbnails', []):
                t_url = clean_thumbnail_url(t.get('url', ''))
                if t_url and t_url not in all_thumbs:
                    all_thumbs.append(t_url)

            clean_thumbs = [t for t in all_thumbs if 'hash=' not in t and 'validto=' not in t and 'hdnea=' not in t and 'svg' not in t and 'logo.jpg' not in t]

            if clean_thumbs:
                thumbnail = clean_thumbs[0]
                thumbnails = clean_thumbs
            else:
                thumbnail = all_thumbs[0] if all_thumbs else ""
                thumbnails = all_thumbs

            qualities_dict = {}

            for f in info.get('formats', []):
                f_url = f.get('url', '')
                if not f_url: continue
                if f.get('vcodec') == 'none': continue

                protocol = str(f.get('protocol', '')).lower()
                ext = str(f.get('ext', '')).lower()
                format_id = str(f.get('format_id', '')).lower()
                format_note = str(f.get('format_note', '')).lower()
                res_str = str(f.get('resolution', '')).lower()

                is_hls = 'm3u8' in protocol or ext == 'm3u8' or '.m3u8' in f_url or 'hls' in format_id
                height = f.get('height')

                if not height:
                    m = re.search(r'(\d{3,4})[pP]?', format_id + "-" + format_note + "-" + res_str)
                    if m: height = int(m.group(1))

                if height:
                    q_label = f"{height}p"
                else:
                    if "auto" in format_note or "auto" in format_id or is_hls:
                        q_label = "Auto"
                        height = 0
                    else: continue 

                if is_hls or 'mp4' in f_url or ext == 'mp4' or protocol.startswith('http'):
                    existing = qualities_dict.get(q_label)
                    if not existing or (is_hls and existing['type'] == 'mp4'):
                        qualities_dict[q_label] = {
                            "quality": q_label,
                            "url": f_url,
                            "type": "hls" if is_hls else "mp4",
                            "height": height
                        }

            has_hls = any(q['type'] == 'hls' for q in qualities_dict.values())
            if has_hls and not is_youtube:
                qualities_dict = {k: v for k, v in qualities_dict.items() if v['type'] == 'hls'}

            qualities = list(qualities_dict.values())
            qualities.sort(key=lambda x: x['height'], reverse=True)
            for q in qualities:
                q.pop('height', None)

            if not qualities:
                last_error = "No valid streams found"
                if attempt < max_retries - 1:
                    time.sleep(1.0)
                    continue
                return {"status": "error", "error": last_error, "url": url}

            result = {
                "status": "success",
                "title": title,
                "thumbnail": thumbnail,
                "thumbnails": thumbnails,
                "duration": duration,
                "upload_date": upload_date,
                "view_count": view_count,
                "streams": {"qualities": qualities},
                "url": url,
                "provider": provider
            }

            if not is_pornhub: extraction_cache[url] = result
            return result

        except Exception as e:
            last_error = str(e)
            # اگر خطای 410 روی پورن‌هاب رخ داد، بلافاصله فال‌بک مستقیم را اجرا کن
            if is_pornhub and ("410" in last_error or "Gone" in last_error):
                direct_res = extract_pornhub_direct_fallback(url)
                if direct_res:
                    return direct_res

            if attempt < max_retries - 1:
                time.sleep(1.0)
                continue

    return {"status": "error", "error": f"Failed after {max_retries} retries: {last_error}", "url": url}

@app.get("/api/explore")
async def explore(q: str = "brazzers", page: int = 1, provider: str = "pornhub"):
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(thread_pool, search_provider_robust, provider.lower(), q, page)
    return JSONResponse(res)

@app.get("/api/extract")
async def extract_endpoint(url: str):
    if not url: return JSONResponse({"status": "error", "error": "Missing URL"})
    target_url = url.strip()

    yt_match = re.search(r'(?:youtu\.be\/|v\/|u\/\w\/|embed\/|watch\?v=|shorts\/|live\/)([A-Za-z0-9_-]{11})', target_url)
    if yt_match:
        target_url = f"https://www.youtube.com/watch?v={yt_match.group(1)}"
    elif "viewkey=" not in target_url and not any(d in target_url for d in ["xhamster.com", "xnxx.com", "xvideos.com", "redtube.com", "youporn.com", "youtube.com", "youtu.be"]):
        if len(target_url) in [13, 15, 16] and "." not in target_url:
            target_url = f"https://www.pornhub.com/view_video.php?viewkey={target_url}"
        elif len(target_url) == 11:
            target_url = f"https://www.youtube.com/watch?v={target_url}"

    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(thread_pool, extract_with_ytdlp, target_url)
    return JSONResponse(res)

@app.get("/proxy-image")
async def fallback_proxy_image(url: str):
    target = url.strip()
    if target.startswith('//'): target = "https:" + target

    headers = get_dynamic_headers(target)

    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                req = await client.get(target, headers=headers)
                if req.status_code == 200:
                    return StreamingResponse(
                        (chunk async for chunk in req.aiter_bytes()),
                        status_code=req.status_code,
                        headers={
                            "Content-Type": req.headers.get("Content-Type", "image/jpeg"),
                            "Access-Control-Allow-Origin": "*",
                            "Cache-Control": "public, max-age=86400"
                        }
                    )
        except Exception:
            await asyncio.sleep(0.5)
    return Response(status_code=404)

@app.get("/proxy-m3u8")
async def proxy_m3u8(request: Request, url: str, sig: str = "", exp: str = "", request_host: str = ""):
    target = url.strip()
    headers = get_dynamic_headers(target)

    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                resp = await client.get(target, headers=headers)
                if resp.status_code == 200:
                    parsed_target = urlparse(target)
                    base_path = target.split('?')[0].rsplit('/', 1)[0] + '/'
                    target_query = parsed_target.query
                    
                    proto = request.headers.get("x-forwarded-proto", "https")
                    if not request_host:
                        request_host = request.headers.get("host", "")

                    lines = resp.text.split('\n')
                    rewritten = []
                    for line in lines:
                        line = line.strip()
                        if not line: continue
                        if line.startswith('#'):
                            if 'URI=' in line:
                                match = re.search(r'URI="([^"]+)"', line)
                                if match:
                                    uri = match.group(1)
                                    abs_uri = uri if uri.startswith('http') else base_path + uri
                                    if target_query and '?' not in abs_uri:
                                        abs_uri += "?" + target_query
                                    next_endpoint = "/proxy-m3u8" if ".m3u8" in abs_uri else "/proxy-video"
                                    new_uri = f"{proto}://{request_host}{next_endpoint}?url={quote(abs_uri)}&sig={sig}&exp={exp}&request_host={request_host}"
                                    line = line.replace(f'URI="{uri}"', f'URI="{new_uri}"')
                            rewritten.append(line)
                        else:
                            abs_uri = line if line.startswith('http') else base_path + line
                            if target_query and '?' not in abs_uri:
                                abs_uri += "?" + target_query
                            next_endpoint = "/proxy-m3u8" if ".m3u8" in abs_uri else "/proxy-video"
                            new_uri = f"{proto}://{request_host}{next_endpoint}?url={quote(abs_uri)}&sig={sig}&exp={exp}&request_host={request_host}"
                            rewritten.append(new_uri)

                    return Response(content="\n".join(rewritten), media_type="application/vnd.apple.mpegurl", headers={
                        "Access-Control-Allow-Origin": "*",
                        "Cache-Control": "no-cache, no-store"
                    })
        except Exception:
            await asyncio.sleep(0.5)
    return Response(status_code=502, content="Backend Proxy Error")

@app.get("/proxy-video")
async def proxy_video(request: Request, url: str, sig: str = "", exp: str = "", request_host: str = ""):
    target = url.strip()
    headers = get_dynamic_headers(target, request.headers)

    for attempt in range(3):
        try:
            client = httpx.AsyncClient(timeout=60.0, follow_redirects=True)
            req = client.build_request("GET", target, headers=headers)
            resp = await client.send(req, stream=True)
            if resp.status_code in [200, 206]:
                resp_headers = {
                    "Access-Control-Allow-Origin": "*",
                    "Accept-Ranges": "bytes",
                    "Content-Type": "video/mp4"
                }
                for k in ["Content-Type", "Content-Length", "Content-Range"]:
                    if k in resp.headers and resp.headers[k]:
                        resp_headers[k] = resp.headers[k]

                async def stream_generator():
                    try:
                        async for chunk in resp.aiter_bytes(chunk_size=65536):
                            yield chunk
                    finally:
                        await resp.aclose()
                        await client.aclose()

                return StreamingResponse(
                    stream_generator(), 
                    status_code=resp.status_code, 
                    headers=resp_headers
                )
        except Exception:
            await asyncio.sleep(0.5)
    return Response(status_code=502)

@app.get("/")
def health():
    return {"status": "Online", "engine": "Fast Edge Extraction Engine"}
