from fastapi import APIRouter, HTTPException, UploadFile, File , Depends ,Form
from datetime import datetime
import os
from datetime import datetime, timezone
from fastapi import APIRouter
from app.models.database import SessionLocal, Video, UploadedImage, Order , FinalVideo , User , Notification , Payment , Invoice
import shutil
import time 
from app.models.database import SessionLocal, Order, UploadedImage, Video, User ,Notification
from app.models.database import get_db
from sqlalchemy.orm import Session  
from sqlalchemy.sql import func
from app.services.runway_service import generate_video
from datetime import timedelta
import dropbox
from app.routers.auth import get_current_user
from app.routers.upload import build_agent_folder_name
from app.models.database import SessionLocal
import dropbox, time, os, tempfile, shutil
import re
from fastapi import HTTPException
from datetime import datetime
from app.models.database import UploadedImage, Video

from app.config import DROPBOX_APP_KEY, DROPBOX_APP_SECRET, DROPBOX_REFRESH_TOKEN
import tempfile

# Initialize Dropbox
DROPBOX_ACCESS_TOKEN = os.getenv("DROPBOX_ACCESS_TOKEN")
DROPBOX_APP_KEY = os.getenv("DROPBOX_APP_KEY")
DROPBOX_APP_SECRET = os.getenv("DROPBOX_APP_SECRET")
DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN")
dbx = dropbox.Dropbox(
            app_key=DROPBOX_APP_KEY,
            app_secret=DROPBOX_APP_SECRET,
            oauth2_refresh_token=DROPBOX_REFRESH_TOKEN
        )
router = APIRouter()
# Helper to present a professional-looking code for users (guest vs registered)
def _format_user_code(user: User | None) -> str | None:
    try:
        if not user:
            return None
        prefix = "GST" if (getattr(user, "is_guest", False) or not getattr(user, "email", None)) else "USR"
        return f"{prefix}-{int(user.id):05d}"
    except Exception:
        return None

def _user_from_video(db: Session, v: Video) -> User | None:
    """Resolve a user for a given video by preferring Video.user, then Image->Order->User,
    else fallback to Invoice.user or Payment.user for the same order if present."""
    try:
        # 1) direct on Video
        if getattr(v, "user_id", None):
            u = db.query(User).filter(User.id == v.user_id).first()
            if u:
                return u
        # 2) via relationships
        if v.image and v.image.order:
            order = v.image.order
            if order.user_id:
                u = db.query(User).filter(User.id == order.user_id).first()
                if u:
                    return u
            # 3) fallback to invoice mapping if available
            inv = (
                db.query(Invoice)
                .filter(Invoice.order_id == order.id)
                .order_by(Invoice.created_at.desc())
                .first()
            )
            if inv and inv.user_id:
                u = db.query(User).filter(User.id == inv.user_id).first()
                if u:
                    return u
            # 4) fallback to payment mapping if available
            pay = (
                db.query(Payment)
                .filter(Payment.order_id == order.id)
                .order_by(Payment.created_at.desc())
                .first()
            )
            if pay and pay.user_id:
                u = db.query(User).filter(User.id == pay.user_id).first()
                if u:
                    return u
    except Exception:
        return None
    return None
