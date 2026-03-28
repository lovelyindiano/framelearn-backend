"""
FrameLearn Backend — app.py
Phase 1: Frame extraction from YouTube Shorts and Instagram Reels
"""

import os
import sys
import glob
import uuid
import subprocess
import logging
from flask import Flask, request, jsonify
from flask_cors import CORS
from supabase import create_client, Client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
BUCKET_NAME  = os.environ.get("SUPABASE_BUCKET", "frames")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Write YouTube cookies from environment variable to a temp file
COOKIES_PATH = "/tmp/yt-cookies.txt"
yt_cookies = os.environ.get("YT_COOKIES", "")
if yt_cookies:
    with open(COOKIES_PATH, "w") as f:
        f.write(yt_cookies)
    log.info("YouTube cookies written to disk")

def is_supported_url(url: str) -> bool:
    supported = [
        "youtube.com/shorts/",
        "youtu.be/",
        "instagram.com/reel/",
        "instagram.com/reels/",
    ]
    return any(pattern in url for pattern in supported)


def download_video(url: str, output_path: str) -> bool:
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "-o", output_path,
        "--merge-output-format", "mp4",
        "--max-filesize", "50m",
        "--no-playlist",
        "--cookies", COOKIES_PATH,
        url,
    ]
    log.info(f"Downloading video: {url}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        log.error(f"yt-dlp stdout: {result.stdout}")
        log.error(f"yt-dlp stderr: {result.stderr}")
        log.error(f"yt-dlp returncode: {result.returncode}")
        return False
    log.info("Video downloaded successfully")
    return True


def extract_frames(video_path: str, output_dir: str, fps: float = 0.5) -> list:
    os.makedirs(output_dir, exist_ok=True)
    pattern = os.path.join(output_dir, "frame_%03d.jpg")
    cmd = [
        "ffmpeg",
        "-i", video_path,
        "-vf", f"fps={fps}",
        "-q:v", "2",
        "-vframes", "40",
        pattern,
        "-y",
        "-loglevel", "error",
    ]
    log.info(f"Extracting frames at {fps} fps")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        log.error(f"FFmpeg error: {result.stderr}")
        return []
    frames = sorted(glob.glob(os.path.join(output_dir, "frame_*.jpg")))
    log.info(f"Extracted {len(frames)} frames")
    return frames


def upload_frames(frame_paths: list, job_id: str) -> list:
    public_urls = []
    for path in frame_paths:
        filename  = os.path.basename(path)
        dest_path = f"{job_id}/{filename}"
        with open(path, "rb") as f:
            image_bytes = f.read()
        try:
            supabase.storage.from_(BUCKET_NAME).upload(
                dest_path,
                image_bytes,
                {"content-type": "image/jpeg"},
            )
            url = supabase.storage.from_(BUCKET_NAME).get_public_url(dest_path)
            public_urls.append(url)
            log.info(f"Uploaded: {dest_path}")
        except Exception as e:
            log.error(f"Upload failed for {filename}: {e}")
    return public_urls


def cleanup(paths: list) -> None:
    for p in paths:
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "framelearn-backend"})


@app.route("/extract", methods=["POST"])
def extract():
    data = request.get_json(silent=True)
    if not data or "url" not in data:
        return jsonify({"error": "Missing 'url' in request body"}), 400

    url = data["url"].strip()

    if not is_supported_url(url):
        return jsonify({"error": "Only YouTube Shorts and Instagram Reels are supported."}), 400

    job_id     = str(uuid.uuid4())[:8]
    video_path = f"/tmp/{job_id}_video.mp4"
    frame_dir  = f"/tmp/{job_id}_frames"

    log.info(f"Job {job_id} started for URL: {url}")

    try:
        if not download_video(url, video_path):
            return jsonify({"error": "Could not download video. The link may be private or unavailable."}), 422

        frame_paths = extract_frames(video_path, frame_dir)
        if not frame_paths:
            return jsonify({"error": "No frames could be extracted from this video."}), 422

        public_urls = upload_frames(frame_paths, job_id)
        if not public_urls:
            return jsonify({"error": "Frame upload failed. Please try again."}), 500

        log.info(f"Job {job_id} complete: {len(public_urls)} frames")
        return jsonify({
            "frames": public_urls,
            "count":  len(public_urls),
            "job_id": job_id,
        })

    finally:
        cleanup(glob.glob(f"/tmp/{job_id}*"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
