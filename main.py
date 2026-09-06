import os
import time
import asyncio
import logging
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

def extract_with_ytdlp(url: str) -> dict:
    if url in extraction_cache:
        return extraction_cache[url]

    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'format': 'all',
        'nocheckcertificate': True,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Referer': 'https://www.pornhub.com/'
        }
    }

    max_retries = 5
    last_error = "Unknown Error"

    provider = "pornhub"
    if "xnxx.com" in url:
        provider = "xnxx"
    elif "xvideos.com" in url:
        provider = "xvideos"

    for attempt in range(max_retries):
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)

            title = info.get('title', 'Unknown Video')
            thumbnail = info.get('thumbnail', '')
            duration = info.get('duration', 0)
            
            upload_date = info.get('upload_date', '') 
            view_count = info.get('view_count', 0)

            thumbnails = [t['url'] for t in info.get('thumbnails', []) if 'url' in t]
            if thumbnail and thumbnail not in thumbnails:
                thumbnails.insert(0, thumbnail)

            qualities = []
            seen_qualities = set()

            for f in info.get('formats', []):
                height = f.get('height')
                if not height:
                    continue
                
                q_label = f"{height}p"
                f_url = f.get('url', '')
                protocol = f.get('protocol', '')
                ext = f.get('ext', '')

                if 'm3u8' not in protocol and ext != 'm3u8' and 'http' not in protocol:
                    continue

                if q_label not in seen_qualities:
                    seen_qualities.add(q_label)
                    qualities.append({
                        "quality": q_label,
                        "url": f_url,
                        "type": "hls" if ('m3u8' in protocol or ext == 'm3u8') else "mp4"
                    })

            qualities.sort(key=lambda x: int(x['quality'].replace('p', '')), reverse=True)

            if not qualities:
                last_error = "No valid streams found"
                if attempt < max_retries - 1:
                    time.sleep(1.5)
                    continue
                return {"status": "error", "error": last_error, "url": url}

            result = {
                "status": "success",
                "title": title,
                "thumbnail": thumbnail or (thumbnails[0] if thumbnails else ""),
                "thumbnails": thumbnails,
                "duration": duration,
                "upload_date": upload_date,
                "view_count": view_count,
                "streams": {"qualities": qualities},
                "url": url,
                "provider": provider
            }
            
            extraction_cache[url] = result
            return result

        except yt_dlp.utils.DownloadError as e:
            last_error = f"Download error: {str(e)}"
            if attempt < max_retries - 1:
                time.sleep(1.5)
                continue
        except Exception as e:
            last_error = str(e)
            logger.error(f"yt-dlp extraction failed: {e}")
            if attempt < max_retries - 1:
                time.sleep(1.5)
                continue

    return {"status": "error", "error": f"Failed after 5 attempts. Last error: {last_error}", "url": url}


