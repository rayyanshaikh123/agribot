import 'package:flutter/material.dart';

import '../core/theme.dart';

/// Diagonal field-map hatching from the reference design.
class HatchPainter extends CustomPainter {
  HatchPainter({required this.color, this.spacing = 8, this.strokeWidth = 1});
  final Color color;
  final double spacing, strokeWidth;

  @override
  void paint(Canvas canvas, Size size) {
    final paint = Paint()
      ..color = color
      ..strokeWidth = strokeWidth;
    for (double x = -size.height; x < size.width; x += spacing) {
      canvas.drawLine(Offset(x, size.height), Offset(x + size.height, 0), paint);
    }
  }

  @override
  bool shouldRepaint(HatchPainter old) => old.color != color || old.spacing != spacing;
}

class Hatch extends StatelessWidget {
  const Hatch({super.key, this.color = const Color(0x1E153F2C), this.child});
  final Color color;
  final Widget? child;
  @override
  Widget build(BuildContext context) => CustomPaint(painter: HatchPainter(color: color), child: child);
}

class IconCircle extends StatelessWidget {
  const IconCircle(this.icon, {super.key, this.size = 40, this.color = AgriColors.textOnDark, this.background = AgriColors.forest});
  final IconData icon;
  final double size;
  final Color color, background;
  @override
  Widget build(BuildContext context) => Container(
        width: size,
        height: size,
        decoration: BoxDecoration(color: background, shape: BoxShape.circle),
        child: Icon(icon, color: color, size: size * 0.48),
      );
}

class AgriCard extends StatelessWidget {
  const AgriCard({super.key, required this.child, this.padding = const EdgeInsets.all(18), this.radius = 26, this.color = AgriColors.surface, this.onTap});
  final Widget child;
  final EdgeInsets padding;
  final double radius;
  final Color color;
  final VoidCallback? onTap;
  @override
  Widget build(BuildContext context) {
    final content = Padding(padding: padding, child: child);
    return Container(
      decoration: BoxDecoration(color: color, borderRadius: BorderRadius.circular(radius), boxShadow: kSoftShadow),
      child: Material(
        type: MaterialType.transparency,
        borderRadius: BorderRadius.circular(radius),
        clipBehavior: Clip.antiAlias,
        child: onTap == null ? content : InkWell(onTap: onTap, child: content),
      ),
    );
  }
}

class MetricChip extends StatelessWidget {
  const MetricChip({super.key, required this.icon, required this.label, required this.value, this.unit, this.badge});
  final IconData icon;
  final String label, value;
  final String? unit;

  /// Small warning label for values that are not measurements (e.g. "Simulated").
  final String? badge;
  @override
  Widget build(BuildContext context) => Container(
        width: 112,
        padding: const EdgeInsets.symmetric(vertical: 16, horizontal: 10),
        decoration: BoxDecoration(color: AgriColors.surface, borderRadius: BorderRadius.circular(26), boxShadow: kSoftShadow),
        child: Column(children: [
          IconCircle(icon, size: 40),
          const SizedBox(height: 10),
          Text(label, style: const TextStyle(fontSize: 12, fontWeight: FontWeight.w600, color: AgriColors.textMuted), maxLines: 1, overflow: TextOverflow.ellipsis),
          const SizedBox(height: 4),
          FittedBox(
            child: Text.rich(TextSpan(children: [
              TextSpan(text: value, style: const TextStyle(fontSize: 20, fontWeight: FontWeight.w800, color: AgriColors.text)),
              if (unit != null) TextSpan(text: unit, style: const TextStyle(fontSize: 12, fontWeight: FontWeight.w700, color: AgriColors.textMuted)),
            ])),
          ),
          if (badge != null) ...[
            const SizedBox(height: 4),
            Text(badge!, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontSize: 10, fontWeight: FontWeight.w800, color: AgriColors.warning)),
          ],
        ]),
      );
}

/// Wide pill tab bar (Overview / Analysis / Trends).
class PillTabs<T> extends StatelessWidget {
  const PillTabs({super.key, required this.options, required this.value, required this.onChanged});
  final Map<T, String> options;
  final T value;
  final ValueChanged<T> onChanged;
  @override
  Widget build(BuildContext context) => Container(
        padding: const EdgeInsets.all(5),
        decoration: BoxDecoration(color: AgriColors.surface, borderRadius: BorderRadius.circular(999), boxShadow: kSoftShadow),
        child: Row(
          children: options.entries.map((e) {
            final active = e.key == value;
            return Expanded(
              child: GestureDetector(
                onTap: () => onChanged(e.key),
                child: AnimatedContainer(
                  duration: const Duration(milliseconds: 200),
                  padding: const EdgeInsets.symmetric(vertical: 12),
                  decoration: BoxDecoration(color: active ? AgriColors.forest : Colors.transparent, borderRadius: BorderRadius.circular(999)),
                  alignment: Alignment.center,
                  child: Text(e.value, style: TextStyle(fontSize: 14, fontWeight: FontWeight.w700, color: active ? AgriColors.textOnDark : AgriColors.primary)),
                ),
              ),
            );
          }).toList(),
        ),
      );
}

