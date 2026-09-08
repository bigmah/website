#!/usr/bin/env python3
"""Serve this folder over HTTP, which llm.html needs and file:// cannot give.

    ./serve.py                  # http://localhost:8765, for working on the page
    ./serve.py 8765 --bind ''   # every interface, for a proxy on another host
    ./serve.py 443 --cert fullchain.pem --key privkey.pem   # https, unproxied

Nothing here wants a real web server. llm.html fetches the checkpoint whole
rather than by ranges, and it runs no threads and touches no SharedArrayBuffer,
so there is no Range support and no COOP/COEP to arrange. What it does want is
an origin: opened as a file://, the page gets an opaque one and Chrome then
refuses to let it read weights.gguf sitting right beside it.

Putting it on the public internet is the other reason to run it, and https is
not optional there. The Cache API the page keeps the 378 MB checkpoint in is a
secure-context feature: over plain http to anything but localhost it is simply
undefined, the cache is skipped without a word, and every visit pays for the
whole download again.

Terminating that TLS is someone else's job by default — nginx, Caddy or a
tunnel in front, pointed at this port. `--cert` is here for when there is no
proxy to do it. What this file does either way is stop being a thing that was
only ever pointed at localhost:

  * The listener is loopback-only unless told otherwise, so nobody reaches the
    plaintext port around the https in front of it.
  * Dotted names are 404 and directories never list. This folder is a git
    checkout; served naively, .git/config and the whole history go out with
    the page.
  * HTTP/1.1, so a proxy holds one connection open instead of reopening it per
    request, and a tab closed halfway through the checkpoint ends quietly
    rather than as a stack trace.

Behind nginx, the settings that matter for a 378 MB response:

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;   # keep-alive, matching protocol_version below
        proxy_buffering off;      # else nginx spools the checkpoint to disk
        proxy_read_timeout 600s;  # a slow phone is still downloading
    }

Caddy wants none of that; `reverse_proxy 127.0.0.1:8765` already streams. What
both need is gzip left off for .gguf and .gba: the weights are quantised
already, so compressing them burns cpu to make them bigger, and a compressed
response drops the Content-Length that the page's progress bar reads.
"""

import argparse
import http.server
import ipaddress
import pathlib
import socket
import socketserver
import ssl
import sys
import threading
from http import HTTPStatus

HERE = pathlib.Path(__file__).resolve().parent
WEIGHTS = HERE / "weights.gguf"
DEFAULT_PORT = 8765

# Per socket operation, not per connection: sixty seconds of a peer saying
# nothing is a dead connection, not a slow one, since even a phone on a bad
# network drains a send buffer far faster than that. Without it, a public port
# holds a thread per idle keep-alive connection for as long as anyone likes.
IDLE_TIMEOUT = 60


