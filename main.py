import os
import time
import asyncio
import logging
import re
from urllib.parse import quote, unquote
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
import httpx
from lxml import html
import yt_dlp
from cachetools import TTLCache

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

extraction_cache = TTLCache(maxsize=2000, ttl=7200)
thread_pool = ThreadPoolExecutor(max_workers=50)

app = FastAPI(title="Media Extraction Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

def search_pornhub_with_ytdlp(q: str, page: int):
    ydl_opts = {
        'quiet': True,
        'extract_flat': True,
        'nocheckcertificate': True,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://www.pornhub.com/',
            'Cookie': 'has_accepted_cookie=1; age_verified=1; platform=pc;'
        }
    }
    videos = []
    try:
        search_url = f"https://www.pornhub.com/video/search?search={quote(q)}&page={page}"
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_url, download=False)
            if not info: return videos
            
            entries = info.get('entries', [])
            for entry in entries:
                if not entry: continue
                
                vkey = entry.get('id')
                if not vkey and entry.get('url') and 'viewkey=' in entry.get('url'):
                    vkey = entry.get('url').split('viewkey=')[1].split('&')[0]
                if not vkey: continue
                
                title = entry.get('title', 'Unknown Video')
                thumb = entry.get('thumbnail', '')
                if not thumb and entry.get('thumbnails'):
                    thumb = entry.get('thumbnails')[0].get('url', '')
                    
                videos.append({
                    "vkey": vkey,
                    "title": title,
                    "thumbnail": thumb,
                    "url": f"https://www.pornhub.com/view_video.php?viewkey={vkey}",
                    "provider": "pornhub"
                })
                
                if len(videos) >= 24:
                    break
    except Exception as e:
        logger.error(f"yt-dlp phsearch error: {e}")
    return videos

def parse_metadata_fallback(url: str, provider: str) -> dict:
    base_domain = "https://www.pornhub.com"
    if "xhamster.com" in url:
        base_domain = "https://xhamster.com"
    elif "xnxx.com" in url:
        base_domain = "https://www.xnxx.com"
    elif "xvideos.com" in url:
        base_domain = "https://www.xvideos.com"

    url = re.sub(r'https?://[a-zA-Z0-9-]+\.' + provider + r'\.com', base_domain, url)
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept-Language': 'en-US,en;q=0.9',
        'Cookie': 'has_accepted_cookie=1; age_verified=1;'
    }
    try:
        import requests
        resp = requests.get(url, headers=headers, timeout=6)
        if resp.status_code == 200:
            html_text = resp.text
            view_count = 0
            upload_date = ""
            poster_url = ""

            view_match = re.search(r'([\d,\.]+)\s*(?:Views|views|Vistas|M views|k views)', html_text)
            if view_match:
                raw_views = view_match.group(1).replace(',', '').replace('.', '')
                if 'k' in view_match.group(0).lower():
                    view_count = int(float(raw_views.replace('k', '')) * 1000)
                elif 'm' in view_match.group(0).lower():
                    view_count = int(float(raw_views.replace('m', '')) * 1000000)
                else:
                    view_count = int(raw_views) if raw_views.isdigit() else 0

            date_match = re.search(r'(\d{4}-\d{2}-\d{2})|(\d{1,2}\s+[a-zA-Z]+\s+\d{4})', html_text)
            if date_match:
                upload_date = date_match.group(0)

            json_thumb = re.search(r'"image_url"\s*:\s*"([^"]+)"', html_text)
            if json_thumb:
                poster_url = json_thumb.group(1).replace('\\/', '/')
            
            if not poster_url:
                og_match = re.search(r'<meta property="og:image" content="([^"]+)"', html_text)
                if og_match:
                    poster_url = og_match.group(1).replace("&amp;", "&")

            return {"view_count": view_count, "upload_date": upload_date, "thumbnail": poster_url}
    except Exception as e:
        logger.error(f"Metadata fallback scrape error: {e}")
    return {"view_count": 0, "upload_date": "", "thumbnail": ""}

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

    referer_url = f"https://www.{provider}.com/" if provider != "xhamster" else "https://xhamster.com/"

    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'format': 'bestvideo+bestaudio/best',
        'nocheckcertificate': True,
        'age_limit': 21,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
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

            title = info.get('title', 'Unknown Video')
            duration = info.get('duration', 0)
            upload_date = info.get('upload_date', '') 
            view_count = info.get('view_count', 0)

            extra_meta = parse_metadata_fallback(url, provider)
            
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

            clean_thumbs = [t for t in all_thumbs if 'hash=' not in t and 'validto=' not in t and 'hdnea=' not in t]
            
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

    return {"status": "error", "error": f"Failed after retries: {last_error}", "url": url}

