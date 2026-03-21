"""
Configuration for vehicle detection, tracking, counting, speed estimation,
and red light / stop line violation detection.
All features can be enabled or disabled via the flags below.
"""

# ---------------------------------------------------------------------------
# FEATURE FLAGS (set True/False to turn features on/off)
# ---------------------------------------------------------------------------
ENABLE_VEHICLE_DETECTION = True       # YOLOv8 vehicle detection
ENABLE_LICENSE_PLATE_RECOGNITION = False  # EasyOCR on cropped plates (slower)
ENABLE_SORT_TRACKING = True          # SORT multi-object tracking
ENABLE_LANE_ROI = True               # Restrict detection to lane polygon (ignore bus stops, pavements)
ENABLE_LANE_COUNTING = True          # Count vehicles crossing lane count line
ENABLE_SPEED_ESTIMATION = True       # Frame-to-frame speed calculation
ENABLE_STOP_LINE_DETECTION = True    # Virtual stop line crossing detection
ENABLE_RED_LIGHT_VIOLATION = True    # Flag vehicle if it crosses stop line (violation logic)

# ---------------------------------------------------------------------------
# INPUT / OUTPUT
# ---------------------------------------------------------------------------
# Video source: path to file, or 0 for webcam, or RTSP URL (relative to project root if needed)
VIDEO_SOURCE = "videos/video-3.mp4"

# YOLOv8 model: "yolov8n.pt" (nano), "yolov8s.pt", "yolov8m.pt", "yolov8l.pt", "yolov8x.pt"
# Nano is fastest; larger models are slower but more accurate.
YOLO_MODEL = "yolov8n.pt"

# Run detection every N frames (1 = every frame; 2 or 3 = much faster, SORT fills gaps)
# Main speed knob: higher = faster, slightly less precise on fast-moving vehicles.
DETECT_EVERY_N_FRAMES = 1

# COCO class IDs for vehicles (car, truck, bus, motorcycle, bicycle, etc.)
VEHICLE_CLASS_IDS = [1, 2, 3, 5, 7]  # 1=bicycle, 2=car, 3=motorcycle, 5=bus, 7=truck

# Detection confidence threshold (0.0 - 1.0)
CONFIDENCE_THRESHOLD = 0.5

# ---------------------------------------------------------------------------
# LANE ROI (Detection runs only inside this region)
# Points define a polygon. Objects outside are ignored (e.g. bus stops, pavements).
# Format: list of (x, y) in image coordinates. Order: e.g. top-left, top-right, bottom-right, bottom-left.
# Set ENABLE_LANE_ROI = False to use full frame.
LANE_ROI_POINTS = [
    (1121, 1034),
    (1903, 923),
    (1248, 480),
    (1141, 430),
    (959, 454),
]

# ---------------------------------------------------------------------------
# LANE COUNTING
# Count line: vehicles crossing this line are counted.
# Defined as two points (x1,y1) -> (x2,y2). Direction of crossing can be used (e.g. left-to-right).
LANE_COUNT_LINE = [
    (1022, 682),
    (1483, 639),
]
# Count direction: "both", "up", "down", "left", "right" (relative to line orientation)
LANE_COUNT_DIRECTION = "down"

# ---------------------------------------------------------------------------
# SPEED ESTIMATION
# Pixels per meter at the count line / ROI (calibrate for your camera angle)
PIXELS_PER_METER = 10.0

# Video FPS (if 0, will try to read from video)
VIDEO_FPS = 30.0

# Speed smoothing: number of frames to average for speed display
SPEED_SMOOTHING_FRAMES = 5

# ---------------------------------------------------------------------------
# VIRTUAL STOP LINE & RED LIGHT VIOLATION
# Stop line: two points (x1,y1), (x2,y2). Vehicles crossing this line may be flagged.
STOP_LINE = [
    (1085, 906),
    (1740, 817),
]

# Red light state: set True when the light is red, False when green.
# Vehicles crossing the stop line while this is True are flagged as violations.
RED_LIGHT_IS_ON = True

# Option: treat every stop line crossing as violation (ignore light state)
TREAT_ALL_STOP_LINE_CROSSINGS_AS_VIOLATION = False

# ---------------------------------------------------------------------------
# LICENSE PLATE (EasyOCR)
# Only used when ENABLE_LICENSE_PLATE_RECOGNITION is True.
LICENSE_PLATE_LANGUAGES = ["en"]
# Expand detection box by this factor to crop plate region (e.g. 1.2 = 20% margin)
LICENSE_PLATE_CROP_MARGIN = 1.2

# ---------------------------------------------------------------------------
# DISPLAY
# Draw ROI, count line, stop line, IDs, speeds, violations on output
DRAW_ROI = True
DRAW_COUNT_LINE = True
DRAW_STOP_LINE = True
DRAW_TRACK_IDS = True
DRAW_SPEED = True
DRAW_LICENSE_PLATE = True   # Show license plate text on bounding box
DRAW_VIOLATIONS = True
# Output video path (None = display only, no save)
OUTPUT_VIDEO_PATH = None  # e.g. "output.mp4"

