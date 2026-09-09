import os
import time
import asyncio
import logging
import re
from urllib.parse import quote, unquote
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

app = FastAPI(title="Media Extraction Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

COMMON_USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'

def clean_thumbnail_url(raw_url: str) -> str:
    if not raw_url:
        return ""
    raw_url = str(raw_url).strip()
    if raw_url.startswith("data:") or "base64," in raw_url or "blank.gif" in raw_url or "pixel.gif" in raw_url:
        return ""
    if ',' in raw_url and ('http://' in raw_url or 'https://' in raw_url):
        parts = re.split(r',\s*', raw_url)
        candidates = [p.strip().split(' ')[0] for p in parts if p.strip().startswith('http') and not p.strip().startswith('data:')]
        if candidates:
            raw_url = candidates[-1]
    if raw_url.startswith('//'):
        raw_url = "https:" + raw_url
    if not raw_url.startswith('http'):
        return ""
    return raw_url

def clean_media_stream_url(raw_url: str) -> str:
    if not raw_url:
        return ""
    raw_url = raw_url.strip().replace('\\/', '/')
    raw_url = re.sub(r'(\.(?:mp4|m3u8|webm|mov|mkv))/+(?=$|\?)', r'\1', raw_url, flags=re.IGNORECASE)
    if re.search(r'\.(?:mp4|m3u8|webm)/+$', raw_url, re.IGNORECASE):
        raw_url = re.sub(r'/+$', '', raw_url)
    return raw_url

def is_invalid_title(t: str) -> bool:
    if not t:
        return True
    t_clean = t.strip()
    if t_clean.isdigit() or len(t_clean) <= 2:
        return True
    if re.search(r'^(?:[A-Za-z]{2,3})?\s*\d{1,2}:\d{2}(?::\d{2})?$', t_clean):
        return True
    if re.search(r'\d{1,2}:\d{2}', t_clean) and len(t_clean) <= 12:
        return True
    return False

def extract_kvs_direct(url: str, provider: str) -> dict:
    base_domain = "https://ok.xxx" if "ok.xxx" in url else "https://www.pornhat.com"
    referer = f"{base_domain}/"
    headers = {
        'User-Agent': COMMON_USER_AGENT,
        'Referer': referer,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Cookie': 'has_accepted_cookie=1; age_verified=1; platform=pc;'
    }
    for attempt in range(3):
        try:
            r = requests.get(url, headers=headers, timeout=6)
            if r.status_code == 200:
                html_text = r.text
                soup = BeautifulSoup(html_text, 'html.parser')

                title = ""
                og_title = soup.find('meta', property='og:title')
                if og_title and og_title.get('content'):
                    title = og_title.get('content').strip()
                if not title or is_invalid_title(title):
                    h1 = soup.find('h1')
                    if h1: title = h1.get_text(strip=True)
                if not title or is_invalid_title(title):
                    title_tag = soup.find('title')
                    if title_tag: title = title_tag.get_text(strip=True).split(' - ')[0].split(' | ')[0]
                if is_invalid_title(title): title = "Video Stream"

                all_thumbs = []
                og_img = soup.find('meta', property='og:image')
                if og_img and og_img.get('content'):
                    c = clean_thumbnail_url(og_img.get('content'))
                    if c: all_thumbs.append(c)

                prev_match = re.search(r'preview_url\s*:\s*[\'"]([^\'"]+)[\'"]', html_text)
                if prev_match:
                    c = clean_thumbnail_url(prev_match.group(1))
                    if c and c not in all_thumbs: all_thumbs.append(c)

                for v in soup.select('video[poster], [data-poster], [data-webp], [data-original], [data-thumb]'):
                    for attr in ['poster', 'data-poster', 'data-webp', 'data-original', 'data-thumb']:
                        val = v.get(attr, '')
                        c = clean_thumbnail_url(val)
                        if c and c not in all_thumbs: all_thumbs.append(c)

                for img in soup.select('img'):
                    for attr in ['src', 'data-src', 'data-original', 'data-webp', 'data-thumb', 'data-lazy-src', 'data-lazy', 'data-preview']:
                        val = img.get(attr, '')
                        c = clean_thumbnail_url(val)
                        if c and c not in all_thumbs: all_thumbs.append(c)

                thumbnail = all_thumbs[0] if all_thumbs else ""

                duration = 0
                dur_m = re.search(r'(?:video_duration|duration)\s*:\s*[\'"]?(\d+)[\'"]?', html_text)
                if dur_m: duration = int(dur_m.group(1))

                view_count = 0
                v_match = re.search(r'([\d,\.]+)\s*(?:Views|views)', html_text)
                if v_match:
                    raw_v = v_match.group(1).replace(',', '').replace('.', '')
                    if raw_v.isdigit(): view_count = int(raw_v)

                upload_date = ""
                date_tag = soup.find('meta', itemprop='uploadDate') or soup.find('meta', property='video:release_date')
                if date_tag and date_tag.get('content'):
                    upload_date = date_tag.get('content')[:10]
                if not upload_date:
                    dm = re.search(r'(\d{4}-\d{2}-\d{2})', html_text)
                    if dm: upload_date = dm.group(1)

                qualities_map = {}
                matches = []
                for m in re.finditer(r'(?:video_url|video_alt_url\d*)\s*:\s*[\'"]([^\'"]+)[\'"]', html_text):
                    matches.append(m.group(1))

                for v in soup.select('video source, video[src]'):
                    src = v.get('src', '')
                    if src: matches.append(src)

                if not matches:
                    json_matches = re.findall(r'[\'"](?:file|src|url)[\'"]\s*:\s*[\'"](https?://[^\'"]+\.(?:mp4|m3u8)[^\'"]*)[\'"]', html_text)
                    matches.extend(json_matches)

                seen = set()
                for raw_m in matches:
                    stream_url = clean_media_stream_url(raw_m)
                    if not stream_url.startswith('http') and stream_url.startswith('/'):
                        stream_url = base_domain + stream_url

                    if not stream_url.startswith('http') or stream_url in seen:
                        continue
                    seen.add(stream_url)

                    is_hls = '.m3u8' in stream_url
                    res_m = re.search(r'(\d{3,4})[pP]', stream_url)
                    if res_m:
                        q_label = f"{res_m.group(1)}p"
                        height = int(res_m.group(1))
                    elif is_hls:
                        q_label = "Auto"
                        height = 9999
                    else:
                        q_label = "720p"
                        height = 720

                    if q_label not in qualities_map:
                        qualities_map[q_label] = {
                            "quality": q_label,
                            "url": stream_url,
                            "type": "hls" if is_hls else "mp4",
                            "height": height
                        }

                qual_list = list(qualities_map.values())
                qual_list.sort(key=lambda x: x["height"], reverse=True)
                for q in qual_list: q.pop("height", None)

                if qual_list:
                    return {
                        "status": "success",
                        "title": html_parser.unescape(title),
                        "thumbnail": thumbnail,
                        "thumbnails": all_thumbs if all_thumbs else ([thumbnail] if thumbnail else []),
                        "duration": duration,
                        "upload_date": upload_date,
                        "view_count": view_count,
                        "streams": {"qualities": qual_list},
                        "url": url,
                        "provider": provider
                    }
        except Exception as e:
            logger.error(f"extract_kvs_direct error for {url}: {e}")
            time.sleep(0.5)

    return {"status": "error", "error": f"Failed to extract direct streams for {url}"}

def search_pornhub_with_ytdlp(q: str, page: int) -> list:
    videos = []
    search_term = f"phsearch48:{q}"
    ydl_opts = {'quiet': True, 'no_warnings': True, 'extract_flat': 'in_playlist', 'skip_download': True, 'nocheckcertificate': True, 'age_limit': 21}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_term, download=False)
            entries = info.get('entries', []) if info else []
            for entry in entries:
                if not entry: continue
                vkey = entry.get('id') or ''
                url = entry.get('url') or f"https://www.pornhub.com/view_video.php?viewkey={vkey}"
                title = html_parser.unescape(entry.get('title', 'Unknown Video'))
                if any(bad in url.lower() or bad in title.lower() for bad in ['/join', 'sponsor', 'promo', 'ad/']): continue
                if is_invalid_title(title): continue
                thumb = clean_thumbnail_url(entry.get('thumbnail', ''))
                videos.append({"vkey": vkey, "title": title, "thumbnail": thumb, "url": url, "provider": "pornhub"})
    except Exception as e:
        logger.error(f"yt-dlp search error Pornhub: {e}")
    return videos

