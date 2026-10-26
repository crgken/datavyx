import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/widgets.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:intl/intl.dart' as intl;

import 'app_localizations_en.dart';
import 'app_localizations_zh.dart';

// ignore_for_file: type=lint

/// Callers can lookup localized strings with an instance of AppLocalizations
/// returned by `AppLocalizations.of(context)`.
///
/// Applications need to include `AppLocalizations.delegate()` in their app's
/// `localizationDelegates` list, and the locales they support in the app's
/// `supportedLocales` list. For example:
///
/// ```dart
/// import 'generated/app_localizations.dart';
///
/// return MaterialApp(
///   localizationsDelegates: AppLocalizations.localizationsDelegates,
///   supportedLocales: AppLocalizations.supportedLocales,
///   home: MyApplicationHome(),
/// );
/// ```
///
/// ## Update pubspec.yaml
///
/// Please make sure to update your pubspec.yaml to include the following
/// packages:
///
/// ```yaml
/// dependencies:
///   # Internationalization support.
///   flutter_localizations:
///     sdk: flutter
///   intl: any # Use the pinned version from flutter_localizations
///
///   # Rest of dependencies
/// ```
///
/// ## iOS Applications
///
/// iOS applications define key application metadata, including supported
/// locales, in an Info.plist file that is built into the application bundle.
/// To configure the locales supported by your app, you’ll need to edit this
/// file.
///
/// First, open your project’s ios/Runner.xcworkspace Xcode workspace file.
/// Then, in the Project Navigator, open the Info.plist file under the Runner
/// project’s Runner folder.
///
/// Next, select the Information Property List item, select Add Item from the
/// Editor menu, then select Localizations from the pop-up menu.
///
/// Select and expand the newly-created Localizations item then, for each
/// locale your application supports, add a new item and select the locale
/// you wish to add from the pop-up menu in the Value field. This list should
/// be consistent with the languages listed in the AppLocalizations.supportedLocales
/// property.
abstract class AppLocalizations {
  AppLocalizations(String locale)
    : localeName = intl.Intl.canonicalizedLocale(locale.toString());

  final String localeName;

  static AppLocalizations of(BuildContext context) {
    return Localizations.of<AppLocalizations>(context, AppLocalizations)!;
  }

  static const LocalizationsDelegate<AppLocalizations> delegate =
      _AppLocalizationsDelegate();

  /// A list of this localizations delegate along with the default localizations
  /// delegates.
  ///
  /// Returns a list of localizations delegates containing this delegate along with
  /// GlobalMaterialLocalizations.delegate, GlobalCupertinoLocalizations.delegate,
  /// and GlobalWidgetsLocalizations.delegate.
  ///
  /// Additional delegates can be added by appending to this list in
  /// MaterialApp. This list does not have to be used at all if a custom list
  /// of delegates is preferred or required.
  static const List<LocalizationsDelegate<dynamic>> localizationsDelegates =
      <LocalizationsDelegate<dynamic>>[
        delegate,
        GlobalMaterialLocalizations.delegate,
        GlobalCupertinoLocalizations.delegate,
        GlobalWidgetsLocalizations.delegate,
      ];

  /// A list of this localizations delegate's supported locales.
  static const List<Locale> supportedLocales = <Locale>[
    Locale('en'),
    Locale('zh'),
  ];

  /// No description provided for @appTitle.
  ///
  /// In zh, this message translates to:
  /// **'知识助手'**
  String get appTitle;

  /// No description provided for @login.
  ///
  /// In zh, this message translates to:
  /// **'登录 / 注册'**
  String get login;

  /// No description provided for @logout.
  ///
  /// In zh, this message translates to:
  /// **'退出账号'**
  String get logout;

  /// No description provided for @username.
  ///
  /// In zh, this message translates to:
  /// **'账号'**
  String get username;

  /// No description provided for @password.
  ///
  /// In zh, this message translates to:
  /// **'密码'**
  String get password;

  /// No description provided for @loginHint.
  ///
  /// In zh, this message translates to:
  /// **'通过 Auth0 安全登录，访问你的知识工作空间。'**
  String get loginHint;

  /// No description provided for @authNotConfigured.
  ///
  /// In zh, this message translates to:
  /// **'当前网页尚未配置登录服务，请联系管理员。'**
  String get authNotConfigured;

