import 'package:flutter/material.dart';
import 'package:easy_refresh/easy_refresh.dart';

import '../data/knowledge_api.dart';
import 'common.dart';

class ObjectBrowser extends StatefulWidget {
  final KnowledgeApi api;
  final String conversationId;
  final Json? object;
  const ObjectBrowser({
    super.key,
    required this.api,
    required this.conversationId,
    this.object,
  });
  @override
  State<ObjectBrowser> createState() => _ObjectBrowserState();
}

class _ObjectBrowserState extends State<ObjectBrowser> {
  final scroll = ScrollController();
  final refreshController = EasyRefreshController();
  final List<Json> rows = [];
  bool loading = false;
  bool hasMore = false;
  int? revision;
  int? total;
  Object? error;
  bool denied = false;
  bool appendError = false;
  int generation = 0;

  @override
  void initState() {
    super.initState();
    refresh();
  }

  bool get refreshRequired =>
      error is ApiFailure && (error as ApiFailure).code == 'data_changed';

  Future<IndicatorResult> refresh() async {
    final result = await load(reset: true);
    if (mounted && result == IndicatorResult.success) {
      refreshController.resetFooter();
      if (!hasMore) refreshController.finishLoad(IndicatorResult.noMore, true);
    }
    return result;
  }

  Future<IndicatorResult> loadNext() async {
    if (denied || refreshRequired || !hasMore) return IndicatorResult.noMore;
    final result = await load();
    return result == IndicatorResult.success && !hasMore
        ? IndicatorResult.noMore
        : result;
  }

  Future<IndicatorResult> load({bool reset = false}) async {
    if (loading) return IndicatorResult.none;
    if (widget.conversationId.isEmpty) {
      setState(() {
        total = 0;
        hasMore = false;
      });
      return IndicatorResult.success;
    }
    final current = ++generation;
    setState(() {
      loading = true;
      error = null;
      appendError = false;
    });
    try {
      final data = await widget.api.execute(
        widget.object == null ? 'conversation_objects' : 'neighbors',
        {
          'conversation_id': widget.conversationId,
          if (widget.object != null) 'id': widget.object!['id'],
          'offset': reset ? 0 : rows.length,
          'limit': 25,
          if (!reset && revision != null) 'revision': revision,
        },
      );
      if (!mounted || current != generation) return IndicatorResult.none;
      setState(() {
        if (reset) rows.clear();
        rows.addAll((data['rows'] as List).cast<Json>());
        revision = data['revision'] as int;
        total = data['total'] as int;
        hasMore = data['has_more'] == true;
        denied = false;
      });
      return IndicatorResult.success;
    } catch (e) {
      if (!mounted || current != generation) return IndicatorResult.none;
      setState(() {
        error = e;
        denied =
            e is ApiFailure &&
            [
              'permission_denied',
              'not_available',
              'object_not_found',
              'session_expired',
            ].contains(e.code);
        appendError = !reset && rows.isNotEmpty && !denied && !refreshRequired;
        if (denied) {
          rows.clear();
          total = null;
          hasMore = false;
        }
      });
      return IndicatorResult.fail;
    } finally {
      if (mounted && current == generation) setState(() => loading = false);
    }
  }

  Future<void> open(Json object) async {
    await Navigator.of(context).push(
      MaterialPageRoute<void>(
        builder: (_) => ObjectBrowser(
          api: widget.api,
          conversationId: widget.conversationId,
          object: object,
        ),
      ),
    );
  }

