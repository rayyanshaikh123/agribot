import 'dart:async';
import 'dart:math' as math;
import 'dart:ui';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:provider/provider.dart';

import '../core/arm_controls.dart';
import '../core/sensor_labels.dart';
import '../core/theme.dart';
import '../services/pi_client.dart';
import '../state/ble_controller.dart';
import '../state/robot_controller.dart';
import '../widgets/common.dart';

/// Full-screen remote control: live camera behind, sensor chips on top,
/// arm panel and joystick + stop at the bottom. Talks to the Pi directly over Wi-Fi.
///
/// Driving: push the joystick (up = forward, down = reverse, left/right = turn);
/// letting go sends zero, and the Pi also stops the wheels on its own if
/// commands stop arriving. STOP stops wheels and arm.
///
/// Arm (pH probe): hold Up/Down to move the elbow (servo 1) and wrist (servo 2)
/// together in short capped nudges; releasing stops both. The Pi and ESP32
/// enforce every limit and timeout; this screen only offers what the robot
/// reports as allowed (see core/arm_controls.dart).
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
  Map<String, String> _sensorStatus = const {};
  String? _gamepad;
  String? _driveSource;
  String? _driveBlocked; // the Pi's reason when it refuses to drive (e.g. arm interlock)

  Offset _stick = Offset.zero;
  bool _sentZero = true;
  bool _inFlight = false;
  int _speed = 1;

  Timer? _driveTimer, _sensorTimer;
  bool _alive = true;

  static const _storage = FlutterSecureStorage();
  static const _armTokenKey = 'agribot_arm_token';
  Map<String, dynamic>? _arm; // GET /arm from the Pi, null if unsupported/unreachable
  String? _armHeld; // 'up' | 'down' while a button is held
  String? _armError;
  String _armToken = '';

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) => _connect());
    _storage.read(key: _armTokenKey).then((value) {
      if (mounted && value != null) setState(() => _armToken = value);
    });
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
        _sensorStatus = parseSensorStatus(results[0]['status']);
        _gamepad = results[1]['gamepad'] as String?;
        _driveSource = results[1]['source'] as String?;
        _reachable = true;
      });
    } catch (_) {
      if (_alive) setState(() => _reachable = false);
    }
    // Separate request: older Pi software has no /arm and must not break driving.
    try {
      final arm = await pi.arm();
      if (_alive) setState(() => _arm = arm);
    } catch (_) {
      if (_alive) setState(() => _arm = null);
    }
  }

  // --- driving --------------------------------------------------------------

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
      if (_driveBlocked != null && _alive) setState(() => _driveBlocked = null);
    } on PiException catch (e) {
      // Reachable, but the robot refused (409 = wheels locked by the arm interlock).
      _reachable = true;
      _sentZero = idle;
      if (_alive && e.statusCode == 409) setState(() => _driveBlocked = e.message);
    } catch (_) {
      _reachable = false;
    } finally {
      _inFlight = false;
    }
  }

  void _emergencyStop() {
    HapticFeedback.heavyImpact();
    setState(() {
      _stick = Offset.zero;
      _armHeld = null;
    });
    _sentZero = true;
    _pi?.stop().catchError((_) => <String, dynamic>{});
    _pi?.armStop().catchError((_) => <String, dynamic>{}); // STOP also stops the arm
  }

  @override
  void dispose() {
    _alive = false;
    _driveTimer?.cancel();
    _sensorTimer?.cancel();
    _armHeld = null;
    final pi = _pi;
    if (pi != null) {
      Future.wait([
        pi.stop().catchError((_) => <String, dynamic>{}),
        pi.armStop().catchError((_) => <String, dynamic>{}),
      ]).whenComplete(pi.close);
    }
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

  // --- arm ------------------------------------------------------------------

  ArmPanelState get _armState => ArmPanelState.from(_pi == null ? null : _arm, hasToken: _armToken.isNotEmpty, error: _armError);

  /// Repeats short capped nudges while the button is held; each one ends by itself on the Pi.
  Future<void> _armHold(String direction) async {
    final pi = _pi;
    if (pi == null || !_armState.canNudge || _armHeld != null) return;
    HapticFeedback.selectionClick();
    setState(() {
      _armHeld = direction;
      _armError = null;
    });
    while (_alive && _armHeld == direction && identical(pi, _pi)) {
      try {
        await pi.armNudge(direction, _armToken);
      } catch (e) {
        // A request refused just after release (stop latch) is expected: ignore it.
        if (_alive && _armHeld == direction) {
          setState(() {
            _armError = e.toString().replaceFirst('Exception: ', '');
            _armHeld = null;
          });
          pi.armStop().catchError((_) => <String, dynamic>{});
        }
        break;
      }
    }
    _pollStatus();
  }

  void _armRelease() {
    if (_armHeld == null) return;
    setState(() => _armHeld = null);
    _pi?.armStop().catchError((_) => <String, dynamic>{});
  }

  Future<void> _editArmToken() async {
    final value = await showModalBottomSheet<String>(
      context: context,
      isScrollControlled: true,
      backgroundColor: AgriColors.surface,
      shape: const RoundedRectangleBorder(borderRadius: BorderRadius.vertical(top: Radius.circular(34))),
      builder: (_) => _ArmTokenSheet(initial: _armToken),
    );
    if (value == null) return;
    await _storage.write(key: _armTokenKey, value: value);
    if (mounted) {
      setState(() {
        _armToken = value;
        _armError = null;
      });
    }
  }

  Future<void> _confirmRaised() async {
    final pi = _pi;
    if (pi == null || !_armState.canConfirmRaised) return;
    final confirmed = await _showArmConfirmSheet(
      title: 'Confirm arm raised',
      body: 'AgriBot cannot measure where the arm is. Only continue if you can see the robot right now and have checked that:',
      checks: const ['The arm is fully raised', 'The pH probe is clear of the ground', 'Nothing on the arm is moving'],
      footnote: 'This records your visual check and unlocks the wheels. It is not a position measurement. '
          'The robot refuses it if the arm controller is offline or a servo is still running.',
      action: 'Confirm and unlock wheels',
    );
    if (confirmed != true || !mounted) return;
    await _runArmRequest(() => pi.armConfirmRaised(_armToken), success: 'Arm confirmed raised. Wheels unlocked.');
  }

  Future<void> _retract() async {
    final pi = _pi;
    if (pi == null || !_armState.canRetract) return;
    final confirmed = await _showArmConfirmSheet(
      title: 'Retract the arm',
      body: "Runs the robot's commissioned retract sequence back to the rest pose. Before starting, check that:",
      checks: const ['Hands and objects are clear of the arm', 'The robot is not moving'],
      footnote: 'The position is an estimate from timed moves. Watch the arm and press STOP if anything looks wrong.',
      action: 'Start retract',
    );
    if (confirmed != true || !mounted) return;
    await _runArmRequest(() => pi.armRetract(_armToken), success: 'Retracting. Watch the arm; STOP stops it.');
  }

  Future<void> _runArmRequest(Future<Map<String, dynamic>> Function() request, {required String success}) async {
    try {
      await request();
      if (!mounted) return;
      setState(() => _armError = null);
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(success)));
    } catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Not done: ${e.toString().replaceFirst('Exception: ', '')}')));
    }
    _pollStatus();
  }

  Future<bool?> _showArmConfirmSheet({
    required String title,
    required String body,
    required List<String> checks,
    required String footnote,
    required String action,
  }) =>
      showModalBottomSheet<bool>(
        context: context,
        isScrollControlled: true,
        backgroundColor: AgriColors.surface,
        shape: const RoundedRectangleBorder(borderRadius: BorderRadius.vertical(top: Radius.circular(34))),
        builder: (_) => _ArmConfirmSheet(title: title, body: body, checks: checks, footnote: footnote, action: action),
      );

  // --- layout ---------------------------------------------------------------

  @override
  Widget build(BuildContext context) {
    // Rebuild when the Pi's IP becomes known (BLE or heartbeat).
    context.watch<BleController>();
    context.watch<RobotController>();
    if (_ip == null && _robotIp() != null) WidgetsBinding.instance.addPostFrameCallback((_) => _connect());

    final badges = metricBadges(_sensorStatus);
    final chips = [
      (Icons.thermostat, _reading('temperature'), '°C', badges['temperature']),
      (Icons.water_drop_outlined, _reading('humidity'), '%', badges['humidity']),
      (Icons.grass, _reading('soilMoisture'), '%', badges['soilMoisture']),
      (Icons.umbrella_outlined, _reading('rainfall'), '%', badges['rainfall']),
    ];
    final notice = sensorNotice(_sensorStatus);
    final (statusText, tone) = _ip == null
        ? ('Robot IP unknown', PillTone.bad)
        : !_reachable
            ? ('Connecting to $_ip…', PillTone.warn)
            : _driveBlocked != null
                ? ('Wheels locked', PillTone.warn)
                : _driveSource == 'gamepad'
                    ? ('Controller driving', PillTone.good)
                    : ('Connected', PillTone.good);
    final width = MediaQuery.sizeOf(context).width;
    final stickSize = math.min(168.0, width * 0.42);

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
                  for (final (icon, value, unit, badge) in chips) ...[
                    Expanded(
                      child: _Glass(
                        radius: 16,
                        child: Padding(
                          padding: const EdgeInsets.symmetric(vertical: 8, horizontal: 6),
                          child: Row(mainAxisAlignment: MainAxisAlignment.center, children: [
                            Icon(icon, size: 14, color: badge == null ? AgriColors.mint : AgriColors.warning),
                            const SizedBox(width: 4),
                            Flexible(
                              child: Text(
                                value == null ? '—' : '${value.toStringAsFixed(0)}$unit${badge == null ? '' : '*'}',
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
                if (notice != null) ...[
                  const SizedBox(height: 8),
                  _GlassNote(icon: Icons.info_outline, text: '* $notice'),
                ],
                if (_driveBlocked != null) ...[
                  const SizedBox(height: 8),
                  _GlassNote(icon: Icons.lock_outline, text: _driveBlocked!),
                ],
                if (_gamepad != null) ...[
                  const SizedBox(height: 8),
                  _GlassNote(icon: Icons.sports_esports, text: _gamepad!),
                ],
                const Spacer(),
                if (_pi != null) ...[
                  _armPanel(),
                  const SizedBox(height: 12),
                ],
                Row(crossAxisAlignment: CrossAxisAlignment.end, children: [
                  Joystick(
                    size: stickSize,
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
                      label: 'Stop wheels and arm',
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

  /// Frosted arm bar: status line, then Up / Down (hold), Retract and Arm Raised.
  Widget _armPanel() {
    final state = _armState;
    return _Glass(
      radius: 22,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(12, 8, 6, 10),
        child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
          Row(children: [
            const Icon(Icons.precision_manufacturing_outlined, size: 18, color: AgriColors.mint),
            const SizedBox(width: 8),
            const Text('Arm', style: TextStyle(color: Colors.white, fontSize: 15, fontWeight: FontWeight.w800)),
            const SizedBox(width: 10),
            Expanded(child: Align(alignment: Alignment.centerLeft, child: StatusPill(state.message, tone: state.tone, dark: true))),
            IconButton(
              tooltip: _armToken.isEmpty ? 'Add arm token' : 'Change arm token',
              visualDensity: VisualDensity.compact,
              icon: Icon(Icons.key_outlined, size: 20, color: state.needsToken ? AgriColors.warning : AgriColors.textOnDarkMuted),
              onPressed: _editArmToken,
            ),
          ]),
          if (_arm != null) ...[
            const SizedBox(height: 6),
            Row(children: [
              Expanded(child: _holdButton('up', 'Up', Icons.keyboard_arrow_up, state.canNudge)),
              const SizedBox(width: 6),
              Expanded(child: _holdButton('down', 'Down', Icons.keyboard_arrow_down, state.canNudge)),
              const SizedBox(width: 6),
              if (state.canRetract) ...[
                Expanded(child: _actionButton('Retract', Icons.u_turn_left, _retract, enabled: true)),
                const SizedBox(width: 6),
              ],
              Expanded(child: _actionButton('Arm raised', Icons.verified_outlined, _confirmRaised, enabled: state.canConfirmRaised)),
              const SizedBox(width: 6),
            ]),
          ],
        ]),
      ),
    );
  }

  Widget _holdButton(String direction, String label, IconData icon, bool enabled) {
    final active = _armHeld == direction;
    final fg = !enabled ? Colors.white38 : active ? AgriColors.forest : Colors.white;
    return Semantics(
      button: true,
      enabled: enabled,
      label: 'Arm $label, hold to move',
      child: Listener(
        onPointerDown: enabled ? (_) => _armHold(direction) : null,
        onPointerUp: (_) => _armRelease(),
        onPointerCancel: (_) => _armRelease(),
        child: AnimatedContainer(
          duration: const Duration(milliseconds: 120),
          height: 48,
          decoration: BoxDecoration(
            color: active ? AgriColors.mint : Colors.white.withValues(alpha: enabled ? 0.12 : 0.05),
            borderRadius: BorderRadius.circular(16),
          ),
          child: Row(mainAxisAlignment: MainAxisAlignment.center, children: [
            Icon(icon, size: 22, color: fg),
            Text(label, style: TextStyle(color: fg, fontSize: 13, fontWeight: FontWeight.w800)),
          ]),
        ),
      ),
    );
  }

  Widget _actionButton(String label, IconData icon, VoidCallback onTap, {required bool enabled}) {
    final fg = enabled ? AgriColors.mint : Colors.white38;
    return Semantics(
      button: true,
      enabled: enabled,
      label: label,
      child: GestureDetector(
        onTap: enabled ? onTap : null,
        child: Container(
          height: 48,
          padding: const EdgeInsets.symmetric(horizontal: 4),
          decoration: BoxDecoration(border: Border.all(color: enabled ? AgriColors.mint : Colors.white24), borderRadius: BorderRadius.circular(16)),
          child: Row(mainAxisAlignment: MainAxisAlignment.center, children: [
            Icon(icon, size: 16, color: fg),
            const SizedBox(width: 4),
            Flexible(child: Text(label, maxLines: 1, overflow: TextOverflow.ellipsis, style: TextStyle(color: fg, fontSize: 12, fontWeight: FontWeight.w800))),
          ]),
        ),
      ),
    );
  }
}

/// One-line frosted note under the sensor chips (demo data, wheel lock, controller).
class _GlassNote extends StatelessWidget {
  const _GlassNote({required this.icon, required this.text});
  final IconData icon;
  final String text;

  @override
  Widget build(BuildContext context) => Align(
        alignment: Alignment.centerLeft,
        child: _Glass(
          radius: 16,
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
            child: Row(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
              Padding(padding: const EdgeInsets.only(top: 1), child: Icon(icon, size: 14, color: AgriColors.mint)),
              const SizedBox(width: 6),
              Flexible(child: Text(text, style: const TextStyle(color: Colors.white, fontSize: 11, height: 1.35, fontWeight: FontWeight.w600))),
            ]),
          ),
        ),
      );
}

/// Sheet header used by the arm sheets (same pattern as share_network_sheet.dart).
class _SheetHeader extends StatelessWidget {
  const _SheetHeader({required this.title, required this.subtitle});
  final String title, subtitle;

  @override
  Widget build(BuildContext context) => Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        Center(child: Container(width: 44, height: 5, decoration: BoxDecoration(color: AgriColors.border, borderRadius: BorderRadius.circular(3)))),
        const SizedBox(height: 14),
        Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Expanded(
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text(title, style: const TextStyle(fontSize: 24, fontWeight: FontWeight.w800)),
              const SizedBox(height: 4),
              Text(subtitle, style: AgriText.body),
            ]),
          ),
          IconButton.filledTonal(onPressed: () => Navigator.pop(context), icon: const Icon(Icons.close), tooltip: 'Close'),
        ]),
      ]);
}