def search_youporn_with_ytdlp(q: str, page: int) -> list:
    videos = []
    search_term = f"ypsearch48:{q}"
    ydl_opts = {'quiet': True, 'no_warnings': True, 'extract_flat': 'in_playlist', 'skip_download': True, 'nocheckcertificate': True, 'age_limit': 21}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_term, download=False)
            entries = info.get('entries', []) if info else []
            for entry in entries:
                if not entry: continue
                vkey = entry.get('id') or ''
                url = entry.get('url') or f"https://www.youporn.com/watch/{vkey}"
                title = html_parser.unescape(entry.get('title', 'Unknown Video'))
                if any(bad in url.lower() or bad in title.lower() for bad in ['/join', 'sponsor', 'promo', 'ad/']): continue
                if is_invalid_title(title): continue
                thumb = clean_thumbnail_url(entry.get('thumbnail', ''))
                videos.append({"vkey": vkey, "title": title, "thumbnail": thumb, "url": url, "provider": "youporn"})
    except Exception as e:
        logger.error(f"yt-dlp search error YouPorn: {e}")
    return videos

def search_xhamster_with_ytdlp(q: str, page: int) -> list:
    videos = []
    search_url = f"https://xhamster.com/search/{quote(q)}" if page <= 1 else f"https://xhamster.com/search/{quote(q)}/{page}"
    ydl_opts = {'quiet': True, 'no_warnings': True, 'extract_flat': 'in_playlist', 'skip_download': True, 'nocheckcertificate': True, 'age_limit': 21}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_url, download=False)
            entries = info.get('entries', []) if info else []
            for entry in entries:
                if not entry: continue
                url = entry.get('url', '')
                vkey = entry.get('id', '')
                if not vkey and url:
                    parts = [p for p in url.split('/') if p]
                    vkey = parts[-1] if parts else ""
                if not vkey or '/videos/' not in url: continue
                title = html_parser.unescape(entry.get('title', f"Video {vkey}"))
                if any(bad in url.lower() or bad in title.lower() for bad in ['/join', 'sponsor', 'promo', 'ad/']): continue
                if is_invalid_title(title): continue
                thumb = entry.get('thumbnail', '')
                if not thumb and entry.get('thumbnails'): thumb = entry.get('thumbnails')[0].get('url', '')
                videos.append({"vkey": vkey, "title": title, "thumbnail": clean_thumbnail_url(thumb), "url": url if url.startswith('http') else f"https://xhamster.com/videos/{vkey}", "provider": "xhamster"})
    except Exception as e:
        logger.error(f"yt-dlp search error xHamster: {e}")
    return videos

