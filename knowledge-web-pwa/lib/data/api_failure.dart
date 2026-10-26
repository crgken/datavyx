class ApiFailure implements Exception {
  final String code;
  final String message;
  const ApiFailure(this.code, [this.message = '']);
  @override
  String toString() => '$code: $message';
}