/// Enter the token that matches AGRIBOT_LOCAL_TOKEN on the robot. Stored in the
/// iOS Keychain / Android Keystore via flutter_secure_storage.
class _ArmTokenSheet extends StatefulWidget {
  const _ArmTokenSheet({required this.initial});
  final String initial;
  @override
  State<_ArmTokenSheet> createState() => _ArmTokenSheetState();
}

class _ArmTokenSheetState extends State<_ArmTokenSheet> {
  late final _token = TextEditingController(text: widget.initial);
  bool _show = false;

  @override
  void dispose() {
    _token.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => Padding(
        padding: EdgeInsets.only(bottom: MediaQuery.viewInsetsOf(context).bottom),
        child: SafeArea(
          top: false,
          child: Padding(
            padding: const EdgeInsets.fromLTRB(20, 10, 20, 20),
            child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.stretch, children: [
              const _SheetHeader(
                title: 'Arm control token',
                subtitle: 'Needed for Arm Up/Down, Retract and Arm Raised. Use the AGRIBOT_LOCAL_TOKEN set on the robot. It is stored securely on this phone.',
              ),
              const SizedBox(height: 8),
              AgriTextField(
                label: 'Token',
                controller: _token,
                obscure: !_show,
                hint: 'From /etc/agribot.env on the Pi',
                suffix: IconButton(
                  tooltip: _show ? 'Hide token' : 'Show token',
                  icon: Icon(_show ? Icons.visibility_off : Icons.visibility, color: AgriColors.textMuted),
                  onPressed: () => setState(() => _show = !_show),
                ),
              ),
              const SizedBox(height: 12),
              PrimaryButton(label: 'Save token', icon: Icons.key, onPressed: () => Navigator.pop(context, _token.text.trim())),
              if (widget.initial.isNotEmpty) ...[
                const SizedBox(height: 4),
                TextButton(
                  onPressed: () => Navigator.pop(context, ''),
                  child: const Text('Remove token', style: TextStyle(fontWeight: FontWeight.w800, color: AgriColors.error)),
                ),
              ],
            ]),
          ),
        ),
      );
}

