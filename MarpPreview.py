"""
Sublime Text plugin: Marp Preview
Renders Marp markdown slides and shows them in a browser with live reload.
Compatible with Python 3.3 (Sublime Text's embedded runtime).

On first use, the Marp CLI standalone binary is automatically downloaded
from GitHub Releases into ~/.marp-binary/ — no Node.js or npm required.
"""

import base64
import hashlib
import json
import mimetypes
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import urllib.request
import webbrowser
import zipfile
import zlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

import sublime
import sublime_plugin

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_server = None
_server_lock = threading.Lock()

_store = {}   # {file_id: {'html': str, 'version': int, 'path': str}}
_store_lock = threading.Lock()

_DEFAULT_PORT    = 8742
_BINARY_DIR      = os.path.expanduser("~/.marp-binary")
_MERMAID_JS_URL  = "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _settings():
    return sublime.load_settings("MarpPreview.sublime-settings")


def _marp_command_setting():
    """Return the marp_command override, or None to use the auto-managed binary."""
    return _settings().get("marp_command", None) or None


def _server_port():
    return int(_settings().get("server_port", _DEFAULT_PORT))


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _is_marp_file(path):
    return bool(path) and path.endswith((".md", ".markdown", ".marp"))


def _status(msg):
    sublime.set_timeout(lambda: sublime.status_message(msg), 0)


def _error(msg):
    sublime.set_timeout(lambda: sublime.error_message(msg), 0)


def _spawn(fn, *args):
    t = threading.Thread(target=fn, args=args)
    t.daemon = True
    t.start()


def _inject_before_body(html, snippet):
    if "</body>" in html:
        return html.replace("</body>", snippet + "\n</body>", 1)
    return html + snippet


def _ensure_binary():
    """Return True if the binary is available (downloading if needed)."""
    if _marp_command_setting() or _binary_ready():
        return True
    return _download_binary()


def _popen(cmd, **kwargs):
    """Suppress console windows on Windows."""
    if sys.platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        kwargs.setdefault("startupinfo", si)
    return subprocess.Popen(cmd, **kwargs)


# ---------------------------------------------------------------------------
# Binary management
# ---------------------------------------------------------------------------

def _binary_path():
    name = "marp.exe" if sys.platform == "win32" else "marp"
    return os.path.join(_BINARY_DIR, name)


def _binary_ready():
    return os.path.isfile(_binary_path())


def _detect_asset():
    """Query GitHub API and return (download_url, filename) for the current OS/arch."""
    api_url = "https://api.github.com/repos/marp-team/marp-cli/releases/latest"
    with urllib.request.urlopen(api_url) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    machine = platform.machine().lower()
    is_arm  = machine in ("arm64", "aarch64")

    if sys.platform == "darwin":
        suffix = "-mac.tar.gz"
    elif sys.platform == "win32":
        suffix = "-win.zip"
    elif is_arm:
        suffix = "-linux-arm64.tar.gz"
    else:
        suffix = "-linux.tar.gz"

    for asset in data["assets"]:
        name = asset["name"]
        if name.endswith(suffix):
            return asset["browser_download_url"], name

    raise RuntimeError(
        "No matching asset found for this platform (suffix: {}).".format(suffix)
    )


def _download_binary():
    """Download and extract the Marp CLI binary. Returns True on success."""
    url, filename = _detect_asset()
    os.makedirs(_BINARY_DIR, exist_ok=True)
    tmp_archive = os.path.join(_BINARY_DIR, filename)
    bin_name    = "marp.exe" if sys.platform == "win32" else "marp"
    target      = _binary_path()

    try:
        _status("Marp Preview: downloading binary ({})...".format(filename))

        with urllib.request.urlopen(url) as resp, open(tmp_archive, "wb") as f:
            shutil.copyfileobj(resp, f)

        # --- Extract the binary ---
        if filename.endswith(".tar.gz"):
            with tarfile.open(tmp_archive, "r:gz") as tar:
                for member in tar.getmembers():
                    if os.path.basename(member.name) == bin_name:
                        src = tar.extractfile(member)
                        if src:
                            with open(target, "wb") as out:
                                out.write(src.read())
                        break

        elif filename.endswith(".zip"):
            with zipfile.ZipFile(tmp_archive, "r") as z:
                for entry in z.namelist():
                    if os.path.basename(entry) == bin_name:
                        with open(target, "wb") as out:
                            out.write(z.read(entry))
                        break

        if not os.path.isfile(target):
            raise RuntimeError("Binary not found in archive.")

        # Make executable on Unix
        if sys.platform != "win32":
            os.chmod(target, 0o755)

        _status("Marp Preview: binary ready.")
        return True

    except Exception as exc:
        _error("Marp Preview: failed to download binary:\n{}".format(str(exc)))
        return False

    finally:
        if os.path.exists(tmp_archive):
            os.remove(tmp_archive)


