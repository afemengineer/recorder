"""LAN narration studio API: reversible takes, resumable PCM uploads and WAV exports."""
from __future__ import annotations

import array
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
from difflib import SequenceMatcher
import hashlib
import hmac
import io
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
import wave
import zipfile

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(os.environ.get("RECORDER_DATA_DIR", "./recorder-data")).resolve()
STATIC = Path(__file__).parent / "static"
MAX_CHUNK = 2 * 1024 * 1024
MAX_SECTIONS = 300
DB_LOCK = threading.RLock()
ASR_LOCK = threading.Lock()
ASR_MODEL = None

app = FastAPI(title="Narration Recorder", version="0.1.0", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def uid():
    return uuid.uuid4().hex


def db():
    ROOT.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(ROOT / "recorder.sqlite3", timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def transaction():
    with DB_LOCK:
        with db() as conn:
            yield conn


def init_db():
    with transaction() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS projects (
          id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sections (
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          position INTEGER NOT NULL, text TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS takes (
          id TEXT PRIMARY KEY, section_id TEXT NOT NULL REFERENCES sections(id) ON DELETE CASCADE,
          created_at TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'uploading',
          sample_rate INTEGER NOT NULL, chunk_count INTEGER NOT NULL DEFAULT 0,
          samples INTEGER NOT NULL DEFAULT 0, duration REAL NOT NULL DEFAULT 0,
          selected INTEGER NOT NULL DEFAULT 0, notes TEXT NOT NULL DEFAULT '',
          transcript TEXT NOT NULL DEFAULT '', transcript_reviewed INTEGER NOT NULL DEFAULT 0,
          analysis_status TEXT NOT NULL DEFAULT 'not_started', analysis_error TEXT NOT NULL DEFAULT '',
          match_ratio REAL, peak_dbfs REAL, rms_dbfs REAL, clipping_pct REAL,
          trim_start REAL NOT NULL DEFAULT 0, trim_end REAL
        );
        CREATE TABLE IF NOT EXISTS chunks (
          take_id TEXT NOT NULL REFERENCES takes(id) ON DELETE CASCADE,
          idx INTEGER NOT NULL, bytes INTEGER NOT NULL, sha256 TEXT NOT NULL,
          PRIMARY KEY (take_id, idx)
        );
        CREATE INDEX IF NOT EXISTS idx_sections_project ON sections(project_id,position);
        CREATE INDEX IF NOT EXISTS idx_takes_section ON takes(section_id,created_at);
        """)


@app.on_event("startup")
def startup():
    init_db()


def row(conn, sql, values=()):
    result = conn.execute(sql, values).fetchone()
    if result is None:
        raise HTTPException(404, "Not found")
    return result


def public_take(t):
    result = dict(t)
    result.update(selected=bool(result["selected"]), transcript_reviewed=bool(result["transcript_reviewed"]))
    return result


def project_detail(conn, project_id):
    p = dict(row(conn, "SELECT * FROM projects WHERE id=?", (project_id,)))
    sections = []
    for s in conn.execute("SELECT * FROM sections WHERE project_id=? ORDER BY position", (project_id,)):
        item = dict(s)
        item["takes"] = [public_take(t) for t in conn.execute(
            "SELECT * FROM takes WHERE section_id=? ORDER BY rowid", (s["id"],))]
        sections.append(item)
    p["sections"] = sections
    return p


def split_script(text):
    # Paragraph-delimited input; single-line scripts can instead be entered section-by-section.
    return [s.strip() for s in re.split(r"\n\s*\n", text.strip()) if s.strip()]


def password():
    return os.getenv("RECORDER_PASSWORD", "")


def signing_key():
    ROOT.mkdir(parents=True, exist_ok=True)
    keypath = ROOT / ".session_key"
    if not keypath.exists():
        try:
            with keypath.open("xb") as out:
                out.write(secrets.token_bytes(32))
            try:
                keypath.chmod(0o600)
            except OSError:
                pass
        except FileExistsError:
            pass
    return keypath.read_bytes()


def authorized(request):
    if not password():
        return True
    token = request.cookies.get("recorder_session", "")
    try:
        payload, sig = token.split(".", 1)
        good = hmac.new(signing_key(), payload.encode(), hashlib.sha256).hexdigest()
        exp = int(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode())
        return hmac.compare_digest(sig, good) and exp > time.time()
    except (ValueError, UnicodeError):
        return False


@app.middleware("http")
async def guard(request: Request, call_next):
    if request.url.path.startswith("/api/") and request.url.path not in (
        "/api/auth/login", "/api/auth/status") and not authorized(request):
        return JSONResponse({"detail": "Authentication required"}, status_code=401)
    if password() and request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin:
            from urllib.parse import urlparse
            if urlparse(origin).netloc.lower() != request.headers.get("host", "").lower():
                return JSONResponse({"detail": "Cross-origin request denied"}, status_code=403)
    return await call_next(request)


class Login(BaseModel):
    password: str


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/auth/status")
def auth_status(request: Request):
    return {"enabled": bool(password()), "authenticated": authorized(request),
            "transcription_available": _has_asr()}


def _has_asr():
    from importlib.util import find_spec
    return find_spec("faster_whisper") is not None


@app.post("/api/auth/login")
def login(body: Login, request: Request, response: Response):
    if not password() or not hmac.compare_digest(body.password, password()):
        raise HTTPException(401, "Invalid password")
    exp = int(time.time() + 7 * 24 * 3600)
    payload = base64.urlsafe_b64encode(str(exp).encode()).decode().rstrip("=")
    sig = hmac.new(signing_key(), payload.encode(), hashlib.sha256).hexdigest()
    forwarded = request.headers.get("x-forwarded-proto", "").lower()
    response.set_cookie("recorder_session", payload + "." + sig, max_age=7 * 24 * 3600,
                        httponly=True, secure=request.url.scheme == "https" or forwarded == "https",
                        samesite="strict", path="/")
    return {"ok": True}


@app.post("/api/auth/logout")
def logout(response: Response):
    response.delete_cookie("recorder_session", path="/")
    return {"ok": True}


class ProjectIn(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    script: str = Field(min_length=1, max_length=150000)


class SectionIn(BaseModel):
    text: str = Field(min_length=1, max_length=15000)


@app.get("/api/projects")
def list_projects():
    with transaction() as conn:
        return [dict(p) for p in conn.execute("SELECT * FROM projects ORDER BY created_at DESC,id DESC")]


@app.post("/api/projects", status_code=201)
def create_project(body: ProjectIn):
    sections = split_script(body.script)
    if not 1 <= len(sections) <= MAX_SECTIONS:
        raise HTTPException(422, f"Script must contain 1–{MAX_SECTIONS} paragraphs")
    pid = uid()
    with transaction() as conn:
        conn.execute("INSERT INTO projects VALUES (?,?,?)", (pid, body.title.strip(), now()))
        conn.executemany("INSERT INTO sections VALUES (?,?,?,?)", [
            (uid(), pid, i, value) for i, value in enumerate(sections)])
        return project_detail(conn, pid)


@app.get("/api/projects/{pid}")
def get_project(pid: str):
    with transaction() as conn:
        return project_detail(conn, pid)


@app.post("/api/projects/{pid}/sections", status_code=201)
def add_section(pid: str, body: SectionIn):
    with transaction() as conn:
        row(conn, "SELECT id FROM projects WHERE id=?", (pid,))
        position = conn.execute("SELECT COUNT(*) FROM sections WHERE project_id=?", (pid,)).fetchone()[0]
        if position >= MAX_SECTIONS:
            raise HTTPException(422, "Too many sections")
        sid = uid()
        conn.execute("INSERT INTO sections VALUES (?,?,?,?)", (sid, pid, position, body.text.strip()))
        return dict(row(conn, "SELECT * FROM sections WHERE id=?", (sid,)))


@app.patch("/api/sections/{sid}")
def edit_section(sid: str, body: SectionIn):
    with transaction() as conn:
        row(conn, "SELECT * FROM sections WHERE id=?", (sid,))
        conn.execute("UPDATE sections SET text=? WHERE id=?", (body.text.strip(), sid))
        return dict(row(conn, "SELECT * FROM sections WHERE id=?", (sid,)))


class StartTake(BaseModel):
    sample_rate: int = Field(ge=8000, le=192000)


@app.post("/api/sections/{sid}/takes", status_code=201)
def start_take(sid: str, body: StartTake):
    tid = uid()
    with transaction() as conn:
        row(conn, "SELECT id FROM sections WHERE id=?", (sid,))
        conn.execute("INSERT INTO takes (id,section_id,created_at,sample_rate) VALUES (?,?,?,?)",
                     (tid, sid, now(), body.sample_rate))
    (ROOT / "takes" / tid).mkdir(parents=True, exist_ok=True)
    return {"id": tid, "state": "uploading", "sample_rate": body.sample_rate}


@app.get("/api/takes/{tid}/upload")
def upload_status(tid: str):
    with transaction() as conn:
        take = row(conn, "SELECT state,chunk_count FROM takes WHERE id=?", (tid,))
        chunks = [r[0] for r in conn.execute("SELECT idx FROM chunks WHERE take_id=? ORDER BY idx", (tid,))]
    return {"state": take["state"], "received": chunks, "chunk_count": take["chunk_count"]}


@app.put("/api/takes/{tid}/chunks/{idx}")
async def put_chunk(tid: str, idx: int, request: Request):
    if idx < 0 or idx > 200000:
        raise HTTPException(422, "Invalid chunk index")
    data = await request.body()
    if not data or len(data) > MAX_CHUNK or len(data) % 2:
        raise HTTPException(413, "PCM16 chunk must be nonempty, even length and <= 2 MiB")
    digest = hashlib.sha256(data).hexdigest()
    with transaction() as conn:
        take = row(conn, "SELECT state FROM takes WHERE id=?", (tid,))
        if take["state"] != "uploading":
            raise HTTPException(409, "Take already finalized")
        previous = conn.execute("SELECT sha256 FROM chunks WHERE take_id=? AND idx=?", (tid, idx)).fetchone()
        if previous:
            if previous[0] != digest:
                raise HTTPException(409, "Chunk index already holds different audio")
            return {"ok": True, "duplicate": True}
        directory = ROOT / "takes" / tid
        directory.mkdir(parents=True, exist_ok=True)
        temp = directory / f"{idx:07d}.{uid()}.tmp"
        temp.write_bytes(data)
        os.replace(temp, directory / f"{idx:07d}.pcm")
        conn.execute("INSERT INTO chunks VALUES (?,?,?,?)", (tid, idx, len(data), digest))
    return {"ok": True, "duplicate": False}


class FinishTake(BaseModel):
    expected_chunks: int = Field(ge=1, le=200001)


def write_wav(tid, rate, chunks):
    folder = ROOT / "takes" / tid
    output = folder / "audio.wav"
    temp = folder / "audio.tmp"
    peak = 0
    sum_sq = 0
    clipped = 0
    samples = 0
    try:
        with wave.open(str(temp), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(rate)
            for idx in chunks:
                data = (folder / f"{idx:07d}.pcm").read_bytes()
                values = array.array("h")
                values.frombytes(data)
                if os.sys.byteorder == "big":
                    values.byteswap()
                samples += len(values)
                if not values:
                    continue
                peak = max(peak, max(abs(v) for v in values))
                sum_sq += sum(v * v for v in values)
                clipped += sum(abs(v) >= 32760 for v in values)
                wav.writeframesraw(data)
        os.replace(temp, output)
    finally:
        temp.unlink(missing_ok=True)
    peak_db = round(20 * math.log10(max(peak, 1) / 32768), 1)
    rms_db = round(20 * math.log10(max(math.sqrt(sum_sq / max(samples, 1)), 1) / 32768), 1)
    return samples, peak_db, rms_db, round(100 * clipped / max(samples, 1), 3)


@app.post("/api/takes/{tid}/finish")
def finish_take(tid: str, body: FinishTake):
    with transaction() as conn:
        take = row(conn, "SELECT * FROM takes WHERE id=?", (tid,))
        if take["state"] == "ready":
            if take["chunk_count"] == body.expected_chunks:
                return public_take(take)
            raise HTTPException(409, "Already finalized with different chunk count")
        if take["state"] != "uploading":
            raise HTTPException(409, "Cannot finish take")
        chunks = [r[0] for r in conn.execute("SELECT idx FROM chunks WHERE take_id=? ORDER BY idx", (tid,))]
        if chunks != list(range(body.expected_chunks)):
            raise HTTPException(409, {"message": "Missing chunks", "received": chunks})
        samples, peak, rms, clipped = write_wav(tid, take["sample_rate"], chunks)
        if samples == 0:
            raise HTTPException(422, "Empty take")
        conn.execute("UPDATE takes SET selected=0 WHERE section_id=?", (take["section_id"],))
        conn.execute("""UPDATE takes SET state='ready',chunk_count=?,samples=?,duration=?,
                       selected=1,peak_dbfs=?,rms_dbfs=?,clipping_pct=? WHERE id=?""",
                     (len(chunks), samples, samples / take["sample_rate"], peak, rms, clipped, tid))
        result = public_take(row(conn, "SELECT * FROM takes WHERE id=?", (tid,)))
    # Recoverable original chunks are retained; don't discard primary source data.
    return result


class TakeEdit(BaseModel):
    selected: bool | None = None
    notes: str | None = Field(default=None, max_length=3000)
    transcript: str | None = Field(default=None, max_length=15000)
    transcript_reviewed: bool | None = None
    trim_start: float | None = Field(default=None, ge=0)
    trim_end: float | None = Field(default=None, ge=0)


@app.patch("/api/takes/{tid}")
def edit_take(tid: str, body: TakeEdit):
    with transaction() as conn:
        take = row(conn, "SELECT * FROM takes WHERE id=?", (tid,))
        if take["state"] != "ready":
            raise HTTPException(409, "Take must be finalized")
        updates = body.model_dump(exclude_unset=True)
        if "selected" in updates and updates["selected"]:
            conn.execute("UPDATE takes SET selected=0 WHERE section_id=?", (take["section_id"],))
        if "trim_start" in updates or "trim_end" in updates:
            start = updates.get("trim_start", take["trim_start"])
            end = updates.get("trim_end", take["trim_end"])
            if start is None or start >= take["duration"] or (end is not None and (end > take["duration"] or end <= start)):
                raise HTTPException(422, "Invalid trim interval")
        allowed = {"selected", "notes", "transcript", "transcript_reviewed", "trim_start", "trim_end"}
        for k, v in updates.items():
            if k not in allowed:
                continue
            conn.execute(f"UPDATE takes SET {k}=? WHERE id=?", (int(v) if k in ("selected", "transcript_reviewed") else v, tid))
        return public_take(row(conn, "SELECT * FROM takes WHERE id=?", (tid,)))


@app.get("/api/takes/{tid}/audio")
def audio(tid: str):
    with transaction() as conn:
        take = row(conn, "SELECT state FROM takes WHERE id=?", (tid,))
        if take["state"] != "ready":
            raise HTTPException(409, "Take not yet finalized")
    return FileResponse(ROOT / "takes" / tid / "audio.wav", media_type="audio/wav")


def selected_takes(conn, pid):
    row(conn, "SELECT id FROM projects WHERE id=?", (pid,))
    return list(conn.execute("""SELECT s.position,s.text,t.* FROM sections s JOIN takes t
      ON t.section_id=s.id WHERE s.project_id=? AND t.state='ready' AND t.selected=1
      ORDER BY s.position""", (pid,)))


def trim_frames(t):
    start = round(t["trim_start"] * t["sample_rate"])
    end = round((t["trim_end"] if t["trim_end"] is not None else t["duration"]) * t["sample_rate"])
    return start, min(end, t["samples"])


def render_mix(pid, out):
    with transaction() as conn:
        takes = selected_takes(conn, pid)
    if not takes:
        raise HTTPException(409, "Select at least one completed take")
    rate = takes[0]["sample_rate"]
    if any(t["sample_rate"] != rate for t in takes):
        raise HTTPException(409, "Different recording sample rates; export individual takes or re-record at one rate")
    with wave.open(str(out), "wb") as dst:
        dst.setnchannels(1)
        dst.setsampwidth(2)
        dst.setframerate(rate)
        for i, t in enumerate(takes):
            with wave.open(str(ROOT / "takes" / t["id"] / "audio.wav"), "rb") as src:
                start, end = trim_frames(t)
                src.setpos(start)
                left = end - start
                while left > 0:
                    count = min(left, rate * 4)
                    dst.writeframesraw(src.readframes(count))
                    left -= count
            if i < len(takes) - 1:
                dst.writeframesraw(b"\x00\x00" * round(rate * 0.25))
    return takes


@app.get("/api/projects/{pid}/mix.wav")
def mix(pid: str, background_tasks: BackgroundTasks):
    tmp = tempfile.NamedTemporaryFile(prefix="recorder-mix-", suffix=".wav", delete=False)
    tmp.close()
    try:
        render_mix(pid, tmp.name)
    except Exception:
        Path(tmp.name).unlink(missing_ok=True)
        raise
    background_tasks.add_task(Path(tmp.name).unlink, missing_ok=True)
    return FileResponse(tmp.name, media_type="audio/wav", filename="narration-rough-cut.wav")


@app.get("/api/projects/{pid}/export.zip")
def export(pid: str, background_tasks: BackgroundTasks):
    with transaction() as conn:
        project = project_detail(conn, pid)
        takes = selected_takes(conn, pid)
    tmp = tempfile.NamedTemporaryFile(prefix="recorder-export-", suffix=".zip", delete=False)
    tmp.close()
    try:
        manifest = {"project": project["title"], "created_at": now(), "format": "PCM16 mono WAV",
                    "note": "Training transcripts are usable only when transcript_reviewed=true.", "takes": []}
        with zipfile.ZipFile(tmp.name, "w", compression=zipfile.ZIP_STORED) as z:
            z.writestr("script.txt", "\n\n".join(s["text"] for s in project["sections"]))
            for t in takes:
                name = f"takes/{int(t['position'])+1:03d}_{t['id'][:8]}.wav"
                z.write(ROOT / "takes" / t["id"] / "audio.wav", name)
                manifest["takes"].append({"section": int(t["position"]) + 1, "file": name,
                    "reference_text": t["text"], "spoken_transcript": t["transcript"],
                    "transcript_reviewed": bool(t["transcript_reviewed"]),
                    "start_sec": t["trim_start"], "end_sec": t["trim_end"],
                    "peak_dbfs": t["peak_dbfs"], "rms_dbfs": t["rms_dbfs"],
                    "clipping_pct": t["clipping_pct"]})
            z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    except Exception:
        Path(tmp.name).unlink(missing_ok=True)
        raise
    background_tasks.add_task(Path(tmp.name).unlink, missing_ok=True)
    return FileResponse(tmp.name, media_type="application/zip", filename="narration-project.zip")


def transcribe_worker(tid):
    global ASR_MODEL
    try:
        with ASR_LOCK:
            if ASR_MODEL is None:
                from faster_whisper import WhisperModel
                ASR_MODEL = WhisperModel(os.getenv("RECORDER_WHISPER_MODEL", "small.en"),
                                        device=os.getenv("RECORDER_WHISPER_DEVICE", "cpu"),
                                        compute_type=os.getenv("RECORDER_WHISPER_COMPUTE", "int8"))
            with transaction() as conn:
                t = row(conn, "SELECT t.*,s.text AS reference FROM takes t JOIN sections s ON s.id=t.section_id WHERE t.id=?", (tid,))
            segments, _ = ASR_MODEL.transcribe(str(ROOT / "takes" / tid / "audio.wav"),
                                                beam_size=5, vad_filter=True,
                                                language=os.getenv("RECORDER_LANGUAGE", "en"))
            transcript = " ".join(s.text.strip() for s in segments).strip()
            ratio = round(SequenceMatcher(None, t["reference"].casefold(), transcript.casefold()).ratio(), 3)
            with transaction() as conn:
                current = row(conn, "SELECT transcript_reviewed FROM takes WHERE id=?", (tid,))
                if current["transcript_reviewed"]:
                    conn.execute("UPDATE takes SET analysis_status='done',match_ratio=?,analysis_error='' WHERE id=?", (ratio, tid))
                else:
                    conn.execute("""UPDATE takes SET analysis_status='done',transcript=?,
                                    transcript_reviewed=0,match_ratio=?,analysis_error='' WHERE id=?""", (transcript, ratio, tid))
    except Exception as exc:
        with transaction() as conn:
            conn.execute("UPDATE takes SET analysis_status='error',analysis_error=? WHERE id=?",
                         (str(exc)[:500], tid))


@app.post("/api/takes/{tid}/transcribe")
def transcribe(tid: str, background_tasks: BackgroundTasks):
    if not _has_asr():
        raise HTTPException(501, "Install optional dependency: pip install -e '.[transcribe]'")
    with transaction() as conn:
        t = row(conn, "SELECT state,analysis_status FROM takes WHERE id=?", (tid,))
        if t["state"] != "ready":
            raise HTTPException(409, "Finish the take first")
        if t["analysis_status"] == "processing":
            return {"status": "processing"}
        conn.execute("UPDATE takes SET analysis_status='processing',analysis_error='' WHERE id=?", (tid,))
    background_tasks.add_task(transcribe_worker, tid)
    return {"status": "processing"}