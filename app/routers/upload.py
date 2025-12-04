import os
import base64
import shutil
import tempfile
from datetime import datetime
import requests
import dropbox
import json
from fastapi import APIRouter, UploadFile, File, HTTPException, Form, Depends
from sqlalchemy.orm import Session
from PIL import Image
from dotenv import load_dotenv
from typing import List, Optional
from pydantic import BaseModel
from runwayml import RunwayML
from dropbox.exceptions import ApiError
from fastapi import BackgroundTasks
from fastapi import Request
import logging
import time
import re
from app.models.database import SessionLocal, UploadedImage, Video, Feedback, Order, Notification, User
from app.routers.auth import get_current_user
from app.services.prompt_generator import (
    generate_cinematic_prompt_from_image,
    improve_prompt_with_feedback,
)
PACKAGE_LIMITS = {
    "Starter": (5, 10),
    "Professional": (11, 20),
    "Premium": (21, 30)
}

# ----------------------- SETUP -----------------------
router = APIRouter()
load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("runwayml_webhook")

# Mock mode via env: RUNWAY_MOCK=true|1|yes
USE_MOCK_RUNWAY = str(os.getenv("RUNWAY_MOCK", "False")).lower() in {"1", "true", "yes"}

# API key for SDK (supports either env var name)
RUNWAY_API_KEY = os.getenv("RUNWAYML_API_SECRET") or os.getenv("RUNWAY_API_KEY")
RUNWAY_MODEL = os.getenv("RUNWAY_MODEL", "gen4_turbo")
RUNWAY_TASKS_BASE_URL = "https://api.runwayml.com/v1/tasks"

# In your routers file
# Project root
BASE_DIR = os.path.dirname(os.path.abspath(__file__))  # points to 'routers' folder
BASE_DIR = os.path.dirname(BASE_DIR)                   # go up to project root

# Correct paths
IMAGES_DIR = os.path.join(BASE_DIR, "uploaded_images")  # where files really exist
VIDEOS_DIR = os.path.join(BASE_DIR, "videos")

os.makedirs(IMAGES_DIR, exist_ok=True)
os.makedirs(VIDEOS_DIR, exist_ok=True)




# ---------------- UTIL: SAFE FOLDER NAME ----------------
def slugify_path_component(name: Optional[str]) -> str:
    if not name:
        return ""
    s = name.strip().lower()
    # If an email was provided, prefer the local-part before '@'
    if "@" in s:
        s = s.split("@")[0]
    s = re.sub(r"[^a-z0-9._-]+", "_", s)
    s = s.strip("._-")
    return s or ""

def build_agent_folder_name(display_name: Optional[str]) -> str:
    """
    Builds a safe folder name for an agent (user) in Dropbox.
    Prefers the user's name if available, otherwise falls back to a generic user ID.
    """
    if not display_name:
        return "user_unknown"
    return slugify_path_component(display_name)

# Initialize SDK client lazily to avoid stale envs during reloads
client: Optional[RunwayML] = None

def get_runway_client() -> RunwayML:
    global client
    if USE_MOCK_RUNWAY:
        raise RuntimeError("Mock mode enabled; Runway client is not used.")
    if client is None:
        api_key = os.getenv("RUNWAYML_API_SECRET") or os.getenv("RUNWAY_API_KEY")
        if not api_key:
            raise RuntimeError("Runway API key missing. Set RUNWAYML_API_SECRET or RUNWAY_API_KEY.")
        print(f"[RUNWAY] Initializing SDK client; model={RUNWAY_MODEL}")
        client = RunwayML(api_key=api_key)
    return client


# ---------------- IMAGE OPTIMIZATION ----------------
def optimize_image_for_runway(image_path: str, max_width: int = 1024, max_height: int = 1024) -> str:
    """
    Resize and convert image to JPEG format optimized for RunwayML API.
    Returns path to the optimized image file.
    """
    try:
        with Image.open(image_path) as img:
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGB")

            width, height = img.size
            if width > max_width or height > max_height:
                ratio = min(max_width / width, max_height / height)
                new_width, new_height = int(width * ratio), int(height * ratio)
                img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)

            temp_path = tempfile.mktemp(suffix=".jpg")
            img.save(temp_path, "JPEG", quality=85, optimize=True)
            return temp_path
    except Exception as e:
        print(f"⚠️ Image optimization failed: {e}")
        return image_path
    
    