def search_redtube_with_ytdlp(q: str, page: int) -> list:
    videos = []
    search_url = f"https://www.redtube.com/?search={quote(q)}" if page <= 1 else f"https://www.redtube.com/?search={quote(q)}&page={page}"
    ydl_opts = {'quiet': True, 'no_warnings': True, 'extract_flat': 'in_playlist', 'skip_download': True, 'nocheckcertificate': True, 'age_limit': 21}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_url, download=False)
            entries = info.get('entries', []) if info else []
            for entry in entries:
                if not entry: continue
                url = entry.get('url', '')
                vkey = entry.get('id', '')
                if not vkey and url:
                    parts = [p for p in url.split('/') if p]
                    vkey = parts[-1] if parts else ""
                if not vkey or not vkey.isdigit(): continue
                title = html_parser.unescape(entry.get('title', f"Video {vkey}"))
                if any(bad in url.lower() or bad in title.lower() for bad in ['/join', 'sponsor', 'promo', 'ad/', '/channels/', '/pornstar/', '/amateur/']): continue
                if is_invalid_title(title) or 'information/' in url: continue
                thumb = entry.get('thumbnail', '')
                videos.append({"vkey": vkey, "title": title, "thumbnail": clean_thumbnail_url(thumb), "url": url if url.startswith('http') else f"https://www.redtube.com/{vkey}", "provider": "redtube"})
    except Exception as e:
        logger.error(f"yt-dlp search error RedTube: {e}")
    return videos

