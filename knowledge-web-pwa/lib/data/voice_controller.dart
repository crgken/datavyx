import 'dart:async';
import 'dart:js_interop';
import 'package:web/web.dart' as web;
import 'dart:math';

import 'package:flutter/foundation.dart';
import 'package:record/record.dart';

import 'knowledge_api.dart';
import 'recorded_wav.dart';

enum VoicePhase { idle, preparing, recording, stopping, recognizing, failed }

@JS('atlasRecordingBusy')
external set _recordingBusy(JSBoolean value);

class VoiceController extends ChangeNotifier {
  static const recordingLimit = Duration(seconds: 60);
  final KnowledgeApi api;
  final Future<void> Function(String text) onTranscript;
  final AudioRecorder _recorder;
  final String Function()? contextKey;
  VoiceController(this.api, this.onTranscript, {AudioRecorder? recorder, this.contextKey,
    Stopwatch? clock})
      : _recorder = recorder ?? AudioRecorder(), _clock = clock ?? Stopwatch();
  VoicePhase phase = VoicePhase.idle;
  Object? error;
  bool cancelled = false;
  bool _releaseWhilePreparing = false;
  bool _disposed = false;
  int _generation = 0;
  Duration elapsed = Duration.zero;
  bool amplitudeUnavailable = false;
  final List<double> amplitudes = List.filled(27, .08);
  Uint8List? _audio;
  String _recordingId = '';
  String _recognitionLanguage = '';
  String _scope = '';
  String? _context;
  Timer? _timer;
  Timer? _expiry;
  StreamSubscription<Amplitude>? _amplitude;
  final Stopwatch _clock;
  int get remainingSeconds => max(0, recordingLimit.inSeconds - elapsed.inSeconds);
  bool get active =>
      phase == VoicePhase.preparing || phase == VoicePhase.recording;
  bool get busy => phase != VoicePhase.idle && phase != VoicePhase.failed;
  bool get retryable => phase == VoicePhase.failed && _audio != null;

  bool _current(int generation) => !_disposed && generation == _generation &&
      _scope == api.storageScope && _recognitionLanguage == api.language &&
      _context == contextKey?.call();

  Future<bool> _accept(int generation) async {
    if (_current(generation)) return true;
    if (!_disposed && generation == _generation) await discard();
    return false;
  }

  // Web recordings are kept only in memory; page reload releases them.
  static Future<void> clearTemporaryRecordings() async {}

  void emit() {
    _recordingBusy = busy.toJS;
    if (!_disposed) notifyListeners();
  }

  Future<void> start() async {
    if (_disposed || busy) return;
    if (api.session?['capabilities']?['speech'] != true) {
      error = const ApiFailure('speech_not_configured');
      emit();
      return;
    }
    final generation = ++_generation;
    _recordingId = KnowledgeApi.newRequestId();
    _recognitionLanguage = api.language;
    _scope = api.storageScope;
    _context = contextKey?.call();
    _releaseWhilePreparing = false;
    cancelled = false;
    error = null;
    elapsed = Duration.zero;
    amplitudes.fillRange(0, amplitudes.length, .05);
    amplitudeUnavailable = false;
    phase = VoicePhase.preparing;
    emit();
    try {
      await _deleteAudio();
      if (!_current(generation) || _releaseWhilePreparing) {
        if (_disposed || generation != _generation) return;
        phase = VoicePhase.idle;
        emit();
        return;
      }
      if (!await _recorder.hasPermission()) {
        throw const ApiFailure('microphone_denied');
      }
      if (!_current(generation) || _releaseWhilePreparing) {
        if (_disposed || generation != _generation) return;
        phase = VoicePhase.idle;
        emit();
        return;
      }
      await _recorder.start(
        const RecordConfig(
          encoder: AudioEncoder.wav,
          sampleRate: 16000,
          numChannels: 1,
        ),
        path: '',
      );
      if (!_current(generation) || _releaseWhilePreparing) {
        await _recorder.cancel();
        if (_disposed || generation != _generation) return;
        await _deleteAudio();
        phase = VoicePhase.idle;
        emit();
        return;
      }
      phase = VoicePhase.recording;
      _clock
        ..reset()
        ..start();
      _amplitude = _recorder
          .onAmplitudeChanged(const Duration(milliseconds: 110))
          .listen((level) {
            if (!_current(generation) || phase != VoicePhase.recording) return;
            if (level.current.isNaN || level.current == double.infinity) {
              amplitudeUnavailable = true;
            } else {
              // This history has fixed length: shift values without remove/add.
              amplitudes.setRange(0, amplitudes.length - 1, amplitudes, 1);
              amplitudes.last = max(.05, min(1, (level.current + 55) / 55));
              amplitudeUnavailable = false;
            }
            emit();
          }, onError: (Object _) {
            if (!_current(generation) || phase != VoicePhase.recording) return;
            amplitudeUnavailable = true;
            emit();
          });
      _timer = Timer.periodic(const Duration(milliseconds: 200), (_) {
        if (!_current(generation)) {
          unawaited(discard());
          return;
        }
        elapsed = _clock.elapsed > recordingLimit ? recordingLimit : _clock.elapsed;
        if (elapsed >= recordingLimit) {
          unawaited(finish());
        } else {
          emit();
        }
      });
      emit();
    } catch (e) {
      if (!await _accept(generation)) return;
      await _recorder.cancel();
      await _deleteAudio();
      if (!_current(generation)) return;
      phase = VoicePhase.idle;
      error = e is ApiFailure ? e : const ApiFailure('microphone_denied');
      emit();
    }
  }