@router.get("/runway/status")
def runway_status():
    """
    Simple integration status for observability (no external calls).
    """
    return {
        "mock": USE_MOCK_RUNWAY,
        "api_key_present": bool(RUNWAY_API_KEY),
        "model": RUNWAY_MODEL,
        "will_charge": (not USE_MOCK_RUNWAY),
        "videos_dir": os.path.abspath(VIDEOS_DIR),
        "images_dir": os.path.abspath(IMAGES_DIR),
    }
    
# ----------------------Notification--------------------

def create_notification(db: Session, user_id: int, type_: str, message: str):
    notif = Notification(
        user_id=user_id,
        type=type_,
        message=message
    )
    db.add(notif)
    db.commit()
    db.refresh(notif)
    return notif

def resolve_user_for_order(db: Session, order: Order) -> Optional[int]:
    """Return the user_id directly from Order only (no invoice/payment fallback)."""
    try:
        return order.user_id if getattr(order, "user_id", None) else None
    except Exception:
        return None

def _extract_output_url_from_task_payload(payload: dict) -> Optional[str]:
    """
    Given a Runway task payload, attempt to extract a usable output URL.
    Supports multiple shapes for the `output` field.
    """
    if not payload:
        return None
    output = payload.get("output")
    if isinstance(output, dict):
        url = output.get("url")
        if url:
            return url
        urls = output.get("urls") or []
        if urls:
            return urls[0]
    elif isinstance(output, list) and output:
        first = output[0]
        if isinstance(first, str):
            return first
        if isinstance(first, dict):
            return first.get("url") or None
    return None

def check_runway_status(task_id: str) -> Optional[dict]:
    """
    Calls Runway's tasks status endpoint for a given task id.
    Returns parsed JSON dict on success, or None on failure.
    """
    try:
        if not RUNWAY_API_KEY:
            raise RuntimeError("Runway API key missing. Set RUNWAYML_API_SECRET or RUNWAY_API_KEY.")

        headers = {"Authorization": f"Bearer {RUNWAY_API_KEY}"}
        url = f"{RUNWAY_TASKS_BASE_URL}/{task_id}"
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"Failed to check Runway status for {task_id}: {e}")
        return None

