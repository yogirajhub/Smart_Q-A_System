"""
Smart Q&A System - FastAPI Backend
Features:
- File upload (TXT, PDF, DOCX)
- Groq LLM Q&A based on file content
- Text-to-Speech responses using gTTS
- Out-of-scope question detection + hallucination warning
- Unanswered questions tracking
- Admin panel to manually answer questions
- Knowledge base that persists admin answers for future queries
"""

from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from starlette.requests import Request
from groq import Groq
from gtts import gTTS
from dotenv import load_dotenv
from pydantic import BaseModel
import os, json, uuid, shutil
from datetime import datetime
import PyPDF2
import docx as python_docx

# ─────────────────────────────────────────────
# Load environment variables
# ─────────────────────────────────────────────
load_dotenv()

GROQ_API_KEY  = os.getenv("GROQ_API_KEY")
GROQ_MODEL    = os.getenv("GROQ_MODEL", "llama3-8b-8192")
TTS_LANGUAGE  = os.getenv("TTS_LANGUAGE", "en")
HOST          = os.getenv("APP_HOST", "0.0.0.0")
PORT          = int(os.getenv("APP_PORT", "8000"))

if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY not found in .env file. Please set it.")

client = Groq(api_key=GROQ_API_KEY)

# ─────────────────────────────────────────────
# Directory & file paths
# ─────────────────────────────────────────────
BASE_DIR            = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR          = os.path.join(BASE_DIR, "uploads")
AUDIO_DIR           = os.path.join(BASE_DIR, "audio_files")
UNANSWERED_FILE     = os.path.join(BASE_DIR, "unanswered_questions.json")
KNOWLEDGE_BASE_FILE = os.path.join(BASE_DIR, "knowledge_base.json")
SESSION_FILE        = os.path.join(BASE_DIR, "session_data.json")

for d in [UPLOAD_DIR, AUDIO_DIR,
          os.path.join(BASE_DIR, "static"),
          os.path.join(BASE_DIR, "templates")]:
    os.makedirs(d, exist_ok=True)

# ─────────────────────────────────────────────
# FastAPI app setup
# ─────────────────────────────────────────────
app = FastAPI(title="Smart Q&A System", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static",  StaticFiles(directory=os.path.join(BASE_DIR, "static")),     name="static")
app.mount("/audio",   StaticFiles(directory=AUDIO_DIR),    name="audio")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# ─────────────────────────────────────────────
# JSON helpers
# ─────────────────────────────────────────────
def load_json(path: str, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if not content:          # file exists but is empty
                    return default
                return json.loads(content)
        except (json.JSONDecodeError, OSError):
            return default              # corrupted file → return default
    return default

def save_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# ─────────────────────────────────────────────
# Session (persists across server restarts)
# ─────────────────────────────────────────────
def get_session() -> dict:
    return load_json(SESSION_FILE, {
        "file_path": "", "filename": "", "file_content": ""
    })

def save_session(data: dict):
    save_json(SESSION_FILE, data)

# ─────────────────────────────────────────────
# File text extraction
# ─────────────────────────────────────────────
def extract_text(file_path: str, filename: str) -> str:
    ext = filename.lower().rsplit(".", 1)[-1]
    try:
        if ext == "pdf":
            text = ""
            with open(file_path, "rb") as f:
                reader = PyPDF2.PdfReader(f)
                for page in reader.pages:
                    extracted = page.extract_text()
                    if extracted:
                        text += extracted + "\n"
            return text.strip() or "PDF parsed but no readable text found."
        elif ext == "docx":
            doc = python_docx.Document(file_path)
            lines = [p.text for p in doc.paragraphs if p.text.strip()]
            return "\n".join(lines)
        else:  # .txt and others
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
    except Exception as e:
        return f"Error reading file: {e}"

# ─────────────────────────────────────────────
# Build full LLM context (file + knowledge base)
# ─────────────────────────────────────────────
def get_full_context(file_content: str) -> str:
    kb = load_json(KNOWLEDGE_BASE_FILE, [])
    if not kb:
        return file_content
    extra = "\n\n═══ Admin Knowledge Base (Q&A additions) ═══\n"
    for item in kb:
        extra += f"\nQ: {item['question']}\nA: {item['answer']}\n"
    return file_content + extra

# ─────────────────────────────────────────────
# Text-to-Speech
# ─────────────────────────────────────────────
def generate_audio(text: str) -> str:
    """Generate MP3 audio and return its UUID filename (without extension)."""
    audio_id = str(uuid.uuid4())
    path = os.path.join(AUDIO_DIR, f"{audio_id}.mp3")
    # Truncate to 500 chars to avoid TTS timeouts
    tts = gTTS(text=text[:500], lang=TTS_LANGUAGE, slow=False)
    tts.save(path)
    return audio_id

# ─────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────

@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    """Accept a file upload, extract its text, persist session."""
    filename = file.filename
    allowed_ext = {"txt", "pdf", "docx", "text", "md"}
    ext = filename.lower().rsplit(".", 1)[-1]
    if ext not in allowed_ext:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '.{ext}'. Allowed: {', '.join(allowed_ext)}"
        )

    safe_name = f"{uuid.uuid4()}_{filename}"
    file_path = os.path.join(UPLOAD_DIR, safe_name)

    with open(file_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    content = extract_text(file_path, filename)
    session = {"file_path": file_path, "filename": filename, "file_content": content}
    save_session(session)

    preview = content[:300] + "…" if len(content) > 300 else content
    return {
        "message": f"File '{filename}' uploaded successfully!",
        "filename": filename,
        "content_preview": preview,
        "word_count": len(content.split()),
    }


@app.get("/session-info")
async def session_info():
    session = get_session()
    return {
        "filename": session.get("filename", ""),
        "has_file": bool(session.get("file_content", "").strip()),
        "word_count": len(session.get("file_content", "").split()),
    }


class AskRequest(BaseModel):
    question: str


@app.post("/ask")
async def ask_question(req: AskRequest):
    """Ask a question; returns text answer + audio URL."""
    session = get_session()
    if not session.get("file_content", "").strip():
        raise HTTPException(status_code=400, detail="Please upload a file first.")

    question   = req.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question cannot be empty.")

    full_ctx   = get_full_context(session["file_content"])
    # Limit context to ~6000 chars to stay within model limits
    ctx_chunk  = full_ctx[:6000]

    system_prompt = f"""You are a smart Q&A assistant.

PRIORITY:
1. First use Admin Knowledge Base (Q&A).
2. Then use document content.

RULES:
- If answer exists → answer clearly
- If not → respond EXACTLY: OUT_OF_SCOPE

CONTEXT:
{ctx_chunk}"""

    try:
        resp = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system",  "content": system_prompt},
                {"role": "user",    "content": question},
            ],
            max_tokens=600,
            temperature=0.1,
        )
        answer = resp.choices[0].message.content.strip()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"LLM Error: {e}")

    # ── Out-of-scope path ──────────────────────────────────────────────
    if "OUT_OF_SCOPE" in answer.upper():
        # Generate a hallucinated / speculative answer
        try:
            hall_resp = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Answer the question with a brief, plausible-sounding "
                            "speculative response. Keep it under 80 words."
                        ),
                    },
                    {"role": "user", "content": question},
                ],
                max_tokens=150,
                temperature=0.75,
            )
            hallucinated = hall_resp.choices[0].message.content.strip()
        except Exception:
            hallucinated = "This topic may require further research."

        display_answer = (
            "⚠️ We will get back to you on this question.\n\n"
            f"💭 Preliminary thought (may not be accurate): {hallucinated}"
        )

        # Save to unanswered list (avoid duplicates)
        unanswered = load_json(UNANSWERED_FILE, [])
        duplicate  = any(
            q["question"].lower().strip() == question.lower() for q in unanswered
        )
        if not duplicate:
            unanswered.append({
                "id":                  str(uuid.uuid4()),
                "question":            question,
                "timestamp":           datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "answered":            False,
                "admin_answer":        "",
                "hallucinated_answer": hallucinated,
            })
            save_json(UNANSWERED_FILE, unanswered)

        audio_text = (
            "We will get back to you on this question. "
            "Our team has been notified and will provide an answer soon."
        )
        audio_id = generate_audio(audio_text)

        return {
            "answer":        display_answer,
            "audio_url":     f"/audio/{audio_id}.mp3",
            "is_out_of_scope": True,
            "question":      question,
        }

    # ── In-scope path ──────────────────────────────────────────────────
    audio_id = generate_audio(answer)
    return {
        "answer":          answer,
        "audio_url":       f"/audio/{audio_id}.mp3",
        "is_out_of_scope": False,
        "question":        question,
    }


