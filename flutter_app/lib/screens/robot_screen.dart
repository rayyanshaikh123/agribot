import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../core/format.dart';
import '../core/theme.dart';
import '../state/auth_controller.dart';
import '../state/ble_controller.dart';
import '../state/robot_controller.dart';
import '../widgets/common.dart';
import 'drive_screen.dart';
import 'home_shell.dart';
import 'map_screen.dart';
import 'share_network_sheet.dart';

class RobotScreen extends StatelessWidget {
  const RobotScreen({super.key});

  (String, PillTone) _wifiSummary(PiBleStatus? s) {
    if (s == null) return ('Reading status…', PillTone.muted);
    return switch (s.wifiState) {
      'connected' => ('Wi-Fi: ${s.ssid}${s.ip != null ? ' · ${s.ip}' : ''}', PillTone.good),
      'connecting' => ('Joining ${s.target ?? 'network'}…', PillTone.warn),
      'failed' => (s.wifiError ?? 'Wi-Fi connection failed', PillTone.bad),
      'unavailable' => ('Wi-Fi control unavailable on the Pi', PillTone.bad),
      _ => ('Not connected to Wi-Fi', PillTone.warn),
    };
  }

  @override
  Widget build(BuildContext context) {
    final robot = context.watch<RobotController>();
    final ble = context.watch<BleController>();
    final auth = context.watch<AuthController>();
    final p = robot.presence;
    final (wifiText, wifiTone) = _wifiSummary(ble.status);
    final publish = robot.commands['publish']!;

    final caps = [
      (Icons.photo_camera, p?.camera != null ? 'Camera (${p!.camera})' : 'No camera', p?.camera != null),
      (Icons.auto_awesome, (p?.model ?? false) ? 'AI model loaded' : 'Model missing', p?.model ?? false),
      (Icons.speed, p?.sensors != null ? 'Sensors: ${p!.sensors}' : 'Sensors unknown', p?.sensors == 'hardware'),
      (Icons.bluetooth, (p?.ble ?? false) ? 'Bluetooth on' : 'Bluetooth off', p?.ble ?? false),
    ];

    return SafeArea(
      bottom: false,
      child: ListView(padding: const EdgeInsets.fromLTRB(16, 16, 16, 130), children: [
        const Text('Your robot', style: AgriText.eyebrow),
        const Text('AgriBot', style: AgriText.title),
        const SizedBox(height: 18),

        // Live status hero
        Container(
          decoration: BoxDecoration(color: AgriColors.forest, borderRadius: BorderRadius.circular(34)),
          clipBehavior: Clip.antiAlias,
          child: Hatch(
            color: const Color(0x1AFFFFFF),
            child: Padding(
              padding: const EdgeInsets.all(20),
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Row(children: [
                  const IconCircle(Icons.memory, size: 52, background: AgriColors.mint, color: AgriColors.forest),
                  const SizedBox(width: 14),
                  Expanded(
                    child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                      Text(robot.online ? 'Online' : 'Offline', style: const TextStyle(color: AgriColors.textOnDark, fontSize: 24, fontWeight: FontWeight.w800)),
                      Text(
                        p?.lastSeenAt != null
                            ? 'Last seen ${timeAgo(p!.lastSeenAt)}'
                            : robot.dashboard != null
                                ? 'Last reading ${timeAgo(robot.dashboard!.lastUpdated)}'
                                : 'Waiting for the robot',
                        style: const TextStyle(color: AgriColors.textOnDarkMuted, fontSize: 13),
                      ),
                    ]),
                  ),
                  StatusPill(robot.online ? 'Live' : 'Idle', tone: robot.online ? PillTone.good : PillTone.warn, dark: true),
                ]),
                const SizedBox(height: 18),
                Container(
                  padding: const EdgeInsets.all(14),
                  decoration: BoxDecoration(color: Colors.white.withValues(alpha: 0.08), borderRadius: BorderRadius.circular(18)),
                  child: Row(children: [
                    for (final (label, value) in [('Network', p?.ssid), ('IP address', p?.ip), ('Version', p?.version)])
                      Expanded(
                        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                          Text(label, style: const TextStyle(color: AgriColors.textOnDarkMuted, fontSize: 11, fontWeight: FontWeight.w600)),
                          const SizedBox(height: 3),
                          Text(value ?? '—', maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(color: AgriColors.textOnDark, fontSize: 14, fontWeight: FontWeight.w800)),
                        ]),
                      ),
                  ]),
                ),
                const SizedBox(height: 14),
                Wrap(spacing: 8, runSpacing: 8, children: [
                  for (final (icon, label, ok) in caps)
                    Container(
                      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
                      decoration: BoxDecoration(color: ok ? AgriColors.mint : Colors.white.withValues(alpha: 0.1), borderRadius: BorderRadius.circular(999)),
                      child: Row(mainAxisSize: MainAxisSize.min, children: [
                        Icon(icon, size: 13, color: ok ? AgriColors.forest : AgriColors.textOnDarkMuted),
                        const SizedBox(width: 5),
                        Text(label, style: TextStyle(fontSize: 11, fontWeight: FontWeight.w700, color: ok ? AgriColors.forest : AgriColors.textOnDarkMuted)),
                      ]),
                    ),
                ]),
                const SizedBox(height: 16),
                PrimaryButton(light: true, icon: Icons.sports_esports, label: 'Drive AgriBot', onPressed: () => DriveScreen.open(context)),
              ]),
            ),
          ),
        ),

        // Bluetooth + share network
        const SizedBox(height: 24),
        const Text('Network setup', style: AgriText.section),
        const SizedBox(height: 10),
        AgriCard(
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            _Step(index: 1, done: ble.phase == BlePhase.connected, title: 'Connect over Bluetooth', detail: 'Stand near the robot. Bluetooth works even when AgriBot has no Wi-Fi.'),
            const SizedBox(height: 16),
            if (ble.phase == BlePhase.connected && ble.device != null)
              Container(
                padding: const EdgeInsets.all(12),
                decoration: BoxDecoration(color: AgriColors.surfaceMuted, borderRadius: BorderRadius.circular(18)),
                child: Row(children: [
                  const IconCircle(Icons.bluetooth_connected, size: 40, background: AgriColors.mint, color: AgriColors.forest),
                  const SizedBox(width: 12),
                  Expanded(
                    child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                      Text(ble.device!.name, style: const TextStyle(fontSize: 15, fontWeight: FontWeight.w800)),
                      const SizedBox(height: 4),
                      StatusPill(wifiText, tone: wifiTone),
                    ]),
                  ),
                  TextButton(onPressed: ble.disconnect, child: const Text('Disconnect', style: TextStyle(fontWeight: FontWeight.w800, color: AgriColors.primary))),
                ]),
              )
            else ...[
              PrimaryButton(
                light: true,
                icon: Icons.bluetooth_searching,
                label: ble.phase == BlePhase.scanning ? 'Searching…' : 'Find AgriBot',
                loading: ble.phase == BlePhase.scanning,
                onPressed: ble.phase == BlePhase.connecting ? null : ble.scan,
              ),
              for (final r in ble.robots)
                Padding(
                  padding: const EdgeInsets.only(top: 10),
                  child: Material(
                    color: Colors.transparent,
                    shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(18), side: const BorderSide(color: AgriColors.border)),
                    child: ListTile(
                      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(18)),
                      leading: const Icon(Icons.sensors, color: AgriColors.forest),
                      title: Text(r.name, style: const TextStyle(fontWeight: FontWeight.w800)),
                      subtitle: Text('Signal ${r.rssi} dBm'),
                      trailing: ble.phase == BlePhase.connecting && ble.device?.id == r.id
                          ? const SizedBox(width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2))
                          : const Text('Connect', style: TextStyle(fontWeight: FontWeight.w800, color: AgriColors.primary)),
                      onTap: ble.phase == BlePhase.connecting ? null : () => ble.connect(r),
                    ),
                  ),
                ),
            ],
            const Divider(height: 40, color: AgriColors.border),
            _Step(index: 2, done: ble.status?.wifiState == 'connected', title: 'Share a network', detail: 'Send your Wi-Fi or phone hotspot to the Pi so it can reach the server.'),
            const SizedBox(height: 16),
            PrimaryButton(
              icon: Icons.share,
              label: 'Share network',
              onPressed: ble.phase == BlePhase.connected ? () => showShareNetworkSheet(context) : null,
            ),
            if (ble.error.isNotEmpty) Padding(padding: const EdgeInsets.only(top: 12), child: Text(ble.error, style: const TextStyle(color: AgriColors.error, fontSize: 13, height: 1.4))),
          ]),
        ),

        // Actions
        const SizedBox(height: 24),
        const Text('Actions', style: AgriText.section),
        const SizedBox(height: 10),
        Row(children: [
          Expanded(
            child: _ActionTile(
              icon: Icons.refresh,
              title: 'Refresh',
              detail: switch (publish.phase) {
                CommandPhase.failed => publish.message ?? 'Failed',
                CommandPhase.done => 'Updated',
                _ => 'Read sensors now',
              },
              loading: publish.phase == CommandPhase.pending,
              onTap: robot.online && publish.phase != CommandPhase.pending ? () => robot.runCommand('publish') : null,
            ),
          ),
          const SizedBox(width: 10),
          Expanded(child: _ActionTile(icon: Icons.map, title: 'Field map', detail: 'Live position', onTap: () => Navigator.of(context).push(MaterialPageRoute<void>(builder: (_) => const MapScreen())))),
          const SizedBox(width: 10),
          Expanded(child: _ActionTile(icon: Icons.center_focus_strong, title: 'Leaf scan', detail: 'Run the AI model', onTap: () => HomeShell.switchTab(context, 1))),
        ]),

        // Account
        const SizedBox(height: 24),
        const Text('Account', style: AgriText.section),
        const SizedBox(height: 10),
        AgriCard(
          child: Row(children: [
            const IconCircle(Icons.person, size: 44, background: AgriColors.primarySoft, color: AgriColors.forest),
            const SizedBox(width: 12),
            Expanded(
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text(auth.user?.name ?? 'Farmer', style: const TextStyle(fontSize: 15, fontWeight: FontWeight.w800)),
                Text(auth.user?.email ?? '', style: AgriText.small),
                Text(auth.serverUrl, style: AgriText.small, maxLines: 1, overflow: TextOverflow.ellipsis),
              ]),
            ),
            TextButton(onPressed: auth.logout, child: const Text('Log out', style: TextStyle(color: AgriColors.error, fontWeight: FontWeight.w800))),
          ]),
        ),
      ]),
    );
  }
}

