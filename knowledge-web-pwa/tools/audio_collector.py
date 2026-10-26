"""Test-only loopback WAV sink. No authentication or business access."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
import io
import wave

TARGET = Path(__file__).resolve().parents[1] / 'artifacts/browser-audio'
TARGET.mkdir(parents=True, exist_ok=True)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def allowed(self):
        origin = self.headers.get('Origin', '')
        value = urlsplit(origin)
        return value.scheme == 'http' and value.hostname in {'localhost', '127.0.0.1'}
    def result(self, status):
        self.send_response(status)
        if self.allowed():
            self.send_header('Access-Control-Allow-Origin', self.headers['Origin'])
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'content-type')
        self.send_header('Content-Length', '0')
        self.end_headers()
    def do_OPTIONS(self):
        self.result(200 if self.allowed() else 403)
    def do_POST(self):
        if not self.allowed() or self.path not in {'/zh', '/en'}:
            return self.result(403)
        size = int(self.headers.get('Content-Length', '0'))
        if not 44 < size <= 2000000:
            return self.result(400)
        audio = self.rfile.read(size)
        with wave.open(io.BytesIO(audio)) as recording:
            if (recording.getframerate(), recording.getnchannels(), recording.getsampwidth()) != (16000, 1, 2):
                return self.result(400)
        (TARGET / (self.path[1:] + '.wav')).write_bytes(audio)
        self.result(200)


if __name__ == '__main__':
    print('Controlled browser audio collector: localhost:8098', flush=True)
    ThreadingHTTPServer(('127.0.0.1', 8098), Handler).serve_forever()