def poll_runway_status(task_id: str, max_checks: int = 30, interval_seconds: int = 30) -> None:
    """
    Background polling loop: checks the task until SUCCEEDED/FAILED or timeout.
    Updates the associated Video record identified by runway_job_id.
    On success, attempts Dropbox upload and updates video_url accordingly.
    """
    db: Session = SessionLocal()
    try:
        # Resolve the video row first; if not found, we still try polling
        video = db.query(Video).filter(Video.runway_job_id == task_id).first()

        if USE_MOCK_RUNWAY:
            if video:
                # Build per-user folder for Dropbox-like URL display
                user_folder = ""
                try:
                    if getattr(video, "user_id", None):
                        u = db.query(User).filter(User.id == video.user_id).first()
                        display = (
                            (u.email.split("@")[0]) if (u and getattr(u, "email", None)) else (u.name or f"user_{u.id}")
                        ) if u else None
                        agent_folder = build_agent_folder_name(display)
                except Exception:
                    user_folder = ""

                video.status = "succeeded"
                file_name = f"video_{video.id}.mp4"
                dropbox_like_path = (
                    f"/quantumtour/{user_folder}/video output/{file_name}" if user_folder else f"/quantumtour/video output/{file_name}"
                )
                video.video_url = f"dropbox://{dropbox_like_path}"
                db.commit()
                try:
                    create_notification(
                        db=db,
                        user_id=getattr(video, "user_id", None),
                        type_="video_created",
                        message=f"Video #{video.id} succeeded (mock job {task_id})"
                    )
                except Exception as notify_err:
                    logger.error(f"Failed to create notification (mock): {notify_err}")
            logger.info(f"[MOCK] Poll complete for task {task_id}")
            return

        for _ in range(max_checks):
            payload = check_runway_status(task_id)
            if not payload:
                time.sleep(interval_seconds)
                continue

            status = str(payload.get("status") or "").upper()
            if status == "SUCCEEDED":
                output_url = _extract_output_url_from_task_payload(payload)

                if video:
                    # Prefer Dropbox upload; if it fails, keep the original URL
                    final_url = output_url
                    if output_url:
                        # Build per-user folder path
                        user_folder = ""
                        try:
                            if getattr(video, "user_id", None):
                                u = db.query(User).filter(User.id == video.user_id).first()
                                display = (
                                    (u.email.split("@")[0]) if (u and getattr(u, "email", None)) else (u.name or f"user_{u.id}")
                                ) if u else None
                                agent_folder = build_agent_folder_name(display)
                        except Exception:
                            user_folder = ""

                        file_name = f"video_{video.id}.mp4"
                        dropbox_path = (
                            f"/quantumtour/{user_folder}/video output/{file_name}" if user_folder else f"/quantumtour/video output/{file_name}"
                        )
                        try:
                            if upload_video_to_dropbox(output_url, dropbox_path):
                                final_url = f"dropbox://{dropbox_path}"
                        except Exception as up_err:
                            logger.error(f"Dropbox upload error for task {task_id}: {up_err}")

                    video.video_url = final_url
                    video.status = "succeeded"
                    db.commit()

                    try:
                        create_notification(
                            db=db,
                            user_id=getattr(video, "user_id", None),
                            type_="video_created",
                            message=f"Video #{video.id} succeeded (job {task_id})"
                        )
                    except Exception as notify_err:
                        logger.error(f"Failed to create success notification: {notify_err}")

                logger.info(f"✅ Runway task {task_id} SUCCEEDED")
                return

            if status == "FAILED":
                if video:
                    video.status = "failed"
                    db.commit()
                    try:
                        create_notification(
                            db=db,
                            user_id=getattr(video, "user_id", None),
                            type_="video_failed",
                            message=f"Video #{video.id} failed (job {task_id})"
                        )
                    except Exception as notify_err:
                        logger.error(f"Failed to create failure notification: {notify_err}")

                logger.warning(f"❌ Runway task {task_id} FAILED")
                return

            # Still processing or unknown status
            time.sleep(interval_seconds)

        logger.info(f"⏳ Runway task {task_id} still processing after timeout.")
    finally:
        db.close()


@router.post("/runway/check-status")
def runway_check_status(task_id: str, background_tasks: BackgroundTasks, poll_interval_seconds: int = 30, max_checks: int = 30):
    """
    Starts a background polling job for a given Runway task id.
    Does not expose any API keys; uses server-side environment configuration.
    """
    background_tasks.add_task(poll_runway_status, task_id, max_checks=max_checks, interval_seconds=poll_interval_seconds)
    return {
        "status": "started",
        "task_id": task_id,
        "poll_interval_seconds": poll_interval_seconds,
        "max_checks": max_checks,
    }

def upload_video_to_dropbox(video_url: str, dropbox_path: str) -> bool:
    """
    Uploads a video directly from a URL to Dropbox without saving locally.
    Uses refresh-token authentication for permanent access.
    """
    try:
        print(f"[DEBUG] Initializing Dropbox client with refresh token")

        DROPBOX_APP_KEY = os.getenv("DROPBOX_APP_KEY")
        DROPBOX_APP_SECRET = os.getenv("DROPBOX_APP_SECRET")
        DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN")

        if not all([DROPBOX_APP_KEY, DROPBOX_APP_SECRET, DROPBOX_REFRESH_TOKEN]):
            raise Exception("Missing Dropbox credentials in environment variables")

        dbx = dropbox.Dropbox(
            app_key=DROPBOX_APP_KEY,
            app_secret=DROPBOX_APP_SECRET,
            oauth2_refresh_token=DROPBOX_REFRESH_TOKEN,
            timeout=300
        )

        print(f"[DEBUG] Downloading video from URL → {video_url}")
        resp = requests.get(video_url, timeout=300)
        resp.raise_for_status()
        video_bytes = resp.content

        # Folders are pre-created in Dropbox; do not attempt to create them here

        print(f"[DEBUG] Uploading video to Dropbox → {dropbox_path}")
        last_err = None
        for attempt in range(1, 4):
            try:
                dbx.files_upload(video_bytes, dropbox_path, mode=dropbox.files.WriteMode.overwrite)
                break
            except Exception as e:
                last_err = e
                print(f"[WARN] Video upload attempt {attempt} failed: {e}")
                if attempt == 3:
                    raise

        print(f"[OK] Uploaded successfully to Dropbox → {dropbox_path}")
        return True

    except ApiError as api_err:
        print(f"[ERROR] Dropbox API error: {api_err}")
        return False
    except Exception as e:
        print(f"[ERROR] Dropbox upload failed: {e}")
        return False
    
