"""
Benchmark detection models against real BinBrain images.

Usage:
    # Run all registered models against all photos in data/photos/
    uv run python benchmark_models.py

    # Run specific model only
    uv run python benchmark_models.py --model yolo11s.pt

    # Run against specific directory
    uv run python benchmark_models.py --images data/photos/test-detect

    # Compare results from two runs
    uv run python benchmark_models.py --compare

    # List all stored runs
    uv run python benchmark_models.py --list
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
RESULTS_DIR = BASE_DIR / "data" / "benchmark_results"
SUPPORTED_EXT = {".jpg", ".jpeg", ".png", ".webp"}

# Classes for text-prompt models (matches benchmark image content)
BENCHMARK_CLASSES = [
    "battery", "bottle", "calculator", "coffee mug", "flashlight",
    "glasses", "glue stick", "hammer", "headphones", "keys",
    "light bulb", "marker", "mouse", "pliers", "ruler",
    "scissors", "screwdriver", "tape measure", "tape roll",
    "usb cable", "wrench", "bolt", "capacitor", "book",
]

# Weights live in ./models/ (shared with the API container via a bind-mount in
# docker-compose.yml — see Finding #3).
MODELS_DIR = "models"

MODEL_REGISTRY = {
    "yolo11s.pt": {"type": "YOLO", "dataset": "COCO", "classes": 80},
    "yoloe-v8s-seg.pt": {"type": "YOLOE", "dataset": "LVIS+Objects365", "classes": "text-prompt"},
    "yoloe-v8s-seg-pf.pt": {"type": "YOLOE", "dataset": "LVIS+Objects365", "classes": 4585},
    "yolov8s-worldv2.pt": {"type": "YOLOWorld", "dataset": "text-prompt", "classes": "text-prompt"},
    "yolo11n_object365.pt": {"type": "YOLO", "dataset": "Objects365", "classes": 365},
}


def find_images(image_dir: str) -> list[Path]:
    """Find all valid image files recursively, skipping benchmark dir."""
    root = BASE_DIR / image_dir
    if not root.exists():
        print(f"ERROR: {root} does not exist")
        sys.exit(1)
    images = []
    for p in sorted(root.rglob("*")):
        if p.suffix.lower() in SUPPORTED_EXT and "benchmark" not in str(p):
            images.append(p)
    return images


def load_model(model_key: str):
    """Load a detection model by registry key."""
    info = MODEL_REGISTRY[model_key]
    model_path = str(BASE_DIR / MODELS_DIR / info.get("path", model_key))

    if info["type"] == "YOLOE":
        from ultralytics import YOLOE
        return YOLOE(model_path)
    elif info["type"] == "YOLOWorld":
        from ultralytics import YOLOWorld
        return YOLOWorld(model_path)
    else:
        from ultralytics import YOLO
        return YOLO(model_path)


def detect_image(model, path: Path, device: str = "mps") -> dict:
    """Run detection on a single image, return structured results."""
    t0 = time.time()
    try:
        results = model.predict(source=str(path), device=device, conf=0.10, iou=0.45, verbose=False)
    except Exception as e:
        return {"error": str(e), "elapsed_s": round(time.time() - t0, 3), "detections": []}

    elapsed = time.time() - t0
    detections = []
    for r in results:
        if r.boxes is None:
            continue
        for i in range(len(r.boxes)):
            cls_id = int(r.boxes.cls[i])
            label = model.names[cls_id]
            conf = float(r.boxes.conf[i])
            x1, y1, x2, y2 = r.boxes.xyxy[i].tolist()
            detections.append({
                "label": label,
                "confidence": round(conf, 4),
                "bbox": [round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)],
            })

    detections.sort(key=lambda d: -d["confidence"])
    return {"elapsed_s": round(elapsed, 3), "detections": detections}


def get_device() -> str:
    import platform
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        return "mps"
    return "cpu"


def run_benchmark(model_keys: list[str], image_dir: str) -> dict:
    """Run all specified models against all images, return full results."""
    images = find_images(image_dir)
    if not images:
        print(f"No images found in {image_dir}")
        sys.exit(1)

    print(f"Found {len(images)} images in {image_dir}")
    device = get_device()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    run_data = {
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "device": device,
        "image_dir": image_dir,
        "image_count": len(images),
        "models": {},
    }

    for model_key in model_keys:
        if model_key not in MODEL_REGISTRY:
            print(f"SKIP unknown model: {model_key}")
            continue

        info = MODEL_REGISTRY[model_key]
        print(f"\n--- {model_key} ({info['dataset']}, {info['classes']} classes) ---")

        model = load_model(model_key)
        if info.get("classes") == "text-prompt":
            print(f"  Setting {len(BENCHMARK_CLASSES)} text-prompt classes")
            model.set_classes(BENCHMARK_CLASSES)
        model_results = {
            "info": info,
            "images": {},
            "summary": {"total": 0, "detected": 0, "errors": 0, "avg_elapsed_s": 0},
        }

        elapsed_total = 0
        for img_path in images:
            rel_path = str(img_path.relative_to(BASE_DIR))
            result = detect_image(model, img_path, device)
            model_results["images"][rel_path] = result

            model_results["summary"]["total"] += 1
            elapsed_total += result.get("elapsed_s", 0)

            if result.get("error"):
                model_results["summary"]["errors"] += 1
                top = f"ERROR: {result['error'][:50]}"
            elif result["detections"]:
                model_results["summary"]["detected"] += 1
                top3 = ", ".join(
                    f"{d['label']}({d['confidence']:.2f})"
                    for d in result["detections"][:3]
                )
                top = top3
            else:
                top = "[empty]"

            print(f"  {rel_path:<60} {result.get('elapsed_s', 0):.2f}s  {top}")

        if model_results["summary"]["total"] > 0:
            model_results["summary"]["avg_elapsed_s"] = round(
                elapsed_total / model_results["summary"]["total"], 3
            )

        run_data["models"][model_key] = model_results
        s = model_results["summary"]
        print(f"  Summary: {s['detected']}/{s['total']} detected, {s['errors']} errors, avg {s['avg_elapsed_s']}s")

    # Save results
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / f"run_{run_id}.json"
    with open(out_path, "w") as f:
        json.dump(run_data, f, indent=2)
    print(f"\nResults saved to {out_path}")
    return run_data


def list_runs():
    """List all stored benchmark runs."""
    if not RESULTS_DIR.exists():
        print("No benchmark results found.")
        return

    runs = sorted(RESULTS_DIR.glob("run_*.json"))
    if not runs:
        print("No benchmark results found.")
        return

    print(f"{'Run ID':<20} {'Date':<25} {'Images':<8} {'Models':<40}")
    print("-" * 95)
    for run_file in runs:
        with open(run_file) as f:
            data = json.load(f)
        models = ", ".join(data.get("models", {}).keys())
        print(f"{data['run_id']:<20} {data['timestamp'][:19]:<25} {data['image_count']:<8} {models:<40}")


def compare_runs():
    """Compare the two most recent runs side by side."""
    if not RESULTS_DIR.exists():
        print("No benchmark results found.")
        return

    runs = sorted(RESULTS_DIR.glob("run_*.json"))
    if len(runs) < 2:
        print("Need at least 2 runs to compare.")
        return

    with open(runs[-2]) as f:
        old = json.load(f)
    with open(runs[-1]) as f:
        new = json.load(f)

    print(f"Comparing: {old['run_id']} vs {new['run_id']}")
    print()

    # Find common models
    common_models = set(old["models"].keys()) & set(new["models"].keys())
    if not common_models:
        print("No common models between runs.")
        return

    for model_key in sorted(common_models):
        old_s = old["models"][model_key]["summary"]
        new_s = new["models"][model_key]["summary"]
        print(f"--- {model_key} ---")
        print(f"  Detection rate: {old_s['detected']}/{old_s['total']} -> {new_s['detected']}/{new_s['total']}")
        print(f"  Avg speed:      {old_s['avg_elapsed_s']}s -> {new_s['avg_elapsed_s']}s")
        print(f"  Errors:         {old_s['errors']} -> {new_s['errors']}")

        # Per-image comparison for changed detections
        old_imgs = old["models"][model_key].get("images", {})
        new_imgs = new["models"][model_key].get("images", {})
        common_imgs = set(old_imgs.keys()) & set(new_imgs.keys())

        changes = []
        for img in sorted(common_imgs):
            old_top = old_imgs[img]["detections"][0]["label"] if old_imgs[img]["detections"] else "[empty]"
            new_top = new_imgs[img]["detections"][0]["label"] if new_imgs[img]["detections"] else "[empty]"
            if old_top != new_top:
                changes.append((img, old_top, new_top))

        if changes:
            print(f"  Changed detections ({len(changes)}):")
            for img, old_top, new_top in changes:
                print(f"    {img}: {old_top} -> {new_top}")
        print()


def main():
    parser = argparse.ArgumentParser(description="Benchmark detection models")
    parser.add_argument("--model", help="Run specific model only (e.g. yolo11s.pt)")
    parser.add_argument("--images", default="data/photos", help="Image directory (default: data/photos)")
    parser.add_argument("--list", action="store_true", help="List stored runs")
    parser.add_argument("--compare", action="store_true", help="Compare two most recent runs")
    args = parser.parse_args()

    if args.list:
        list_runs()
        return

    if args.compare:
        compare_runs()
        return

    if args.model:
        model_keys = [args.model]
    else:
        model_keys = list(MODEL_REGISTRY.keys())

    run_benchmark(model_keys, args.images)


if __name__ == "__main__":
    main()
