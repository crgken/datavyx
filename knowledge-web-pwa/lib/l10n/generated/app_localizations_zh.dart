// ignore: unused_import
import 'package:intl/intl.dart' as intl;
import 'app_localizations.dart';

// ignore_for_file: type=lint

/// The translations for Chinese (`zh`).
class AppLocalizationsZh extends AppLocalizations {
  AppLocalizationsZh([String locale = 'zh']) : super(locale);

  @override
  String get appTitle => '知识助手';

  @override
  String get login => '登录 / 注册';

  @override
  String get logout => '退出账号';

  @override
  String get username => '账号';

  @override
  String get password => '密码';

  @override
  String get loginHint => '通过 Auth0 安全登录，访问你的知识工作空间。';

  @override
  String get authNotConfigured => '当前网页尚未配置登录服务，请联系管理员。';

  @override
  String get loginCancelled => '已取消登录，可以重新操作。';

  @override
  String get loginFailed => '登录未完成，请重试；若持续失败，请联系管理员检查登录配置。';

  @override
  String get loginAccessDenied => '登录授权被拒绝，请重试或联系管理员检查登录授权配置。';

  @override
  String get identityUnavailable => '身份验证服务暂不可用，请稍后重试。';

  @override
  String get serviceUnavailable => '知识服务暂不可用，请稍后重试；若持续失败，请联系管理员。';

  @override
  String get serviceNotConfigured => '当前网页的服务连接配置异常，请联系管理员。';

  @override
  String get workspaceAccessRequired => '账号尚未获得工作空间访问权限，请联系管理员。';

  @override
  String get switchWorkspace => '切换工作空间';

  @override
  String get roleViewer => '查看者';

  @override
  String get roleEditor => '录入者';

  @override
  String get roleReviewer => '审核者';

  @override
  String get rateLimited => '请求过于频繁，请稍后重试。';

  @override
  String get settings => '设置';

  @override
  String get account => '账号';

  @override
  String get language => '语言';

  @override
  String get chinese => '简体中文';

  @override
  String get english => 'English';

  @override
  String get newConversation => '新建会话';

  @override
  String get history => '会话历史';

  @override
  String get conversation => '对话';

  @override
  String get objects => '对象';

  @override
  String get conversationObjects => '当前会话对象';

  @override
  String get noMessages => '从一句话开始，记录或询问你的知识。';

  @override
  String get noObjects => '当前会话暂无可浏览对象';

  @override
  String get noRelations => '暂无关联对象';

  @override
  String get noConversations => '暂无历史会话';

  @override
  String get relatedObjects => '关联对象';

  @override
  String get relationCount => '关系';

  @override
  String get confirmed => '已确认';

  @override
  String get pending => '待确认';

  @override
  String get hasPending => '有待确认变更';

  @override
  String get incoming => '入向';

  @override
  String get outgoing => '出向';

  @override
  String get browseHint => '点击对象，逐层查看关联';

  @override
  String get browseScope => '当前可见、未归档记录，包含待确认信息';

  @override
  String get holdToTalk => '按住说话';

  @override
  String get typeText => '输入文字';

  @override
  String get inputHint => '输入你想记录或询问的内容…';

  @override
  String get send => '发送';

  @override
  String get sending => '处理中…';

  @override
  String get releaseToFinish => '松手完成录音';

  @override
  String get releaseToCancel => '松开取消';

  @override
  String get slideToCancel => '滑至此处取消';

  @override
  String get editBeforeSend => '识别后可编辑，再发送';

  @override
  String get recognizing => '正在识别…';

  @override
  String get transcript => '识别文字';

  @override
  String get discard => '放弃';

  @override
  String get mergeDraft => '合并到原草稿';

  @override
  String get draftPreserved => '原有文字草稿已保留';

  @override
  String get recordingCancelled => '本次录音已取消，未发送。';

  @override
  String get recordingInterrupted => '录音已中断，未发送。';

