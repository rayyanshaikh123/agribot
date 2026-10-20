"""Tomato leaf disease detection with the exported YOLO ONNX model.

Uses ONNX Runtime + OpenCV only (no PyTorch), which keeps the Pi footprint
small. Export the model on a laptop with ai/scripts/export_onnx.py and copy
best.onnx to the path in MODEL_PATH.
"""

import ast
import base64
import threading
import time

import numpy as np

try:
    import cv2
except ImportError:  # pragma: no cover - reported through status()
    cv2 = None

# Fallback only; the real names are read from the ONNX metadata.
DEFAULT_NAMES = {0: "Bacterial Spot", 1: "Early Blight", 2: "Healthy", 3: "Late Blight", 4: "Yellow Leaf Curl Virus"}
# BGR colours for annotations, matched to the mobile app palette.
COLORS = {
    "Healthy": (94, 197, 34),
    "Bacterial Spot": (22, 115, 249),
    "Early Blight": (8, 179, 234),
    "Late Blight": (68, 68, 239),
    "Yellow Leaf Curl Virus": (247, 85, 168),
}


def _nms(boxes, scores, iou_threshold):
    """Plain numpy non-maximum suppression. boxes are x1, y1, x2, y2."""
    order = scores.argsort()[::-1]
    areas = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
    keep = []
    while order.size:
        i = order[0]
        keep.append(i)
        xx1 = np.maximum(boxes[i, 0], boxes[order[1:], 0])
        yy1 = np.maximum(boxes[i, 1], boxes[order[1:], 1])
        xx2 = np.minimum(boxes[i, 2], boxes[order[1:], 2])
        yy2 = np.minimum(boxes[i, 3], boxes[order[1:], 3])
        inter = np.clip(xx2 - xx1, 0, None) * np.clip(yy2 - yy1, 0, None)
        iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-9)
        order = order[1:][iou <= iou_threshold]
    return keep