class Handler(http.server.SimpleHTTPRequestHandler):
    # HTTP/1.0 — the default — closes the connection after every response, and
    # a reverse proxy then has to open a new one for the next request. Every
    # response here carries a Content-Length, so keep-alive is safe to promise.
    protocol_version = "HTTP/1.1"
    timeout = IDLE_TIMEOUT

    # No point advertising the Python version to whatever scans the port.
    server_version = "serve.py"
    sys_version = ""

    # Set per request in send_head, read in end_headers, which errors reach too.
    _cache_control = None

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(HERE), **kw)

    def version_string(self):
        # The default joins server_version and sys_version with a space, and
        # with the latter empty the leftover one trails the Server header.
        return self.server_version

    def handle_one_request(self):
        # One handler object serves every request on a kept-alive connection,
        # so the last one's Cache-Control must not ride along with a response
        # that never reaches send_head — a 400 on a malformed request, say.
        self._cache_control = None
        super().handle_one_request()

    def send_head(self):
        # translate_path has already unescaped the path and collapsed "..", so
        # this is the name of the file that would actually be opened rather
        # than the one the request claimed.
        target = pathlib.Path(self.translate_path(self.path))
        try:
            parts = target.relative_to(HERE).parts
        except ValueError:
            return self.not_found()
        if any(part.startswith(".") for part in parts):
            return self.not_found()

        # The pages are what change; the rom and the checkpoint beside them do
        # not, and a CDN holding those for a day is most of the reason to have
        # one. Revalidating the html costs nearly nothing — it comes back 304 on
        # the Last-Modified this already sends.
        self._cache_control = (
            "public, max-age=86400"
            if target.suffix.lower() in (".gguf", ".gba")
            else "no-cache"
        )
        return super().send_head()

    def list_directory(self, path):
        # index.html is found before this is reached, so arriving here means a
        # directory that has none, and publishing its contents helps nobody.
        return self.not_found()

    def not_found(self):
        self.send_error(HTTPStatus.NOT_FOUND, "File not found")
        return None

    def end_headers(self):
        # Every Content-Type here is right, so there is no reason to let a
        # browser guess a different one.
        self.send_header("X-Content-Type-Options", "nosniff")
        if self._cache_control:
            self.send_header("Cache-Control", self._cache_control)
        super().end_headers()

    def log_message(self, fmt, *args):
        # Behind a proxy every connection comes from 127.0.0.1, so the only
        # informative address is the one the proxy passed along. It is a header
        # and a header can say anything, but this is a log line, not a decision.
        headers = getattr(self, "headers", None)
        forwarded = headers.get("X-Forwarded-For") if headers else None
        who = forwarded.split(",")[0].strip() if forwarded else self.address_string()
        sys.stderr.write(
            "%s - - [%s] %s\n" % (who, self.log_date_time_string(), fmt % args)
        )


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    # Threading matters more than it looks: the model is one long response, and
    # a single-threaded server would sit on it while the emulator in the tile
    # above is still waiting for its rom.
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):
        if self.address_family == socket.AF_INET6:
            # A wildcard v6 socket answers v4 connections too, once this is off.
            try:
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            except OSError:
                pass
        super().server_bind()

    def handle_error(self, request, client_address):
        # A tab closed halfway through 378 MB is not an incident, and neither
        # is a scanner speaking plain http at the https port. Both arrive here
        # as exceptions, and both print a stack trace to a public server's log
        # unless they are named.
        exc = sys.exc_info()[1]
        if isinstance(
            exc,
            (
                BrokenPipeError,
                ConnectionResetError,
                ConnectionAbortedError,
                TimeoutError,
                ssl.SSLError,
            ),
        ):
            return
        super().handle_error(request, client_address)


def listen_addresses(host, port):
    """Every address `host` names, so that binding it misses none of them.

    A proxy configured with `localhost` reaches for ::1 first on anything
    modern and falls back to 127.0.0.1; listening on only one of the two is a
    connection refused that reads as the server being down. getaddrinfo knows
    both, so bind both. The empty host is the exception — one dual-stack
    wildcard socket already covers everything.
    """
    if host == "":
        if socket.has_ipv6:
            return [(socket.AF_INET6, "::")]
        return [(socket.AF_INET, "0.0.0.0")]

    found, seen = [], set()
    for family, _type, _proto, _canon, sockaddr in socket.getaddrinfo(
        host, port, type=socket.SOCK_STREAM
    ):
        if family in (socket.AF_INET, socket.AF_INET6) and sockaddr[0] not in seen:
            seen.add(sockaddr[0])
            found.append((family, sockaddr[0]))
    return found


def make_server(family, addr, port, context):
    # address_family is read while the socket is being made, so it has to be on
    # the class rather than set afterwards.
    server_cls = type("Server", (Server,), {"address_family": family})
    httpd = server_cls((addr, port), Handler)
    if context is not None:
        # Wrapping the listening socket makes every accepted connection a TLS
        # one; a handshake that fails takes the connection down and nothing
        # else, which is what the OSError arm of _handle_request_noblock does.
        httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    return httpd


def show(addr, port):
    return f"[{addr}]:{port}" if ":" in addr else f"{addr}:{port}"


