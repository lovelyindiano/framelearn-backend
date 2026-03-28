"""
FrameLearn Backend — app.py
Phase 1: Frame extraction from YouTube Shorts and Instagram Reels

Endpoint:
    POST /extract
    Body: { "url": "https://youtube.com/shorts/..." }
    Returns: { "frames": ["https://...", ...], "count": N, "job_id": "..." }

Dependencies: see requirements.txt
Environment variables: see .env.example
"""

import os
import glob
import uuid
import subprocess
import logging
from flask import Flask, request, jsonify
from flask_cors import CORS
from supabase import create_client, Client

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── App setup ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)  # Allow requests from Lovable frontend

# ── Supabase client ───────────────────────────────────────────────────────────
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
BUCKET_NAME  = os.environ.get("SUPABASE_BUCKET", "frames")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_supported_url(url: str) -> bool:
    """
    Validates that the URL is a YouTube Shorts or Instagram Reels link.
    Rejects anything else to prevent abuse.
    """
    supported = [
        "youtube.com/shorts/",
        "youtu.be/",
        "instagram.com/reel/",
        "instagram.com/reels/",
    ]
    return any(pattern in url for pattern in supported)


def download_video(url: str, output_path: str) -> bool:
    """
    Downloads a video using yt-dlp.
    Returns True on success, False on failure.

    yt-dlp flags:
        -o                      output filename template
        --merge-output-format   ensures output is always .mp4
        --max-filesize 50m      rejects videos over 50MB (safety limit)
        --no-playlist           never downloads a whole playlist
        -q                      quiet mode (logs handled separately)
    """
    import sys

cmd = [
    sys.executable, "-m", "yt_dlp",
    "-o", output_path,
    "--merge-output-format", "mp4",
    "--max-filesize", "50m",
    "--no-playlist",
    "-q",
    url,
]
    log.info(f"Downloading video: {url}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

    if result.returncode != 0:
        log.error(f"yt-dlp error: {result.stderr}")
        return False

    log.info("Video downloaded successfully")
    return True


def extract_frames(video_path: str, output_dir: str, fps: float = 0.5) -> list:
    """
    Extracts frames from a video using FFmpeg.

    fps=0.5 means 1 frame every 2 seconds.
    For a 60-second Short, this gives ~30 frames (capped at 40).

    FFmpeg flags:
        -i          input file
        -vf fps=N   extract N frames per second
        -q:v 2      high quality JPEG (1=best, 31=worst)
        -vframes 40 hard cap at 40 frames
    """
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
    """
    Uploads extracted frames to Supabase Storage.
    Each frame stored at: frames/{job_id}/frame_001.jpg

    Returns a list of public URLs for each uploaded frame.
    """
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
    """Removes temporary files after upload to keep /tmp tidy."""
    for p in paths:
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    """Health check. Railway uses this to confirm the server is alive."""
    return jsonify({"status": "ok", "service": "framelearn-backend"})


@app.route("/extract", methods=["POST"])
def extract():
    """
    Main extraction endpoint.

    Request body (JSON):
        { "url": "https://youtube.com/shorts/abc123" }

    Success response:
        { "frames": ["https://...jpg", ...], "count": 12, "job_id": "uuid" }

    Error response:
        { "error": "human-readable message" }, HTTP 4xx or 5xx
    """
    # 1. Validate input
    data = request.get_json(silent=True)
    if not data or "url" not in data:
        return jsonify({"error": "Missing 'url' in request body"}), 400

    url = data["url"].strip()

    if not is_supported_url(url):
        return jsonify({
            "error": "Only YouTube Shorts and Instagram Reels are supported."
        }), 400

    # 2. Set up temp paths
    job_id     = str(uuid.uuid4())[:8]
    video_path = f"/tmp/{job_id}_video.mp4"
    frame_dir  = f"/tmp/{job_id}_frames"

    log.info(f"Job {job_id} started for URL: {url}")

    try:
        # 3. Download video
        if not download_video(url, video_path):
            return jsonify({"error": "Could not download video. The link may be private or unavailable."}), 422

        # 4. Extract frames
        frame_paths = extract_frames(video_path, frame_dir)
        if not frame_paths:
            return jsonify({"error": "No frames could be extracted from this video."}), 422

        # 5. Upload to Supabase
        public_urls = upload_frames(frame_paths, job_id)
        if not public_urls:
            return jsonify({"error": "Frame upload failed. Please try again."}), 500

        log.info(f"Job {job_id} complete: {len(public_urls)} frames")
        return jsonify({
            "frames":  public_urls,
            "count":   len(public_urls),
            "job_id":  job_id,
        })

    finally:
        # 6. Always clean up temp files
        all_tmp = glob.glob(f"/tmp/{job_id}*")
        cleanup(all_tmp)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