def upload_image_to_dropbox(image_path: str, dropbox_path: str) -> bool:
    """
    Uploads a local image file to Dropbox.
    """
    try:
        DROPBOX_APP_KEY = os.getenv("DROPBOX_APP_KEY")
        DROPBOX_APP_SECRET = os.getenv("DROPBOX_APP_SECRET")
        DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN")

        if not all([DROPBOX_APP_KEY, DROPBOX_APP_SECRET, DROPBOX_REFRESH_TOKEN]):
            raise Exception("Missing Dropbox credentials in environment variables")

        dbx = dropbox.Dropbox(
            app_key=DROPBOX_APP_KEY,
            app_secret=DROPBOX_APP_SECRET,
            oauth2_refresh_token=DROPBOX_REFRESH_TOKEN,
            timeout=300
        )

        with open(image_path, "rb") as f:
            img_bytes = f.read()

        # Folders are pre-created in Dropbox; do not attempt to create them here

        print(f"[DEBUG] Uploading image to Dropbox → {dropbox_path}")
        last_err = None
        for attempt in range(1, 4):
            try:
                dbx.files_upload(img_bytes, dropbox_path, mode=dropbox.files.WriteMode.overwrite)
                break
            except Exception as e:
                last_err = e
                print(f"[WARN] Image upload attempt {attempt} failed: {e}")
                if attempt == 3:
                    raise

        print(f"[OK] Uploaded image successfully → {dropbox_path}")
        return True

    except Exception as e:
        print(f"[ERROR] Failed to upload image to Dropbox: {e}")
        return False

