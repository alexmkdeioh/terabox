import os
import re
import csv
import io
import json
import time
import base64
import sqlite3
import requests
import threading
from urllib.parse import urlparse, parse_qs, quote
from flask import Flask, request, jsonify, render_template, redirect, url_for, session, Response

try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False


_YTDLP_MODULE = None

def get_ytdlp():
    """Lazy-loads yt_dlp on-demand to save ~60MB RAM on server startup."""
    global _YTDLP_MODULE
    if _YTDLP_MODULE is not None:
        return _YTDLP_MODULE
    try:
        import yt_dlp
        _YTDLP_MODULE = yt_dlp
        return _YTDLP_MODULE
    except ImportError:
        return None

app = Flask(__name__, template_folder="templates")
app.secret_key = os.environ.get("SECRET_KEY", "terastream_secure_session_key_2026")
PORT = int(os.environ.get("PORT", 8080))

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "mahabir")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "mk@123")
DB_FILE = os.path.join(os.path.dirname(__file__), "terastream.db")
DISKWALA_API_KEY = os.environ.get("DISKWALA_API_KEY", "6a9c2fc6ae1992e641218130")

# Lifetime Cloud Database (PostgreSQL) configuration
DEFAULT_DATABASE_URL = "postgresql://neondb_owner:npg_0JyzHhFLNGs1@ep-gentle-bonus-ae1k78jw-pooler.c-2.us-east-2.aws.neon.tech/neondb?sslmode=require"
DATABASE_URL = os.environ.get("DATABASE_URL") or DEFAULT_DATABASE_URL
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    HAS_PSYCOPG2 = True
except ImportError:
    HAS_PSYCOPG2 = False


# ----------------- LIFETIME PERSISTENT DATABASE ENGINE -----------------

def get_db_connection():
    """Returns a database connection (PostgreSQL if DATABASE_URL is set, otherwise local SQLite)."""
    if DATABASE_URL and HAS_PSYCOPG2:
        try:
            conn = psycopg2.connect(DATABASE_URL, connect_timeout=4)
            return "postgres", conn
        except Exception as e:
            print("PostgreSQL connection error, falling back to SQLite:", e)

    conn = sqlite3.connect(DB_FILE, timeout=15)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
    except Exception:
        pass
    return "sqlite", conn


