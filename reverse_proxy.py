from __future__ import annotations

import argparse
import http.client
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


HOP_BY_HOP_HEADERS = {
    'connection',
    'keep-alive',
    'proxy-authenticate',
    'proxy-authorization',
    'te',
    'trailers',
    'transfer-encoding',
    'upgrade',
}


class ProxyHandler(BaseHTTPRequestHandler):
    server_version = 'CalibreReviewProxy/0.1'

    def do_GET(self):
        self._proxy()

    def do_POST(self):
        self._proxy()

    def do_PUT(self):
        self._proxy()

    def do_PATCH(self):
        self._proxy()

    def do_DELETE(self):
        self._proxy()

    def do_OPTIONS(self):
        self._proxy()

    def do_HEAD(self):
        self._proxy(send_body=False)

    def _proxy(self, send_body: bool = True):
        parsed = urlsplit(self.path)
        upstream = self.server.review_upstream if parsed.path == self.server.review_prefix or parsed.path.startswith(self.server.review_prefix + '/') else self.server.cwa_upstream
        conn = http.client.HTTPConnection(*upstream, timeout=60)
        body = None
        if send_body and self.command not in {'GET', 'HEAD'}:
            length = int(self.headers.get('Content-Length', '0'))
            body = self.rfile.read(length) if length else None
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() != 'host'
        }
        headers['Host'] = self.headers.get('Host', f'{upstream[0]}:{upstream[1]}')
        conn.request(self.command, self.path, body=body, headers=headers)
        resp = conn.getresponse()
        payload = resp.read()
        self.send_response(resp.status, resp.reason)
        for key, value in resp.getheaders():
            if key.lower() in HOP_BY_HOP_HEADERS:
                continue
            if key.lower() == 'location' and value.startswith('http://127.0.0.1:'):
                value = value.split('127.0.0.1', 1)[-1]
            self.send_header(key, value)
        self.end_headers()
        if send_body and self.command != 'HEAD':
            self.wfile.write(payload)
        conn.close()

    def log_message(self, fmt, *args):
        return


class ProxyServer(ThreadingHTTPServer):
    def __init__(self, server_address, RequestHandlerClass, review_upstream, cwa_upstream, review_prefix='/ai-review'):
        super().__init__(server_address, RequestHandlerClass)
        self.review_upstream = review_upstream
        self.cwa_upstream = cwa_upstream
        self.review_prefix = review_prefix.rstrip('/')


def main():
    parser = argparse.ArgumentParser(description='Reverse proxy for Calibre-Web and metadata review.')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8083)
    parser.add_argument('--review-host', default='127.0.0.1')
    parser.add_argument('--review-port', type=int, default=8137)
    parser.add_argument('--cwa-host', default='127.0.0.1')
    parser.add_argument('--cwa-port', type=int, default=18083)
    parser.add_argument('--review-prefix', default='/ai-review')
    args = parser.parse_args()
    server = ProxyServer(
        (args.host, args.port),
        ProxyHandler,
        review_upstream=(args.review_host, args.review_port),
        cwa_upstream=(args.cwa_host, args.cwa_port),
        review_prefix=args.review_prefix,
    )
    print(f'Proxy listening on http://{args.host}:{args.port}, review at {args.review_prefix}')
    server.serve_forever()


if __name__ == '__main__':
    main()