@app.get("/api/explore")
async def explore(q: str = "brazzers", page: int = 1, provider: str = "pornhub"):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept-Language': 'en-US,en;q=0.9',
        'Cookie': 'has_accepted_cookie=1; age_verified=1; platform=pc;',
        'Referer': 'https://xhamster.com/' if provider == "xhamster" else 'https://www.pornhub.com/'
    }

    if provider == "xhamster":
        search_url = f"https://xhamster.com/search/{quote(q)}" if page <= 1 else f"https://xhamster.com/search/{quote(q)}/{page}"
    elif provider == "xnxx":
        search_url = f"https://www.xnxx.com/search/{quote(q)}" if page <= 1 else f"https://www.xnxx.com/search/{quote(q)}/{page}"
    elif provider == "xvideos":
        p_val = page - 1 if page > 1 else 0
        search_url = f"https://www.xvideos.com/?k={quote(q)}" if p_val == 0 else f"https://www.xvideos.com/?k={quote(q)}&p={p_val}"
    else:
        search_url = f"https://www.pornhub.com/video/search?search={quote(q)}&page={page}"
    
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(search_url, headers=headers)

            if resp.status_code != 200:
                if provider == "pornhub":
                    loop = asyncio.get_running_loop()
                    return JSONResponse(await loop.run_in_executor(thread_pool, search_pornhub_with_ytdlp, q, page))
                return JSONResponse([])

            try: html_content = resp.content.decode('utf-8', errors='replace')
            except Exception: html_content = resp.text

            tree = html.fromstring(html_content)
            videos = []
            seen_urls = set()

            if provider == "xhamster":
                items = tree.xpath('//div[contains(@class, "video-thumb-info")] | //div[contains(@class, "thumb-list__item")] | //div[contains(@class, "video-container")] | //div[contains(@class, "video-thumb")]')
                if not items:
                    items = tree.xpath('//div[contains(@class, "masonry-item")]')
                
                for item in items:
                    link_elems = item.xpath('.//a[contains(@class, "video-thumb__image-container")]/@href | .//a[contains(@class, "thumb-image-container")]/@href | .//a/@href')
                    href = next((l for l in link_elems if l and ('/videos/' in l or '/movie/' in l)), None)
                    if not href:
                        href = next((l for l in link_elems if l and ('http' in l or '/' in l)), None)
                    if not href: continue

                    full_url = href if href.startswith('http') else f"https://xhamster.com{href}"
                    
                    if not re.search(r'/videos?/', full_url) or full_url in seen_urls:
                        continue
                    seen_urls.add(full_url)
                    
                    vid_parts = [p for p in full_url.split('/') if p]
                    vid_id = vid_parts[-1] if vid_parts else "unknown"

                    title_elems = item.xpath('.//a[contains(@class, "video-thumb__title")]/text() | .//a/@title | .//img/@alt')
                    title = next((t.strip() for t in title_elems if t and len(t.strip()) > 2), "")
                    if not title or title.upper() == "UNKNOWN VIDEO":
                        all_texts = item.xpath('.//a//text() | .//span//text()')
                        title = next((t.strip() for t in all_texts if t and len(t.strip()) > 3 and "unknown" not in t.lower()), "")
                    
                    if not title or title.upper() == "UNKNOWN VIDEO":
                        continue

                    raw_thumbs = item.xpath('.//img/@data-src | .//img/@src | .//img/@data-lazy-src')
                    thumb = next((t for t in raw_thumbs if t and "data:image" not in t and "blank" not in t), "")
                    if not thumb and raw_thumbs: thumb = raw_thumbs[0]
                    
                    if not thumb:
                        continue

                    videos.append({
                        "vkey": vid_id,
                        "title": title,
                        "thumbnail": thumb,
                        "url": full_url,
                        "provider": "xhamster"
                    })
                    if len(videos) >= 24: break

            elif provider == "xnxx":
                items = tree.xpath('//div[contains(@class, "mozaique")]//div[contains(@class, "thumb-block")]')
                for item in items:
                    link_elems = item.xpath('.//div[@class="thumb-under"]//a/@href | .//a[contains(@class, "pure-u")]/@href | .//a/@href')
                    href = next((l for l in link_elems if l and ('/video-' in l or '/video.' in l)), None)
                    if not href: continue
                    full_url = f"https://www.xnxx.com{href}" if href.startswith('/') else href
                    if full_url in seen_urls: continue
                    seen_urls.add(full_url)

                    vid_id = href.split('/')[1] if len(href.split('/')) > 1 else href
                    title_elems = item.xpath('.//div[@class="thumb-under"]//a/@title | .//div[@class="thumb-under"]//a/text() | .//a/@title')
                    title = next((t.strip() for t in title_elems if t and t.strip()), "Unknown Video")
                    if not title or title.upper() == "UNKNOWN VIDEO": continue

                    raw_thumbs = item.xpath('.//img/@data-src | .//img/@src | .//div[@data-videothumb]/@data-videothumb')
                    thumb = next((t for t in raw_thumbs if t and "data:image" not in t and "blank" not in t and "lightbox" not in t), "")
                    if not thumb and raw_thumbs: thumb = raw_thumbs[0]
                    if not thumb: continue
                    
                    videos.append({"vkey": vid_id, "title": title, "thumbnail": thumb, "url": full_url, "provider": "xnxx"})
                    if len(videos) >= 24: break

            elif provider == "xvideos":
                items = tree.xpath('//div[contains(@class, "mozaique")]//div[contains(@class, "thumb-block")]')
                for item in items:
                    link_elems = item.xpath('.//p[@class="title"]//a/@href | .//a/@href')
                    href = next((l for l in link_elems if l and ('/video.' in l or '/video-' in l)), None)
                    if not href: continue
                    vid_id = href.split('/')[1] if len(href.split('/')) > 1 else href
                    clean_href = href.rstrip('/')
                    if clean_href.endswith('_') or clean_href.endswith('/_') or len(clean_href.split('/')) < 3:
                        clean_href = f"/{vid_id}/video_stream"
                    full_url = f"https://www.xvideos.com{clean_href}" if clean_href.startswith('/') else clean_href
                    if full_url in seen_urls: continue
                    seen_urls.add(full_url)

                    title_elems = item.xpath('.//p[@class="title"]//a/@title | .//p[@class="title"]//a/text() | .//a/@title')
                    title = next((t.strip() for t in title_elems if t and t.strip()), "Unknown Video")
                    if not title or title.upper() == "UNKNOWN VIDEO": continue

                    raw_thumbs = item.xpath('.//img/@data-src | .//img/@src | .//div[@data-videothumb]/@data-videothumb')
                    thumb = next((t for t in raw_thumbs if t and "data:image" not in t and "blank" not in t and "lightbox" not in t), "")
                    if not thumb and raw_thumbs: thumb = raw_thumbs[0]
                    if not thumb: continue

                    videos.append({"vkey": vid_id, "title": title, "thumbnail": thumb, "url": full_url, "provider": "xvideos"})
                    if len(videos) >= 24: break
            else:
                items = tree.xpath('//li[contains(@class, "videoblock") or contains(@class, "pcVideoListItem") or contains(@class, "js-pop") or contains(@class, "videoBox")]')
                if not items:
                    items = tree.xpath('//ul[@id="videoSearchResult"]//li | //div[contains(@class, "search-video-list")]//li')

                for item in items:
                    vkey = item.get("data-video-vkey") or next(iter(item.xpath('.//@data-video-vkey')), None)
                    if not vkey:
                        hrefs = item.xpath('.//a[contains(@href, "viewkey=")]/@href | .//a[contains(@href, "/view_video.php")]/@href | .//a[contains(@href, "/video/")]/@href')
                        for h in hrefs:
                            if "viewkey=" in h or "/view_video.php?" in h:
                                try:
                                    vkey = h.split("viewkey=")[1].split("&")[0]
                                    break
                                except Exception: pass
                            elif "/video/" in h:
                                try:
                                    parts = [p for p in h.split('/') if p]
                                    if parts:
                                        vkey = parts[-1]
                                        break
                                except Exception: pass
                    if not vkey: continue
                    full_url = f"https://www.pornhub.com/view_video.php?viewkey={vkey}"
                    if full_url in seen_urls: continue
                    seen_urls.add(full_url)

                    title_elem = item.xpath('.//span[@class="title"]//a/text() | .//a[contains(@class, "title")]/text() | .//img/@alt | .//a/@title')
                    title = next((t.strip() for t in title_elem if t and len(t.strip()) > 3), "Unknown Video")
                    if not title or title.upper() == "UNKNOWN VIDEO": continue

                    raw_thumbs = item.xpath('.//img/@data-thumb_url | .//img/@data-mediumthumb | .//img/@data-image | .//img/@data-src | .//img/@src')
                    thumb = ""
                    for t in raw_thumbs:
                        if t and "data:image" not in t and "blank" not in t and "transparent" not in t and "data:auto" not in t:
                            thumb = t
                            break
                    if not thumb: continue

                    videos.append({
                        "vkey": vkey,
                        "title": title,
                        "thumbnail": thumb,
                        "url": full_url,
                        "provider": "pornhub"
                    })

                    if len(videos) >= 24: break
                
                if len(videos) == 0:
                    loop = asyncio.get_running_loop()
                    return JSONResponse(await loop.run_in_executor(thread_pool, search_pornhub_with_ytdlp, q, page))

            return JSONResponse(videos)

    except Exception as e:
        logger.error(f"Explore error: {e}")
        return JSONResponse([])


