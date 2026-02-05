import os
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "..", "uploaded_images")

# Create upload folder if it doesn't exist
os.makedirs(UPLOAD_DIR, exist_ok=True)
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_PROJECT = os.getenv("OPENAI_PROJECT", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

RUNWAY_API_KEY = os.getenv("RUNWAY_API_KEY")
# Public API uses the dev host per Runway docs/error message
RUNWAY_API_URL = os.getenv("RUNWAY_API_URL", "https://api.dev.runwayml.com/v1")

# JWT Secret - Required for security
SECRET_KEY = os.getenv("JWT_SECRET")
if not SECRET_KEY:
    raise ValueError("JWT_SECRET environment variable is required for secure authentication")

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60  # 1 hour

# Stripe Configuration
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()


# ✅ Dropbox credentials
DROPBOX_APP_KEY = os.getenv("DROPBOX_APP_KEY")
DROPBOX_APP_SECRET = os.getenv("DROPBOX_APP_SECRET")
DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN")

if not RUNWAY_API_KEY:
    print("⚠️ WARNING: RUNWAY_API_KEY is not set!")

if not STRIPE_SECRET_KEY:
    print("⚠️ WARNING: STRIPE_SECRET_KEY is not set!")