# ----------------------- UPLOAD (MULTI) -----------------------
def process_videos_for_order(order_id: int, file_paths: list, reorder_user_id: Optional[int] = None):
    print(f"[BG] Start processing order {order_id} with {len(file_paths)} files")
    db: Session = SessionLocal()
    try:
        order = db.query(Order).filter(Order.id == order_id).first()
        if not order:
            print(f"[ERROR] Order {order_id} not found")
            return

        # Resolve a user once per order if possible
        resolved_user_id = resolve_user_for_order(db, order)

        # Build per-user folder for Dropbox videos
        user_folder = ""
        try:
            if resolved_user_id:
                user = db.query(User).filter(User.id == resolved_user_id).first()
                display = (
                    (user.email.split("@")[0]) if (user and getattr(user, "email", None)) else (user.name or f"user_{user.id}")
                ) if user else None
                user_folder = build_agent_folder_name(display)
        except Exception:
            user_folder = ""

        for src_path in file_paths:
            filename = os.path.basename(src_path)
            try:
                print(f"[STEP] Opening image → {src_path}")
                with Image.open(src_path) as im:
                    w, h = im.size
                print(f"[OK] Image size: {w}x{h}")
            except Exception as e:
                print(f"[ERROR] While processing {filename}: {e}")
                continue

            with open(src_path, "rb") as f:
                file_content = f.read()

            # Create UploadedImage row
            img_row = UploadedImage(
                order_id=order.id,
                filename=filename,
                content=file_content,
                upload_time=datetime.utcnow(),
            )
            db.add(img_row)
            db.commit()
            db.refresh(img_row)

            print(f"[STEP] Generating cinematic prompt for {filename}")
            prompt_text = generate_cinematic_prompt_from_image(src_path)
            img_row.prompt = prompt_text
            db.commit()

            ratio = "1280:720" if w >= h else "720:1280"
            opt_path = optimize_image_for_runway(src_path)
            with open(opt_path, "rb") as rf:
                image_b64 = base64.b64encode(rf.read()).decode("utf-8")
            data_url = f"data:image/jpeg;base64,{image_b64}"

            # RunwayML video generation
            print(f"[RUNWAY] mock={USE_MOCK_RUNWAY} key_present={bool(RUNWAY_API_KEY)} model={RUNWAY_MODEL}")
            if USE_MOCK_RUNWAY:
                video_filename = f"mock_{img_row.id}.mp4"
                video_url, task_id, status = None, f"mock-job-{int(datetime.utcnow().timestamp())}", "succeeded"
            else:
                try:
                    print(f"[STEP] Sending request to RunwayML for {filename}")
                    sdk = get_runway_client()
                    task = sdk.image_to_video.create(
                        model=RUNWAY_MODEL,
                        prompt_image=data_url,
                        prompt_text=prompt_text,
                        duration=5,
                        ratio=ratio,
                    ).wait_for_task_output()

                    if not task.output:
                        raise Exception("RunwayML did not return a video")

                    video_url = task.output[0]
                    task_id = task.id
                    status = "succeeded"
                    # Build filename and path inside optional per-user folder
                    file_name = (
                        f"reorder_{reorder_user_id}_{img_row.id}.mp4" if reorder_user_id else f"video_{img_row.id}.mp4"
                    )
                    dropbox_path = (
                        f"/quantumtour/{user_folder}/video output/{file_name}" if user_folder else f"/quantumtour/video output/{file_name}"
                    )
                    upload_success = upload_video_to_dropbox(video_url, dropbox_path)

                    if upload_success:
                        video_url = f"dropbox://{dropbox_path}"
                    else:
                        video_url = None
                        status = "failed"

                except Exception as e:
                    print(f"[ERROR] RunwayML generation failed: {e}")
                    status, video_url, task_id = "failed", None, None

            # Save Video row
            video_row = Video(
                user_id=resolved_user_id,
                image_id=img_row.id,
                prompt=prompt_text,
                runway_job_id=task_id,
                status=status,
                video_url=video_url,
                iteration=1,
            )
            db.add(video_row)
            db.commit()
            db.refresh(video_row)

            # Update UploadedImage with video info
            img_row.video_url = video_url
            img_row.video_generated_at = datetime.utcnow()
            db.commit()

            if video_row.status == "succeeded":
                create_notification(
                    db=db,
                    user_id=resolved_user_id,
                    type_="video_created",
                    message=f"Video #{video_row.id} created for Order #{order.id} ({filename})"
                )
            else:
                create_notification(
                    db=db,
                    user_id=resolved_user_id,
                    type_="video_failed",
                    message=f"Video #{video_row.id} failed for Order #{order.id} ({filename})"
                )

            if opt_path != src_path:
                try:
                    os.remove(opt_path)
                except Exception:
                    pass

        print(f"[OK] Finished background processing for order {order_id}")
    finally:
        db.close()
        

@router.post("/upload")
async def upload_photos(
    background_tasks: BackgroundTasks,
    package: str = Form(...),
    add_ons: Optional[str] = Form(None),
    files: List[UploadFile] = File(...),
    current_user = Depends(get_current_user)
):
    print("[UPLOAD] Endpoint called ✅")

    if package not in PACKAGE_LIMITS:
        raise HTTPException(status_code=400, detail="Invalid package selected")

    # ✅ Validate number of files
    min_files, max_files = PACKAGE_LIMITS[package]
    if not (min_files <= len(files) <= max_files):
        raise HTTPException(
            status_code=400,
            detail=f"{package} allows {min_files}-{max_files} photos"
        )

    db = SessionLocal()
    try:
        # Create a new order
        order = Order(
            package=package,
            add_ons=add_ons,
            user_id=(current_user.id if current_user else None)
        )
        db.add(order)
        db.commit()
        db.refresh(order)

        print(f"[UPLOAD] New order created → ID: {order.id}")

        saved_files = []
        # Build per-user folder for Dropbox images
        user_display = None
        try:
            if current_user:
                user_display = (
                    current_user.email.split("@")[0] if getattr(current_user, "email", None) else (
                        current_user.name or (f"user_{current_user.id}" if getattr(current_user, "id", None) else None)
                    )
                )
        except Exception:
            user_display = None
        user_folder = slugify_path_component(user_display)
        for file in files:
            dst_path = os.path.join(IMAGES_DIR, file.filename)
            print(f"[STEP] Saving file → {dst_path}")
            try:
                with open(dst_path, "wb") as f:
                    shutil.copyfileobj(file.file, f)
                saved_files.append(dst_path)
                print(f"[OK] Saved file {file.filename}")

                # Upload to Dropbox immediately
                dropbox_path = (
                    f"/quantumtour/{user_folder}/uploaded images/{file.filename}" if user_folder else f"/quantumtour/uploaded images/{file.filename}"
                )
                # Queue Dropbox upload in background to return response faster
                background_tasks.add_task(upload_image_to_dropbox, dst_path, dropbox_path)
                print(f"[BG] Scheduled Dropbox upload → {dropbox_path}")

            except Exception as e:
                print(f"[ERROR] Could not save {file.filename}: {e}")
                raise HTTPException(status_code=500, detail=f"Failed to save {file.filename}")

        print("[UPLOAD] Adding background task now...")
        background_tasks.add_task(process_videos_for_order, order.id, saved_files)
        print("[UPLOAD] Background task added successfully")

        # ✅ Build structured response
        response_data = {
            "status": "success",
            "order_id": order.id,
            "package": order.package,
            "add_ons": order.add_ons,
            "status": "processing",
            "date": order.created_at.isoformat() if hasattr(order, "created_at") else str(datetime.utcnow()),
            "videos": []
        }

        print(f"[UPLOAD] Returning response → {response_data}")
        return response_data

    finally:
        db.close()
        
