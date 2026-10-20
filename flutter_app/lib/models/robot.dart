// API models shared with the Express backend (backend/src/routes/robot.routes.js).

import '../core/sensor_labels.dart';

double _d(dynamic v, [double fallback = 0]) => v is num ? v.toDouble() : fallback;
double? _dn(dynamic v) => v is num ? v.toDouble() : null;
DateTime? _date(dynamic v) => v is String ? DateTime.tryParse(v) : null;

class SensorData {
  SensorData({required this.soilMoisture, required this.temperature, required this.humidity, required this.rainfall, this.ph});
  final double soilMoisture, temperature, humidity, rainfall;
  final double? ph;

  factory SensorData.fromJson(Map<String, dynamic> j) => SensorData(
        soilMoisture: _d(j['soilMoisture']),
        temperature: _d(j['temperature']),
        humidity: _d(j['humidity']),
        rainfall: _d(j['rainfall']),
        ph: _dn(j['ph']),
      );
}

class DashboardData {
  DashboardData({required this.sensors, required this.status, required this.lastUpdated, required this.latitude, required this.longitude});
  final SensorData sensors;
  final String status;
  final DateTime? lastUpdated;
  final double latitude, longitude;

  factory DashboardData.fromJson(Map<String, dynamic> j) => DashboardData(
        sensors: SensorData.fromJson(Map<String, dynamic>.from(j['sensors'] as Map)),
        status: (j['robot']?['status'] as String?) ?? 'offline',
        lastUpdated: _date(j['robot']?['lastUpdated']),
        latitude: _d(j['location']?['latitude']),
        longitude: _d(j['location']?['longitude']),
      );

  /// Sample values shown before the robot's first reading.
  static DashboardData sample() => DashboardData(
        sensors: SensorData(soilMoisture: 48, temperature: 29, humidity: 64, rainfall: 0, ph: 6.5),
        status: 'offline',
        lastUpdated: null,
        latitude: 19.047838,
        longitude: 72.872712,
      );
}

enum HistoryRange { hour, day, week }

class HistoryPoint {
  HistoryPoint({required this.at, required this.values});
  final DateTime? at;
  final Map<String, double?> values;

  factory HistoryPoint.fromJson(Map<String, dynamic> j) => HistoryPoint(
        at: _date(j['at']),
        values: {for (final k in ['soilMoisture', 'temperature', 'humidity', 'rainfall', 'ph']) k: _dn(j[k])},
      );
}

class LeafDetection {
  LeafDetection({required this.label, required this.confidence});
  final String label;
  final double confidence;
  factory LeafDetection.fromJson(Map<String, dynamic> j) => LeafDetection(label: j['label'] as String? ?? '?', confidence: _d(j['confidence']));
}

class CropScan {
  CropScan({required this.id, required this.status, required this.label, required this.confidence, required this.detections, this.createdAt, this.image, this.inferenceMs});
  final String id;
  final String status; // healthy | disease | none
  final String label;
  final double confidence;
  final List<LeafDetection> detections;
  final DateTime? createdAt;
  final String? image; // data:image/jpeg;base64,...
  final int? inferenceMs;

  factory CropScan.fromJson(Map<String, dynamic> j) {
    final summary = Map<String, dynamic>.from((j['summary'] as Map?) ?? {});
    return CropScan(
      id: j['id'] as String? ?? '',
      status: summary['status'] as String? ?? 'none',
      label: summary['label'] as String? ?? 'Unknown',
      confidence: _d(summary['confidence']),
      detections: ((j['detections'] as List?) ?? []).map((e) => LeafDetection.fromJson(Map<String, dynamic>.from(e as Map))).toList(),
      createdAt: _date(j['createdAt']),
      image: j['image'] as String?,
      inferenceMs: (j['inferenceMs'] as num?)?.toInt(),
    );
  }

  CropScan withoutImage() => CropScan(id: id, status: status, label: label, confidence: confidence, detections: detections, createdAt: createdAt, inferenceMs: inferenceMs);
}

/// Heartbeat details the Pi reports (pi/agribot_service.py AgriBot.status).
class RobotPresence {
  RobotPresence({
    required this.online,
    this.lastSeenAt,
    this.camera,
    this.model = false,
    this.sensors,
    this.ble = false,
    this.ssid,
    this.ip,
    this.version,
    this.sensorStatus = const {},
    this.demoMode = false,
  });
  final bool online;
  final DateTime? lastSeenAt;
  final String? camera;
  final bool model;
  final String? sensors;
  final bool ble;
  final String? ssid, ip, version;

  /// Per-sensor status from the Pi (soilMoisture, dht22, rainfall, ph): ok,
  /// stale, simulated, placeholder, uncalibrated, ... Empty on older Pi software.
  final Map<String, String> sensorStatus;

  /// True while the Pi runs SENSOR_DEMO_MODE (simulated temperature/humidity).
  final bool demoMode;

  factory RobotPresence.fromJson(Map<String, dynamic> j) {
    final info = Map<String, dynamic>.from((j['info'] as Map?) ?? {});
    final caps = Map<String, dynamic>.from((info['capabilities'] as Map?) ?? {});
    final net = Map<String, dynamic>.from((info['network'] as Map?) ?? {});
    return RobotPresence(
      online: j['online'] == true,
      lastSeenAt: _date(j['lastSeenAt']),
      camera: caps['camera'] as String?,
      model: caps['model'] == true,
      sensors: caps['sensors'] as String?,
      ble: caps['ble'] == true,
      ssid: net['ssid'] as String?,
      ip: net['ip'] as String?,
      version: info['version'] as String?,
      sensorStatus: parseSensorStatus(info['sensorStatus']),
      demoMode: info['demoMode'] == true,
    );
  }
}

class AuthUser {
  AuthUser({required this.name, required this.email});
  final String name, email;
  factory AuthUser.fromJson(Map<String, dynamic> j) => AuthUser(name: j['name'] as String? ?? 'Farmer', email: j['email'] as String? ?? '');
}