# ---------------- ADMIN: VIDEOS LISTING ----------------
@router.get("/admin/videos", tags=["Admin Portal"])
def list_videos():
    """Return latest videos with playable/downloadable URLs and related client/image info."""
    db = SessionLocal()
    try:
        videos = db.query(Video).order_by(Video.created_at.desc()).limit(200).all()

        items = []
        for v in videos:
            filename = os.path.basename(v.video_path) if v.video_path else None
            local_url = f"/videos/{filename}" if filename else None
            image = db.query(UploadedImage).filter(UploadedImage.id == v.image_id).first()
            order = db.query(Order).filter(Order.id == image.order_id).first() if image else None
            user = db.query(User).filter(User.id == order.user_id).first() if order and order.user_id else None
            image_url = f"/uploaded_images/{image.filename}" if image and image.filename else None

            # Consistent naming + provide stable unique download name
            download_filename = f"video_{v.id}.mp4"

            items.append({
                "video_id": v.id,
                "image_id": v.image_id,
                "user_id": user.id if user else None,
                "user_email": user.email if user else None,
                "user_name": user.name if user else None,
                "user_code": _format_user_code(user),
                "status": v.status,
                "prompt": v.prompt,
                "created_at": v.created_at.isoformat() if v.created_at else None,
                "iteration": v.iteration,
                "runway_job_id": v.runway_job_id,
                "remote_url": v.video_url,
                "local_url": local_url,
                "filename": filename,                 # original base name if present
                "download_filename": download_filename, # unique suggested name
                "download_url": local_url,
                "image_filename": image.filename if image else None,
                "image_url": image_url,
            })

        return {"videos": items, "count": len(items)}
    finally:
        db.close()


# ---------------- ADMIN: ORDER MANAGEMENT ----------------
@router.get("/Admin/order_management", tags=["Admin Portal"])
def get_order_status():
    db = SessionLocal()
    try:
        # ✅ Join Orders with Users to fetch email directly
        orders_with_users = (
            db.query(Order, User)
            .join(User, Order.user_id == User.id)
            .order_by(Order.created_at.desc())
            .all()
        )

        response = []

        for order, user in orders_with_users:
            # Get images for this order
            images = db.query(UploadedImage).filter(UploadedImage.order_id == order.id).all()

            # Latest video per image
            latest_video_subq = (
                db.query(
                    Video.image_id,
                    func.max(Video.iteration).label("max_iter")
                )
                .group_by(Video.image_id)
                .subquery()
            )

            latest_videos = (
                db.query(Video)
                .join(
                    latest_video_subq,
                    (Video.image_id == latest_video_subq.c.image_id)
                    & (Video.iteration == latest_video_subq.c.max_iter),
                )
                .join(UploadedImage, UploadedImage.id == Video.image_id)
                .filter(UploadedImage.order_id == order.id)
                .all()
            )

            image_id_to_video = {v.image_id: v for v in latest_videos}
            photo_count = len(images)

            # Determine order status
            if all(v.status in ["completed", "succeeded"] for v in image_id_to_video.values()):
                status = "completed"
            elif any(v.status == "processing" for v in image_id_to_video.values()):
                status = "processing"
            elif any(v.status == "failed" for v in image_id_to_video.values()):
                status = "failed"
            else:
                status = "submitted"

            response.append({
                "order_id": order.id,
                "user_id": user.id,
                "user_email": user.email,
                "user_name": user.name,
                "user_code": _format_user_code(user),
                "package": order.package,
                "add_ons": order.add_ons,
                "photos": photo_count,
                "status": status,
                "date": order.created_at,
                "videos": [
                    {
                        "filename": (v.video_path.split("/")[-1] if v.video_path else None),
                        "download_filename": f"video_{v.id}.mp4",
                        "url": v.video_url or "",
                        "status": v.status
                    }
                    for v in image_id_to_video.values()
                ],
            })

        return {"orders": response, "count": len(response)}

    finally:
        db.close()

# ----------------------- ADMIN: UPDATE STATUS -----------------------

