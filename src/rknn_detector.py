#!/usr/bin/env python3
"""
YOLOv8 detector that runs on the Rockchip 6 TOPS NPU (Orange Pi 5 / RK3588).

Load a converted ``.rknn`` (or ``.rnn``) file with RKNN-Toolkit-Lite2, or fall
back to Ultralytics for ``.pt`` / ``.onnx``. The returned object is callable like
Ultralytics YOLO so existing call sites keep working:

    model = load_detector("models/yolov8n.rknn")
    result = model(frame, verbose=False)[0]
    # result.boxes.xyxy / .conf / .cls  — same as ultralytics

On the Orange Pi, install the Lite runtime (not needed on a Mac/PC):

    pip3 install rknn-toolkit-lite2
"""

from __future__ import annotations

import atexit
import logging
from pathlib import Path
from typing import Iterable, Sequence

import cv2
import numpy as np

logger = logging.getLogger("rknn_detector")

RKNN_EXTS = {".rknn", ".rnn"}
_NPU_CORE_ALIASES = {
    "0": "NPU_CORE_0",
    "1": "NPU_CORE_1",
    "2": "NPU_CORE_2",
    "0_1": "NPU_CORE_0_1",
    "0_1_2": "NPU_CORE_0_1_2",
    "auto": "NPU_CORE_AUTO",
}


def is_rknn_path(path) -> bool:
    return Path(str(path)).suffix.lower() in RKNN_EXTS


def resolve_model_path(path) -> str:
    """Prefer a sibling .rknn/.rnn when the Lite runtime is available."""
    p = Path(str(path))
    if is_rknn_path(p):
        return str(p)
    if _rknn_lite_available():
        for ext in (".rknn", ".rnn"):
            cand = p.with_suffix(ext)
            if cand.is_file():
                logger.info("Using NPU model %s (sibling of %s)", cand, p)
                return str(cand)
    return str(p)


def load_detector(path, imgsz=640, conf=0.25, iou=0.45, npu_cores="0_1_2", num_classes=None):
    """Load a YOLO detector: RKNN on the NPU, otherwise Ultralytics on CPU/GPU."""
    resolved = resolve_model_path(path)
    if is_rknn_path(resolved):
        return RKNNDetector(
            resolved,
            imgsz=imgsz,
            conf=conf,
            iou=iou,
            npu_cores=npu_cores,
            num_classes=num_classes,
        )
    try:
        from ultralytics import YOLO
    except ImportError as exc:
        raise SystemExit(
            f"Cannot load {resolved}: ultralytics is not installed "
            "(pip install ultralytics), and this is not an RKNN model."
        ) from exc
    logger.info("Loading Ultralytics model %s", resolved)
    return YOLO(resolved)


# ---------------------------------------------------------------------------
# Tensor-like wrappers so existing YOLO call sites keep working
# ---------------------------------------------------------------------------

class _TensorLike:
    """Minimal stand-in for the torch tensors the detectors already unwrap."""

    def __init__(self, data):
        self._data = np.asarray(data)

    def __len__(self):
        return int(self._data.shape[0]) if self._data.ndim else 1

    def __getitem__(self, idx):
        return _TensorLike(self._data[idx])

    def item(self):
        return self._data.item()

    def cpu(self):
        return self

    def numpy(self):
        return np.asarray(self._data)


class _Boxes:
    def __init__(self, xyxy, conf, cls):
        self.xyxy = _TensorLike(xyxy)
        self.conf = _TensorLike(conf)
        self.cls = _TensorLike(cls)

    def __len__(self):
        return len(self.xyxy)


class _Result:
    def __init__(self, boxes: _Boxes):
        self.boxes = boxes


# ---------------------------------------------------------------------------
# Geometry / NMS
# ---------------------------------------------------------------------------

