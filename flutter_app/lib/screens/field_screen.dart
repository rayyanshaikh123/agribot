import 'dart:async';

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../core/crop_advice.dart';
import '../core/format.dart';
import '../core/sensor_labels.dart';
import '../core/theme.dart';
import '../models/robot.dart';
import '../state/auth_controller.dart';
import '../state/robot_controller.dart';
import '../widgets/common.dart';
import '../widgets/field_map.dart';
import 'home_shell.dart';
import 'map_screen.dart';
import 'scan_image.dart';

enum _Section { overview, analysis, trends }

class FieldScreen extends StatefulWidget {
  const FieldScreen({super.key});
  @override
  State<FieldScreen> createState() => _FieldScreenState();
}

class _FieldScreenState extends State<FieldScreen> {
  _Section _section = _Section.overview;

  @override
  Widget build(BuildContext context) {
    final robot = context.watch<RobotController>();
    final firstName = (context.select<AuthController, String?>((a) => a.user?.name) ?? 'Farmer').split(' ').first;
    if (robot.loading) return const Center(child: CircularProgressIndicator());

    final data = robot.dashboard ?? DashboardData.sample();
    final live = robot.dashboard != null;
    final s = data.sensors;
    // Current sensor status reported by the Pi: label anything that is not a measurement.
    final sensorStatus = robot.presence?.sensorStatus ?? const <String, String>{};
    final badges = live ? metricBadges(sensorStatus) : const <String, String?>{};
    final notice = live ? sensorNotice(sensorStatus) : null;
    final top = MediaQuery.paddingOf(context).top;
    final socketText = switch (robot.socketState) {
      SocketState.live => 'Live field data',
      SocketState.connecting => 'Connecting…',
      SocketState.offline => 'Live updates paused',
    };

    return RefreshIndicator(
      color: AgriColors.primary,
      onRefresh: robot.refresh,
      child: ListView(padding: const EdgeInsets.only(bottom: 130), children: [
        // Map hero
        ClipRRect(
          borderRadius: const BorderRadius.vertical(bottom: Radius.circular(40)),
          child: SizedBox(
            height: 380,
            child: Stack(children: [
              Positioned.fill(child: FieldMap(latitude: data.latitude, longitude: data.longitude, online: robot.online, interactive: false)),
              const Positioned.fill(child: IgnorePointer(child: Hatch(color: Color(0x14153F2C)))),
              Positioned(
                top: top + 8,
                left: 18,
                right: 18,
                child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Container(
                    padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
                    decoration: BoxDecoration(color: Colors.white.withValues(alpha: 0.94), borderRadius: BorderRadius.circular(24), boxShadow: kSoftShadow),
                    child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                      Text('Hello, $firstName', style: const TextStyle(fontSize: 16, fontWeight: FontWeight.w800)),
                      Text(socketText, style: const TextStyle(fontSize: 12, color: AgriColors.textMuted, fontWeight: FontWeight.w600)),
                    ]),
                  ),
                  const Spacer(),
                  RoundButton(icon: Icons.open_in_full, tooltip: 'Open full map', onPressed: () => Navigator.of(context).push(MaterialPageRoute<void>(builder: (_) => const MapScreen()))),
                ]),
              ),
              Positioned(
                left: 18,
                right: 18,
                bottom: 22,
                child: Container(
                  padding: const EdgeInsets.fromLTRB(10, 10, 14, 10),
                  decoration: BoxDecoration(color: Colors.white.withValues(alpha: 0.95), borderRadius: BorderRadius.circular(26), boxShadow: kSoftShadow),
                  child: Row(children: [
                    const IconCircle(Icons.eco, size: 38, background: AgriColors.mint, color: AgriColors.forest),
                    const SizedBox(width: 12),
                    Expanded(
                      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                        const Text('AgriBot field', style: AgriText.cardTitle),
                        Text('${data.latitude.toStringAsFixed(4)}, ${data.longitude.toStringAsFixed(4)}', style: AgriText.small),
                      ]),
                    ),
                    StatusPill(robot.online ? 'Online' : 'Offline', tone: robot.online ? PillTone.good : PillTone.warn),
                  ]),
                ),
              ),
            ]),
          ),
        ),
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 20, 16, 0),
          child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
            PillTabs<_Section>(
              options: const {_Section.overview: 'Overview', _Section.analysis: 'Analysis', _Section.trends: 'Trends'},
              value: _section,
              onChanged: (v) => setState(() => _section = v),
            ),
            if (!live)
              Container(
                margin: const EdgeInsets.only(top: 14),
                padding: const EdgeInsets.all(12),
                decoration: BoxDecoration(color: const Color(0xFFFBF1DD), borderRadius: BorderRadius.circular(18)),
                child: Row(children: [
                  const Icon(Icons.info, color: AgriColors.warning, size: 20),
                  const SizedBox(width: 10),
                  Expanded(child: Text('Showing sample values. ${robot.dashboardError.isEmpty ? 'Waiting for the first reading from AgriBot.' : robot.dashboardError}', style: const TextStyle(fontSize: 12, height: 1.4))),
                ]),
              ),
            if (notice != null)
              Container(
                margin: const EdgeInsets.only(top: 14),
                padding: const EdgeInsets.all(12),
                decoration: BoxDecoration(color: const Color(0xFFFBF1DD), borderRadius: BorderRadius.circular(18)),
                child: Row(children: [
                  const Icon(Icons.info, color: AgriColors.warning, size: 20),
                  const SizedBox(width: 10),
                  Expanded(child: Text(notice, style: const TextStyle(fontSize: 12, height: 1.4))),
                ]),
              ),
            ...switch (_section) {
              _Section.overview => [
                  const SizedBox(height: 16),
                  SizedBox(
                    height: 150,
                    child: ListView(
                      scrollDirection: Axis.horizontal,
                      clipBehavior: Clip.none,
                      children: [
                        MetricChip(icon: Icons.water_drop, label: 'Moisture', value: round1(s.soilMoisture), unit: '%', badge: badges['soilMoisture']),
                        const SizedBox(width: 10),
                        MetricChip(icon: Icons.thermostat, label: 'Temperature', value: round1(s.temperature), unit: '°C', badge: badges['temperature']),
                        const SizedBox(width: 10),
                        MetricChip(icon: Icons.cloud, label: 'Humidity', value: round1(s.humidity), unit: '%', badge: badges['humidity']),
                        const SizedBox(width: 10),
                        MetricChip(icon: Icons.science, label: 'Acidity', value: s.ph == null ? '—' : 'pH ${s.ph!.toStringAsFixed(1)}', badge: badges['ph']),
                        const SizedBox(width: 10),
                        MetricChip(icon: Icons.umbrella, label: 'Rain', value: s.rainfall >= 50 ? 'Wet' : 'Dry', badge: badges['rainfall']),
                      ],
                    ),
                  ),
                  const SizedBox(height: 8),
                  _CropCard(scan: robot.latestScan),
                  const SizedBox(height: 14),
                  const TrendCard(),
                ],
              _Section.analysis => [_Analysis(insights: buildInsights(live ? s : null, robot.latestScan), updated: robot.dashboard?.lastUpdated)],
              _Section.trends => [const SizedBox(height: 16), const TrendCard(selectable: true)],
            },
          ]),
        ),
      ]),
    );
  }
}