def parse_metadata_fallback(url: str, provider: str) -> dict:
    base_domain = "https://www.pornhub.com"
    if "xhamster.com" in url: base_domain = "https://xhamster.com"
    elif "xnxx.com" in url: base_domain = "https://www.xnxx.com"
    elif "xvideos.com" in url: base_domain = "https://www.xvideos.com"
    elif "redtube.com" in url: base_domain = "https://www.redtube.com"
    elif "youporn.com" in url: base_domain = "https://www.youporn.com"
    elif "ok.xxx" in url: base_domain = "https://ok.xxx"
    elif "pornhat.com" in url: base_domain = "https://www.pornhat.com"

    url = re.sub(r'https?://[a-zA-Z0-9-]+\.' + provider + r'\.com', base_domain, url)
    headers = {'User-Agent': COMMON_USER_AGENT, 'Accept-Language': 'en-US,en;q=0.9', 'Cookie': 'has_accepted_cookie=1; age_verified=1;'}
    try:
        resp = requests.get(url, headers=headers, timeout=5.0)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, 'html.parser')
            view_count = 0
            upload_date = ""
            poster_url = ""
            scraped_title = ""

            og_title = soup.find('meta', property='og:title')
            if og_title and og_title.get('content'): scraped_title = og_title.get('content').strip()
            if not scraped_title:
                t_tag = soup.find('title')
                if t_tag: scraped_title = t_tag.get_text().split(' - ')[0].split(' | ')[0].strip()

            og_img = soup.find('meta', property='og:image')
            if og_img and og_img.get('content'):
                cleaned = clean_thumbnail_url(og_img.get('content'))
                if cleaned: poster_url = cleaned

            if not poster_url:
                img_json = re.search(r'"image_url"\s*:\s*"([^"]+)"', resp.text)
                if img_json:
                    cleaned = clean_thumbnail_url(img_json.group(1).replace('\\/', '/'))
                    if cleaned: poster_url = cleaned

            if not poster_url:
                for img in soup.select('img'):
                    for attr in ['src', 'data-src', 'data-original', 'data-webp', 'data-thumb', 'data-lazy-src', 'data-preview']:
                        val = img.get(attr, '')
                        cleaned = clean_thumbnail_url(val)
                        if cleaned:
                            poster_url = cleaned
                            break
                    if poster_url: break

            if not poster_url:
                prev_match = re.search(r'preview_url\s*:\s*[\'"]([^\'"]+)[\'"]', resp.text)
                if prev_match:
                    cleaned = clean_thumbnail_url(prev_match.group(1))
                    if cleaned: poster_url = cleaned

            return {"view_count": view_count, "upload_date": upload_date, "thumbnail": poster_url, "title": scraped_title}
    except Exception:
        pass
    return {"view_count": 0, "upload_date": "", "thumbnail": "", "title": ""}

