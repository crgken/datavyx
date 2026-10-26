import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';
import 'dart:math';

import 'package:http/http.dart' as http;
import 'browser_store.dart';
import 'api_failure.dart';
import 'auth_service.dart';
export 'api_failure.dart';

typedef Json = Map<String, dynamic>;

class KnowledgeApi {
  final _storage = BrowserStore();
  final _http = http.Client();
  Uri? _origin;
  final auth = AuthService();
  String _workspace = '';
  Json? session;
  String language = 'zh';
  Future<void> Function()? onAuthenticationRequired;
  Future<void> Function()? onAccessRestricted;

  String get server => _origin?.toString() ?? '';
  bool get signedIn => session != null;
  String get storageScope => base64Url.encode(
    utf8.encode('$server|${session?['user']}|${session?['workspace']?['id']}'),
  );

  static String newRequestId() {
    final random = Random.secure();
    return base64Url
        .encode(List.generate(24, (_) => random.nextInt(256)))
        .replaceAll('=', '');
  }

  Future<String?> readLocal(String key) => _storage.read(key);
  Future<void> writeLocal(String key, String value) =>
      _storage.write(key, value);
  Future<void> deleteLocal(String key) => _storage.delete(key);

  Future<void> clearUserDrafts() => _storage.clearPrefix('draft:$storageScope:');

  Future<void> restore() async {
    language = await readLocal('language') ?? 'zh';
    // The deployment proxy fixes the API target; users cannot choose a server.
    _origin = Uri.parse(Uri.base.origin);
    final saved = await readLocal('connection');
    if (saved != null) {
      try {
        final data = jsonDecode(saved);
        if (data is Json && data['server'] == server && data['provider'] == 'auth0-web') {
          _workspace = '${data['workspace'] ?? ''}';
        }
      } on FormatException { await deleteLocal('connection'); }
    }
    // A first OAuth callback does not yet have a saved connection record.
    if (await auth.restore()) {
      await refreshSession();
      await writeLocal('connection', jsonEncode({
        'server': server, 'workspace': _workspace, 'provider': 'auth0-web'}));
    }
  }

  Future<void> login() async => auth.login(language);

  Future<void> refreshSession() async {
    final response = await _request('/api/mobile/session');
    final payload = _envelope(response);
    if (payload is! Json ||
        payload['user'] is! String ||
        payload['workspace'] is! Json || payload['permissions'] is! List ||
        payload['workspaces'] is! List) {
      throw const ApiFailure('invalid_response');
    }
    session = payload;
    _workspace = '${payload['workspace']['id']}';
  }

  Future<void> selectWorkspace(String id) async {
    final previous = _workspace;
    final previousSession = session;
    _workspace = id;
    try {
      await refreshSession();
      await writeLocal('connection', jsonEncode({
        'server': server, 'workspace': _workspace, 'provider': 'auth0-web',
      }));
    } catch (_) {
      _workspace = previous;
      session = previousSession;
      rethrow;
    }
  }

  Future<void> logout() async {
    try {
      await _request('/api/mobile/logout', body: const {});
    } finally {
      try { await auth.logout(); } finally { await clearSession(); }
    }
  }

  Future<void> clearSession() async {
    session = null;
    _workspace = '';
    try { await auth.clear(); } finally { await deleteLocal('connection'); }
  }

  Future<Json> execute(String operation, [Json payload = const {}]) async {
    final response = await _request(
      '/api/mobile/execute',
      body: {
        'operation': operation,
        'payload': {...payload, 'language': language},
      },
    );
    final data = _envelope(response);
    if (data is! Json) throw const ApiFailure('invalid_response');
    return data;
  }

  Future<String> transcribe(Uint8List wav, {
    required String recordingId,
    required String recognitionLanguage,
  }) async {
    final bytes = wav;
    final response = await _request(
      '/api/mobile/transcribe',
      body: {
        'audio': base64Encode(bytes),
        'language': recognitionLanguage,
        'recording_id': recordingId,
      },
      timeout: const Duration(seconds: 120),
    );
    final data = _envelope(response);
    if (data is! Json || data['text'] is! String ||
        data['recording_id'] != recordingId || data['language'] != recognitionLanguage) {
      throw const ApiFailure('invalid_response');
    }
    return (data['text'] as String).trim();
  }

  dynamic _envelope(Json response) {
    final result = response;
    if (result['ok'] is! bool) {
      throw const ApiFailure('invalid_response');
    }
    if (result['ok'] != true) {
      final errors = result['errors'];
      final first = errors is List && errors.isNotEmpty ? errors.first : null;
      throw ApiFailure(
        first is Json ? '${first['code']}' : 'operation_failed',
        first is Json ? '${first['message'] ?? ''}' : '',
      );
    }
    return result['data'];
  }

  Future<Json> _request(String path, {
    Json? body, Duration timeout = const Duration(seconds: 45),
  }) async {
    if (_origin == null) throw const ApiFailure('service_not_configured');
    try {
      final token = await auth.token();
      final request = http.Request(body == null ? 'GET' : 'POST', _origin!.resolve(path))
        ..followRedirects = false
        ..headers.addAll({'Accept': 'application/json', 'Accept-Language': language,
          'Authorization': 'Bearer $token',
          if (_workspace.isNotEmpty) 'X-Workspace-ID': _workspace});
      if (body != null) {
        request.headers['Content-Type'] = 'application/json; charset=utf-8';
        request.bodyBytes = utf8.encode(jsonEncode(body));
      }
      final response = await _http.send(request).timeout(timeout);
      final bytes = await response.stream.toBytes().timeout(timeout);
      if (response.statusCode == 401) throw const ApiFailure('session_expired');
      if (response.statusCode >= 300 && response.statusCode < 400) {
        throw const ApiFailure('unexpected_redirect');
      }
      final decoded = jsonDecode(utf8.decode(bytes));
      if (decoded is! Json) throw const ApiFailure('invalid_response');
      if (response.statusCode == 403 && decoded['errors'] is List &&
          (decoded['errors'] as List).any((e) => e is Map && e['code'] == 'workspace_access_required')) {
        await onAccessRestricted?.call();
      }
      if (response.statusCode >= 400) _envelope(decoded);
      return decoded;
    } on ApiFailure catch (error) {
      if (error.code == 'session_expired') await onAuthenticationRequired?.call();
      rethrow;
    } on TimeoutException {
      throw ApiFailure(path.endsWith('/transcribe') ? 'speech_timeout' : 'network_error');
    } on http.ClientException { throw const ApiFailure('network_error'); }
    on FormatException { throw const ApiFailure('invalid_response'); }
  }

  void dispose() => _http.close();
}
