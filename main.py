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
                videos.append({
                    "vkey": vkey,
                    "title": html_parser.unescape(entry.get('title', 'Unknown Video')),
                    "thumbnail": entry.get('thumbnail', ''),
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
                videos.append({
                    "vkey": vkey,
                    "title": html_parser.unescape(entry.get('title', 'Unknown Video')),
                    "thumbnail": entry.get('thumbnail', ''),
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
                if not vkey:
                    continue
                title = html_parser.unescape(entry.get('title', f"Video {vkey}"))
                thumb = entry.get('thumbnail', '')
                if not thumb and entry.get('thumbnails'):
                    thumb = entry.get('thumbnails')[0].get('url', '')
                videos.append({
                    "vkey": vkey,
                    "title": title,
                    "thumbnail": thumb,
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
                if not vkey:
                    continue
                title = html_parser.unescape(entry.get('title', f"Video {vkey}"))
                thumb = entry.get('thumbnail', '')
                videos.append({
                    "vkey": vkey,
                    "title": title,
                    "thumbnail": thumb,
                    "url": url if url.startswith('http') else f"https://www.redtube.com/{vkey}",
                    "provider": "redtube"
                })
    except Exception as e:
        logger.error(f"yt-dlp fallback search error for RedTube: {e}")
    return videos

def search_provider_robust(provider: str, q: str, page: int):
    cache_key = f"{provider}:{q}:{page}"
    if cache_key in search_cache:
        return search_cache[cache_key]

    videos = []
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Sec-Ch-Ua': '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        'Sec-Ch-Ua-Mobile': '?0',
        'Sec-Ch-Ua-Platform': '"Windows"',
        'Cookie': 'has_accepted_cookie=1; age_verified=1; platform=pc; yp_access_confirmed=1; accessAgeConfirmed=1;'
    }

    if provider == "youporn":
        search_url = f"https://www.youporn.com/search/?query={quote(q)}&page={page}"
        headers['Referer'] = 'https://www.youporn.com/'
    elif provider == "xhamster":
        search_url = f"https://xhamster.com/search/{quote(q)}" if page <= 1 else f"https://xhamster.com/search/{quote(q)}/{page}"
        headers['Referer'] = 'https://xhamster.com/'
    elif provider == "redtube":
        search_url = f"https://www.redtube.com/?search={quote(q)}" if page <= 1 else f"https://www.redtube.com/?search={quote(q)}&page={page}"
        headers['Referer'] = 'https://www.redtube.com/'
    elif provider == "xnxx":
        search_url = f"https://www.xnxx.com/search/{quote(q)}" if page <= 1 else f"https://www.xnxx.com/search/{quote(q)}/{page}"
        headers['Referer'] = 'https://www.xnxx.com/'
    elif provider == "xvideos":
        p_val = page - 1 if page > 1 else 0
        search_url = f"https://www.xvideos.com/?k={quote(q)}" if p_val == 0 else f"https://www.xvideos.com/?k={quote(q)}&p={p_val}"
        headers['Referer'] = 'https://www.xvideos.com/'
    else:
        search_url = f"https://www.pornhub.com/video/search?search={quote(q)}&page={page}"
        headers['Referer'] = 'https://www.pornhub.com/'

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
                                if not title or title.isdigit() or re.match(r'^(?:ES|PT)?\d{1,2}:\d{2}', title):
                                    title = img_tag.get('alt') or title
                                thumb = (img_tag.get('data-src') or img_tag.get('src') or img_tag.get('data-lazy-src') or img_tag.get('data-image') or img_tag.get('data-thumb') or img_tag.get('data-poster') or "")
                        else:
                            a_tag = item.select_one('a[href*="/watch/"]')
                            if not a_tag: continue
                            href = a_tag.get('href', '')
                            full_url = href if href.startswith('http') else f"https://www.youporn.com{href}"
                            title_tag = item.select_one('a[href*="/watch/"] [title], p.title, span.title, a, div.title, h3, h4')
                            title = title_tag.get('title') or title_tag.get('alt') or title_tag.get_text(strip=True) if title_tag else a_tag.get('title', '')
                            img_tag = item.select_one('img')
                            if img_tag:
                                if not title or title == "Unknown Video" or title.isdigit() or re.match(r'^(?:ES|PT)?\d{1,2}:\d{2}', title):
                                    title = img_tag.get('alt') or title
                                thumb = (img_tag.get('data-src') or img_tag.get('src') or img_tag.get('data-lazy-src') or img_tag.get('data-image') or img_tag.get('data-thumb') or img_tag.get('data-poster') or "")

                        full_url = full_url.split('?')[0].rstrip('/')
                        if not full_url or full_url in seen: continue
                        seen.add(full_url)

                        match_id = re.search(r'/watch/(\d+)', full_url)
                        if not match_id: continue
                        vkey = match_id.group(1)

                        if not title or title.isdigit() or re.match(r'^(?:ES|PT)?\d{1,2}:\d{2}', title) or 'youporn' in title.lower():
                            title = f"YouPorn Video {vkey}"

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
                        seen.add(full_url)

                        title_tag = item.select_one('.title a, a.title, img[alt]')
                        title = title_tag.get('alt') or title_tag.get_text(strip=True) if title_tag else "Unknown Video"
                        
                        img_tag = item.select_one('img')
                        thumb = img_tag.get('data-thumb_url') or img_tag.get('data-mediumthumb') or img_tag.get('data-image') or img_tag.get('data-src') or img_tag.get('src') or img_tag.get('data-lazy-src') or "" if img_tag else ""

                        videos.append({"vkey": vkey, "title": html_parser.unescape(title), "thumbnail": thumb, "url": full_url, "provider": "pornhub"})
                        if len(videos) >= 48: break

                elif provider == "xhamster":
                    items = soup.select('div.video-thumb, div.thumb-list__item, div.video-container, div.cell, article, div.video-thumb-info, div[class*="video-thumb"]')
                    for item in items:
                        a_tag = item.select_one('a[href*="/videos/"], a[href*="/movie/"], a[href*="/pornstar/"]')
                        if not a_tag: continue
                        href = a_tag.get('href', '')
                        full_url = href if href.startswith('http') else f"https://xhamster.com{href}"
                        full_url = full_url.split('?')[0].rstrip('/')
                        if full_url in seen: continue
                        seen.add(full_url)
                        
                        vid_parts = [p for p in full_url.split('/') if p]
                        vid_id = vid_parts[-1] if vid_parts else "unknown"

                        title_tag = item.select_one('a.video-thumb__title, a[title], h4, p, span.title')
                        title = title_tag.get('title') or title_tag.get_text(strip=True) if title_tag else vid_id.replace('-', ' ').title()

                        if "results" in title.lower() or not a_tag.get('href'):
                            continue

                        thumb = ""
                        for attr_name in ['data-image', 'data-poster', 'data-src', 'data-background', 'content', 'data-thumb']:
                            val = item.get(attr_name, '')
                            if val and val.startswith('http') and 'svg' not in val and 'logo' not in val:
                                thumb = val
                                break

                        if not thumb:
                            img_tag = item.select_one('img')
                            if img_tag:
                                for attr_name in ['data-src', 'src', 'data-lazy-src', 'data-thumb', 'data-image', 'srcset']:
                                    src_candidate = img_tag.get(attr_name, '')
                                    if src_candidate:
                                        match_url = re.search(r'https?://[^\s<>"]+?\.(?:jpg|jpeg|png|webp)', src_candidate)
                                        if match_url:
                                            candidate = match_url.group(0)
                                            if 'svg' not in candidate and 'logo' not in candidate and 'results' not in candidate:
                                                thumb = candidate
                                                break
                                if thumb: break

                        if not thumb:
                            match_img = re.search(r'https?://[^\s<>"]+?\.(?:jpg|jpeg|png|webp)', str(item))
                            if match_img:
                                candidate = match_img.group(0)
                                if 'logo' not in candidate and 'svg' not in candidate and 'results' not in candidate:
                                    thumb = candidate

                        videos.append({"vkey": vid_id, "title": html_parser.unescape(title), "thumbnail": thumb, "url": full_url, "provider": "xhamster"})
                        if len(videos) >= 48: break

                elif provider == "redtube":
                    items = soup.select('div.videoBox, li.videoblock, div.video-item, div.pb-card, div[class*="video"]')
                    for item in items:
                        a_tag = item.select_one('a[href]')
                        if not a_tag: continue
                        href = a_tag.get('href', '')
                        if not href or ('/' not in href and not any(char.isdigit() for char in href)): continue
                        if 'search=' in href or '/hot' in href: continue

                        full_url = href if href.startswith('http') else f"https://www.redtube.com{href}"
                        if full_url in seen: continue
                        seen.add(full_url)

                        vid_parts = [p for p in full_url.split('/') if p]
                        vid_id = vid_parts[-1] if vid_parts else "unknown"

                        title_tag = item.select_one('.video-title-text, a[title], span.title, a, p, h3, h4')
                        title = ""
                        if title_tag:
                            title = title_tag.get('title') or title_tag.get_text(strip=True)

                        if not title or title == vid_id or title.isdigit() or re.match(r'^(?:ES|PT)?\d{1,2}:\d{2}', title) or 'redtube' in title.lower():
                            alt_title = a_tag.get('title', '')
                            if alt_title and alt_title != vid_id and not alt_title.isdigit() and not re.match(r'^(?:ES|PT)?\d{1,2}:\d{2}', alt_title):
                                title = alt_title
                            else:
                                img_alt = item.select_one('img[alt]')
                                if img_alt and img_alt.get('alt') and img_alt.get('alt') != vid_id and not re.match(r'^(?:ES|PT)?\d{1,2}:\d{2}', img_alt.get('alt')):
                                    title = img_alt.get('alt')
                                else:
                                    title = vid_id.replace('-', ' ').title()

                        img_tag = item.select_one('img')
                        thumb = ""
                        if img_tag:
                            thumb = (img_tag.get('data-src') or img_tag.get('src') or img_tag.get('data-lazy-src') or img_tag.get('data-image') or img_tag.get('data-thumb') or "")
                        if not thumb:
                            match_img = re.search(r'https?://[^\s<>"]+?\.(?:jpg|jpeg|png|webp)', str(item))
                            if match_img:
                                candidate = match_img.group(0)
                                if 'logo' not in candidate and 'svg' not in candidate:
                                    thumb = candidate

                        videos.append({"vkey": vid_id, "title": html_parser.unescape(title), "thumbnail": thumb, "url": full_url, "provider": "redtube"})
                        if len(videos) >= 48: break

                elif provider == "xnxx":
                    items = soup.select('div.mozaique div.thumb-block')
                    for item in items:
                        a_tag = item.select_one('a[href*="/video-"], a[href*="/video."]')
                        if not a_tag: continue
                        href = a_tag.get('href', '')
                        full_url = f"https://www.xnxx.com{href}" if href.startswith('/') else href
                        if full_url in seen: continue
                        seen.add(full_url)

                        vid_id = href.split('/')[1] if len(href.split('/')) > 1 else href
                        title_tag = item.select_one('div.thumb-under a[title], div.thumb-under a')
                        title = title_tag.get('title') or title_tag.get_text(strip=True) if title_tag else "Unknown Video"

                        img_tag = item.select_one('img')
                        thumb = img_tag.get('data-src') or img_tag.get('src') or item.select_one('[data-videothumb]').get('data-videothumb', '') if img_tag else ""

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
                        if full_url in seen: continue
                        seen.add(full_url)

                        title_tag = item.select_one('p.title a')
                        title = title_tag.get('title') or title_tag.get_text(strip=True) if title_tag else "Unknown Video"

                        img_tag = item.select_one('img')
                        thumb = img_tag.get('data-src') or img_tag.get('src') or "" if img_tag else ""

                        videos.append({"vkey": vid_id, "title": html_parser.unescape(title), "thumbnail": thumb, "url": full_url, "provider": "xvideos"})
                        if len(videos) >= 48: break

                if len(videos) > 0:
                    search_cache[cache_key] = videos
                    return videos
        except Exception as e:
            logger.error(f"Search provider {provider} attempt {attempt+1} error: {e}")
            time.sleep(1.0)

    # Fallback to yt-dlp search if BeautifulSoup scraping fails or returns empty
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

def parse_metadata_fallback(url: str, provider: str) -> dict:
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
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept-Language': 'en-US,en;q=0.9',
        'Cookie': 'has_accepted_cookie=1; age_verified=1;'
    }
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=headers, timeout=8)
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
                    poster_url = og_img.get('content').replace('&amp;', '&')

                if not poster_url:
                    img_json = re.search(r'"image_url"\s*:\s*"([^"]+)"', resp.text)
                    if img_json:
                        poster_url = img_json.group(1).replace('\\/', '/')

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
            time.sleep(1.0)
    return {"view_count": 0, "upload_date": "", "thumbnail": "", "title": ""}

def extract_with_ytdlp(url: str) -> dict:
    is_pornhub = "pornhub.com" in url
    if not is_pornhub and url in extraction_cache:
        return extraction_cache[url]

    provider = "pornhub"
    if "xhamster.com" in url:
        provider = "xhamster"
    elif "xnxx.com" in url:
        provider = "xnxx"
    elif "xvideos.com" in url:
        provider = "xvideos"
    elif "redtube.com" in url:
        provider = "redtube"
    elif "youporn.com" in url:
        provider = "youporn"

    referer_url = f"https://www.{provider}.com/" if provider != "xhamster" else "https://xhamster.com/"

    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'format': 'bestvideo+bestaudio/best',
        'nocheckcertificate': True,
        'age_limit': 21,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Sec-Ch-Ua': '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            'Sec-Ch-Ua-Mobile': '?0',
            'Sec-Ch-Ua-Platform': '"Windows"',
            'Referer': referer_url,
            'Origin': referer_url.rstrip('/'),
            'Cookie': 'has_accepted_cookie=1; age_verified=1; platform=pc;'
        }
    }

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

            extra_meta = parse_metadata_fallback(url, provider)
            
            if not is_pornhub and (not title or title.isdigit() or re.match(r'^(?:ES|PT)?\d{1,2}:\d{2}', title) or (len(title) <= 8 and title.isalnum())):
                if extra_meta.get("title"):
                    title = extra_meta.get("title")
            if not title or title.isdigit() or re.match(r'^(?:ES|PT)?\d{1,2}:\d{2}', title):
                title = "Unknown Video"

            view_count = view_count or extra_meta.get("view_count", 0)
            upload_date = upload_date or extra_meta.get("upload_date", "")
            
            all_thumbs = []
            safe_thumb = extra_meta.get("thumbnail", "")
            if safe_thumb and not safe_thumb.startswith("data:image"):
                all_thumbs.append(safe_thumb)

            if info.get('thumbnail') and info.get('thumbnail') not in all_thumbs:
                all_thumbs.append(info.get('thumbnail'))

            for t in info.get('thumbnails', []):
                if t.get('url') and t.get('url') not in all_thumbs:
                    all_thumbs.append(t.get('url'))

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
            if has_hls:
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
            if attempt < max_retries - 1:
                time.sleep(1.0)
                continue

    return {"status": "error", "error": f"Failed after {max_retries} retries: {last_error}", "url": url}

@app.get("/api/explore")
async def explore(q: str = "brazzers", page: int = 1, provider: str = "pornhub"):
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(thread_pool, search_provider_robust, provider, q, page)
    return JSONResponse(res)

@app.get("/api/extract")
async def extract_endpoint(url: str):
    if not url: return JSONResponse({"status": "error", "error": "Missing URL"})
    target_url = url.strip()
    if "viewkey=" not in target_url and not any(d in target_url for d in ["xhamster.com", "xnxx.com", "xvideos.com", "redtube.com", "youporn.com"]):
        if len(target_url) in [13, 15, 16] and "." not in target_url:
             target_url = f"https://www.pornhub.com/view_video.php?viewkey={target_url}"
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(thread_pool, extract_with_ytdlp, target_url)
    return JSONResponse(res)

@app.get("/proxy-image")
async def fallback_proxy_image(url: str):
    target = url.strip()
    if target.startswith('//'): target = "https:" + target
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': 'https://www.youporn.com/',
        'Cookie': 'has_accepted_cookie=1; age_verified=1; platform=pc;'
    }
    
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
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", 
        "Referer": "https://www.youporn.com/",
        "Cookie": "has_accepted_cookie=1; age_verified=1; platform=pc;"
    }
    
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                resp = await client.get(target, headers=headers)
                if resp.status_code == 200:
                    base_url = target.rsplit('/', 1)[0] + '/'
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
                                    abs_uri = uri if uri.startswith('http') else base_url + uri
                                    next_endpoint = "/proxy-m3u8" if ".m3u8" in abs_uri else "/proxy-video"
                                    new_uri = f"{proto}://{request_host}{next_endpoint}?url={quote(abs_uri)}&sig={sig}&exp={exp}&request_host={request_host}"
                                    line = line.replace(f'URI="{uri}"', f'URI="{new_uri}"')
                            rewritten.append(line)
                        else:
                            abs_uri = line if line.startswith('http') else base_url + line
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
async def proxy_video(request: Request, url: str):
    target = url.strip()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", 
        "Referer": "https://www.youporn.com/",
        "Cookie": "has_accepted_cookie=1; age_verified=1; platform=pc;"
    }
    if "range" in request.headers:
        headers["Range"] = request.headers["range"]
        
    for attempt in range(3):
        try:
            client = httpx.AsyncClient(timeout=60.0, follow_redirects=True)
            req = client.build_request("GET", target, headers=headers)
            resp = await client.send(req, stream=True)
            if resp.status_code in [200, 206]:
                resp_headers = {
                    "Access-Control-Allow-Origin": "*",
                    "Accept-Ranges": "bytes"
                }
                for k in ["Content-Type", "Content-Length", "Content-Range"]:
                    if k in resp.headers:
                        resp_headers[k] = resp.headers[k]
                        
                async def stream_generator():
                    try:
                        async for chunk in resp.aiter_bytes(chunk_size=65536):
                            yield chunk
                    finally:
                        await client.aclose()

                return StreamingResponse(stream_generator(), status_code=resp.status_code, headers=resp_headers)
        except Exception:
            await asyncio.sleep(0.5)
    return Response(status_code=502)

@app.get("/")
def health():
    return {"status": "Online", "engine": "Fast Edge Extraction Engine"}
