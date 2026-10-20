# AgriBot

AgriBot is an IoT-based smart farming prototype. A robot collects soil and environmental readings, sends them to an Express backend, and the farmer mobile app displays the latest information and live updates.

This repository contains these parts:

- `backend`: Node.js, Express, MongoDB/Mongoose, JWT authentication, and Socket.IO.
- `flutter_app`: the farmer app for iOS and Android, written in Flutter. It shows live field data, a field map, crop-disease scans, and Bluetooth Wi-Fi sharing with the robot. See [flutter_app/README.md](flutter_app/README.md).
- `pi`: one lightweight Python service for the Raspberry Pi. It reads the sensors, runs the tomato-leaf disease model on the camera, takes commands from the app, and accepts Wi-Fi details from the phone over Bluetooth.
- `ai`: training-side tools for the YOLO26s tomato-leaf disease model and its ONNX export.
- `website`: the product landing page in Next.js + three.js, with a scroll-driven 3D rover story, an app screenshot carousel and the APK download. See [website/README.md](website/README.md).
- `docs`: Raspberry Pi pin mapping and wiring diagram.

The physical robot is not required for development. The development simulator sends the same ingestion request that an ESP32 or Raspberry Pi will eventually send.

## Architecture

```text
                 Bluetooth LE (share Wi-Fi + server address)
   Phone app  <------------------------------------------->  Raspberry Pi service
      |                                                         |  sensors, camera,
      | REST + Socket.IO (farmer JWT)                           |  ONNX disease model
      v                                                         v
   Express backend  <---- readings, scans, heartbeat (x-robot-key) ----
      |    \---- queued commands (scan, refresh) returned in heartbeat ---->
      v
   MongoDB
```

Original sensor data flow:

```text
ESP32 / Raspberry Pi / Simulator
        |
        | POST /api/robot/data
        | x-robot-key
        v
Node.js + Express
        |
        v
MongoDB Community Server
        |
        | Socket.IO: robot:data
        v
Flutter Farmer App
        |
        | GET /api/robot/dashboard + farmer JWT
        v
Latest dashboard data
```

For farmer authentication:

```text
Mobile app
    |
    | POST /api/auth/login
    v
Express + bcryptjs + JWT
    |
    | JWT stored in the iOS Keychain / Android Keystore
    v
Protected dashboard and Socket.IO connections
```

## Technology Stack

Use Node.js 22 LTS (recorded in `.nvmrc`) and Flutter 3.47.1 / Dart 3.13.1, matching CI. The backend requires Node.js 20.19.0 or newer because of Mongoose and the MongoDB driver.

### Backend

- Node.js 22 LTS: JavaScript runtime; minimum supported version is 20.19.0.
- Express `^5.2.1`: HTTP server and API routes.
- Mongoose `^9.10.1`: MongoDB connection and schemas.
- dotenv `^18.0.1`: Loads local backend environment variables.
- cors `^2.8.6`: Allows the mobile client to make HTTP requests during development.
- bcryptjs `^3.0.3`: Hashes and verifies farmer passwords.
- jsonwebtoken `^9.0.3`: Creates and verifies farmer JWTs.
- socket.io `^4.8.3`: Authenticated real-time server connections and `robot:data` events.
- nodemon `^3.1.14`: Restarts the backend during development.

### Farmer app (Flutter)

- Flutter 3.47.1 and its bundled Dart 3.13.1.
- `provider`: app state.
- `http` and `socket_io_client`: REST and live Socket.IO updates.
- `flutter_secure_storage`: stores the farmer JWT in the Keychain or Keystore.
- `flutter_map` and `latlong2`: native OpenStreetMap field map.
- `flutter_reactive_ble`: Bluetooth LE to the Pi. It is BSD-licensed and free for commercial use.
- `permission_handler`: Android "Nearby devices" permission.

## Prerequisites

Install the following before cloning or running the project:

