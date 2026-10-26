import 'package:flutter/material.dart';
import 'package:easy_refresh/easy_refresh.dart';
import '../data/knowledge_api.dart';
import '../l10n/generated/app_localizations.dart';

extension LocalStrings on BuildContext {
  AppLocalizations get l10n => AppLocalizations.of(this);
}

abstract final class Palette {
  static const teal = Color(0xff078b8e);
  static const soft = Color(0xffdef3f2);
  static const background = Color(0xfff5f9fa);
  static const ink = Color(0xff10212d);
  static const muted = Color(0xff647780);
  static const line = Color(0xffe5edef);
  static const warning = Color(0xff906311);
  static const warningFill = Color(0xfffff3d9);
  static const danger = Color(0xffd83c59);
  static const dangerFill = Color(0xfffff0f3);
}

ClassicFooter listFooter(BuildContext context) {
  final s = context.l10n;
  return ClassicFooter(
    infiniteOffset: 160,
    safeArea: false,
    showMessage: false,
    dragText: s.pullUpToLoad,
    armedText: s.releaseToLoad,
    readyText: s.loadingMore,
    processingText: s.loadingMore,
    processedText: s.loadSucceeded,
    noMoreText: s.allLoaded,
    failedText: s.loadFailedRetry,
    textStyle: const TextStyle(fontSize: 12, color: Palette.muted),
    iconTheme: const IconThemeData(color: Palette.teal, size: 18),
  );
}

bool accessLost(Object error) => error is ApiFailure && const [
  'permission_denied', 'not_available', 'object_not_found', 'session_expired',
].contains(error.code);

/// Shared indicator lifecycle; each caller keeps its own API and pagination.
class PagedRefresh extends StatefulWidget {
  final Future<bool> Function() onRefresh;
  final Future<bool> Function()? onLoad;
  final ERChildBuilder builder;
  final bool refreshOnStart;
  const PagedRefresh({
    super.key,
    required this.onRefresh,
    required this.builder,
    this.onLoad,
    this.refreshOnStart = true,
  });

  @override
  State<PagedRefresh> createState() => _PagedRefreshState();
}

class _PagedRefreshState extends State<PagedRefresh> {
  final controller = EasyRefreshController();
  bool loading = false;
  Object? error;
  bool blocked = false;

  Future<IndicatorResult> run({required bool reset}) async {
    if (loading) return IndicatorResult.none;
    final work = reset ? widget.onRefresh : widget.onLoad;
    if (work == null) return IndicatorResult.noMore;
    loading = true;
    setState(() => error = null);
    try {
      final hasMore = await work();
      if (!mounted) return IndicatorResult.none;
      setState(() => blocked = false);
      if (reset) {
        controller.resetFooter();
        if (!hasMore) controller.finishLoad(IndicatorResult.noMore, true);
        return IndicatorResult.success;
      }
      return hasMore ? IndicatorResult.success : IndicatorResult.noMore;
    } catch (e) {
      if (!mounted) return IndicatorResult.none;
      setState(() {
        blocked = accessLost(e) || e is ApiFailure && e.code == 'data_changed';
        // Transient append errors belong to the footer; keep existing rows.
        if (reset || blocked) error = e;
      });
      return IndicatorResult.fail;
    } finally {
      loading = false;
    }
  }

  @override
  Widget build(BuildContext context) => Column(
    children: [
      if (error != null)
        Padding(
          padding: const EdgeInsets.all(12),
          child: ErrorNotice(error: error!, onRetry: () => run(reset: true)),
        ),
      Expanded(
        child: EasyRefresh.builder(
          controller: controller,
          refreshOnStart: widget.refreshOnStart,
          resetAfterRefresh: false,
          canRefreshAfterNoMore: true,
          header: MaterialHeader(color: Palette.teal, semanticsLabel: context.l10n.refresh),
          footer: listFooter(context),
          onRefresh: () => run(reset: true),
          onLoad: blocked || widget.onLoad == null ? null : () => run(reset: false),
          childBuilder: widget.builder,
        ),
      ),
    ],
  );

  @override
  void dispose() {
    controller.dispose();
    super.dispose();
  }
}

