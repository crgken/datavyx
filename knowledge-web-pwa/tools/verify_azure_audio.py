"""Recognize controlled browser-captured audio using the existing Azure backend SDK.

This is not the authenticated Web HTTP end-to-end check; that needs a real SPA login.
"""
import base64
import json
from pathlib import Path
import sys
import time
import uuid
import wave

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT.parent / 'knowledge-management-demo'
sys.path.insert(0, str(BACKEND))
load_dotenv(BACKEND / '.env')
from demo.mobile_speech import transcribe  # noqa: E402
from demo.model import DemoError  # noqa: E402

target = ROOT / 'artifacts/azure-browser-audio.json'
report = json.loads(target.read_text()) if target.exists() else {
    'provider': 'azure', 'fixture_type': 'controlled synthetic browser microphone',
    'scope': 'real browser capture to real Azure SDK; Auth0 HTTP flow not yet verified',
    'checks': [],
}
for language in sys.argv[1:]:
    if language not in {'zh', 'en'}:
        raise SystemExit('Expected zh or en')
    fixture = ROOT / 'artifacts/browser-audio' / (language + '.wav')
    with wave.open(str(fixture)) as recording:
        seconds = recording.getnframes() / recording.getframerate()
    started = time.monotonic()
    row = {'language': language, 'audio_seconds': round(seconds, 3)}
    try:
        result = transcribe({'audio': base64.b64encode(fixture.read_bytes()).decode(),
            'language': language, 'recording_id': str(uuid.uuid4())},
            actor='controlled-browser-media-verification', workspace='knowledge-demo')
        text = result['text']
        row.update(text=text, elapsed_seconds=round(time.monotonic() - started, 2),
            passed=('合同' in text and '广通' in text) if language == 'zh'
                else ('confirmed contracts' in text.lower() and 'northwind' in text.lower()))
    except DemoError as error:
        row.update(passed=False, error=error.code)
    except Exception as error:
        row.update(passed=False, error=type(error).__name__)
    report['checks'] = [r for r in report['checks'] if r['language'] != language] + [row]
report['passed'] = len(report['checks']) == 2 and all(r['passed'] for r in report['checks'])
target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(report, ensure_ascii=False))
if any(not r['passed'] for r in report['checks']):
    raise SystemExit(1)
