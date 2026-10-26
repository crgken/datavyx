{{flutter_js}}
{{flutter_build_config}}
window.addEventListener('flutter-first-frame', () => document.getElementById('boot')?.remove());
_flutter.loader.load({config: {canvasKitBaseUrl: 'canvaskit/'}});
