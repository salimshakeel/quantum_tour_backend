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
            "You are given a real estate photo. "
            "Your task is to generate a short Runway video prompt that turns this photo into a video. "
            "Look at the image to identify the main visible subject and whether the scene is interior or exterior. "
            "Output EXACTLY ONE line using this format: <camera movement> toward <visible subject> "
            "Use ONLY these camera movements: "
            "• push in "
            "• push in and subtle rotation "
            "• gentle orbit "
            "• push in and slight descent. "
            "Rules: "
            "- Keep under 15 words. "
            "- Reference only what is visible in the photo. "
            "- Never invent rooms, objects, or views. "
            "- Use 'push in and slight descent' ONLY when the image supports a drone-style or elevated approach. "
            "- If doors or openings are visible, name the rooms they lead to (bedroom, bathroom, kitchen, hallway) without description. "
            "- Use 'and' when combining movements."
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