def _letterbox(im, new_shape, color=(114, 114, 114)):
    """Resize + pad to a square, matching Ultralytics letterbox."""
    if isinstance(new_shape, int):
        new_shape = (new_shape, new_shape)
    new_h, new_w = int(new_shape[0]), int(new_shape[1])
    h, w = im.shape[:2]
    r = min(new_h / h, new_w / w)
    nw, nh = int(round(w * r)), int(round(h * r))
    resized = cv2.resize(im, (nw, nh), interpolation=cv2.INTER_LINEAR)
    dw, dh = new_w - nw, new_h - nh
    top, bottom = int(round(dh / 2 - 0.1)), int(round(dh / 2 + 0.1))
    left, right = int(round(dw / 2 - 0.1)), int(round(dw / 2 + 0.1))
    out = cv2.copyMakeBorder(
        resized, top, bottom, left, right, cv2.BORDER_CONSTANT, value=color
    )
    return out, r, left, top


def _scale_boxes(xyxy, ratio, pad_x, pad_y, orig_w, orig_h):
    xyxy = xyxy.copy()
    xyxy[:, [0, 2]] = (xyxy[:, [0, 2]] - pad_x) / ratio
    xyxy[:, [1, 3]] = (xyxy[:, [1, 3]] - pad_y) / ratio
    xyxy[:, [0, 2]] = xyxy[:, [0, 2]].clip(0, orig_w)
    xyxy[:, [1, 3]] = xyxy[:, [1, 3]].clip(0, orig_h)
    return xyxy


def _xywh_to_xyxy(xywh):
    out = np.empty_like(xywh)
    out[:, 0] = xywh[:, 0] - xywh[:, 2] / 2.0
    out[:, 1] = xywh[:, 1] - xywh[:, 3] / 2.0
    out[:, 2] = xywh[:, 0] + xywh[:, 2] / 2.0
    out[:, 3] = xywh[:, 1] + xywh[:, 3] / 2.0
    return out


def _nms(boxes, scores, iou_thr):
    """Class-agnostic NMS. boxes = xyxy."""
    if len(boxes) == 0:
        return np.zeros((0,), dtype=np.int32)
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = int(order[0])
        keep.append(i)
        if order.size == 1:
            break
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        inter = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
        iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-9)
        order = order[1:][iou <= iou_thr]
    return np.asarray(keep, dtype=np.int32)


def _nms_per_class(boxes, scores, classes, iou_thr):
    keep = []
    for c in np.unique(classes):
        idx = np.where(classes == c)[0]
        local = _nms(boxes[idx], scores[idx], iou_thr)
        keep.extend(idx[local].tolist())
    return np.asarray(keep, dtype=np.int32)


def _dfl(position):
    """Distribution Focal Loss decode — numpy, no torch needed on the Pi."""
    n, c, h, w = position.shape
    mc = c // 4
    y = position.reshape(n, 4, mc, h, w)
    y = y - y.max(axis=2, keepdims=True)
    y = np.exp(y)
    y = y / (y.sum(axis=2, keepdims=True) + 1e-9)
    acc = np.arange(mc, dtype=np.float32).reshape(1, 1, mc, 1, 1)
    return (y * acc).sum(axis=2)


def _to_nchw(t):
    t = np.asarray(t)
    if t.ndim != 4:
        return t

    def _spatial(a, b):
        return a >= 8 and b >= 8 and abs(a - b) <= max(a, b) * 0.3

    def _channels(c):
        return 1 <= c <= 256

    # Prefer NCHW when dim1 looks like channels and the last two are a square grid.
    if _channels(t.shape[1]) and _spatial(t.shape[2], t.shape[3]):
        return t
    if _channels(t.shape[3]) and _spatial(t.shape[1], t.shape[2]):
        return np.transpose(t, (0, 3, 1, 2))
    return t


def _box_process(position, img_w, img_h):
    position = _to_nchw(position).astype(np.float32)
    grid_h, grid_w = position.shape[2], position.shape[3]
    col, row = np.meshgrid(np.arange(grid_w), np.arange(grid_h))
    grid = np.stack((col, row), axis=0).reshape(1, 2, grid_h, grid_w).astype(np.float32)
    stride = np.array([img_w / grid_w, img_h / grid_h], dtype=np.float32).reshape(1, 2, 1, 1)
    decoded = _dfl(position)
    box_xy = grid + 0.5 - decoded[:, 0:2, :, :]
    box_xy2 = grid + 0.5 + decoded[:, 2:4, :, :]
    return np.concatenate((box_xy * stride, box_xy2 * stride), axis=1)


