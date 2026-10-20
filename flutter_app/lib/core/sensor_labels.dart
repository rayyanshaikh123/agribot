/// Honest labels for readings that are not live measurements.
///
/// The Pi reports a status per sensor (GET /sensors `status`, and the same map
/// as `sensorStatus` in its heartbeat): ok / stale = measured; simulated =
/// SENSOR_DEMO_MODE or SIMULATE_SENSORS data; placeholder = stand-in value for a
/// sensor that is not installed; uncalibrated = pH voltage without calibration.
/// Keys: soilMoisture, dht22 (temperature + humidity), rainfall, ph.
library;

/// Small badge for a metric, or null when the value is a measurement or the
/// status is unknown (older Pi software).
String? readingBadge(String? status) => switch (status) {
      'simulated' => 'Simulated',
      'placeholder' => 'Placeholder',
      'uncalibrated' => 'Uncalibrated',
      _ => null,
    };

/// Badge for each dashboard metric, from the Pi's status map.
Map<String, String?> metricBadges(Map<String, String> status) => {
      'temperature': readingBadge(status['dht22']),
      'humidity': readingBadge(status['dht22']),
      'soilMoisture': readingBadge(status['soilMoisture']),
      'rainfall': readingBadge(status['rainfall']),
      'ph': readingBadge(status['ph']),
    };

/// One-sentence notice for the dashboard, or null when every reading is real.
String? sensorNotice(Map<String, String> status) {
  final parts = <String>[];
  if (status['dht22'] == 'simulated') parts.add('Temperature and humidity are simulated (demo mode), not measured.');
  if (status['rainfall'] == 'simulated') parts.add('Rain is simulated.');
  if (status['soilMoisture'] == 'simulated') parts.add('Soil moisture is simulated.');
  if (status['soilMoisture'] == 'placeholder') parts.add('Soil moisture is a placeholder: no sensor is installed.');
  return parts.isEmpty ? null : parts.join(' ');
}

/// Reads a `{name: status}` map from JSON, ignoring anything malformed.
Map<String, String> parseSensorStatus(Object? json) {
  if (json is! Map) return const {};
  return {
    for (final entry in json.entries)
      if (entry.key is String && entry.value is String) entry.key as String: entry.value as String,
  };
}
