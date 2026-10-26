import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';

import 'data/knowledge_api.dart';
import 'data/session_controller.dart';
import 'data/voice_controller.dart';
import 'l10n/generated/app_localizations.dart';
import 'ui/common.dart';
import 'ui/conversation_page.dart';
import 'ui/pwa_update.dart';

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(const KnowledgeApp());
}

class KnowledgeApp extends StatefulWidget {
  const KnowledgeApp({super.key});
  @override
  State<KnowledgeApp> createState() => _KnowledgeAppState();
}

class _KnowledgeAppState extends State<KnowledgeApp> {
  final api = KnowledgeApi();
  SessionController? controller;
  bool restoring = true;
  Object? restoreError;
  Locale locale = const Locale('zh');
  final navigator = GlobalKey<NavigatorState>();
  bool expiring = false;

  @override
  void initState() {
    super.initState();
    api.onAuthenticationRequired = _expired;
    api.onAccessRestricted = () => _expired('workspace_access_required');
    _restore();
  }

  Future<void> _expired([String code = 'session_expired']) async {
    if (controller == null || expiring) return;
    expiring = true;
    try {
      try { await controller!.saveDraft(); }
      finally {
        try { await api.clearSession(); }
        finally {
          navigator.currentState?.popUntil((route) => route.isFirst);
          controller?.dispose();
          if (mounted) {
            setState(() {
              controller = null;
              restoreError = ApiFailure(code);
            });
          }
        }
      }
    } finally { expiring = false; }
  }

  Future<void> _restore() async {
    try {
      await VoiceController.clearTemporaryRecordings();
      await api.restore();
      if (api.signedIn) controller = SessionController(api);
    } catch (e) {
      restoreError = e;
    }
    if (!mounted) return;
    setState(() {
      locale = Locale(api.language);
      restoring = false;
    });
    if (controller != null) await controller!.start();
  }

  Future<void> _changeLanguage(String language) async {
    api.language = language;
    await api.writeLocal('language', language);
    if (mounted) setState(() => locale = Locale(language));
  }

  Future<void> _signedIn() async {
    controller?.dispose();
    final next = SessionController(api);
    setState(() => controller = next);
    await next.start();
  }

  Future<void> _workspace(String id) async {
    final current = controller;
    if (current == null || current.busy || current.loading) return;
    await current.saveDraft();
    await api.selectWorkspace(id);
    if (!mounted) return;
    navigator.currentState?.popUntil((route) => route.isFirst);
    await _signedIn();
  }

  Future<void> _signOut() async {
    final current = controller;
    if (current == null) return;
    Object? failure;
    try {
      await current.discardLocalData();
      await api.logout();
    } catch (e) {
      failure = e;
    } finally {
      try { await api.clearSession(); }
      catch (e) { failure ??= e; }
      finally {
        if (identical(controller, current)) current.dispose();
        if (mounted) {
          setState(() {
            controller = null;
            restoreError = failure;
          });
        }
      }
    }
  }