@router.post("/admin/orders/{image_id}/status", tags=["Admin Portal"])
def admin_update_order_status(order_id: int, payload: dict):
    """Admin sets the latest video's status for an order (UploadedImage).
    
    Accepts statuses like pending|processing|completed|failed.
    Maps 'completed' -> 'succeeded' for internal storage.
    """
    new_status = (payload.get("status") or "").strip().lower()
    status_map = {"completed": "succeeded", "pending": "queued"}
    internal_status = status_map.get(new_status, new_status)
    if internal_status not in {"queued", "processing", "succeeded", "failed"}:
        raise HTTPException(status_code=400, detail="Invalid status")
    
    db = SessionLocal()
    try:
        # 1️⃣ Get the uploaded image
        image = db.query(UploadedImage).filter(UploadedImage.id == order_id).first()
        if not image:
            raise HTTPException(status_code=404, detail="Image not found")

        # 2️⃣ Get the latest video for this image
        latest_video = (
            db.query(Video)
            .filter(Video.image_id == image.id)
            .order_by(Video.iteration.desc(), Video.id.desc())
            .first()
        )

        # 3️⃣ If no video exists, create one
        if not latest_video:
            latest_video = Video(
                image_id=image.id,
                prompt=image.prompt or "",
                status=internal_status or "queued",
                iteration=1,
            )
            db.add(latest_video)
            db.commit()
            db.refresh(latest_video)
        else:
            # Update status if internal_status is provided
            if internal_status:
                latest_video.status = internal_status
                db.commit()

        # 4️⃣ Return correct IDs
        return {
            "image_id": image.id,            # UploadedImage.id
            "video_id": latest_video.id,     # Video.id
            "status": latest_video.status,
            "video_path": latest_video.video_path,
            "video_url": latest_video.video_url,
        }
    finally:
        db.close()
        
def resolve_user_for_order(db: Session, order: Order):
    """Try to find a user_id for an order using invoice/payment fallbacks."""
    if order.user_id:
        return order.user_id

    # try invoice
    inv = db.query(Invoice).filter(Invoice.order_id == order.id).order_by(Invoice.created_at.desc()).first()
    if inv and inv.user_id:
        return inv.user_id

    # try payment
    pay = db.query(Payment).filter(Payment.order_id == order.id).order_by(Payment.created_at.desc()).first()
    if pay and pay.user_id:
        return pay.user_id

    # no user found
    return None

# @router.post("/admin/orders/{order_id}/final-video", tags=["Admin Portal"])
# async def admin_upload_final_video(
#     order_id: int,
#     file: UploadFile = File(...),
#     assign_user_id: int | None = None,         # optional admin override
#     assign_user_email: str | None = None,      # optional admin override
# ):
#     db = SessionLocal()
#     try:
#         # 1️⃣ Get the order
#         order = db.query(Order).filter(Order.id == order_id).first()
#         if not order:
#             raise HTTPException(status_code=404, detail="Order not found")

#         # 2️⃣ Determine user_id
#         user_id = None
#         if assign_user_id:
#             user_id = assign_user_id
#         elif assign_user_email:
#             u = db.query(User).filter(User.email == assign_user_email).first()
#             if not u:
#                 raise HTTPException(status_code=404, detail=f"No user with email {assign_user_email}")
#             user_id = u.id
#         else:
#             user_id = resolve_user_for_order(db, order)

#         if not user_id:
#             raise HTTPException(
#                 status_code=400,
#                 detail=(
#                     "Order has no associated user_id. Provide assign_user_id or assign_user_email "
#                     f"or fix the order record. Order id: {order.id}"
#                 )
#             )

#         # 3️⃣ Save file temporarily and upload to Dropbox
#         ts = int(time.time())
#         filename = f"final_order_{order_id}_{ts}.mp4"
#         temp_path = os.path.join(tempfile.gettempdir(), filename)
#         with open(temp_path, "wb") as out:
#             shutil.copyfileobj(file.file, out)

#         dbx = dropbox.Dropbox(
#             app_key=os.getenv("DROPBOX_APP_KEY"),
#             app_secret=os.getenv("DROPBOX_APP_SECRET"),
#             oauth2_refresh_token=os.getenv("DROPBOX_REFRESH_TOKEN"),
#         )
#         dropbox_path = f"/final_videos/{filename}"
#         with open(temp_path, "rb") as f:
#             dbx.files_upload(f.read(), dropbox_path, mode=dropbox.files.WriteMode("overwrite"))

#         shared_link_metadata = dbx.sharing_create_shared_link_with_settings(dropbox_path)
#         video_url = shared_link_metadata.url.replace("?dl=0", "?raw=1")