class _CropCard extends StatelessWidget {
  const _CropCard({required this.scan});
  final CropScan? scan;

  @override
  Widget build(BuildContext context) {
    final scan = this.scan;
    return AgriCard(
      padding: const EdgeInsets.all(10),
      radius: 34,
      onTap: () => HomeShell.switchTab(context, 1),
      child: Row(children: [
        ClipRRect(
          borderRadius: BorderRadius.circular(26),
          child: SizedBox(width: 116, height: 128, child: ScanImage(scan: scan, iconSize: 34)),
        ),
        const SizedBox(width: 14),
        Expanded(
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            const Text('Crop health', style: TextStyle(fontSize: 13, fontWeight: FontWeight.w700, color: AgriColors.textMuted)),
            const SizedBox(height: 4),
            if (scan == null)
              const Text('No camera scan yet. Tap to run the leaf disease model.', style: AgriText.small)
            else ...[
              Row(children: [
                Container(width: 10, height: 10, decoration: BoxDecoration(color: diseaseColor(scan.label), shape: BoxShape.circle)),
                const SizedBox(width: 8),
                Flexible(child: Text(scan.status == 'none' ? 'No leaves found' : scan.label, maxLines: 2, style: const TextStyle(fontSize: 19, fontWeight: FontWeight.w800))),
              ]),
              const SizedBox(height: 4),
              Text('Scanned ${timeAgo(scan.createdAt)}', style: AgriText.small),
              if (scan.confidence > 0) Text('${percent(scan.confidence)} confidence', style: AgriText.small),
            ],
          ]),
        ),
        Container(
          width: 56,
          height: 56,
          margin: const EdgeInsets.only(right: 6),
          decoration: const BoxDecoration(color: AgriColors.forest, shape: BoxShape.circle),
          child: const Icon(Icons.north_east, color: AgriColors.textOnDark),
        ),
      ]),
    );
  }
}

