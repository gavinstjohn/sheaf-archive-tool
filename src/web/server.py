from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import subprocess
import tempfile
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Settings

log = logging.getLogger(__name__)

_THUMB_SIZE = 200          # pixels
_THUMB_CACHE_DIR = Path(tempfile.gettempdir()) / "sheaf_thumbs"


# ---------------------------------------------------------------------------
# HTML / JS for the browser GUI
# ---------------------------------------------------------------------------

_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sheaf</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,sans-serif;background:#f2f2f2;color:#1a1a1a}
header{position:fixed;top:0;left:0;right:0;background:#fff;border-bottom:1px solid #ddd;
  padding:10px 16px;z-index:100;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
header h1{font-size:1rem;font-weight:700;letter-spacing:.05em;color:#333;margin-right:4px}
input[type=search],select,input[type=date]{padding:5px 8px;border:1px solid #ccc;
  border-radius:4px;font-size:.85rem;outline:none}
input[type=search]{min-width:180px}
input[type=search]:focus,select:focus,input[type=date]:focus{border-color:#555}
.ctrl-label{font-size:.75rem;color:#888;display:flex;gap:4px;align-items:center}
input[type=range]{width:80px}
#status{font-size:.75rem;color:#888;margin-left:auto;white-space:nowrap}
main{padding:68px 12px 12px}
#grid{display:grid;
  grid-template-columns:repeat(auto-fill,minmax(var(--tsz,160px),1fr));gap:6px}
.card{background:#ddd;border-radius:6px;overflow:hidden;cursor:pointer;
  aspect-ratio:1;position:relative;transition:opacity .15s}
.card:hover{opacity:.85}
.card img{width:100%;height:100%;object-fit:cover;display:block}
.card .fallback{width:100%;height:100%;display:flex;flex-direction:column;
  align-items:center;justify-content:center;background:#e4e4e4;
  color:#888;font-size:.65rem;text-align:center;padding:8px;gap:2px}
.card .lbl{position:absolute;bottom:0;left:0;right:0;
  background:rgba(0,0,0,.5);color:#fff;font-size:.6rem;
  padding:2px 4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.88);
  z-index:200;cursor:zoom-out}
#lightbox{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);
  cursor:default;text-align:center;max-width:92vw}
#lightbox img,#lightbox video{max-width:90vw;max-height:80vh;
  object-fit:contain;display:block;border-radius:4px}
#lightbox video{background:#000}
#lbox-meta{color:#ccc;font-size:.75rem;margin-top:6px;line-height:1.5}
#close-btn{position:fixed;top:14px;right:18px;color:#fff;font-size:1.4rem;
  cursor:pointer;background:none;border:none;z-index:201}
#empty{display:none;text-align:center;padding:60px;color:#888;font-size:.9rem}
</style>
</head>
<body>
<header>
  <h1>SHEAF</h1>
  <input type="search" id="q" placeholder="Search…" oninput="debounce()">
  <select id="ft" onchange="load()"><option value="">All types</option></select>
  <input type="date" id="ds" onchange="load()" title="From date">
  <span class="ctrl-label">–</span>
  <input type="date" id="de" onchange="load()" title="To date">
  <label class="ctrl-label">Size
    <input type="range" id="tsz" min="80" max="320" value="160" oninput="resize()">
  </label>
  <span id="status">—</span>
</header>
<main>
  <div id="grid"></div>
  <div id="empty">No files found.</div>
</main>

<div id="overlay" onclick="closeLB()">
  <button id="close-btn" onclick="closeLB()">&#x2715;</button>
  <div id="lightbox" onclick="event.stopPropagation()">
    <div id="lbox-media"></div>
    <div id="lbox-meta"></div>
  </div>
</div>

<script>
var files = [], timer = null;
var VIDEO_EXT = new Set(['mp4','mov','avi','mkv','webm','m4v','mpg','mpeg']);

function resize(){
  document.documentElement.style.setProperty('--tsz', id('tsz').value + 'px');
}
function id(x){ return document.getElementById(x); }
function debounce(){ clearTimeout(timer); timer = setTimeout(load, 280); }

async function loadTypes(){
  try {
    var r = await fetch('/api/types');
    var d = await r.json();
    var sel = id('ft');
    d.types.forEach(function(t){
      var o = document.createElement('option'); o.value = o.textContent = t;
      sel.appendChild(o);
    });
  } catch(e){}
}

