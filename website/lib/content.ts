// All page copy and data in one place.

export interface Chapter {
  start: number;
  end: number;
  eyebrow: string;
  title: string;
  accent?: string;
  body: string;
  hero?: boolean;
  /** Shown centred at the top (used for the top-view finale). */
  top?: boolean;
}

/** Story chapters; start/end are 0..1 progress through the 3D section. */
export const CHAPTERS: Chapter[] = [
  { start: 0, end: 0.1, eyebrow: 'Introducing AgriBot', title: 'The field robot that sees disease', accent: 'before you do.', body: 'Scroll to follow one patrol through a tomato field.', hero: true },
  { start: 0.12, end: 0.25, eyebrow: '01 · Patrol', title: 'Drives every row, so you don’t have to.', body: 'Four-wheel drive with chunky treads rolls over furrows and mud while live readings stream to your phone.' },
  { start: 0.27, end: 0.385, eyebrow: '02 · Target', title: 'Locks on to the plant that looks wrong.', body: 'The camera mast pans across the canopy and stops on a suspicious leaf.' },
  { start: 0.4, end: 0.53, eyebrow: '03 · Vision', title: 'On-board AI names the disease in under a second.', body: 'A YOLO model trained on tomato leaves runs right on the robot — healthy, bacterial spot, early blight, late blight or leaf curl virus.' },
  { start: 0.55, end: 0.67, eyebrow: '04 · Sense', title: 'Then it checks the soil around the plant.', body: 'The sensor arm probes moisture and pH while onboard sensors read temperature, humidity and rain.' },
  { start: 0.69, end: 0.81, eyebrow: '05 · Notify', title: 'And tells you exactly what to do.', body: 'One alert with the photo, the diagnosis, the field conditions and the next step.' },
  { start: 0.86, end: 1.01, eyebrow: '06 · Inside AgriBot', title: 'Every sensor, one robot.', body: 'Camera, AI, soil probes and climate sensors, all wired to a Raspberry Pi.', top: true },
];

export type IconName = 'water' | 'thermometer' | 'cloud' | 'flask';

export interface Reading {
  icon: IconName;
  label: string;
  to: number;
  decimals?: number;
  unit?: string;
  prefix?: string;
}

export const READINGS: Reading[] = [
  { icon: 'water', label: 'Soil moisture', to: 42, unit: '%' },
  { icon: 'thermometer', label: 'Temperature', to: 27.8, decimals: 1, unit: '°C' },
  { icon: 'cloud', label: 'Humidity', to: 68, unit: '%' },
  { icon: 'flask', label: 'Soil pH', to: 6.6, decimals: 1, prefix: 'pH ' },
];

export interface Screen {
  src: string;
  /** Status-bar colour, matched to the top of the screenshot. */
  bar: string;
  title: string;
  text: string;
}

export const SCREENS: Screen[] = [
  { src: '/screens/02-field.jpg', bar: '#dcefd6', title: 'Your field at a glance', text: 'Live soil moisture, temperature, humidity, pH and rain on top of a map of your field.' },
  { src: '/screens/05-scan.jpg', bar: '#f1f3ee', title: 'Leaf scan', text: 'Tap once and the robot photographs the crop, names the disease and shows you how sure it is.' },
  { src: '/screens/03-analysis.jpg', bar: '#dcefd6', title: 'What to do next', text: 'Plain-language recommendations from the latest readings and scan.' },
  { src: '/screens/04-trends.jpg', bar: '#dcefd6', title: 'Trends', text: 'Hour, day and week charts show how your soil and weather are changing.' },
  { src: '/screens/06-robot.jpg', bar: '#f1f3ee', title: 'Robot control', text: 'Connect over Bluetooth, share your hotspot and check the robot’s camera, model and sensors.' },
  { src: '/screens/07-map.jpg', bar: '#dcefd6', title: 'Find your robot', text: 'A full-screen live map of where AgriBot is working right now.' },
];

export const SPECS: { title: string; text: string }[] = [
  { title: '5 classes', text: 'Healthy, Bacterial Spot, Early Blight, Late Blight, Yellow Leaf Curl Virus' },
  { title: 'On-device AI', text: 'YOLO26s on ONNX Runtime — no internet needed to diagnose' },
  { title: '5 field sensors', text: 'Soil moisture, pH, DHT22 temperature & humidity, FC-37 rain' },
  { title: 'Bluetooth setup', text: 'Share your phone hotspot with the robot in one tap' },
  { title: 'Live updates', text: 'Readings every 30 s, scans on demand, alerts in real time' },
  { title: 'Raspberry Pi', text: 'Open hardware, standard GPIO wiring, one install script' },
];

/** APK asset published on the GitHub Release. */
export const APK_PATH = 'https://github.com/rayyanshaikh123/agribot/releases/download/app/agribot.apk';

/** Callouts for the top-view finale; ids match the anchors in lib/scene.ts. */
export const SENSORS: { id: 'camera' | 'probe' | 'dht' | 'rain' | 'pi' | 'radio' | 'power' | 'drive'; name: string; detail: string }[] = [
  { id: 'camera', name: 'Camera + AI', detail: 'Pi Camera · YOLO26s on ONNX Runtime' },
  { id: 'probe', name: 'Soil probe', detail: 'Capacitive moisture + pH via MCP3008' },
  { id: 'dht', name: 'DHT22', detail: 'Air temperature & humidity' },
  { id: 'rain', name: 'FC-37 rain sensor', detail: 'Detects rain on the deck' },
  { id: 'pi', name: 'Raspberry Pi 5', detail: 'MCP3008 ADC · runs everything on board' },
  { id: 'radio', name: 'Bluetooth + Wi-Fi', detail: 'Phone setup and live sync' },
  { id: 'power', name: 'Battery bay', detail: 'Battery pack + 5 V buck for the Pi 5' },
  { id: 'drive', name: 'Four-wheel drive', detail: 'Geared DC motors on an L298N driver' },
];
