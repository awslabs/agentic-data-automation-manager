#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""An HTTP server standing in for the AgentCore Runtime endpoint, the one piece of the chat
path with no local equivalent. It records each request and answers with a chosen status and
body; where the runtime's answer matters, a test passes it what the real runtime app answered."""

import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


class RuntimeStandIn:

    def __init__(self, status: int, body: str = "", delay: float = 0.0, headers: dict | None = None):
        self.requests: list[tuple[str, str, dict, bytes]] = []
        stand_in = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self):
                length = int(self.headers.get("Content-Length", 0))
                stand_in.requests.append((self.command, self.path, dict(self.headers), self.rfile.read(length)))
                time.sleep(delay)
                try:
                    self.send_response(status)
                    for name, value in (headers or {}).items():
                        self.send_header(name, value)
                    self.end_headers()
                    self.wfile.write(body.encode())
                except OSError:
                    pass  # the caller stopped waiting

            do_POST = do_GET = _answer

            def log_message(self, format, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