#         # 4️⃣ Update all images of this order
#         images = db.query(UploadedImage).filter(UploadedImage.order_id == order_id).all()
#         for img in images:
#             img.video_path = dropbox_path
#             img.video_url = video_url
#             img.video_generated_at = datetime.utcnow()
#         db.commit()

#         # 5️⃣ Save Video row for each image
#         video_records = []
#         for img in images:
#             latest_video = (
#                 db.query(Video)
#                 .filter(Video.image_id == img.id)
#                 .order_by(Video.iteration.desc(), Video.id.desc())
#                 .first()
#             )
#             if not latest_video:
#                 latest_video = Video(
#                     image_id=img.id,
#                     prompt=img.prompt or "",
#                     status="succeeded",
#                     iteration=1,
#                     video_path=dropbox_path,
#                     video_url=video_url,
#                     created_at=datetime.utcnow(),
#                     user_id=user_id,
#                 )
#                 db.add(latest_video)
#             else:
#                 latest_video.status = "succeeded"
#                 latest_video.video_path = dropbox_path
#                 latest_video.video_url = video_url
#                 latest_video.updated_at = datetime.utcnow()
#                 latest_video.user_id = user_id
#             db.commit()
#             db.refresh(latest_video)
#             video_records.append(latest_video)

#         # 6️⃣ Save FinalVideo
#         final_video = FinalVideo(
#             user_id=user_id,
#             image_id=images[0].id if images else None,  # assign first image as reference
#             dropbox_path=dropbox_path,
#             video_url=video_url,
#             created_at=datetime.utcnow(),
#         )
#         db.add(final_video)
#         db.commit()
#         db.refresh(final_video)

#         if os.path.exists(temp_path):
#             os.remove(temp_path)

#         return {
#             "order_id": order.id,
#             "user_id": user_id,
#             "video_ids": [v.id for v in video_records],
#             "final_video_id": final_video.id,
#             "video_url": video_url,
#             "dropbox_path": dropbox_path,
#             "images_updated": [img.id for img in images],
#         }

#     except dropbox.exceptions.ApiError as e:
#         raise HTTPException(status_code=500, detail=f"Dropbox upload failed: {str(e)}")
#     finally:
#         db.close()   

@router.post("/admin/final-video", tags=["Admin Portal"])
async def admin_upload_final_video(
    user_id: int,  # pass the client ID directly
    file: UploadFile = File(...)
):
    db = SessionLocal()
    try:
        # 1️⃣ Find all images for this user
        images = db.query(UploadedImage).join(Order).filter(Order.user_id == user_id).all()
        if not images:
            raise HTTPException(status_code=404, detail="No images found for this user")

        # 2️⃣ Upload file to Dropbox (same as before)
        ts = int(time.time())
        filename = f"final_user_{user_id}_{ts}.mp4"
        temp_path = os.path.join(tempfile.gettempdir(), filename)
        with open(temp_path, "wb") as out:
            shutil.copyfileobj(file.file, out)

        dbx = dropbox.Dropbox(
            app_key=os.getenv("DROPBOX_APP_KEY"),
            app_secret=os.getenv("DROPBOX_APP_SECRET"),
            oauth2_refresh_token=os.getenv("DROPBOX_REFRESH_TOKEN"),
        )
        # Build agent folder name: '<Display Name> (Agent Name)'
        user = db.query(User).filter(User.id == user_id).first()
        display = (user.name or user.email or f"user_{user_id}") if user else f"user_{user_id}"
        agent_folder = build_agent_folder_name(display)
        dropbox_path = f"/Quantumtour/{agent_folder}/Final Edited Video (Editor)/{filename}"
        with open(temp_path, "rb") as f:
            dbx.files_upload(f.read(), dropbox_path, mode=dropbox.files.WriteMode("overwrite"))

        shared_link_metadata = dbx.sharing_create_shared_link_with_settings(dropbox_path)
        video_url = shared_link_metadata.url.replace("?dl=0", "?raw=1")

        # 3️⃣ Update images and create Video records
        video_records = []
        for img in images:
            img.video_path = dropbox_path
            img.video_url = video_url
            img.video_generated_at = datetime.utcnow()

            latest_video = Video(
                image_id=img.id,
                prompt=img.prompt or "",
                status="succeeded",
                iteration=1,
                video_path=dropbox_path,
                video_url=video_url,
                created_at=datetime.utcnow(),
                user_id=user_id,
            )
            db.add(latest_video)
            video_records.append(latest_video)

        db.commit()
        for v in video_records:
            db.refresh(v)

        # 4️⃣ Save FinalVideo (just link to the first image as reference)
        final_video = FinalVideo(
            user_id=user_id,
            image_id=images[0].id,
            dropbox_path=dropbox_path,
            video_url=video_url,
            created_at=datetime.utcnow(),
        )
        db.add(final_video)
        db.commit()
        db.refresh(final_video)

        if os.path.exists(temp_path):
            os.remove(temp_path)

        return {
            "user_id": user_id,
            "video_ids": [v.id for v in video_records],
            "final_video_id": final_video.id,
            "video_url": video_url,
            "dropbox_path": dropbox_path,
            "images_updated": [img.id for img in images],
        }

    finally:
        db.close()


