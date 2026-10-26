import 'package:flutter/material.dart';

import '../data/voice_controller.dart';
import 'common.dart';

class RecordingFeedback extends StatelessWidget {
  final VoiceController voice;
  const RecordingFeedback({super.key, required this.voice});

  @override
  Widget build(BuildContext context) => AnimatedBuilder(
    animation: voice,
    builder: (context, _) {
      final s = context.l10n;
      final remaining = voice.remainingSeconds;
      return Column(mainAxisSize: MainAxisSize.min, children: [
        ExcludeSemantics(child: SizedBox(
          height: 54,
          child: Row(mainAxisAlignment: MainAxisAlignment.center, children: [
            for (int i = 0; i < voice.amplitudes.length; i++)
              Expanded(child: Center(child: AnimatedContainer(
                key: ValueKey('wave-bar-$i'),
                duration: MediaQuery.disableAnimationsOf(context)
                    ? Duration.zero : const Duration(milliseconds: 100),
                curve: Curves.easeOut,
                width: 4,
                height: 6 + voice.amplitudes[i] * 45,
                decoration: BoxDecoration(
                  color: voice.cancelled ? Palette.danger : Palette.teal,
                  borderRadius: BorderRadius.circular(4),
                ),
              ))),
          ]),
        )),
        const SizedBox(height: 6),
        Text(
          s.recordingRemaining('00:${remaining.toString().padLeft(2, '0')}'),
          key: const ValueKey('recording-countdown'),
          style: TextStyle(fontSize: 28, fontWeight: FontWeight.w600,
            color: remaining <= 10 ? Palette.warning : Palette.ink),
        ),
        if (remaining <= 10)
          Text(s.recordingLimit, style: const TextStyle(color: Palette.warning)),
        if (voice.amplitudeUnavailable)
          Text(s.microphoneLevelUnavailable, textAlign: TextAlign.center,
            style: const TextStyle(color: Palette.warning)),
      ]);
    },
  );
}
