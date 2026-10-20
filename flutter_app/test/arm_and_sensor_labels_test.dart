import 'package:agribot/core/arm_controls.dart';
import 'package:agribot/core/sensor_labels.dart';
import 'package:agribot/models/robot.dart';
import 'package:agribot/widgets/common.dart' show PillTone;
import 'package:flutter_test/flutter_test.dart';

/// GET /arm payload shaped like pi/agribot/arm.py Arm.state().
Map<String, dynamic> arm({
  String status = 'manual (uncommissioned)',
  String? operation,
  bool commissioned = false,
  bool appControl = true,
  bool nudgeAllowed = true,
  bool driveLocked = false,
  bool confirmRaisedAllowed = true,
  bool retractAllowed = false,
}) =>
    {
      'status': status,
      'mode': 'manual',
      'commissioned': commissioned,
      'operation': operation,
      'probe': driveLocked ? 'partial' : 'unknown',
      'controls': {
        'appControl': appControl,
        'nudgeAllowed': nudgeAllowed,
        'driveLocked': driveLocked,
        'confirmRaisedAllowed': confirmRaisedAllowed,
        'retractAllowed': retractAllowed,
      },
    };

void main() {
  group('ArmPanelState', () {
    test('no arm support or no robot: everything disabled', () {
      final s = ArmPanelState.from(null, hasToken: true);
      expect(s.anyEnabled, isFalse);
      expect(s.message, contains('not available'));
    });

    test('robot-side states disable every control with an explanation', () {
      for (final (status, text) in [
        ('esp32 disabled', 'switched off'),
        ('offline', 'offline'),
        ('motion disabled', 'disabled'),
        ('config error', 'settings error'),
      ]) {
        final s = ArmPanelState.from(arm(status: status, driveLocked: true), hasToken: true);
        expect(s.anyEnabled, isFalse, reason: status);
        expect(s.message, contains(text), reason: status);
      }
    });

    test('busy arm, app control off, or missing token disable everything', () {
      expect(ArmPanelState.from(arm(operation: 'nudge'), hasToken: true).anyEnabled, isFalse);
      final off = ArmPanelState.from(arm(appControl: false), hasToken: true);
      expect(off.anyEnabled, isFalse);
      expect(off.message, contains('App arm control is off'));
      final noToken = ArmPanelState.from(arm(), hasToken: false);
      expect(noToken.anyEnabled, isFalse);
      expect(noToken.needsToken, isTrue);
    });

    test('manual uncommissioned arm: nudge only, labelled as such', () {
      final s = ArmPanelState.from(arm(), hasToken: true);
      expect(s.canNudge, isTrue);
      expect(s.canRetract, isFalse);
      expect(s.canConfirmRaised, isFalse); // nothing locked, nothing to confirm
      expect(s.message, contains('not commissioned'));
      expect(s.tone, PillTone.warn);
    });

    test('Arm Raised only when the robot reports a wheel lock it can verify', () {
      final locked = ArmPanelState.from(arm(driveLocked: true), hasToken: true);
      expect(locked.canConfirmRaised, isTrue);
      expect(locked.wheelsLocked, isTrue);
      expect(locked.message, contains('Wheels locked'));
      final unverifiable = ArmPanelState.from(arm(driveLocked: true, confirmRaisedAllowed: false), hasToken: true);
      expect(unverifiable.canConfirmRaised, isFalse);
    });

    test('Retract only when the robot offers it', () {
      expect(ArmPanelState.from(arm(commissioned: true), hasToken: true).canRetract, isFalse);
      final ready = ArmPanelState.from(arm(commissioned: true, retractAllowed: true), hasToken: true);
      expect(ready.canRetract, isTrue);
      expect(ready.message, contains('Hold to move'));
    });

    test('last error is shown but does not grant anything', () {
      final s = ArmPanelState.from(arm(nudgeAllowed: false), hasToken: true, error: 'stop was just requested');
      expect(s.message, 'stop was just requested');
      expect(s.tone, PillTone.bad);
      expect(s.canNudge, isFalse);
    });
  });

  group('sensor labels', () {
    const demo = {'soilMoisture': 'placeholder', 'dht22': 'simulated', 'rainfall': 'ok', 'ph': 'disabled'};

    test('badges mark simulated, placeholder and uncalibrated values only', () {
      final badges = metricBadges(demo);
      expect(badges['temperature'], 'Simulated');
      expect(badges['humidity'], 'Simulated');
      expect(badges['soilMoisture'], 'Placeholder');
      expect(badges['rainfall'], isNull);
      expect(badges['ph'], isNull);
      expect(readingBadge('uncalibrated'), 'Uncalibrated');
      expect(readingBadge('ok'), isNull);
      expect(readingBadge('stale'), isNull);
      expect(readingBadge(null), isNull);
    });

    test('notice explains demo data and is absent for real readings', () {
      final notice = sensorNotice(demo)!;
      expect(notice, contains('simulated'));
      expect(notice, contains('placeholder'));
      expect(sensorNotice({'soilMoisture': 'ok', 'dht22': 'ok', 'rainfall': 'ok'}), isNull);
      expect(sensorNotice(const {}), isNull);
    });

    test('malformed status is ignored', () {
      expect(parseSensorStatus(null), isEmpty);
      expect(parseSensorStatus('x'), isEmpty);
      expect(parseSensorStatus({'dht22': 'simulated', 'bad': 3}), {'dht22': 'simulated'});
    });

    test('presence parses the Pi heartbeat demo flags', () {
      final p = RobotPresence.fromJson({
        'online': true,
        'info': {
          'capabilities': {'sensors': 'demo (simulated climate)'},
          'demoMode': true,
          'sensorStatus': demo,
        },
      });
      expect(p.demoMode, isTrue);
      expect(p.sensors, 'demo (simulated climate)');
      expect(p.sensorStatus['dht22'], 'simulated');
      final old = RobotPresence.fromJson({'online': true, 'info': {}});
      expect(old.demoMode, isFalse);
      expect(old.sensorStatus, isEmpty);
    });
  });
}