# ----------------------- ADMIN: UPLOAD FINAL VIDEO -----------------------
# @router.post("/admin/orders/{image_id}/final-video", tags=["Admin Portal"])
# async def admin_upload_final_video(image_id: int, file: UploadFile = File(...)):
#     """
#     Admin uploads a final rendered video for an image.
#     The function:
#       - uploads to Dropbox,
#       - updates the video + image,
#       - and stores FinalVideo linked to the correct user automatically.
#     """

#     dbx = dropbox.Dropbox(
#         app_key=os.getenv("DROPBOX_APP_KEY"),
#         app_secret=os.getenv("DROPBOX_APP_SECRET"),
#         oauth2_refresh_token=os.getenv("DROPBOX_REFRESH_TOKEN"),
#     )

#     db = SessionLocal()
#     try:
#         # 🔍 Get the image
#         image = db.query(UploadedImage).filter(UploadedImage.id == image_id).first()
#         if not image:
#             raise HTTPException(status_code=404, detail="Image not found")

#         # 🔍 Get order (linked to user)
#         order = db.query(Order).filter(Order.id == image.order_id).first()
#         if not order:
#             raise HTTPException(status_code=404, detail="Order not found")

#         # ✅ Ensure user_id exists
#         if not order.user_id:
#             raise HTTPException(status_code=400, detail="Order has no associated user")

#         # ✅ Prepare filename
#         ts = int(time.time())
#         filename = f"final_{image_id}_{ts}.mp4"
#         temp_path = os.path.join(tempfile.gettempdir(), filename)

#         # ✅ Save temp file
#         with open(temp_path, "wb") as buffer:
#             shutil.copyfileobj(file.file, buffer)

#         # ✅ Upload to Dropbox
#         dropbox_path = f"/final_videos/{filename}"
#         with open(temp_path, "rb") as f:
#             dbx.files_upload(f.read(), dropbox_path, mode=dropbox.files.WriteMode("overwrite"))

#         shared_link_metadata = dbx.sharing_create_shared_link_with_settings(dropbox_path)
#         video_url = shared_link_metadata.url.replace("?dl=0", "?raw=1")

#         # ✅ Update or create Video entry
#         latest_video = (
#             db.query(Video)
#             .filter(Video.image_id == image_id)
#             .order_by(Video.iteration.desc(), Video.id.desc())
#             .first()
#         )

#         if not latest_video:
#             latest_video = Video(
#                 image_id=image.id,
#                 prompt=image.prompt or "",
#                 status="succeeded",
#                 iteration=1,
#                 video_path=dropbox_path,
#                 video_url=video_url,
#                 created_at=datetime.utcnow(),
#                 user_id=order.user_id,
#             )
#             db.add(latest_video)
#         else:
#             latest_video.status = "succeeded"
#             latest_video.video_path = dropbox_path
#             latest_video.video_url = video_url
#             latest_video.updated_at = datetime.utcnow()
#             latest_video.user_id = order.user_id