/// Small round toggles, like the W / M / Y control in the reference.
class RoundToggle<T> extends StatelessWidget {
  const RoundToggle({super.key, required this.options, required this.value, required this.onChanged});
  final Map<T, String> options;
  final T value;
  final ValueChanged<T> onChanged;
  @override
  Widget build(BuildContext context) => Row(
        mainAxisSize: MainAxisSize.min,
        children: options.entries.map((e) {
          final active = e.key == value;
          return Padding(
            padding: const EdgeInsets.only(left: 6),
            child: GestureDetector(
              onTap: () => onChanged(e.key),
              child: AnimatedContainer(
                duration: const Duration(milliseconds: 200),
                width: 34,
                height: 34,
                alignment: Alignment.center,
                decoration: BoxDecoration(color: active ? AgriColors.forestDeep : const Color(0xFFE6EAE4), shape: BoxShape.circle),
                child: Text(e.value, style: TextStyle(fontSize: 13, fontWeight: FontWeight.w700, color: active ? AgriColors.textOnDark : AgriColors.textMuted)),
              ),
            ),
          );
        }).toList(),
      );
}

/// Bars with a light-to-dark gradient over time (reference "Growth rate" card).
class TrendBars extends StatelessWidget {
  const TrendBars({super.key, required this.values, this.height = 130});
  final List<double?> values;
  final double height;

  @override
  Widget build(BuildContext context) {
    final nums = values.whereType<double>().toList();
    if (nums.isEmpty) {
      return Container(
        height: height,
        alignment: Alignment.center,
        decoration: BoxDecoration(color: AgriColors.surfaceMuted, borderRadius: BorderRadius.circular(14)),
        child: const Text('Not enough readings yet', style: TextStyle(color: AgriColors.textFaint, fontWeight: FontWeight.w600, fontSize: 12)),
      );
    }
    final min = nums.reduce((a, b) => a < b ? a : b);
    final max = nums.reduce((a, b) => a > b ? a : b);
    final span = (max - min) == 0 ? 1 : max - min;
    return SizedBox(
      height: height,
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.end,
        children: [
          for (var i = 0; i < values.length; i++)
            Expanded(
              child: Padding(
                padding: const EdgeInsets.symmetric(horizontal: 1.5),
                child: Container(
                  height: values[i] == null ? 4 : height * (0.18 + 0.82 * ((values[i]! - min) / span)),
                  decoration: BoxDecoration(
                    color: values[i] == null
                        ? AgriColors.border
                        : Color.lerp(const Color(0xFFB8E6A0), const Color(0xFF15462D), values.length > 1 ? i / (values.length - 1) : 1),
                    borderRadius: BorderRadius.circular(4),
                  ),
                ),
              ),
            ),
        ],
      ),
    );
  }
}

class RoundButton extends StatelessWidget {
  const RoundButton({super.key, required this.icon, required this.onPressed, required this.tooltip, this.size = 50, this.dark = false});
  final IconData icon;
  final VoidCallback onPressed;
  final String tooltip;
  final double size;
  final bool dark;
  @override
  Widget build(BuildContext context) => Tooltip(
        message: tooltip,
        child: Material(
          color: dark ? AgriColors.forest : AgriColors.surface,
          shape: CircleBorder(side: BorderSide(color: AgriColors.forest.withValues(alpha: 0.12))),
          elevation: 0,
          child: InkWell(
            customBorder: const CircleBorder(),
            onTap: onPressed,
            child: Container(
              width: size,
              height: size,
              decoration: const BoxDecoration(shape: BoxShape.circle, boxShadow: kSoftShadow),
              child: Icon(icon, size: size * 0.42, color: dark ? AgriColors.textOnDark : AgriColors.forest),
            ),
          ),
        ),
      );
}

enum PillTone { good, warn, bad, muted }

