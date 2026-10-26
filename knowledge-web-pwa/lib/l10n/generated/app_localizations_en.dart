// ignore: unused_import
import 'package:intl/intl.dart' as intl;
import 'app_localizations.dart';

// ignore_for_file: type=lint

/// The translations for English (`en`).
class AppLocalizationsEn extends AppLocalizations {
  AppLocalizationsEn([String locale = 'en']) : super(locale);

  @override
  String get appTitle => 'Knowledge Assistant';

  @override
  String get login => 'Sign in / Sign up';

  @override
  String get logout => 'Sign out';

  @override
  String get username => 'Account';

  @override
  String get password => 'Password';

  @override
  String get loginHint =>
      'Sign in securely with Auth0 to access your knowledge workspace.';

  @override
  String get authNotConfigured =>
      'Auth0 access has not been configured for this build.';

  @override
  String get loginCancelled => 'Sign-in cancelled. You can try again.';

  @override
  String get loginFailed =>
      'Sign-in could not be completed. Try again; if it persists, ask your administrator to check the sign-in configuration.';

  @override
  String get loginAccessDenied =>
      'Sign-in authorization was denied. Try again or ask your administrator to check the authorization settings.';

  @override
  String get identityUnavailable =>
      'The identity verification service is temporarily unavailable. Try again shortly.';

  @override
  String get serviceUnavailable =>
      'The knowledge service is temporarily unavailable. Try again shortly; if it persists, contact your administrator.';

  @override
  String get serviceNotConfigured =>
      'The service connection is not configured correctly for this app. Contact your administrator.';

  @override
  String get workspaceAccessRequired =>
      'Your account has not been granted workspace access. Contact the administrator.';

  @override
  String get switchWorkspace => 'Switch workspace';

  @override
  String get roleViewer => 'Viewer';

  @override
  String get roleEditor => 'Editor';

  @override
  String get roleReviewer => 'Reviewer';

  @override
  String get rateLimited => 'Too many requests. Please try again shortly.';

  @override
  String get settings => 'Settings';

  @override
  String get account => 'Account';

  @override
  String get language => 'Language';

  @override
  String get chinese => '简体中文';

  @override
  String get english => 'English';

  @override
  String get newConversation => 'New conversation';

  @override
  String get history => 'Conversations';

  @override
  String get conversation => 'Conversation';

  @override
  String get objects => 'Objects';

  @override
  String get conversationObjects => 'Conversation objects';

  @override
  String get noMessages =>
      'Start with a message to record something or ask a question.';

  @override
  String get noObjects => 'No objects in this conversation';

  @override
  String get noRelations => 'No connected objects';

  @override
  String get noConversations => 'No conversations yet';

  @override
  String get relatedObjects => 'Related objects';

  @override
  String get relationCount => 'Relations';

  @override
  String get confirmed => 'Confirmed';

  @override
  String get pending => 'Unconfirmed';

  @override
  String get hasPending => 'Unconfirmed changes';

  @override
  String get incoming => 'Incoming';

  @override
  String get outgoing => 'Outgoing';

  @override
  String get browseHint => 'Select an object to explore its connections';

  @override
  String get browseScope =>
      'Visible, unarchived records, including unconfirmed information';

  @override
  String get holdToTalk => 'Hold to talk';

  @override
  String get typeText => 'Type';

  @override
  String get inputHint => 'Write something to record or ask…';

  @override
  String get send => 'Send';

  @override
  String get sending => 'Processing…';

  @override
  String get releaseToFinish => 'Release to finish recording';

  @override
  String get releaseToCancel => 'Release to cancel';

  @override
  String get slideToCancel => 'Slide here to cancel';

  @override
  String get editBeforeSend => 'Edit the transcript before sending';

  @override
  String get recognizing => 'Transcribing…';

  @override
  String get transcript => 'Transcript';

  @override
  String get discard => 'Discard';

  @override
  String get mergeDraft => 'Merge into text draft';

  @override
  String get draftPreserved => 'Your earlier text draft is preserved';

  @override
  String get recordingCancelled => 'Recording cancelled. Nothing was sent.';

  @override
  String get recordingInterrupted => 'Recording interrupted. Nothing was sent.';

