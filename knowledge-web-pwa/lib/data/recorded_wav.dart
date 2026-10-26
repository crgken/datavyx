import 'dart:typed_data';

import 'api_failure.dart';

/// Remove stop-callback overshoot from this app's 16kHz mono PCM WAV.
/// The server still independently validates the actual frame count.
Uint8List limitRecordedWav(Uint8List bytes, Duration limit) {
  final maxBytes = limit.inSeconds * 16000 * 2;
  if (bytes.length <= maxBytes + 44) return bytes;
  final header = ByteData.sublistView(bytes);
  bool tag(int offset, String value) => offset + value.length <= bytes.length &&
      String.fromCharCodes(bytes.sublist(offset, offset + value.length)) == value;
  if (!tag(0, 'RIFF') || !tag(8, 'WAVE')) {
    throw const ApiFailure('invalid_audio');
  }
  bool pcm = false;
  for (int offset = 12; offset + 8 <= bytes.length;) {
    final size = header.getUint32(offset + 4, Endian.little);
    final start = offset + 8;
    if (start + size > bytes.length) throw const ApiFailure('invalid_audio');
    if (tag(offset, 'fmt ')) {
      pcm = size >= 16 && header.getUint16(start, Endian.little) == 1 &&
          header.getUint16(start + 2, Endian.little) == 1 &&
          header.getUint32(start + 4, Endian.little) == 16000 &&
          header.getUint16(start + 12, Endian.little) == 2 &&
          header.getUint16(start + 14, Endian.little) == 16;
    }
    if (tag(offset, 'data')) {
      if (!pcm || size.isOdd) throw const ApiFailure('invalid_audio');
      if (size <= maxBytes) return bytes;
      final trimmed = Uint8List.fromList(bytes.sublist(0, start + maxBytes));
      final lengths = ByteData.sublistView(trimmed);
      lengths.setUint32(4, trimmed.length - 8, Endian.little);
      lengths.setUint32(offset + 4, maxBytes, Endian.little);
      return trimmed;
    }
    offset = start + size + (size.isOdd ? 1 : 0);
  }
  throw const ApiFailure('invalid_audio');
}