/// Operator check-list before an arm action. The action button stays disabled
/// until every item is ticked.
class _ArmConfirmSheet extends StatefulWidget {
  const _ArmConfirmSheet({required this.title, required this.body, required this.checks, required this.footnote, required this.action});
  final String title, body, footnote, action;
  final List<String> checks;
  @override
  State<_ArmConfirmSheet> createState() => _ArmConfirmSheetState();
}

class _ArmConfirmSheetState extends State<_ArmConfirmSheet> {
  late final List<bool> _ticked = List<bool>.filled(widget.checks.length, false);

  @override
  Widget build(BuildContext context) => SafeArea(
        top: false,
        child: Padding(
          padding: const EdgeInsets.fromLTRB(20, 10, 20, 20),
          child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.stretch, children: [
            _SheetHeader(title: widget.title, subtitle: widget.body),
            const SizedBox(height: 6),
            for (var i = 0; i < widget.checks.length; i++)
              CheckboxListTile(
                contentPadding: EdgeInsets.zero,
                controlAffinity: ListTileControlAffinity.leading,
                activeColor: AgriColors.primary,
                value: _ticked[i],
                onChanged: (v) => setState(() => _ticked[i] = v ?? false),
                title: Text(widget.checks[i], style: const TextStyle(fontSize: 14, fontWeight: FontWeight.w700)),
              ),
            Container(
              margin: const EdgeInsets.only(top: 6, bottom: 14),
              padding: const EdgeInsets.all(12),
              decoration: BoxDecoration(color: const Color(0xFFFBF1DD), borderRadius: BorderRadius.circular(18)),
              child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                const Icon(Icons.info, color: AgriColors.warning, size: 20),
                const SizedBox(width: 10),
                Expanded(child: Text(widget.footnote, style: const TextStyle(fontSize: 12, height: 1.4))),
              ]),
            ),
            PrimaryButton(label: widget.action, onPressed: _ticked.every((t) => t) ? () => Navigator.pop(context, true) : null),
          ]),
        ),
      );
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