  Widget card(Json row) {
    final s = context.l10n;
    final object = row['object'] as Json;
    final relations = (row['relations'] as List? ?? []).cast<Json>();
    return Padding(
      padding: const EdgeInsets.only(bottom: 13),
      child: Material(
        color: Colors.white,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(18),
          side: const BorderSide(color: Palette.line),
        ),
        child: InkWell(
          borderRadius: BorderRadius.circular(18),
          onTap: () => open(object),
          child: Padding(
            padding: const EdgeInsets.all(17),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Expanded(
                      child: Text(
                        objectLabel(object),
                        softWrap: true,
                        style: const TextStyle(
                          fontSize: 17,
                          fontWeight: FontWeight.w600,
                          height: 1.5,
                        ),
                      ),
                    ),
                    const SizedBox(width: 8),
                    const Icon(
                      Icons.chevron_right,
                      size: 21,
                      color: Palette.muted,
                    ),
                  ],
                ),
                const SizedBox(height: 11),
                StatusBadge(
                  confirmed: object['confirmed'] == true,
                  hasPending: object['has_pending'] == true,
                ),
                const SizedBox(height: 12),
                Wrap(
                  spacing: 20,
                  runSpacing: 7,
                  children: [
                    Text(
                      '${s.relatedObjects} ${object['related_object_count']}',
                      style: const TextStyle(
                        fontSize: 13,
                        color: Palette.muted,
                      ),
                    ),
                    Text(
                      '${s.relationCount} ${object['relation_count']}',
                      style: const TextStyle(
                        fontSize: 13,
                        color: Palette.muted,
                      ),
                    ),
                  ],
                ),
                if (relations.isNotEmpty) ...[
                  const Divider(height: 28),
                  for (final relation in relations)
                    Padding(
                      padding: const EdgeInsets.only(bottom: 11),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            '${relation['predicate']} · ${relation['direction'] == 'out' ? s.outgoing : s.incoming}',
                            maxLines: 2,
                            overflow: TextOverflow.ellipsis,
                            style: const TextStyle(
                              fontSize: 13,
                              fontWeight: FontWeight.w500,
                              color: Palette.teal,
                            ),
                          ),
                          const SizedBox(height: 4),
                          Text(
                            '${objectLabel(relation['source'] as Json)} → ${objectLabel(relation['target'] as Json)}',
                            maxLines: 2,
                            overflow: TextOverflow.ellipsis,
                            style: const TextStyle(
                              fontSize: 13,
                              height: 1.55,
                              color: Palette.muted,
                            ),
                          ),
                        ],
                      ),
                    ),
                ],
              ],
            ),
          ),
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final s = context.l10n;
    return Scaffold(
      appBar: AppBar(
        title: Text(s.objects),
        leading: IconButton(
          tooltip: s.back,
          icon: const Icon(Icons.arrow_back),
          onPressed: () => Navigator.pop(context),
        ),
        actions: [
          IconButton(
            tooltip: s.close,
            icon: const Icon(Icons.close),
            onPressed: () =>
                Navigator.of(context).popUntil((route) => route.isFirst),
          ),
        ],
      ),
      body: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          if (widget.object != null && !denied)
            Container(
              color: Colors.white,
              padding: const EdgeInsets.fromLTRB(20, 16, 14, 16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    objectLabel(widget.object!),
                    softWrap: true,
                    style: const TextStyle(
                      fontSize: 21,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  const SizedBox(height: 9),
                  Row(
                    children: [
                      Expanded(
                        child: StatusBadge(
                          confirmed: widget.object!['confirmed'] == true,
                          hasPending: widget.object!['has_pending'] == true,
                        ),
                      ),
                      TextButton.icon(
                        onPressed: () => showObjectInfo(
                          context,
                          widget.api,
                          widget.conversationId,
                          '${widget.object!['id']}',
                        ),
                        icon: const Icon(Icons.article_outlined, size: 18),
                        label: Text(s.details),
                      ),
                    ],
                  ),
                ],
              ),
            ),
          if (loading && rows.isEmpty) const LinearProgressIndicator(minHeight: 2),
          if (error != null && !appendError)
            Padding(
              padding: const EdgeInsets.all(12),
              child: ErrorNotice(
                error: error!,
                onRetry: refresh,
              ),
            ),
          Padding(
            padding: const EdgeInsets.fromLTRB(20, 18, 10, 6),
            child: Row(
              children: [
                Expanded(
                  child: Text(
                    widget.object == null
                        ? s.conversationObjects
                        : s.browseHint,
                    style: const TextStyle(fontSize: 13, color: Palette.muted),
                  ),
                ),
                if (total != null)
                  Text('$total', style: const TextStyle(color: Palette.muted)),
                IconButton(
                  onPressed: loading ? null : refresh,
                  tooltip: s.refresh,
                  icon: const Icon(Icons.refresh, size: 20),
                ),
              ],
            ),
          ),
          Expanded(
            child: SafeArea(
              top: false,
              child: EasyRefresh.builder(
                controller: refreshController,
                scrollController: scroll,
                resetAfterRefresh: false,
                canRefreshAfterNoMore: true,
                header: MaterialHeader(
                  color: Palette.teal,
                  semanticsLabel: s.refresh,
                ),
                footer: listFooter(context),
                onRefresh: refresh,
                onLoad: rows.isEmpty || denied || refreshRequired ? null : loadNext,
                childBuilder: (_, physics) => rows.isEmpty
                    ? CustomScrollView(
                        controller: scroll,
                        physics: physics,
                        slivers: [
                          SliverFillRemaining(
                            hasScrollBody: false,
                            child: Center(
                              child: Padding(
                                padding: const EdgeInsets.all(28),
                                child: Column(
                                  mainAxisSize: MainAxisSize.min,
                                  children: [
                                    Icon(
                                      error != null
                                          ? Icons.cloud_off_outlined
                                          : Icons.layers_outlined,
                                      color: Palette.teal,
                                      size: 42,
                                    ),
                                    const SizedBox(height: 16),
                                    Text(
                                      loading
                                          ? s.sending
                                          : error != null
                                          ? errorLabel(context, error!)
                                          : widget.object == null
                                          ? s.noObjects
                                          : s.noRelations,
                                      textAlign: TextAlign.center,
                                    ),
                                  ],
                                ),
                              ),
                            ),
                          ),
                        ],
                      )
                    : ListView.builder(
                        controller: scroll,
                        physics: physics,
                        padding: const EdgeInsets.fromLTRB(16, 8, 16, 22),
                        itemCount: rows.length + 1,
                        itemBuilder: (_, index) {
                          if (index < rows.length) return card(rows[index]);
                          return Padding(
                            padding: const EdgeInsets.only(top: 8),
                            child: Text(
                              s.browseScope,
                              style: const TextStyle(
                                fontSize: 12,
                                color: Palette.muted,
                              ),
                            ),
                          );
                        },
                      ),
              ),
            ),
          ),
        ],
      ),
    );
  }

  @override
  void dispose() {
    generation++;
    refreshController.dispose();
    scroll.dispose();
    super.dispose();
  }
}

