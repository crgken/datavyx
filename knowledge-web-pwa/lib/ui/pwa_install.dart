import 'dart:js_interop';
import 'package:flutter/material.dart';
import 'package:web/web.dart' as web;
import 'common.dart';

@JS('atlasPwaInstall')
external JSPromise<JSString> _install();

Future<void> showInstall(BuildContext context) async {
  final zh = Localizations.localeOf(context).languageCode == 'zh';
  final result = (await _install().toDart).toDart;
  if (result != 'manual' || !context.mounted) return;
  final ios = RegExp(r'iPad|iPhone|iPod').hasMatch(web.window.navigator.userAgent) ||
      (web.window.navigator.platform == 'MacIntel' && web.window.navigator.maxTouchPoints > 1);
  await showModalBottomSheet<void>(context: context, showDragHandle: true,
    useSafeArea: true, builder: (_) => Padding(
      padding: const EdgeInsets.fromLTRB(24, 8, 24, 32),
      child: Text(zh
        ? (ios ? '请点击浏览器的分享按钮，选择“添加到主屏幕”。' : '请在浏览器菜单中选择“安装应用”或“添加到主屏幕”。')
        : (ios ? 'Open the browser Share menu and choose Add to Home Screen.'
          : 'Choose Install app or Add to Home Screen from the browser menu.'),
        style: const TextStyle(color: Palette.ink, fontSize: 16, height: 1.7)),
    ));
}
