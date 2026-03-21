"""
SORT: A Simple, Online and Realtime Tracker.
Uses Kalman filter for state estimation and IoU for association.
"""

from __future__ import annotations

import numpy as np
from filterpy.kalman import KalmanFilter
from scipy.optimize import linear_sum_assignment


def iou_batch(boxes_a: np.ndarray, boxes_b: np.ndarray) -> np.ndarray:
    """Compute IoU between two sets of boxes. boxes format: (N, 4) as (x1, y1, x2, y2)."""
    if boxes_a.size == 0 or boxes_b.size == 0:
        return np.zeros((len(boxes_a), len(boxes_b)))
    x1 = np.maximum(boxes_a[:, None, 0], boxes_b[None, :, 0])
    y1 = np.maximum(boxes_a[:, None, 1], boxes_b[None, :, 1])
    x2 = np.minimum(boxes_a[:, None, 2], boxes_b[None, :, 2])
    y2 = np.minimum(boxes_a[:, None, 3], boxes_b[None, :, 3])
    inter = np.maximum(0, x2 - x1) * np.maximum(0, y2 - y1)
    area_a = (boxes_a[:, 2] - boxes_a[:, 0]) * (boxes_a[:, 3] - boxes_a[:, 1])
    area_b = (boxes_b[:, 2] - boxes_b[:, 0]) * (boxes_b[:, 3] - boxes_b[:, 1])
    union = area_a[:, None] + area_b[None, :] - inter
    return inter / np.maximum(union, 1e-6)


class KalmanBoxTracker:
    """Tracks a single bbox with Kalman filter. State: [x, y, s, r, vx, vy, vs] (s=scale, r=aspect ratio)."""

    count = 0

    def __init__(self, bbox: np.ndarray):
        # bbox: (x1, y1, x2, y2)
        self.kf = KalmanFilter(dim_x=7, dim_z=4)
        self.kf.F = np.array([
            [1, 0, 0, 0, 1, 0, 0],
            [0, 1, 0, 0, 0, 1, 0],
            [0, 0, 1, 0, 0, 0, 1],
            [0, 0, 0, 1, 0, 0, 0],
            [0, 0, 0, 0, 1, 0, 0],
            [0, 0, 0, 0, 0, 1, 0],
            [0, 0, 0, 0, 0, 0, 1],
        ])
        self.kf.H = np.array([
            [1, 0, 0, 0, 0, 0, 0],
            [0, 1, 0, 0, 0, 0, 0],
            [0, 0, 1, 0, 0, 0, 0],
            [0, 0, 0, 1, 0, 0, 0],
        ])
        self.kf.R *= 10.0
        self.kf.P[4:, 4:] *= 1000.0
        self.kf.Q[4:, 4:] *= 0.01

        cx = (bbox[0] + bbox[2]) / 2.0
        cy = (bbox[1] + bbox[3]) / 2.0
        s = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
        r = (bbox[2] - bbox[0]) / np.maximum(bbox[3] - bbox[1], 1e-6)
        # FilterPy uses state x as column vector (dim_x, 1)
        self.kf.x = np.array([[cx], [cy], [s], [r], [0], [0], [0]], dtype=float)

        KalmanBoxTracker.count += 1
        self.id = KalmanBoxTracker.count
        self.time_since_update = 0
        self.history = []

    def update(self, bbox: np.ndarray):
        self.time_since_update = 0
        cx = (bbox[0] + bbox[2]) / 2.0
        cy = (bbox[1] + bbox[3]) / 2.0
        s = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
        r = (bbox[2] - bbox[0]) / np.maximum(bbox[3] - bbox[1], 1e-6)
        z = np.array([cx, cy, s, r])
        self.kf.update(z)

    def predict(self) -> np.ndarray:
        x = self.kf.x
        if x.flat[6] + x.flat[2] <= 0:
            self.kf.x.flat[6] = 0.0
        self.kf.predict()
        self.time_since_update += 1
        return self.get_state_bbox()

    def get_state_bbox(self) -> np.ndarray:
        x = self.kf.x.flat
        cx, cy, s, r = x[0], x[1], x[2], x[3]
        w = np.sqrt(s * r)
        h = np.sqrt(s / np.maximum(r, 1e-6))
        return np.array([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2])

    def get_velocity_xy(self) -> tuple:
        x = self.kf.x.flat
        return float(x[4]), float(x[5])


class Sort:
    """SORT tracker: maintains list of KalmanBoxTracker, associates detections by IoU."""

    def __init__(self, max_age: int = 3, min_hits: int = 3, iou_threshold: float = 0.3):
        self.max_age = max_age
        self.min_hits = min_hits
        self.iou_threshold = iou_threshold
        self.trackers: list = []

    def update(self, detections: np.ndarray) -> np.ndarray:
        """
        detections: (N, 5) array of (x1, y1, x2, y2, score) or (N, 4) (x1,y1,x2,y2).
        Returns: (M, 5) array of (x1, y1, x2, y2, track_id).
        """
        if detections is None or len(detections) == 0:
            detections = np.empty((0, 5))
        if detections.shape[1] == 4:
            detections = np.hstack([detections, np.ones((len(detections), 1))])

        # Predict
        predicted = np.array([t.predict() for t in self.trackers])
        if len(predicted) == 0:
            matched = np.array([]).reshape(0, 2)
            unmatched_det = np.arange(len(detections))
            unmatched_trk = np.array([])
        else:
            iou = iou_batch(detections[:, :4], predicted)
            iou[iou < self.iou_threshold] = 0
            cost = 1 - iou
            row_ind, col_ind = linear_sum_assignment(cost)
            matched_list = [[r, c] for r, c in zip(row_ind, col_ind) if cost[r, c] < 1 - self.iou_threshold]
            matched = np.array(matched_list).reshape(-1, 2) if matched_list else np.empty((0, 2))
            unmatched_det = np.setdiff1d(np.arange(len(detections)), matched[:, 0])
            unmatched_trk = np.setdiff1d(np.arange(len(self.trackers)), matched[:, 1])

        # Update matched
        for m in matched:
            self.trackers[m[1]].update(detections[m[0], :4])

        # New trackers for unmatched detections
        for i in unmatched_det:
            trk = KalmanBoxTracker(detections[i, :4])
            self.trackers.append(trk)

        # Remove dead trackers
        self.trackers = [t for t in self.trackers if t.time_since_update <= self.max_age]

        # Build output: only return tracks that were updated this frame (or predicted and still valid)
        ret = []
        for t in self.trackers:
            if t.time_since_update == 0:
                bbox = t.get_state_bbox()
                ret.append([*bbox, t.id])
        return np.array(ret) if ret else np.empty((0, 5))
