"""Authenticated speech transcription; no audio or knowledge facts are persisted."""
import base64
from collections import OrderedDict
from functools import lru_cache
import hashlib
import io
import logging
import os
import re
import threading
import time
import wave

from .model import DemoError, require


LOCALES = {'zh': 'zh-CN', 'en': 'en-US'}
RECOGNITION_TIMEOUT = 90
_slot = threading.BoundedSemaphore(1)  # Azure Speech F0: one concurrent request.
_lock = threading.Lock()
_active = {}
_completed = OrderedDict()
_logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _sdk():
    import azure.cognitiveservices.speech as speechsdk
    return speechsdk


def speech_available():
    if (os.getenv('KD_SPEECH_PROVIDER') != 'azure' or
            not os.getenv('KD_AZURE_SPEECH_KEY', '').strip() or
            not re.fullmatch(r'[a-z0-9]+', os.getenv('KD_AZURE_SPEECH_REGION', ''))):
        return False
    try:
        _sdk()
        return True
    except (ImportError, OSError):
        return False


def _audio(body):
    require(isinstance(body, dict) and set(body) == {'audio', 'language', 'recording_id'},
            'invalid_audio', '音频请求格式无效。')
    audio, language, recording_id = body['audio'], body['language'], body['recording_id']
    require(isinstance(language, str) and language in LOCALES and
            isinstance(recording_id, str) and
            re.fullmatch(r'[A-Za-z0-9_-]{16,100}', recording_id) and
            isinstance(audio, str) and 0 < len(audio) <= 2700000,
            'invalid_audio', '录音格式或长度无效。')
    try:
        raw = base64.b64decode(audio, validate=True)
        require(len(raw) <= 2000000, 'invalid_audio', '录音文件过大。')
        with wave.open(io.BytesIO(raw), 'rb') as recording:
            frames = recording.getnframes()
            require(recording.getframerate() == 16000 and recording.getnchannels() == 1 and
                    recording.getsampwidth() == 2 and recording.getcomptype() == 'NONE' and
                    0 < frames <= 60 * 16000,
                    'invalid_audio', '录音格式或长度无效。')
            pcm = recording.readframes(frames)
            require(len(pcm) == frames * 2, 'invalid_audio', '录音内容不完整。')
    except DemoError:
        raise
    except (ValueError, wave.Error, EOFError) as exc:
        raise DemoError('invalid_audio', '录音格式无效。') from exc
    return pcm, language, recording_id


def _cancellation(code, sdk):
    if code == sdk.CancellationErrorCode.AuthenticationFailure:
        return DemoError('speech_config_invalid', '语音服务认证失败，请联系管理员检查区域和密钥。', 503)
    if code == sdk.CancellationErrorCode.Forbidden:
        return DemoError('speech_quota_exceeded', '语音服务额度不足或访问被拒绝，请联系管理员。', 503)
    if code == sdk.CancellationErrorCode.TooManyRequests:
        return DemoError('speech_busy', '语音服务繁忙，请稍后重试。', 429)
    return DemoError('speech_unavailable', '语音识别暂不可用，请重试或输入文字。', 503)


def _recognize(pcm, locale):
    sdk = _sdk()
    config = sdk.SpeechConfig(subscription=os.environ['KD_AZURE_SPEECH_KEY'].strip(),
                              region=os.environ['KD_AZURE_SPEECH_REGION'])
    config.speech_recognition_language = locale
    # Default SDK audio logging is not enabled. Feed raw PCM in memory.
    stream = sdk.audio.PushAudioInputStream(sdk.audio.AudioStreamFormat(
        samples_per_second=16000, bits_per_sample=16, channels=1))
    recognizer = None
    done = threading.Event()
    fragments, failures = [], []

    def recognized(event):
        if event.result.reason == sdk.ResultReason.RecognizedSpeech:
            fragments.append((event.result.offset, event.result.text))

    def canceled(event):
        if event.reason == sdk.CancellationReason.Error:
            failures.append(event.error_code)
        done.set()

    try:
        recognizer = sdk.SpeechRecognizer(speech_config=config,
            audio_config=sdk.audio.AudioConfig(stream=stream))
        recognizer.recognized.connect(recognized)
        recognizer.canceled.connect(canceled)
        recognizer.session_stopped.connect(lambda _: done.set())
        recognizer.start_continuous_recognition_async().get()
        stream.write(pcm)
        stream.close()
        require(done.wait(RECOGNITION_TIMEOUT), 'speech_timeout',
                '语音识别超时，请重试或输入文字。', 504)
        if failures:
            raise _cancellation(failures[0], sdk)
        text = (' ' if locale.startswith('en') else '').join(
            text.strip() for _, text in sorted(fragments, key=lambda item: item[0]) if text.strip())
        require(bool(text), 'empty_transcript', '未识别到内容，请重新录音。')
        require(len(text) <= 16000, 'speech_invalid_response', '识别结果过长。', 502)
        return text
    except DemoError:
        raise
    except Exception as exc:
        # SDK exception text may contain endpoints or credentials; never expose it.
        raise DemoError('speech_unavailable', '语音识别暂不可用，请重试或输入文字。', 503) from exc
    finally:
        stream.close()
        if recognizer is not None:
            try:
                recognizer.stop_continuous_recognition_async().get()
            except Exception:
                pass
            recognizer.recognized.disconnect_all()
            recognizer.canceled.disconnect_all()
            recognizer.session_stopped.disconnect_all()


def transcribe(body, *, actor, workspace):
    require(isinstance(actor, str) and bool(actor) and isinstance(workspace, str) and bool(workspace),
            'permission_denied', '语音识别需要已授权的工作空间。', 403)
    pcm, language, recording_id = _audio(body)
    require(speech_available(), 'speech_not_configured', '语音识别服务尚未配置。', 503)
    key = (workspace, actor, recording_id)
    fingerprint = hashlib.sha256(language.encode() + pcm).hexdigest()
    now = time.monotonic()
    with _lock:
        for old in [k for k, item in _completed.items() if item[0] <= now]:
            del _completed[old]
        cached = _completed.get(key)
        if cached:
            require(cached[1] == fingerprint, 'invalid_audio', '录音编号与内容不匹配。')
            return dict(cached[2])
        if key in _active:
            require(_active[key] == fingerprint, 'invalid_audio', '录音编号与内容不匹配。')
            raise DemoError('speech_busy', '该录音正在识别，请稍后重试。', 429)
        require(len(_active) < 8, 'speech_busy', '语音服务繁忙，请稍后重试。', 429)
        _active[key] = fingerprint
    acquired = False
    status = 'speech_unavailable'
    started = time.monotonic()
    try:
        acquired = _slot.acquire(timeout=10)
        require(acquired, 'speech_busy', '语音服务繁忙，请稍后重试。', 429)
        text = _recognize(pcm, LOCALES[language])
        result = {'recording_id': recording_id, 'language': language, 'text': text}
        with _lock:
            _completed[key] = (time.monotonic() + 300, fingerprint, result)
            while len(_completed) > 128:
                _completed.popitem(last=False)
        status = 'success'
        return dict(result)
    except DemoError as exc:
        status = exc.code
        raise
    finally:
        if acquired:
            _slot.release()
        with _lock:
            _active.pop(key, None)
        # Metadata only. No audio, transcripts, subscription keys or SDK messages.
        _logger.info('speech provider=azure actor=%s language=%s audio_ms=%d elapsed_ms=%d status=%s',
                     hashlib.sha256(actor.encode()).hexdigest()[:12], language,
                     len(pcm) * 1000 // 32000, int((time.monotonic() - started) * 1000), status)

