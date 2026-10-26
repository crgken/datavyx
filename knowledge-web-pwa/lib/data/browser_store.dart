import 'package:idb_shim/idb_browser.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// Drafts use real IndexedDB. Preferences never contain credentials.
class BrowserStore {
  Future<Database>? _database;
  final _preferences = SharedPreferencesAsync();
  Future<Database> get database => _database ??= idbFactoryBrowser.open(
    'atlas-knowledge-web', version: 1,
    onUpgradeNeeded: (event) => event.database.createObjectStore('drafts'),
  );
  Future<String?> read(String key) async {
    if (!key.startsWith('draft:')) return _preferences.getString(key);
    final db = await database;
    final tx = db.transaction('drafts', idbModeReadOnly);
    final value = await tx.objectStore('drafts').getObject(key);
    await tx.completed;
    return value as String?;
  }
  Future<void> write(String key, String value) async {
    if (!key.startsWith('draft:')) return _preferences.setString(key, value);
    final db = await database;
    final tx = db.transaction('drafts', idbModeReadWrite);
    await tx.objectStore('drafts').put(value, key);
    await tx.completed;
  }
  Future<void> delete(String key) async {
    if (!key.startsWith('draft:')) return _preferences.remove(key);
    final db = await database;
    final tx = db.transaction('drafts', idbModeReadWrite);
    await tx.objectStore('drafts').delete(key);
    await tx.completed;
  }
  Future<void> clearPrefix(String prefix) async {
    final db = await database;
    final tx = db.transaction('drafts', idbModeReadWrite);
    final store = tx.objectStore('drafts');
    final keys = await store.getAllKeys();
    for (final key in keys.whereType<String>().where((k) => k.startsWith(prefix))) {
      await store.delete(key);
    }
    await tx.completed;
  }
}
