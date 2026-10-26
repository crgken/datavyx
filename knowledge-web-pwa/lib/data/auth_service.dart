import 'dart:async';
import 'package:auth0_flutter/auth0_flutter.dart' show Credentials;
import 'package:auth0_flutter/auth0_flutter_web.dart';
import 'api_failure.dart';

/// Only public SPA configuration; tokens stay in the official SDK memory cache.
class AuthService {
  static const domain = String.fromEnvironment('AUTH0_DOMAIN');
  static const clientId = String.fromEnvironment('AUTH0_CLIENT_ID');
  static const audience = String.fromEnvironment('AUTH0_AUDIENCE');
  static const organization = String.fromEnvironment('AUTH0_ORGANIZATION_ID');
  Auth0Web? _client;
  Future<String>? _refresh;
  bool _blocked = false;
  Map<String, String?> profile = {};
  bool get configured {
    final uri = Uri.tryParse('https://$domain');
    return domain.isNotEmpty && uri?.host == domain && uri!.path.isEmpty &&
      !uri.hasPort && uri.userInfo.isEmpty && !uri.hasQuery && !uri.hasFragment &&
      clientId.isNotEmpty && clientId != 'YOUR_WEB_SPA_CLIENT_ID' && audience.isNotEmpty;
  }
  String get callback => Uri.base.origin;
  Auth0Web get client {
    if (!configured) throw const ApiFailure('auth_not_configured');
    return _client ??= Auth0Web(domain, clientId, redirectUrl: callback,
      cacheLocation: CacheLocation.memory);
  }
  void _profile(Credentials credentials) {
    profile = {'name': credentials.user.name, 'email': credentials.user.email};
  }
  Future<bool> restore() async {
    if (!configured || _blocked) return false;
    try {
      final credentials = await client.onLoad(audience: audience,
        scopes: {'openid', 'profile', 'email', 'offline_access'},
        useRefreshTokens: true, useRefreshTokensFallback: false,
        authorizeTimeoutInSeconds: 20, httpTimeoutInSeconds: 20);
      if (credentials == null) return false;
      _profile(credentials);
      return true;
    } on WebException catch (error) {
      if (['login_required', 'consent_required', 'missing_refresh_token'].contains(error.code)) return false;
      throw _failure(error);
    }
  }
  Future<void> login(String language) async {
    _blocked = false;
    try {
      await client.loginWithRedirect(audience: audience, redirectUrl: callback,
        organizationId: organization.isEmpty ? null : organization,
        scopes: {'openid', 'profile', 'email', 'offline_access'},
        parameters: {
          'ui_locales': language == 'zh' ? 'zh-CN' : 'en',
          'screen_hint': 'signup',
          'prompt': 'login',
        });
      // Callback restores the session in the next document, not this old page.
      await Completer<void>().future;
    } on WebException catch (error) { throw _failure(error); }
  }
  Future<String> token() {
    if (_blocked) throw const ApiFailure('session_expired');
    return _refresh ??= _token().whenComplete(() => _refresh = null);
  }
  Future<String> _token() async {
    try {
      final credentials = await client.credentials(audience: audience, timeoutInSeconds: 20);
      _profile(credentials);
      return credentials.accessToken;
    } on WebException catch (error) { throw _failure(error); }
  }
  ApiFailure _failure(WebException error) => ApiFailure(switch (error.code) {
    'login_required' || 'missing_refresh_token' || 'invalid_grant' => 'session_expired',
    'access_denied' => 'login_access_denied',
    'timeout' || 'network_error' || 'request_error' => 'identity_unavailable',
    _ => 'login_failed',
  });
  Future<void> logout() async {
    _blocked = true;
    profile = {};
    if (configured) await client.logout(returnToUrl: callback);
  }
  Future<void> clear() async { _blocked = true; profile = {}; }
}
