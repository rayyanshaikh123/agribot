# AgriBot Flutter app

The farmer app for AgriBot, using the backend APIs, Socket.IO events and Pi Bluetooth protocol in this repository.

## Build from a fresh clone

Use **Flutter 3.47.1**, which includes **Dart 3.13.1** and matches the GitHub Actions build. Android uses Gradle 9.3.1, Android Gradle Plugin 9.1.0 and Kotlin 2.4.0; older Flutter installations may not support this configuration.

```bash
git clone https://github.com/rayyanshaikh123/agribot.git
cd agribot/flutter_app
flutter --version
flutter doctor -v
flutter pub get --enforce-lockfile
flutter analyze --no-pub
flutter test --no-pub
```

You can compile and test without the robot, AI weights, a local backend or private credentials. Running the app's data features requires a reachable backend; Bluetooth requires a physical phone.

### Android (Windows, Linux or macOS)

Install Android Studio and **JDK 17**. In Android Studio's SDK Manager, install Android SDK Platform **36**, Build-Tools **36.0.0**, Android SDK Command-line Tools, Platform-Tools, and NDK **28.2.13676358** (enable **Show Package Details** to select it).

```bash
flutter config --android-sdk <your-android-sdk-directory>
flutter config --jdk-dir <your-jdk-17-home>
flutter doctor --android-licenses
flutter doctor -v
flutter build apk --release --no-pub
```

Replace the angle-bracket paths with your actual installation directories; the JDK path must contain `bin/java` (`bin/java.exe` on Windows). Do not commit `android/local.properties`: Flutter generates it with your machine's SDK paths.

The APK is at `build/app/outputs/flutter-apk/app-release.apk`. Enable USB debugging and use `flutter devices` followed by `flutter run -d <device-id>` to run on a phone. Android 6 / API 23 or newer is required. Release APKs currently use the local debug key for team testing; configure a private release key before publishing to an app store. APKs built on different machines may have different signatures, requiring an uninstall before reinstalling (which clears local app data).

[Build and check](https://github.com/rayyanshaikh123/agribot/actions/workflows/ci.yml) runs `flutter analyze` and `flutter test` only; it does not produce an APK or iOS build.

### iPhone (macOS only)

Install Xcode, its iOS platform support, and CocoaPods. After `flutter pub get`, run:

```bash
cd ios
pod install
cd ..
flutter build ios --release --no-codesign
```

This checks compilation without Apple signing credentials. To install on your iPhone, open `ios/Runner.xcworkspace` in Xcode, select **Runner > Signing & Capabilities**, choose your own development team and a unique bundle identifier, then run `flutter run -d <device-id>`. iOS 15 or newer is required. A teammate cannot use another developer's provisioning profile.

The project includes CocoaPods support for plugins that do not provide Swift packages. Keep `Podfile` and `Podfile.lock` committed; generated `Pods/` and `Flutter/ephemeral/` stay local.

## Features

- **Field**: a map header with a mint field map, plus Overview, Analysis and Trends tabs. They show live sensor chips, the crop-health card, recommendations, and charts by hour, day or week.
- **Leaf scan**: the latest annotated camera photo from the Pi's disease model, a **Scan leaves now** button, detections with confidence, care advice, and scan history.
- **Robot**: online status, capabilities, **Find AgriBot** over Bluetooth, **Share network** (Wi-Fi or hotspot plus the server address), refresh readings, and account.
- **Full map**: a native map with zoom and recenter buttons.

## Packages

| Package | Purpose |
| --- | --- |
| `provider` | App state (`AuthController`, `RobotController`, `BleController`) |
| `http`, `socket_io_client` | REST and live Socket.IO updates |
| `flutter_secure_storage` | JWT in the iOS Keychain or Android Keystore |
| `flutter_map`, `latlong2` | Native OpenStreetMap field map, with no WebView |
| `flutter_reactive_ble` | Bluetooth LE to the Pi. It is BSD-licensed and free for commercial use. |
| `permission_handler` | Android "Nearby devices" permission |

`flutter_blue_plus` was deliberately avoided, because version 2 requires a paid licence for any for-profit use.

## Run

```bash
flutter pub get
flutter run -d <device>                       # debug
flutter run --release -d <device>             # what you install for real use
flutter run --dart-define=API_URL=http://192.168.1.20:5001
```

The default server is the hosted backend, `https://agribot-egl8.onrender.com`. You can change it on the login screen with **Server: …**. The new address is saved on the phone, so changing networks does not need a rebuild.

## Layout

```text
lib/
├── main.dart                 Providers and the auth gate
├── core/                     Config (API URL, BLE UUIDs), theme, formatting, crop advice
├── models/robot.dart         API models
├── services/api_client.dart  JSON client
├── state/                    Auth, robot (REST + Socket.IO + commands), Bluetooth provisioning
├── widgets/                  Hatch texture, cards, chips, pill tabs, charts, field map
└── screens/                  Auth, home shell, field, leaf scan, robot, share sheet, map
```