Future<void> showObjectInfo(
  BuildContext context,
  KnowledgeApi api,
  String conversationId,
  String objectId,
) => showModalBottomSheet<void>(
  context: context,
  showDragHandle: true,
  useSafeArea: true,
  isScrollControlled: true,
  builder: (_) =>
      _ObjectInfo(api: api, conversationId: conversationId, objectId: objectId),
);

class _ObjectInfo extends StatefulWidget {
  final KnowledgeApi api;
  final String conversationId;
  final String objectId;
  const _ObjectInfo({
    required this.api,
    required this.conversationId,
    required this.objectId,
  });
  @override
  State<_ObjectInfo> createState() => _ObjectInfoState();
}

class _ObjectInfoState extends State<_ObjectInfo> {
  Json? object;
  final List<Json> facts = [];
  bool hasMore = false;
  int? revision;

  Future<bool> load({bool reset = false}) async {
    if (!reset && !hasMore) return false;
    try {
      final data = await widget.api.execute('detail', {
        'conversation_id': widget.conversationId,
        'id': widget.objectId,
        'offset': reset ? 0 : facts.length,
        'limit': 25,
        if (!reset && revision != null) 'revision': revision,
      });
      if (!mounted) return false;
      setState(() {
        if (reset) facts.clear();
        object = data['object'] as Json;
        facts.addAll((data['rows'] as List).cast<Json>());
        hasMore = data['has_more'] == true;
        revision = data['revision'] as int;
      });
      return hasMore;
    } catch (e) {
      if (mounted && accessLost(e)) {
        setState(() {
          object = null;
          facts.clear();
          hasMore = false;
        });
      }
      rethrow;
    }
  }