# ---------------------------------------------------------------------------
# Mermaid.js management
# ---------------------------------------------------------------------------

def _mermaid_js_path():
    return os.path.join(_BINARY_DIR, "mermaid.min.js")


def _mermaid_js_ready():
    return os.path.isfile(_mermaid_js_path())


def _download_mermaid_js():
    """Download mermaid.min.js to ~/.marp-binary/. Returns True on success."""
    os.makedirs(_BINARY_DIR, exist_ok=True)
    tmp = _mermaid_js_path() + ".tmp"
    try:
        _status("Marp Preview: downloading Mermaid.js (first run)...")
        with urllib.request.urlopen(_MERMAID_JS_URL) as resp, open(tmp, "wb") as f:
            shutil.copyfileobj(resp, f)
        os.rename(tmp, _mermaid_js_path())
        return True
    except Exception as exc:
        _error("Marp Preview: failed to download Mermaid.js:\n{}".format(str(exc)))
        return False
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _file_id(path):
    return hashlib.md5(path.encode()).hexdigest()[:12]


def _marp_base_cmd():
    setting = _marp_command_setting()
    if setting:
        return setting if isinstance(setting, list) else [setting]
    return [_binary_path()]


def _build_cmd(file_path):
    return _marp_base_cmd() + [file_path, "-o", "-", "--html", "--allow-local-files"]


def _embed_local_resources(html, base_dir):
    """Inline @import url() and url() references that marp leaves unprocessed."""

    def is_external(url):
        return url.startswith(("http://", "https://", "data:", "//", "/"))

    def to_data_uri(path):
        try:
            mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
            with open(path, "rb") as f:
                return "data:{};base64,{}".format(
                    mime, base64.b64encode(f.read()).decode("ascii")
                )
        except Exception:
            return None

    def process_css(css, css_dir):
        # @import url('...') or @import '...'
        def replace_import(m):
            url = (m.group(1) or m.group(2) or "").strip("'\" ")
            if not url or is_external(url):
                return m.group(0)
            path = os.path.normpath(os.path.join(css_dir, url))
            try:
                with open(path, "r", encoding="utf-8") as f:
                    imported = f.read()
                return process_css(imported, os.path.dirname(path))
            except Exception:
                return m.group(0)

        css = re.sub(
            r"""@import\s+(?:url\(['"]?([^'"\)\s]+)['"]?\)|['"]([^'"]+)['"])\s*;?""",
            replace_import, css
        )

        # url('...') → data URI
        def replace_url(m):
            url = m.group(1).strip("'\" ")
            if not url or is_external(url):
                return m.group(0)
            path = os.path.normpath(os.path.join(css_dir, url))
            uri = to_data_uri(path)
            return "url('{}')".format(uri) if uri else m.group(0)

        css = re.sub(r"""url\(['"]?([^'"\)\s]+)['"]?\)""", replace_url, css)
        return css

    def process_style_tag(m):
        return "<style>" + process_css(m.group(1), base_dir) + "</style>"

    html = re.sub(r"<style>(.*?)</style>", process_style_tag, html, flags=re.DOTALL)

    # Process inline style="..." attributes (e.g. background-image:url(&quot;...&quot;))
    def html_unescape(s):
        return (s.replace("&amp;", "&")
                 .replace("&quot;", '"')
                 .replace("&#39;", "'")
                 .replace("&lt;", "<")
                 .replace("&gt;", ">"))

    def process_style_attr(m):
        unescaped = html_unescape(m.group(1))
        processed = process_css(unescaped, base_dir)
        processed = processed.replace('"', "&quot;")
        return 'style="{}"'.format(processed)

    html = re.sub(r'style="([^"]*url[^"]*)"', process_style_attr, html)

    return html