  @override
  String get microphoneDenied =>
      'Microphone access is unavailable or denied. Enable it in system settings, or type instead.';

  @override
  String get speechUnavailable =>
      'Speech recognition is not configured on the server. You can still type.';

  @override
  String get speechConfigInvalid =>
      'Speech service authentication failed. Contact your administrator or type instead.';

  @override
  String get speechQuotaExceeded =>
      'The speech quota is exhausted or access was denied. Contact your administrator or type instead.';

  @override
  String get speechBusy =>
      'The speech service is busy. Please try again shortly.';

  @override
  String get speechServiceUnavailable =>
      'Speech recognition is temporarily unavailable. Please retry or type instead.';

  @override
  String get speechTimeout =>
      'Speech recognition timed out. Please retry or type instead.';

  @override
  String get speechAudioExpired =>
      'The temporary recording expired and was deleted. Please record again.';

  @override
  String get invalidAudio =>
      'The recording is empty, invalid, or longer than 60 seconds. Please record again.';

  @override
  String get recordingLimit =>
      'Approaching the 60-second limit. Recording will stop automatically.';

  @override
  String recordingRemaining(String time) {
    return 'Remaining $time';
  }

  @override
  String get microphoneLevelUnavailable =>
      'Unable to read microphone levels. Check the microphone or record again.';

  @override
  String get emptyTranscript => 'No speech recognized. Please try again.';

  @override
  String get retry => 'Retry';

  @override
  String get refresh => 'Refresh';

  @override
  String get close => 'Close';

  @override
  String get back => 'Go back';

  @override
  String get cancel => 'Cancel';

  @override
  String get continueAction => 'Continue';

  @override
  String get loadFailed =>
      'Unable to load. Existing content and drafts are preserved.';

  @override
  String get networkError =>
      'Unable to connect. Check your network and retry; if it persists, contact your administrator.';

  @override
  String get sessionExpired =>
      'Your session has expired. Please sign in again.';

  @override
  String get notAvailable => 'Content is unavailable or access is restricted.';

  @override
  String get invalidResponse =>
      'The service returned an unexpected response. Please retry later.';

  @override
  String get requestUncertain =>
      'The result is not yet known. Check the original request before submitting again.';

  @override
  String get recoverRequest => 'Check processing result';

  @override
  String get versionChanged =>
      'The data changed. Refresh this list to continue.';

  @override
  String get details => 'Full information';

  @override
  String get sources => 'Sources';

  @override
  String get name => 'Name';

  @override
  String get identifier => 'Identifier';

  @override
  String get kind => 'Type';

  @override
  String get status => 'Status';

  @override
  String get emptyValue => 'Not provided';

  @override
  String get pendingRule =>
      'Unconfirmed information is searchable and visible, but excluded from precise statistics.';

  @override
  String get keepDraft => 'Keep current draft';

  @override
  String get finishInputFirst =>
      'Finish editing the transcript or wait for the current request first.';

  @override
  String get saved => 'Saved';

  @override
  String get connection => 'Connection';

  @override
  String get requestFailed =>
      'The operation failed. Your original input is preserved.';

  @override
  String get resolveIdentity => 'Choose object identities';

  @override
  String get createObject => 'Create object';

  @override
  String get useExisting => 'Use existing object';

  @override
  String get continuePreview => 'Continue';

  @override
  String get requestQueued => 'Request accepted and processing';

  @override
  String get loadMore => 'Load more';

  @override
  String get pullUpToLoad => 'Pull up to load more';

  @override
  String get releaseToLoad => 'Release to load';

  @override
  String get loadingMore => 'Loading…';

  @override
  String get loadSucceeded => 'Loaded';

  @override
  String get allLoaded => 'All items loaded';

  @override
  String get loadFailedRetry => 'Unable to load. Pull up to retry';

  @override
  String get historyLoadFailedRetry => 'Unable to load. Pull down to retry';

  @override
  String get noListItems => 'No records yet. Pull down to refresh.';

  @override
  String get startTalking => 'Hold the microphone to start';

  @override
  String get newConversationHint =>
      'Starting a conversation does not delete saved information.';
}