@router.post("/runwayml/webhook")
async def runwayml_webhook(request: Request):
    """
    RunwayML webhook: upserts video status by runway job id and logs a notification.
    Always returns 200 to avoid noisy retries; errors are logged.
    """
    try:
        # Be tolerant to empty/invalid JSON bodies
        raw_body = await request.body()
        payload = {}
        if raw_body:
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except Exception:
                payload = {}

        logger.info(f"📩 Webhook received from RunwayML: {json.dumps(payload, indent=2)}")

        job_id = payload.get("id") or payload.get("job_id")
        status = payload.get("status")

        # Try to determine an output URL from multiple possible shapes
        output_url = None
        output = payload.get("output")
        if isinstance(output, dict):
            output_url = output.get("url") or (
                (output.get("urls") or [])[:1][0] if output.get("urls") else None
            )
        elif isinstance(output, list) and output:
            first_item = output[0]
            output_url = first_item if isinstance(first_item, str) else first_item.get("url")

        db: Session = SessionLocal()
        try:
            if not job_id:
                logger.warning("Runway webhook missing job id; payload ignored")
                return {"status": "ok"}

            video = db.query(Video).filter(Video.runway_job_id == job_id).first()
            if not video:
                logger.warning(f"Runway webhook: no Video found for job {job_id}")
                return {"status": "ok"}

            if status == "succeeded":
                video.status = "succeeded"
                if output_url:
                    video.video_url = output_url
                db.commit()
                try:
                    create_notification(
                        db=db,
                        user_id=video.user_id,
                        type_="video_created",
                        message=f"Video #{video.id} succeeded (job {job_id})"
                    )
                except Exception as notify_err:
                    logger.error(f"Failed to create success notification: {notify_err}")
                logger.info(f"✅ Runway job {job_id} mapped to Video {video.id} marked succeeded")
            elif status == "failed":
                video.status = "failed"
                db.commit()
                try:
                    create_notification(
                        db=db,
                        user_id=video.user_id,
                        type_="video_failed",
                        message=f"Video #{video.id} failed (job {job_id})"
                    )
                except Exception as notify_err:
                    logger.error(f"Failed to create failure notification: {notify_err}")
                logger.warning(f"❌ Runway job {job_id} mapped to Video {video.id} marked failed")
            else:
                # Update status if provided (e.g., 'processing') without notifications
                if status:
                    video.status = status
                    db.commit()
                logger.info(f"ℹ️ Runway job {job_id} current status: {status}")
        finally:
            db.close()

        return {"status": "ok"}

    except Exception as e:
        logger.error(f"⚠️ Error processing webhook: {e}")
        # Avoid failing the webhook; return 200 to keep pipeline flowing
        return {"status": "ok"}

# ----------------------- FEEDBACK -----------------------
class FeedbackPayload(BaseModel):
    video_id: int
    feedback_text: str


