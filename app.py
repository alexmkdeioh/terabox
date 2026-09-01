import os
import re
import csv
import io
import json
import time
import sqlite3
import requests
from urllib.parse import urlparse, parse_qs
from flask import Flask, request, jsonify, render_template, redirect, url_for, session, Response

app = Flask(__name__, template_folder="templates")
app.secret_key = os.environ.get("SECRET_KEY", "terastream_secure_session_key_2026")
PORT = int(os.environ.get("PORT", 8080))

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "mahabir")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "mk@123")
DB_FILE = os.path.join(os.path.dirname(__file__), "terastream.db")

# ----------------- DATABASE INITIALIZATION -----------------

def init_db():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
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
            conn.commit()
    except Exception as e:
        print("DB Init Error:", e)

init_db()


def log_search(searched_url, surl, video_title, video_size, stream_url, download_url, user_ip, user_agent):
    """Logs user searching query to SQLite database."""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO search_logs (searched_url, surl, video_title, video_size, stream_url, download_url, user_ip, user_agent)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (searched_url, surl, video_title, video_size, stream_url, download_url, user_ip, user_agent))
            conn.commit()
    except Exception as e:
        print("Log search error:", e)


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


def resolve_terabox_stream(raw_url_or_surl: str) -> dict:
    """Extracts direct HLS stream (.m3u8), download link, thumbnails, and metadata for any TeraBox link."""
    clean_surl = extract_surl(raw_url_or_surl)
    if not clean_surl:
        return {"success": False, "error": "Please enter a valid TeraBox share link."}

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

            # Build smart candidate links (including user's original URL and all share patterns)
            clean_raw = raw_url_or_surl.strip().replace(" ", "")
            candidate_links = []
            if clean_raw.startswith("http"):
                candidate_links.append(clean_raw)

            candidate_links.extend([
                f"https://www.terabox.app/wap/share/filelist?surl={clean_surl}",
                f"https://1024tera.com/s/1{clean_surl}",
                f"https://teraboxshare.com/s/1{clean_surl}",
                f"https://terabox.app/s/1{clean_surl}",
                f"https://terasharefile.com/s/1{clean_surl}",
                f"https://1024tera.com/s/{clean_surl}",
                f"https://terabox.app/s/{clean_surl}",
                f"https://www.terabox.com/sharing/link?surl={clean_surl}"
            ])

            seen = set()
            unique_candidates = [x for x in candidate_links if not (x in seen or seen.add(x))]

            for target_link in unique_candidates:
                try:
                    resp = session_obj.post('https://flowvideoplayer.com/search/video', json={'url': target_link}, headers=ajax_headers, timeout=10)
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
                            result["is_hls"] = bool(result["stream_url"])

                            playlist = []
                            for idx, v in enumerate(video_list):
                                playlist.append({
                                    "index": idx,
                                    "title": v.get("file_name") or f"Part {idx+1}",
                                    "size": v.get("file_size") or "",
                                    "thumbnail": v.get("thumbnail"),
                                    "stream_url": v.get("fast_stream_url"),
                                    "download_url": v.get("download_url")
                                })
                            result["playlist"] = playlist
                            return result
                except Exception as inner_e:
                    continue
        except Exception as e:
            print("Direct HLS extraction error:", e)

    return result


# ----------------- USER PUBLIC ROUTES -----------------

@app.route('/')
def index():
    query_url = request.args.get('url') or request.args.get('surl')
    if query_url:
        info = resolve_terabox_stream(query_url)
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
    clean_surl = extract_surl(surl)
    if not clean_surl:
        return redirect(url_for('index'))
    
    info = resolve_terabox_stream(clean_surl)
    return render_template(
        "index.html",
        initial_data=info if info.get("success") else None,
        initial_url=f"https://teraboxshare.com/s/1{clean_surl}"
    )


@app.route('/api/resolve', methods=['GET', 'POST'])
def api_resolve():
    raw_input = ""
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        raw_input = data.get('url') or data.get('surl') or ""
    else:
        raw_input = request.args.get('url') or request.args.get('surl') or ""

    if not raw_input:
        return jsonify({"success": False, "error": "No TeraBox link provided."}), 400

    surl = extract_surl(raw_input)
    if not surl:
        return jsonify({
            "success": False, 
            "error": "Invalid TeraBox link. Please provide a valid URL like https://teraboxshare.com/s/..."
        }), 400

    stream_info = resolve_terabox_stream(raw_input)

    # Log user search activity
    if stream_info.get("success"):
        user_ip = request.headers.get('X-Forwarded-For', request.remote_addr or '127.0.0.1').split(',')[0].strip()
        user_agent = request.headers.get('User-Agent', 'Unknown')
        log_search(
            searched_url=raw_input,
            surl=stream_info.get("surl", ""),
            video_title=stream_info.get("title", ""),
            video_size=stream_info.get("size", ""),
            stream_url=stream_info.get("stream_url", ""),
            download_url=stream_info.get("download_url", ""),
            user_ip=user_ip,
            user_agent=user_agent
        )

    return jsonify(stream_info)


@app.route('/api/health')
def health():
    return jsonify({"status": "ok", "service": "TeraStream Pro Native", "port": PORT})


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
    stats = {
        "total_searches": 0,
        "unique_links": 0,
        "today_searches": 0,
        "unique_ips": 0
    }

    try:
        with sqlite3.connect(DB_FILE) as conn:
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

            cursor.execute('SELECT * FROM search_logs ORDER BY id DESC LIMIT 500')
            logs = [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        print("Admin fetch error:", e)

    return render_template('admin.html', logs=logs, stats=stats)


@app.route('/admin/export/csv')
def admin_export_csv():
    if not session.get('is_admin'):
        return redirect(url_for('admin_login'))

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID', 'Timestamp', 'Video Title', 'Size', 'Searched URL', 'SURL', 'Stream URL', 'Download URL', 'User IP', 'User Agent'])

    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM search_logs ORDER BY id DESC')
            for row in cursor.fetchall():
                writer.writerow([
                    row['id'], row['created_at'], row['video_title'], row['video_size'],
                    row['searched_url'], row['surl'], row['stream_url'], row['download_url'],
                    row['user_ip'], row['user_agent']
                ])
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
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM search_logs WHERE id = ?', (log_id,))
            conn.commit()
    except Exception as e:
        print("Delete error:", e)

    return redirect(url_for('admin_dashboard'))


@app.route('/admin/clear')
def admin_clear():
    if not session.get('is_admin'):
        return redirect(url_for('admin_login'))

    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM search_logs')
            conn.commit()
    except Exception as e:
        print("Clear error:", e)

    return redirect(url_for('admin_dashboard'))


if __name__ == '__main__':
    import sys
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    print("==================================================")
    print(f"TeraStream Pro running at: http://127.0.0.1:{PORT}")
    print(f"Admin Panel: http://127.0.0.1:{PORT}/admin")
    print("==================================================")
    app.run(host='127.0.0.1', port=PORT, debug=False, threaded=True)
