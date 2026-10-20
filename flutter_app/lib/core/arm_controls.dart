import '../widgets/common.dart' show PillTone;

/// What the Drive screen's arm panel may offer, derived only from what the Pi
/// reports in GET /arm (pi/agribot/arm.py `Arm.state`). The Pi and the ESP32
/// remain the authority: these flags only decide which buttons are enabled,
/// and every request is still validated (and may be refused) on the robot.
class ArmPanelState {
  const ArmPanelState({
    required this.message,
    required this.tone,
    this.canNudge = false,
    this.canConfirmRaised = false,
    this.canRetract = false,
    this.wheelsLocked = false,
    this.needsToken = false,
  });

  /// One short line explaining the current state (shown under the buttons).
  final String message;
  final PillTone tone;
  final bool canNudge, canConfirmRaised, canRetract, wheelsLocked, needsToken;

  bool get anyEnabled => canNudge || canConfirmRaised || canRetract;

  factory ArmPanelState.from(Map<String, dynamic>? arm, {required bool hasToken, String? error}) {
    if (arm == null) return const ArmPanelState(message: 'Arm not available on this robot', tone: PillTone.muted);

    final controls = (arm['controls'] as Map?) ?? const {};
    final status = (arm['status'] as String?) ?? '';
    final operation = arm['operation'] as String?;
    final appControl = controls['appControl'] == true;
    final wheelsLocked = controls['driveLocked'] == true;

    ArmPanelState off(String message, PillTone tone) =>
        ArmPanelState(message: message, tone: tone, wheelsLocked: wheelsLocked, needsToken: !hasToken && appControl);

    switch (status) {
      case 'esp32 disabled':
        return off('Arm controller is switched off on the robot', PillTone.muted);
      case 'offline':
        return off('Arm controller offline', PillTone.bad);
      case 'motion disabled':
        return off('Arm motion is disabled on the robot', PillTone.muted);
      case 'config error':
        return off('Arm settings error on the robot', PillTone.bad);
    }
    if (operation != null) return off('Arm busy: ${operation.replaceAll('_', ' ')}', PillTone.warn);
    if (!appControl) return off('App arm control is off on the robot', PillTone.muted);
    if (!hasToken) return off('Add the arm token to use these controls', PillTone.muted);

    final canNudge = controls['nudgeAllowed'] == true;
    final canConfirmRaised = wheelsLocked && controls['confirmRaisedAllowed'] == true;
    final canRetract = controls['retractAllowed'] == true;
    final (message, tone) = error != null
        ? (error, PillTone.bad)
        : wheelsLocked
            ? ('Wheels locked until the arm is confirmed raised', PillTone.warn)
            : arm['commissioned'] != true
                ? ('Manual test mode: arm not commissioned', PillTone.warn)
                : ('Hold to move, release to stop', PillTone.good);
    return ArmPanelState(
      message: message,
      tone: tone,
      canNudge: canNudge,
      canConfirmRaised: canConfirmRaised,
      canRetract: canRetract,
      wheelsLocked: wheelsLocked,
    );
  }
}