  @override
  Widget build(BuildContext context) => MaterialApp(
    navigatorKey: navigator,
    debugShowCheckedModeBanner: false,
    builder: (context, child) => ColoredBox(color: Palette.background,
      child: Center(child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 520),
        child: PwaUpdateFrame(prepare: () async {
          if (controller?.busy == true || controller?.loading == true) return false;
          await controller?.saveDraft();
          return true;
        }, child: child!)))),
    locale: locale,
    supportedLocales: AppLocalizations.supportedLocales,
    localizationsDelegates: const [
      AppLocalizations.delegate,
      GlobalMaterialLocalizations.delegate,
      GlobalWidgetsLocalizations.delegate,
      GlobalCupertinoLocalizations.delegate,
    ],
    onGenerateTitle: (context) => context.l10n.appTitle,
    theme: ThemeData(
      useMaterial3: true,
      colorScheme: ColorScheme.fromSeed(
        seedColor: Palette.teal,
        brightness: Brightness.light,
        surface: Colors.white,
      ),
      scaffoldBackgroundColor: Palette.background,
      appBarTheme: const AppBarTheme(
        backgroundColor: Colors.white,
        surfaceTintColor: Colors.transparent,
        centerTitle: false,
        titleTextStyle: TextStyle(
          color: Palette.ink,
          fontSize: 23,
          fontWeight: FontWeight.w600,
        ),
      ),
      filledButtonTheme: FilledButtonThemeData(
        style: FilledButton.styleFrom(
          backgroundColor: Palette.teal,
          foregroundColor: Colors.white,
          minimumSize: const Size(48, 50),
        ),
      ),
      inputDecorationTheme: InputDecorationTheme(
        filled: true,
        fillColor: Palette.background,
        border: OutlineInputBorder(
          borderRadius: BorderRadius.circular(14),
          borderSide: const BorderSide(color: Palette.line),
        ),
      ),
      dividerTheme: const DividerThemeData(color: Palette.line, thickness: 1),
    ),
    home: restoring
        ? const Scaffold(body: Center(child: CircularProgressIndicator()))
        : controller == null
        ? LoginPage(
            api: api,
            error: restoreError,
            onSignedIn: _signedIn,
            language: locale.languageCode,
            onLanguage: _changeLanguage,
          )
        : ConversationPage(
            key: ValueKey(api.storageScope),
            controller: controller!,
            onSignOut: _signOut,
            onLanguage: _changeLanguage,
            onWorkspace: _workspace,
          ),
  );

  @override
  void dispose() {
    controller?.dispose();
    api.dispose();
    super.dispose();
  }
}

class LoginPage extends StatefulWidget {
  final KnowledgeApi api;
  final Object? error;
  final Future<void> Function() onSignedIn;
  final String language;
  final Future<void> Function(String) onLanguage;
  const LoginPage({
    super.key,
    required this.api,
    this.error,
    required this.onSignedIn,
    required this.language,
    required this.onLanguage,
  });
  @override
  State<LoginPage> createState() => _LoginPageState();
}

class _LoginPageState extends State<LoginPage> {
  bool busy = false;
  Object? error;
  @override
  void initState() {
    super.initState();
    error = widget.error;
  }

  Future<void> signIn() async {
    if (busy) return;
    setState(() {
      busy = true;
      error = null;
    });
    try {
      await widget.api.login();
      await widget.onSignedIn();
    } catch (e) {
      if (mounted) setState(() => error = e);
    } finally {
      if (mounted) setState(() => busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final s = context.l10n;
    return Scaffold(
      appBar: AppBar(
        actions: [
          Padding(
            padding: const EdgeInsets.only(right: 12),
            child: DropdownButton<String>(
              value: widget.language,
              underline: const SizedBox(),
              onChanged: busy ? null : (value) => widget.onLanguage(value!),
              items: [
                DropdownMenuItem(value: 'zh', child: Text(s.chinese)),
                DropdownMenuItem(value: 'en', child: Text(s.english)),
              ],
            ),
          ),
        ],
      ),
      body: SafeArea(
        child: Center(
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 480),
            child: SingleChildScrollView(
              padding: const EdgeInsets.all(24),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  const Icon(
                    Icons.auto_awesome_outlined,
                    size: 44,
                    color: Palette.teal,
                  ),
                  const SizedBox(height: 18),
                  Text(
                    s.appTitle,
                    textAlign: TextAlign.center,
                    style: const TextStyle(
                      fontSize: 28,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  const SizedBox(height: 8),
                  Text(
                    s.loginHint,
                    textAlign: TextAlign.center,
                    style: const TextStyle(color: Palette.muted),
                  ),
                  const SizedBox(height: 30),
                  if (error != null) ...[
                    ErrorNotice(error: error!),
                    const SizedBox(height: 16),
                  ],
                  FilledButton(
                    onPressed: busy ? null : signIn,
                    child: busy
                        ? const SizedBox.square(
                            dimension: 20,
                            child: CircularProgressIndicator(
                              strokeWidth: 2,
                            ),
                          )
                        : Text(s.login),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }

}