class _Analysis extends StatelessWidget {
  const _Analysis({required this.insights, required this.updated});
  final List<Insight> insights;
  final DateTime? updated;

  static const _tones = {
    InsightTone.good: (AgriColors.mint, AgriColors.forest),
    InsightTone.watch: (Color(0xFFF8EBCF), AgriColors.warning),
    InsightTone.alert: (Color(0xFFF9DEDB), AgriColors.error),
  };

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.only(top: 16),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text('Recommendations from the latest readings${updated != null ? ' (${formatTime(updated)})' : ''} and camera scan.', style: AgriText.body),
          if (insights.isEmpty) const Padding(padding: EdgeInsets.only(top: 8), child: Text('No readings available yet.', style: AgriText.body)),
          for (final i in insights)
            Padding(
              padding: const EdgeInsets.only(top: 10),
              child: AgriCard(
                padding: const EdgeInsets.all(14),
                child: Row(children: [
                  IconCircle(i.icon, size: 44, background: _tones[i.tone]!.$1, color: _tones[i.tone]!.$2),
                  const SizedBox(width: 14),
                  Expanded(
                    child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                      Text(i.title, style: const TextStyle(fontSize: 15, fontWeight: FontWeight.w800)),
                      const SizedBox(height: 2),
                      Text(i.detail, style: AgriText.body),
                    ]),
                  ),
                ]),
              ),
            ),
        ]),
      );
}

const _metrics = {
  'soilMoisture': ('Soil moisture', '%', Icons.water_drop),
  'temperature': ('Temperature', '°C', Icons.thermostat),
  'humidity': ('Humidity', '%', Icons.cloud),
};

/// History chart; refetches when the range changes or new readings arrive (at most every 20 s).
class TrendCard extends StatefulWidget {
  const TrendCard({super.key, this.selectable = false});
  final bool selectable;
  @override
  State<TrendCard> createState() => _TrendCardState();
}

