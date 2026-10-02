import 'dart:async';
import 'dart:math' as math;
import 'dart:ui';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:provider/provider.dart';

import '../core/theme.dart';
import '../services/pi_client.dart';
import '../state/ble_controller.dart';
import '../state/robot_controller.dart';
import '../widgets/common.dart';

/// Full-screen remote control: live camera behind, sensor chips on top,
/// joystick + stop at the bottom. Talks to the Pi directly over Wi-Fi.
class DriveScreen extends StatefulWidget {
  const DriveScreen({super.key});

  static Future<void> open(BuildContext context) =>
      Navigator.of(context).push(MaterialPageRoute<void>(fullscreenDialog: true, builder: (_) => const DriveScreen()));

  @override
  State<DriveScreen> createState() => _DriveScreenState();
}

class _DriveScreenState extends State<DriveScreen> {
  static const _speeds = [('Slow', 0.45), ('Normal', 0.75), ('Fast', 1.0)];

  PiClient? _pi;
  String? _ip;
  Uint8List? _frame;
  String? _cameraError;
  bool _reachable = false;
  Map<String, dynamic>? _sensors;
  String? _gamepad;
  String? _driveSource;

  Offset _stick = Offset.zero;
  bool _sentZero = true;
  bool _inFlight = false;
  int _speed = 1;