def _render(file_path):
    """Run Marp and return HTML (or an error page string)."""
    cmd = _build_cmd(file_path)
    file_dir = os.path.dirname(file_path)
    try:
        proc = _popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=file_dir,
        )
        try:
            stdout_bytes, stderr_bytes = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return _error_page("Marp rendering timed out (30 s).")

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        if proc.returncode == 0 and stdout.strip():
            html = _embed_local_resources(stdout, file_dir)
            html = _inject_mermaid(html)
            return html

        err = stderr or "Exit code {}".format(proc.returncode)
        return _error_page("Marp rendering failed:\n{}".format(err))

    except FileNotFoundError:
        return _error_page('Binary not found: "{}".'.format(cmd[0]))


_MERMAID_SCRIPT = """\
<script src="/static/mermaid.min.js"></script>
<script>
(function(){
  function run() {
    mermaid.initialize({startOnLoad:false,theme:'default'});
    var blocks = document.querySelectorAll('code.language-mermaid');
    for (var i = 0; i < blocks.length; i++) {
      (function(el, idx) {
        var pre = el.parentElement;
        if (!pre || !pre.parentNode) return;
        mermaid.render('mermaid-' + idx, el.textContent || '').then(function(r) {
          var wrap = document.createElement('div');
          wrap.className = 'mermaid-diagram';
          wrap.style.cssText = 'text-align:center;margin:.5em 0;';
          wrap.innerHTML = r.svg;
          // scale down to the same limit as export
          var svg = wrap.querySelector('svg');
          if (svg) {
            svg.style.maxWidth = '900px';
            svg.style.maxHeight = '500px';
          }
          pre.parentNode.replaceChild(wrap, pre);
        }).catch(function(e){ console.warn('Mermaid:', e); });
      })(blocks[i], i);
    }
  }
  setTimeout(run, 600);
})();
</script>"""


def _inject_mermaid(html):
    """Download Mermaid.js if needed, then inject it into the HTML."""
    if "language-mermaid" not in html:
        return html
    if not _mermaid_js_ready():
        if not _download_mermaid_js():
            return html  # download failed — skip injection
    return _inject_before_body(html, _MERMAID_SCRIPT)


def _error_page(message):
    escaped = (message
               .replace("&", "&amp;")
               .replace("<", "&lt;")
               .replace(">", "&gt;"))
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<title>Marp Preview - Error</title>"
        "<style>body{{font-family:monospace;padding:2em;background:#1e1e1e;color:#f44;}}"
        "pre{{white-space:pre-wrap;word-break:break-word;}}</style></head>"
        "<body><h2>Marp Preview Error</h2><pre>{msg}</pre></body></html>"
    ).format(msg=escaped)


def _inject_reload(html, file_id, version):
    script = (
        "\n<script>\n"
        "(function(){{\n"
        "  var v={v};\n"
        "  setInterval(function(){{\n"
        "    fetch('/poll/{fid}')\n"
        "      .then(function(r){{return r.json();}})\n"
        "      .then(function(d){{if(d.v>v){{location.reload();}}}})\n"
        "      .catch(function(){{}});\n"
        "  }},1000);\n"
        "}})();\n"
        "</script>"
    ).format(v=version, fid=file_id)
    return _inject_before_body(html, script)


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parts = urlparse(self.path).path.strip("/").split("/", 1)
        if len(parts) == 2:
            action, file_id = parts

            if action == "static" and file_id == "mermaid.min.js":
                path = _mermaid_js_path()
                if os.path.isfile(path):
                    with open(path, "rb") as f:
                        body = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/javascript")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "max-age=86400")
                    self.end_headers()
                    self.wfile.write(body)
                    return

            with _store_lock:
                data = _store.get(file_id)

            if action == "preview" and data:
                body = _inject_reload(
                    data["html"], file_id, data["version"]
                ).encode("utf-8")
                self._send(200, "text/html; charset=utf-8", body)
                return

            if action == "poll" and data:
                body = json.dumps({"v": data["version"]}).encode("utf-8")
                self._send(200, "application/json", body)
                return

        self._send(404, "text/plain", b"Not Found")

    def _send(self, code, content_type, body):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


def _ensure_server():
    global _server
    with _server_lock:
        if _server is not None:
            return True
        port = _server_port()
        try:
            srv = HTTPServer(("127.0.0.1", port), _Handler)
        except OSError as exc:
            sublime.error_message(
                "Marp Preview: cannot start server on port {}:\n{}".format(port, exc)
            )
            return False
        t = threading.Thread(target=srv.serve_forever)
        t.daemon = True
        t.start()
        _server = srv
    return True


# ---------------------------------------------------------------------------
# Background workers
# ---------------------------------------------------------------------------