@app.get("/api/explore")
async def explore(q: str = "brazzers", page: int = 1, provider: str = "pornhub"):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Accept-Language': 'en-US,en;q=0.9',
        'Cookie': 'has_accepted_cookie=1; age_verified=1;',
        'Referer': 'https://www.xvideos.com/' if provider == "xvideos" else 'https://www.pornhub.com/'
    }

    if provider == "xnxx":
        search_url = f"https://www.xnxx.com/search/{quote(q)}" if page <= 1 else f"https://www.xnxx.com/search/{quote(q)}/{page}"
    elif provider == "xvideos":
        p_val = page - 1 if page > 1 else 0
        search_url = f"https://www.xvideos.com/?k={quote(q)}" if p_val == 0 else f"https://www.xvideos.com/?k={quote(q)}&p={p_val}"
    else:
        search_url = f"https://www.pornhub.com/video/search?search={quote(q)}&page={page}"
    
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(search_url, headers=headers)
            
        if resp.status_code != 200:
            return JSONResponse([])

        # Ensure explicit utf-8 decoding to properly handle Persian, Arabic, and Hindi titles withoutMojibake
        try:
            html_content = resp.content.decode('utf-8', errors='replace')
        except Exception:
            html_content = resp.text

        tree = html.fromstring(html_content)
        videos = []

        if provider == "xnxx":
            items = tree.xpath('//div[contains(@class, "mozaique")]//div[contains(@class, "thumb-block")]')
            for item in items:
                link_elems = item.xpath('.//div[@class="thumb-under"]//a/@href | .//a[contains(@class, "pure-u")]/@href | .//a/@href')
                href = next((l for l in link_elems if l and ('/video-' in l or '/video.' in l)), None)
                if not href:
                    continue
                
                vid_id = href.split('/')[1] if len(href.split('/')) > 1 else href
                full_url = f"https://www.xnxx.com{href}" if href.startswith('/') else href

                title_elems = item.xpath('.//div[@class="thumb-under"]//a/@title | .//div[@class="thumb-under"]//a/text() | .//a/@title')
                title = next((t.strip() for t in title_elems if t and t.strip()), "Unknown Video")

                raw_thumbs = item.xpath('.//img/@data-src | .//img/@src | .//div[@data-videothumb]/@data-videothumb')
                thumb = next((t for t in raw_thumbs if t and "data:image" not in t and "blank" not in t and "lightbox" not in t), "")
                if not thumb and raw_thumbs:
                    thumb = raw_thumbs[0]
                if not thumb:
                    continue

                videos.append({"vkey": vid_id, "title": title, "thumbnail": thumb, "url": full_url, "provider": "xnxx"})
                if len(videos) >= 24:
                    break

        elif provider == "xvideos":
            items = tree.xpath('//div[contains(@class, "mozaique")]//div[contains(@class, "thumb-block")]')
            for item in items:
                link_elems = item.xpath('.//p[@class="title"]//a/@href | .//a/@href')
                href = next((l for l in link_elems if l and ('/video.' in l or '/video-' in l)), None)
                if not href:
                    continue

                vid_id = href.split('/')[1] if len(href.split('/')) > 1 else href
                
                title_elems = item.xpath('.//p[@class="title"]//a/@title | .//p[@class="title"]//a/text() | .//a/@title')
                title = next((t.strip() for t in title_elems if t and t.strip()), "Unknown Video")

                # Sanitize slug creation safely using a fallback identifier if non-ASCII url path characters break
                clean_href = href.rstrip('/')
                if clean_href.endswith('_') or clean_href.endswith('/_') or len(clean_href.split('/')) < 3:
                    clean_href = f"/{vid_id}/video_stream"

                full_url = f"https://www.xvideos.com{clean_href}" if clean_href.startswith('/') else clean_href

                raw_thumbs = item.xpath('.//img/@data-src | .//img/@src | .//div[@data-videothumb]/@data-videothumb')
                thumb = next((t for t in raw_thumbs if t and "data:image" not in t and "blank" not in t and "lightbox" not in t), "")
                if not thumb and raw_thumbs:
                    thumb = raw_thumbs[0]
                if not thumb:
                    continue

                videos.append({"vkey": vid_id, "title": title, "thumbnail": thumb, "url": full_url, "provider": "xvideos"})
                if len(videos) >= 24:
                    break
        else:
            items = tree.xpath('//li[contains(@class, "js-pop videoblock") or contains(@class, "pcVideoListItem")]')
            if not items:
                items = tree.xpath('//ul[@id="videoSearchResult"]//li')

            for item in items:
                vkey = item.get("data-video-vkey") or next(iter(item.xpath('.//@data-video-vkey')), None)
                if not vkey:
                    hrefs = item.xpath('.//a[contains(@href, "viewkey=")]/@href')
                    for h in hrefs:
                        if "viewkey=" in h:
                            vkey = h.split("viewkey=")[1].split("&")[0]
                            break
                if not vkey:
                    continue

                title_elem = item.xpath('.//span[@class="title"]//a/text() | .//a[contains(@class, "title")]/text() | .//img/@alt | .//a/@title')
                title = title_elem[0].strip() if title_elem else "Unknown Video"

                raw_thumbs = item.xpath('.//img/@data-thumb_url | .//img/@data-mediumthumb | .//img/@data-image | .//img/@src')
                thumb = next((t for t in raw_thumbs if t and "data:image" not in t and "blank" not in t), "")
                if not thumb and raw_thumbs:
                    thumb = raw_thumbs[0]

                if not thumb:
                    continue

                videos.append({
                    "vkey": vkey,
                    "title": title,
                    "thumbnail": thumb,
                    "url": f"https://www.pornhub.com/view_video.php?viewkey={vkey}",
                    "provider": "pornhub"
                })

                if len(videos) >= 24:
                    break

        return JSONResponse(videos)

    except Exception as e:
        logger.error(f"Explore error: {e}")
        return JSONResponse([])


@app.get("/api/extract")
async def extract_endpoint(url: str):
    if not url:
        return JSONResponse({"status": "error", "error": "Missing URL"})
    
    target_url = unquote(url)
    if "viewkey=" not in target_url and "xnxx.com" not in target_url and "xvideos.com" not in target_url:
        if len(target_url) in [13, 15, 16] and "." not in target_url:
             target_url = f"https://www.pornhub.com/view_video.php?viewkey={target_url}"
         
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(thread_pool, extract_with_ytdlp, target_url)
    return JSONResponse(res)


@app.get("/proxy-image")
async def fallback_proxy_image(url: str):
    target = unquote(url).strip()
    if target.startswith('//'):
        target = "https:" + target
        
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            req = await client.get(target, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.xvideos.com/'})
            return StreamingResponse(
                (chunk async for chunk in req.aiter_bytes()),
                status_code=req.status_code,
                headers={
                    "Content-Type": req.headers.get("Content-Type", "image/jpeg"),
                    "Access-Control-Allow-Origin": "*"
                }
            )
    except Exception:
        return Response(status_code=404)

@app.get("/")
def health():
    return {"status": "Online", "engine": "yt-dlp Multi-Hub Core (Pornhub, XNXX, XVideos) + Metadata Engine"}
