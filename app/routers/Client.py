from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Form, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy.sql import func
from typing import List, Optional
from datetime import datetime
import shutil, os
from app.routers.auth import get_current_user
from app.routers.upload import IMAGES_DIR, process_videos_for_order, slugify_path_component
from app.config import STRIPE_SECRET_KEY
import stripe
stripe.api_key = STRIPE_SECRET_KEY
import re

from app.models.database import SessionLocal, Order, UploadedImage, Video, Invoice, User , Payment, FinalVideo

router = APIRouter(tags=["Client Portal"])

# ---------------- DB Dependency ----------------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------- CLIENT: STATUS ----------------

@router.get("/whoami")
def whoami(current_user: User = Depends(get_current_user)):
    """Return the authenticated user's basic info for quick debugging."""
    return {
        "id": current_user.id,
        "name": current_user.name,
        "email": current_user.email
    }

@router.get("/client/status")
def client_status(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """
    Returns whether the user has any orders and basic info about them,
    including related videos (if available).
    """
    orders = db.query(Order).filter(Order.user_id == current_user.id).all()

    if not orders:
        return {
            "has_orders": False,
            "user_email": current_user.email,
            "user_name": current_user.name
        }

    response_orders = []

    for order in orders:
        # get all uploaded images for this order
        images_data = []
        for img in order.images:
            # collect related video info
            videos = db.query(Video).filter(Video.image_id == img.id).all()
            videos_data = [
                {
                    "id": v.id,
                    "prompt": v.prompt,
                    "status": v.status,
                    "video_url": v.video_url,
                    "created_at": v.created_at
                }
                for v in videos
            ]

            images_data.append({
                "id": img.id,
                "filename": img.filename,
                "video_url": img.video_url,
                "video_generated_at": img.video_generated_at,
                "videos": videos_data
            })

        response_orders.append({
            "order_id": order.id,
            "package": order.package,
            "add_ons": order.add_ons,
            "created_at": order.created_at,
            "images": images_data
        })

    return {
        "has_orders": True,
        "user_email": current_user.email,
        "user_name": current_user.name,
        "orders": response_orders
    }


# ---------------- 1. DOWNLOAD CENTER ----------------
# @router.get("/download-center")
# def get_download_center(
#     current_user: User = Depends(get_current_user),
#     db: Session = Depends(get_db)
# ):
#     """
#     Returns only final (completed/succeeded) videos for the currently logged-in client.
#     """
#     user_id = current_user.id

#     # Get all orders for this user
#     orders = (
#         db.query(Order)
#         .filter(Order.user_id == user_id)
#         .order_by(Order.created_at.desc())
#         .all()
#     )

#     response = []

#     for order in orders:
#         # Get latest completed/succeeded videos for images in this order
#         latest_video_subq = (
#             db.query(
#                 Video.image_id.label("image_id"),
#                 func.max(Video.iteration).label("max_iter")
#             )
#             .group_by(Video.image_id)
#             .subquery()
#         )

#         completed_videos = (
#             db.query(Video)
#             .join(
#                 latest_video_subq,
#                 (Video.image_id == latest_video_subq.c.image_id) &
#                 (Video.iteration == latest_video_subq.c.max_iter)
#             )
#             .join(UploadedImage, UploadedImage.id == Video.image_id)
#             .filter(
#                 UploadedImage.order_id == order.id,
#                 Video.status.in_(["completed", "succeeded"])
#             )
#             .all()
#         )

#         # Build response for each completed video
#         videos_info = []
#         for v in completed_videos:
#             if v.video_url:
#                 # Convert Dropbox URL to direct link (so frontend can preview/download)
#                 direct_url = v.video_url.replace("?dl=0", "?raw=1")
#                 videos_info.append({
#                     "filename": v.video_path.split("/")[-1] if v.video_path else None,
#                     "direct_url": direct_url,
#                     "dropbox_url": v.video_url,
#                     "status": v.status
#                 })

#         if videos_info:
#             response.append({
#                 "order_id": order.id,
#                 "package": order.package,
#                 "add_ons": order.add_ons,
#                 "date": order.created_at.isoformat(),
#                 "videos": videos_info
#             })

#     return {
#         "user_email": current_user.email,
#         "downloads": response,
#         "count": len(response)
#     }

@router.get("/download-center")
def get_download_center(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    user_id = current_user.id

    final_videos = (
        db.query(FinalVideo)
        .filter(FinalVideo.user_id == user_id)
        .order_by(FinalVideo.created_at.desc())
        .all()
    )

    downloads = [
        {
            "video_id": v.id,
            "filename": os.path.basename(v.dropbox_path),
            "url": v.video_url,
            "created_at": v.created_at.isoformat() if v.created_at else None
        }
        for v in final_videos
    ]

    return {
        "user_email": current_user.email,
        "downloads": downloads,
        "count": len(downloads)
    }


# ---------------- 2. NEW ORDER ----------------
@router.post("/orders/new")
async def create_new_order(
    user_id: int = Form(...),
    package: str = Form(...),
    add_ons: Optional[str] = Form(None),
    files: List[UploadFile] = File(...),
    db: Session = Depends(get_db)
):
    """Create a new order + upload images + generate invoice."""
    order = Order(user_id=user_id, package=package, add_ons=add_ons)
    db.add(order)
    db.commit()
    db.refresh(order)

    # Save uploaded images
    upload_dir = "uploads"
    os.makedirs(upload_dir, exist_ok=True)

    for file in files:
        file_path = os.path.join(upload_dir, file.filename)
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        image = UploadedImage(
            order_id=order.id,
            filename=file.filename,
            upload_time=datetime.utcnow()
        )
        db.add(image)

    db.commit()

    # Create invoice
    invoice = Invoice(order_id=order.id, user_id=user_id, amount=100, is_paid=False)
    db.add(invoice)
    db.commit()
    db.refresh(invoice)

    return {
        "message": "Order created successfully",
        "order": {
            "id": order.id,
            "package": order.package,
            "add_ons": order.add_ons,
            "date": order.created_at.isoformat(),
        },
        "invoice": {
            "id": invoice.id,
            "amount": invoice.amount,
            "status": "unpaid"
        }
    }
@router.get("/orders/status")
def get_client_orders(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Fetch all orders for the currently authenticated client 
    with their current status (submitted, processing, completed).
    """
    user_id = current_user.id  # Automatically detect user from token/session

    orders = (
        db.query(Order)
        .filter(Order.user_id == user_id)
        .order_by(Order.created_at.desc())
        .all()
    )

    if not orders:
        raise HTTPException(status_code=44, detail="No orders found for this client.")

    # Subquery to get only the latest video per image
    latest_video_subq = (
        db.query(Video.image_id, func.max(Video.iteration).label("max_iter"))
        .group_by(Video.image_id)
        .subquery()
    )

    response = []
    for order in orders:
        images = db.query(UploadedImage).filter(UploadedImage.order_id == order.id).all()

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
        statuses = [v.status for v in image_id_to_video.values()]

        if statuses and all(s == "succeeded" for s in statuses):
            status = "completed"
        elif any(s == "processing" for s in statuses):
            status = "processing"
        else:
            status = "submitted"


        response.append({
            "order_id": order.id,
            "package": order.package,
            "add_ons": order.add_ons,
            "status": status,
            "date": order.created_at.isoformat(),
            "videos": [
                {
                    "filename": v.video_path.split("/")[-1] if v.video_path else None,
                    "url": v.video_url or "",
                    "status": v.status,
                }
                for v in image_id_to_video.values()
            ],
        })

    return {"orders": response, "count": len(response)}
# ---------------- 3. REORDER ----------------
@router.post("/orders/{order_id}/reorder")
def reorder(
    order_id: int,
    background_tasks: BackgroundTasks,
    success_url: str = Form(...),
    cancel_url: str = Form(...),
    amount: int = Form(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Reorder: create a new order linked to a previous one.
    - Verifies ownership
    - Creates a new order linked via parent_order_id
    - Initiates Stripe Checkout for payment (no re-upload/no package selection)
    - Processing starts automatically after Stripe webhook confirms success
    """
    old_order = db.query(Order).filter(Order.id == order_id).first()
    if not old_order:
        raise HTTPException(status_code=404, detail="Order not found")
    if old_order.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not allowed to reorder this order")

    new_order = Order(
        user_id=old_order.user_id,
        package=old_order.package,
        add_ons=old_order.add_ons,
        parent_order_id=old_order.id
    )
    db.add(new_order)
    db.commit()
    db.refresh(new_order)

    # Create Stripe Checkout session for this reorder
    metadata = {
        "user_id": str(current_user.id),
        "order_id": str(new_order.id),
        "addon_type": "reorder"
    }
    checkout_session = stripe.checkout.Session.create(
        payment_method_types=['card'],
        line_items=[{
            'price_data': {
                'currency': 'usd',
                'product_data': {
                    'name': 'Reorder',
                    'description': 'Reorder processing fee'
                },
                'unit_amount': amount,
            },
            'quantity': 1,
        }],
        mode='payment',
        success_url=success_url,
        cancel_url=cancel_url,
        metadata=metadata,
        customer_email=current_user.email if current_user.email else None,
    )

    return {
        "message": "Reorder created successfully. Proceed to payment.",
        "order": {
            "id": new_order.id,
            "linked_to": old_order.id,
            "package": new_order.package,
            "add_ons": new_order.add_ons
        },
        "checkout": {
            "session_id": checkout_session.id,
            "url": checkout_session.url,
            "amount": amount
        }
    }

@router.get("/invoices")
def get_client_invoices(
    current_user = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Returns all invoices for the logged-in user,
    including Stripe payment info and order details.
    """
    invoices = (
        db.query(Invoice)
        .filter(Invoice.user_id == current_user.id)
        .all()
    )

    if not invoices:
        raise HTTPException(status_code=404, detail="No invoices found")

    response = []

    for inv in invoices:
        payment = (
            db.query(Payment)
            .filter(Payment.order_id == inv.order_id)
            .order_by(Payment.created_at.desc())
            .first()
        )

        order = db.query(Order).filter(Order.id == inv.order_id).first()

        response.append({
            "invoice_id": inv.id,
            "order_id": inv.order_id,
            "amount": inv.amount,
            "currency": payment.currency if payment else "usd",
            "status": payment.status if payment else inv.status,
            "is_paid": True if (payment and payment.status == "succeeded") else False,
            "created_at": inv.created_at,
            "due_date": inv.due_date,
            "order_info": {
                "package": order.package if order else None,
                "addons": order.add_ons if order else None,
                "created_at": order.created_at if order else None
            },
            "stripe_metadata": payment.payment_metadata if payment else None,
        })

    return {"user": current_user.email, "invoices": response}

from fastapi import APIRouter, UploadFile, File, HTTPException, Form
from dotenv import load_dotenv
import os
import dropbox
from datetime import datetime
import json

# Load environment variables
load_dotenv()

DROPBOX_APP_KEY = os.getenv("DROPBOX_APP_KEY")
DROPBOX_APP_SECRET = os.getenv("DROPBOX_APP_SECRET")
DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN")
DROPBOX_FOLDER_PATH = "/quantumtour"



def get_dropbox_access_token():
    """Generate Dropbox access token using refresh token."""
    import requests
    url = "https://api.dropboxapi.com/oauth2/token"
    data = {
        "grant_type": "refresh_token",
        "refresh_token": DROPBOX_REFRESH_TOKEN,
        "client_id": DROPBOX_APP_KEY,
        "client_secret": DROPBOX_APP_SECRET,
    }
    response = requests.post(url, data=data)
    if response.status_code != 200:
        raise HTTPException(status_code=400, detail=f"Dropbox token refresh failed: {response.text}")
    return response.json()["access_token"]

@router.post("/brand_assets")
async def upload_brand_asset(
    file: UploadFile = File(...),
    primary_color: Optional[str] = Form(None),
    font_family: Optional[str] = Form(None),
    # Accept common camelCase variants from some frontends
    primaryColor: Optional[str] = Form(None),
    fontFamily: Optional[str] = Form(None),
    current_user: User = Depends(get_current_user)
):
    """
    Upload a brand logo to Dropbox and return the shareable URL.
    """
    try:
        # Normalize possible camelCase field names to snake_case
        if not primary_color and primaryColor:
            primary_color = primaryColor
        if not font_family and fontFamily:
            font_family = fontFamily

        # Sanitize/normalize color into #RRGGBB if possible
        def _rgb_to_hex(rgb: str) -> Optional[str]:
            m = re.match(r"rgb\\(\\s*(\\d{1,3})\\s*,\\s*(\\d{1,3})\\s*,\\s*(\\d{1,3})\\s*\\)", rgb, flags=re.IGNORECASE)
            if not m:
                return None
            r, g, b = (max(0, min(255, int(x))) for x in m.groups())
            return f"#{r:02x}{g:02x}{b:02x}"

        if isinstance(primary_color, str):
            pc = primary_color.strip()
            if pc.lower().startswith("rgb"):
                hex_pc = _rgb_to_hex(pc)
                if hex_pc:
                    primary_color = hex_pc
            elif re.fullmatch(r"#?[0-9a-fA-F]{6}", pc):
                primary_color = pc if pc.startswith("#") else f"#{pc}"
            # else leave as-is; Dropbox JSON will store what we received

        # Debug log to quickly verify what server received
        print(f"[BrandAssets] Received → primary_color={primary_color}, font_family={font_family}, file={getattr(file, 'filename', None)}")

        # Initialize Dropbox client using refresh token (long-lived)
        if not all([DROPBOX_APP_KEY, DROPBOX_APP_SECRET, DROPBOX_REFRESH_TOKEN]):
            raise HTTPException(status_code=500, detail="Missing Dropbox credentials in environment")
        dbx = dropbox.Dropbox(
            app_key=DROPBOX_APP_KEY,
            app_secret=DROPBOX_APP_SECRET,
            oauth2_refresh_token=DROPBOX_REFRESH_TOKEN,
            timeout=300
        )

        # Build agent folder from user's email local-part, else name, else user_{id}
        if current_user and getattr(current_user, "email", None):
            agent_folder = current_user.email.split("@")[0]
        else:
            agent_folder = (current_user.name if getattr(current_user, "name", None) else f"user_{current_user.id}")
        base_folder = f"{DROPBOX_FOLDER_PATH}/{agent_folder}/brand_assets"
        dropbox_path = f"{base_folder}/{file.filename}"

        # Ensure destination folder exists: /quantumtour/{agent}/brand_assets
        # Agent folder should already exist; create only the brand_assets subfolder
        try:
            dbx.files_create_folder_v2(base_folder)
        except dropbox.exceptions.ApiError:
            pass  # likely exists

        # Upload the file with retries
        file_bytes = file.file.read()
        last_err = None
        for attempt in range(1, 4):
            try:
                dbx.files_upload(file_bytes, dropbox_path, mode=dropbox.files.WriteMode.overwrite)
                break
            except Exception as e:
                last_err = e
                print(f"[BrandAssets] Upload attempt {attempt} failed: {e}")
                if attempt == 3:
                    raise

        # Obtain a temporary link (does not require sharing scopes)
        public_url = None
        try:
            tmp = None
            for attempt in range(1, 3):
                try:
                    tmp = dbx.files_get_temporary_link(dropbox_path)
                    break
                except Exception as e:
                    print(f"[BrandAssets] Temp link attempt {attempt} failed: {e}")
                    if attempt == 2:
                        raise
            public_url = tmp.link if tmp else None
        except dropbox.exceptions.ApiError:
            public_url = None

        # Update or create branding.json alongside the asset with color/font metadata
        branding_json_path = f"{base_folder}/branding.json"
        branding = {
            "primary_color": None,
            "font_family": None,
            "logos": []
        }
        try:
            md, res = None, None
            for attempt in range(1, 3):
                try:
                    md, res = dbx.files_download(branding_json_path)
                    break
                except dropbox.exceptions.ApiError as e:
                    # likely not found; treat as new branding
                    md, res = None, None
                    break
                except Exception as e:
                    print(f"[BrandAssets] Download branding.json attempt {attempt} failed: {e}")
                    if attempt == 2:
                        raise
            if res and getattr(res, "content", None):
                existing = res.content.decode("utf-8")
                branding = json.loads(existing)
                if not isinstance(branding, dict):
                    branding = {"primary_color": None, "font_family": None, "logos": []}
        except dropbox.exceptions.ApiError:
            # file may not exist yet; proceed with default branding
            pass

        # Set metadata if provided (keep existing otherwise)
        if primary_color:
            branding["primary_color"] = primary_color
        if font_family:
            branding["font_family"] = font_family

        # Append uploaded logo reference
        branding.setdefault("logos", [])
        branding["logos"].append({
            "path": dropbox_path,
            "filename": file.filename,
            "uploaded_at": datetime.utcnow().isoformat()
        })

        # Upload/overwrite branding.json
        branding_bytes = json.dumps(branding, ensure_ascii=False, indent=2).encode("utf-8")
        last_err = None
        for attempt in range(1, 3):
            try:
                dbx.files_upload(
                    branding_bytes,
                    branding_json_path,
                    mode=dropbox.files.WriteMode.overwrite
                )
                break
            except Exception as e:
                last_err = e
                print(f"[BrandAssets] Upload branding.json attempt {attempt} failed: {e}")
                if attempt == 2:
                    raise

        return {
            "message": "✅ Brand asset uploaded successfully!",
            "file_name": file.filename,
            "dropbox_path": dropbox_path,
            "dropbox_url": public_url,
            "branding_json_path": branding_json_path,
            "branding": branding,
            "uploaded_at": datetime.utcnow().isoformat()
        }

    except dropbox.exceptions.ApiError as e:
        raise HTTPException(status_code=400, detail=f"Dropbox error: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {str(e)}")
