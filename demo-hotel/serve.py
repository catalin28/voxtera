"""Simple HTTP server for the demo frontend that handles connection resets gracefully."""

import contextlib
import http.server
import socketserver
import sys


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    """Suppresses ConnectionResetError tracebacks from aborted browser connections."""

    def handle_one_request(self):
        with contextlib.suppress(ConnectionResetError):
            super().handle_one_request()

    def log_message(self, format, *args):  # noqa: A002
        # Standard logging but without noisy tracebacks
        msg = format % args
        sys.stderr.write(f"{self.address_string()} - - [{self.log_date_time_string()}] {msg}\n")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    with socketserver.ThreadingTCPServer(("", port), QuietHandler) as httpd:
        httpd.allow_reuse_address = True
        print(f"Serving demo on http://localhost:{port}/demo.html")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