def _open_preview(file_path):
    """Download binary if needed, render, register in store, open browser."""
    if not _ensure_binary():
        return

    file_id = _file_id(file_path)
    html    = _render(file_path)

    with _store_lock:
        prev = _store.get(file_id, {}).get("version", 0)
        _store[file_id] = {"html": html, "version": prev + 1, "path": file_path}

    url = "http://127.0.0.1:{}/preview/{}".format(_server_port(), file_id)

    def _open():
        webbrowser.open(url)
        sublime.status_message("Marp Preview: " + url)

    sublime.set_timeout(_open, 0)


def _update_store(file_id, file_path):
    """Re-render on save (worker thread)."""
    html = _render(file_path)
    with _store_lock:
        if file_id not in _store:
            return
        _store[file_id]["html"] = html
        _store[file_id]["version"] += 1


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

class MarpPreviewCommand(sublime_plugin.TextCommand):
    """Open a live-reload Marp preview in the default browser."""

    def run(self, edit):
        file_path = self.view.file_name()
        if not file_path:
            sublime.error_message("Marp Preview: Save the file before opening a preview.")
            return
        if not _ensure_server():
            return
        _spawn(_open_preview, file_path)

    def is_enabled(self):
        return _is_marp_file(self.view.file_name() or "")


def _mermaid_to_svg(code):
    """Convert mermaid code to SVG via mermaid.ink. Returns normalized SVG bytes or None."""
    try:
        payload = json.dumps({"code": code, "mermaid": {"theme": "default"}})
        compressed = zlib.compress(payload.encode("utf-8"), 9)
        encoded = base64.urlsafe_b64encode(compressed).decode("ascii").rstrip("=")
        url = "https://mermaid.ink/svg/pako:{}".format(encoded)
        req = urllib.request.Request(url, headers={"User-Agent": "MarpPreview/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
            if b"<svg" in data:
                return _fit_svg(data)
    except Exception:
        pass
    return None


# Marp slides are 1280×720 px; maximum size accounting for padding
_SLIDE_MAX_W = 900
_SLIDE_MAX_H = 500


def _fit_svg(svg_bytes):
    """
    Scale SVG to fit within _SLIDE_MAX_W × _SLIDE_MAX_H while preserving aspect ratio
    (contain scaling). Sets explicit px width/height so <img> renders at exact size.
    """
    svg = svg_bytes.decode("utf-8")

    def resize(m):
        tag = m.group(0)
        vb = re.search(r'viewBox=["\']([^"\']+)["\']', tag)
        if not vb:
            return tag
        parts = vb.group(1).split()
        if len(parts) < 4:
            return tag
        try:
            vb_w, vb_h = float(parts[2]), float(parts[3])
        except ValueError:
            return tag
        if vb_w <= 0 or vb_h <= 0:
            return tag

        scale = min(_SLIDE_MAX_W / vb_w, _SLIDE_MAX_H / vb_h, 1.0)
        new_w = int(vb_w * scale)
        new_h = int(vb_h * scale)

        tag = re.sub(r'\s+width=["\'][^"\']*["\']', "", tag)
        tag = re.sub(r'\s+height=["\'][^"\']*["\']', "", tag)
        tag = re.sub(r'\bmax-width:[^;"]*;?\s*', "", tag)
        tag = re.sub(r'\s+style="\s*"', "", tag)
        return tag[:-1] + ' width="{}" height="{}">'.format(new_w, new_h)

    return re.sub(r"<svg\b[^>]+>", resize, svg, count=1).encode("utf-8")


_MERMAID_EXPORT_STYLE = (
    "<style>\n"
    "img.mermaid-export {\n"
    "  display: block !important;\n"
    "  margin-left: auto !important;\n"
    "  margin-right: auto !important;\n"
    "}\n"
    "</style>"
)


def _insert_after_frontmatter(content, text):
    """Insert text immediately after the YAML front matter."""
    if content.startswith("---"):
        end = content.find("\n---", 3)
        if end >= 0:
            pos = end + 4
            while pos < len(content) and content[pos] == "\n":
                pos += 1
            return content[:pos] + "\n" + text + "\n\n" + content[pos:]
    return text + "\n\n" + content


def _preprocess_mermaid_for_export(content):
    """
    Replace ```mermaid blocks with <img> tags (data URI).
    Injects a <style> block with centering CSS after the frontmatter.
    Returns (processed_content, ok).
    """
    if "```mermaid" not in content:
        return content, True

    failed = [False]
    replaced = [False]

    def replace(m):
        code = m.group(1).strip()
        svg = _mermaid_to_svg(code)
        if svg is None:
            failed[0] = True
            return m.group(0)
        replaced[0] = True
        data_uri = "data:image/svg+xml;base64,{}".format(
            base64.b64encode(svg).decode("ascii")
        )
        return '\n<img class="mermaid-export" src="{}">\n'.format(data_uri)

    result = re.sub(
        r"```mermaid\s*\n(.*?)\n\s*```", replace, content, flags=re.DOTALL
    )

    if replaced[0]:
        result = _insert_after_frontmatter(result, _MERMAID_EXPORT_STYLE)

    return result, not failed[0]


class MarpExportCommand(sublime_plugin.TextCommand):
    """Export the current Marp file to a chosen format."""

    _FORMATS = [
        ["HTML",       "Single self-contained HTML file", ".html"],
        ["PDF",        "PDF file",                         ".pdf"],
        ["PowerPoint", "PPTX file",                      ".pptx"],
    ]

    def run(self, edit):
        file_path = self.view.file_name()
        if not file_path:
            sublime.error_message("Marp Export: Save the file before exporting.")
            return

        items = [[f[0], f[1]] for f in self._FORMATS]

        def on_select(index):
            if index < 0:
                return
            ext = self._FORMATS[index][2]
            output_path = os.path.splitext(file_path)[0] + ext
            _spawn(self._export, file_path, output_path, ext)

        self.view.window().show_quick_panel(items, on_select)

    def _export(self, file_path, output_path, ext):
        if not _ensure_binary():
            return

        base = _marp_base_cmd()
        file_dir = os.path.dirname(file_path)
        tmp_path = None

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as exc:
            _error("Marp Export: Cannot read file:\n{}".format(str(exc)))
            return

        if "```mermaid" in content:
            _status("Marp Export: Converting Mermaid diagrams...")
            processed, ok = _preprocess_mermaid_for_export(content)
            if not ok:
                _error(
                    "Marp Export: Failed to convert Mermaid diagrams.\n"
                    "Check your internet connection (uses mermaid.ink)."
                )
                return
            # write temp .md in the same directory so relative paths resolve correctly
            fd, tmp_path = tempfile.mkstemp(suffix=".md", dir=file_dir)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(processed)
            except Exception:
                os.close(fd)
                raise
            input_path = tmp_path
        else:
            input_path = file_path

        _status("Marp Export: Exporting...")

        try:
            cmd = base + [input_path, "-o", output_path, "--allow-local-files"]
            proc = _popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=file_dir,
            )
            try:
                _, stderr_bytes = proc.communicate(timeout=120)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                _error("Marp Export: Timed out.")
                return

            stderr = stderr_bytes.decode("utf-8", errors="replace")

            if proc.returncode == 0:
                _status("Marp Export: Done → {}".format(os.path.basename(output_path)))
                if sys.platform == "darwin":
                    _popen(["open", "-R", output_path])
            else:
                _error("Marp Export: Failed\n{}".format(stderr))

        except FileNotFoundError:
            _error("Marp Export: Binary not found.")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def is_enabled(self):
        return _is_marp_file(self.view.file_name() or "")


class MarpPreviewStopCommand(sublime_plugin.TextCommand):
    """Stop live-reload tracking for the current file."""

    def run(self, edit):
        file_path = self.view.file_name()
        if not file_path:
            return
        file_id = _file_id(file_path)
        with _store_lock:
            removed = _store.pop(file_id, None)
        if removed:
            sublime.status_message("Marp Preview: stopped tracking this file.")
        else:
            sublime.status_message("Marp Preview: this file was not being tracked.")

    def is_enabled(self):
        path = self.view.file_name() or ""
        return _is_marp_file(path) and _file_id(path) in _store


# ---------------------------------------------------------------------------
# Event listener
# ---------------------------------------------------------------------------

class MarpPreviewListener(sublime_plugin.EventListener):
    def on_post_save(self, view):
        file_path = view.file_name()
        if not _is_marp_file(file_path or ""):
            return
        file_id = _file_id(file_path)
        with _store_lock:
            if file_id not in _store:
                return
        _spawn(_update_store, file_id, file_path)


# ---------------------------------------------------------------------------
# Plugin lifecycle
# ---------------------------------------------------------------------------

def plugin_unloaded():
    global _server
    with _server_lock:
        if _server is not None:
            _server.shutdown()
            _server = None
    with _store_lock:
        _store.clear()