  Timer? _driveTimer, _sensorTimer;
  bool _alive = true;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) => _connect());
  }

  String? _robotIp() => context.read<BleController>().status?.ip ?? context.read<RobotController>().presence?.ip;

  void _connect() {
    final ip = _robotIp();
    if (ip == null || ip == _ip) {
      setState(() {});
      return;
    }
    _pi?.close();
    _ip = ip;
    _pi = PiClient(ip);
    _cameraLoop(_pi!);
    _driveTimer?.cancel();
    _driveTimer = Timer.periodic(const Duration(milliseconds: 100), (_) => _sendDrive());
    _sensorTimer?.cancel();
    _sensorTimer = Timer.periodic(const Duration(seconds: 3), (_) => _pollStatus());
    _pollStatus();
  }

  Future<void> _cameraLoop(PiClient pi) async {
    while (_alive && identical(pi, _pi)) {
      try {
        final bytes = await pi.frame();
        if (!_alive) return;
        setState(() {
          _frame = bytes;
          _cameraError = null;
          _reachable = true;
        });
      } catch (e) {
        if (!_alive) return;
        setState(() => _cameraError = e.toString().replaceFirst('Exception: ', ''));
        await Future<void>.delayed(const Duration(seconds: 2));
      }
    }
  }

  Future<void> _pollStatus() async {
    final pi = _pi;
    if (pi == null) return;
    try {
      final results = await Future.wait([pi.sensors(), pi.driveState()]);
      if (!_alive) return;
      setState(() {
        _sensors = Map<String, dynamic>.from(results[0]['sensors'] as Map);
        _gamepad = results[1]['gamepad'] as String?;
        _driveSource = results[1]['source'] as String?;
        _reachable = true;
      });
    } catch (_) {
      if (_alive) setState(() => _reachable = false);
    }
  }

  Future<void> _sendDrive() async {
    final pi = _pi;
    if (pi == null || _inFlight) return;
    final idle = _stick == Offset.zero;
    if (idle && _sentZero) return;
    _inFlight = true;
    final scale = _speeds[_speed].$2;
    try {
      final state = await pi.drive(-_stick.dy * scale, _stick.dx * scale);
      _sentZero = idle;
      _driveSource = state['source'] as String?;
      _reachable = true;
    } catch (_) {
      _reachable = false;
    } finally {
      _inFlight = false;
    }
  }

  void _emergencyStop() {
    HapticFeedback.heavyImpact();
    setState(() => _stick = Offset.zero);
    _sentZero = true;
    _pi?.stop().catchError((_) => <String, dynamic>{});
  }

  @override
  void dispose() {
    _alive = false;
    _driveTimer?.cancel();
    _sensorTimer?.cancel();
    final pi = _pi;
    if (pi != null) pi.stop().whenComplete(pi.close).catchError((_) => <String, dynamic>{});
    super.dispose();
  }

  double? _reading(String key) {
    final live = _sensors?[key];
    if (live is num) return live.toDouble();
    final s = context.read<RobotController>().dashboard?.sensors;
    return switch (key) {
      'temperature' => s?.temperature,
      'humidity' => s?.humidity,
      'soilMoisture' => s?.soilMoisture,
      'rainfall' => s?.rainfall,
      _ => null,
    };
  }

  @override
  Widget build(BuildContext context) {
    // Rebuild when the Pi's IP becomes known (BLE or heartbeat).
    context.watch<BleController>();
    context.watch<RobotController>();
    if (_ip == null && _robotIp() != null) WidgetsBinding.instance.addPostFrameCallback((_) => _connect());

    final chips = [
      (Icons.thermostat, _reading('temperature'), '°C'),
      (Icons.water_drop_outlined, _reading('humidity'), '%'),
      (Icons.grass, _reading('soilMoisture'), '%'),
      (Icons.umbrella_outlined, _reading('rainfall'), '%'),
    ];
    final (statusText, tone) = _ip == null
        ? ('Robot IP unknown', PillTone.bad)
        : !_reachable
            ? ('Connecting to $_ip…', PillTone.warn)
            : _driveSource == 'gamepad'
                ? ('Controller driving', PillTone.good)
                : ('Connected', PillTone.good);

    return AnnotatedRegion<SystemUiOverlayStyle>(
      value: SystemUiOverlayStyle.light,
      child: Scaffold(
        backgroundColor: AgriColors.forestDeep,
        body: Stack(fit: StackFit.expand, children: [
          // Camera background
          if (_frame != null)
            Image.memory(_frame!, fit: BoxFit.cover, gaplessPlayback: true)
          else
            Hatch(
              color: const Color(0x14FFFFFF),
              child: Center(
                child: Padding(
                  padding: const EdgeInsets.all(40),
                  child: Column(mainAxisSize: MainAxisSize.min, children: [
                    const Icon(Icons.videocam_off_outlined, color: AgriColors.textOnDarkMuted, size: 40),
                    const SizedBox(height: 12),
                    Text(
                      _ip == null ? 'Connect AgriBot to Wi-Fi first (Robot tab → Share network).' : _cameraError ?? 'Starting camera…',
                      textAlign: TextAlign.center,
                      style: const TextStyle(color: AgriColors.textOnDarkMuted, fontSize: 14, height: 1.4),
                    ),
                  ]),
                ),
              ),
            ),
          // Shade top and bottom so the controls stay readable on any image
          const DecoratedBox(
            decoration: BoxDecoration(
              gradient: LinearGradient(
                begin: Alignment.topCenter,
                end: Alignment.bottomCenter,
                colors: [Color(0x99000000), Color(0x00000000), Color(0x00000000), Color(0xB3000000)],
                stops: [0, 0.22, 0.6, 1],
              ),
            ),
          ),
          SafeArea(
            child: Padding(
              padding: const EdgeInsets.fromLTRB(16, 8, 16, 16),
              child: Column(children: [
                Row(children: [
                  _Glass(
                    shape: BoxShape.circle,
                    child: IconButton(
                      tooltip: 'Close',
                      icon: const Icon(Icons.close, color: Colors.white),
                      onPressed: () => Navigator.of(context).pop(),
                    ),
                  ),
                  const SizedBox(width: 12),
                  const Expanded(child: Text('Drive', style: TextStyle(color: Colors.white, fontSize: 24, fontWeight: FontWeight.w800))),
                  StatusPill(statusText, tone: tone, dark: true),
                ]),
                const SizedBox(height: 12),
                Row(children: [
                  for (final (icon, value, unit) in chips) ...[
                    Expanded(
                      child: _Glass(
                        radius: 16,
                        child: Padding(
                          padding: const EdgeInsets.symmetric(vertical: 8, horizontal: 6),
                          child: Row(mainAxisAlignment: MainAxisAlignment.center, children: [
                            Icon(icon, size: 14, color: AgriColors.mint),
                            const SizedBox(width: 4),
                            Flexible(
                              child: Text(
                                value == null ? '—' : '${value.toStringAsFixed(0)}$unit',
                                maxLines: 1,
                                style: const TextStyle(color: Colors.white, fontSize: 13, fontWeight: FontWeight.w800),
                              ),
                            ),
                          ]),
                        ),
                      ),
                    ),
                    if (icon != chips.last.$1) const SizedBox(width: 6),
                  ],
                ]),
                if (_gamepad != null) ...[
                  const SizedBox(height: 8),
                  Align(
                    alignment: Alignment.centerLeft,
                    child: _Glass(
                      radius: 999,
                      child: Padding(
                        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
                        child: Row(mainAxisSize: MainAxisSize.min, children: [
                          const Icon(Icons.sports_esports, size: 14, color: AgriColors.mint),
                          const SizedBox(width: 6),
                          Text(_gamepad!, style: const TextStyle(color: Colors.white, fontSize: 11, fontWeight: FontWeight.w700)),
                        ]),
                      ),
                    ),
                  ),
                ],
                const Spacer(),
                Row(crossAxisAlignment: CrossAxisAlignment.end, children: [
                  Joystick(
                    size: 168,
                    enabled: _pi != null,
                    onChanged: (v) {
                      if (_stick == Offset.zero && v != Offset.zero) HapticFeedback.selectionClick();
                      setState(() => _stick = v);
                      if (v == Offset.zero) _sentZero = false; // make sure the release reaches the Pi
                    },
                  ),
                  const Spacer(),
                  Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.end, children: [
                    _Glass(
                      radius: 20,
                      child: Padding(
                        padding: const EdgeInsets.all(4),
                        child: Column(mainAxisSize: MainAxisSize.min, children: [
                          for (var i = _speeds.length - 1; i >= 0; i--)
                            GestureDetector(
                              onTap: () => setState(() => _speed = i),
                              child: AnimatedContainer(
                                duration: const Duration(milliseconds: 180),
                                width: 76,
                                padding: const EdgeInsets.symmetric(vertical: 8),
                                decoration: BoxDecoration(color: i == _speed ? AgriColors.mint : Colors.transparent, borderRadius: BorderRadius.circular(16)),
                                child: Text(
                                  _speeds[i].$1,
                                  textAlign: TextAlign.center,
                                  style: TextStyle(color: i == _speed ? AgriColors.forest : Colors.white, fontSize: 13, fontWeight: FontWeight.w800),
                                ),
                              ),
                            ),
                        ]),
                      ),
                    ),
                    const SizedBox(height: 14),
                    Semantics(
                      button: true,
                      label: 'Stop',
                      child: GestureDetector(
                        onTap: _emergencyStop,
                        child: Container(
                          width: 84,
                          height: 84,
                          alignment: Alignment.center,
                          decoration: BoxDecoration(
                            color: AgriColors.error,
                            shape: BoxShape.circle,
                            border: Border.all(color: Colors.white.withValues(alpha: 0.85), width: 3),
                            boxShadow: kStrongShadow,
                          ),
                          child: const Text('STOP', style: TextStyle(color: Colors.white, fontSize: 16, fontWeight: FontWeight.w900, letterSpacing: 1)),
                        ),
                      ),
                    ),
                  ]),
                ]),
              ]),
            ),
          ),
        ]),
      ),
    );
  }
}