def init_db():
    """Initializes tables for search_logs, feed_sources, and feed_videos across PostgreSQL and SQLite."""
    try:
        db_type, conn = get_db_connection()
        with conn:
            cursor = conn.cursor()
            if db_type == "postgres":
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS search_logs (
                        id SERIAL PRIMARY KEY,
                        searched_url TEXT,
                        surl TEXT,
                        video_title TEXT,
                        video_size TEXT,
                        stream_url TEXT,
                        download_url TEXT,
                        user_ip TEXT,
                        user_agent TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                ''')
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS feed_sources (
                        id SERIAL PRIMARY KEY,
                        name TEXT NOT NULL,
                        url TEXT UNIQUE NOT NULL,
                        is_active BOOLEAN DEFAULT TRUE,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        last_synced_at TIMESTAMP
                    );
                ''')
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS feed_videos (
                        id SERIAL PRIMARY KEY,
                        source_id INTEGER,
                        source_name TEXT,
                        title TEXT NOT NULL,
                        video_url TEXT UNIQUE NOT NULL,
                        thumbnail_url TEXT,
                        surl TEXT,
                        discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                ''')
                cursor.execute("SELECT COUNT(*) FROM feed_sources")
                if cursor.fetchone()[0] == 0:
                    cursor.execute('''
                        INSERT INTO feed_sources (name, url, is_active)
                        VALUES (%s, %s, %s)
                    ''', ('BuriburiReviews', 'https://bio.site/BuriburiReviews', True))
            else:
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS search_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        searched_url TEXT,
                        surl TEXT,
                        video_title TEXT,
                        video_size TEXT,
                        stream_url TEXT,
                        download_url TEXT,
                        user_ip TEXT,
                        user_agent TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS feed_sources (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        url TEXT UNIQUE NOT NULL,
                        is_active INTEGER DEFAULT 1,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        last_synced_at TIMESTAMP
                    )
                ''')
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS feed_videos (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        source_id INTEGER,
                        source_name TEXT,
                        title TEXT NOT NULL,
                        video_url TEXT UNIQUE NOT NULL,
                        thumbnail_url TEXT,
                        surl TEXT,
                        discovered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                cursor.execute("SELECT COUNT(*) FROM feed_sources")
                if cursor.fetchone()[0] == 0:
                    cursor.execute('''
                        INSERT INTO feed_sources (name, url, is_active)
                        VALUES (?, ?, ?)
                    ''', ('BuriburiReviews', 'https://bio.site/BuriburiReviews', 1))
            conn.commit()
        conn.close()
    except Exception as e:
        print("DB Init Error:", e)

init_db()


def _async_log_worker(searched_url, surl, video_title, video_size, stream_url, download_url, user_ip, user_agent):
    """Worker function executed in background thread to write database records without blocking API."""
    conn = None
    try:
        db_type, conn = get_db_connection()
        with conn:
            cursor = conn.cursor()
            if db_type == "postgres":
                cursor.execute('''
                    INSERT INTO search_logs (searched_url, surl, video_title, video_size, stream_url, download_url, user_ip, user_agent)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ''', (
                    str(searched_url or ''),
                    str(surl or ''),
                    str(video_title or 'TeraBox Video'),
                    str(video_size or 'HD Video'),
                    str(stream_url or ''),
                    str(download_url or ''),
                    str(user_ip or '127.0.0.1'),
                    str(user_agent or 'Unknown')
                ))
            else:
                cursor.execute('''
                    INSERT INTO search_logs (searched_url, surl, video_title, video_size, stream_url, download_url, user_ip, user_agent)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    str(searched_url or ''),
                    str(surl or ''),
                    str(video_title or 'TeraBox Video'),
                    str(video_size or 'HD Video'),
                    str(stream_url or ''),
                    str(download_url or ''),
                    str(user_ip or '127.0.0.1'),
                    str(user_agent or 'Unknown')
                ))
            conn.commit()
    except Exception as e:
        print("Async log search error:", e)
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def log_search(searched_url, surl, video_title, video_size, stream_url, download_url, user_ip, user_agent):
    """Guarantees every user search query is saved asynchronously in background thread for 0ms API response!"""
    threading.Thread(
        target=_async_log_worker,
        args=(searched_url, surl, video_title, video_size, stream_url, download_url, user_ip, user_agent),
        daemon=True
    ).start()


# ----------------- IN-MEMORY RESOLUTION CACHE (BOUNDED) -----------------
RESOLVE_CACHE = {}
MAX_CACHE_ENTRIES = 120
CACHE_TTL = 3600  # 1 hour max age


def cache_set(key, val):
    """Sets cache entry with LRU eviction to strictly limit RAM usage under 100MB."""
    global RESOLVE_CACHE
    now = time.time()
    if len(RESOLVE_CACHE) >= MAX_CACHE_ENTRIES:
        # Purge items older than TTL
        expired = [k for k, v in RESOLVE_CACHE.items() if isinstance(v, tuple) and (now - v[0] > CACHE_TTL)]
        for k in expired:
            RESOLVE_CACHE.pop(k, None)
        # If still over limit, discard oldest 30 items
        if len(RESOLVE_CACHE) >= MAX_CACHE_ENTRIES:
            sorted_keys = sorted(
                RESOLVE_CACHE.keys(),
                key=lambda k: RESOLVE_CACHE[k][0] if isinstance(RESOLVE_CACHE[k], tuple) else 0
            )
            for k in sorted_keys[:30]:
                RESOLVE_CACHE.pop(k, None)
    RESOLVE_CACHE[key] = (now, val)


# ----------------- SESSION CACHE FOR HLS STREAMING -----------------

FLOW_SESSION = {
    "session": None,
    "csrf_token": None,
    "last_init": 0
}


def get_flow_session():
    """Maintains an active, initialized device session for direct HLS stream extraction."""
    now = time.time()
    if FLOW_SESSION["session"] and FLOW_SESSION["csrf_token"] and (now - FLOW_SESSION["last_init"] < 1200):
        return FLOW_SESSION["session"], FLOW_SESSION["csrf_token"]

    sess = requests.Session()
    sess.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Sec-Ch-Ua': '"Chromium";v="124", "Not(A:Brand";v="24", "Google Chrome";v="124"',
        'Sec-Ch-Ua-Mobile': '?0',
        'Sec-Ch-Ua-Platform': '"Windows"',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-User': '?1',
        'Upgrade-Insecure-Requests': '1'
    })

    try:
        r_home = sess.get('https://flowvideoplayer.com', timeout=8)
        csrf_match = re.search(r'name="csrf-token"\s+content="([^"]+)"', r_home.text)
        if not csrf_match:
            return None, None
        
        csrf_token = csrf_match.group(1)

        device_data = {
            "cpu": 8,
            "memory": 8,
            "touch": 0,
            "platform": "Win32",
            "lang": "en-US",
            "vendor": "Google Inc.",
            "webgl_vendor": "Google Inc. (NVIDIA)",
            "webgl_renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)",
            "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "backup_token": None,
            "os": "windows",
            "browser": "chrome",
            "pwa_installed": False
        }

        ajax_headers = {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'Origin': 'https://flowvideoplayer.com',
            'Referer': 'https://flowvideoplayer.com/',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin',
            'X-CSRF-TOKEN': csrf_token,
            'X-Requested-With': 'XMLHttpRequest',
        }

        r_init = sess.post('https://flowvideoplayer.com/device/init', json=device_data, headers=ajax_headers, timeout=8)
        if r_init.status_code == 200:
            FLOW_SESSION["session"] = sess
            FLOW_SESSION["csrf_token"] = csrf_token
            FLOW_SESSION["last_init"] = now
            return sess, csrf_token
    except Exception as e:
        print("Session init err:", e)

    return None, None


def extract_surl(raw_input: str) -> str:
    """Extracts the clean shorturl (surl) from ANY TeraBox / TeraShare / TeraBoxLink format."""
    if not raw_input:
        return ""
    
    # Strip all inner and outer whitespaces
    clean_input = re.sub(r'\s+', '', raw_input.strip())
    
    # 1. Query parameters (?surl=... or ?shorturl=... or ?key=...)
    if "?" in clean_input:
        try:
            parsed = urlparse(clean_input)
            qs = parse_qs(parsed.query)
            for k in ["surl", "shorturl", "key"]:
                if k in qs and qs[k]:
                    surl = qs[k][0]
                    return surl[1:] if surl.startswith("1") else surl
        except Exception:
            pass

    # 2. Regex for /s/1... or /s/...
    match = re.search(r'/s/(?:1)?([a-zA-Z0-9_-]{10,40})', clean_input)
    if match:
        return match.group(1)

    # 3. Regex for surl=...
    match_surl = re.search(r'surl=(?:1)?([a-zA-Z0-9_-]{10,40})', clean_input)
    if match_surl:
        return match_surl.group(1)

    # 4. Regex for /share/link, /sharing/link, or /share/file
    match_share = re.search(r'/share(?:ing)?/(?:link|file|init)?.*?1?([a-zA-Z0-9_-]{10,40})', clean_input)
    if match_share:
        return match_share.group(1)

    # 5. Raw key format
    match_raw = re.search(r'\b1?([a-zA-Z0-9_-]{15,40})\b', clean_input)
    if match_raw:
        val = match_raw.group(1)
        return val[1:] if val.startswith("1") else val

    return ""


FLARE_AES_KEY = b"CMrhmcd9oFUjWBBleiMfS0BiBfupaVsG"
FLARE_AES_IV = b"2Xk4dLo38c9Z2Q2a"


def decrypt_flare_data(encrypted_b64: str, key_str: str = None) -> str:
    """Decrypts AES-CBC stream data from Flare/CashSnap using IV 2Xk4dLo38c9Z2Q2a."""
    if not HAS_CRYPTO or not encrypted_b64:
        return ""
    try:
        clean_ciphertext = encrypted_b64.strip('"').strip()
        raw_cipher = base64.b64decode(clean_ciphertext)

        keys_to_try = [FLARE_AES_KEY]
        if key_str:
            keys_to_try.insert(0, key_str.encode('utf-8'))

        for k in keys_to_try:
            try:
                cipher = AES.new(k, AES.MODE_CBC, FLARE_AES_IV)
                decrypted = unpad(cipher.decrypt(raw_cipher), AES.block_size)
                res = decrypted.decode('utf-8')
                if res.startswith("http"):
                    return res
            except Exception:
                pass

        for k in keys_to_try:
            try:
                cipher = AES.new(k, AES.MODE_ECB)
                decrypted = unpad(cipher.decrypt(raw_cipher), AES.block_size)
                res = decrypted.decode('utf-8')
                if res.startswith("http"):
                    return res
            except Exception:
                pass

        return ""
    except Exception as e:
        print("Decrypt flare data error:", e)
        return ""


def extract_flare_id(raw_input: str) -> str:
    """Extracts numeric link ID from Flare / Flaredvns / CashSnap links, URLs, or raw IDs."""
    if not raw_input:
        return ""
    clean = raw_input.strip()

    # 1. Query parameter (?linkId=... or ?link_id=... or ?id=...)
    m1 = re.search(r'[?&#](?:linkId|link_id|linkid|id)=(\d{15,25})', clean, re.IGNORECASE)
    if m1:
        return m1.group(1)

    # 2. Path pattern like /s/2096112993141268482 or /share/2096112993141268482
    m2 = re.search(r'/(?:s|play|v|share|link)/(\d{15,25})', clean, re.IGNORECASE)
    if m2:
        return m2.group(1)

    # 3. Flare / CashSnap / HugeBox / BlinkNote / Flaredvns domains containing numbers
    if any(k in clean.lower() for k in ["flare", "hugebox", "cashsnap", "flaredvns", "blinknote", "cshsnp"]):
        m3 = re.search(r'(\d{15,25})', clean)
        if m3:
            return m3.group(1)

    # 4. Pure numeric ID (16 to 25 digits)
    m4 = re.search(r'\b(\d{16,25})\b', clean)
    if m4:
        return m4.group(1)

    return ""


def is_flare_link(raw_input: str) -> bool:
    """Detects if input is a Flare / CashSnap / HugeBox / Flaredvns link or numeric link ID."""
    return bool(extract_flare_id(raw_input))


def resolve_flare_stream(raw_url_or_id: str) -> dict:
    """Resolves video streams and playlists from Flare / Flaredvns / CashSnap links."""
    clean_id = extract_flare_id(raw_url_or_id)
    if not clean_id:
        return {"success": False, "error": "Please enter a valid Flare / Flaredvns link or Link ID."}

    now = time.time()
    cache_key = f"flare_{clean_id}"
    if cache_key in RESOLVE_CACHE:
        cached_time, cached_res = RESOLVE_CACHE[cache_key]
        if now - cached_time < 1200:
            return cached_res

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Content-Type': 'application/json',
        'Origin': 'https://www.flarewliv.com',
        'Referer': f'https://www.flarewliv.com/?linkId={clean_id}'
    }

    api_hosts = [
        "https://api.cshsnpcwio.com",
        "https://api.cashsnapnowhawk.com"
    ]

    files = []
    for host in api_hosts:
        try:
            r = requests.post(
                f"{host}/v1/h5/share/link/files/page",
                json={"link_id": clean_id, "page": 0, "size": 20, "file_type": "FILE"},
                headers=headers,
                timeout=8
            )
            if r.status_code == 200:
                data = r.json()
                if data.get("files"):
                    files = data["files"]
                    break
        except Exception:
            continue

    if not files:
        return {
            "success": False,
            "error": f"Flare link (ID: {clean_id}) has expired, been deleted by the owner, or is no longer accessible.",
            "surl": clean_id,
            "mode": "flare"
        }

    playlist = []
    for idx, f in enumerate(files):
        file_id = f.get("file_id") or f.get("id")
        namespace = f.get("namespace") or {}
        uid = namespace.get("name") or namespace.get("id") or f.get("uid") or ""
        file_meta = f.get("file_meta") or {}

        title = file_meta.get("display_name") or f.get("file_name") or f"Flare Video {idx+1}"
        raw_size = file_meta.get("size") or f.get("file_size") or 0
        size_str = f"{raw_size / (1024 * 1024):.1f} MB" if isinstance(raw_size, (int, float)) and raw_size > 0 else "HD Video"
        thumbnail = file_meta.get("thumbnail") or f.get("thumbnail") or ""

        stream_url = None
        for host in api_hosts:
            try:
                r_dl = requests.post(f"{host}/v1/h5/download_file_url", json={"uid": uid, "file_id": file_id}, headers=headers, timeout=8)
                if r_dl.status_code == 200 and r_dl.text:
                    enc_data = r_dl.text.strip('"').strip()
                    if enc_data:
                        stream_url = decrypt_flare_data(enc_data)
                        if stream_url:
                            break
            except Exception:
                continue

        playlist.append({
            "index": idx,
            "title": title,
            "size": size_str,
            "thumbnail": thumbnail,
            "stream_url": stream_url,
            "proxy_stream_url": f"/api/stream/proxy?url={quote(stream_url, safe='')}" if stream_url and ".m3u8" in stream_url else None,
            "download_url": stream_url
        })

    first = playlist[0]
    result = {
        "success": True,
        "surl": clean_id,
        "full_surl": clean_id,
        "title": first["title"],
        "size": first["size"],
        "size_bytes": 0,
        "duration_str": "Full HD",
        "thumbnail": first["thumbnail"],
        "stream_url": first["stream_url"],
        "proxy_stream_url": first.get("proxy_stream_url"),
        "download_url": first["download_url"],
        "is_hls": bool(first["stream_url"] and ".m3u8" in first["stream_url"]),
        "mode": "flare",
        "playlist": playlist
    }
    cache_set(cache_key, result)
    return result


def is_direct_stream(raw_input: str) -> bool:
    """Detects if input is a direct video (.mp4, .m3u8, .webm) URL."""
    if not raw_input:
        return False
    clean = raw_input.strip().lower()
    return clean.startswith("http") and any(ext in clean for ext in [".m3u8", ".mp4", ".webm", ".mkv", ".mov"])


def resolve_direct_stream(raw_input: str) -> dict:
    """Prepares direct streaming player payload for MP4 / M3U8 links."""
    clean = raw_input.strip()
    is_hls = ".m3u8" in clean.lower()
    parsed_path = urlparse(clean).path
    filename = os.path.basename(parsed_path) or "Direct Stream Video"

    return {
        "success": True,
        "surl": "direct",
        "full_surl": "direct",
        "title": f"Direct: {filename}",
        "size": "Direct Stream",
        "size_bytes": 0,
        "duration_str": "HD Quality",
        "thumbnail": None,
        "stream_url": clean,
        "proxy_stream_url": f"/api/stream/proxy?url={quote(clean, safe='')}" if is_hls else None,
        "download_url": clean if not is_hls else None,
        "is_hls": is_hls,
        "mode": "direct",
        "playlist": [{
            "index": 0,
            "title": filename,
            "size": "Direct Video",
            "thumbnail": None,
            "stream_url": clean,
            "proxy_stream_url": f"/api/stream/proxy?url={quote(clean, safe='')}" if is_hls else None,
            "download_url": clean if not is_hls else None
        }]
    }


# ----------------- YOUTUBE RESOLVER ENGINE -----------------

def extract_youtube_id(raw_input: str) -> str:
    """Extracts YouTube 11-char video ID from any YouTube URL format, shorts, youtu.be, or raw ID."""
    if not raw_input:
        return ""
    clean = raw_input.strip()

    # 1. Query parameter (?v=... or &v=...)
    if "v=" in clean:
        m = re.search(r'[?&]v=([a-zA-Z0-9_-]{11})', clean)
        if m:
            return m.group(1)

    # 2. Path formats (youtu.be/..., /shorts/..., /embed/..., /live/..., /v/..., /e/...)
    m_path = re.search(r'(?:youtu\.be\/|\/shorts\/|\/embed\/|\/live\/|\/v\/|\/e\/)([a-zA-Z0-9_-]{11})', clean)
    if m_path:
        return m_path.group(1)

    # 3. Pure 11-char ID
    m_raw = re.match(r'^[a-zA-Z0-9_-]{11}$', clean)
    if m_raw:
        return clean

    return ""


def is_youtube_link(raw_input: str) -> bool:
    """Detects if input is a YouTube video, shorts, or video ID."""
    if not raw_input:
        return False
    clean = raw_input.lower().strip()
    if any(k in clean for k in ["youtube.com", "youtu.be", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com"]):
        return True
    return bool(extract_youtube_id(raw_input))


def resolve_youtube_stream(raw_url_or_id: str) -> dict:
    """Resolves YouTube video stream, metadata, thumbnail, duration, and multi-quality download options."""
    video_id = extract_youtube_id(raw_url_or_id)
    target_url = f"https://www.youtube.com/watch?v={video_id}" if video_id else raw_url_or_id.strip()

    now = time.time()
    cache_key = f"yt_{video_id or target_url}"
    if cache_key in RESOLVE_CACHE:
        cached_time, cached_res = RESOLVE_CACHE[cache_key]
        if now - cached_time < 1200:  # 20 minutes cache
            return cached_res

    yt_dlp = get_ytdlp()
    if not yt_dlp:
        return {
            "success": False,
            "error": "YouTube extraction engine (yt-dlp) is not installed on the server.",
            "mode": "youtube"
        }

    vid = video_id or "youtube"
    title = f"YouTube Video {vid}"
    duration_str = "HD Video"
    uploader = "YouTube"
    views_str = ""
    thumbnail = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid != "youtube" else None
    formats = []

    # 1. Extract progressive format (format 18) via android client
    try:
        ydl_opts_prog = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'noplaylist': True,
            'format': None,
            'extractor_args': {'youtube': {'player_client': ['android']}}
        }
        with yt_dlp.YoutubeDL(ydl_opts_prog) as ydl_prog:
            info = ydl_prog.extract_info(target_url, download=False)
            if info:
                vid = info.get('id') or vid
                title = info.get('title') or title
                duration_secs = info.get('duration') or 0
                if duration_secs >= 3600:
                    duration_str = f"{int(duration_secs // 3600):02d}:{int((duration_secs % 3600) // 60):02d}:{int(duration_secs % 60):02d}"
                elif duration_secs > 0:
                    duration_str = f"{int(duration_secs // 60):02d}:{int(duration_secs % 60):02d}"
                uploader = info.get('uploader') or info.get('channel') or uploader
                views = info.get('view_count')
                if views:
                    views_str = f"{views:,} views"
                thumbnail = info.get('thumbnail') or thumbnail
                formats.extend(info.get('formats', []))
    except Exception as e:
        print("Android extraction notice:", e)

    # 2. Extract HD and Audio formats via android_creator
    try:
        ydl_opts_hd = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'noplaylist': True,
            'format': None,
            'extractor_args': {'youtube': {'player_client': ['android_creator']}}
        }
        with yt_dlp.YoutubeDL(ydl_opts_hd) as ydl_hd:
            info_hd = ydl_hd.extract_info(target_url, download=False)
            if info_hd and info_hd.get('formats'):
                formats.extend(info_hd['formats'])
                if title == f"YouTube Video {vid}" and info_hd.get('title'):
                    title = info_hd['title']
    except Exception as e:
        print("Android creator extraction notice:", e)

    try:
        if not formats and not thumbnail:
            raise Exception("No formats or metadata retrieved from YouTube")

            # 1. Progressive formats (both video and audio)
            prog_formats = [f for f in formats if f.get('vcodec') != 'none' and f.get('acodec') != 'none' and f.get('url')]
            best_prog = None
            if prog_formats:
                best_prog = sorted(prog_formats, key=lambda x: (x.get('height') or 0, x.get('tbr') or 0), reverse=True)[0]

            stream_url = best_prog['url'] if best_prog else None
            proxy_stream_url = f"/api/youtube/stream?v={vid}&itag={best_prog.get('format_id', '')}" if best_prog else None

            # 2. Build multi-quality download options
            download_formats = []
            seen_resolutions = set()

            # Progressive MP4
            if best_prog:
                h = best_prog.get('height') or '360'
                size_bytes = best_prog.get('filesize') or best_prog.get('filesize_approx') or 0
                size_label = f"{size_bytes / (1024*1024):.1f} MB" if size_bytes else "HD"
                download_formats.append({
                    "label": f"⚡ {h}p Fast MP4 (Video + Audio)",
                    "quality": f"{h}p MP4",
                    "format_id": best_prog.get('format_id'),
                    "ext": best_prog.get('ext', 'mp4'),
                    "size": size_label,
                    "download_url": f"/api/youtube/download?v={vid}&itag={best_prog.get('format_id')}&title={quote(title)}",
                    "is_progressive": True
                })
                seen_resolutions.add(f"{h}p")

            # HD Video formats (1080p, 720p, 480p, 360p, etc.)
            video_only_formats = [f for f in formats if f.get('vcodec') != 'none' and f.get('url')]
            sorted_video = sorted(video_only_formats, key=lambda x: (x.get('height') or 0, x.get('tbr') or 0), reverse=True)

            for f in sorted_video:
                h = f.get('height')
                if not h:
                    continue
                res_key = f"{h}p"
                if res_key in seen_resolutions:
                    continue
                seen_resolutions.add(res_key)
                size_bytes = f.get('filesize') or f.get('filesize_approx') or 0
                size_label = f"{size_bytes / (1024*1024):.1f} MB" if size_bytes else "HD"
                ext = f.get('ext', 'mp4')
                download_formats.append({
                    "label": f"📺 {res_key} HD Video ({ext.upper()})",
                    "quality": f"{res_key}",
                    "format_id": f.get('format_id'),
                    "ext": ext,
                    "size": size_label,
                    "download_url": f"/api/youtube/download?v={vid}&itag={f.get('format_id')}&title={quote(title)}",
                    "is_progressive": f.get('acodec') != 'none'
                })

            # Audio formats (M4A / MP3)
            audio_formats = [f for f in formats if f.get('vcodec') == 'none' and f.get('acodec') != 'none' and f.get('url')]
            if audio_formats:
                best_audio = sorted(audio_formats, key=lambda x: (x.get('abr') or x.get('tbr') or 0), reverse=True)[0]
                size_bytes = best_audio.get('filesize') or best_audio.get('filesize_approx') or 0
                size_label = f"{size_bytes / (1024*1024):.1f} MB" if size_bytes else "Audio"
                download_formats.append({
                    "label": f"🎵 High Quality Audio (M4A / MP3)",
                    "quality": "Audio MP3/M4A",
                    "format_id": best_audio.get('format_id'),
                    "ext": best_audio.get('ext', 'm4a'),
                    "size": size_label,
                    "download_url": f"/api/youtube/download?v={vid}&itag={best_audio.get('format_id')}&title={quote(title)}",
                    "is_progressive": True
                })

            primary_dl = download_formats[0]["download_url"] if download_formats else (stream_url or f"https://www.youtube.com/watch?v={vid}")

            result = {
                "success": True,
                "surl": vid,
                "full_surl": vid,
                "title": title,
                "channel": uploader,
                "views": views_str,
                "size": duration_str,
                "size_bytes": 0,
                "duration_str": duration_str,
                "thumbnail": thumbnail,
                "stream_url": stream_url,
                "proxy_stream_url": proxy_stream_url,
                "download_url": primary_dl,
                "download_formats": download_formats,
                "is_hls": False,
                "embed_url": f"https://www.youtube-nocookie.com/embed/{vid}?autoplay=1",
                "mode": "youtube",
                "playlist": [{
                    "index": 0,
                    "title": title,
                    "size": duration_str,
                    "thumbnail": thumbnail,
                    "stream_url": stream_url,
                    "proxy_stream_url": proxy_stream_url,
                    "download_url": primary_dl
                }]
            }

            cache_set(cache_key, result)
            return result

    except Exception as e:
        print("YouTube extraction notice:", e)
        # Ultra-reliable fallback via oEmbed + YouTube embed player
        fallback_title = "YouTube Video"
        fallback_thumb = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg" if video_id else None
        try:
            r_oembed = requests.get(f"https://www.youtube.com/oembed?url={quote(target_url)}&format=json", timeout=5)
            if r_oembed.status_code == 200:
                oembed_data = r_oembed.json()
                fallback_title = oembed_data.get('title') or fallback_title
                fallback_thumb = oembed_data.get('thumbnail_url') or fallback_thumb
        except Exception:
            pass

        vid = video_id or "dQw4w9WgXcQ"
        embed_result = {
            "success": True,
            "surl": vid,
            "full_surl": vid,
            "title": fallback_title,
            "size": "HD Video",
            "size_bytes": 0,
            "duration_str": "HD Video",
            "thumbnail": fallback_thumb,
            "stream_url": None,
            "proxy_stream_url": None,
            "download_url": f"https://www.youtube.com/watch?v={vid}",
            "is_hls": False,
            "embed_url": f"https://www.youtube-nocookie.com/embed/{vid}?autoplay=1",
            "mode": "youtube",
            "playlist": [{
                "index": 0,
                "title": fallback_title,
                "size": "HD Video",
                "thumbnail": fallback_thumb,
                "stream_url": None,
                "proxy_stream_url": None,
                "download_url": f"https://www.youtube.com/watch?v={vid}"
            }]
        }
        cache_set(cache_key, embed_result)
        return embed_result



# ----------------- DISKWALA RESOLVER ENGINE -----------------

def extract_diskwala_id(raw_input: str) -> str:
    """Extracts the DiskWala file/watch code or ID from any DiskWala URL format."""
    if not raw_input:
        return ""
    clean = raw_input.strip()

    # 1. Path match (/watch/ID, /d/ID, /file/ID, /v/ID, /e/ID, /download/ID)
    m = re.search(r'/(?:watch|d|file|v|e|download|embed)/([a-zA-Z0-9_-]{4,64})', clean, re.IGNORECASE)
    if m:
        return m.group(1)

    # 2. Query param (?id=... or ?v=... or ?code=... or ?url=...)
    m_q = re.search(r'[?&](?:id|v|code|file_id)=([a-zA-Z0-9_-]{4,64})', clean, re.IGNORECASE)
    if m_q:
        return m_q.group(1)

    # 3. If domain is diskwala and last part is alphanumeric
    if any(d in clean.lower() for d in ["diskwala", "disk.wlc.pw"]):
        parts = clean.rstrip('/').split('?')[0].split('/')
        if len(parts) >= 4 and re.match(r'^[a-zA-Z0-9_-]{4,64}$', parts[-1]):
            return parts[-1]

    return ""


def is_diskwala_link(raw_input: str) -> bool:
    """Detects if input is a DiskWala link."""
    if not raw_input:
        return False
    clean = raw_input.lower().strip()
    return any(k in clean for k in ["diskwala", "disk.wlc.pw", "diskwala.net", "diskwala.com", "diskwala.org"])


def resolve_diskwala_stream(raw_url_or_id: str) -> dict:
    """Resolves DiskWala videos via Method 1: Mobile App Client Emulation (Android Headers & API Signatures)."""
    clean = raw_url_or_id.strip()
    disk_id = extract_diskwala_id(clean)

    target_url = clean
    if not target_url.startswith("http"):
        target_url = f"https://diskwala.com/watch/{clean}" if disk_id else f"https://{clean}"

    cache_key = f"diskwala_{disk_id or target_url}"
    now = time.time()
    if cache_key in RESOLVE_CACHE:
        cached_time, cached_res = RESOLVE_CACHE[cache_key]
        if now - cached_time < 1200:
            return cached_res

    title = f"DiskWala Video {disk_id}" if disk_id else "DiskWala Video"
    stream_url = None
    download_url = target_url
    thumbnail = None
    size_str = "DiskWala HD"

    # Method 1: Android Mobile App Client Headers & Signatures (Bypasses Web "Download App" Walls)
    mobile_app_headers = {
        'User-Agent': 'DiskWala/2.4.2 (Linux; U; Android 13; en-US; SM-S918B Build/TP1A.220624.014) Mobile/1.0',
        'X-App-Version': '2.4.2',
        'X-Client-Type': 'android',
        'X-Platform': 'Android',
        'X-Requested-With': 'com.diskwala.app',
        'Accept': 'application/json, text/plain, */*',
        'Authorization': f'Bearer {DISKWALA_API_KEY}' if DISKWALA_API_KEY else ''
    }

    desktop_headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/html, */*',
        'Referer': 'https://diskwala.com/',
        'Authorization': f'Bearer {DISKWALA_API_KEY}' if DISKWALA_API_KEY else ''
    }

    # Query Mobile App API and Web API endpoints
    api_endpoints = [
        (f"https://diskwala.com/api/v1/app/file/play?key={DISKWALA_API_KEY}&file_code={disk_id}", mobile_app_headers),
        (f"https://diskwala.com/api/file/info?key={DISKWALA_API_KEY}&file_code={disk_id}", mobile_app_headers),
        (f"https://diskwala.com/api/file/direct_link?key={DISKWALA_API_KEY}&file_code={disk_id}", mobile_app_headers),
        (f"https://diskwala.com/api/v1/file/info?key={DISKWALA_API_KEY}&file_id={disk_id}", desktop_headers),
        (f"https://diskwala.net/api?key={DISKWALA_API_KEY}&url={quote(target_url, safe='')}", desktop_headers),
        (f"https://diskwala.net/api?api_key={DISKWALA_API_KEY}&id={disk_id}", desktop_headers),
        (f"https://api.diskwala.com/v1/file/play?key={DISKWALA_API_KEY}&file_id={disk_id}", mobile_app_headers)
    ]

    for ep, hdrs in api_endpoints:
        try:
            r_api = requests.get(ep, headers=hdrs, timeout=5)
            if r_api.status_code == 200:
                data = r_api.json()
                if isinstance(data, dict):
                    res_obj = data.get("result") or data.get("data") or data.get("response") or data
                    title = res_obj.get("title") or res_obj.get("file_name") or res_obj.get("name") or title
                    thumbnail = res_obj.get("thumbnail") or res_obj.get("poster") or thumbnail
                    size_raw = res_obj.get("size") or res_obj.get("file_size")
                    if size_raw:
                        size_str = str(size_raw)
                    stream_candidate = res_obj.get("stream_url") or res_obj.get("direct_link") or res_obj.get("download_url") or res_obj.get("url") or res_obj.get("play_url")
                    if stream_candidate and ("http" in str(stream_candidate)):
                        stream_url = stream_candidate
                        download_url = stream_candidate
                        break
        except Exception:
            pass

    if not stream_url:
        return {
            "success": False,
            "error": "DiskWala Mobile App API did not return an active stream link for this file. Please verify the link or API credentials.",
            "mode": "diskwala"
        }

    is_hls = bool(".m3u8" in stream_url)
    result = {
        "success": True,
        "surl": disk_id or "diskwala",
        "full_surl": disk_id or "diskwala",
        "title": title,
        "size": size_str,
        "size_bytes": 0,
        "duration_str": "HD Stream",
        "thumbnail": thumbnail,
        "stream_url": stream_url,
        "proxy_stream_url": f"/api/diskwala/stream?url={quote(stream_url, safe='')}" if stream_url else None,
        "download_url": download_url,
        "is_hls": is_hls,
        "mode": "diskwala",
        "playlist": [{
            "index": 0,
            "title": title,
            "size": size_str,
            "thumbnail": thumbnail,
            "stream_url": stream_url,
            "proxy_stream_url": f"/api/diskwala/stream?url={quote(stream_url, safe='')}" if stream_url else None,
            "download_url": download_url
        }]
    }

    cache_set(cache_key, result)
    return result


def resolve_universal_stream(raw_input: str, mode: str = None) -> dict:
    """Intelligently routes any input to YouTube, DiskWala, TeraBox, Flare/CashSnap, or Direct Video player."""
    clean = raw_input.strip()
    if not clean:
        return {"success": False, "error": "Please enter a valid link."}

    # 1. YouTube mode or YouTube link format
    if mode == "youtube" or is_youtube_link(clean):
        return resolve_youtube_stream(clean)

    # 2. DiskWala mode or DiskWala link format
    if mode == "diskwala" or is_diskwala_link(clean):
        return resolve_diskwala_stream(clean)

    # 3. Flare mode or Flare link format (flaredvns, flare*, hugebox, or numeric link ID)
    if mode == "flare" or is_flare_link(clean):
        return resolve_flare_stream(clean)

    # 4. Direct video stream (.mp4, .m3u8, .webm)
    if mode == "direct" or (is_direct_stream(clean) and not any(k in clean.lower() for k in ["terabox", "1024tera", "terashare", "flare", "hugebox", "cashsnap", "youtube", "youtu.be", "diskwala"])):
        return resolve_direct_stream(clean)

    # 5. TeraBox / 1024Tera / TeraShare links
    return resolve_terabox_stream(clean)


def resolve_terabox_stream(raw_url_or_surl: str) -> dict:
    """Extracts direct HLS stream (.m3u8), download link, thumbnails, and metadata with high-speed caching."""
    clean_surl = extract_surl(raw_url_or_surl)
    if not clean_surl:
        return {"success": False, "error": "Please enter a valid TeraBox share link."}

    # Check cache first for instant sub-millisecond response
    now = time.time()
    if clean_surl in RESOLVE_CACHE:
        cached_time, cached_res = RESOLVE_CACHE[clean_surl]
        if now - cached_time < 1200:  # 20 minutes cache
            return cached_res

    full_surl = f"1{clean_surl}"

    result = {
        "success": True,
        "surl": clean_surl,
        "full_surl": full_surl,
        "title": "TeraBox Video Stream",
        "size": "HD 720p / 1080p",
        "size_bytes": 0,
        "duration_str": "Full Length",
        "thumbnail": None,
        "stream_url": None,
        "download_url": None,
        "is_hls": False,
        "embed_url": f"https://www.terabox.app/sharing/embed?surl={full_surl}",
        "mode": "terabox",
        "playlist": []
    }

    session_obj, csrf_token = get_flow_session()
    if session_obj and csrf_token:
        try:
            ajax_headers = {
                'Accept': 'application/json',
                'Content-Type': 'application/json',
                'Origin': 'https://flowvideoplayer.com',
                'Referer': 'https://flowvideoplayer.com/',
                'Sec-Fetch-Dest': 'empty',
                'Sec-Fetch-Mode': 'cors',
                'Sec-Fetch-Site': 'same-origin',
                'X-CSRF-TOKEN': csrf_token,
                'X-Requested-With': 'XMLHttpRequest',
            }

            # Top high-performing canonical formats for instant resolution
            candidate_links = [
                f"https://teraboxlink.com/s/1{clean_surl}",
                f"https://1024tera.com/s/1{clean_surl}",
                f"https://www.terabox.app/wap/share/filelist?surl={clean_surl}"
            ]

            clean_raw = raw_url_or_surl.strip().replace(" ", "")
            if clean_raw.startswith("http") and "wap/share" in clean_raw:
                candidate_links.insert(0, clean_raw)

            for target_link in candidate_links:
                try:
                    resp = session_obj.post('https://flowvideoplayer.com/search/video', json={'url': target_link}, headers=ajax_headers, timeout=8)
                    if resp.status_code == 200:
                        data = resp.json()
                        if data.get("status") is True and data.get("response") and len(data["response"]) > 0:
                            video_list = data["response"]
                            first_video = video_list[0]

                            result["title"] = first_video.get("file_name") or "TeraBox Video Stream"
                            result["size"] = first_video.get("file_size") or "HD Video"
                            result["size_bytes"] = first_video.get("file_size_bytes") or 0
                            result["thumbnail"] = first_video.get("thumbnail")
                            result["stream_url"] = first_video.get("fast_stream_url")
                            result["download_url"] = first_video.get("download_url")
                            result["proxy_stream_url"] = f"/api/stream/proxy?url={quote(result['stream_url'], safe='')}" if result["stream_url"] else None
                            result["is_hls"] = bool(result["stream_url"])

                            playlist = []
                            for idx, v in enumerate(video_list):
                                item_stream = v.get("fast_stream_url")
                                playlist.append({
                                    "index": idx,
                                    "title": v.get("file_name") or f"Part {idx+1}",
                                    "size": v.get("file_size") or "",
                                    "thumbnail": v.get("thumbnail"),
                                    "stream_url": item_stream,
                                    "proxy_stream_url": f"/api/stream/proxy?url={quote(item_stream, safe='')}" if item_stream else None,
                                    "download_url": v.get("download_url")
                                })
                            result["playlist"] = playlist

                            # Save to bounded memory cache for instant future loads
                            cache_set(clean_surl, result)
                            return result
                except Exception as inner_e:
                    continue
        except Exception as e:
            print("Direct HLS extraction error:", e)

    # If stream could not be extracted (expired/deleted link)
    if not result.get("stream_url"):
        result["success"] = False
        result["error"] = "This TeraBox video link has expired, been deleted by the owner, or is no longer available on TeraBox."

    return result


# ----------------- USER PUBLIC ROUTES -----------------

@app.route('/')
def index():
    query_url = request.args.get('url') or request.args.get('surl')
    if query_url:
        info = resolve_universal_stream(query_url)
        user_ip = request.headers.get('X-Forwarded-For', request.remote_addr or '127.0.0.1').split(',')[0].strip()
        user_agent = request.headers.get('User-Agent', 'Unknown')
        title_to_log = info.get("title") if info.get("success") else f"[Unresolved / Expired: {info.get('error', 'Error')}]"
        log_search(
            searched_url=query_url,
            surl=info.get("surl") or extract_youtube_id(query_url) or extract_surl(query_url) or extract_flare_id(query_url),
            video_title=title_to_log,
            video_size=info.get("size", "HD Video"),
            stream_url=info.get("stream_url", ""),
            download_url=info.get("download_url", ""),
            user_ip=user_ip,
            user_agent=user_agent
        )
        return render_template(
            "index.html",
            initial_data=info if info.get("success") else None,
            initial_url=query_url
        )
    
    return render_template(
        "index.html",
        initial_data=None,
        initial_url=""
    )


@app.route('/play/<surl>')
def play_surl(surl):
    info = resolve_universal_stream(surl)
    user_ip = request.headers.get('X-Forwarded-For', request.remote_addr or '127.0.0.1').split(',')[0].strip()
    user_agent = request.headers.get('User-Agent', 'Unknown')
    title_to_log = info.get("title") if info.get("success") else f"[Unresolved / Expired: {info.get('error', 'Error')}]"
    
    searched_url = surl
    if not is_flare_link(surl) and not is_youtube_link(surl):
        searched_url = f"https://teraboxshare.com/s/1{surl}"

    log_search(
        searched_url=searched_url,
        surl=info.get("surl") or surl,
        video_title=title_to_log,
        video_size=info.get("size", "HD Video"),
        stream_url=info.get("stream_url", ""),
        download_url=info.get("download_url", ""),
        user_ip=user_ip,
        user_agent=user_agent
    )
    return render_template(
        "index.html",
        initial_data=info if info.get("success") else None,
        initial_url=surl
    )


@app.route('/api/resolve', methods=['GET', 'POST'])
def api_resolve():
    raw_input = ""
    mode = ""
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        raw_input = data.get('url') or data.get('surl') or ""
        mode = data.get('mode') or ""
    else:
        raw_input = request.args.get('url') or request.args.get('surl') or ""
        mode = request.args.get('mode') or ""

    if not raw_input:
        return jsonify({"success": False, "error": "No link provided. Please paste a video link."}), 400

    user_ip = request.headers.get('X-Forwarded-For', request.remote_addr or '127.0.0.1').split(',')[0].strip()
    user_agent = request.headers.get('User-Agent', 'Unknown')

    stream_info = resolve_universal_stream(raw_input, mode=mode)

    # Guaranteed logging for every search query (success or failed/expired)
    title_to_log = stream_info.get("title") if stream_info.get("success") else f"[Unresolved / Expired: {stream_info.get('error', 'Error')}]"
    log_search(
        searched_url=raw_input,
        surl=stream_info.get("surl") or extract_youtube_id(raw_input) or extract_surl(raw_input) or extract_flare_id(raw_input),
        video_title=title_to_log,
        video_size=stream_info.get("size", "HD Video"),
        stream_url=stream_info.get("stream_url", ""),
        download_url=stream_info.get("download_url", ""),
        user_ip=user_ip,
        user_agent=user_agent
    )

    return jsonify(stream_info)


@app.route('/api/log_client', methods=['POST'])
def api_log_client():
    """Logs or updates client-resolved streaming results (DiskWala / Cloudflare bypass) to the lifetime database."""
    try:
        data = request.get_json(silent=True) or {}
        searched_url = data.get("searched_url") or ""
        surl = data.get("surl") or ""
        video_title = data.get("video_title") or "DiskWala Video"
        video_size = data.get("video_size") or "HD Video"
        stream_url = data.get("stream_url") or ""
        download_url = data.get("download_url") or ""

        user_ip = request.headers.get('X-Forwarded-For', request.remote_addr or '127.0.0.1').split(',')[0].strip()
        user_agent = request.headers.get('User-Agent', 'Unknown')

        log_search(
            searched_url=searched_url,
            surl=surl,
            video_title=video_title,
            video_size=video_size,
            stream_url=stream_url,
            download_url=download_url,
            user_ip=user_ip,
            user_agent=user_agent
        )
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/diskwala/stream')
def diskwala_stream_proxy():
    """Proxies DiskWala media streams with mobile app signatures and Referer headers to bypass hotlinking/403 blocks."""
    target_stream = request.args.get('url')
    if not target_stream:
        return Response("Missing stream URL", status=400)

    proxy_headers = {
        'User-Agent': 'DiskWala/2.4.2 (Linux; U; Android 13; en-US; SM-S918B Build/TP1A.220624.014) Mobile/1.0',
        'Referer': 'https://diskwala.com/',
        'Origin': 'https://diskwala.com'
    }

    range_header = request.headers.get('Range')
    if range_header:
        proxy_headers['Range'] = range_header

    try:
        req = requests.get(target_stream, headers=proxy_headers, stream=True, timeout=12)
        resp_headers = {}
        for h in ['Content-Type', 'Content-Length', 'Accept-Ranges', 'Content-Range']:
            if h in req.headers:
                resp_headers[h] = req.headers[h]
        resp_headers['Access-Control-Allow-Origin'] = '*'

        def generate():
            for chunk in req.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk

        return Response(generate(), status=req.status_code, headers=resp_headers)
    except Exception as e:
        return Response(f"DiskWala stream error: {e}", status=502)


@app.route('/api/youtube/stream')
def youtube_stream_proxy():
    """Proxies YouTube media stream chunks with Range header support for fast buffer & seeking."""
    video_id = request.args.get('v') or request.args.get('id')
    itag = request.args.get('itag')
    if not video_id:
        return Response("Missing video ID", status=400)

    # Resolve or use cached info
    cache_key = f"yt_{video_id}"
    resolved = None
    if cache_key in RESOLVE_CACHE:
        _, resolved = RESOLVE_CACHE[cache_key]
    else:
        resolved = resolve_youtube_stream(video_id)

    if not resolved or not resolved.get("success"):
        return Response("Could not resolve video stream", status=404)

    target_url = resolved.get("stream_url")
    if not target_url:
        return Response("No streamable format found", status=404)

    req_headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
    }
    if 'Range' in request.headers:
        req_headers['Range'] = request.headers['Range']

    try:
        r = requests.get(target_url, headers=req_headers, stream=True, timeout=25)
        def generate():
            try:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        yield chunk
            finally:
                try:
                    r.close()
                except Exception:
                    pass

        resp = Response(generate(), status=r.status_code, content_type=r.headers.get('content-type', 'video/mp4'))
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Methods'] = 'GET, HEAD, OPTIONS'
        if 'Content-Range' in r.headers:
            resp.headers['Content-Range'] = r.headers['Content-Range']
        if 'Content-Length' in r.headers:
            resp.headers['Content-Length'] = r.headers['Content-Length']
        if 'Accept-Ranges' in r.headers:
            resp.headers['Accept-Ranges'] = r.headers['Accept-Ranges']
        return resp
    except Exception as e:
        return Response(f"Stream proxy error: {str(e)}", status=500)


@app.route('/api/youtube/download')
def youtube_download():
    """Directly streams video/audio file attachment with clean filename and maximum download speed."""
    video_id = request.args.get('v') or request.args.get('id')
    itag = request.args.get('itag')
    title = request.args.get('title') or f"YouTube_Video_{video_id}"

    if not video_id:
        return Response("Missing video ID", status=400)

    yt_dlp = get_ytdlp()
    if not yt_dlp:
        return Response("YouTube extraction engine not available", status=500)

    # Sanitize title for filename
    clean_title = re.sub(r'[\\/*?:"<>|]', "", title).strip() or f"youtube_{video_id}"

    target_url = None
    ext = "mp4"
    formats = []
    try:
        # Try android client first
        with yt_dlp.YoutubeDL({
            'quiet': True, 'no_warnings': True, 'skip_download': True, 'noplaylist': True,
            'format': None,
            'extractor_args': {'youtube': {'player_client': ['android']}}
        }) as ydl_and:
            info_and = ydl_and.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            if info_and and info_and.get('formats'):
                formats.extend(info_and['formats'])

        # If specific itag requested and not in android formats, try android_creator
        if itag and not any(str(f.get('format_id')) == str(itag) for f in formats):
            with yt_dlp.YoutubeDL({
                'quiet': True, 'no_warnings': True, 'skip_download': True, 'noplaylist': True,
                'format': None,
                'extractor_args': {'youtube': {'player_client': ['android_creator']}}
            }) as ydl_hd:
                info_hd = ydl_hd.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
                if info_hd and info_hd.get('formats'):
                    formats.extend(info_hd['formats'])

        matched_format = None
        if itag:
            for f in formats:
                if str(f.get('format_id')) == str(itag):
                    matched_format = f
                    break
        if not matched_format:
            progs = [f for f in formats if f.get('vcodec') != 'none' and f.get('acodec') != 'none' and f.get('url')]
            matched_format = progs[0] if progs else (formats[-1] if formats else None)

        if matched_format:
            target_url = matched_format.get('url')
            ext = matched_format.get('ext', 'mp4')
    except Exception as e:
        print("YouTube download extract error:", e)

    if not target_url:
        return Response("Failed to resolve download URL", status=500)

    # Stream the file directly as an attachment
    req_headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
    }
    try:
        r = requests.get(target_url, headers=req_headers, stream=True, timeout=30)
        def generate():
            try:
                for chunk in r.iter_content(chunk_size=131072):
                    if chunk:
                        yield chunk
            finally:
                try:
                    r.close()
                except Exception:
                    pass

        content_type = r.headers.get('content-type', 'video/mp4')
        resp = Response(generate(), status=r.status_code, content_type=content_type)
        resp.headers['Content-Disposition'] = f'attachment; filename="{clean_title}.{ext}"'
        resp.headers['Access-Control-Allow-Origin'] = '*'
        if 'Content-Length' in r.headers:
            resp.headers['Content-Length'] = r.headers['Content-Length']
        return resp
    except Exception as e:
        return Response(f"Download stream error: {str(e)}", status=500)


@app.route('/api/stream/proxy')
def stream_proxy():
    """Proxies HLS .m3u8 playlists and video streams with full CORS headers."""
    target_url = request.args.get('url')
    if not target_url:
        return Response("Missing URL parameter", status=400)

    req_headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Referer': 'https://flowvideoplayer.com/'
    }
    if 'Range' in request.headers:
        req_headers['Range'] = request.headers['Range']

    try:
        if '.m3u8' in target_url or 'get_m3u8' in target_url:
            r = requests.get(target_url, headers=req_headers, timeout=12)
            content = r.text
            base_url = target_url.rsplit('/', 1)[0] + '/'
            lines = []
            for line in content.splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith('#') and not stripped.startswith('http'):
                    full_segment_url = base_url + stripped
                    lines.append(f"/api/stream/proxy?url={quote(full_segment_url, safe='')}")
                else:
                    lines.append(line)
            rewritten_m3u8 = '\n'.join(lines).encode('utf-8')
            resp = Response(rewritten_m3u8, status=r.status_code, content_type="application/vnd.apple.mpegurl; charset=utf-8")
            resp.headers['Access-Control-Allow-Origin'] = '*'
            resp.headers['Access-Control-Allow-Methods'] = 'GET, HEAD, OPTIONS'
            resp.headers['Cache-Control'] = 'no-cache'
            return resp
        else:
            r = requests.get(target_url, headers=req_headers, stream=True, timeout=20)
            def generate():
                try:
                    for chunk in r.iter_content(chunk_size=65536):
                        if chunk:
                            yield chunk
                finally:
                    try:
                        r.close()
                    except Exception:
                        pass

            resp = Response(generate(), status=r.status_code, content_type=r.headers.get('content-type', 'video/MP2T'))
            resp.headers['Access-Control-Allow-Origin'] = '*'
            resp.headers['Access-Control-Allow-Methods'] = 'GET, HEAD, OPTIONS'
            if 'Content-Range' in r.headers:
                resp.headers['Content-Range'] = r.headers['Content-Range']
            if 'Content-Length' in r.headers:
                resp.headers['Content-Length'] = r.headers['Content-Length']
            if 'Accept-Ranges' in r.headers:
                resp.headers['Accept-Ranges'] = r.headers['Accept-Ranges']
            return resp
    except Exception as e:
        return Response(f"Proxy stream error: {str(e)}", status=500)


@app.route('/ping', methods=['GET', 'HEAD'])
def ping():
    """Ultra-fast ping endpoint for UptimeRobot, Render, and health monitors (keeps server awake 24/7)."""
    return jsonify({"status": "ok", "message": "pong", "service": "TeraStream Pro Native"}), 200


@app.route('/api/health', methods=['GET', 'HEAD'])
def health():
    """Detailed health check endpoint for monitoring uptime, memory, and services."""
    return jsonify({
        "status": "ok",
        "service": "TeraStream Pro Native",
        "port": PORT,
        "crypto": HAS_CRYPTO,
        "ytdlp_loaded": _YTDLP_MODULE is not None
    }), 200


# ----------------- ADMIN CURATED VIDEO FEED & MULTI-SOURCE SCRAPER -----------------

def scrape_feed_source(source_url):
    """
    Universal scraper capable of parsing bio.site, linktree, and video link aggregators.
    Returns a list of dicts: [{'title': ..., 'video_url': ..., 'thumbnail_url': ..., 'surl': ...}]
    """
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9'
    }
    try:
        # Normalize telegram source URLs to web preview
        target_url = source_url
        if 't.me/' in source_url:
            target_url = re.sub(r'https?://t\.me/(?!s/)([^/]+)', r'https://t.me/s/\1', source_url.strip())

        r = requests.get(target_url, headers=headers, timeout=15)
        if r.status_code != 200:
            print(f"Failed to fetch source {source_url}: status {r.status_code}")
            return []

        soup = BeautifulSoup(r.text, 'html.parser')
        raw_items = []

        # 1. Telegram Channel Web Preview (t.me/s/...)
        tg_wraps = soup.find_all('div', class_='tgme_widget_message_wrap')
        if tg_wraps:
            for wrap in tg_wraps:
                text_el = wrap.find('div', class_='tgme_widget_message_text')
                text = text_el.get_text(separator="\n", strip=True) if text_el else ""

                img_src = ""
                photo_el = wrap.find('a', class_='tgme_widget_message_photo_wrap')
                if photo_el and photo_el.get('style'):
                    m_bg = re.search(r"background-image:url\('([^']+)'\)", photo_el['style'])
                    if m_bg:
                        img_src = m_bg.group(1)

                links = re.findall(r'https?://[^\s<>"]+', text)
                if text_el:
                    for a in text_el.find_all('a', href=True):
                        links.append(a['href'])

                lines = [l.strip() for l in text.split('\n') if l.strip() and not l.startswith('http')]
                title = lines[0] if lines else 'Telegram Video'

                for href in links:
                    clean_surl = extract_surl(href) or extract_youtube_id(href) or extract_flare_id(href)
                    is_video = bool(clean_surl) or any(k in href.lower() for k in ['terabox', 'terashare', 'mirrobox', 'nephobox', '4funbox'])
                    if is_video:
                        raw_items.append({
                            'title': title[:180],
                            'video_url': href,
                            'thumbnail_url': img_src,
                            'surl': clean_surl
                        })

        # 2. bio.site specific detection
        biosite_links = soup.find_all('a', attrs={'data-cy': 'biosite-link'})
        if biosite_links:
            for a in biosite_links:
                href = (a.get('href') or '').strip()
                title_el = a.find(attrs={'data-cy': 'link-text-name'})
                title = title_el.get_text(strip=True) if title_el else ''
                img_el = a.find('img')
                img_src = (img_el.get('src') or '').strip() if img_el else ''

                if not href:
                    continue

                clean_surl = extract_surl(href) or extract_youtube_id(href) or extract_flare_id(href)
                is_video = bool(clean_surl) or any(k in href.lower() for k in ['terabox', 'terashare', 'mirrobox', 'nephobox', '4funbox', 'youtube.com', 'youtu.be', 'flare'])

                if is_video:
                    raw_items.append({
                        'title': title or 'Video Post',
                        'video_url': href,
                        'thumbnail_url': img_src,
                        'surl': clean_surl
                    })
        elif not tg_wraps:
            # 3. Generic HTML / aggregator fallback
            for a in soup.find_all('a', href=True):
                href = (a.get('href') or '').strip()
                if not href.startswith('http'):
                    continue
                clean_surl = extract_surl(href) or extract_youtube_id(href) or extract_flare_id(href)
                is_video = bool(clean_surl) or any(k in href.lower() for k in ['terabox', 'terashare', 'mirrobox', 'nephobox', '4funbox', 'youtube.com', 'youtu.be', 'flare'])
                if is_video:
                    title = a.get_text(strip=True)
                    if not title:
                        title = a.get('title') or a.get('aria-label') or ''
                    img_el = a.find('img') or (a.parent and a.parent.find('img'))
                    img_src = (img_el.get('src') or '').strip() if img_el else ''

                    raw_items.append({
                        'title': title or 'Video Post',
                        'video_url': href,
                        'thumbnail_url': img_src,
                        'surl': clean_surl
                    })

        # Deduplicate while preserving order
        seen_urls = set()
        unique_items = []
        for it in raw_items:
            u = it['video_url']
            if u not in seen_urls:
                seen_urls.add(u)
                unique_items.append(it)

        return unique_items
    except Exception as e:
        print(f"Error scraping source {source_url}:", e)
        return []


def import_single_link(raw_input: str) -> dict:
    """
    Imports video(s) into the admin feed from:
    1. Telegram post link (public e.g. t.me/channel/123 or private t.me/c/...)
    2. Direct TeraBox / video link
    3. Copied message text containing video links
    """
    clean_input = (raw_input or "").strip()
    if not clean_input:
        return {"success": False, "error": "Please enter a valid link or post URL."}

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
    }

    found_videos = []

    # Case 1: Telegram Post Link (e.g. https://t.me/channel/123 or https://t.me/s/channel/123)
    tg_match = re.search(r't\.me/(?:s/)?([^/\s]+)/(\d+)', clean_input)
    if tg_match:
        channel, msg_id = tg_match.group(1), tg_match.group(2)
        if channel == 'c':
            # Private group link: https://t.me/c/CHAT_ID/MSG_ID
            config_file = os.path.join(os.path.dirname(__file__), "telegram_config.json")
            session_file = os.path.join(os.path.dirname(__file__), "telegram_session.session")
            if os.path.exists(config_file) and os.path.exists(session_file):
                try:
                    with open(config_file, "r", encoding="utf-8") as f:
                        cfg = json.load(f)
                    api_id = cfg.get("api_id")
                    api_hash = cfg.get("api_hash")
                    from telethon import TelegramClient
                    import asyncio

                    async def get_private_msg():
                        client = TelegramClient(os.path.join(os.path.dirname(__file__), "telegram_session"), api_id, api_hash)
                        await client.connect()
                        if await client.is_user_authorized():
                            chat_num = int(f"-100{msg_id}") if not msg_id else int(f"-100{channel}")
                            real_msg_id = int(tg_match.group(2))
                            msg = await client.get_messages(chat_num, ids=real_msg_id)
                            await client.disconnect()
                            return msg
                        await client.disconnect()
                        return None

                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    msg = loop.run_until_complete(get_private_msg())
                    if msg:
                        text = msg.text or ""
                        links = re.findall(r'https?://[^\s<>"]+', text)
                        lines = [l.strip() for l in text.split('\n') if l.strip() and not l.startswith('http')]
                        title = lines[0] if lines else f"Telegram Private Video #{msg_id}"
                        for href in links:
                            surl = extract_surl(href)
                            if surl or any(k in href.lower() for k in ['terabox', 'terashare', 'mirrobox']):
                                found_videos.append({
                                    "title": title,
                                    "video_url": href,
                                    "thumbnail_url": "",
                                    "surl": surl,
                                    "source_name": "Telegram (Private)"
                                })
                except Exception as e:
                    print("Error fetching private telegram post:", e)

            if not found_videos:
                # Check if there are other links inside the input string
                extra_links = re.findall(r'https?://[^\s<>"]+', clean_input)
                video_links = [l for l in extra_links if not 't.me/' in l and (extract_surl(l) or any(k in l.lower() for k in ['terabox', 'terashare', 'mirrobox']))]
                if not video_links:
                    return {
                        "success": False,
                        "error": f"Post link '{clean_input}' is from a private Telegram group. Either authenticate via setup_telegram.py, or paste the video link directly!"
                    }
        else:
            # Public channel post: https://t.me/channel/msg_id
            scrape_url = f"https://t.me/s/{channel}/{msg_id}"
            try:
                r = requests.get(scrape_url, headers=headers, timeout=10)
                if r.status_code == 200:
                    soup = BeautifulSoup(r.text, 'html.parser')
                    text_el = soup.find('div', class_='tgme_widget_message_text')
                    text = text_el.get_text(separator="\n", strip=True) if text_el else ""

                    img_src = ""
                    photo_el = soup.find('a', class_='tgme_widget_message_photo_wrap')
                    if photo_el and photo_el.get('style'):
                        m_bg = re.search(r"background-image:url\('([^']+)'\)", photo_el['style'])
                        if m_bg:
                            img_src = m_bg.group(1)

                    links = re.findall(r'https?://[^\s<>"]+', text)
                    if text_el:
                        for a in text_el.find_all('a', href=True):
                            links.append(a['href'])

                    lines = [l.strip() for l in text.split('\n') if l.strip() and not l.startswith('http')]
                    title = lines[0] if lines else f"Telegram Post #{msg_id}"

                    for href in links:
                        clean_surl = extract_surl(href) or extract_youtube_id(href) or extract_flare_id(href)
                        is_video = bool(clean_surl) or any(k in href.lower() for k in ['terabox', 'terashare', 'mirrobox', 'nephobox', '4funbox'])
                        if is_video:
                            found_videos.append({
                                "title": title,
                                "video_url": href,
                                "thumbnail_url": img_src,
                                "surl": clean_surl,
                                "source_name": f"Telegram (@{channel})"
                            })
            except Exception as e:
                print("Error scraping public telegram post:", e)

    # Case 2: Extract all video / TeraBox links directly from input
    if not found_videos:
        raw_links = re.findall(r'https?://[^\s<>"]+', clean_input)
        if not raw_links:
            surl = extract_surl(clean_input)
            if surl:
                raw_links = [f"https://1024terabox.com/s/1{surl}"]

        for href in raw_links:
            clean_surl = extract_surl(href) or extract_youtube_id(href) or extract_flare_id(href)
            is_video = bool(clean_surl) or any(k in href.lower() for k in ['terabox', 'terashare', 'mirrobox', 'nephobox', '4funbox', 'youtube', 'youtu.be', 'flare'])
            if is_video:
                lines = [l.strip() for l in clean_input.split('\n') if l.strip() and not l.startswith('http')]
                title = lines[0] if lines else f"Imported Video ({clean_surl or 'TeraBox'})"

                thumbnail = ""
                try:
                    info = resolve_universal_stream(href)
                    if info.get("success") and info.get("title") and not info.get("title").startswith("["):
                        title = info["title"]
                        thumbnail = info.get("thumbnail") or ""
                except Exception:
                    pass

                found_videos.append({
                    "title": title,
                    "video_url": href,
                    "thumbnail_url": thumbnail,
                    "surl": clean_surl,
                    "source_name": "Direct Import"
                })

    if not found_videos:
        return {"success": False, "error": "Could not find any playable video or TeraBox links in the provided post."}

    # Insert into database at the very top (highest ID)
    db_type, conn = get_db_connection()
    inserted_records = []
    with conn:
        cursor = conn.cursor()
        for v in found_videos:
            surl = v.get("surl") or extract_surl(v["video_url"])
            source_name = v.get("source_name", "Imported")
            title = v["title"]
            v_url = v["video_url"]
            thumb = v.get("thumbnail_url", "")

            if db_type == "postgres":
                cursor.execute('''
                    INSERT INTO feed_videos (source_id, source_name, title, video_url, thumbnail_url, surl)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (video_url) DO UPDATE SET title = EXCLUDED.title, thumbnail_url = EXCLUDED.thumbnail_url
                    RETURNING id, source_name, title, video_url, thumbnail_url, surl, discovered_at
                ''', (100, source_name, title, v_url, thumb, surl))
                row = cursor.fetchone()
                inserted_records.append({
                    "id": row[0],
                    "source_name": row[1],
                    "title": row[2],
                    "video_url": row[3],
                    "thumbnail_url": row[4],
                    "surl": row[5],
                    "discovered_at": str(row[6])
                })
            else:
                cursor.execute('''
                    INSERT OR REPLACE INTO feed_videos (source_id, source_name, title, video_url, thumbnail_url, surl)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (100, source_name, title, v_url, thumb, surl))
                vid_id = cursor.lastrowid
                inserted_records.append({
                    "id": vid_id,
                    "source_name": source_name,
                    "title": title,
                    "video_url": v_url,
                    "thumbnail_url": thumb,
                    "surl": surl,
                    "discovered_at": "Just now"
                })
        conn.commit()
    conn.close()

    return {
        "success": True,
        "message": f"Successfully imported {len(inserted_records)} video(s) to top of feed!",
        "videos": inserted_records
    }



def sync_feed_sources():
    """Fetches all active sources, scrapes their latest videos, and inserts new videos into feed_videos."""
    try:
        db_type, conn = get_db_connection()
        sources = []
        if db_type == "postgres" and HAS_PSYCOPG2:
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute("SELECT id, name, url FROM feed_sources WHERE is_active = TRUE ORDER BY id ASC")
            sources = cursor.fetchall()
        else:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT id, name, url FROM feed_sources WHERE is_active = 1 ORDER BY id ASC")
            sources = [dict(r) for r in cursor.fetchall()]
        conn.close()

        total_new_videos = 0

        for src in sources:
            source_id = src['id']
            source_name = src['name']
            source_url = src['url']

            items = scrape_feed_source(source_url)
            if not items:
                continue

            db_type, conn = get_db_connection()
            with conn:
                cursor = conn.cursor()
                if db_type == "postgres":
                    cursor.execute("SELECT video_url FROM feed_videos WHERE source_id = %s", (source_id,))
                else:
                    cursor.execute("SELECT video_url FROM feed_videos WHERE source_id = ?", (source_id,))
                existing_urls = set(row[0] for row in cursor.fetchall())

                new_items = [it for it in items if it['video_url'] not in existing_urls]

                # Insert in reverse order so top item on source page (the newest post)
                # is inserted last, getting the highest auto-increment ID and latest timestamp.
                for it in reversed(new_items):
                    clean_surl = it.get('surl') or extract_surl(it['video_url']) or extract_youtube_id(it['video_url']) or extract_flare_id(it['video_url'])
                    if db_type == "postgres":
                        cursor.execute('''
                            INSERT INTO feed_videos (source_id, source_name, title, video_url, thumbnail_url, surl)
                            VALUES (%s, %s, %s, %s, %s, %s)
                            ON CONFLICT (video_url) DO NOTHING
                        ''', (source_id, source_name, it['title'], it['video_url'], it['thumbnail_url'], clean_surl))
                    else:
                        cursor.execute('''
                            INSERT OR IGNORE INTO feed_videos (source_id, source_name, title, video_url, thumbnail_url, surl)
                            VALUES (?, ?, ?, ?, ?, ?)
                        ''', (source_id, source_name, it['title'], it['video_url'], it['thumbnail_url'], clean_surl))
                    total_new_videos += 1

                if db_type == "postgres":
                    cursor.execute("UPDATE feed_sources SET last_synced_at = CURRENT_TIMESTAMP WHERE id = %s", (source_id,))
                else:
                    cursor.execute("UPDATE feed_sources SET last_synced_at = datetime('now') WHERE id = ?", (source_id,))
                conn.commit()
            conn.close()

        return total_new_videos
    except Exception as e:
        print("sync_feed_sources error:", e)
        return 0


def _initial_feed_sync():
    """Initializes feed sync asynchronously in background."""
    time.sleep(2)
    try:
        sync_feed_sources()
    except Exception as e:
        print("Initial feed sync error:", e)

threading.Thread(target=_initial_feed_sync, daemon=True).start()


def _start_telegram_listener():
    """Starts background Telethon listener for Telegram private group if configured."""
    config_file = os.path.join(os.path.dirname(__file__), "telegram_config.json")
    session_file = os.path.join(os.path.dirname(__file__), "telegram_session.session")
    if not os.path.exists(config_file) or not os.path.exists(session_file):
        return

    try:
        with open(config_file, "r", encoding="utf-8") as f:
            cfg = json.load(f)

        if not cfg.get("active"):
            return

        api_id = cfg.get("api_id")
        api_hash = cfg.get("api_hash")
        target_group_id = cfg.get("target_group_id")
        group_title = cfg.get("target_group_title", "Private Group")

        from telethon import TelegramClient, events
        import asyncio

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        client = TelegramClient(os.path.join(os.path.dirname(__file__), "telegram_session"), api_id, api_hash, loop=loop)

        @client.on(events.NewMessage(chats=target_group_id))
        async def handler(event):
            text = event.raw_text or ""
            links = re.findall(r'https?://[^\s]+', text)
            for link in links:
                clean_surl = extract_surl(link)
                is_video = bool(clean_surl) or any(k in link.lower() for k in ['terabox', 'terashare', 'mirrobox', 'nephobox', '4funbox'])
                if is_video:
                    lines = [l.strip() for l in text.split('\n') if l.strip() and not l.startswith('http')]
                    title = lines[0] if lines else f"Telegram Video ({group_title})"
                    db_type, conn = get_db_connection()
                    with conn:
                        cursor = conn.cursor()
                        if db_type == "postgres":
                            cursor.execute('''
                                INSERT INTO feed_videos (source_id, source_name, title, video_url, thumbnail_url, surl)
                                VALUES (%s, %s, %s, %s, %s, %s)
                                ON CONFLICT (video_url) DO NOTHING
                            ''', (target_group_id, f"Telegram: {group_title}", title[:200], link, "", clean_surl))
                        else:
                            cursor.execute('''
                                INSERT OR IGNORE INTO feed_videos (source_id, source_name, title, video_url, thumbnail_url, surl)
                                VALUES (?, ?, ?, ?, ?, ?)
                            ''', (target_group_id, f"Telegram: {group_title}", title[:200], link, "", clean_surl))
                        conn.commit()
                    conn.close()

        loop.run_until_complete(client.start())
        client.run_until_disconnected()
    except Exception as e:
        print("Telegram background listener error:", e)

threading.Thread(target=_start_telegram_listener, daemon=True).start()



# ----------------- ADMIN PANEL ROUTES -----------------

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if session.get('is_admin'):
        return redirect(url_for('admin_dashboard'))

    error = None
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session['is_admin'] = True
            return redirect(url_for('admin_dashboard'))
        else:
            error = "Invalid admin username or password."

    return render_template('admin_login.html', error=error)


@app.route('/admin/logout')
def admin_logout():
    session.pop('is_admin', None)
    return redirect(url_for('admin_login'))


@app.route('/admin')
def admin_dashboard():
    if not session.get('is_admin'):
        return redirect(url_for('admin_login'))

    logs = []
    feed_videos = []
    feed_sources = []
    stats = {
        "total_searches": 0,
        "unique_links": 0,
        "today_searches": 0,
        "unique_ips": 0,
        "total_feed_videos": 0,
        "active_sources": 0,
        "db_type": "SQLite (Local File)"
    }

    try:
        db_type, conn = get_db_connection()
        stats["db_type"] = "Cloud PostgreSQL (Lifetime Persistent)" if db_type == "postgres" else "SQLite (Local File)"

        if db_type == "postgres" and HAS_PSYCOPG2:
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute('SELECT COUNT(*) AS count FROM search_logs')
            stats["total_searches"] = cursor.fetchone()['count']

            cursor.execute('SELECT COUNT(DISTINCT surl) AS count FROM search_logs')
            stats["unique_links"] = cursor.fetchone()['count']

            cursor.execute("SELECT COUNT(*) AS count FROM search_logs WHERE created_at >= NOW() - INTERVAL '1 day'")
            stats["today_searches"] = cursor.fetchone()['count']

            cursor.execute('SELECT COUNT(DISTINCT user_ip) AS count FROM search_logs')
            stats["unique_ips"] = cursor.fetchone()['count']

            cursor.execute('SELECT COUNT(*) AS count FROM feed_videos')
            stats["total_feed_videos"] = cursor.fetchone()['count']

            cursor.execute('SELECT COUNT(*) AS count FROM feed_sources WHERE is_active = TRUE')
            stats["active_sources"] = cursor.fetchone()['count']

            cursor.execute('SELECT * FROM search_logs ORDER BY id DESC LIMIT 500')
            logs = cursor.fetchall()

            cursor.execute('SELECT * FROM feed_videos ORDER BY id DESC LIMIT 500')
            feed_videos = cursor.fetchall()

            cursor.execute('''
                SELECT s.*, (SELECT COUNT(*) FROM feed_videos v WHERE v.source_id = s.id) AS video_count
                FROM feed_sources s ORDER BY s.id ASC
            ''')
            feed_sources = cursor.fetchall()
        else:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute('SELECT COUNT(*) FROM search_logs')
            stats["total_searches"] = cursor.fetchone()[0]

            cursor.execute('SELECT COUNT(DISTINCT surl) FROM search_logs')
            stats["unique_links"] = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM search_logs WHERE created_at >= datetime('now', '-1 day')")
            stats["today_searches"] = cursor.fetchone()[0]

            cursor.execute('SELECT COUNT(DISTINCT user_ip) FROM search_logs')
            stats["unique_ips"] = cursor.fetchone()[0]

            cursor.execute('SELECT COUNT(*) FROM feed_videos')
            stats["total_feed_videos"] = cursor.fetchone()[0]

            cursor.execute('SELECT COUNT(*) FROM feed_sources WHERE is_active = 1')
            stats["active_sources"] = cursor.fetchone()[0]

            cursor.execute('SELECT * FROM search_logs ORDER BY id DESC LIMIT 500')
            logs = [dict(row) for row in cursor.fetchall()]

            cursor.execute('SELECT * FROM feed_videos ORDER BY id DESC LIMIT 500')
            feed_videos = [dict(row) for row in cursor.fetchall()]

            cursor.execute('''
                SELECT s.*, (SELECT COUNT(*) FROM feed_videos v WHERE v.source_id = s.id) AS video_count
                FROM feed_sources s ORDER BY s.id ASC
            ''')
            feed_sources = [dict(row) for row in cursor.fetchall()]

        conn.close()
    except Exception as e:
        print("Admin fetch error:", e)

    return render_template('admin.html', logs=logs, stats=stats, feed_videos=feed_videos, feed_sources=feed_sources)


@app.route('/admin/api/sources/sync', methods=['POST'])
def admin_api_sync_sources():
    if not session.get('is_admin'):
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    try:
        new_count = sync_feed_sources()
        return jsonify({"success": True, "new_count": new_count, "message": f"Sync completed! {new_count} new video(s) added."})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/admin/api/sources', methods=['GET'])
def admin_api_get_sources():
    if not session.get('is_admin'):
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    try:
        db_type, conn = get_db_connection()
        if db_type == "postgres" and HAS_PSYCOPG2:
            cursor = conn.cursor(cursor_factory=RealDictCursor)
        else:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
        cursor.execute('''
            SELECT s.*, (SELECT COUNT(*) FROM feed_videos v WHERE v.source_id = s.id) AS video_count
            FROM feed_sources s ORDER BY s.id ASC
        ''')
        rows = [dict(r) for r in cursor.fetchall()]
        conn.close()
        return jsonify({"success": True, "sources": rows})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/admin/api/sources/add', methods=['POST'])
def admin_api_add_source():
    if not session.get('is_admin'):
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or request.form or {}
    url = (data.get('url') or '').strip()
    name = (data.get('name') or '').strip()
    if not url:
        return jsonify({"success": False, "error": "Source URL is required."}), 400
    if not name:
        name = urlparse(url).netloc or "New Source"

    try:
        db_type, conn = get_db_connection()
        with conn:
            cursor = conn.cursor()
            if db_type == "postgres":
                cursor.execute('''
                    INSERT INTO feed_sources (name, url, is_active)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (url) DO UPDATE SET is_active = TRUE
                ''', (name, url, True))
            else:
                cursor.execute('''
                    INSERT OR REPLACE INTO feed_sources (name, url, is_active)
                    VALUES (?, ?, ?)
                ''', (name, url, 1))
            conn.commit()
        conn.close()

        # Run background sync for the new source
        threading.Thread(target=sync_feed_sources, daemon=True).start()
        return jsonify({"success": True, "message": f"Source '{name}' added successfully! Fetching videos in background."})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/admin/api/sources/toggle/<int:source_id>', methods=['POST'])
def admin_api_toggle_source(source_id):
    if not session.get('is_admin'):
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    try:
        db_type, conn = get_db_connection()
        with conn:
            cursor = conn.cursor()
            if db_type == "postgres":
                cursor.execute("UPDATE feed_sources SET is_active = NOT is_active WHERE id = %s", (source_id,))
            else:
                cursor.execute("UPDATE feed_sources SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END WHERE id = ?", (source_id,))
            conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/admin/api/sources/delete/<int:source_id>', methods=['POST'])
def admin_api_delete_source(source_id):
    if not session.get('is_admin'):
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    try:
        db_type, conn = get_db_connection()
        with conn:
            cursor = conn.cursor()
            if db_type == "postgres":
                cursor.execute("DELETE FROM feed_sources WHERE id = %s", (source_id,))
                cursor.execute("DELETE FROM feed_videos WHERE source_id = %s", (source_id,))
            else:
                cursor.execute("DELETE FROM feed_sources WHERE id = ?", (source_id,))
                cursor.execute("DELETE FROM feed_videos WHERE source_id = ?", (source_id,))
            conn.commit()
        conn.close()
        return jsonify({"success": True, "message": "Source and its videos removed."})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/admin/api/feed/delete/<int:video_id>', methods=['POST'])
def admin_api_delete_feed_video(video_id):
    if not session.get('is_admin'):
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    try:
        db_type, conn = get_db_connection()
        with conn:
            cursor = conn.cursor()
            if db_type == "postgres":
                cursor.execute("DELETE FROM feed_videos WHERE id = %s", (video_id,))
            else:
                cursor.execute("DELETE FROM feed_videos WHERE id = ?", (video_id,))
            conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/admin/api/feed/clear', methods=['POST'])
def admin_api_clear_feed():
    if not session.get('is_admin'):
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    try:
        db_type, conn = get_db_connection()
        with conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM feed_videos")
            conn.commit()
        conn.close()
        return jsonify({"success": True, "message": "Feed cleared."})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/admin/api/feed/import_link', methods=['POST'])
def admin_api_import_link():
    if not session.get('is_admin'):
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    data = request.get_json(silent=True) or request.form or {}
    raw_input = (data.get('link') or data.get('url') or data.get('text') or '').strip()
    if not raw_input:
        return jsonify({"success": False, "error": "No link or post text provided."}), 400

    result = import_single_link(raw_input)
    status_code = 200 if result.get("success") else 400
    return jsonify(result), status_code




@app.route('/admin/export/csv')
def admin_export_csv():
    if not session.get('is_admin'):
        return redirect(url_for('admin_login'))

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID', 'Timestamp', 'Video Title', 'Size', 'Searched URL', 'SURL', 'Stream URL', 'Download URL', 'User IP', 'User Agent'])

    try:
        db_type, conn = get_db_connection()
        if db_type == "postgres" and HAS_PSYCOPG2:
            cursor = conn.cursor(cursor_factory=RealDictCursor)
        else:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

        cursor.execute('SELECT * FROM search_logs ORDER BY id DESC')
        for row in cursor.fetchall():
            writer.writerow([
                row['id'], str(row['created_at']), row['video_title'], row['video_size'],
                row['searched_url'], row['surl'], row['stream_url'], row['download_url'],
                row['user_ip'], row['user_agent']
            ])
        conn.close()
    except Exception as e:
        print("CSV export error:", e)

    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=terastream_user_searches.csv"}
    )


