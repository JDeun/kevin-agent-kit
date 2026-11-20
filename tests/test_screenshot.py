import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest


def test_screenshot_blocks_fonts_from_another_origin(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as automation:
        try:
            automation.chromium.launch().close()
        except playwright.Error as error:
            if "Executable doesn't exist" not in str(error):
                raise
            pytest.skip("Playwright Chromium must be installed for screenshot verification")

    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            if self.path == "/redirect.woff2":
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{other.server_port}/redirected.woff2")
                self.end_headers()
                return
            self.send_response(200)
            if self.path == "/":
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(f"""
                    <style>
                      @font-face {{ font-family: Bundled; src: url(/bundled.woff2); }}
                      @font-face {{ font-family: External;
                        src: url(http://127.0.0.1:{other.server_port}/external.woff2); }}
                      @font-face {{ font-family: Redirected; src: url(/redirect.woff2); }}
                    </style>
                    <div style="font-family: Bundled">Bundled</div>
                    <div style="font-family: External">External</div>
                    <div style="font-family: Redirected">Redirected</div>
                """.encode())
            else:
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(b"example font")

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as app, \
            ThreadingHTTPServer(("127.0.0.1", 0), Handler) as other:
        servers = (app, other)
        threads = [Thread(target=server.serve_forever, daemon=True) for server in servers]
        for thread in threads:
            thread.start()
        try:
            output = tmp_path / "capture.png"
            subprocess.run(
                [sys.executable, "-m", "verify.screenshot",
                 f"http://127.0.0.1:{app.server_port}/", str(output)],
                capture_output=True,
                text=True,
                check=True,
            )
            assert output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
            assert "/bundled.woff2" in requests
            assert "/redirect.woff2" in requests
            assert "/external.woff2" not in requests
            assert "/redirected.woff2" not in requests
        finally:
            for server in servers:
                server.shutdown()
            for thread in threads:
                thread.join()