@app.get("/api/extract")
async def extract_endpoint(url: str):
    if not url: return JSONResponse({"status": "error", "error": "Missing URL"})
    target_url = url.strip()
    if "viewkey=" not in target_url and "xhamster.com" not in target_url and "xnxx.com" not in target_url and "xvideos.com" not in target_url:
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
        'Referer': 'https://xhamster.com/',
        'Cookie': 'has_accepted_cookie=1; age_verified=1; platform=pc;'
    }
    
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            req = await client.get(target, headers=headers)
            if req.status_code != 200:
                return Response(status_code=req.status_code, content=f"Image CDN Error: {req.status_code}")
                
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
        return Response(status_code=404)

@app.get("/proxy-m3u8")
async def proxy_m3u8(request: Request, url: str, sig: str = "", exp: str = "", request_host: str = ""):
    target = url.strip()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", 
        "Referer": "https://xhamster.com/",
        "Cookie": "has_accepted_cookie=1; age_verified=1; platform=pc;"
    }
    
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(target, headers=headers)
            if resp.status_code != 200:
                return Response(status_code=resp.status_code, content=f"CDN Error: {resp.status_code}")
                
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
    except Exception as e:
        return Response(status_code=502, content="Backend Proxy Error")

@app.get("/proxy-video")
async def proxy_video(request: Request, url: str):
    target = url.strip()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", 
        "Referer": "https://xhamster.com/",
        "Cookie": "has_accepted_cookie=1; age_verified=1; platform=pc;"
    }
    if "range" in request.headers:
        headers["Range"] = request.headers["range"]
        
    try:
        client = httpx.AsyncClient(timeout=60.0, follow_redirects=True)
        req = client.build_request("GET", target, headers=headers)
        resp = await client.send(req, stream=True)
        
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
    except Exception as e:
        return Response(status_code=502)

@app.get("/")
def health():
    return {"status": "Online", "engine": "Fast Edge Extraction Engine"}
