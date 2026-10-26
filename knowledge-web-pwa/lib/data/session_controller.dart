import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';

import 'knowledge_api.dart';

class SessionController extends ChangeNotifier {
  final KnowledgeApi api;
  SessionController(this.api);
  List<Json> conversations = [];
  List<Json> messages = [];
  Json? activeConversation;
  Json? response;
  Json? pendingRequest;
  int? objectCount;
  bool busy = false;
  bool loading = false;
  bool hasOlderMessages = false;
  bool hasMoreConversations = false;
  bool _disposed = false;
  int _generation = 0;
  int _messageGeneration = 0;
  Object? error;
  String draft = '';
  String transcript = '';
  Timer? _draftSave;
  final Map<String, Future<void>> _writes = {};

  String get conversationId => '${activeConversation?['id'] ?? ''}';
  String get _localKey => 'draft:${api.storageScope}:$conversationId';
  bool get hasConversation => activeConversation != null;
  void emit() {
    if (!_disposed) notifyListeners();
  }

  List<Json> _rows(Json data) => (data['rows'] as List).cast<Json>();

  Future<void> start() => guard(() async {
    final data = await api.execute('conversations');
    conversations = _rows(data);
    hasMoreConversations = data['has_more'] == true;
    if (conversations.isNotEmpty) {
      await open(conversations.first);
    } else {
      final saved = await api.readLocal(_localKey);
      if (saved != null) {
        final data = jsonDecode(saved) as Json;
        draft = '${data['draft'] ?? ''}';
        transcript = '${data['transcript'] ?? ''}';
      }
    }
  });

  Future<void> guard(Future<void> Function() work) async {
    error = null;
    loading = true;
    emit();
    try {
      await work();
    } catch (e) {
      error = e;
    } finally {
      loading = false;
      emit();
    }
  }

  Future<void> create() async {
    if (busy) return;
    await guard(() async {
      final first = !hasConversation;
      final previousDraft = draft;
      final previousTranscript = transcript;
      await saveDraft();
      final result = await api.execute('conversation_create');
      conversations.insert(0, result);
      await open(result);
      if (first) {
        draft = previousDraft;
        transcript = previousTranscript;
        await saveDraft();
        await api.deleteLocal('draft:${api.storageScope}:');
      }
    });
  }

  Future<void> open(Json conversation) async {
    if (busy && conversation['id'] != activeConversation?['id']) return;
    await saveDraft();
    _generation++;
    activeConversation = conversation;
    messages = [];
    response = null;
    objectCount = null;
    draft = '';
    transcript = '';
    pendingRequest = null;
    final saved = await api.readLocal(_localKey);
    if (saved != null) {
      final data = jsonDecode(saved) as Json;
      draft = '${data['draft'] ?? ''}';
      transcript = '${data['transcript'] ?? ''}';
      pendingRequest = data['request'] as Json?;
    }
    emit();
    await refresh();
    if (pendingRequest != null) await recover();
  }

  Future<void> refresh({bool preserveHistory = false}) async {
    if (!hasConversation) return;
    final id = conversationId;
    final generation = _generation;
    final messageGeneration = ++_messageGeneration;
    final previous = List<Json>.of(messages);
    var result = await api.execute('messages', {'conversation_id': id});
    var latest = _rows(result);
    // Fetch until the latest window overlaps retained history. Never leave a
    // gap when more than one page arrived while this screen was inactive.
    int overlap() => latest.isEmpty ? -1
        : previous.indexWhere((m) => m['id'] == latest.first['id']);
    while (preserveHistory && previous.isNotEmpty && latest.isNotEmpty &&
        overlap() < 0 && result['has_more'] == true) {
      final page = await api.execute('messages', {
        'conversation_id': id, 'offset': latest.length, 'limit': 25,
      });
      if (id != conversationId || generation != _generation ||
          messageGeneration != _messageGeneration || _disposed) {
        return;
      }
      final known = latest.map((m) => m['id']).toSet();
      final extra = _rows(page).where((m) => !known.contains(m['id'])).toList();
      if (extra.isEmpty) throw const ApiFailure('invalid_response');
      latest = [...extra, ...latest];
      result = {...page, 'response': result['response']};
    }
    if (id != conversationId || generation != _generation ||
        messageGeneration != _messageGeneration || _disposed) {
      return;
    }
    final index = overlap();
    final retained = preserveHistory && index >= 0;
    messages = retained ? [...previous.take(index), ...latest] : latest;
    if (!retained) hasOlderMessages = result['has_more'] == true;
    response = result['response'] as Json?;
    await refreshCount();
    emit();
  }

  Future<bool> olderMessages() async {
    if (!hasOlderMessages || !hasConversation) return false;
    final id = conversationId;
    final generation = _generation;
    final messageGeneration = _messageGeneration;
    final data = await api.execute('messages', {
      'conversation_id': id, 'offset': messages.length, 'limit': 25,
    });
    if (id != conversationId || generation != _generation ||
        messageGeneration != _messageGeneration || _disposed) {
      return false;
    }
    final known = messages.map((m) => m['id']).toSet();
    messages.insertAll(0, _rows(data).where((m) => !known.contains(m['id'])));
    hasOlderMessages = data['has_more'] == true;
    emit();
    return hasOlderMessages;
  }