#         db.commit()
#         db.refresh(latest_video)

#         # ✅ Update image record
#         image.video_path = dropbox_path
#         image.video_url = video_url
#         image.video_generated_at = datetime.utcnow()
#         db.commit()

#         # ✅ Save FinalVideo linked to the same user
#         final_video = FinalVideo(
#             user_id=order.user_id,
#             image_id=image.id,
#             dropbox_path=dropbox_path,
#             video_url=video_url,
#             created_at=datetime.utcnow(),
#         )
#         db.add(final_video)
#         db.commit()
#         db.refresh(final_video)

#         # ✅ Cleanup
#         if os.path.exists(temp_path):
#             os.remove(temp_path)

#         return {
#             "image_id": image.id,
#             "user_id": order.user_id,
#             "video_id": latest_video.id,
#             "final_video_id": final_video.id,
#             "video_url": video_url,
#             "dropbox_path": dropbox_path,
#             "status": "✅ Final video uploaded and linked successfully"
#         }

#     except dropbox.exceptions.ApiError as e:
#         raise HTTPException(status_code=500, detail=f"Dropbox upload failed: {str(e)}")
#     except Exception as e:
#         raise HTTPException(status_code=500, detail=str(e))
#     finally:
#         db.close()



# ----------------------- ADMIN: CUSTOMIZE PROMPT & REGENERATE -----------------------
@router.post("/admin/orders/{image_id}/regenerate", tags=["Admin Portal"])
def admin_regenerate_video(image_id: int, payload: dict):
    """Regenerate a video's latest iteration from a custom prompt using RunwayML."""
    new_prompt = (payload.get("prompt") or "").strip()
    if not new_prompt:
        raise HTTPException(status_code=422, detail="Prompt is required")

    db = SessionLocal()
    try:
        image = db.query(UploadedImage).filter(UploadedImage.id == image_id).first()
        if not image:
            raise HTTPException(status_code=404, detail="Image not found")

        # Determine input and output paths
        image_path = os.path.join("uploads", image.filename)
        if not os.path.exists(image_path):
            raise HTTPException(status_code=404, detail="Source image file not found on server")

        ts = int(time.time())
        out_filename = f"regen_{image_id}_{ts}.mp4"
        out_path = os.path.join("videos", out_filename)

        # Determine next iteration number
        latest_for_image = (
            db.query(Video)
            .filter(Video.image_id == image.id)
            .order_by(Video.iteration.desc(), Video.id.desc())
            .first()
        )

        # Create Video record first (with queued status)
        video = Video(
            image_id=image.id,
            prompt=new_prompt,
            iteration=((latest_for_image.iteration if latest_for_image and latest_for_image.iteration else 0) + 1),
            status="queued",
            runway_job_id=None
        )
        db.add(video)
        db.commit()
        db.refresh(video)

        # Call RunwayML with video_id for real-time tracking
        try:
            gen_result = generate_video(
                prompt=new_prompt, 
                image_path=image_path, 
                output_path=out_path,
                video_id=video.id
            )
        except Exception as e:
            
        # Update video status to failed
            video.status = "failed"
            video.updated_at = datetime.utcnow()
            db.commit()
            raise HTTPException(status_code=500, detail=str(e))

        # Mirror basic info onto UploadedImage for convenience
        image.video_path = out_path
        image.video_url = gen_result.get("video_url")
        image.video_generated_at = datetime.utcnow()
        db.commit()

        return {
            "image_id": image.id,
            "video_id": video.id,
            "iteration": video.iteration,
            "status": video.status,
            "video_path": video.video_path,
            "video_url": video.video_url,
        }
    finally:
        db.close()