  /// No description provided for @loginCancelled.
  ///
  /// In zh, this message translates to:
  /// **'已取消登录，可以重新操作。'**
  String get loginCancelled;

  /// No description provided for @loginFailed.
  ///
  /// In zh, this message translates to:
  /// **'登录未完成，请重试；若持续失败，请联系管理员检查登录配置。'**
  String get loginFailed;

  /// No description provided for @loginAccessDenied.
  ///
  /// In zh, this message translates to:
  /// **'登录授权被拒绝，请重试或联系管理员检查登录授权配置。'**
  String get loginAccessDenied;

  /// No description provided for @identityUnavailable.
  ///
  /// In zh, this message translates to:
  /// **'身份验证服务暂不可用，请稍后重试。'**
  String get identityUnavailable;

  /// No description provided for @serviceUnavailable.
  ///
  /// In zh, this message translates to:
  /// **'知识服务暂不可用，请稍后重试；若持续失败，请联系管理员。'**
  String get serviceUnavailable;

  /// No description provided for @serviceNotConfigured.
  ///
  /// In zh, this message translates to:
  /// **'当前网页的服务连接配置异常，请联系管理员。'**
  String get serviceNotConfigured;

  /// No description provided for @workspaceAccessRequired.
  ///
  /// In zh, this message translates to:
  /// **'账号尚未获得工作空间访问权限，请联系管理员。'**
  String get workspaceAccessRequired;

  /// No description provided for @switchWorkspace.
  ///
  /// In zh, this message translates to:
  /// **'切换工作空间'**
  String get switchWorkspace;

  /// No description provided for @roleViewer.
  ///
  /// In zh, this message translates to:
  /// **'查看者'**
  String get roleViewer;

  /// No description provided for @roleEditor.
  ///
  /// In zh, this message translates to:
  /// **'录入者'**
  String get roleEditor;

  /// No description provided for @roleReviewer.
  ///
  /// In zh, this message translates to:
  /// **'审核者'**
  String get roleReviewer;

  /// No description provided for @rateLimited.
  ///
  /// In zh, this message translates to:
  /// **'请求过于频繁，请稍后重试。'**
  String get rateLimited;

  /// No description provided for @settings.
  ///
  /// In zh, this message translates to:
  /// **'设置'**
  String get settings;

  /// No description provided for @account.
  ///
  /// In zh, this message translates to:
  /// **'账号'**
  String get account;

  /// No description provided for @language.
  ///
  /// In zh, this message translates to:
  /// **'语言'**
  String get language;

  /// No description provided for @chinese.
  ///
  /// In zh, this message translates to:
  /// **'简体中文'**
  String get chinese;

  /// No description provided for @english.
  ///
  /// In zh, this message translates to:
  /// **'English'**
  String get english;

  /// No description provided for @newConversation.
  ///
  /// In zh, this message translates to:
  /// **'新建会话'**
  String get newConversation;

  /// No description provided for @history.
  ///
  /// In zh, this message translates to:
  /// **'会话历史'**
  String get history;

  /// No description provided for @conversation.
  ///
  /// In zh, this message translates to:
  /// **'对话'**
  String get conversation;

  /// No description provided for @objects.
  ///
  /// In zh, this message translates to:
  /// **'对象'**
  String get objects;

  /// No description provided for @conversationObjects.
  ///
  /// In zh, this message translates to:
  /// **'当前会话对象'**
  String get conversationObjects;

  /// No description provided for @noMessages.
  ///
  /// In zh, this message translates to:
  /// **'从一句话开始，记录或询问你的知识。'**
  String get noMessages;

  /// No description provided for @noObjects.
  ///
  /// In zh, this message translates to:
  /// **'当前会话暂无可浏览对象'**
  String get noObjects;

  /// No description provided for @noRelations.
  ///
  /// In zh, this message translates to:
  /// **'暂无关联对象'**
  String get noRelations;

  /// No description provided for @noConversations.
  ///
  /// In zh, this message translates to:
  /// **'暂无历史会话'**
  String get noConversations;

  /// No description provided for @relatedObjects.
  ///
  /// In zh, this message translates to:
  /// **'关联对象'**
  String get relatedObjects;

  /// No description provided for @relationCount.
  ///
  /// In zh, this message translates to:
  /// **'关系'**
  String get relationCount;

  /// No description provided for @confirmed.
  ///
  /// In zh, this message translates to:
  /// **'已确认'**
  String get confirmed;

