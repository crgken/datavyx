import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:knowledge_web_pwa/ui/pending_changes.dart';

Map<String, dynamic> operation(String id, int count) => {
  'draft': {
    'draft_id': id,
    'reviews': List.generate(
      count,
      (i) => {'action': 'archive', 'target_id': '$i'},
    ),
  },
  'confirmation_required': true,
  'approval_id': 'approval-$id',
  'change_summary': List.generate(
    count,
    (i) => 'archive: Test object ${i + 1}',
  ),
};

Future<void> showPanel(
  WidgetTester tester,
  List<Map<String, dynamic>> operations, {
  bool canApprove = true,
  bool busy = false,
  double width = 390,
  double textScale = 1,
  void Function(Map<String, dynamic>)? onApply,
  void Function(Map<String, dynamic>)? onCancel,
}) async {
  await tester.pumpWidget(
    MaterialApp(
      home: Scaffold(
        body: Center(
          child: MediaQuery(
            data: MediaQueryData(textScaler: TextScaler.linear(textScale)),
            child: SizedBox(
              width: width,
              height: 230,
              child: PendingChanges(
                operations: operations,
                canApprove: canApprove,
                busy: busy,
                chinese: false,
                onApply: onApply ?? (_) {},
                onCancel: onCancel ?? (_) {},
              ),
            ),
          ),
        ),
      ),
    ),
  );
  await tester.pumpAndSettle();
}

void main() {
  testWidgets(
    '24-row archive keeps actions visible before and after scrolling',
    (tester) async {
      String? applied;
      await showPanel(tester, [
        operation('batch', 24),
      ], onApply: (o) => applied = o['draft']['draft_id'] as String);
      expect(find.text('Not applied · 24 changes'), findsOneWidget);
      final apply = find.widgetWithText(FilledButton, 'Apply change');
      final cancel = find.widgetWithText(TextButton, 'Cancel');
      expect(apply.hitTestable(), findsOneWidget);
      expect(cancel.hitTestable(), findsOneWidget);
      final before = tester.getRect(apply);
      await tester.drag(
        find.byKey(const ValueKey('change-details-batch')),
        const Offset(0, -1000),
      );
      await tester.pumpAndSettle();
      expect(find.text('archive: Test object 24'), findsOneWidget);
      expect(tester.getRect(apply), before);
      expect(cancel.hitTestable(), findsOneWidget);
      await tester.tap(apply);
      expect(applied, 'batch');
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets('multiple proposals have selectable actions for each batch', (
    tester,
  ) async {
    String? cancelled;
    await showPanel(tester, [
      operation('first', 24),
      operation('second', 40),
    ], onCancel: (o) => cancelled = o['draft']['draft_id'] as String);
    await tester.tap(find.text('Change 2'));
    await tester.pumpAndSettle();
    expect(find.text('Not applied · 40 changes'), findsOneWidget);
    expect(
      find.widgetWithText(FilledButton, 'Apply change').hitTestable(),
      findsOneWidget,
    );
    await tester.tap(find.widgetWithText(TextButton, 'Cancel').hitTestable());
    expect(cancelled, 'second');
    expect(tester.takeException(), isNull);
  });

  testWidgets('compact width and larger text preserve visible controls', (
    tester,
  ) async {
    await showPanel(
      tester,
      [operation('large', 100)],
      width: 320,
      textScale: 1.8,
    );
    expect(
      find.widgetWithText(FilledButton, 'Apply change').hitTestable(),
      findsOneWidget,
    );
    expect(
      find.widgetWithText(TextButton, 'Cancel').hitTestable(),
      findsOneWidget,
    );
    expect(tester.takeException(), isNull);
  });

  testWidgets('expired, unauthorized and busy proposals cannot execute', (
    tester,
  ) async {
    final expired = {...operation('expired', 24), 'expired': true};
    await showPanel(tester, [expired]);
    expect(find.text('Expired · 24 changes'), findsOneWidget);
    expect(
      find
          .text('Request this operation again in the conversation.')
          .hitTestable(),
      findsOneWidget,
    );
    expect(
      tester.widget<FilledButton>(find.byType(FilledButton)).onPressed,
      isNull,
    );
    expect(
      tester
          .widget<TextButton>(find.widgetWithText(TextButton, 'Cancel'))
          .onPressed,
      isNotNull,
    );
    await showPanel(tester, [
      operation('no-permission', 24),
    ], canApprove: false);
    expect(
      tester.widget<FilledButton>(find.byType(FilledButton)).onPressed,
      isNull,
    );
    await showPanel(tester, [operation('busy', 24)], busy: true);
    expect(
      tester.widget<FilledButton>(find.byType(FilledButton)).onPressed,
      isNull,
    );
    expect(
      tester
          .widget<TextButton>(find.widgetWithText(TextButton, 'Cancel'))
          .onPressed,
      isNull,
    );
  });
}