  Future<bool> moreConversations({bool reset = false}) async {
    if (!reset && !hasMoreConversations) return false;
    try {
      final data = await api.execute('conversations', {
        'offset': reset ? 0 : conversations.length, 'limit': 25,
      });
      if (_disposed) return false;
      if (reset) conversations = [];
      final known = conversations.map((c) => c['id']).toSet();
      conversations.addAll(_rows(data).where((c) => !known.contains(c['id'])));
      hasMoreConversations = data['has_more'] == true;
      emit();
      return hasMoreConversations;
    } catch (e) {
      if (!_disposed && e is ApiFailure && const [
        'permission_denied', 'not_available', 'object_not_found', 'session_expired',
      ].contains(e.code)) {
        conversations = [];
        hasMoreConversations = false;
        emit();
      }
      rethrow;
    }
  }

  Future<void> refreshCount() async {
    if (!hasConversation) {
      objectCount = 0;
      emit();
      return;
    }
    final id = conversationId;
    try {
      final objects = await api.execute('conversation_objects', {
        'conversation_id': id,
        'offset': 0,
        'limit': 1,
      });
      if (id == conversationId && !_disposed) {
        objectCount = objects['total'] as int;
      }
    } catch (_) {
      if (id == conversationId) objectCount = null;
      rethrow;
    } finally {
      emit();
    }
  }

  void editDraft(String value) {
    draft = value;
    _scheduleSave();
  }

  void editTranscript(String value) {
    transcript = value;
    _scheduleSave();
  }

  void _scheduleSave() {
    _draftSave?.cancel();
    _draftSave = Timer(const Duration(milliseconds: 300), () {
      saveDraft().catchError((Object e) {
        error = e;
        emit();
      });
    });
  }

  Future<void> saveDraft() async {
    _draftSave?.cancel();
    if (!api.signedIn) return;
    final key = _localKey;
    final encoded = jsonEncode({
      'draft': draft,
      'transcript': transcript,
      'request': pendingRequest,
    });
    final previous = _writes[key] ?? Future<void>.value();
    final write = previous
        .catchError((_) {})
        .then((_) => api.writeLocal(key, encoded));
    _writes[key] = write;
    await write;
  }

  Future<void> send(String text, {bool voice = false}) async {
    if (busy || text.trim().isEmpty) return;
    if (pendingRequest != null) {
      await recover();
      return;
    }
    if (!hasConversation) await create();
    if (!hasConversation) return;
    pendingRequest = {
      'request_id': KnowledgeApi.newRequestId(),
      'conversation_id': conversationId,
      'text': text.trim(),
      'voice': voice,
    };
    await saveDraft();
    await _submit();
  }

  Future<void> _submit({bool retry = false}) async {
    final request = pendingRequest;
    if (request == null) return;
    busy = true;
    error = null;
    emit();
    try {
      await api.execute('chat_submit', {
        'conversation_id': request['conversation_id'],
        'request_id': request['request_id'],
        'message': request['text'],
        if (retry) 'retry': true,
      });
      await _poll(request, _generation);
    } catch (e) {
      error = e;
    } finally {
      busy = false;
      await saveDraft();
      emit();
    }
  }

  Future<void> recover() async {
    if (pendingRequest == null || busy) return;
    busy = true;
    error = null;
    emit();
    try {
      final data = await api.execute('request_status', {
        'conversation_id': conversationId,
        'request_id': pendingRequest!['request_id'],
      });
      if (data['status'] == 'not_found') {
        busy = false;
        await _submit();
      } else {
        await _poll(pendingRequest!, _generation);
      }
    } catch (e) {
      error = e;
    } finally {
      busy = false;
      await saveDraft();
      emit();
    }
  }

  Future<void> _poll(Json request, int generation) async {
    for (var attempt = 0; attempt < 150; attempt++) {
      if (_disposed || generation != _generation) return;
      final data = await api.execute('request_status', {
        'conversation_id': request['conversation_id'],
        'request_id': request['request_id'],
      });
      if (data['status'] == 'succeeded') {
        response = data['result'] as Json?;
        if (request['voice'] == true) {
          if (transcript.trim() == request['text']) transcript = '';
        } else if (draft.trim() == request['text']) {
          draft = '';
        }
        pendingRequest = null;
        await saveDraft();
        await refresh();
        return;
      }
      if (['failed', 'interrupted'].contains(data['status'])) {
        pendingRequest?['failed'] = true;
        await saveDraft();
        await refresh();
        throw ApiFailure(
          '${data['error']?['code'] ?? 'operation_failed'}',
          '${data['error']?['message'] ?? ''}',
        );
      }
      await Future<void>.delayed(const Duration(seconds: 2));
    }
    throw const ApiFailure('request_uncertain');
  }

  Future<void> retryFailed() async {
    if (busy || pendingRequest?['failed'] != true) return;
    pendingRequest!['failed'] = false;
    await _submit(retry: true);
  }

  Future<void> discardFailed() async {
    if (busy || pendingRequest?['failed'] != true) return;
    pendingRequest = null;
    error = null;
    await saveDraft();
    emit();
  }

  Future<void> resolve(String draftId, Json choices) => guard(() async {
    response = await api.execute('resolve', {
      'conversation_id': conversationId,
      'draft_id': draftId,
      'choices': choices,
    });
    await refresh();
  });

  Future<void> discardLocalData() async {
    await saveDraft();
    await Future.wait(_writes.values);
    await api.clearUserDrafts();
  }

  @override
  void dispose() {
    _disposed = true;
    _generation++;
    _draftSave?.cancel();
    super.dispose();
  }
}