class _TrendCardState extends State<TrendCard> {
  HistoryRange _range = HistoryRange.day;
  String _metric = 'soilMoisture';
  List<HistoryPoint> _points = [];
  bool _loading = true;
  DateTime? _lastFetch;
  DateTime? _seenUpdate;

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    final updated = context.watch<RobotController>().dashboard?.lastUpdated;
    if (_lastFetch == null || (updated != _seenUpdate && DateTime.now().difference(_lastFetch!).inSeconds >= 20)) {
      _seenUpdate = updated;
      _load();
    }
  }

  Future<void> _load({bool showSpinner = false}) async {
    final range = _range;
    _lastFetch = DateTime.now();
    if (showSpinner) setState(() => _loading = true);
    List<HistoryPoint> points = [];
    try {
      points = await context.read<RobotController>().history(range);
    } catch (_) {}
    if (!mounted || range != _range) return; // user switched range meanwhile
    setState(() {
      _points = points;
      _loading = false;
    });
  }

  String _axis(HistoryPoint? p) => p == null ? '' : (_range == HistoryRange.week ? formatDay(p.at) : formatTime(p.at));

  @override
  Widget build(BuildContext context) {
    final (label, unit, _) = _metrics[_metric]!;
    final values = _points.map((p) => p.values[_metric]).toList();
    final nums = values.whereType<double>().toList();
    final latest = nums.isEmpty ? null : nums.last;
    final delta = nums.length > 1 ? nums.last - nums.first : 0.0;

    return AgriCard(
      radius: 34,
      padding: const EdgeInsets.all(20),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        if (widget.selectable) ...[
          Wrap(spacing: 8, runSpacing: 8, children: [
            for (final e in _metrics.entries)
              ChoiceChip(
                selected: e.key == _metric,
                onSelected: (_) => setState(() => _metric = e.key),
                showCheckmark: false,
                avatar: Icon(e.value.$3, size: 16, color: e.key == _metric ? AgriColors.textOnDark : AgriColors.forest),
                label: Text(e.value.$1),
                labelStyle: TextStyle(fontWeight: FontWeight.w700, color: e.key == _metric ? AgriColors.textOnDark : AgriColors.forest),
                backgroundColor: AgriColors.primarySoft,
                selectedColor: AgriColors.forest,
                side: BorderSide.none,
                shape: const StadiumBorder(),
              ),
          ]),
          const SizedBox(height: 18),
        ],
        Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Expanded(
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text(label, style: const TextStyle(fontSize: 16, fontWeight: FontWeight.w700)),
              const SizedBox(height: 4),
              Row(children: [
                Text(latest == null ? '—' : '${round1(latest)} $unit', style: const TextStyle(fontSize: 22, fontWeight: FontWeight.w800)),
                if (nums.length > 1) ...[
                  const SizedBox(width: 8),
                  Icon(delta >= 0 ? Icons.arrow_upward : Icons.arrow_downward, size: 16, color: delta >= 0 ? AgriColors.success : AgriColors.warning),
                  Text(round1(delta.abs()), style: TextStyle(fontWeight: FontWeight.w700, color: delta >= 0 ? AgriColors.success : AgriColors.warning)),
                ],
              ]),
            ]),
          ),
          RoundToggle<HistoryRange>(
            options: const {HistoryRange.hour: 'H', HistoryRange.day: 'D', HistoryRange.week: 'W'},
            value: _range,
            onChanged: (r) {
              setState(() => _range = r);
              unawaited(_load(showSpinner: true));
            },
          ),
        ]),
        const SizedBox(height: 16),
        if (_loading)
          const SizedBox(height: 130, child: Center(child: CircularProgressIndicator()))
        else ...[
          TrendBars(values: values),
          const SizedBox(height: 8),
          Row(mainAxisAlignment: MainAxisAlignment.spaceBetween, children: [
            Text(_axis(_points.firstOrNull), style: AgriText.small),
            Text(_axis(_points.lastOrNull), style: AgriText.small),
          ]),
        ],
        if (widget.selectable && nums.isNotEmpty) ...[
          const Divider(height: 32, color: AgriColors.border),
          Row(children: [
            for (final (name, value) in [
              ('Min', nums.reduce((a, b) => a < b ? a : b)),
              ('Average', nums.reduce((a, b) => a + b) / nums.length),
              ('Max', nums.reduce((a, b) => a > b ? a : b)),
            ])
              Expanded(
                child: Column(children: [
                  Text(name, style: AgriText.small),
                  Text('${round1(value)}$unit', style: const TextStyle(fontSize: 16, fontWeight: FontWeight.w800)),
                ]),
              ),
          ]),
        ],
      ]),
    );
  }
}
