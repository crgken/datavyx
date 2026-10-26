import 'dart:async';

import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:easy_refresh/easy_refresh.dart';
import 'package:intl/intl.dart';

import '../data/knowledge_api.dart';
import '../data/session_controller.dart';
import '../data/voice_controller.dart';
import 'common.dart';
import 'object_browser.dart';
import 'recording_feedback.dart';
import 'pwa_install.dart';

class ConversationPage extends StatefulWidget {
  final SessionController controller;
  final Future<void> Function() onSignOut;
  final Future<void> Function(String) onLanguage;
  final Future<void> Function(String) onWorkspace;
  const ConversationPage({
    super.key,
    required this.controller,
    required this.onSignOut,
    required this.onLanguage,
    required this.onWorkspace,
  });
  @override
  State<ConversationPage> createState() => _ConversationPageState();
}

class _ConversationPageState extends State<ConversationPage>
    with WidgetsBindingObserver {
  SessionController get session => widget.controller;
  KnowledgeApi get api => session.api;
  late final VoiceController voice;
  final editor = TextEditingController();
  final transcriptEditor = TextEditingController();
  final scroll = ScrollController();
  final historyRefresh = EasyRefreshController();
  final scaffold = GlobalKey<ScaffoldState>();
  bool textMode = false;
  int? heldPointer;
  Offset? pointerStart;
  int previousMessageCount = 0;
  String? messageAnchor;
  String viewedConversation = '';
  bool readingHistory = false;
  Object? localError;

  @override
  void initState() {
    super.initState();
    voice = VoiceController(api, (text) async {
      session.transcript = text;
      await session.saveDraft();
      session.emit();
    }, contextKey: () => session.conversationId);
    voice.addListener(changed);
    session.addListener(changed);
    WidgetsBinding.instance.addObserver(this);
    GestureBinding.instance.pointerRouter.addGlobalRoute(routePointer);
    syncEditors();
    viewedConversation = session.conversationId;
    messageAnchor = session.messages.isEmpty ? null : '${session.messages.first['id']}';
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted && scroll.hasClients) scroll.jumpTo(scroll.position.maxScrollExtent);
    });
  }

  void syncEditors() {
    if (editor.text != session.draft) {
      editor.text = session.draft;
    }
    if (transcriptEditor.text != session.transcript) {
      transcriptEditor.text = session.transcript;
    }
  }

  void changed() {
    if (!mounted) return;
    syncEditors();
    if (viewedConversation != session.conversationId) {
      viewedConversation = session.conversationId;
      messageAnchor = null;
      previousMessageCount = 0;
      historyRefresh.resetHeader();
    }
    if (!session.messages.any((m) => m['id'] == messageAnchor)) {
      messageAnchor = session.messages.isEmpty ? null : '${session.messages.first['id']}';
    }
    final shouldScroll =
        !readingHistory &&
        session.messages.length > previousMessageCount &&
        (!scroll.hasClients ||
            scroll.position.extentAfter < 120 ||
            session.busy);
    previousMessageCount = session.messages.length;
    setState(() {});
    if (shouldScroll) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted && scroll.hasClients) {
          scroll.animateTo(
            scroll.position.maxScrollExtent,
            duration: MediaQuery.disableAnimationsOf(context)
                ? Duration.zero
                : const Duration(milliseconds: 180),
            curve: Curves.easeOut,
          );
        }
      });
    }
  }

  Future<IndicatorResult> refreshHistory() async {
    if (readingHistory || session.loading || session.busy) return IndicatorResult.none;
    final id = session.conversationId;
    readingHistory = true;
    setState(() => localError = null);
    try {
      // A top-edge gesture refreshes the latest window as well as fetching
      // older history. The centered slivers keep the visible message anchored.
      await session.refresh(preserveHistory: true);
      if (!mounted || id != session.conversationId) return IndicatorResult.none;
      await session.olderMessages();
      return IndicatorResult.success;
    } catch (e) {
      if (mounted && id == session.conversationId) {
        if (accessLost(e)) {
          session.messages.clear();
          session.hasOlderMessages = false;
          session.response = null;
          session.objectCount = null;
          session.emit();
        }
        setState(() => localError = e);
      }
      return IndicatorResult.fail;
    } finally {
      readingHistory = false;
    }
  }

  Widget messageList(ScrollPhysics physics) {
    final center = ValueKey('messages:${session.conversationId}');
    final anchor = session.messages.indexWhere((m) => m['id'] == messageAnchor);
    final split = anchor < 0 ? 0 : anchor;
    Widget bubble(int index) => KeyedSubtree(
      key: ValueKey(session.messages[index]['id']),
      child: messageBubble(session.messages[index]),
    );
    return CustomScrollView(
      controller: scroll,
      physics: physics,
      center: center,
      slivers: [
        if (!session.hasOlderMessages && session.messages.isNotEmpty)
          SliverToBoxAdapter(child: Padding(
            padding: const EdgeInsets.all(12),
            child: Text(context.l10n.allLoaded, textAlign: TextAlign.center,
              style: const TextStyle(fontSize: 12, color: Palette.muted)),
          )),
        SliverPadding(
          padding: const EdgeInsets.symmetric(horizontal: 16),
          sliver: SliverList.builder(
            itemCount: split,
            itemBuilder: (_, index) => bubble(split - index - 1),
          ),
        ),
        SliverPadding(
          key: center,
          padding: const EdgeInsets.fromLTRB(16, 22, 16, 12),
          sliver: session.messages.isEmpty
              ? SliverFillRemaining(
                  hasScrollBody: false,
                  child: Center(child: Padding(
                    padding: const EdgeInsets.all(32),
                    child: Text(context.l10n.noMessages, textAlign: TextAlign.center,
                      style: const TextStyle(color: Palette.muted, height: 1.7)),
                  )),
                )
              : SliverList.builder(
                  itemCount: session.messages.length - split,
                  itemBuilder: (_, index) => bubble(split + index),
                ),
        ),
      ],
    );
  }

  void startVoice(PointerDownEvent event) {
    if (heldPointer != null ||
        voice.busy ||
        session.busy ||
        session.transcript.isNotEmpty) {
      if (!voice.active) {
        setState(
          () => localError = ApiFailure(
            'finish_input',
            context.l10n.finishInputFirst,
          ),
        );
      }
      return;
    }
    heldPointer = event.pointer;
    pointerStart = event.position;
    FocusManager.instance.primaryFocus?.unfocus();
    unawaited(voice.start());
  }

  void routePointer(PointerEvent event) {
    if (event.pointer != heldPointer) return;
    if (event is PointerMoveEvent && pointerStart != null) {
      voice.updateCancel(event.position.dx < pointerStart!.dx - 72);
    }
    if (event is PointerUpEvent || event is PointerCancelEvent) {
      heldPointer = null;
      unawaited(voice.finish(cancel: event is PointerCancelEvent));
    }
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.hidden ||
        state == AppLifecycleState.paused ||
        state == AppLifecycleState.detached) {
      heldPointer = null;
      if (voice.active) unawaited(voice.discard());
      unawaited(session.saveDraft());
    }
    if (state == AppLifecycleState.resumed &&
        session.pendingRequest != null &&
        !session.busy) {
      unawaited(session.recover());
    }
  }

  Future<void> browse() async {
    await Navigator.of(context).push(
      MaterialPageRoute<void>(
        builder: (_) =>
            ObjectBrowser(api: api, conversationId: session.conversationId),
      ),
    );
    if (mounted) await session.guard(session.refreshCount);
  }

  Future<void> newConversation() async {
    if (session.busy || voice.busy || session.transcript.isNotEmpty) {
      setState(
        () => localError = ApiFailure(
          'finish_input',
          context.l10n.finishInputFirst,
        ),
      );
      return;
    }
    await session.create();
    if (mounted) scaffold.currentState?.closeDrawer();
  }

  Future<void> settings() async {
    scaffold.currentState?.closeDrawer();
    await showModalBottomSheet<void>(
      context: context,
      showDragHandle: true,
      isScrollControlled: true,
      useSafeArea: true,
      builder: (sheetContext) {
        final s = sheetContext.l10n;
        return Padding(
          padding: const EdgeInsets.fromLTRB(24, 8, 24, 28),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Text(s.settings, style: Theme.of(context).textTheme.titleLarge),
              const SizedBox(height: 12),
              ListTile(
                contentPadding: EdgeInsets.zero,
                leading: const Icon(Icons.install_mobile, color: Palette.teal),
                title: Text(Localizations.localeOf(context).languageCode == 'zh'
                    ? '添加到主屏幕' : 'Install app'),
                onTap: () => showInstall(sheetContext),
              ),
              const SizedBox(height: 12),
              DropdownButtonFormField<String>(
                initialValue: api.language,
                decoration: InputDecoration(labelText: s.language),
                items: [
                  DropdownMenuItem(value: 'zh', child: Text(s.chinese)),
                  DropdownMenuItem(value: 'en', child: Text(s.english)),
                ],
                onChanged: (value) async {
                  if (value == null) return;
                  await voice.discard();
                  if (!sheetContext.mounted) return;
                  Navigator.pop(sheetContext);
                  await widget.onLanguage(value);
                },
              ),
              const SizedBox(height: 20),
              Text(
                s.pendingRule,
                style: const TextStyle(fontSize: 13, color: Palette.muted),
              ),
            ],
          ),
        );
      },
    );
  }

  Future<void> account() async {
    scaffold.currentState?.closeDrawer();
    await showModalBottomSheet<void>(
      context: context,
      showDragHandle: true,
      useSafeArea: true,
      builder: (sheetContext) {
        final s = sheetContext.l10n;
        return Padding(
          padding: const EdgeInsets.fromLTRB(24, 8, 24, 28),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Text(s.account, style: Theme.of(context).textTheme.titleLarge),
              const SizedBox(height: 20),
              SelectableText(api.auth.profile['name'] ?? '${api.session?['user']}'),
              if (api.auth.profile['email'] != null) ...[
                const SizedBox(height: 8),
                SelectableText(api.auth.profile['email']!),
              ],
              const SizedBox(height: 8),
              Text('${api.session?['workspace']?['name']}'),
              const SizedBox(height: 8),
              Text(switch (api.session?['role']) {
                'viewer' => s.roleViewer,
                'editor' => s.roleEditor,
                'reviewer' => s.roleReviewer,
                _ => s.notAvailable,
              }),
              if ((api.session?['workspaces'] as List? ?? []).length > 1) ...[
                const SizedBox(height: 16),
                DropdownButtonFormField<String>(
                  initialValue: '${api.session?['workspace']?['id']}',
                  decoration: InputDecoration(labelText: s.switchWorkspace),
                  items: [for (final space in api.session!['workspaces'] as List)
                    DropdownMenuItem(value: '${space['id']}', child: Text('${space['name']}'))],
                  onChanged: session.busy || voice.busy ? null : (id) async {
                    if (id == null || id == api.session?['workspace']?['id']) return;
                    Navigator.pop(sheetContext);
                    try {
                      await voice.discard();
                      await widget.onWorkspace(id);
                    } catch (e) {
                      if (mounted) setState(() => localError = e);
                    }
                  },
                ),
              ],
              const SizedBox(height: 24),
              FilledButton.tonal(
                onPressed: session.busy || voice.busy
                    ? null
                    : () async {
                        Navigator.pop(sheetContext);
                        try {
                          await voice.discard();
                          await widget.onSignOut();
                        } catch (e) {
                          if (mounted) setState(() => localError = e);
                        }
                      },
                child: Text(s.logout),
              ),
            ],
          ),
        );
      },
    );
  }

  Widget drawer() {
    final s = context.l10n;
    return Drawer(
      backgroundColor: Colors.white,
      child: SafeArea(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(22, 20, 10, 20),
              child: Row(
                children: [
                  Expanded(
                    child: Text(
                      s.appTitle,
                      style: const TextStyle(
                        fontSize: 23,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                  ),
                  IconButton(
                    onPressed: () => scaffold.currentState?.closeDrawer(),
                    tooltip: s.close,
                    icon: const Icon(Icons.close),
                  ),
                ],
              ),
            ),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 20),
              child: OutlinedButton.icon(
                onPressed: session.busy ? null : newConversation,
                icon: const Icon(Icons.add),
                label: Text(s.newConversation),
                style: OutlinedButton.styleFrom(
                  minimumSize: const Size(48, 52),
                  foregroundColor: Palette.teal,
                  side: const BorderSide(color: Palette.teal),
                ),
              ),
            ),
            Padding(
              padding: const EdgeInsets.fromLTRB(24, 26, 24, 10),
              child: Text(
                s.history,
                style: const TextStyle(color: Palette.muted),
              ),
            ),
            Expanded(
              child: PagedRefresh(
                refreshOnStart: false,
                onRefresh: () => session.moreConversations(reset: true),
                onLoad: session.conversations.isEmpty ? null : session.moreConversations,
                builder: (_, physics) => session.conversations.isEmpty
                  ? CustomScrollView(physics: physics, slivers: [
                      SliverFillRemaining(hasScrollBody: false,
                        child: Center(child: Text(s.noConversations))),
                    ])
                  : ListView.builder(
                      physics: physics,
                      itemCount: session.conversations.length,
                      padding: const EdgeInsets.symmetric(horizontal: 12),
                      itemBuilder: (_, index) {
                        final item = session.conversations[index];
                        return Padding(
                          padding: const EdgeInsets.only(bottom: 5),
                          child: ListTile(
                            shape: RoundedRectangleBorder(
                              borderRadius: BorderRadius.circular(14),
                            ),
                            selected: item['id'] == session.conversationId,
                            selectedTileColor: Palette.soft,
                            leading: const Icon(Icons.chat_bubble_outline),
                            title: Text(
                              '${item['title'] ?? ''}'.isEmpty
                                  ? s.newConversation
                                  : '${item['title']}',
                              maxLines: 2,
                              overflow: TextOverflow.ellipsis,
                            ),
                            onTap: session.busy || voice.busy
                                ? null
                                : () async {
                                    scaffold.currentState?.closeDrawer();
                                    await session.guard(
                                      () => session.open(item),
                                    );
                                  },
                          ),
                        );
                      },
                    ),
              ),
            ),
            const Divider(indent: 22, endIndent: 22),
            ListTile(
              leading: const Icon(Icons.settings_outlined),
              title: Text(s.settings),
              onTap: settings,
            ),
            ListTile(
              leading: const Icon(Icons.person_outline),
              title: Text(s.account),
              onTap: account,
            ),
            const SizedBox(height: 14),
          ],
        ),
      ),
    );
  }

  Widget messageBubble(Json message) {
    final user = message['role'] == 'user';
    final created = message['created_at'];
    final time = created is num
        ? DateTime.fromMillisecondsSinceEpoch(
            (created * 1000).round(),
          ).toLocal()
        : null;
    final avatar = CircleAvatar(
      radius: 18,
      backgroundColor: Palette.soft,
      child: Icon(
        user ? Icons.person_outline : Icons.smart_toy_outlined,
        size: 22,
        color: Palette.teal,
      ),
    );
    final bubble = Flexible(
      child: Column(
        crossAxisAlignment: user
            ? CrossAxisAlignment.end
            : CrossAxisAlignment.start,
        children: [
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 13),
            decoration: BoxDecoration(
              color: user ? Palette.soft : Colors.white,
              border: Border.all(color: user ? Palette.soft : Palette.line),
              borderRadius: BorderRadius.only(
                topLeft: const Radius.circular(19),
                topRight: const Radius.circular(19),
                bottomLeft: Radius.circular(user ? 19 : 6),
                bottomRight: Radius.circular(user ? 6 : 19),
              ),
            ),
            child: SelectableText(
              '${message['text'] ?? ''}',
              style: const TextStyle(fontSize: 16, height: 1.65),
            ),
          ),
          if (time != null)
            Padding(
              padding: const EdgeInsets.only(top: 5, left: 8, right: 8),
              child: Text(
                DateFormat.jm(api.language).format(time),
                style: const TextStyle(fontSize: 11, color: Palette.muted),
              ),
            ),
        ],
      ),
    );
    return Padding(
      padding: EdgeInsets.fromLTRB(user ? 40 : 0, 0, user ? 0 : 24, 22),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: user
            ? [bubble, const SizedBox(width: 9), avatar]
            : [avatar, const SizedBox(width: 9), bubble],
      ),
    );
  }

  Widget microphone({bool round = false}) {
    final s = context.l10n;
    final content = round
        ? Container(
            width: 82,
            height: 82,
            decoration: BoxDecoration(
              shape: BoxShape.circle,
              color: voice.cancelled ? Palette.danger : Palette.teal,
              boxShadow: [
                BoxShadow(
                  color: (voice.cancelled ? Palette.dangerFill : Palette.soft),
                  spreadRadius: 11,
                ),
              ],
            ),
            child: const Icon(Icons.mic_none, size: 38, color: Colors.white),
          )
        : Container(
            constraints: const BoxConstraints(minHeight: 54),
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
            decoration: BoxDecoration(
              color: Palette.teal,
              borderRadius: BorderRadius.circular(30),
            ),
            child: Row(
              mainAxisAlignment: MainAxisAlignment.center,
              children: [
                const Icon(Icons.mic_none, color: Colors.white, size: 25),
                const SizedBox(width: 7),
                Flexible(
                  child: Text(
                    s.holdToTalk,
                    textAlign: TextAlign.center,
                    style: const TextStyle(color: Colors.white, fontSize: 16),
                  ),
                ),
              ],
            ),
          );
    return Semantics(
      label: s.holdToTalk,
      button: true,
      onTap: voice.active ? () => unawaited(voice.finish()) : () => unawaited(voice.start()),
      onLongPress: () async {
        await voice.start();
      },
      child: Listener(
        onPointerDown: startVoice,
        behavior: HitTestBehavior.opaque,
        child: content,
      ),
    );
  }

  Widget voicePanel() {
    final s = context.l10n;
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 18, 16, 14),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          RecordingFeedback(voice: voice),
          Text(
            voice.cancelled ? s.releaseToCancel : s.releaseToFinish,
            style: const TextStyle(color: Palette.muted),
          ),
          const SizedBox(height: 12),
          SizedBox(
            height: 104,
            child: Stack(
              alignment: Alignment.center,
              children: [
                Align(
                  alignment: Alignment.centerLeft,
                  child: FractionallySizedBox(
                    widthFactor: .39,
                    child: Container(
                      padding: const EdgeInsets.symmetric(
                        horizontal: 9,
                        vertical: 16,
                      ),
                      decoration: BoxDecoration(
                        color: voice.cancelled
                            ? Palette.danger
                            : Palette.dangerFill,
                        borderRadius: const BorderRadius.horizontal(
                          left: Radius.circular(30),
                        ),
                      ),
                      child: Row(
                        children: [
                          Icon(
                            Icons.cancel_outlined,
                            size: 25,
                            color: voice.cancelled
                                ? Colors.white
                                : Palette.danger,
                          ),
                          const SizedBox(width: 5),
                          Expanded(
                            child: Text(
                              s.slideToCancel,
                              style: TextStyle(
                                fontSize: 12,
                                color: voice.cancelled
                                    ? Colors.white
                                    : Palette.danger,
                              ),
                            ),
                          ),
                        ],
                      ),
                    ),
                  ),
                ),
                microphone(round: true),
              ],
            ),
          ),
          const SizedBox(height: 9),
          Text(
            s.editBeforeSend,
            style: const TextStyle(color: Palette.muted, fontSize: 12),
          ),
          if (voice.phase == VoicePhase.preparing)
            const LinearProgressIndicator(minHeight: 2),
        ],
      ),
    );
  }

  Widget composer() {
    final s = context.l10n;
    final isTranscript = session.transcript.isNotEmpty;
    Widget body;
    if (voice.active) {
      body = voicePanel();
    } else if (voice.phase == VoicePhase.recognizing || voice.phase == VoicePhase.stopping) {
      body = Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const CircularProgressIndicator(),
            const SizedBox(height: 12),
            Text(s.recognizing),
            TextButton(onPressed: voice.discard, child: Text(s.cancel)),
          ],
        ),
      );
    } else if (isTranscript || textMode) {
      body = Padding(
        padding: const EdgeInsets.fromLTRB(14, 14, 14, 8),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            if (isTranscript)
              Padding(
                padding: const EdgeInsets.only(bottom: 7),
                child: Text(
                  session.draft.isNotEmpty ? s.draftPreserved : s.transcript,
                  style: const TextStyle(fontSize: 12, color: Palette.muted),
                ),
              ),
            TextField(
              controller: isTranscript ? transcriptEditor : editor,
              minLines: 2,
              maxLines: 5,
              maxLength: 16000,
              decoration: InputDecoration(
                hintText: s.inputHint,
                counterText: '',
              ),
              onChanged: isTranscript
                  ? session.editTranscript
                  : session.editDraft,
            ),
            const SizedBox(height: 5),
            Wrap(
              alignment: WrapAlignment.spaceBetween,
              crossAxisAlignment: WrapCrossAlignment.center,
              spacing: 7,
              children: [
                if (isTranscript)
                  TextButton(
                    onPressed: () {
                      session.transcript = '';
                      unawaited(session.saveDraft());
                      session.emit();
                    },
                    child: Text(s.discard),
                  )
                else
                  IconButton(
                    tooltip: s.holdToTalk,
                    onPressed: () => setState(() => textMode = false),
                    icon: const Icon(Icons.mic_none, color: Palette.teal),
                  ),
                if (isTranscript && session.draft.isNotEmpty)
                  TextButton(
                    onPressed: () {
                      session.draft = '${session.draft}\n${session.transcript}';
                      session.transcript = '';
                      textMode = true;
                      unawaited(session.saveDraft());
                      session.emit();
                    },
                    child: Text(s.mergeDraft),
                  ),
                FilledButton.icon(
                  onPressed: session.busy
                      ? null
                      : () => session.send(
                          isTranscript ? session.transcript : session.draft,
                          voice: isTranscript,
                        ),
                  icon: const Icon(Icons.arrow_upward, size: 18),
                  label: Text(session.busy ? s.sending : s.send),
                ),
              ],
            ),
          ],
        ),
      );
    } else {
      body = Padding(
        padding: const EdgeInsets.fromLTRB(10, 17, 10, 12),
        child: Row(
          children: [
            IconButton(
              onPressed: () => setState(() => textMode = true),
              tooltip: s.typeText,
              icon: const Icon(Icons.keyboard_outlined),
            ),
            const SizedBox(width: 5),
            Expanded(child: microphone()),
            const SizedBox(width: 7),
            TextButton.icon(
              onPressed: () => setState(() => textMode = true),
              icon: const Icon(Icons.text_fields),
              label: Text(s.typeText),
            ),
          ],
        ),
      );
    }
    return Material(
      color: Colors.white,
      borderRadius: const BorderRadius.vertical(top: Radius.circular(22)),
      child: SafeArea(top: false, child: body),
    );
  }

  @override
  Widget build(BuildContext context) {
    final s = context.l10n;
    final error = localError ?? voice.error ?? session.error;
    return PopScope(
      canPop: !voice.active,
      onPopInvokedWithResult: (didPop, _) {
        if (!didPop && voice.active) unawaited(voice.discard());
      },
      child: Scaffold(
        key: scaffold,
        drawer: drawer(),
        appBar: AppBar(
          title: Text(s.appTitle),
          actions: [
            IconButton(
              tooltip: s.refresh,
              onPressed: session.loading || readingHistory
                  ? null
                  : () => session.guard(session.refresh),
              icon: const Icon(Icons.refresh, size: 22),
            ),
          ],
        ),
        body: Column(
          children: [
            if (error != null)
              Padding(
                padding: const EdgeInsets.fromLTRB(12, 8, 12, 0),
                child: ErrorNotice(
                  error: error,
                  onClose: () {
                    localError = null;
                    voice.error = null;
                    session.error = null;
                    setState(() {});
                  },
                  onRetry: voice.retryable
                      ? voice.retry
                      : session.pendingRequest?['failed'] == true
                      ? session.retryFailed
                      : session.pendingRequest != null
                      ? session.recover
                      : () => session.guard(session.refresh),
                ),
              ),
            if (session.pendingRequest != null)
              Padding(
                padding: const EdgeInsets.symmetric(horizontal: 12),
                child: ListTile(
                  dense: true,
                  contentPadding: EdgeInsets.zero,
                  leading: session.busy
                      ? const SizedBox.square(
                          dimension: 18,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      : const Icon(Icons.schedule),
                  title: Text(
                    session.busy ? s.requestQueued : s.requestUncertain,
                    style: const TextStyle(fontSize: 12),
                  ),
                  subtitle: session.pendingRequest?['failed'] == true ? Wrap(children: [
                    TextButton(onPressed: session.retryFailed, child: Text(s.retry)),
                    TextButton(onPressed: session.discardFailed, child: Text(s.discard)),
                  ]) : null,
                  trailing: session.busy || session.pendingRequest?['failed'] == true
                      ? null : TextButton(onPressed: session.recover, child: Text(s.recoverRequest)),
                ),
              ),
            if (session.loading) const LinearProgressIndicator(minHeight: 2),
            Expanded(
              child: EasyRefresh.builder(
                key: ValueKey('history:${session.conversationId}'),
                controller: historyRefresh,
                scrollController: scroll,
                resetAfterRefresh: false,
                canRefreshAfterNoMore: true,
                header: ClassicHeader(
                  infiniteOffset: session.hasOlderMessages ? 120 : null,
                  safeArea: false,
                  showMessage: false,
                  dragText: s.refresh,
                  armedText: s.refresh,
                  readyText: s.loadingMore,
                  processingText: s.loadingMore,
                  processedText: s.loadSucceeded,
                  failedText: s.historyLoadFailedRetry,
                  noMoreText: s.allLoaded,
                  textStyle: const TextStyle(fontSize: 12, color: Palette.muted),
                  iconTheme: const IconThemeData(color: Palette.teal, size: 18),
                ),
                onRefresh: session.busy || session.loading ? null : refreshHistory,
                childBuilder: (_, physics) => messageList(physics),
              ),
            ),
            if (session.response?['draft'] != null)
              TextButton.icon(
                onPressed: () => _resolve(),
                icon: const Icon(Icons.person_search_outlined),
                label: Text(s.resolveIdentity),
              ),
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 9, 16, 14),
              child: Align(
                alignment: Alignment.centerRight,
                child: OutlinedButton.icon(
                  onPressed: browse,
                  style: OutlinedButton.styleFrom(
                    backgroundColor: Colors.white,
                    foregroundColor: Palette.ink,
                    side: const BorderSide(color: Palette.line),
                    padding: const EdgeInsets.symmetric(
                      horizontal: 16,
                      vertical: 12,
                    ),
                  ),
                  icon: const Icon(Icons.layers_outlined, color: Palette.teal),
                  label: Row(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      Text(
                        '${s.objects} ${session.objectCount ?? (session.hasConversation ? '…' : 0)}',
                      ),
                      const SizedBox(width: 7),
                      const Icon(Icons.chevron_right, size: 19),
                    ],
                  ),
                ),
              ),
            ),
            composer(),
          ],
        ),
      ),
    );
  }

  Future<void> _resolve() async {
    final draft = session.response?['draft'] as Json?;
    if (draft == null) return;
    final unresolved = (draft['objects'] as List)
        .cast<Json>()
        .where((o) => o['resolved'] != true)
        .toList();
    final choices = <String, dynamic>{};
    await showModalBottomSheet<void>(
      context: context,
      showDragHandle: true,
      useSafeArea: true,
      isScrollControlled: true,
      builder: (sheetContext) => StatefulBuilder(
        builder: (_, change) => Padding(
          padding: const EdgeInsets.fromLTRB(20, 8, 20, 24),
          child: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Text(
                  context.l10n.resolveIdentity,
                  style: Theme.of(context).textTheme.titleLarge,
                ),
                for (final object in unresolved) ...[
                  const SizedBox(height: 16),
                  Text(objectLabel(object)),
                  DropdownButtonFormField<String>(
                    isExpanded: true,
                    items: [
                      DropdownMenuItem(
                        value: 'new',
                        child: Text(context.l10n.createObject),
                      ),
                      for (final candidate
                          in (object['candidates'] as List? ?? []).cast<Json>())
                        DropdownMenuItem(
                          value: '${candidate['id']}',
                          child: Text(objectLabel(candidate)),
                        ),
                    ],
                    onChanged: (value) =>
                        change(() => choices['${object['ref']}'] = value),
                  ),
                ],
                const SizedBox(height: 22),
                FilledButton(
                  onPressed: choices.length != unresolved.length
                      ? null
                      : () async {
                          Navigator.pop(sheetContext);
                          await session.resolve(
                            '${draft['draft_id']}',
                            choices,
                          );
                        },
                  child: Text(context.l10n.continuePreview),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }

  @override
  void dispose() {
    session.removeListener(changed);
    voice.removeListener(changed);
    WidgetsBinding.instance.removeObserver(this);
    GestureBinding.instance.pointerRouter.removeGlobalRoute(routePointer);
    voice.dispose();
    editor.dispose();
    transcriptEditor.dispose();
    scroll.dispose();
    historyRefresh.dispose();
    super.dispose();
  }
}
