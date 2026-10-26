"""Loopback-only static PWA host and strict same-origin mobile API proxy."""
import argparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

ROOT = Path(__file__).resolve().parents[1]
READ = {'/api/mobile/session'}
WRITE = {'/api/mobile/execute', '/api/mobile/transcribe', '/api/mobile/logout'}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def serve(port, directory):
    origins = {f'http://localhost:{port}', f'http://127.0.0.1:{port}'}
    hosts = {f'localhost:{port}', f'127.0.0.1:{port}'}
    probe_connect = ' http://localhost:8098' if directory.name == 'media-probe' else ''
    class Handler(SimpleHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        timeout = 15
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(directory), **kwargs)
        def log_message(self, *args):
            pass  # No auth codes, token headers, transcripts or URL queries in logs.
        def end_headers(self):
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Permissions-Policy', 'microphone=(self), camera=()')
            self.send_header('Cache-Control', 'no-cache' if self.path in {'/', '/index.html', '/sw.js'} else 'no-store')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' https://fonts.gstatic.com; "
                "connect-src 'self' blob: https://dev-zto67ad0zoj5nzmb.au.auth0.com https://fonts.gstatic.com https://www.gstatic.com" + probe_connect + "; "
                "frame-src https://dev-zto67ad0zoj5nzmb.au.auth0.com; worker-src 'self' blob:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'")
            super().end_headers()
        def fail(self, code, status):
            body = json.dumps({'ok': False, 'errors': [{'code': code}]}).encode()
            self.close_connection = True
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(body)
        def trusted(self):
            return self.headers.get('Host') in hosts and self.headers.get('Origin') in origins | {None}
        def proxy(self):
            if not self.trusted():
                return self.fail('invalid_origin', 403)
            path = urlsplit(self.path)
            allowed = READ if self.command == 'GET' else WRITE
            if path.path not in allowed or path.query:
                return self.fail('not_found', 404)
            if self.command == 'POST':
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                except ValueError:
                    return self.fail('invalid_body', 400)
                if not 0 < size <= 2800000:
                    return self.fail('invalid_body', 400)
                body = self.rfile.read(size)
            else:
                body = None
            headers = {key: self.headers[key] for key in ('Authorization', 'X-Workspace-ID',
                'Accept-Language', 'Content-Type', 'Origin') if key in self.headers}
            request = Request('http://127.0.0.1:8096' + path.path, data=body,
                              method=self.command, headers=headers)
            try:
                response = build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=125)
            except HTTPError as error:
                response = error
            except (URLError, TimeoutError, OSError):
                return self.fail('service_unavailable', 503)
            with response:
                data = response.read(4000000)
                self.send_response(response.code)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        def do_GET(self):
            if not self.trusted():
                return self.fail('invalid_origin', 403)
            if self.path.startswith('/api/'):
                return self.proxy()
            return super().do_GET()
        def do_POST(self):
            return self.proxy()
    ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8097)
    parser.add_argument('--directory', default='build/web')
    args = parser.parse_args()
    directory = (ROOT / args.directory).resolve()
    if not directory.is_relative_to((ROOT / 'build').resolve()) or not directory.is_dir():
        parser.error('Static content must be an existing directory inside this project build folder')
    print(f'Knowledge Web PWA: http://localhost:{args.port}', flush=True)
    serve(args.port, directory)
