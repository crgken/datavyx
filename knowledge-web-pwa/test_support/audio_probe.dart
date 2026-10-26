// Isolated media verification target. Excluded from the normal application.
import 'dart:async';
import 'dart:js_interop';
import 'dart:typed_data';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:web/web.dart' as web;
import 'package:knowledge_web_pwa/data/knowledge_api.dart';
import 'package:knowledge_web_pwa/data/voice_controller.dart';

@JS('atlasProbeResult')
external set _result(JSAny? value);

class ProbeApi extends KnowledgeApi {
  Uint8List? audio;
  @override
  Future<String> transcribe(Uint8List wav, {required String recordingId,
    required String recognitionLanguage}) async {
    audio = wav;
    final response = await http.post(Uri.parse('http://localhost:8098/$recognitionLanguage'),
      body: wav, headers: {'Content-Type': 'application/octet-stream'});
    if (response.statusCode != 200) throw const ApiFailure('collector_failed');
    return 'Media probe only; no speech or business API was called.';
  }
  @override
  Future<Json> execute(String operation, [Json payload = const {}]) async =>
      throw StateError('Media probe cannot execute business operations');
}

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(const MaterialApp(home: AudioProbe()));
}

class AudioProbe extends StatefulWidget {
  const AudioProbe({super.key});
  @override
  State<AudioProbe> createState() => _AudioProbeState();
}

class _AudioProbeState extends State<AudioProbe> {
  final api = ProbeApi();
  late final voice = VoiceController(api, (_) async {});
  final peaks = <double>[];
  String status = 'Checking controlled microphone input…';
  @override
  void initState() {
    super.initState();
    voice.addListener(() {
      if (voice.phase == VoicePhase.recording) peaks.add(voice.amplitudes.last);
    });
    WidgetsBinding.instance.addPostFrameCallback((_) => runProbe());
  }
  Future<void> runProbe() async {
    final report = <String, Object?>{'fixture_type': 'controlled synthetic browser microphone'};
    try {
      final language = Uri.base.queryParameters['language'] ?? 'zh';
      if (!{'zh', 'en'}.contains(language)) throw const ApiFailure('invalid_language');
      api.language = language;
      api.session = {'capabilities': {'speech': true}};
      if (!web.window.isSecureContext) throw const ApiFailure('insecure_context');
      await voice.start();
      if (voice.phase != VoicePhase.recording) throw voice.error ?? const ApiFailure('recording_failed');
      await Future<void>.delayed(const Duration(seconds: 15));
      await voice.finish();
      if (voice.error != null) throw voice.error!;
      final bytes = api.audio;
      if (bytes == null || bytes.length < 44) throw const ApiFailure('invalid_audio');
      final header = ByteData.sublistView(bytes);
      report.addAll({
        'language': language, 'bytes': bytes.length,
        'sample_rate': header.getUint32(24, Endian.little),
        'channels': header.getUint16(22, Endian.little),
        'bits': header.getUint16(34, Endian.little),
        'audio_seconds': (bytes.length - 44) / 32000,
        'live_wave': peaks.any((n) => n > .1),
        'wave_changed': peaks.toSet().length > 1,
        'returned_idle': voice.phase == VoicePhase.idle,
      });
      report['passed'] = report['sample_rate'] == 16000 && report['channels'] == 1 &&
          report['bits'] == 16 && report['live_wave'] == true && report['wave_changed'] == true &&
          report['returned_idle'] == true;
    } catch (error) {
      report['passed'] = false;
      report['error'] = error is ApiFailure ? error.code : error.runtimeType.toString();
    } finally {
      await voice.discard();
      _result = report.jsify();
      if (mounted) setState(() => status = report.toString());
    }
  }
  @override
  Widget build(BuildContext context) => Scaffold(
    body: Center(child: Padding(padding: const EdgeInsets.all(24), child: Text(status))));
  @override
  void dispose() { voice.dispose(); api.dispose(); super.dispose(); }
}