  /// No description provided for @pending.
  ///
  /// In zh, this message translates to:
  /// **'待确认'**
  String get pending;

  /// No description provided for @hasPending.
  ///
  /// In zh, this message translates to:
  /// **'有待确认变更'**
  String get hasPending;

  /// No description provided for @incoming.
  ///
  /// In zh, this message translates to:
  /// **'入向'**
  String get incoming;

  /// No description provided for @outgoing.
  ///
  /// In zh, this message translates to:
  /// **'出向'**
  String get outgoing;

  /// No description provided for @browseHint.
  ///
  /// In zh, this message translates to:
  /// **'点击对象，逐层查看关联'**
  String get browseHint;

  /// No description provided for @browseScope.
  ///
  /// In zh, this message translates to:
  /// **'当前可见、未归档记录，包含待确认信息'**
  String get browseScope;

  /// No description provided for @holdToTalk.
  ///
  /// In zh, this message translates to:
  /// **'按住说话'**
  String get holdToTalk;

  /// No description provided for @typeText.
  ///
  /// In zh, this message translates to:
  /// **'输入文字'**
  String get typeText;

  /// No description provided for @inputHint.
  ///
  /// In zh, this message translates to:
  /// **'输入你想记录或询问的内容…'**
  String get inputHint;

  /// No description provided for @send.
  ///
  /// In zh, this message translates to:
  /// **'发送'**
  String get send;

  /// No description provided for @sending.
  ///
  /// In zh, this message translates to:
  /// **'处理中…'**
  String get sending;

  /// No description provided for @releaseToFinish.
  ///
  /// In zh, this message translates to:
  /// **'松手完成录音'**
  String get releaseToFinish;

  /// No description provided for @releaseToCancel.
  ///
  /// In zh, this message translates to:
  /// **'松开取消'**
  String get releaseToCancel;

  /// No description provided for @slideToCancel.
  ///
  /// In zh, this message translates to:
  /// **'滑至此处取消'**
  String get slideToCancel;

  /// No description provided for @editBeforeSend.
  ///
  /// In zh, this message translates to:
  /// **'识别后可编辑，再发送'**
  String get editBeforeSend;

  /// No description provided for @recognizing.
  ///
  /// In zh, this message translates to:
  /// **'正在识别…'**
  String get recognizing;

  /// No description provided for @transcript.
  ///
  /// In zh, this message translates to:
  /// **'识别文字'**
  String get transcript;

  /// No description provided for @discard.
  ///
  /// In zh, this message translates to:
  /// **'放弃'**
  String get discard;

  /// No description provided for @mergeDraft.
  ///
  /// In zh, this message translates to:
  /// **'合并到原草稿'**
  String get mergeDraft;

  /// No description provided for @draftPreserved.
  ///
  /// In zh, this message translates to:
  /// **'原有文字草稿已保留'**
  String get draftPreserved;

  /// No description provided for @recordingCancelled.
  ///
  /// In zh, this message translates to:
  /// **'本次录音已取消，未发送。'**
  String get recordingCancelled;

  /// No description provided for @recordingInterrupted.
  ///
  /// In zh, this message translates to:
  /// **'录音已中断，未发送。'**
  String get recordingInterrupted;

  /// No description provided for @microphoneDenied.
  ///
  /// In zh, this message translates to:
  /// **'麦克风不可用或未获授权，请在系统设置中授权，也可使用文字输入。'**
  String get microphoneDenied;

  /// No description provided for @speechUnavailable.
  ///
  /// In zh, this message translates to:
  /// **'服务端尚未配置语音识别，可继续使用文字输入。'**
  String get speechUnavailable;

  /// No description provided for @speechConfigInvalid.
  ///
  /// In zh, this message translates to:
  /// **'语音服务认证失败，请联系管理员检查配置，可继续输入文字。'**
  String get speechConfigInvalid;

  /// No description provided for @speechQuotaExceeded.
  ///
  /// In zh, this message translates to:
  /// **'语音服务额度不足或访问被拒绝，请联系管理员，可继续输入文字。'**
  String get speechQuotaExceeded;

  /// No description provided for @speechBusy.
  ///
  /// In zh, this message translates to:
  /// **'语音服务繁忙，请稍后重试。'**
  String get speechBusy;

  /// No description provided for @speechServiceUnavailable.
  ///
  /// In zh, this message translates to:
  /// **'语音识别暂不可用，请重试或输入文字。'**
  String get speechServiceUnavailable;

