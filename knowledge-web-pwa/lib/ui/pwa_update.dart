import 'dart:js_interop';
import 'package:flutter/material.dart';
import 'package:web/web.dart' as web;
import 'common.dart';

@JS('atlasUpdateReady')
external JSBoolean? get _ready;
@JS('atlasRecordingBusy')
external JSBoolean? get _recordingBusy;
@JS('atlasUpdateRequested')
external set _requested(JSBoolean value);
@JS('atlasPwaUpdate')
external void _update();

class PwaUpdateFrame extends StatefulWidget {
  final Widget child;
  final Future<bool> Function() prepare;
  const PwaUpdateFrame({super.key, required this.child, required this.prepare});
  @override
  State<PwaUpdateFrame> createState() => _PwaUpdateFrameState();
}
class _PwaUpdateFrameState extends State<PwaUpdateFrame> {
  late final JSFunction listener;
  bool ready = false;
  bool busy = false;
  String? error;
  @override
  void initState() {
    super.initState();
    ready = _ready?.toDart ?? false;
    listener = ((web.Event _) { if (mounted) setState(() => ready = true); }).toJS;
    web.window.addEventListener('atlas-update-ready', listener);
  }
  Future<void> update() async {
    final zh = Localizations.localeOf(context).languageCode == 'zh';
    setState(() {busy = true;error = null;});
    try {
      if ((_recordingBusy?.toDart ?? false) || !await widget.prepare()) {
        if (mounted) setState(() => error = zh ? '请先完成录音或当前请求。' : 'Finish recording or the current request first.');
        return;
      }
      _requested = true.toJS;
      _update();
    } catch (_) {
      if (mounted) setState(() => error = zh ? '草稿未能保存，请稍后重试。' : 'Unable to save the draft. Try again.');
    } finally { if (mounted) setState(() => busy = false); }
  }
  @override
  Widget build(BuildContext context) {
    final zh = Localizations.localeOf(context).languageCode == 'zh';
    return Column(children: [
      if (ready) Material(color: Palette.soft, child: SafeArea(bottom: false,
        child: Padding(padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
          child: Row(children: [Expanded(child: Text(error ?? (zh ? '新版本已就绪' : 'An update is ready'))),
            TextButton(onPressed: busy ? null : update, child: Text(zh ? '保存并更新' : 'Save and update'))])))),
      Expanded(child: widget.child),
    ]);
  }
  @override
  void dispose() { web.window.removeEventListener('atlas-update-ready', listener); super.dispose(); }
}