async function load(){
  try {
    var p = new URLSearchParams();
    var q = id('q').value, ft = id('ft').value;
    var ds = id('ds').value, de = id('de').value;
    if(q)  p.set('q', q);
    if(ft) p.set('type', ft);
    if(ds) p.set('date_start', ds);
    if(de) p.set('date_end', de);
    var r = await fetch('/api/search?' + p);
    var d = await r.json();
    files = d.files || [];
    render(files);
    id('status').textContent = files.length + (files.length === 1 ? ' file' : ' files');
  } catch(e){ id('status').textContent = 'error: ' + e.message; console.error(e); }
}

function enc(s){ return encodeURIComponent(s); }
function ext(p){ var parts = p.split('.'); return parts.length > 1 ? parts.pop().toLowerCase() : ''; }
function fname(p){ return p.split('/').pop(); }

function render(files){
  var grid = id('grid'), empty = id('empty');
  grid.innerHTML = '';
  empty.style.display = files.length ? 'none' : 'block';
  files.forEach(function(f, i){
    var card = document.createElement('div');
    card.className = 'card';
    card.onclick = function(){ openLB(i); };

    var img = document.createElement('img');
    img.src = '/thumb/' + enc(f.file_path);
    img.loading = 'lazy';

    var fb = document.createElement('div');
    fb.className = 'fallback';
    fb.style.display = 'none';
    var fb1 = document.createElement('div');
    fb1.textContent = f.file_type || ext(f.file_path);
    var fb2 = document.createElement('div');
    fb2.textContent = f.capture_date || '';
    fb.appendChild(fb1);
    fb.appendChild(fb2);

    img.onerror = function(){ img.style.display = 'none'; fb.style.display = 'flex'; };

    var lbl = document.createElement('div');
    lbl.className = 'lbl';
    lbl.textContent = fname(f.file_path);

    card.appendChild(img);
    card.appendChild(fb);
    card.appendChild(lbl);
    grid.appendChild(card);
  });
}

function openLB(i){
  var f = files[i];
  var mediaUrl = '/media/' + enc(f.file_path);
  var e = ext(f.file_path);
  var media;
  if(VIDEO_EXT.has(e)){
    media = '<video controls autoplay><source src="' + mediaUrl + '"></video>';
  } else {
    media = '<img src="' + mediaUrl + '" alt="' + fname(f.file_path) + '">';
  }
  id('lbox-media').innerHTML = media;
  id('lbox-meta').textContent =
    fname(f.file_path) + '  \u00b7  ' + (f.capture_date || '') +
    (f.file_type ? '  \u00b7  ' + f.file_type : '');
  id('overlay').style.display = 'block';
}

function closeLB(){
  id('overlay').style.display = 'none';
  id('lbox-media').innerHTML = '';
}