from sqlalchemy.orm import joinedload
# ----------------------- ADMIN: LOGS & STATUS -----------------------
# @router.get("/admin/logs-status", tags=["Admin Portal"])
# def admin_logs_status():
#     """Return grouped and readable video processing logs with username and order_id quickly."""
#     db: Session = SessionLocal()
#     try:
#         now = datetime.now(timezone.utc)
#         response = {"queued": [], "processing": [], "succeeded": [], "failed": []}
        
#         # Loop through each status type
#         for status in ["queued", "processing", "succeeded", "failed"]:
#             # Select only needed fields
#             videos = (
#                 db.query(
#                     Video.id.label("video_id"),
#                     Order.id.label("order_id"),
#                     User.name.label("username"),
#                     User.email.label("email"),
#                     Video.created_at
#                 )
#                 .join(UploadedImage, Video.image_id == UploadedImage.id)
#                 .join(Order, UploadedImage.order_id == Order.id)
#                 .outerjoin(User, Order.user_id == User.id)
#                 .filter(Video.status == status)
#                 .order_by(Video.created_at.desc())
#                 .limit(20)
#                 .all()
#             )

#             for v in videos:
#                 for v in videos:
#                     print(v)
#                     # print(v.__dict__)
#                     break

#                 # Determine username to show
#                 if v.username:
#                     username = v.username
#                 elif v.email:
#                     username = v.email
#                 else:
#                     username = "Guest"
#                 response[status].append({
#                     "video_id": v.video_id,
#                     "order_id": v.order_id,
#                     "username": username,
#                     "email": v.email,
#                     "created_at": v.created_at.isoformat() if v.created_at else None
#                 })

#         # Add summary counts
#         summary = {
#             "queued": db.query(Video).filter(Video.status == "queued").count(),
#             "processing": db.query(Video).filter(Video.status == "processing").count(),
#             "succeeded": db.query(Video).filter(Video.status == "succeeded").count(),
#             "failed": db.query(Video).filter(Video.status == "failed").count(),
#         }

#         return {
#             "summary": summary,
#             "details": response,
#             "last_updated": now.isoformat()
#         }

#     finally:
#         db.close()
@router.get("/admin/logs-status", tags=["Admin Portal"])
def admin_logs_status():
    """Return grouped and readable video processing logs with client info."""
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        response = {"queued": [], "processing": [], "succeeded": [], "failed": []}

        for status in ["queued", "processing", "succeeded", "failed"]:
            videos = (
                db.query(Video)
                .join(UploadedImage, Video.image_id == UploadedImage.id)
                .join(Order, UploadedImage.order_id == Order.id)
                .join(User, Order.user_id == User.id, isouter=True)  # Left join for guests
                .filter(Video.status == status)
                .order_by(Video.created_at.desc())
                .limit(20)
                .all()
            )

            for v in videos:
                image = v.image
                order = image.order if image else None
                user = order.user if order else None

                created_at = v.created_at or now
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                elapsed_seconds = (now - created_at).total_seconds()
                elapsed_minutes = round(elapsed_seconds / 60, 1)

                response[status].append({
                    "video_id": v.id,
                    "prompt": v.prompt[:80] + "..." if len(v.prompt) > 80 else v.prompt,
                    "package": order.package if order else "Unknown",
                    "user_id": user.id if user else None,
                    "user_email": user.email if user else "Guest",
                    "user_name": user.name if user else "Guest User",
                    "user_code": _format_user_code(user) if user else None,
                    "created_at": v.created_at.isoformat() if v.created_at else None,
                    "elapsed_time": f"{elapsed_minutes} min ago",
                    "video_url": v.video_url,
                    "runway_job_id": v.runway_job_id,
                    "iteration": v.iteration,
                    "status": v.status,
                    "download_filename": f"video_{v.id}.mp4",
                })


        summary = {
            "queued": db.query(Video).filter(Video.status == "queued").count(),
            "processing": db.query(Video).filter(Video.status == "processing").count(),
            "succeeded": db.query(Video).filter(Video.status == "succeeded").count(),
            "failed": db.query(Video).filter(Video.status == "failed").count(),
        }

        return {
            "summary": summary,
            "details": response,
            "last_updated": now.isoformat()
        }

    finally:
        db.close()

