#!/usr/bin/env python3
"""Serve this folder over HTTP, which index.html needs and file:// cannot give.

    ./serve.py [port]        # default 8765

Nothing here wants a real web server. The page fetches the checkpoint whole
rather than by ranges, and it runs no threads and touches no SharedArrayBuffer,
so there is no Range support and no COOP/COEP to arrange. What it does want is
an origin: opened as a file://, the page gets an opaque one and Chrome then
refuses to let it read weights.gguf sitting right beside it.
"""

import http.server
import pathlib
import socketserver
import sys

HERE = pathlib.Path(__file__).resolve().parent
WEIGHTS = HERE / "weights.gguf"
DEFAULT_PORT = 8765


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(HERE), **kw)


def main() -> int:
    port = DEFAULT_PORT
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            print(f"not a port: {sys.argv[1]}", file=sys.stderr)
            return 2

    # weights.gguf is deliberately not in git — it is 378 MB, over GitHub's
    # 100 MB limit — so a fresh clone has everything except the model. Say so
    # here rather than let the page report a 404 as a failed download.
    if not WEIGHTS.exists():
        print(
            "no weights.gguf here, so the llm tile will have nothing to load.\n"
            "  cp ~/dev/llmoxide/models/Qwen3-0.6B-Q4_K_M.gguf weights.gguf\n",
            flush=True,
        )

    # Threading matters more than it looks: the model is one long response, and
    # a single-threaded server would sit on it while the emulator in the tile
    # above is still waiting for its rom.
    class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    try:
        httpd = Server(("", port), Handler)
    except OSError as e:
        print(f"cannot listen on {port}: {e}", file=sys.stderr)
        print("pick another: ./serve.py 8080", file=sys.stderr)
        return 1

    # flush: stdout is block-buffered the moment this is piped anywhere, and a
    # banner that appears only once the server is killed is worse than none.
    print(
        f"serving {HERE} at http://localhost:{port}/\n"
        f"  board http://localhost:{port}/\n"
        "  the emulator and the llm are folded into it — both open from their tiles\n"
        "ctrl-c to stop",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