@app.route('/admin/delete/<int:log_id>')
def admin_delete(log_id):
    if not session.get('is_admin'):
        return redirect(url_for('admin_login'))

    try:
        db_type, conn = get_db_connection()
        with conn:
            cursor = conn.cursor()
            if db_type == "postgres":
                cursor.execute('DELETE FROM search_logs WHERE id = %s', (log_id,))
            else:
                cursor.execute('DELETE FROM search_logs WHERE id = ?', (log_id,))
            conn.commit()
        conn.close()
    except Exception as e:
        print("Delete error:", e)

    return redirect(url_for('admin_dashboard'))


@app.route('/admin/clear')
def admin_clear():
    if not session.get('is_admin'):
        return redirect(url_for('admin_login'))

    try:
        db_type, conn = get_db_connection()
        with conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM search_logs')
            conn.commit()
        conn.close()
    except Exception as e:
        print("Clear error:", e)

    return redirect(url_for('admin_dashboard'))


@app.after_request
def add_performance_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    if request.path.startswith('/static/'):
        response.headers['Cache-Control'] = 'public, max-age=86400, immutable'
    elif request.path == '/' or request.path.startswith('/play/'):
        response.headers['Cache-Control'] = 'public, max-age=60'
    return response


if __name__ == '__main__':
    import sys
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    print("==================================================")
    print(f"TeraStream Pro running at: http://127.0.0.1:{PORT}")
    print(f"Admin Panel: http://127.0.0.1:{PORT}/admin")
    print("==================================================")
    app.run(host='127.0.0.1', port=PORT, debug=False, threaded=True)