class StatusPill extends StatelessWidget {
  const StatusPill(this.label, {super.key, this.tone = PillTone.muted, this.dark = false});
  final String label;
  final PillTone tone;
  final bool dark;
  static const _colors = {
    PillTone.good: (Color(0xFFDDF3DF), AgriColors.success),
    PillTone.warn: (Color(0xFFFBEFD9), AgriColors.warning),
    PillTone.bad: (Color(0xFFFBE3E1), AgriColors.error),
    PillTone.muted: (AgriColors.surfaceMuted, AgriColors.textMuted),
  };
  @override
  Widget build(BuildContext context) {
    final (bg, fg) = _colors[tone]!;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
      decoration: BoxDecoration(color: dark ? Colors.white.withValues(alpha: 0.12) : bg, borderRadius: BorderRadius.circular(999)),
      child: Row(mainAxisSize: MainAxisSize.min, children: [
        Container(width: 8, height: 8, decoration: BoxDecoration(color: fg, shape: BoxShape.circle)),
        const SizedBox(width: 6),
        Flexible(child: Text(label, overflow: TextOverflow.ellipsis, style: TextStyle(fontSize: 12, fontWeight: FontWeight.w700, color: dark ? AgriColors.textOnDark : fg))),
      ]),
    );
  }
}

class PrimaryButton extends StatelessWidget {
  const PrimaryButton({super.key, required this.label, required this.onPressed, this.icon, this.loading = false, this.light = false});
  final String label;
  final VoidCallback? onPressed;
  final IconData? icon;
  final bool loading, light;
  @override
  Widget build(BuildContext context) {
    final fg = light ? AgriColors.forest : AgriColors.textOnDark;
    return SizedBox(
      width: double.infinity,
      height: 56,
      child: FilledButton(
        onPressed: loading ? null : onPressed,
        style: FilledButton.styleFrom(
          backgroundColor: light ? AgriColors.mint : AgriColors.forest,
          foregroundColor: fg,
          disabledBackgroundColor: (light ? AgriColors.mint : AgriColors.forest).withValues(alpha: 0.5),
          disabledForegroundColor: fg.withValues(alpha: 0.8),
          shape: const StadiumBorder(),
          textStyle: const TextStyle(fontSize: 16, fontWeight: FontWeight.w700),
        ),
        child: loading
            ? SizedBox(width: 22, height: 22, child: CircularProgressIndicator(strokeWidth: 2.4, color: fg))
            : Row(mainAxisSize: MainAxisSize.min, children: [
                if (icon != null) ...[Icon(icon, size: 20), const SizedBox(width: 10)],
                Flexible(child: Text(label, overflow: TextOverflow.ellipsis)),
              ]),
      ),
    );
  }
}

class AgriTextField extends StatelessWidget {
  const AgriTextField({super.key, required this.label, required this.controller, this.hint, this.obscure = false, this.keyboardType, this.suffix, this.autofill, this.textCapitalization = TextCapitalization.none});
  final String label;
  final TextEditingController controller;
  final String? hint;
  final bool obscure;
  final TextInputType? keyboardType;
  final Widget? suffix;
  final Iterable<String>? autofill;
  final TextCapitalization textCapitalization;
  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.only(top: 14),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Padding(padding: const EdgeInsets.only(left: 4, bottom: 8), child: Text(label, style: const TextStyle(fontSize: 13, fontWeight: FontWeight.w700, color: AgriColors.textMuted))),
          TextField(
            controller: controller,
            obscureText: obscure,
            keyboardType: keyboardType,
            autocorrect: false,
            autofillHints: autofill,
            textCapitalization: textCapitalization,
            style: const TextStyle(fontSize: 16, color: AgriColors.text),
            decoration: InputDecoration(
              hintText: hint,
              hintStyle: const TextStyle(color: AgriColors.textFaint),
              filled: true,
              fillColor: AgriColors.surfaceMuted,
              suffixIcon: suffix,
              contentPadding: const EdgeInsets.symmetric(horizontal: 18, vertical: 17),
              border: OutlineInputBorder(borderRadius: BorderRadius.circular(18), borderSide: const BorderSide(color: AgriColors.border)),
              enabledBorder: OutlineInputBorder(borderRadius: BorderRadius.circular(18), borderSide: const BorderSide(color: AgriColors.border)),
              focusedBorder: OutlineInputBorder(borderRadius: BorderRadius.circular(18), borderSide: const BorderSide(color: AgriColors.primary, width: 1.5)),
            ),
          ),
        ]),
      );
}

class ErrorBanner extends StatelessWidget {
  const ErrorBanner(this.message, {super.key});
  final String message;
  @override
  Widget build(BuildContext context) => Container(
        width: double.infinity,
        margin: const EdgeInsets.only(top: 14),
        padding: const EdgeInsets.all(12),
        decoration: BoxDecoration(color: const Color(0xFFFBE3E1), borderRadius: BorderRadius.circular(12)),
        child: Text(message, style: const TextStyle(color: AgriColors.error, fontSize: 13)),
      );
}