@app.get("/unanswered")
async def get_unanswered():
    """Return all unanswered questions."""
    questions = load_json(UNANSWERED_FILE, [])
    pending   = [q for q in questions if not q["answered"]]
    return {"questions": pending, "count": len(pending)}


class AdminAnswerRequest(BaseModel):
    question_id: str
    answer: str


@app.post("/admin/answer")
async def save_admin_answer(req: AdminAnswerRequest):
    """Admin saves an answer → stored in knowledge base + original file (TXT)."""
    questions = load_json(UNANSWERED_FILE, [])
    target    = None

    for q in questions:
        if q["id"] == req.question_id:
            q["answered"]    = True
            q["admin_answer"] = req.answer.strip()
            q["answered_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            target = q
            break

    if not target:
        raise HTTPException(status_code=404, detail="Question not found.")

    save_json(UNANSWERED_FILE, questions)

    # Persist answer in knowledge base (all file types)
    kb = load_json(KNOWLEDGE_BASE_FILE, [])
    kb.append({
        "question":  target["question"],
        "answer":    req.answer.strip(),
        "added_at":  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    save_json(KNOWLEDGE_BASE_FILE, kb)

    # For plain-text files: also append directly to the source file
    session = get_session()
    if session.get("file_path") and session.get("filename"):
        ext = session["filename"].lower().rsplit(".", 1)[-1]
        if ext in ("txt", "text", "md"):
            try:
                with open(session["file_path"], "a", encoding="utf-8") as f:
                    f.write(
                        f"\n\n--- Admin Q&A Addition ({datetime.now().strftime('%Y-%m-%d')}) ---\n"
                        f"Q: {target['question']}\nA: {req.answer.strip()}\n"
                    )
                # Reload content into session
                content = extract_text(session["file_path"], session["filename"])
                session["file_content"] = content
                save_session(session)
            except Exception:
                pass  # Knowledge base already updated; silent fail for file write

    return {"message": "Answer saved and added to knowledge base!"}


@app.delete("/admin/remove/{question_id}")
async def remove_question(question_id: str):
    """Remove a question (mark resolved / delete from list)."""
    questions = load_json(UNANSWERED_FILE, [])
    filtered  = [q for q in questions if q["id"] != question_id]

    if len(filtered) == len(questions):
        raise HTTPException(status_code=404, detail="Question not found.")

    save_json(UNANSWERED_FILE, filtered)
    return {"message": "Question removed successfully."}


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=HOST, port=PORT, reload=True)