  @override
  Widget build(BuildContext context) {
    final s = context.l10n;
    return SizedBox(
      height: MediaQuery.sizeOf(context).height * .8,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(20, 0, 20, 18),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Row(
              children: [
                Expanded(
                  child: Text(
                    s.details,
                    style: Theme.of(context).textTheme.titleLarge,
                  ),
                ),
                IconButton(
                  onPressed: () => Navigator.pop(context),
                  tooltip: s.close,
                  icon: const Icon(Icons.close),
                ),
              ],
            ),
            Expanded(
              child: PagedRefresh(
                onRefresh: () => load(reset: true),
                onLoad: load,
                builder: (_, physics) => object == null
                    ? CustomScrollView(physics: physics, slivers: [
                        SliverFillRemaining(hasScrollBody: false,
                          child: Center(child: Text(s.noListItems))),
                      ])
                    : ListView(
                        physics: physics,
                        children: [
                          if (object != null) ...[
                            _field(s.name, '${object!['name'] ?? ''}'),
                            _field(s.identifier, '${object!['identifier'] ?? ''}'),
                            _field(s.kind, '${object!['kind'] ?? ''}'),
                            const SizedBox(height: 10),
                            StatusBadge(confirmed: object!['confirmed'] == true),
                            const Divider(height: 30),
                          ],
                          for (final fact in facts)
                            Padding(
                              padding: const EdgeInsets.only(bottom: 16),
                              child: Column(
                                crossAxisAlignment: CrossAxisAlignment.start,
                                children: [
                                  Text(
                                    '${fact['predicate']}',
                                    style: const TextStyle(fontWeight: FontWeight.w600),
                                  ),
                                  const SizedBox(height: 6),
                                  SelectableText(factValue(fact['value'])),
                                  const SizedBox(height: 7),
                                  StatusBadge(
                                    confirmed: fact['review_status'] == 'confirmed',
                                  ),
                                  TextButton.icon(
                                    onPressed: () => _showSources('${fact['id']}'),
                                    icon: const Icon(Icons.format_quote, size: 18),
                                    label: Text(
                                      '${s.sources} ${fact['source_count'] ?? ''}',
                                    ),
                                  ),
                                ],
                              ),
                            ),
                        ],
                      ),
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _field(String label, String value) => Padding(
    padding: const EdgeInsets.only(top: 12),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(label, style: const TextStyle(color: Palette.muted, fontSize: 12)),
        const SizedBox(height: 3),
        SelectableText(value.isEmpty ? context.l10n.emptyValue : value),
      ],
    ),
  );

  Future<void> _showSources(String factId) async {
    await showModalBottomSheet<void>(
      context: context,
      showDragHandle: true,
      useSafeArea: true,
      isScrollControlled: true,
      builder: (_) => _Sources(
        api: widget.api,
        conversationId: widget.conversationId,
        factId: factId,
      ),
    );
  }
}

class _Sources extends StatefulWidget {
  final KnowledgeApi api;
  final String conversationId;
  final String factId;
  const _Sources({
    required this.api,
    required this.conversationId,
    required this.factId,
  });
  @override
  State<_Sources> createState() => _SourcesState();
}

class _SourcesState extends State<_Sources> {
  final List<Json> rows = [];
  bool hasMore = false;

  Future<bool> load({bool reset = false}) async {
    if (!reset && !hasMore) return false;
    try {
      final data = await widget.api.execute('sources', {
        'conversation_id': widget.conversationId,
        'id': widget.factId,
        'offset': reset ? 0 : rows.length,
        'limit': 10,
      });
      if (!mounted) return false;
      setState(() {
        if (reset) rows.clear();
        rows.addAll((data['rows'] as List).cast<Json>());
        hasMore = data['has_more'] == true;
      });
      return hasMore;
    } catch (e) {
      if (mounted && accessLost(e)) {
        setState(() {
          rows.clear();
          hasMore = false;
        });
      }
      rethrow;
    }
  }

  @override
  Widget build(BuildContext context) => SizedBox(
    height: MediaQuery.sizeOf(context).height * .72,
    child: Padding(
      padding: const EdgeInsets.fromLTRB(20, 0, 20, 18),
      child: Column(
        children: [
          Row(
            children: [
              Expanded(
                child: Text(
                  context.l10n.sources,
                  style: Theme.of(context).textTheme.titleLarge,
                ),
              ),
              IconButton(
                onPressed: () => Navigator.pop(context),
                tooltip: context.l10n.close,
                icon: const Icon(Icons.close),
              ),
            ],
          ),
          Expanded(
            child: PagedRefresh(
              onRefresh: () => load(reset: true),
              onLoad: load,
              builder: (_, physics) => rows.isEmpty
                  ? CustomScrollView(physics: physics, slivers: [
                      SliverFillRemaining(hasScrollBody: false,
                        child: Center(child: Text(context.l10n.noListItems))),
                    ])
                  : ListView(
                      physics: physics,
                      children: [
                        for (final source in rows)
                          Padding(
                            padding: const EdgeInsets.symmetric(vertical: 15),
                            child: Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                SelectableText('${source['message'] ?? ''}'),
                                const SizedBox(height: 12),
                                for (final quote in source['quotes'] as List? ?? [])
                                  Padding(
                                    padding: const EdgeInsets.only(bottom: 6),
                                    child: SelectableText(
                                      '$quote',
                                      style: const TextStyle(
                                        color: Palette.teal,
                                        height: 1.6,
                                      ),
                                    ),
                                  ),
                                const Divider(height: 28),
                              ],
                            ),
                          ),
                      ],
                    ),
            ),
          ),
        ],
      ),
    ),
  );
}