def _flatten_nchw(t):
    t = _to_nchw(t)
    ch = t.shape[1]
    return t.transpose(0, 2, 3, 1).reshape(-1, ch)


# ---------------------------------------------------------------------------
# RKNN runtime
# ---------------------------------------------------------------------------

def _rknn_lite_available() -> bool:
    try:
        from rknnlite.api import RKNNLite  # noqa: F401
        return True
    except ImportError:
        return False


def _core_mask(rknn_lite_cls, cores: str):
    name = _NPU_CORE_ALIASES.get(str(cores).strip().lower(), "NPU_CORE_0_1_2")
    return getattr(rknn_lite_cls, name, None)


class RKNNDetector:
    """YOLOv8 inference on the RK3588 NPU via rknn-toolkit-lite2."""

    def __init__(
        self,
        model_path,
        imgsz=640,
        conf=0.25,
        iou=0.45,
        npu_cores="0_1_2",
        num_classes=None,
    ):
        try:
            from rknnlite.api import RKNNLite
        except ImportError as exc:
            raise SystemExit(
                "rknn-toolkit-lite2 is not installed. On the Orange Pi run:\n"
                "  pip3 install rknn-toolkit-lite2\n"
                "or install the aarch64 wheel from "
                "https://github.com/airockchip/rknn-toolkit2"
            ) from exc

        path = Path(model_path)
        if not path.is_file():
            raise SystemExit(f"RKNN model not found: {path}")

        self.model_path = str(path)
        self.imgsz = int(imgsz)
        self.conf = float(conf)
        self.iou = float(iou)
        self.num_classes = num_classes
        self._lite_cls = RKNNLite
        self.rknn = RKNNLite()

        logger.info("Loading RKNN model %s", path)
        ret = self.rknn.load_rknn(self.model_path)
        if ret != 0:
            raise SystemExit(f"RKNN load_rknn failed ({ret}): {path}")

        mask = _core_mask(RKNNLite, npu_cores)
        try:
            if mask is not None:
                ret = self.rknn.init_runtime(core_mask=mask)
            else:
                ret = self.rknn.init_runtime()
        except TypeError:
            ret = self.rknn.init_runtime()
        if ret != 0:
            raise SystemExit(f"RKNN init_runtime failed ({ret}). Is the NPU driver loaded?")

        logger.info("RKNN runtime ready on NPU cores=%s imgsz=%d", npu_cores, self.imgsz)
        atexit.register(self.release)

    def release(self):
        rknn = getattr(self, "rknn", None)
        if rknn is not None:
            try:
                rknn.release()
            except Exception:
                pass
            self.rknn = None

    def __call__(self, source, verbose=False, conf=None, iou=None, **_kwargs):
        if not isinstance(source, np.ndarray):
            raise TypeError("RKNNDetector expects a BGR numpy image (OpenCV frame).")
        return [self._infer(source, conf=conf, iou=iou)]

    def predict(self, source, **kwargs):
        return self(source, **kwargs)

    def _infer(self, frame_bgr, conf=None, iou=None):
        conf_thr = self.conf if conf is None else float(conf)
        iou_thr = self.iou if iou is None else float(iou)
        orig_h, orig_w = frame_bgr.shape[:2]

        img, ratio, pad_x, pad_y = _letterbox(frame_bgr, self.imgsz)
        img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        outputs = self.rknn.inference(inputs=[img_rgb])
        if not outputs:
            return _Result(_Boxes(np.zeros((0, 4)), np.zeros((0,)), np.zeros((0,))))

        boxes, classes, scores = self._postprocess(outputs, conf_thr, iou_thr)
        if boxes is None or len(boxes) == 0:
            return _Result(_Boxes(np.zeros((0, 4)), np.zeros((0,)), np.zeros((0,))))

        boxes = _scale_boxes(boxes.astype(np.float32), ratio, pad_x, pad_y, orig_w, orig_h)
        return _Result(_Boxes(boxes, scores.astype(np.float32), classes.astype(np.float32)))

    def _postprocess(self, outputs, conf_thr, iou_thr):
        tensors = [_as_numpy(o) for o in outputs]
        if _is_fused_output(tensors):
            return _decode_fused(tensors[0], conf_thr, iou_thr, self.num_classes)
        return _decode_heads(tensors, conf_thr, iou_thr, self.imgsz, self.imgsz)