  /// No description provided for @speechTimeout.
  ///
  /// In zh, this message translates to:
  /// **'语音识别超时，请重试或输入文字。'**
  String get speechTimeout;

  /// No description provided for @speechAudioExpired.
  ///
  /// In zh, this message translates to:
  /// **'临时录音已过期并删除，请重新录音。'**
  String get speechAudioExpired;

  /// No description provided for @invalidAudio.
  ///
  /// In zh, this message translates to:
  /// **'录音为空、格式无效或超过 60 秒，请重新录音。'**
  String get invalidAudio;

  /// No description provided for @recordingLimit.
  ///
  /// In zh, this message translates to:
  /// **'接近 60 秒上限，即将自动结束录音。'**
  String get recordingLimit;

  /// No description provided for @recordingRemaining.
  ///
  /// In zh, this message translates to:
  /// **'剩余 {time}'**
  String recordingRemaining(String time);

  /// No description provided for @microphoneLevelUnavailable.
  ///
  /// In zh, this message translates to:
  /// **'无法读取麦克风音量，请检查麦克风或重新录音。'**
  String get microphoneLevelUnavailable;

  /// No description provided for @emptyTranscript.
  ///
  /// In zh, this message translates to:
  /// **'未识别到内容，请重新录音。'**
  String get emptyTranscript;

  /// No description provided for @retry.
  ///
  /// In zh, this message translates to:
  /// **'重试'**
  String get retry;

  /// No description provided for @refresh.
  ///
  /// In zh, this message translates to:
  /// **'刷新'**
  String get refresh;

  /// No description provided for @close.
  ///
  /// In zh, this message translates to:
  /// **'关闭'**
  String get close;

  /// No description provided for @back.
  ///
  /// In zh, this message translates to:
  /// **'返回上一层'**
  String get back;

  /// No description provided for @cancel.
  ///
  /// In zh, this message translates to:
  /// **'取消'**
  String get cancel;

  /// No description provided for @continueAction.
  ///
  /// In zh, this message translates to:
  /// **'继续'**
  String get continueAction;

  /// No description provided for @loadFailed.
  ///
  /// In zh, this message translates to:
  /// **'加载失败，已加载内容与草稿会保留。'**
  String get loadFailed;

  /// No description provided for @networkError.
  ///
  /// In zh, this message translates to:
  /// **'无法连接服务，请检查网络后重试；若持续失败，请联系管理员。'**
  String get networkError;

  /// No description provided for @sessionExpired.
  ///
  /// In zh, this message translates to:
  /// **'登录状态已失效，请重新登录。'**
  String get sessionExpired;

  /// No description provided for @notAvailable.
  ///
  /// In zh, this message translates to:
  /// **'内容不可用或当前账号无访问权限。'**
  String get notAvailable;

  /// No description provided for @invalidResponse.
  ///
  /// In zh, this message translates to:
  /// **'服务返回格式异常，请稍后重试。'**
  String get invalidResponse;

  /// No description provided for @requestUncertain.
  ///
  /// In zh, this message translates to:
  /// **'处理结果尚未确定，请查询原请求，不要重复提交。'**
  String get requestUncertain;

  /// No description provided for @recoverRequest.
  ///
  /// In zh, this message translates to:
  /// **'查询处理结果'**
  String get recoverRequest;

  /// No description provided for @versionChanged.
  ///
  /// In zh, this message translates to:
  /// **'数据已变化，请刷新当前列表后继续。'**
  String get versionChanged;

  /// No description provided for @details.
  ///
  /// In zh, this message translates to:
  /// **'完整信息'**
  String get details;

  /// No description provided for @sources.
  ///
  /// In zh, this message translates to:
  /// **'来源'**
  String get sources;

  /// No description provided for @name.
  ///
  /// In zh, this message translates to:
  /// **'名称'**
  String get name;

  /// No description provided for @identifier.
  ///
  /// In zh, this message translates to:
  /// **'编号'**
  String get identifier;

  /// No description provided for @kind.
  ///
  /// In zh, this message translates to:
  /// **'类型'**
  String get kind;

  /// No description provided for @status.
  ///
  /// In zh, this message translates to:
  /// **'状态'**
  String get status;

  /// No description provided for @emptyValue.
  ///
  /// In zh, this message translates to:
  /// **'未提供'**
  String get emptyValue;