class Detector:
    def __init__(self, config):
        self.cfg = config
        self.error = None
        self.session = None
        self.names = dict(DEFAULT_NAMES)
        self._lock = threading.Lock()
        try:
            self._load()
        except Exception as error:
            self.error = str(error)
            print(f"[vision] Model disabled: {error}")

    def _load(self):
        if cv2 is None:
            raise RuntimeError("OpenCV is not installed (pip install opencv-python-headless)")
        # Check the file first: "model missing" is the more useful message.
        if not self.cfg.model_path.exists():
            raise FileNotFoundError(f"model not found at {self.cfg.model_path}")
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(str(self.cfg.model_path), options, providers=["CPUExecutionProvider"])
        meta = self.session.get_modelmeta().custom_metadata_map
        if "names" in meta:
            self.names = {int(k): v for k, v in ast.literal_eval(meta["names"]).items()}
        model_input = self.session.get_inputs()[0]
        self.input_name = model_input.name
        self.input_size = int(model_input.shape[2]) if isinstance(model_input.shape[2], int) else 640
        print(f"[vision] Loaded {self.cfg.model_path.name} ({self.input_size}px): {list(self.names.values())}")

    @property
    def ready(self):
        return self.session is not None

    def _letterbox(self, image):
        h, w = image.shape[:2]
        scale = min(self.input_size / h, self.input_size / w)
        nh, nw = round(h * scale), round(w * scale)
        resized = cv2.resize(image, (nw, nh), interpolation=cv2.INTER_LINEAR)
        top, left = (self.input_size - nh) // 2, (self.input_size - nw) // 2
        canvas = np.full((self.input_size, self.input_size, 3), 114, dtype=np.uint8)
        canvas[top:top + nh, left:left + nw] = resized
        blob = canvas[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0
        return np.ascontiguousarray(blob), scale, left, top

    def _decode(self, output, scale, pad_x, pad_y, width, height):
        pred = output[0]
        if pred.ndim == 2 and pred.shape[-1] == 6:  # end-to-end export: x1,y1,x2,y2,score,class
            boxes, scores, classes = pred[:, :4], pred[:, 4], pred[:, 5].astype(int)
            mask = scores >= self.cfg.confidence
            boxes, scores, classes = boxes[mask], scores[mask], classes[mask]
        else:  # standard export: (4 + classes, anchors)
            pred = pred.T
            class_scores = pred[:, 4:]
            classes = class_scores.argmax(axis=1)
            scores = class_scores[np.arange(len(pred)), classes]
            mask = scores >= self.cfg.confidence
            cx, cy, bw, bh = pred[mask, :4].T
            boxes = np.stack([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], axis=1)
            scores, classes = scores[mask], classes[mask]
            if len(boxes):
                offset = classes[:, None] * 4096.0  # class-aware NMS
                keep = _nms(boxes + offset, scores, self.cfg.iou)[:100]
                boxes, scores, classes = boxes[keep], scores[keep], classes[keep]

        detections = []
        for box, score, cls in zip(boxes, scores, classes):
            x1, y1, x2, y2 = box
            x1 = float(np.clip((x1 - pad_x) / scale, 0, width))
            x2 = float(np.clip((x2 - pad_x) / scale, 0, width))
            y1 = float(np.clip((y1 - pad_y) / scale, 0, height))
            y2 = float(np.clip((y2 - pad_y) / scale, 0, height))
            detections.append({
                "classId": int(cls),
                "label": self.names.get(int(cls), f"class_{int(cls)}"),
                "confidence": round(float(score), 4),
                "box": [round(x1), round(y1), round(x2), round(y2)],
            })
        detections.sort(key=lambda d: d["confidence"], reverse=True)
        return detections

    def detect(self, image):
        """Run inference on a BGR image and return detections plus timing."""
        if not self.ready:
            raise RuntimeError(f"Vision model is not available: {self.error}")
        image = validate_frame(image)
        height, width = image.shape[:2]
        blob, scale, pad_x, pad_y = self._letterbox(image)
        started = time.perf_counter()
        try:
            with self._lock:
                output = self.session.run(None, {self.input_name: blob})[0]
        except Exception as error:  # onnxruntime errors must not crash the worker
            raise RuntimeError(f"Inference failed: {error}") from error
        elapsed = round((time.perf_counter() - started) * 1000)
        return self._decode(output, scale, pad_x, pad_y, width, height), elapsed

    @staticmethod
    def annotate(image, detections):
        out = image.copy()
        thickness = max(2, round(max(out.shape[:2]) / 400))
        font_scale = max(0.5, max(out.shape[:2]) / 1400)
        for det in detections:
            x1, y1, x2, y2 = det["box"]
            color = COLORS.get(det["label"], (255, 255, 255))
            cv2.rectangle(out, (x1, y1), (x2, y2), color, thickness)
            text = f"{det['label']} {det['confidence']:.0%}"
            (tw, th), base = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, font_scale, 1)
            ty = max(y1, th + base + 4)
            cv2.rectangle(out, (x1, ty - th - base - 4), (x1 + tw + 8, ty), color, -1)
            cv2.putText(out, text, (x1 + 4, ty - base - 2), cv2.FONT_HERSHEY_SIMPLEX, font_scale, (20, 20, 20), 1, cv2.LINE_AA)
        return out


def summarize(detections):
    """Collapse detections into one crop-health verdict for the dashboard."""
    if not detections:
        return {"status": "none", "label": "No leaves detected", "confidence": 0}
    diseases = [d for d in detections if d["label"] != "Healthy"]
    top = diseases[0] if diseases else detections[0]
    return {"status": "disease" if diseases else "healthy", "label": top["label"], "confidence": top["confidence"]}


def encode_jpeg(image, max_side=800, quality=75):
    h, w = image.shape[:2]
    scale = min(1.0, max_side / max(h, w))
    if scale < 1.0:
        image = cv2.resize(image, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)
    ok, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("Could not encode JPEG.")
    return buffer.tobytes()


def jpeg_data_uri(jpeg_bytes):
    return "data:image/jpeg;base64," + base64.b64encode(jpeg_bytes).decode("ascii")


def decode_image(data):
    if cv2 is None:
        raise RuntimeError("OpenCV is not installed.")
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("Body is not a valid JPEG/PNG image.")
    return image