@router.post("/feedback")
def submit_feedback(payload: FeedbackPayload):
    db = SessionLocal()
    try:
        # 1) Get parent video
        parent_video = db.query(Video).filter(Video.id == payload.video_id).first()
        if not parent_video:
            raise HTTPException(status_code=404, detail="Video not found")

        # 2) Get source image
        image = db.query(UploadedImage).filter(UploadedImage.id == parent_video.image_id).first()
        if not image:
            raise HTTPException(status_code=404, detail="Source image not found")

        # 3) Generate improved prompt
        new_prompt = improve_prompt_with_feedback(parent_video.prompt, payload.feedback_text)

        # 4) Save feedback
        fb = Feedback(video_id=parent_video.id, feedback_text=payload.feedback_text, new_prompt=new_prompt)
        db.add(fb)
        db.commit()
        db.refresh(fb)

        # 5) Prepare image for Runway
        image_b64 = base64.b64encode(image.content).decode("utf-8")
        opt_path = f"data:image/jpeg;base64,{image_b64}"
        with open(opt_path, "rb") as rf:
            image_b64 = base64.b64encode(rf.read()).decode("utf-8")
        data_url = f"data:image/jpeg;base64,{image_b64}"

        # 6) Decide aspect ratio
        with Image.open(opt_path) as im:
            w, h = im.size
        ratio = "1280:720" if w >= h else "720:1280"

        # 7) Generate new video via Runway
        print(f"[RUNWAY] mock={USE_MOCK_RUNWAY} key_present={bool(RUNWAY_API_KEY)} model={RUNWAY_MODEL}")
        sdk = get_runway_client()
        task = sdk.image_to_video.create(
            model=RUNWAY_MODEL,
            prompt_image=data_url,
            prompt_text=new_prompt,
            duration=5,
            ratio=ratio,
        ).wait_for_task_output()

        if not task.output:
            raise HTTPException(status_code=500, detail="RunwayML did not return a video")

        video_url = task.output[0]
        task_id = task.id
        status = "succeeded"

        video_filename = f"video_{image.id}_{int(datetime.utcnow().timestamp())}.mp4"
        video_path = os.path.join(VIDEOS_DIR, video_filename)
        resp = requests.get(video_url, timeout=300)
        resp.raise_for_status()
        with open(video_path, "wb") as vf:
            vf.write(resp.content)

        # 8) Create child Video row
        # Derive user for child video: prefer parent.user_id, else from order fallbacks
        derived_user_id = parent_video.user_id
        if not derived_user_id:
            try:
                parent_image_order = db.query(UploadedImage).filter(UploadedImage.id == parent_video.image_id).first().order
                derived_user_id = resolve_user_for_order(db, parent_image_order) if parent_image_order else None
            except Exception:
                derived_user_id = None

        child = Video(
            image_id=image.id,
            prompt=new_prompt,
            parent_video_id=parent_video.id,
            iteration=(parent_video.iteration or 1) + 1,
            runway_job_id=task_id,
            status=status,
            video_url=video_url,
            video_path=video_path,
            user_id=derived_user_id,
        )
        db.add(child)
        db.commit()
        db.refresh(child)

        return {
            "new_video_id": child.id,
            "status": status,
            "video_url": video_url,
            "local_path": video_path,
            "new_prompt": new_prompt
        }

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error generating video: {str(e)}")
    finally:
        db.close()

# ----------------------- VIDEO STATUS -----------------------

# @router.get("/video/status/{video_id}")
# def get_video_status(video_id: int):
#     """
#     Returns the stored status for a video job.
#     Since /upload waits for completion (SDK .wait_for_task_output), most videos
#     will already be 'succeeded'. This endpoint simply reflects DB state.
#     """
#     db = SessionLocal()
#     try:
#         video = db.query(Video).filter(Video.id == video_id).first()
#         if not video:
#             raise HTTPException(status_code=404, detail="Video not found")

#         image = db.query(UploadedImage).filter(UploadedImage.id == video.image_id).first()

#         data = {
#             "video_id": video.id,
#             "status": video.status,
#             "prompt": video.prompt,
#             "runway_task_id": video.runway_job_id,
#             "video_url": video.video_url,
#             "video_path": video.video_path,
#             "iteration": video.iteration,
#             "created_at": video.created_at,
#             "image_filename": image.filename if image else None,
#         }

#         if video.video_path and os.path.exists(video.video_path):
#             data["local_url"] = f"/videos/{os.path.basename(video.video_path)}"

#         return data

#     finally:
#         db.close()


# ----------------------- RUNWAY STATUS -----------------------