  void updateCancel(bool value) {
    if (phase != VoicePhase.recording || cancelled == value) return;
    cancelled = value;
    emit();
  }

  Future<void> finish({bool cancel = false}) async {
    if (phase == VoicePhase.preparing) {
      _releaseWhilePreparing = true;
      return;
    }
    if (phase != VoicePhase.recording) return;
    final generation = _generation;
    final shouldCancel = cancel || cancelled;
    phase = shouldCancel ? VoicePhase.stopping : VoicePhase.recognizing;
    _timer?.cancel();
    _clock.stop();
    await _amplitude?.cancel();
    if (!_current(generation)) return;
    if (shouldCancel) {
      await _recorder.cancel();
      await _deleteAudio();
      if (!_current(generation)) return;
      phase = VoicePhase.idle;
      emit();
      return;
    }
    emit();
    try {
      final path = await _recorder.stop();
      if (path == null || !path.startsWith('blob:')) throw const ApiFailure('invalid_audio');
      try {
        if (!await _accept(generation)) return;
        final response = await web.window.fetch(path.toJS).toDart;
        final buffer = await response.arrayBuffer().toDart;
        if (!await _accept(generation)) return;
        _audio = limitRecordedWav(buffer.toDart.asUint8List(), recordingLimit);
      } finally {
        web.URL.revokeObjectURL(path);
      }
      if (!await _accept(generation)) return;
      await _transcribe();
    } catch (e) {
      if (!await _accept(generation)) return;
      phase = VoicePhase.failed;
      error = e;
      _expireAudio();
      emit();
    }
  }

  Future<void> retry() async {
    if (!retryable) return;
    final generation = _generation;
    if (!_current(generation)) {
      await discard();
      return;
    }
    error = null;
    phase = VoicePhase.recognizing;
    emit();
    try {
      await _transcribe();
    } catch (e) {
      if (!await _accept(generation)) return;
      phase = VoicePhase.failed;
      error = e;
      _expireAudio();
      emit();
    }
  }

  Future<void> _transcribe() async {
    final generation = _generation;
    final audio = _audio!;
    final text = await api.transcribe(audio, recordingId: _recordingId,
        recognitionLanguage: _recognitionLanguage);
    if (!await _accept(generation)) return;
    if (text.isEmpty) throw const ApiFailure('empty_transcript');
    await onTranscript(text);
    if (!await _accept(generation)) return;
    await _deleteAudio();
    phase = VoicePhase.idle;
    error = null;
    emit();
  }

  void _expireAudio() {
    if (_expiry != null) return;
    _expiry = Timer(const Duration(minutes: 5), () async {
      _generation++;
      await _deleteAudio();
      if (!_disposed) {
        phase = VoicePhase.idle;
        error = const ApiFailure('speech_audio_expired');
        emit();
      }
    });
  }

  Future<void> _deleteAudio() async {
    _audio = null;
    _expiry?.cancel();
    _expiry = null;
  }

  Future<void> discard() async {
    final cancelRecorder = active || phase == VoicePhase.stopping;
    _generation++;
    phase = VoicePhase.stopping;
    _releaseWhilePreparing = true;
    _timer?.cancel();
    _clock.stop();
    await _amplitude?.cancel();
    if (cancelRecorder) await _recorder.cancel();
    await _deleteAudio();
    phase = VoicePhase.idle;
    error = null;
    emit();
  }

  @override
  void dispose() {
    _recordingBusy = false.toJS;
    _disposed = true;
    _generation++;
    _timer?.cancel();
    _expiry?.cancel();
    unawaited(_amplitude?.cancel());
    unawaited(_recorder.dispose());
    unawaited(_deleteAudio());
    super.dispose();
  }
}
