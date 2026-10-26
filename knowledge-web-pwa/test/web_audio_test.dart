@TestOn('browser')
library;

import 'dart:js_interop';
import 'dart:typed_data';
import 'package:flutter_test/flutter_test.dart';
import 'package:record/record.dart';
import 'package:web/web.dart' as web;
import 'package:knowledge_web_pwa/data/recorded_wav.dart';

/// Run only in an isolated browser using a controlled WAV microphone fixture.
/// This probe never authenticates, sends a chat or calls a knowledge API.
void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  test('browser microphone produces 16kHz mono PCM WAV and live levels', () async {
    const language = String.fromEnvironment('PROBE_LANGUAGE', defaultValue: 'zh');
    final recorder = AudioRecorder();
    final levels = <double>[];
    try {
      expect(web.window.isSecureContext, isTrue);
      expect(await recorder.hasPermission(), isTrue);
      await recorder.start(const RecordConfig(encoder: AudioEncoder.wav,
        sampleRate: 16000, numChannels: 1), path: '');
      final subscription = recorder.onAmplitudeChanged(const Duration(milliseconds: 100))
          .listen((level) => levels.add(level.current));
      await Future<void>.delayed(const Duration(seconds: 15));
      await subscription.cancel();
      final blob = await recorder.stop();
      expect(blob?.startsWith('blob:'), isTrue);
      late Uint8List bytes;
      try {
        final response = await web.window.fetch(blob!.toJS).toDart;
        bytes = (await response.arrayBuffer().toDart).toDart.asUint8List();
      } finally { web.URL.revokeObjectURL(blob!); }
      bytes = limitRecordedWav(bytes, const Duration(seconds: 60));
      final header = ByteData.sublistView(bytes);
      expect(String.fromCharCodes(bytes.sublist(0, 4)), 'RIFF');
      expect(String.fromCharCodes(bytes.sublist(8, 12)), 'WAVE');
      expect(header.getUint16(20, Endian.little), 1);
      expect(header.getUint16(22, Endian.little), 1);
      expect(header.getUint32(24, Endian.little), 16000);
      expect(header.getUint16(34, Endian.little), 16);
      expect(bytes.length, greaterThan(8 * 32000));
      expect(bytes.length, lessThanOrEqualTo(60 * 32000 + 44));
      expect(levels.where((n) => n.isFinite && n > -55), isNotEmpty);
      final request = web.RequestInit(method: 'POST', body: bytes.toJS);
      final stored = await web.window.fetch('http://localhost:8098/$language'.toJS, request).toDart;
      expect(stored.status, 200);
      expect(await recorder.isRecording(), isFalse);
    } finally { await recorder.cancel(); await recorder.dispose(); }
  }, timeout: const Timeout(Duration(seconds: 55)));
}