def is_loopback(addr):
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(
        description="serve this folder for the page sitting in it",
    )
    ap.add_argument(
        "port", nargs="?", type=int, default=DEFAULT_PORT,
        help=f"port to listen on (default {DEFAULT_PORT})",
    )
    ap.add_argument(
        "-b", "--bind", default=None, metavar="HOST",
        help="address to listen on; '' for every interface "
             "(default: localhost, or every interface with --cert)",
    )
    ap.add_argument(
        "--cert", metavar="FILE",
        help="serve https directly with this certificate, instead of leaving "
             "TLS to a proxy in front (certbot: fullchain.pem)",
    )
    ap.add_argument(
        "--key", metavar="FILE",
        help="private key for --cert, if it is not in the same file "
             "(certbot: privkey.pem)",
    )
    args = ap.parse_args()

    context = None
    if args.key and not args.cert:
        print("--key needs --cert", file=sys.stderr)
        return 2
    if args.cert:
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        try:
            context.load_cert_chain(args.cert, args.key)
        except (OSError, ssl.SSLError) as e:
            # Almost always one of two things: certbot keeps privkey.pem
            # readable by root alone, and a fullchain without its key is not a
            # chain this can serve.
            print(f"cannot load {args.cert}: {e}", file=sys.stderr)
            return 1

    # Plaintext exists for a proxy on this machine to pick up and for nobody
    # else, so it stays on loopback until asked otherwise. A certificate of its
    # own means there is nothing left to hide behind.
    host = args.bind
    if host is None:
        host = "" if context else "localhost"

    try:
        addresses = listen_addresses(host, args.port)
    except socket.gaierror as e:
        print(f"cannot resolve {host!r}: {e}", file=sys.stderr)
        return 1
    if not addresses:
        print(f"nothing to bind for {host!r}", file=sys.stderr)
        return 1

    # weights.gguf is deliberately not in git — it is 378 MB, over GitHub's
    # 100 MB limit — so a fresh clone has everything except the model. Say so
    # here rather than let the page report a 404 as a failed download.
    if not WEIGHTS.exists():
        print(
            "no weights.gguf here, so llm.html will have nothing to load.\n"
            "  cp ~/dev/llmoxide/models/Qwen3-0.6B-Q4_K_M.gguf weights.gguf\n",
            flush=True,
        )

    servers = []
    for family, addr in addresses:
        try:
            servers.append(make_server(family, addr, args.port, context))
        except OSError as e:
            for httpd in servers:
                httpd.server_close()
            print(f"cannot listen on {show(addr, args.port)}: {e}", file=sys.stderr)
            if args.port < 1024:
                print("ports under 1024 need root", file=sys.stderr)
            print("pick another: ./serve.py 8080", file=sys.stderr)
            return 1

    scheme = "https" if context else "http"
    seen_addrs = [addr for _family, addr in addresses]
    public = not all(is_loopback(a) for a in seen_addrs)
    # An empty --bind names no host to put in a URL, so use the machine's own.
    reachable = "localhost" if not public else (host or socket.gethostname())

    # flush: stdout is block-buffered the moment this is piped anywhere, and a
    # banner that appears only once the server is killed is worse than none.
    print(
        f"serving {HERE} at {scheme}://{reachable}:{args.port}/\n"
        f"  listening on {', '.join(show(a, args.port) for a in seen_addrs)}\n"
        f"  on their own {scheme}://{reachable}:{args.port}/gba.html"
        f" and /llm.html",
        flush=True,
    )
    if public and context is None:
        print(
            "  plaintext on a public interface: put https in front of it, or\n"
            "  pass --cert. Without it the page cannot cache the checkpoint and\n"
            "  re-downloads 378 MB on every visit.",
            flush=True,
        )
    print("ctrl-c to stop", flush=True)

    # One of them runs here rather than in a thread of its own: serve_forever
    # polls, so ctrl-c reaches it, and a main thread parked on anything else
    # can sit through the signal instead.
    for httpd in servers[1:]:
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        for httpd in servers[1:]:
            httpd.shutdown()
        for httpd in servers:
            httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