def validate_frame(frame):
    """Return a contiguous 3-channel BGR frame or raise RuntimeError."""
    if not isinstance(frame, np.ndarray) or frame.ndim != 3 or frame.size == 0 or min(frame.shape[:2]) < 16:
        raise RuntimeError("camera returned an invalid frame")
    if frame.shape[2] == 4:  # XBGR/XRGB buffers: drop the padding channel
        frame = frame[:, :, :3]
    elif frame.shape[2] != 3:
        raise RuntimeError(f"camera returned {frame.shape[2]}-channel frames")
    return np.ascontiguousarray(frame)


class Camera:
    """Picamera2 (libcamera also drives UVC webcams such as the Logitech),
    falling back to a USB webcam through OpenCV/V4L2.

    The device stays open between captures; a failed capture closes it and the
    next capture reopens it, so unplugging and replugging the camera recovers
    without restarting the service."""

    def __init__(self, config):
        self.cfg = config
        self.kind = None
        self.error = None
        self.failures = 0
        self._picam = None
        self._usb = None
        self._lock = threading.Lock()
        if config.camera == "none":
            self.error = "disabled by CAMERA=none"
            return
        if config.camera in {"auto", "picamera"}:
            try:
                from picamera2 import Picamera2

                cam = Picamera2()
                cam.configure(cam.create_still_configuration(main={"size": (config.camera_width, config.camera_height), "format": "RGB888"}))
                cam.start()
                time.sleep(1.0)  # let exposure settle
                self._picam, self.kind = cam, "picamera"
            except Exception as error:
                self.error = f"Pi camera unavailable: {error}"
        if self.kind is None and config.camera in {"auto", "usb"}:
            if cv2 is None:
                self.error = (self.error + "; " if self.error else "") + "OpenCV not installed"
            else:
                self._usb = self._open_usb()
                if self._usb is not None:
                    self.kind, self.error = "usb", None
                else:
                    self.error = (self.error + "; " if self.error else "") + f"no USB camera at /dev/video{config.camera_index}"
        if self.kind:
            print(f"[camera] Using {self.kind} camera")
        else:
            print(f"[camera] Disabled: {self.error}")

    def _open_usb(self):
        capture = cv2.VideoCapture(f"/dev/video{self.cfg.camera_index}", cv2.CAP_V4L2)
        if not capture.isOpened():
            capture.release()
            return None
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.cfg.camera_width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cfg.camera_height)
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # keep frames fresh
        for _ in range(3):  # discard warm-up frames (auto exposure)
            capture.read()
        return capture

    def _release_usb(self):
        if self._usb is not None:
            try:
                self._usb.release()
            except Exception:
                pass
            self._usb = None

    @property
    def ready(self):
        return self.kind is not None

    def _capture_usb(self):
        for _ in range(2):  # one reopen attempt after a failure
            if self._usb is None:
                self._usb = self._open_usb()
                if self._usb is None:
                    raise RuntimeError(f"USB camera /dev/video{self.cfg.camera_index} cannot be opened (unplugged?)")
            self._usb.grab()  # drop the buffered (possibly old) frame
            ok, frame = self._usb.read()
            if ok and frame is not None:
                return frame
            self._release_usb()
        raise RuntimeError("USB camera returned no frame.")

    def capture(self):
        if not self.ready:
            raise RuntimeError(f"No camera available: {self.error}")
        with self._lock:
            try:
                if self.kind == "picamera":
                    # Picamera2's "RGB888" buffer is laid out B,G,R - already OpenCV order.
                    frame = self._picam.capture_array()
                else:
                    frame = self._capture_usb()
                frame = validate_frame(frame)
            except Exception as error:
                self.failures += 1
                if self.failures in (1, 10) or self.failures % 100 == 0:
                    print(f"[camera] Capture failed ({self.failures}x): {error}")
                raise RuntimeError(f"Camera capture failed: {error}") from error
            if self.failures:
                print(f"[camera] Capturing again after {self.failures} failure(s)")
                self.failures = 0
            return frame

    def close(self):
        with self._lock:
            self._release_usb()
            if self._picam:
                for method in ("stop", "close"):
                    try:
                        getattr(self._picam, method)()
                    except Exception:
                        pass
                self._picam = None
