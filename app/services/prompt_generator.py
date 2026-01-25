import os
import base64
from openai import OpenAI
import traceback
# Load credentials
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_PROJECT_ID = os.getenv("OPENAI_PROJECT", "").strip() # or OPENAI_PROJECT_ID

if not OPENAI_API_KEY or not OPENAI_PROJECT_ID:
    raise RuntimeError("❌ Missing OpenAI credentials. Please set OPENAI_API_KEY and OPENAI_PROJECT.")

# Init client


def _encode_image_to_data_url(image_path: str) -> str:
    """Read local image and return data URL for OpenAI vision input."""
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    ext = os.path.splitext(image_path)[1].lower().lstrip(".") or "jpeg"
    mime = "image/jpeg" if ext in {"jpg", "jpeg"} else "image/png"
    return f"data:{mime};base64,{b64}"

def generate_cinematic_prompt_from_image(image_path: str) -> str:
    """Generate a short cinematic prompt directly with OpenAI Vision."""

    # Lazy-load API credentials
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
    OPENAI_PROJECT_ID = os.getenv("OPENAI_PROJECT", "").strip()

    if not OPENAI_API_KEY or not OPENAI_PROJECT_ID:
        raise RuntimeError("❌ Missing OpenAI credentials. Please set OPENAI_API_KEY and OPENAI_PROJECT.")

    client = OpenAI(api_key=OPENAI_API_KEY, project=OPENAI_PROJECT_ID)

    image_data_url = _encode_image_to_data_url(image_path)

    try:
        resp = client.chat.completions.create(
            model="gpt-4o",  # ✅ vision model
            temperature=0.8,
            max_tokens=120,
          messages = [
    {
        "role": "system",
        "content": (
            "You are generating simple, reliable video prompts for RunwayML based on real estate images. "
            "Given a single image, output a short, scene-aware camera movement prompt. "
            "Rules: "
            "1. Prefer ONE primary camera movement. "
            "   Examples: push in, pull back, orbit, slide. "
            "   Only combine movements when proven stable (e.g., push in and subtle rotation). "
            "2. Use correct camera logic: "
            "   INTERIOR: dolly or steadicam only. "
            "   EXTERIOR: drone-style movement allowed, but keep it simple. "
            "3. Be scene-aware. "
            "   Reference only what is clearly visible in the image. "
            "   If doors or openings are visible, state what rooms they lead to. "
            "   Never imply or invent unseen rooms or objects. "
            "4. Motion wording must be precise. "
            "   Use 'and' when combining movements. "
            "   Avoid vague terms like 'drift,' 'float,' or 'cinematic sweep.' "
            "5. Keep prompts short, clean, and literal. "
            "   The goal is consistency and control, not flair. "
            "Examples: "
            "- Interior Bedroom: Slow push in toward the bed, keeping the bed centered as the focal point. "
            "- Interior Living Room: Slow push in and subtle rotation left toward the seating area and fireplace. "
            "- Interior Kitchen: Push in and gentle orbit around the kitchen island. "
            "- Exterior Front: Slow push in toward the front of the house, centered on the entryway. "
            "- Exterior Aerial: Slow push in and gentle descent toward the house. "
            "- Patio: Slow push in toward the outdoor seating area. "
        )
    },
    {
        "role": "user",
        "content": [
            {"type": "text", "text": "Analyze this image and output the video prompt."},
            {"type": "image_url", "image_url": {"url": image_data_url}},
        ],
    },
]

        )
        return (resp.choices[0].message.content or "").strip()

    except Exception as e:
        print("❌ OpenAI API Error:", str(e))
        traceback.print_exc()
        return "Short, cinematic shot with warm lighting and smooth, elegant camera movement."


def improve_prompt_with_feedback(original_prompt: str, feedback_text: str) -> str:
    """Given an original prompt and user feedback, return a refined prompt.

    Keeps the core idea but integrates the feedback succinctly for image-to-video models.
    """
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
    OPENAI_PROJECT_ID = os.getenv("OPENAI_PROJECT", "").strip()

    if not OPENAI_API_KEY or not OPENAI_PROJECT_ID:
        # Fall back to a simple deterministic merge
        merged = f"{original_prompt} Incorporate this revision: {feedback_text}."
        return merged.strip()

    client = OpenAI(api_key=OPENAI_API_KEY, project=OPENAI_PROJECT_ID)

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.6,
            max_tokens=160,
            messages=[
                {
                    "role": "system",
                    "content": "You improve prompts for an image-to-video model. Rewrite the prompt concisely (<= 2 sentences), keep the original intent, strictly integrate the feedback, avoid filler, and avoid technical camera jargon unless explicitly requested.",
                },
                {
                    "role": "user",
                    "content": f"Original prompt: {original_prompt}\nUser feedback: {feedback_text}\nRewrite the prompt.",
                },
            ],
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception:
        merged = f"{original_prompt} Incorporate this revision: {feedback_text}."
        return merged.strip()