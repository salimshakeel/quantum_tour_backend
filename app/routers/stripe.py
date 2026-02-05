from fastapi import APIRouter, HTTPException, Request, Depends
from pydantic import BaseModel
from typing import Optional, Dict, Any
import stripe
import json
import os
from datetime import datetime

from app.config import STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET
from app.models.database import SessionLocal, Payment, User, Order, UploadedImage
from app.routers.upload import IMAGES_DIR, process_videos_for_order
import threading, os

# Initialize Stripe
stripe.api_key = STRIPE_SECRET_KEY

router = APIRouter()

# Dependency to get database session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Pydantic models for request/response
class CheckoutSessionRequest(BaseModel):
    user_id: int
    order_id: Optional[int] = None
    amount: int  # Amount in cents
    currency: str = "usd"
    success_url: str
    cancel_url: str
    addon_type: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

class CheckoutSessionResponse(BaseModel):
    session_id: str
    url: str

class WebhookEvent(BaseModel):
    id: str
    object: str
    type: str

@router.post("/create-checkout-session", response_model=CheckoutSessionResponse)
async def create_checkout_session(
    request: CheckoutSessionRequest,
    db = Depends(get_db)
):
    """
    Create a Stripe Checkout Session for payment processing
    """
    try:
        # Verify user exists
        user = db.query(User).filter(User.id == request.user_id).first()
        if not user:
            print(f"❌ User not found with ID: {request.user_id}")
            # List available users for debugging
            all_users = db.query(User).all()
            print(f"Available users: {[{'id': u.id, 'email': u.email} for u in all_users]}")
            raise HTTPException(status_code=404, detail=f"User not found with ID: {request.user_id}")
        
        # Verify order exists if provided
        if request.order_id:
            order = db.query(Order).filter(Order.id == request.order_id).first()
            if not order:
                raise HTTPException(status_code=404, detail="Order not found")

        # Prepare metadata
        metadata = {
            "user_id": str(request.user_id),
            "addon_type": request.addon_type or "general"
        }
        if request.order_id:
            metadata["order_id"] = str(request.order_id)
        if request.metadata:
            metadata.update(request.metadata)

        # Create Stripe Checkout Session
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': request.currency,
                    'product_data': {
                        'name': f'Add-on: {request.addon_type or "General"}',
                        'description': 'Real Estate Video Service Add-on'
                    },
                    'unit_amount': request.amount,
                },
                'quantity': 1,
            }],
            mode='payment',
            success_url=request.success_url,
            cancel_url=request.cancel_url,
            metadata=metadata,
            customer_email=user.email if user.email else None,
        )

        # Store payment record in database
        payment = Payment(
            user_id=request.user_id,
            order_id=request.order_id,
            session_id=checkout_session.id,
            amount=request.amount,
            currency=request.currency,
            status="pending",
            payment_metadata=json.dumps(metadata) if metadata else None
        )
        db.add(payment)
        db.commit()

        return CheckoutSessionResponse(
            session_id=checkout_session.id,
            url=checkout_session.url
        )

    except stripe.error.StripeError as e:
        raise HTTPException(status_code=400, detail=f"Stripe error: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

@router.post("/webhook")
async def stripe_webhook(request: Request, db = Depends(get_db)):
    """
    Handle Stripe webhook events
    """
    try:
        # Get the raw body and signature
        payload = await request.body()
        sig_header = request.headers.get('stripe-signature')
        
        if not sig_header:
            raise HTTPException(status_code=400, detail="Missing stripe-signature header")
        
        if not STRIPE_WEBHOOK_SECRET:
            raise HTTPException(status_code=500, detail="Stripe webhook secret not configured")

        # Verify webhook signature
        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, STRIPE_WEBHOOK_SECRET
            )
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid payload")
        except stripe.error.SignatureVerificationError:
            raise HTTPException(status_code=400, detail="Invalid signature")

        # Handle the event
        if event['type'] == 'checkout.session.completed':
            session = event['data']['object']
            await handle_checkout_session_completed(session, db)
        elif event['type'] == 'payment_intent.succeeded':
            payment_intent = event['data']['object']
            await handle_payment_intent_succeeded(payment_intent, db)
        elif event['type'] == 'payment_intent.payment_failed':
            payment_intent = event['data']['object']
            await handle_payment_intent_failed(payment_intent, db)
        else:
            print(f"Unhandled event type: {event['type']}")

        return {"status": "success"}

    except HTTPException:
        raise
    except Exception as e:
        print(f"Webhook error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Webhook processing error: {str(e)}")

