import os
import re
import json
import time
import requests
from urllib.parse import urlparse, parse_qs
from flask import Flask, request, jsonify, render_template, redirect, url_for, Response

app = Flask(__name__, template_folder="templates")
PORT = int(os.environ.get("PORT", 8080))


# Global session cache for high-speed resolution
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

    session = requests.Session()
    session.headers.update({
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
        r_home = session.get('https://flowvideoplayer.com', timeout=8)
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

        r_init = session.post('https://flowvideoplayer.com/device/init', json=device_data, headers=ajax_headers, timeout=8)
        if r_init.status_code == 200:
            FLOW_SESSION["session"] = session
            FLOW_SESSION["csrf_token"] = csrf_token
            FLOW_SESSION["last_init"] = now
            return session, csrf_token
    except Exception as e:
        print("Session init err:", e)

    return None, None


def extract_surl(raw_input: str) -> str:
    """Extracts the clean shorturl (surl) from any TeraBox link format."""
    if not raw_input:
        return ""
    clean_input = raw_input.strip()
    
    if "?" in clean_input:
        try:
            parsed = urlparse(clean_input)
            qs = parse_qs(parsed.query)
            if "surl" in qs and qs["surl"]:
                surl = qs["surl"][0]
                return surl[1:] if surl.startswith("1") else surl
            if "shorturl" in qs and qs["shorturl"]:
                surl = qs["shorturl"][0]
                return surl[1:] if surl.startswith("1") else surl
        except Exception:
            pass

    match = re.search(r'/s/(?:1)?([a-zA-Z0-9_-]+)', clean_input)
    if match:
        return match.group(1)
    
    match_embed = re.search(r'surl=(?:1)?([a-zA-Z0-9_-]+)', clean_input)
    if match_embed:
        return match_embed.group(1)

    if re.match(r'^[1]?[a-zA-Z0-9_-]{10,40}$', clean_input):
        return clean_input[1:] if clean_input.startswith("1") else clean_input

    return ""


def get_mirrors(surl: str) -> list:
    clean_surl = surl[1:] if surl.startswith("1") else surl
    full_surl = f"1{clean_surl}"
    return [
        {"id": "native-hls", "name": "Native Ultra-Fast HLS Engine (Default)", "url": ""},
        {"id": "terabox-app", "name": "Mirror 1: TeraBox App", "url": f"https://www.terabox.app/sharing/embed?surl={full_surl}"},
        {"id": "1024tera", "name": "Mirror 2: 1024Tera CDN", "url": f"https://www.1024tera.com/sharing/embed?surl={full_surl}"},
        {"id": "teraboxshare", "name": "Mirror 3: TeraBox Share", "url": f"https://teraboxshare.com/sharing/embed?surl={full_surl}"},
        {"id": "nephobox", "name": "Mirror 4: NephoBox Fast", "url": f"https://www.nephobox.com/sharing/embed?surl={full_surl}"},
        {"id": "freeterabox", "name": "Mirror 5: FreeTeraBox", "url": f"https://www.freeterabox.com/sharing/embed?surl={full_surl}"}
    ]


def resolve_terabox_stream(raw_url_or_surl: str) -> dict:
    """
    Extracts direct HLS stream (.m3u8), download link, thumbnails, and metadata.
    """
    clean_surl = extract_surl(raw_url_or_surl)
    if not clean_surl:
        return {"success": False, "error": "Please enter a valid TeraBox share link."}

    full_surl = f"1{clean_surl}"
    target_link = f"https://teraboxshare.com/s/{full_surl}"

    # Default fallback data
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
        "mirrors": get_mirrors(clean_surl),
        "playlist": []
    }

    # Attempt direct high-speed HLS extraction
    session, csrf_token = get_flow_session()
    if session and csrf_token:
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

            resp = session.post('https://flowvideoplayer.com/search/video', json={'url': target_link}, headers=ajax_headers, timeout=10)
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

                    # Format playlist for multi-video folders
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
        except Exception as e:
            print("Direct HLS extraction exception:", e)

    return result


@app.route('/')
def index():
    query_url = request.args.get('url') or request.args.get('surl')
    if query_url:
        info = resolve_terabox_stream(query_url)
        return render_template(
            "index.html",
            initial_data=info,
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
        initial_data=info,
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
    return jsonify(stream_info)


@app.route('/api/health')
def health():
    return jsonify({"status": "ok", "service": "TeraStream Pro Native", "port": PORT})


if __name__ == '__main__':
    import sys
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    print("==================================================")
    print(f"TeraStream Pro running at: http://127.0.0.1:{PORT}")
    print("==================================================")
    app.run(host='127.0.0.1', port=PORT, debug=False, threaded=True)