def search_provider_robust(provider: str, q: str, page: int):
    cache_key = f"{provider}:{q}:{page}"
    if cache_key in search_cache: return search_cache[cache_key]

    videos = []
    headers = {
        'User-Agent': COMMON_USER_AGENT,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Cookie': 'has_accepted_cookie=1; age_verified=1; platform=pc;'
    }

    if provider == "youporn": search_url = f"https://www.youporn.com/search/?query={quote(q)}&page={page}"
    elif provider == "xhamster": search_url = f"https://xhamster.com/search/{quote(q)}" if page <= 1 else f"https://xhamster.com/search/{quote(q)}/{page}"
    elif provider == "redtube": search_url = f"https://www.redtube.com/?search={quote(q)}" if page <= 1 else f"https://www.redtube.com/?search={quote(q)}&page={page}"
    elif provider == "xnxx": search_url = f"https://www.xnxx.com/search/{quote(q)}" if page <= 1 else f"https://www.xnxx.com/search/{quote(q)}/{page}"
    elif provider == "xvideos":
        p_val = page - 1 if page > 1 else 0
        search_url = f"https://www.xvideos.com/?k={quote(q)}" if p_val == 0 else f"https://www.xvideos.com/?k={quote(q)}&p={p_val}"
    elif provider in ["okxxx", "ok", "ok.xxx"]: search_url = f"https://ok.xxx/search/{quote(q)}/" if page <= 1 else f"https://ok.xxx/search/{quote(q)}/{page}/"
    elif provider in ["pornhat", "pornhat.com"]: search_url = f"https://www.pornhat.com/sites/{quote(q)}/" if page <= 1 else f"https://www.pornhat.com/sites/{quote(q)}/{page}/"
    else: search_url = f"https://www.pornhub.com/video/search?search={quote(q)}&page={page}"

    for attempt in range(3):
        try:
            resp = requests.get(search_url, headers=headers, timeout=10)
            if resp.status_code == 404 and provider in ["pornhat", "pornhat.com"]:
                search_url = f"https://www.pornhat.com/search/{quote(q)}/" if page <= 1 else f"https://www.pornhat.com/search/{quote(q)}/{page}/"
                resp = requests.get(search_url, headers=headers, timeout=10)

            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, 'html.parser')
                seen = set()

                if provider in ["okxxx", "ok", "ok.xxx", "pornhat", "pornhat.com"]:
                    is_ok = provider in ["okxxx", "ok", "ok.xxx"]
                    domain = "ok.xxx" if is_ok else "www.pornhat.com"
                    items = soup.select('div.item, div.video-item, div.thumb-block, div.thumb, div[class*="item"], div[class*="video"], a[href*="/video/"]')
                    for item in items:
                        a_tag = item if item.name == 'a' else item.select_one('a[href*="/video/"]')
                        if not a_tag: continue
                        href = a_tag.get('href', '')
                        if not href or '/video/' not in href: continue

                        if is_ok:
                            match_id = re.search(r'/video/(\d+)', href)
                            if not match_id: continue
                            vid_id = match_id.group(1)
                            full_url = f"https://ok.xxx/video/{vid_id}/"
                        else:
                            parts = [p for p in href.split('?')[0].split('/') if p]
                            vid_id = parts[-1] if parts else "unknown"
                            full_url = href if href.startswith('http') else f"https://{domain}{href}"

                        full_url = full_url.split('?')[0].rstrip('/')
                        if full_url in seen: continue
                        seen.add(full_url)

                        title_tag = item.select_one('.title, a.title, .video-title, strong, h3, h4, p') if item.name != 'a' else a_tag
                        title = title_tag.get('title') or title_tag.get_text(strip=True) if title_tag else ""
                        if is_invalid_title(title): title = a_tag.get('title', '')

                        container = item if item.name != 'a' else (item.parent if item.parent else item)
                        thumb = ""
                        for noscript in container.select('noscript'):
                            ns_match = re.search(r'https?://[^\s<>"\']+\.(?:jpg|jpeg|png|webp)', noscript.text)
                            if ns_match:
                                cleaned = clean_thumbnail_url(ns_match.group(0))
                                if cleaned: thumb = cleaned; break

                        if not thumb:
                            for img in container.select('img'):
                                if is_invalid_title(title): title = img.get('alt') or title
                                for attr in ['data-webp', 'data-original', 'data-src', 'data-lazy-src', 'data-thumb', 'data-image', 'data-poster', 'src']:
                                    val = img.get(attr, '')
                                    cleaned = clean_thumbnail_url(val)
                                    if cleaned: thumb = cleaned; break
                                if thumb: break

                        if not thumb:
                            meta_res = parse_metadata_fallback(full_url, "okxxx" if is_ok else "pornhat")
                            if meta_res.get("thumbnail"): thumb = meta_res["thumbnail"]

                        if any(bad in full_url.lower() or bad in title.lower() for bad in ['/join', 'sponsor', 'promo', 'ad/']): continue
                        if is_invalid_title(title): continue

                        prov_key = "okxxx" if is_ok else "pornhat"
                        videos.append({"vkey": vid_id, "title": html_parser.unescape(title), "thumbnail": thumb, "url": full_url, "provider": prov_key})
                        if len(videos) >= 48: break
                
                elif provider == "pornhub":
                    items = soup.select('li.videoblock, li.pcVideoListItem, li.js-pop, li.videoBox')
                    for item in items:
                        vkey = item.get("data-video-vkey")
                        if not vkey: continue
                        full_url = f"https://www.pornhub.com/view_video.php?viewkey={vkey}"
                        if full_url in seen: continue
                        title_tag = item.select_one('.title a, a.title')
                        title = title_tag.get_text(strip=True) if title_tag else "Video"
                        if is_invalid_title(title): continue
                        seen.add(full_url)
                        img_tag = item.select_one('img')
                        thumb = clean_thumbnail_url(img_tag.get('data-thumb_url') or img_tag.get('data-src') or img_tag.get('src') or "") if img_tag else ""
                        videos.append({"vkey": vkey, "title": html_parser.unescape(title), "thumbnail": thumb, "url": full_url, "provider": "pornhub"})
                        if len(videos) >= 48: break

                elif provider == "xhamster":
                    items = soup.select('div.thumb-list__item, article.video-thumb, div[class*="video-thumb"]')
                    for item in items:
                        a_tag = item.select_one('a[href*="/videos/"]')
                        if not a_tag: continue
                        full_url = a_tag.get('href', '').split('?')[0].rstrip('/')
                        if not full_url.startswith('http'): full_url = "https://xhamster.com" + full_url
                        if full_url in seen: continue
                        vid_id = full_url.split('/')[-1]
                        title = a_tag.get('title') or a_tag.get_text(strip=True)
                        if is_invalid_title(title): title = vid_id.replace('-', ' ').title()
                        seen.add(full_url)
                        img = item.select_one('img')
                        thumb = clean_thumbnail_url(img.get('data-src') or img.get('src') or "") if img else ""
                        videos.append({"vkey": vid_id, "title": html_parser.unescape(title), "thumbnail": thumb, "url": full_url, "provider": "xhamster"})
                        if len(videos) >= 48: break

                if len(videos) > 0:
                    search_cache[cache_key] = videos
                    return videos
        except Exception as e:
            logger.error(f"Search error {provider}: {e}")
            time.sleep(1.0)

    search_cache[cache_key] = videos
    return videos

