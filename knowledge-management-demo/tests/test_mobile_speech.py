import base64
import io
import os
import threading
import unittest
from unittest.mock import Mock, patch
import wave

from demo import mobile_speech as speech
from demo.model import DemoError


def wav(seconds=1, rate=16000, channels=1):
    out = io.BytesIO()
    with wave.open(out, 'wb') as recording:
        recording.setnchannels(channels)
        recording.setsampwidth(2)
        recording.setframerate(rate)
        recording.writeframes(b'\x00\x00' * (seconds * rate * channels))
    return out.getvalue()


def body(data=None, language='zh', recording_id='test-recording-id-001'):
    return {'audio': base64.b64encode(data if data is not None else wav()).decode(),
            'language': language, 'recording_id': recording_id}


class SpeechTests(unittest.TestCase):
    def setUp(self):
        speech._active.clear()
        speech._completed.clear()
        self.ready = patch.object(speech, 'speech_available', return_value=True)
        self.ready.start()
        self.addCleanup(self.ready.stop)

    def call(self, value, actor='user-a', workspace='space-a'):
        return speech.transcribe(value, actor=actor, workspace=workspace)

    def assert_code(self, code, work):
        with self.assertRaises(DemoError) as caught:
            work()
        self.assertEqual(caught.exception.code, code)

    def test_valid_wav_and_both_locales(self):
        for language in ('zh', 'en'):
            pcm, actual, recording_id = speech._audio(body(language=language))
            self.assertEqual(len(pcm), 32000)
            self.assertEqual(actual, language)
            self.assertEqual(recording_id, 'test-recording-id-001')

    def test_reject_malformed_audio_and_extra_parameters(self):
        for value in [body(b'not wav'), {**body(), 'actor': 'admin'},
                      body(language='auto'), body(recording_id='short'),
                      body(wav(rate=8000)), body(wav(channels=2)),
                      body(wav(seconds=0)), body(wav()[:-10])]:
            with self.subTest(fields=list(value)):
                self.assert_code('invalid_audio', lambda: self.call(value))

    def test_audio_limit_is_enforced_before_provider_call(self):
        with patch.object(speech, '_recognize') as recognize:
            self.assert_code('invalid_audio', lambda: self.call(body(wav(seconds=61))))
            recognize.assert_not_called()
        self.assertEqual(len(speech._audio(body(wav(seconds=60)))[0]), 1920000)

    def test_requires_server_supplied_scope(self):
        self.assert_code('permission_denied', lambda: self.call(body(), actor=''))
        self.assert_code('permission_denied', lambda: self.call(body(), workspace=''))

    def test_success_is_cached_only_in_same_user_and_workspace(self):
        with patch.object(speech, '_recognize', return_value='识别文字') as recognize:
            first = self.call(body())
            self.assertEqual(self.call(body()), first)
            self.call(body(), actor='user-b')
            self.call(body(), workspace='space-b')
            self.assertEqual(recognize.call_count, 3)

    def test_reused_recording_id_cannot_change_content_or_language(self):
        with patch.object(speech, '_recognize', return_value='text'):
            self.call(body())
            self.assert_code('invalid_audio', lambda: self.call(body(language='en')))
            self.assert_code('invalid_audio', lambda: self.call(body(wav(seconds=2))))

    def test_failure_can_be_explicitly_retried_without_fake_success(self):
        with patch.object(speech, '_recognize', side_effect=[
                DemoError('speech_timeout', 'timeout', 504), 'retained result']) as recognize:
            self.assert_code('speech_timeout', lambda: self.call(body()))
            self.assertFalse(speech._active)
            self.assertEqual(self.call(body())['text'], 'retained result')
            self.assertEqual(recognize.call_count, 2)

    def test_provider_slot_timeout_is_explicit_and_clears_pending(self):
        slot = Mock()
        slot.acquire.return_value = False
        with patch.object(speech, '_slot', slot), patch.object(speech, '_recognize') as recognize:
            self.assert_code('speech_busy', lambda: self.call(body()))
            slot.release.assert_not_called()
            recognize.assert_not_called()
            self.assertFalse(speech._active)

    def test_duplicate_in_flight_request_never_calls_provider_twice(self):
        entered, release = threading.Event(), threading.Event()
        result = []
        def recognize(*_):
            entered.set()
            release.wait(3)
            return 'one result'
        with patch.object(speech, '_recognize', side_effect=recognize) as provider:
            thread = threading.Thread(target=lambda: result.append(self.call(body())))
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assert_code('speech_busy', lambda: self.call(body()))
            finally:
                release.set()
                thread.join(3)
            self.assertEqual(provider.call_count, 1)
            self.assertEqual(len(result), 1)

    def test_missing_configuration_is_not_available(self):
        with patch.object(speech, 'speech_available', return_value=False):
            self.assert_code('speech_not_configured', lambda: self.call(body()))

    def test_logs_only_metadata(self):
        with patch.object(speech, '_recognize', return_value='PRIVATE_TRANSCRIPT'), \
                self.assertLogs(speech._logger, level='INFO') as logs:
            self.call(body(), actor='PRIVATE_ACTOR')
        logged = '\n'.join(logs.output)
        self.assertNotIn('PRIVATE_TRANSCRIPT', logged)
        self.assertNotIn('PRIVATE_ACTOR', logged)
        self.assertIn('status=success', logged)


if __name__ == '__main__':
    unittest.main()