# ----------------------- ADMIN: NOTIFICATIONS -----------------------
@router.get("/admin/notifications", tags=["Admin Portal"])
def admin_notifications():
    """Unified admin notifications for UI with user emails via relationship."""
    db = SessionLocal()
    try:
        notifications = []

        # 1️⃣ Video processing notifications
        failed_count = db.query(Video).filter(Video.status == "failed").count()
        processing_count = db.query(Video).filter(Video.status == "processing").count()
        succeeded_videos = (
            db.query(Video)
            .filter(Video.status == "succeeded")
            .order_by(Video.updated_at.desc())
            .limit(5)
            .all()
        )

        if failed_count:
            notifications.append({
                "type": "video_failed",
                "message": f"{failed_count} video jobs failed. Review and retry.",
                "category": "video_processing"
            })

        if processing_count:
            notifications.append({
                "type": "video_processing",
                "message": f"{processing_count} video jobs currently processing.",
                "category": "video_processing"
            })

        for v in succeeded_videos:
            u = _user_from_video(db, v)
            user_email = (u.email if u and u.email else "Guest")
            user_id = (u.id if u else None)
            user_code = _format_user_code(u) if u else None

            notifications.append({
                "type": "video_completed",
                "message": f"Video #{v.id} for order #{v.image.order_id} completed.",
                "video_id": v.id,
                "video_path": v.video_path,
                "image_id": v.image_id,
                "order_id": v.image.order_id,
                "user_id": user_id,
                "user_email": user_email,
                "user_code": user_code,
                "category": "video_processing"
            })

        # 2️⃣ Notifications from the Notification table (new users, etc.)
        recent_notifications = db.query(Notification).order_by(Notification.created_at.desc()).limit(20).all()
        for notif in recent_notifications:
            notif_user = notif.user if notif.user_id else None
            # Backfill: parse video id from message if user is missing
            if not notif_user:
                try:
                    m = re.search(r"Video\s+#(\d+)", notif.message or "")
                    if m:
                        vid = int(m.group(1))
                        v = db.query(Video).filter(Video.id == vid).first()
                        if v:
                            notif_user = _user_from_video(db, v)
                except Exception:
                    pass

            notifications.append({
                "type": notif.type,
                "message": notif.message,
                "user_id": (notif_user.id if notif_user else notif.user_id),
                "user_email": (notif_user.email if notif_user and notif_user.email else "Guest"),
                "user_code": _format_user_code(notif_user) if notif_user else None,
                "is_read": notif.is_read,
                "created_at": notif.created_at.isoformat(),
                "category": "system_notifications"
            })

        # 3️⃣ System stats
        total_users = db.query(User).count()
        total_orders = db.query(Order).count()
        total_videos = db.query(Video).count()

        notifications.append({
            "type": "system_stats",
            "message": f"System stats: {total_users} users, {total_orders} orders, {total_videos} videos",
            "stats": {
                "users": total_users,
                "orders": total_orders,
                "videos": total_videos
            },
            "category": "system_stats"
        })

        return {"notifications": notifications}
    finally:
        db.close()
        
@router.get("/admin/clients", tags=["Admin Portal"])
def get_all_clients(db: Session = Depends(get_db)):
    """
    Return all registered clients for the Admin Dashboard.
    Includes name, email, joined date, and total orders.
    """
    clients = db.query(User).filter(User.is_guest == False).all()

    response = []
    for client in clients:
        total_orders = db.query(Order).filter(Order.user_id == client.id).count()
        response.append({
            "user_id": client.id,
            "name": client.name or "N/A",
            "email": client.email,
            "joined": client.created_at.strftime("%Y-%m-%d") if client.created_at else None,
            "orders": total_orders
        })

    return {"clients": response, "count": len(response)}