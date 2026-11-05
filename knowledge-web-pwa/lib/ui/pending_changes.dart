import 'package:flutter/material.dart';

class PendingChanges extends StatelessWidget {
  final List<Map<String, dynamic>> operations;
  final bool canApprove;
  final bool busy;
  final bool chinese;
  final void Function(Map<String, dynamic>) onApply;
  final void Function(Map<String, dynamic>) onCancel;

  const PendingChanges({
    super.key,
    required this.operations,
    required this.canApprove,
    required this.busy,
    required this.chinese,
    required this.onApply,
    required this.onCancel,
  });

  @override
  Widget build(BuildContext context) => DefaultTabController(
    key: ValueKey(operations.map((o) => o['draft']['draft_id']).join(',')),
    length: operations.length,
    child: Column(
      children: [
        if (operations.length > 1)
          TabBar(
            isScrollable: true,
            tabs: [
              for (var i = 0; i < operations.length; i++)
                Tab(text: chinese ? '变更 ${i + 1}' : 'Change ${i + 1}'),
            ],
          ),
        Expanded(
          child: TabBarView(
            children: [
              for (final operation in operations)
                _ChangeCard(
                  operation: operation,
                  canApprove: canApprove,
                  busy: busy,
                  chinese: chinese,
                  onApply: () => onApply(operation),
                  onCancel: () => onCancel(operation),
                ),
            ],
          ),
        ),
      ],
    ),
  );
}

class _ChangeCard extends StatefulWidget {
  final Map<String, dynamic> operation;
  final bool canApprove;
  final bool busy;
  final bool chinese;
  final VoidCallback onApply;
  final VoidCallback onCancel;

  const _ChangeCard({
    required this.operation,
    required this.canApprove,
    required this.busy,
    required this.chinese,
    required this.onApply,
    required this.onCancel,
  });

  @override
  State<_ChangeCard> createState() => _ChangeCardState();
}

class _ChangeCardState extends State<_ChangeCard> {
  final detailsScroll = ScrollController();

  @override
  void dispose() {
    detailsScroll.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final operation = widget.operation;
    final draft = operation['draft'] as Map<String, dynamic>;
    final objects = draft['objects'] as List? ?? [];
    final count =
        objects.where((o) => o['new'] == true).length +
        ['facts', 'entity_updates', 'retractions', 'reviews'].fold<int>(
          0,
          (sum, field) => sum + (draft[field] as List? ?? []).length,
        );
    final zh = widget.chinese;
    final expired = operation['expired'] == true;
    final requiresApproval = operation['confirmation_required'] == true;
    final canExecute =
        widget.canApprove &&
        requiresApproval &&
        operation['approval_id'] != null &&
        !expired;
    final status = expired
        ? (zh ? '已过期' : 'Expired')
        : (zh ? '尚未执行' : 'Not applied');
    final explanation = expired
        ? (zh
              ? '请在对话中重新发起此操作。'
              : 'Request this operation again in the conversation.')
        : !widget.canApprove && requiresApproval
        ? (zh ? '此操作需要审批权限。' : 'Approval permission is required.')
        : operation['clarification_required'] == true
        ? (zh
              ? '请在对话中补充对象身份或有效来源。'
              : 'Provide the missing identity or valid source in the conversation.')
        : null;
    return Card(
      margin: const EdgeInsets.symmetric(horizontal: 14, vertical: 5),
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Text(
              zh ? '$status · $count 项变更' : '$status · $count changes',
              maxLines: 2,
              overflow: TextOverflow.ellipsis,
              style: const TextStyle(fontWeight: FontWeight.w600),
            ),
            if (explanation != null)
              Text(
                explanation,
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(fontSize: 12),
              ),
            const SizedBox(height: 6),
            Expanded(
              child: Scrollbar(
                controller: detailsScroll,
                thumbVisibility: true,
                child: ListView(
                  key: ValueKey('change-details-${draft['draft_id']}'),
                  controller: detailsScroll,
                  padding: const EdgeInsets.only(right: 12),
                  children: [
                    for (final line
                        in operation['change_summary'] as List? ?? [])
                      Text('$line', style: const TextStyle(fontSize: 13)),
                  ],
                ),
              ),
            ),
            const Divider(height: 12),
            Row(
              children: [
                if (requiresApproval) ...[
                  Expanded(
                    child: FilledButton(
                      onPressed: widget.busy || !canExecute
                          ? null
                          : widget.onApply,
                      child: Text(
                        zh ? '执行变更' : 'Apply change',
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                      ),
                    ),
                  ),
                  const SizedBox(width: 10),
                ],
                Expanded(
                  child: TextButton(
                    onPressed: widget.busy ? null : widget.onCancel,
                    child: Text(zh ? '取消' : 'Cancel'),
                  ),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}