def extract_with_ytdlp(url: str) -> dict:
    if url in extraction_cache: return extraction_cache[url]

    provider = "pornhub"
    if "xhamster.com" in url: provider = "xhamster"
    elif "xnxx.com" in url: provider = "xnxx"
    elif "xvideos.com" in url: provider = "xvideos"
    elif "redtube.com" in url: provider = "redtube"
    elif "youporn.com" in url: provider = "youporn"
    elif "ok.xxx" in url: provider = "okxxx"
    elif "pornhat.com" in url: provider = "pornhat"

    if provider in ["okxxx", "pornhat"]:
        res = extract_kvs_direct(url, provider)
        if res.get("status") == "success":
            extraction_cache[url] = res
            return res

    ydl_opts = {
        'quiet': True, 'no_warnings': True, 'extract_flat': False,
        'format': 'bestvideo+bestaudio/best', 'nocheckcertificate': True, 'age_limit': 21,
        'http_headers': {'User-Agent': COMMON_USER_AGENT, 'Referer': f'https://www.{provider}.com/'}
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False) or {}
            title = html_parser.unescape(info.get('title', 'Unknown Video'))
            duration = info.get('duration', 0)
            upload_date = info.get('upload_date', '')
            view_count = info.get('view_count', 0)

            qualities_dict = {}
            for f in info.get('formats', []):
                f_url = clean_media_stream_url(f.get('url', ''))
                if not f_url or f.get('vcodec') == 'none': continue
                is_hls = '.m3u8' in f_url or f.get('ext') == 'm3u8'
                height = f.get('height', 0)
                q_label = f"{height}p" if height else ("Auto" if is_hls else "720p")
                qualities_dict[q_label] = {"quality": q_label, "url": f_url, "type": "hls" if is_hls else "mp4"}

            qualities = list(qualities_dict.values())
            if qualities:
                result = {
                    "status": "success", "title": title,
                    "thumbnail": clean_thumbnail_url(info.get('thumbnail', '')),
                    "thumbnails": [clean_thumbnail_url(info.get('thumbnail', ''))],
                    "duration": duration, "upload_date": upload_date, "view_count": view_count,
                    "streams": {"qualities": qualities}, "url": url, "provider": provider
                }
                extraction_cache[url] = result
                return result
    except Exception as e:
        logger.error(f"yt-dlp extract error: {e}")

    return {"status": "error", "error": "No valid streams found", "url": url}