def _as_numpy(t):
    if hasattr(t, "numpy"):
        try:
            return t.numpy()
        except Exception:
            pass
    return np.asarray(t)


def _is_fused_output(tensors: Sequence[np.ndarray]) -> bool:
    """Ultralytics ONNX export: one tensor (1, 4+nc, anchors) after DFL."""
    if len(tensors) != 1:
        return False
    t = np.squeeze(tensors[0])
    if t.ndim == 2:
        return True
    if t.ndim == 3 and min(t.shape[1], t.shape[2]) <= 256:
        return True
    return False


def _decode_fused(pred, conf_thr, iou_thr, num_classes):
    pred = np.squeeze(pred).astype(np.float32)
    if pred.ndim == 3:
        # (C, H, W) → (C, N)
        pred = pred.reshape(pred.shape[0], -1)
    if pred.ndim != 2:
        logger.warning("Unexpected fused RKNN output shape %s", pred.shape)
        return None, None, None
    # Prefer (C, N) where C is 4+nc (5 / 84 / …) and N is thousands of anchors.
    if pred.shape[0] < pred.shape[1]:
        pred = pred.T
    # pred: (N, 4+nc)
    xywh = pred[:, :4]
    cls_scores = pred[:, 4:]
    if num_classes is not None and cls_scores.shape[1] > num_classes:
        cls_scores = cls_scores[:, :num_classes]
    classes = cls_scores.argmax(axis=1)
    scores = cls_scores.max(axis=1)
    mask = scores >= conf_thr
    if not np.any(mask):
        return None, None, None
    boxes = _xywh_to_xyxy(xywh[mask])
    classes = classes[mask]
    scores = scores[mask]
    keep = _nms_per_class(boxes, scores, classes, iou_thr)
    return boxes[keep], classes[keep], scores[keep]


def _decode_heads(outputs, conf_thr, iou_thr, img_w, img_h):
    """Rockchip model-zoo style: 3 or 6 raw heads (DFL boxes + class maps)."""
    outputs = [_to_nchw(o).astype(np.float32) for o in outputs]
    boxes_l, cls_l = _split_heads(outputs)
    if not boxes_l:
        logger.warning("Could not parse RKNN head outputs (%d tensors)", len(outputs))
        return None, None, None

    boxes = np.concatenate([_flatten_nchw(_box_process(b, img_w, img_h)) for b in boxes_l], axis=0)
    cls_scores = np.concatenate([_flatten_nchw(c) for c in cls_l], axis=0)
    classes = cls_scores.argmax(axis=1)
    scores = cls_scores.max(axis=1)
    mask = scores >= conf_thr
    if not np.any(mask):
        return None, None, None
    boxes, classes, scores = boxes[mask], classes[mask], scores[mask]
    keep = _nms_per_class(boxes, scores, classes, iou_thr)
    return boxes[keep], classes[keep], scores[keep]


def _split_heads(outputs: Iterable[np.ndarray]):
    outputs = list(outputs)
    n = len(outputs)
    if n == 6:
        # [box0, cls0, box1, cls1, box2, cls2]
        return [outputs[0], outputs[2], outputs[4]], [outputs[1], outputs[3], outputs[5]]
    if n == 3:
        # Either 3 fused (64+nc) DFL heads, or already-decoded (4+nc) maps.
        boxes, clses = [], []
        for t in outputs:
            t = _to_nchw(t)
            ch = t.shape[1]
            if ch >= 64 and (ch - 64) >= 1:
                boxes.append(t[:, :64])
                clses.append(t[:, 64:])
            elif ch > 4:
                # Already-decoded xyxy/xywh + classes on a grid — treat as fused later.
                return [], []
            else:
                return [], []
        return boxes, clses
    return [], []