/// Frosted panel that sits over the camera image.
class _Glass extends StatelessWidget {
  const _Glass({required this.child, this.radius = 20, this.shape = BoxShape.rectangle});
  final Widget child;
  final double radius;
  final BoxShape shape;

  @override
  Widget build(BuildContext context) {
    final decoration = BoxDecoration(
      color: Colors.black.withValues(alpha: 0.28),
      shape: shape,
      borderRadius: shape == BoxShape.circle ? null : BorderRadius.circular(radius),
      border: Border.all(color: Colors.white.withValues(alpha: 0.16)),
    );
    final blurred = BackdropFilter(filter: ImageFilter.blur(sigmaX: 14, sigmaY: 14), child: DecoratedBox(decoration: decoration, child: child));
    return shape == BoxShape.circle ? ClipOval(child: blurred) : ClipRRect(borderRadius: BorderRadius.circular(radius), child: blurred);
  }
}

/// Virtual thumbstick. Reports x (right +) and y (down +) in -1..1, zero on release.
class Joystick extends StatefulWidget {
  const Joystick({super.key, required this.onChanged, this.size = 160, this.enabled = true});
  final ValueChanged<Offset> onChanged;
  final double size;
  final bool enabled;

  @override
  State<Joystick> createState() => _JoystickState();
}