String errorLabel(BuildContext context, Object error) {
  final s = context.l10n;
  if (error is! ApiFailure) return s.requestFailed;
  return switch (error.code) {
    'network_error' => s.networkError,
    'service_unavailable' => s.serviceUnavailable,
    'identity_unavailable' => s.identityUnavailable,
    'auth_not_configured' || 'mobile_not_configured' => s.authNotConfigured,
    'login_cancelled' => s.loginCancelled,
    'login_failed' => s.loginFailed,
    'login_access_denied' => s.loginAccessDenied,
    'workspace_access_required' => s.workspaceAccessRequired,
    'rate_limited' => s.rateLimited,
    'session_expired' => s.sessionExpired,
    'permission_denied' ||
    'not_available' ||
    'object_not_found' => s.notAvailable,
    'https_required' || 'invalid_server' || 'service_not_configured' => s.serviceNotConfigured,
    'data_changed' ||
    'conversation_changed' ||
    'preview_stale' => s.versionChanged,
    'speech_not_configured' => s.speechUnavailable,
    'speech_config_invalid' => s.speechConfigInvalid,
    'speech_quota_exceeded' => s.speechQuotaExceeded,
    'speech_busy' => s.speechBusy,
    'speech_unavailable' => s.speechServiceUnavailable,
    'speech_timeout' => s.speechTimeout,
    'speech_audio_expired' => s.speechAudioExpired,
    'invalid_audio' => s.invalidAudio,
    'speech_invalid_response' => s.invalidResponse,
    'empty_transcript' => s.emptyTranscript,
    'microphone_denied' => s.microphoneDenied,
    'invalid_response' => s.invalidResponse,
    'request_uncertain' || 'request_interrupted' => s.requestUncertain,
    _ => error.message.isNotEmpty ? error.message : s.requestFailed,
  };
}

String objectLabel(Json object) {
  final name = '${object['name'] ?? ''}';
  final identifier = '${object['identifier'] ?? ''}';
  return name.isNotEmpty
      ? name
      : identifier.isNotEmpty
      ? identifier
      : '${object['kind'] ?? ''}';
}

String factValue(dynamic value) {
  if (value is Map) {
    if (value['text'] != null) return '${value['text']}';
    if (value['number'] != null) {
      return '${value['number']} ${value['unit'] ?? ''}'.trim();
    }
    if (value['date'] != null) return '${value['date']}';
    if (value['boolean'] != null) return '${value['boolean']}';
    if (value['name'] != null) return '${value['name']}';
    if (value['identifier'] != null) return '${value['identifier']}';
  }
  return value == null ? '' : '$value';
}

class ErrorNotice extends StatelessWidget {
  final Object error;
  final VoidCallback? onRetry;
  final VoidCallback? onClose;
  const ErrorNotice({
    super.key,
    required this.error,
    this.onRetry,
    this.onClose,
  });
  @override
  Widget build(BuildContext context) => Material(
    color: Palette.dangerFill,
    borderRadius: BorderRadius.circular(12),
    child: Padding(
      padding: const EdgeInsets.fromLTRB(12, 8, 4, 8),
      child: Row(
        children: [
          const Icon(Icons.info_outline, color: Palette.danger, size: 21),
          const SizedBox(width: 9),
          Expanded(
            child: Text(
              errorLabel(context, error),
              style: const TextStyle(color: Palette.danger, fontSize: 13),
            ),
          ),
          if (onRetry != null)
            IconButton(
              onPressed: onRetry,
              tooltip: context.l10n.retry,
              icon: const Icon(Icons.refresh),
            ),
          if (onClose != null)
            IconButton(
              onPressed: onClose,
              tooltip: context.l10n.close,
              icon: const Icon(Icons.close),
            ),
        ],
      ),
    ),
  );
}

class StatusBadge extends StatelessWidget {
  final bool confirmed;
  final bool hasPending;
  const StatusBadge({
    super.key,
    required this.confirmed,
    this.hasPending = false,
  });
  @override
  Widget build(BuildContext context) => Wrap(
    spacing: 7,
    runSpacing: 6,
    children: [
      _tag(
        confirmed ? context.l10n.confirmed : context.l10n.pending,
        confirmed ? Icons.check_circle_outline : Icons.schedule,
        confirmed ? Palette.teal : Palette.warning,
        confirmed ? Palette.soft : Palette.warningFill,
      ),
      if (confirmed && hasPending)
        _tag(
          context.l10n.hasPending,
          Icons.edit_outlined,
          Palette.warning,
          Palette.warningFill,
        ),
    ],
  );
  Widget _tag(String text, IconData icon, Color color, Color fill) => Container(
    padding: const EdgeInsets.symmetric(horizontal: 7, vertical: 4),
    decoration: BoxDecoration(
      color: fill,
      borderRadius: BorderRadius.circular(7),
    ),
    child: Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        Icon(icon, size: 13, color: color),
        const SizedBox(width: 4),
        Flexible(
          child: Text(text, style: TextStyle(fontSize: 12, color: color)),
        ),
      ],
    ),
  );
}
