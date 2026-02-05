from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from passlib.context import CryptContext
from datetime import datetime, timedelta
from typing import Optional
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from app.models.database import SessionLocal, Admin  # Your DB model
import os

# Router for authentication
router = APIRouter(tags=["Admin Auth"])

# ---------------- CONFIG ----------------
SECRET_KEY = os.getenv("ADMIN_SECRET_KEY")
if not SECRET_KEY:
    raise ValueError("ADMIN_SECRET_KEY environment variable is required for admin authentication")

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60
REFRESH_TOKEN_EXPIRE_DAYS = 7

# Password hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# OAuth2 scheme
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/admin/login")


# ---------------- UTILS ----------------
def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)

def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=15))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def create_refresh_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

# DB Dependency
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------- SCHEMAS ----------------
class AdminRegister(BaseModel):
    email: EmailStr
    password: str


# ---------------- ROUTES ----------------
@router.post("/admin/register")
def register_admin(data: AdminRegister, db: Session = Depends(get_db)):
    """Register a new admin"""
    existing_admin = db.query(Admin).filter(Admin.email == data.email).first()
    if existing_admin:
        raise HTTPException(status_code=400, detail="Email already registered")

    hashed_password = get_password_hash(data.password)
    new_admin = Admin(email=data.email, password_hash=hashed_password)

    db.add(new_admin)
    db.commit()
    db.refresh(new_admin)

    return {"message": "✅ Admin registered successfully", "email": new_admin.email}


@router.post("/admin/login")
def admin_login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    """Admin login with email + password → returns JWT access & refresh tokens"""
    admin = db.query(Admin).filter(Admin.email == form_data.username).first()
    if not admin or not verify_password(form_data.password, admin.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    
    # Create tokens
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": admin.email, "role": "admin"}, expires_delta=access_token_expires
    )

    refresh_token_expires = timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    refresh_token = create_refresh_token(
        data={"sub": admin.email, "role": "admin"}, expires_delta=refresh_token_expires
    )

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer"
    }


@router.post("/admin/refresh")
def refresh_token(refresh_token: str):
    """Generate a new access token from a valid refresh token"""
    try:
        payload = jwt.decode(refresh_token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise HTTPException(status_code=401, detail="Invalid refresh token")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    new_access_token = create_access_token(
        data={"sub": email, "role": "admin"}, expires_delta=access_token_expires
    )

    return {"access_token": new_access_token, "token_type": "bearer"}


@router.post("/admin/logout")
def logout():
    """Client should delete tokens on logout (real logout = blacklist refresh token in DB)"""
    return {"message": "Logged out successfully. Please discard your tokens."}


# ---------------- PROTECTED DEPENDENCY ----------------
def get_current_admin(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    """Protect routes: ensures valid admin JWT"""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        role: str = payload.get("role")
        if email is None or role != "admin":
            raise HTTPException(status_code=401, detail="Invalid credentials")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    admin = db.query(Admin).filter(Admin.email == email).first()
    if not admin:
        raise HTTPException(status_code=401, detail="Admin not found")

    return admin