async def handle_checkout_session_completed(session: Dict[str, Any], db):
    """
    Handle successful checkout session completion
    """
    try:
        session_id = session['id']
        
        # Find the payment record
        payment = db.query(Payment).filter(Payment.session_id == session_id).first()
        if not payment:
            print(f"Payment not found for session_id: {session_id}")
            return
        
        # Update payment status
        payment.status = "succeeded"
        payment.stripe_payment_intent_id = session.get('payment_intent')
        payment.updated_at = datetime.utcnow()
        
        # Update order status if applicable
        if payment.order_id:
            order = db.query(Order).filter(Order.id == payment.order_id).first()
            if order:
                # You might want to add an order status field to track payment status
                # For now, we'll just log it
                print(f"Order {order.id} payment completed")
        
        db.commit()
        print(f"Payment {payment.id} marked as succeeded")

        # If this was a reorder, automatically start processing the new order
        try:
            md = session.get('metadata') or {}
            if md.get('addon_type') == 'reorder' and md.get('order_id') and md.get('user_id'):
                new_order_id = int(md['order_id'])
                user_id = int(md['user_id'])
                new_order = db.query(Order).filter(Order.id == new_order_id).first()
                
                # ==================== DUPLICATE PROTECTION ====================
                # Check if order is already being processed or was already processed
                if new_order:
                    if getattr(new_order, 'is_processing', False):
                        print(f"[SKIP WEBHOOK] Order {new_order_id} is already being processed. Skipping duplicate webhook.")
                        return
                    if getattr(new_order, 'processed_at', None) is not None:
                        print(f"[SKIP WEBHOOK] Order {new_order_id} was already processed. Skipping duplicate webhook.")
                        return
                # ==============================================================
                
                if new_order and new_order.parent_order_id:
                    old_images = db.query(UploadedImage).filter(UploadedImage.order_id == new_order.parent_order_id).all()
                    saved_files = []
                    ts = int(datetime.utcnow().timestamp())
                    for img in old_images:
                        try:
                            filename = img.filename or f"image_{img.id}.jpg"
                            dst_path = os.path.join(IMAGES_DIR, f"reorder_{user_id}_{ts}_{filename}")
                            if img.content:
                                with open(dst_path, 'wb') as f:
                                    f.write(img.content)
                                saved_files.append(dst_path)
                        except Exception:
                            continue
                    if saved_files:
                        threading.Thread(
                            target=process_videos_for_order,
                            args=(new_order_id, saved_files, user_id),
                            daemon=True
                        ).start()
        except Exception as auto_err:
            print(f"Auto-processing after reorder payment failed to start: {auto_err}")
        
    except Exception as e:
        print(f"Error handling checkout session completed: {str(e)}")
        db.rollback()

async def handle_payment_intent_succeeded(payment_intent: Dict[str, Any], db):
    """
    Handle successful payment intent
    """
    try:
        payment_intent_id = payment_intent['id']
        
        # Find payment by payment intent ID
        payment = db.query(Payment).filter(
            Payment.stripe_payment_intent_id == payment_intent_id
        ).first()
        
        if payment and payment.status != "succeeded":
            payment.status = "succeeded"
            payment.updated_at = datetime.utcnow()
            db.commit()
            print(f"Payment {payment.id} confirmed as succeeded")
        
    except Exception as e:
        print(f"Error handling payment intent succeeded: {str(e)}")
        db.rollback()

async def handle_payment_intent_failed(payment_intent: Dict[str, Any], db):
    """
    Handle failed payment intent
    """
    try:
        payment_intent_id = payment_intent['id']
        
        # Find payment by payment intent ID
        payment = db.query(Payment).filter(
            Payment.stripe_payment_intent_id == payment_intent_id
        ).first()
        
        if payment:
            payment.status = "failed"
            payment.updated_at = datetime.utcnow()
            db.commit()
            print(f"Payment {payment.id} marked as failed")
        
    except Exception as e:
        print(f"Error handling payment intent failed: {str(e)}")
        db.rollback()

@router.get("/payment-status/{session_id}")
async def get_payment_status(session_id: str, db = Depends(get_db)):
    """
    Get payment status by session ID
    """
    payment = db.query(Payment).filter(Payment.session_id == session_id).first()
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    
    return {
        "session_id": payment.session_id,
        "status": payment.status,
        "amount": payment.amount,
        "currency": payment.currency,
        "created_at": payment.created_at,
        "updated_at": payment.updated_at
    }