class _Step extends StatelessWidget {
  const _Step({required this.index, required this.done, required this.title, required this.detail});
  final int index;
  final bool done;
  final String title, detail;
  @override
  Widget build(BuildContext context) => Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Container(
          width: 30,
          height: 30,
          alignment: Alignment.center,
          decoration: BoxDecoration(color: done ? AgriColors.success : AgriColors.primarySoft, shape: BoxShape.circle),
          child: done
              ? const Icon(Icons.check, size: 16, color: AgriColors.textOnDark)
              : Text('$index', style: const TextStyle(color: AgriColors.forest, fontWeight: FontWeight.w800)),
        ),
        const SizedBox(width: 12),
        Expanded(
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text(title, style: const TextStyle(fontSize: 16, fontWeight: FontWeight.w800)),
            const SizedBox(height: 2),
            Text(detail, style: AgriText.body),
          ]),
        ),
      ]);
}

class _ActionTile extends StatelessWidget {
  const _ActionTile({required this.icon, required this.title, required this.detail, this.onTap, this.loading = false});
  final IconData icon;
  final String title, detail;
  final VoidCallback? onTap;
  final bool loading;
  @override
  Widget build(BuildContext context) => Opacity(
        opacity: onTap == null && !loading ? 0.55 : 1,
        child: AgriCard(
          padding: const EdgeInsets.all(14),
          onTap: onTap,
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            loading
                ? Container(
                    width: 40,
                    height: 40,
                    padding: const EdgeInsets.all(10),
                    decoration: const BoxDecoration(color: AgriColors.forest, shape: BoxShape.circle),
                    child: const CircularProgressIndicator(strokeWidth: 2, color: AgriColors.textOnDark),
                  )
                : IconCircle(icon),
            const SizedBox(height: 10),
            Text(title, style: const TextStyle(fontSize: 15, fontWeight: FontWeight.w800)),
            const SizedBox(height: 2),
            Text(detail, maxLines: 2, overflow: TextOverflow.ellipsis, style: AgriText.small),
          ]),
        ),
      );
}