1. Git.
2. Node.js 22 LTS and npm, for the backend and website. With nvm, run `nvm install` then `nvm use` in the repository root.
3. Flutter 3.47.1, for the app. For Android, install Android Studio, JDK 17, Android SDK Platform 36, Build-Tools 36.0.0, and NDK 28.2.13676358. For iPhone builds, use macOS with Xcode and CocoaPods.
4. A MongoDB database: MongoDB Atlas, or MongoDB Community Server locally.
5. Visual Studio Code, recommended.

Do not commit `node_modules`, Flutter `build/` output, or `.env` files. The `.gitignore` files already exclude them.

## Clone the Repository

The repository is public. Use HTTPS; cloning this URL does not require an SSH key or a collaborator invitation:

```powershell
git clone https://github.com/rayyanshaikh123/agribot.git
cd agribot
```

The project folders are independent:

```text
agribot/
├── backend/       Node.js API (npm)
├── flutter_app/   Farmer app (Flutter)
├── pi/            Raspberry Pi service (Python)
├── ai/            Model training and export tools
└── docs/          Wiring and pin mapping
```

If cloning fails:

- `Permission denied (publickey)`: use the HTTPS command above instead of `git@github.com:...`.
- `Could not resolve host`, connection timeout or TLS errors: check your network/proxy and Git installation. Try another network. Do not disable SSL verification.
- `destination path 'agribot' already exists`: use the existing checkout with `cd agribot` and `git pull --ff-only`, or clone into a different new folder.
- For an interrupted download on a slow connection, try `git clone --depth 1 https://github.com/rayyanshaikh123/agribot.git`.

There are no submodules or Git LFS downloads. The AI weights are separate and are **not needed to build the phone app**, run the backend or build the website.

## Farmer App Installation

```bash
cd flutter_app
flutter --version                        # use Flutter 3.47.1
flutter doctor -v
flutter doctor --android-licenses         # Android only; accept the SDK licenses
flutter pub get --enforce-lockfile
flutter devices
flutter run -d <your-phone>               # debug, with hot reload
flutter run --release -d <your-phone>     # install for everyday use
```

Bluetooth works in every build, so no special build is needed. On iPhone, allow **Local Network** and **Bluetooth** when iOS asks.

For an Android APK, run `flutter build apk --release` from `flutter_app`. The output is `flutter_app/build/app/outputs/flutter-apk/app-release.apk`. It uses a local debug signing key for team testing; Play Store publishing requires your own release signing configuration.