  @override
  String get microphoneDenied => '麦克风不可用或未获授权，请在系统设置中授权，也可使用文字输入。';

  @override
  String get speechUnavailable => '服务端尚未配置语音识别，可继续使用文字输入。';

  @override
  String get speechConfigInvalid => '语音服务认证失败，请联系管理员检查配置，可继续输入文字。';

  @override
  String get speechQuotaExceeded => '语音服务额度不足或访问被拒绝，请联系管理员，可继续输入文字。';

  @override
  String get speechBusy => '语音服务繁忙，请稍后重试。';

  @override
  String get speechServiceUnavailable => '语音识别暂不可用，请重试或输入文字。';

  @override
  String get speechTimeout => '语音识别超时，请重试或输入文字。';

  @override
  String get speechAudioExpired => '临时录音已过期并删除，请重新录音。';

  @override
  String get invalidAudio => '录音为空、格式无效或超过 60 秒，请重新录音。';

  @override
  String get recordingLimit => '接近 60 秒上限，即将自动结束录音。';

  @override
  String recordingRemaining(String time) {
    return '剩余 $time';
  }

  @override
  String get microphoneLevelUnavailable => '无法读取麦克风音量，请检查麦克风或重新录音。';

  @override
  String get emptyTranscript => '未识别到内容，请重新录音。';

  @override
  String get retry => '重试';

  @override
  String get refresh => '刷新';

  @override
  String get close => '关闭';

  @override
  String get back => '返回上一层';

  @override
  String get cancel => '取消';

  @override
  String get continueAction => '继续';

  @override
  String get loadFailed => '加载失败，已加载内容与草稿会保留。';

  @override
  String get networkError => '无法连接服务，请检查网络后重试；若持续失败，请联系管理员。';

  @override
  String get sessionExpired => '登录状态已失效，请重新登录。';

  @override
  String get notAvailable => '内容不可用或当前账号无访问权限。';

  @override
  String get invalidResponse => '服务返回格式异常，请稍后重试。';

  @override
  String get requestUncertain => '处理结果尚未确定，请查询原请求，不要重复提交。';

  @override
  String get recoverRequest => '查询处理结果';

  @override
  String get versionChanged => '数据已变化，请刷新当前列表后继续。';

  @override
  String get details => '完整信息';

  @override
  String get sources => '来源';

  @override
  String get name => '名称';

  @override
  String get identifier => '编号';

  @override
  String get kind => '类型';

  @override
  String get status => '状态';

  @override
  String get emptyValue => '未提供';

  @override
  String get pendingRule => '待确认信息可浏览和查询，不参与精准统计。';

  @override
  String get keepDraft => '保留当前草稿';

  @override
  String get finishInputFirst => '请先处理识别文字或等待当前请求结束。';

  @override
  String get saved => '已保存';

  @override
  String get connection => '连接';

  @override
  String get requestFailed => '操作失败，原始输入已保留。';

  @override
  String get resolveIdentity => '选择对象身份';

  @override
  String get createObject => '新建对象';

  @override
  String get useExisting => '使用已有对象';

  @override
  String get continuePreview => '继续处理';

  @override
  String get requestQueued => '请求已受理，正在处理';

  @override
  String get loadMore => '加载更多';

  @override
  String get pullUpToLoad => '上拉加载更多';

  @override
  String get releaseToLoad => '松开加载';

  @override
  String get loadingMore => '正在加载…';

  @override
  String get loadSucceeded => '加载完成';

  @override
  String get allLoaded => '已全部加载';

  @override
  String get loadFailedRetry => '加载失败，上拉重试';

  @override
  String get historyLoadFailedRetry => '加载失败，下拉重试';

  @override
  String get noListItems => '暂无记录，可下拉刷新。';

  @override
  String get startTalking => '按住麦克风开始录音';

  @override
  String get newConversationHint => '新会话不会删除已保存的信息。';
}
