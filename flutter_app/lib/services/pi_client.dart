import 'dart:convert';
import 'dart:typed_data';

import 'package:http/http.dart' as http;

/// Error reply from the Pi. [statusCode] tells a refusal (e.g. 409: wheels locked
/// by the arm, 403: not authorised) apart from the robot being unreachable.
class PiException implements Exception {
  PiException(this.statusCode, this.message);
  final int statusCode;
  final String message;
  @override
  String toString() => message;
}

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

  /// Arm state (mode, controls.appControl, controls.nudgeAllowed, ...). Throws on Pis without arm support.
  Future<Map<String, dynamic>> arm() async => _data(await _http.get(Uri.parse('$base/arm')).timeout(const Duration(seconds: 3)));

  /// One short, capped Arm Up / Arm Down move (elbow + wrist together). [token] = AGRIBOT_LOCAL_TOKEN on the Pi.
  Future<Map<String, dynamic>> armNudge(String direction, String token) async => _data(await _http
      .post(Uri.parse('$base/arm/nudge'),
          headers: {'Content-Type': 'application/json', 'X-AgriBot-Token': token}, body: jsonEncode({'direction': direction}))
      .timeout(const Duration(seconds: 3)));

  /// Starts the robot's commissioned retract sequence (202 Accepted; poll [arm]).
  /// The Pi only offers it (controls.retractAllowed) for a commissioned, homed arm.
  Future<Map<String, dynamic>> armRetract(String token) async => _data(await _http
      .post(Uri.parse('$base/arm/retract'), headers: {'Content-Type': 'application/json', 'X-AgriBot-Token': token}, body: jsonEncode({}))
      .timeout(const Duration(seconds: 4)));

  /// Stops both arm servos. Always allowed, no token needed.
  Future<Map<String, dynamic>> armStop() => _post('/arm/stop', {});

  /// Operator confirms the arm is raised and the probe is clear, releasing the wheel lock.
  /// The Pi only accepts it with the token and when the ESP32 reports both servos stopped.
  Future<Map<String, dynamic>> armConfirmRaised(String token) async => _data(await _http
      .post(Uri.parse('$base/arm/confirm-raised'),
          headers: {'Content-Type': 'application/json', 'X-AgriBot-Token': token}, body: jsonEncode({'confirm': 'raised-and-clear'}))
      .timeout(const Duration(seconds: 4)));

  Future<Map<String, dynamic>> driveState() async => _data(await _http.get(Uri.parse('$base/drive')).timeout(const Duration(seconds: 3)));

  /// Live readings straight from the Pi: {sensors: {...}, sources: {...}}.
  Future<Map<String, dynamic>> sensors() async => _data(await _http.get(Uri.parse('$base/sensors')).timeout(const Duration(seconds: 5)));

  Future<Map<String, dynamic>> _post(String path, Map<String, dynamic> body) async => _data(await _http
      .post(Uri.parse('$base$path'), headers: {'Content-Type': 'application/json'}, body: jsonEncode(body))
      .timeout(const Duration(seconds: 2)));

  Map<String, dynamic> _data(http.Response res) {
    if (res.statusCode < 200 || res.statusCode > 299) throw PiException(res.statusCode, _message(res) ?? 'HTTP ${res.statusCode}');
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