Every push to `main` and pull request runs [build checks](https://github.com/rayyanshaikh123/agribot/actions/workflows/ci.yml). After a successful run, open it and download **agribot-android-apk** from **Artifacts** (GitHub sign-in required). This lets teammates install the app without setting up Flutter locally.

See [the app build guide](flutter_app/README.md#build-from-a-fresh-clone) for Java, Android SDK and iPhone setup. No Pi, model weights or backend credentials are needed to compile the app.

## Backend Installation

From the repository root:

```powershell
cd backend
npm ci
```

Available scripts from the actual backend `package.json`:

```powershell
npm run dev
```

Runs `nodemon src/server.js` and restarts the server when backend source files change.

```powershell
npm start
```

Runs `node src/server.js` normally.

## Local MongoDB Setup

Install and run MongoDB Community Server locally. MongoDB Compass is only a graphical client; opening Compass does not start the database server.

The backend is configured to use:

```text
mongodb://127.0.0.1:27017/agribot
```

The `agribot` database and application collections are created when the backend writes data. Use this URI for local MongoDB Community Server, or replace `MONGODB_URI` with your team's MongoDB Atlas connection string.

## Environment Variables

The safe template is [backend/.env.example](backend/.env.example). Create the real local file by copying it:

```powershell
cd backend
Copy-Item .env.example .env
```

On macOS/Linux, use `cp .env.example .env` instead of `Copy-Item`.

Open `backend/.env` locally and replace the placeholder values. Never paste real values into the repository or into documentation.

Required variables:

```text
PORT=5000
MONGODB_URI=mongodb://127.0.0.1:27017/agribot
JWT_SECRET=<private farmer JWT signing secret>
ROBOT_INGEST_KEY=<private robot ingestion key>
```

Optional simulator variable:

```text
ROBOT_SIM_INTERVAL_MS=5000
```

The backend reads these values with `process.env`. The simulator uses `ROBOT_INGEST_KEY` and `ROBOT_SIM_INTERVAL_MS`.

## App Server Address

On the login screen, tap **Server: …** to choose the backend. Use **Test** to check it, then **Save**. The address is stored on the phone, so changing networks never needs a rebuild.

- **Laptop on the same Wi-Fi**: `http://<laptop-name>.local:5000` or `http://<laptop-IPv4>:5000`. On Windows, find the IPv4 address with `ipconfig`. On a Mac, use `ipconfig getifaddr en0`.
- **Hosted backend**: `https://<your-service>.onrender.com`. See [Hosting the Backend](#hosting-the-backend).

Never use `localhost` from a phone, because there it means the phone itself.

To bake a different default into a build, pass it at build time:

```bash
flutter run --release --dart-define=API_URL=http://192.168.1.20:5000
```

When the app shares a network with the Pi over Bluetooth, it also sends this server address. The Pi then publishes to the same server the app uses.

## Running the Project

Start the backend first.

### Terminal 1: backend

```powershell
cd agribot\backend
npm run dev
```

Expected responsibilities:

- Connect to local MongoDB.
- Start Express and Socket.IO on port `5000`.
- Serve authentication and robot routes.

### Terminal 2: farmer app

```bash
cd flutter_app
flutter run -d <your-phone>
```

Log in with a farmer account. Keep the backend running while you use the app.

## Authentication API

### `POST /api/auth/signup`

Creates a farmer account. Request body:

```json
{
  "name": "Test Farmer",
  "email": "farmer@example.com",
  "password": "password123"
}
```

Passwords are hashed with bcryptjs before storage. The password hash is not returned to the client.

### `POST /api/auth/login`

Request body:

```json
{
  "email": "farmer@example.com",
  "password": "password123"
}
```

The backend verifies the bcrypt hash and returns a JWT plus safe user information. The app stores only the JWT, in the iOS Keychain or Android Keystore.

### `GET /api/auth/me`

Requires:

```http
Authorization: Bearer <farmer-jwt>
```

This verifies the token and returns safe authenticated user information. The mobile app uses this during startup to restore a valid session.

## Robot Ingestion API

### `POST /api/robot/data`

This endpoint represents robot-to-backend communication. It requires the separate robot ingestion key, not a farmer JWT:

```http
x-robot-key: <ROBOT_INGEST_KEY>
```

Example body:

```json
{
  "sensors": {
    "soilMoisture": 88,
    "temperature": 36,
    "humidity": 45
  },
  "robot": {
    "status": "online"
  },
  "location": {
    "latitude": 19.047838,
    "longitude": 72.872712
  }
}
```

The backend validates the request, generates `robot.lastUpdated`, stores the document in MongoDB, emits the safe `robot:data` payload, and returns the stored dashboard data. It does not store or emit the robot key.

The real ESP32/Raspberry Pi will eventually send this same request.

## Dashboard API

### `GET /api/robot/dashboard`

This endpoint is for the farmer mobile app and requires a farmer JWT:

```http
Authorization: Bearer <farmer-jwt>
```

It returns the latest available robot data:

```json
{
  "success": true,
  "data": {
    "sensors": {
      "soilMoisture": 48,
      "temperature": 29,
      "humidity": 64
    },
    "robot": {
      "status": "online",
      "lastUpdated": "2026-09-19T10:00:00.000Z"
    },
    "location": {
      "latitude": 19.076,
      "longitude": 72.8777
    }
  }
}
```

The mobile dashboard and Location screen use the same data contract. MongoDB is accessed only by the backend, never directly by the mobile app.

## Robot Control, Scans and History APIs

Robot-side endpoints require `x-robot-key`. Farmer endpoints require `Authorization: Bearer <farmer-jwt>`.

| Method and path | Caller | Purpose |
| --- | --- | --- |
| `POST /api/robot/heartbeat` | Pi, every 3 s | Reports Pi status and capabilities. The response carries queued app commands. |
| `POST /api/robot/detections` | Pi | Stores a disease scan with its annotated JPEG and emits `robot:detection`. |
| `POST /api/robot/commands/:id/result` | Pi | Reports whether a command worked and emits `robot:command`. |
| `GET /api/robot/status` | App | Returns whether the robot is online (heartbeat within 15 s), its last heartbeat, and pending commands. |
| `POST /api/robot/commands` | App | Queues `{ "type": "scan" }` or `{ "type": "publish" }`. Returns 409 when the robot is offline. |
| `GET /api/robot/history?range=hour\|day\|week` | App | Returns readings averaged into 48 time buckets for charts. |
| `GET /api/robot/detections` | App | Lists recent scans without images. |
| `GET /api/robot/detections/latest` | App | Returns the newest scan with its image. |
| `GET /api/robot/detections/:id` | App | Returns one scan with its image. |

## Socket.IO Real-Time Updates

The backend and mobile client use Socket.IO `4.8.3`.

| Event | Meaning |
| --- | --- |
| `robot:data` | A new sensor reading was stored. |
| `robot:status` | The robot went online or offline, or its reported status changed. |
| `robot:detection` | A new crop-disease scan, including the annotated image. |
| `robot:command` | A queued command finished, with `{ id, ok, message }`. |

The live flow is:

```text
POST /api/robot/data
        ↓
MongoDB save
        ↓
Socket.IO emits robot:data
        ↓
Flutter app Field / Map / Leaf scan screens
        ↓
Flutter app state updates immediately
```

REST provides the initial/latest state when a screen opens. Socket.IO delivers later changes without a manual refresh. The mobile app authenticates its Socket.IO connection with the farmer JWT. The robot ingestion key is not sent to the mobile app.

## Robot Simulator

The simulator is [backend/src/tools/robot-simulator.js](backend/src/tools/robot-simulator.js). It is explicitly **development/test only** and represents one future physical AgriBot.

Run it from the backend directory:

```powershell
cd backend
npm run simulate
```

It:

- Reads `ROBOT_INGEST_KEY` using dotenv.
- Sends `POST /api/robot/data` with the `x-robot-key` header.
- Generates gradually changing sensor and GPS values.
- Sends updates every `5000` milliseconds by default.
- Uses `ROBOT_SIM_INTERVAL_MS` when that environment variable is a positive number.
- Continues after temporary network or HTTP failures.
- Stops cleanly with Ctrl+C.
- Never prints the robot key, JWT, password, MongoDB URI, or other secrets.

The simulator targets `http://127.0.0.1:$PORT` by default. To send to another server, set `ROBOT_SIM_URL`, for example `ROBOT_SIM_URL=https://<your-service>.onrender.com npm run simulate`.

## Field Map

The app draws the map natively with `flutter_map` and OpenStreetMap tiles. The tiles are tinted mint to match the design, with a dashed field boundary and the robot marker. There is no WebView. The phone needs internet access to load tiles, and the map shows the required OpenStreetMap attribution. Only latitude, longitude and online state reach the map widget.

## First-Run Testing Checklist

- [ ] Node.js LTS and the Flutter SDK are installed.
- [ ] A MongoDB database is available (Atlas or local).
- [ ] `npm ci` completed in `backend`.
- [ ] `backend/.env` was created from `.env.example`.
- [ ] Backend starts with `npm run dev`, and `GET /api/health` reports `"database":"connected"`.
- [ ] `flutter pub get` completed in `flutter_app`.
- [ ] The app is installed on a phone and the login screen's **Server** points at the backend.
- [ ] Signup and login work.
- [ ] The Field screen shows soil moisture, temperature, humidity, pH and rain.
- [ ] Live updates arrive without refreshing (run `npm run simulate`).
- [ ] The full map shows tiles and the robot marker.
- [ ] With the Pi running, the Robot tab shows **Online**, and **Scan leaves now** returns a scan.

A simple local health check from PowerShell is:

```powershell
Invoke-RestMethod http://127.0.0.1:5000/api/health
```

## Troubleshooting

### Port 5000 already in use

On macOS, the AirPlay Receiver already listens on port 5000. Set `PORT=5001` in `backend/.env` and use `:5001` in the app's server address.

Find the process using the port:

```powershell
Get-NetTCPConnection -LocalPort 5000 -State Listen
```

Close the old backend process from the terminal where it is running, or stop the specific process after confirming it is safe to stop. Do not blindly terminate unrelated processes.

### MongoDB connection refused

Confirm MongoDB Community Server is running. Compass alone is not enough. Check that the local URI in your private `backend/.env` is:

```text
mongodb://127.0.0.1:27017/agribot
```

### Phone cannot reach the backend

- Confirm the backend is running on port `5000`.
- Confirm the phone and laptop use the same Wi-Fi network.
- On the login screen, tap **Server: …** and use **Test**. The address must be the laptop's current name or IP, not `localhost`.
- On iPhone, check *Settings > AgriBot > Local Network* is on.
- Check Windows Firewall or macOS firewall rules if the phone cannot reach the laptop.

### Wrong laptop IP

Run:

```powershell
ipconfig
```

Use the active adapter's IPv4 address in the app's **Server** setting. A `.local` name, such as `http://My-Laptop.local:5000`, survives IP changes.

### Phone and laptop are not on the same network

Connect both devices to the same local network. Guest Wi-Fi, VPNs, and client isolation can prevent device-to-laptop communication.

### Backend is not running

From `backend` run:

```powershell
npm run dev
```

The mobile app cannot authenticate or load robot data while the backend is stopped.

### HTTP `401` from authentication

Check that the user credentials are correct and that the backend `.env` contains the same private `JWT_SECRET` used when the server started. Do not print or commit the secret. Logging out and logging in again clears an expired mobile token.

### HTTP `401` from robot ingestion

The simulator or hardware must send the `x-robot-key` header. The value must match the private `ROBOT_INGEST_KEY` in `backend/.env`. Never place the key in the mobile app, URL, logs, or README.

### Socket.IO is not connecting

- Confirm the backend is running and reachable from the phone.
- Confirm the farmer is logged in and has a valid JWT.
- Confirm the app's **Server** setting points at the same backend.
- The backend uses Socket.IO 4 and the app uses `socket_io_client` 3.x, which are compatible.
- The dashboard should retain the last REST data if Socket.IO temporarily disconnects.

### Map tiles are not loading

- Confirm the phone has internet access.
- Confirm OpenStreetMap tile requests are not blocked by the network.
- OpenStreetMap tiles require visible attribution and are not an offline tile service.

### Flutter build problems

- Use Flutter 3.47.1 and run `flutter doctor -v`. An older Flutter SDK may fail on the Android Gradle Plugin 9 configuration even if Dart dependencies resolve.
- `No Android SDK found`: install Android Studio's SDK tools and configure the SDK path with `flutter config --android-sdk <sdk-path>`.
- Install Android Platform 36, Build-Tools 36.0.0 and NDK 28.2.13676358 in Android Studio's SDK Manager; then run `flutter doctor --android-licenses`.
- Java/Gradle compatibility errors: select JDK 17 with `flutter config --jdk-dir <jdk-17-home>`, then rerun `flutter doctor -v`. Do not use Java 8 or 11.
- Dependency lockfile mismatch: use the documented Flutter version before running `flutter pub get --enforce-lockfile`. Keep the lockfile committed.
- iPhone: open `flutter_app/ios/Runner.xcworkspace` once in Xcode and pick your signing team.
- See [the app build guide](flutter_app/README.md#build-from-a-fresh-clone) for full commands and iOS bundle identifier setup.
- Android: Bluetooth needs Android 6 (SDK 23) or newer.

### `npm install` problems

- Confirm Node.js LTS and npm are installed.
- Run the command from the correct project directory.
- Do not delete lockfiles or change package versions casually.
- Keep `package-lock.json` committed and do not commit `node_modules`.

## File and Folder Overview

### Farmer app

```text
flutter_app/lib/
├── main.dart                 Providers and the sign-in gate.
├── core/                     Config (default server, Bluetooth UUIDs), theme, formatting, crop advice.
├── models/robot.dart         API models.
├── services/api_client.dart  JSON client for the backend.
├── state/
│   ├── auth_controller.dart  Login, signup, token storage, server address.
│   ├── robot_controller.dart One REST load and one Socket.IO connection; scan/refresh commands.
│   └── ble_controller.dart   Find the Pi, connect, read Wi-Fi status, share a network.
├── widgets/                  Hatch texture, cards, chips, pill tabs, charts, field map.
└── screens/                  Login/signup, home shell, Field, Leaf scan, Robot, Share network, full map.
```

### Backend

```text
backend/src/
├── server.js                         Express + HTTP server + Socket.IO setup.
├── config/
│   ├── db.js                         Mongoose connection using MONGODB_URI (+ DNS fallback).
│   └── env.js                        Startup checks for required settings.
├── models/
│   ├── User.js                       Farmer account schema.
│   ├── RobotData.js                  Sensor/status/GPS schema with timestamps.
│   └── Detection.js                  Crop-disease scan results and annotated image.
├── middleware/
│   ├── auth.middleware.js            Farmer JWT HTTP protection.
│   ├── robot-auth.middleware.js      x-robot-key ingestion protection (constant-time).
│   ├── rate-limit.middleware.js      Login/signup attempt limiting.
│   └── socket-auth.middleware.js     Farmer JWT Socket.IO handshake protection.
├── routes/
│   ├── test.routes.js                GET /api/health.
│   ├── auth.routes.js                Signup, login, and /me.
│   └── robot.routes.js               Ingestion, heartbeat, commands, scans, history, dashboard.
├── services/
│   └── robot-presence.js             In-memory robot online state and command queue.
└── tools/
    └── robot-simulator.js            Development-only changing robot data sender.
```

## Security Rules

Never commit or share:

- `backend/.env`
- MongoDB credentials or connection secrets
- `JWT_SECRET`
- `ROBOT_INGEST_KEY`
- Passwords
- JWT tokens
- Keychain or Keystore contents

The ignore files cover `.env` files, `node_modules`, Flutter `build/` and `.dart_tool/`, model weights, and Python caches.

Do not upload:

- `node_modules`
- `.env`
- Flutter build output
- Temporary files
- Logs
- Personal machine configuration

Do upload:

- Source code
- `package.json`
- `package-lock.json`
- Root `README.md`
- `backend/.env.example`

## Raspberry Pi Service

The Pi runs one process, [pi/agribot_service.py](pi/agribot_service.py), with small modules in [pi/agribot/](pi/agribot/). It replaces the earlier `sensor_server.py`. Every part is optional: missing hardware is reported to the app and the rest keeps working.

- **Sensors** publish to `POST /api/robot/data` every 30 seconds. They use DHT22, FC-37 and the MCP3008 as wired below, or simulated values when the GPIO libraries are absent.
- **Crop vision** captures from a Pi Camera (Picamera2) or USB webcam. It runs `best.onnx` with ONNX Runtime and uploads an annotated JPEG. Expect roughly 1–2 seconds per scan on the Raspberry Pi 5. It measured about 0.2 seconds on a laptop CPU.
- **Commands** arrive in the heartbeat response, so the app's **Scan leaves now** and **Refresh** buttons work without the phone reaching the Pi directly.
- **Bluetooth Wi-Fi sharing** advertises as `AgriBot`. The app connects, shows the Pi's Wi-Fi state, and sends a network name, password and the server address. The Pi joins the network with NetworkManager, which remembers it for later boots.

### Wiring

The full pin mapping covers sensors, the MCP3008, the L298N motor driver and power. It lives in [docs/PINOUT.md](docs/PINOUT.md), with a colour-coded header diagram:

![AgriBot Raspberry Pi wiring](docs/pi-pinout.svg)

In short:

- **DHT22** DATA goes to GPIO 24 (pin 18). **FC-37** DO goes to GPIO 27 (pin 13).
- The **MCP3008** is on SPI0 (pins 19, 21, 23, 24). The soil probe goes to CH0 and the pH probe to CH2.
- The **L298N** is on GPIO 12, 13 (PWM speed) and GPIO 5, 6, 16, 26 (direction).

Pi GPIO is 3.3 V only, and all grounds must be shared.

### Install on the Pi

Use Raspberry Pi OS Bookworm or newer, 64-bit, because it uses NetworkManager. Clone the repository on the Pi, then copy the model from your laptop. Weights are git-ignored.

```bash
# On the laptop, from the ai/ folder:
python scripts/export_onnx.py
ssh PI_USER@PI_IP 'mkdir -p ~/agribot/ai/models/weights'
scp models/weights/best.onnx PI_USER@PI_IP:~/agribot/ai/models/weights/

# On the Pi:
cd ~/agribot/pi
cp agribot.env.example agribot.env      # set ROBOT_INGEST_KEY and the robot location
sudo ./install.sh
```

[pi/install.sh](pi/install.sh) does the following:

- Installs the apt packages and creates `/opt/agribot/venv`.
- Copies `agribot.env` to `/etc/agribot.env` and enables SPI.
- Disables the old `agribot-sensors` service.
- Installs and starts [pi/agribot.service](pi/agribot.service).

The service runs as root because changing Wi-Fi and registering a Bluetooth service both need it. Follow the logs with:

```bash
journalctl -u agribot -f
```

`ROBOT_INGEST_KEY` is the only value that must be set by hand. `AGRIBOT_BACKEND_URL` can be left as a placeholder, because the app sends its server address when it shares a network.

The service reads `/etc/agribot.env` itself and takes every value literally, so a robot key containing characters like `$ # & !` works as-is. Quotes are optional.

A server address shared from the app over Bluetooth is saved in `/var/lib/agribot/state.json`, and it takes priority over `AGRIBOT_BACKEND_URL`. To go back to the env file value, run `sudo rm /var/lib/agribot/state.json && sudo systemctl restart agribot`.

Set `BLE_PAIRING_PIN` in `/etc/agribot.env` so that strangers nearby cannot change the robot's Wi-Fi. The app then asks for that PIN.

### Share a network from the app

1. Open the **Robot** tab and tap **Find AgriBot** near the robot, then **Connect**.
2. Tap **Share network**. Pick a network the Pi can see, or type your phone hotspot's name, then enter the password.
3. Keep **Also send server address** on. The sheet shows live progress until the Pi reports that it has joined and can reach the server.

### Local diagnostics API

The service also listens on port 8000 for manual tests on the LAN:

```bash
curl http://PI_IP:8000/status                 # capabilities, Wi-Fi, backend connection
curl http://PI_IP:8000/sensors                # read all sensors without publishing
curl http://PI_IP:8000/sensors/dht22          # also: soil-moisture, rainfall, ph
curl -X POST http://PI_IP:8000/sensors/publish
curl -X POST http://PI_IP:8000/vision/scan    # capture, detect, upload
curl -X POST --data-binary @leaf.jpg "http://PI_IP:8000/vision/detect?publish=1"
curl -o latest.jpg http://PI_IP:8000/vision/latest.jpg
curl http://PI_IP:8000/network                # Wi-Fi state and nearby networks
```

Wi-Fi can only be changed over Bluetooth, never over this HTTP API.

### Calibration and demo values

Use the raw value from `/sensors/soil-moisture` to set `SOIL_DRY_VALUE` with the probe in dry soil and `SOIL_WET_VALUE` with it in wet soil. Until the analog probes are wired, `USE_FAKE_SOIL_MOISTURE` and `USE_FAKE_PH` publish the `FAKE_*` values. Restart the service after editing `/etc/agribot.env`.

### Run on a laptop for testing

The service also runs on a laptop with simulated sensors and no Bluetooth:

```bash
pip install onnxruntime opencv-python-headless numpy
cd pi
AGRIBOT_BACKEND_URL=http://127.0.0.1:5000 ROBOT_INGEST_KEY=... CAMERA=none STATE_FILE=state.json python agribot_service.py
```

### Robot JSON contract

Readings sent to `POST /api/robot/data` keep the original shape:

```json
{
  "sensors": { "soilMoisture": 48, "temperature": 29, "humidity": 64, "rainfall": 0, "ph": 6.8 },
  "robot": { "status": "online" },
  "location": { "latitude": 19.047838, "longitude": 72.872712 }
}
```

The backend generates `robot.lastUpdated`, MongoDB `_id`, `createdAt`, and `updatedAt`. The hardware must not send or store farmer JWTs, MongoDB credentials, JWT secrets, or the robot key in the JSON body.

## Hosting the Backend

The backend runs as a single Node.js process. It has no local files and stores everything in MongoDB, so it deploys to any Node or Docker host.

### What the backend expects from the host

| Variable | Required | Notes |
| --- | --- | --- |
| `MONGODB_URI` | Yes | Use MongoDB Atlas. In Atlas, open *Network Access* and allow the host's IPs, or `0.0.0.0/0` for most free hosts. |
| `JWT_SECRET` | Yes | At least 32 random characters when `NODE_ENV=production`. |
| `ROBOT_INGEST_KEY` | Yes | At least 24 characters. Put the same value in the Pi's `/etc/agribot.env`. |
| `NODE_ENV` | Recommended | `production` enforces strong secrets. |
| `PORT` | Set by host | The server listens on whatever port the platform provides. |
| `CORS_ORIGINS` | Optional | Comma-separated browser origins. Defaults to `*`. The phone app and the Pi are not browsers and are unaffected. |
| `JWT_EXPIRES_IN` | Optional | How long a login lasts. Defaults to `30d`. |

Production behaviour:

- `GET /api/health` returns 503 while the database is down, so the platform can restart the service.
- On SIGTERM the server finishes in-flight requests and closes the database cleanly.
- Login and signup are limited to 20 attempts per IP every 15 minutes.
- Robot keys are compared in constant time.
- Real client IPs are read from the host's proxy.

Run **one instance only**. Robot online status and the app's command queue are kept in memory.

### Render (free tier works)

1. Push this repository to GitHub.
2. In Render, choose **New > Blueprint** and select the repository. [render.yaml](render.yaml) creates the `agribot-backend` service with a generated `JWT_SECRET`.
3. Enter `MONGODB_URI` and `ROBOT_INGEST_KEY` when asked.
4. After deploy, open `https://<your-service>.onrender.com/api/health`.

Free services sleep after 15 minutes without traffic. The Pi's heartbeat, every 3 seconds, keeps the service awake while the robot is on.

### Any Docker host (Railway, Fly.io, Cloud Run, a VPS)

```bash
cd backend
docker build -t agribot-backend .
docker run -p 5000:5000 --env-file .env -e NODE_ENV=production agribot-backend
```

### Point the app and the robot at the hosted URL

- **Flutter app**: on the login screen, tap **Server: …** and enter `https://<your-service>.onrender.com`, then tap **Test**.
- **Raspberry Pi**: share a network from the app's Robot tab with **Also send server address** on, and the Pi saves the hosted URL. You can also set `AGRIBOT_BACKEND_URL` in `/etc/agribot.env`.
- **Simulator**: `ROBOT_SIM_URL=https://<your-service>.onrender.com npm run simulate`.

## Clean Repository Rules

Keep machine-specific and sensitive files local. Do not commit `node_modules`, `.env`, Flutter build output, temporary files, logs, or personal configuration. Keep source code, lockfiles, package manifests, this README, and placeholder environment templates under version control.

## NEW DEVELOPER QUICK START

1. Install Git, Node.js LTS, the Flutter SDK, and VS Code. Use MongoDB Atlas or a local MongoDB.
2. Clone the repository and `cd` into it.
3. Run `cd backend` then `npm ci`, then copy `.env.example` to `.env` and fill in private values.
4. Run `npm run dev` and open `http://localhost:5000/api/health`.
5. Run `cd ../flutter_app && flutter pub get && flutter run -d <your-phone>`.
6. On the login screen, set **Server** to your laptop's address, then sign up.
7. Optional: `npm run simulate` in `backend` to see live data without the robot.