class _JoystickState extends State<Joystick> {
  Offset _knob = Offset.zero; // pixels from centre

  double get _travel => widget.size / 2 - widget.size * 0.17;

  void _move(Offset local) {
    final centre = Offset(widget.size / 2, widget.size / 2);
    var d = local - centre;
    if (d.distance > _travel) d = d / d.distance * _travel;
    setState(() => _knob = d);
    final v = d / _travel;
    // Small dead zone so a resting thumb doesn't creep.
    widget.onChanged(v.distance < 0.08 ? Offset.zero : Offset(v.dx.clamp(-1, 1), v.dy.clamp(-1, 1)));
  }

  void _release() {
    setState(() => _knob = Offset.zero);
    widget.onChanged(Offset.zero);
  }

  @override
  Widget build(BuildContext context) {
    final knob = widget.size * 0.34;
    return Opacity(
      opacity: widget.enabled ? 1 : 0.5,
      child: GestureDetector(
        onPanStart: widget.enabled ? (d) => _move(d.localPosition) : null,
        onPanUpdate: widget.enabled ? (d) => _move(d.localPosition) : null,
        onPanEnd: widget.enabled ? (_) => _release() : null,
        onPanCancel: widget.enabled ? _release : null,
        child: SizedBox(
          width: widget.size,
          height: widget.size,
          child: Stack(alignment: Alignment.center, children: [
            _Glass(shape: BoxShape.circle, child: SizedBox(width: widget.size, height: widget.size, child: CustomPaint(painter: _RingPainter()))),
            AnimatedContainer(
              duration: _knob == Offset.zero ? const Duration(milliseconds: 160) : Duration.zero,
              curve: Curves.easeOutBack,
              transform: Matrix4.translationValues(_knob.dx, _knob.dy, 0),
              width: knob,
              height: knob,
              decoration: BoxDecoration(
                color: AgriColors.mint,
                shape: BoxShape.circle,
                border: Border.all(color: Colors.white, width: 2),
                boxShadow: const [BoxShadow(color: Color(0x66000000), blurRadius: 12, offset: Offset(0, 4))],
              ),
              child: const Icon(Icons.agriculture, color: AgriColors.forest),
            ),
          ]),
        ),
      ),
    );
  }
}

class _RingPainter extends CustomPainter {
  @override
  void paint(Canvas canvas, Size size) {
    final c = size.center(Offset.zero);
    final r = size.width / 2;
    final ring = Paint()
      ..style = PaintingStyle.stroke
      ..strokeWidth = 1.2
      ..color = Colors.white.withValues(alpha: 0.22);
    canvas.drawCircle(c, r * 0.62, ring);
    final arrow = Paint()..color = Colors.white.withValues(alpha: 0.7);
    for (var i = 0; i < 4; i++) {
      final a = i * math.pi / 2 - math.pi / 2;
      final tip = c + Offset(math.cos(a), math.sin(a)) * (r - 10);
      final side = Offset(-math.sin(a), math.cos(a)) * 6;
      final back = c + Offset(math.cos(a), math.sin(a)) * (r - 19);
      canvas.drawPath(Path()..moveTo(tip.dx, tip.dy)..lineTo(back.dx + side.dx, back.dy + side.dy)..lineTo(back.dx - side.dx, back.dy - side.dy)..close(), arrow);
    }
  }

  @override
  bool shouldRepaint(covariant CustomPainter oldDelegate) => false;
}