document.addEventListener('keydown', function(e){ if(e.key === 'Escape') closeLB(); });
resize();
loadTypes();
load();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class SheafHandler(BaseHTTPRequestHandler):
    settings: "Settings"   # set on the class before creating the server

    def log_message(self, fmt, *args):  # suppress default access log
        log.debug(fmt, *args)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=False)

        try:
            if path == "/" or path == "/index.html":
                self._serve_html()
            elif path == "/api/types":
                self._api_types()
            elif path == "/api/search":
                self._api_search(qs)
            elif path.startswith("/media/"):
                self._serve_media(path[len("/media/"):])
            elif path.startswith("/thumb/"):
                self._serve_thumb(path[len("/thumb/"):])
            else:
                self._not_found()
        except Exception as e:
            log.error("Handler error for %s: %s", path, e)
            self._error(500, str(e))

    # ------------------------------------------------------------------

    def _serve_html(self):
        body = _HTML.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _api_types(self):
        from ..db.schema import open_db
        from ..db.queries import list_file_types
        conn = open_db(self.settings.db_path)
        types = list_file_types(conn)
        conn.close()
        self._json({"types": types})

    def _api_search(self, qs):
        from ..db.schema import open_db
        from ..db.queries import search_files

        q = qs.get("q", [None])[0]
        file_type = qs.get("type", [None])[0]
        date_start = qs.get("date_start", [None])[0]
        date_end = qs.get("date_end", [None])[0]
        limit = int(qs.get("limit", ["500"])[0])
        offset = int(qs.get("offset", ["0"])[0])

        conn = open_db(self.settings.db_path)
        rows = search_files(
            conn,
            query=q,
            file_type=file_type,
            date_start=date_start,
            date_end=date_end,
            limit=limit,
            offset=offset,
        )
        conn.close()

        files = [
            {
                "file_path": r["file_path"],
                "capture_date": r["capture_date"],
                "file_type": r["file_type"],
                "file_hash": r["file_hash"],
            }
            for r in rows
        ]
        self._json({"files": files, "total": len(files)})

    def _serve_media(self, encoded_path: str):
        rel = urllib.parse.unquote(encoded_path)
        abs_path = self.settings.archive_root / rel
        # Security: must stay within archive root
        try:
            abs_path.resolve().relative_to(self.settings.archive_root.resolve())
        except ValueError:
            self._error(403, "Forbidden")
            return

        if not abs_path.exists():
            self._not_found()
            return

        ctype, _ = mimetypes.guess_type(str(abs_path))
        ctype = ctype or "application/octet-stream"
        data = abs_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", len(data))
        self.end_headers()
        self.wfile.write(data)

    def _serve_thumb(self, encoded_path: str):
        rel = urllib.parse.unquote(encoded_path)
        abs_path = self.settings.archive_root / rel
        try:
            abs_path.resolve().relative_to(self.settings.archive_root.resolve())
        except ValueError:
            self._error(403, "Forbidden")
            return

        if not abs_path.exists():
            self._send_svg_thumb(abs_path.suffix.lstrip(".").upper() or "?")
            return

        thumb = _get_or_create_thumb(abs_path)
        if thumb is None:
            ext_label = abs_path.suffix.lstrip(".").upper() or "?"
            self._send_svg_thumb(ext_label)
            return

        data = thumb.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", len(data))
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def _send_svg_thumb(self, label: str):
        label = label[:8]
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{_THUMB_SIZE}" height="{_THUMB_SIZE}">'
            f'<rect width="100%" height="100%" fill="#e0e0e0"/>'
            f'<text x="50%" y="50%" text-anchor="middle" dominant-baseline="middle" '
            f'font-family="system-ui" font-size="20" fill="#999">{label}</text>'
            f'</svg>'
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "image/svg+xml")
        self.send_header("Content-Length", len(svg))
        self.end_headers()
        self.wfile.write(svg)

    def _json(self, data: dict):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _not_found(self):
        self._error(404, "Not Found")

    def _error(self, code: int, msg: str):
        body = msg.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)


# ---------------------------------------------------------------------------
# Thumbnail generation
# ---------------------------------------------------------------------------

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".tiff", ".tif", ".bmp", ".heic", ".heif"}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".mpg", ".mpeg"}


def _get_or_create_thumb(src: Path) -> Path | None:
    """Return path to a cached thumbnail, generating it if needed.

    Returns None if thumbnail generation is not possible.
    """
    _THUMB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(str(src).encode()).hexdigest()
    thumb_path = _THUMB_CACHE_DIR / f"{key}.jpg"

    if thumb_path.exists():
        return thumb_path

    ext = src.suffix.lower()
    if ext in _IMAGE_EXTS:
        return _make_image_thumb(src, thumb_path)
    elif ext in _VIDEO_EXTS:
        return _make_video_thumb(src, thumb_path)
    return None


def _make_image_thumb(src: Path, dest: Path) -> Path | None:
    """Generate a thumbnail using ImageMagick (if available)."""
    sz = f"{_THUMB_SIZE}x{_THUMB_SIZE}"
    # Try magick (IMv7) first, then the legacy convert wrapper
    for cmd in [
        ["magick", str(src) + "[0]", "-thumbnail", sz + "^",
         "-gravity", "center", "-extent", sz, str(dest)],
        ["convert", str(src) + "[0]", "-thumbnail", sz + "^",
         "-gravity", "center", "-extent", sz, str(dest)],
    ]:
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=20)
            if r.returncode == 0 and dest.exists():
                return dest
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return None


def _make_video_thumb(src: Path, dest: Path) -> Path | None:
    """Extract a video frame thumbnail using ffmpeg (if available)."""
    sz = f"{_THUMB_SIZE}x{_THUMB_SIZE}"
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-vframes", "1",
             "-vf", f"scale={sz}:force_original_aspect_ratio=increase,crop={sz}",
             str(dest)],
            capture_output=True, timeout=30,
        )
        if r.returncode == 0 and dest.exists():
            return dest
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def start_server(settings: "Settings", port: int = 8765, open_browser: bool = True):
    """Start the Sheaf web server and optionally open a browser tab."""

    class _Handler(SheafHandler):
        pass

    _Handler.settings = settings

    server = HTTPServer(("127.0.0.1", port), _Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"Sheaf GUI running at {url}  (press Ctrl-C to stop)")

    if open_browser:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        server.server_close()