@app.get("/api/explore")
async def explore(q: str = "brazzers", page: int = 1, provider: str = "pornhub"):
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(thread_pool, search_provider_robust, provider, q, page)
    return JSONResponse(res)

@app.get("/api/extract")
async def extract_endpoint(url: str):
    if not url: return JSONResponse({"status": "error", "error": "Missing URL"})
    target_url = url.strip().strip('"\'')
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(thread_pool, extract_with_ytdlp, target_url)
    return JSONResponse(res)

@app.get("/proxy-image")
async def fallback_proxy_image(url: str):
    target = url.strip()
    if target.startswith('//'): target = "https:" + target
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        req = await client.get(target, headers={'User-Agent': COMMON_USER_AGENT, 'Referer': 'https://ok.xxx/'})
        if req.status_code == 200:
            return StreamingResponse(req.aiter_bytes(), status_code=200, headers={"Content-Type": req.headers.get("Content-Type", "image/jpeg"), "Access-Control-Allow-Origin": "*"})
    return Response(status_code=404)

@app.get("/proxy-m3u8")
async def proxy_m3u8(request: Request, url: str):
    target = clean_media_stream_url(url)
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        resp = await client.get(target, headers={"User-Agent": COMMON_USER_AGENT})
        if resp.status_code == 200:
            return Response(resp.text, media_type="application/vnd.apple.mpegurl", headers={"Access-Control-Allow-Origin": "*"})
    return Response(status_code=502)

@app.get("/proxy-video")
async def proxy_video(request: Request, url: str):
    target = clean_media_stream_url(url)
    headers = {"User-Agent": COMMON_USER_AGENT, "Referer": "https://ok.xxx/"}
    if "range" in request.headers: headers["Range"] = request.headers["range"]
    client = httpx.AsyncClient(timeout=60.0, follow_redirects=True)
    req = client.build_request("GET", target, headers=headers)
    resp = await client.send(req, stream=True)
    
    resp_headers = {
        "Access-Control-Allow-Origin": "*",
        "Accept-Ranges": "bytes",
        "Content-Type": "video/mp4"
    }
    for k in ["Content-Length", "Content-Range"]:
        if k in resp.headers:
            resp_headers[k] = resp.headers[k]

    return StreamingResponse(resp.aiter_bytes(), status_code=resp.status_code, headers=resp_headers)

@app.get("/")
def health():
    return {"status": "Online", "engine": "Fast Edge Extraction Engine"}
