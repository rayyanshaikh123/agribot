import 'dart:convert';
import 'dart:typed_data';

import 'package:http/http.dart' as http;

/// Direct local-network client for the Pi's HTTP API (pi/agribot_service.py).
/// Driving goes straight to the robot; the cloud backend is far too slow for it.
class PiClient {
  PiClient(String ip, {int port = 8000}) : base = 'http://$ip:$port';

  final String base;
  final _http = http.Client();

  Future<Uint8List> frame() async {
    final res = await _http.get(Uri.parse('$base/camera/frame.jpg')).timeout(const Duration(seconds: 4));
    if (res.statusCode != 200) throw Exception(_message(res) ?? 'Camera unavailable');
    return res.bodyBytes;
  }

  Future<Map<String, dynamic>> drive(double throttle, double turn) => _post('/drive', {'throttle': throttle, 'turn': turn});

  Future<Map<String, dynamic>> stop() => _post('/drive/stop', {});

  Future<Map<String, dynamic>> driveState() async => _data(await _http.get(Uri.parse('$base/drive')).timeout(const Duration(seconds: 3)));

  /// Live readings straight from the Pi: {sensors: {...}, sources: {...}}.
  Future<Map<String, dynamic>> sensors() async => _data(await _http.get(Uri.parse('$base/sensors')).timeout(const Duration(seconds: 5)));

  Future<Map<String, dynamic>> _post(String path, Map<String, dynamic> body) async => _data(await _http
      .post(Uri.parse('$base$path'), headers: {'Content-Type': 'application/json'}, body: jsonEncode(body))
      .timeout(const Duration(seconds: 2)));

  Map<String, dynamic> _data(http.Response res) {
    if (res.statusCode != 200) throw Exception(_message(res) ?? 'HTTP ${res.statusCode}');
    return Map<String, dynamic>.from((jsonDecode(res.body) as Map)['data'] as Map);
  }

  String? _message(http.Response res) {
    try {
      return (jsonDecode(res.body) as Map)['message'] as String?;
    } catch (_) {
      return null;
    }
  }

  void close() => _http.close();
}