  /// No description provided for @pendingRule.
  ///
  /// In zh, this message translates to:
  /// **'待确认信息可浏览和查询，不参与精准统计。'**
  String get pendingRule;

  /// No description provided for @keepDraft.
  ///
  /// In zh, this message translates to:
  /// **'保留当前草稿'**
  String get keepDraft;

  /// No description provided for @finishInputFirst.
  ///
  /// In zh, this message translates to:
  /// **'请先处理识别文字或等待当前请求结束。'**
  String get finishInputFirst;

  /// No description provided for @saved.
  ///
  /// In zh, this message translates to:
  /// **'已保存'**
  String get saved;

  /// No description provided for @connection.
  ///
  /// In zh, this message translates to:
  /// **'连接'**
  String get connection;

  /// No description provided for @requestFailed.
  ///
  /// In zh, this message translates to:
  /// **'操作失败，原始输入已保留。'**
  String get requestFailed;

  /// No description provided for @resolveIdentity.
  ///
  /// In zh, this message translates to:
  /// **'选择对象身份'**
  String get resolveIdentity;

  /// No description provided for @createObject.
  ///
  /// In zh, this message translates to:
  /// **'新建对象'**
  String get createObject;

  /// No description provided for @useExisting.
  ///
  /// In zh, this message translates to:
  /// **'使用已有对象'**
  String get useExisting;

  /// No description provided for @continuePreview.
  ///
  /// In zh, this message translates to:
  /// **'继续处理'**
  String get continuePreview;

  /// No description provided for @requestQueued.
  ///
  /// In zh, this message translates to:
  /// **'请求已受理，正在处理'**
  String get requestQueued;

  /// No description provided for @loadMore.
  ///
  /// In zh, this message translates to:
  /// **'加载更多'**
  String get loadMore;

  /// No description provided for @pullUpToLoad.
  ///
  /// In zh, this message translates to:
  /// **'上拉加载更多'**
  String get pullUpToLoad;

  /// No description provided for @releaseToLoad.
  ///
  /// In zh, this message translates to:
  /// **'松开加载'**
  String get releaseToLoad;

  /// No description provided for @loadingMore.
  ///
  /// In zh, this message translates to:
  /// **'正在加载…'**
  String get loadingMore;

  /// No description provided for @loadSucceeded.
  ///
  /// In zh, this message translates to:
  /// **'加载完成'**
  String get loadSucceeded;

  /// No description provided for @allLoaded.
  ///
  /// In zh, this message translates to:
  /// **'已全部加载'**
  String get allLoaded;

  /// No description provided for @loadFailedRetry.
  ///
  /// In zh, this message translates to:
  /// **'加载失败，上拉重试'**
  String get loadFailedRetry;

  /// No description provided for @historyLoadFailedRetry.
  ///
  /// In zh, this message translates to:
  /// **'加载失败，下拉重试'**
  String get historyLoadFailedRetry;

  /// No description provided for @noListItems.
  ///
  /// In zh, this message translates to:
  /// **'暂无记录，可下拉刷新。'**
  String get noListItems;

  /// No description provided for @startTalking.
  ///
  /// In zh, this message translates to:
  /// **'按住麦克风开始录音'**
  String get startTalking;

  /// No description provided for @newConversationHint.
  ///
  /// In zh, this message translates to:
  /// **'新会话不会删除已保存的信息。'**
  String get newConversationHint;
}

class _AppLocalizationsDelegate
    extends LocalizationsDelegate<AppLocalizations> {
  const _AppLocalizationsDelegate();

  @override
  Future<AppLocalizations> load(Locale locale) {
    return SynchronousFuture<AppLocalizations>(lookupAppLocalizations(locale));
  }

  @override
  bool isSupported(Locale locale) =>
      <String>['en', 'zh'].contains(locale.languageCode);

  @override
  bool shouldReload(_AppLocalizationsDelegate old) => false;
}

AppLocalizations lookupAppLocalizations(Locale locale) {
  // Lookup logic when only language code is specified.
  switch (locale.languageCode) {
    case 'en':
      return AppLocalizationsEn();
    case 'zh':
      return AppLocalizationsZh();
  }

  throw FlutterError(
    'AppLocalizations.delegate failed to load unsupported locale "$locale". This is likely '
    'an issue with the localizations generation tool. Please file an issue '
    'on GitHub with a reproducible sample app and the gen-l10n configuration '
    'that was used.',
  );
}
