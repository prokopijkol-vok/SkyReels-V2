import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import modal
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from supabase import Client, create_client

APP_NAME = "skyreels-v2-scene-generator"
BUCKET = "skyreels-videos"
MODEL_ID = "Skywork/SkyReels-V2-I2V-14B-720P"
REPO_DIR = "/opt/SkyReels-V2"
MODEL_CACHE = "/model-cache"

app = modal.App(APP_NAME)
model_volume = modal.Volume.from_name("skyreels-v2-model-cache", create_if_missing=True)

api_image = (
    modal.Image.debian_slim(python_version="3.10")
    .pip_install("fastapi==0.115.12", "python-multipart==0.0.20", "supabase==2.15.1")
)

gpu_image = (
    modal.Image.from_registry(
        "nvidia/cuda:12.4.1-devel-ubuntu22.04",
        add_python="3.10",
    )
    .apt_install("git", "ffmpeg", "libgl1", "libglib2.0-0")
    .pip_install("torch==2.5.1", "torchvision==0.20.1")
    .run_commands(
        "pip install flash-attn==2.7.4.post1 --no-build-isolation",
        f"git clone --depth 1 https://github.com/SkyworkAI/SkyReels-V2.git {REPO_DIR}",
        f"cd {REPO_DIR} && pip install -r requirements.txt",
        "pip install supabase==2.15.1",
    )
    .env({"HF_HOME": MODEL_CACHE, "PYTHONUNBUFFERED": "1"})
)

def get_supabase(service: bool = True) -> Client:
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"] if service else os.environ["SUPABASE_ANON_KEY"]
    return create_client(url, key)

def verify_user(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="A Supabase access token is required.")
    token = authorization.split(" ", 1)[1].strip()
    try:
        user = get_supabase(service=False).auth.get_user(token).user
        if not user:
            raise ValueError("No authenticated user")
        return str(user.id)
    except Exception:
        raise HTTPException(status_code=401, detail="The Supabase access token is invalid or expired.")

@app.function(
    image=gpu_image,
    gpu="H100",
    timeout=3600,
    retries=0,
    secrets=[modal.Secret.from_name("skyreels-supabase")],
    volumes={MODEL_CACHE: model_volume},
)
def generate_scene(job_id: str, user_id: str, input_path: str, prompt: str, output_path: str):
    sb = get_supabase()
    sb.table("freefield_jobs").update({"status": "running", "updated_at": "now()"}).eq("id", job_id).execute()
    try:
        image_bytes = sb.storage.from_(BUCKET).download(input_path)
        work_dir = Path(tempfile.mkdtemp(prefix="skyreels-"))
        input_file = work_dir / "reference.jpg"
        input_file.write_bytes(image_bytes)

        run_name = f"job-{job_id}"
        command = [
            "python", "generate_video.py",
            "--model_id", MODEL_ID,
            "--resolution", "720P",
            "--num_frames", "121",
            "--fps", "15",
            "--guidance_scale", "5.0",
            "--shift", "3.0",
            "--inference_steps", "30",
            "--offload",
            "--image", str(input_file),
            "--prompt", prompt,
            "--outdir", run_name,
        ]
        subprocess.run(command, cwd=REPO_DIR, check=True)
        model_volume.commit()
        candidates = list((Path(REPO_DIR) / "result" / run_name).glob("*.mp4"))
        if not candidates:
            raise RuntimeError("SkyReels finished without producing an MP4 file.")
        generated = candidates[-1]

        final_file = work_dir / "scene-01.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(generated), "-t", "8", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(final_file)],
            check=True,
            capture_output=True,
        )
        video_bytes = final_file.read_bytes()
        sb.storage.from_(BUCKET).upload(
            output_path,
            video_bytes,
            file_options={"content-type": "video/mp4", "upsert": "true"},
        )
        sb.table("freefield_assets").insert({
            "user_id": user_id,
            "job_id": job_id,
            "storage_path": output_path,
            "media_type": "video/mp4",
            "file_size_bytes": len(video_bytes),
        }).execute()
        sb.table("freefield_jobs").update({
            "status": "succeeded",
            "output": {
                "bucket": BUCKET,
                "storage_path": output_path,
                "model": MODEL_ID,
                "duration_seconds": 8,
                "resolution": "720p portrait",
                "fps": 15,
                "audio": "not generated; add a separate voice track for dialogue",
            },
            "updated_at": "now()",
        }).eq("id", job_id).execute()
        shutil.rmtree(work_dir, ignore_errors=True)
        return {"job_id": job_id, "storage_path": output_path, "bytes": len(video_bytes)}
    except Exception as exc:
        sb.table("freefield_jobs").update({
            "status": "failed",
            "error": str(exc)[:4000],
            "updated_at": "now()",
        }).eq("id", job_id).execute()
        raise

web = FastAPI(title="SkyReels V2 + Supabase", version="1.0.0")

@web.get("/health")
def health():
    return {"ok": True, "model": MODEL_ID, "storage_bucket": BUCKET}

@web.post("/v1/scenes")
async def submit_scene(
    image: UploadFile = File(...),
    prompt: str = Form(...),
    scene_name: str = Form("Scene 01 — The First Meeting"),
    authorization: str | None = Header(default=None),
):
    user_id = verify_user(authorization)
    if image.content_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise HTTPException(status_code=415, detail="Upload a JPEG, PNG, or WebP reference image.")
    if len(prompt) > 4000 or not prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt must contain 1–4000 characters.")
    raw = await image.read()
    if not raw or len(raw) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Reference image must be smaller than 20 MB.")

    sb = get_supabase()
    job_id = str(uuid.uuid4())
    input_path = f"{user_id}/{job_id}/reference"
    output_path = f"{user_id}/{job_id}/scene-01.mp4"
    sb.storage.from_(BUCKET).upload(
        input_path,
        raw,
        file_options={"content-type": image.content_type, "upsert": "true"},
    )
    sb.table("freefield_jobs").insert({
        "id": job_id,
        "user_id": user_id,
        "app_id": "skyreels-v2",
        "status": "queued",
        "input": {
            "scene_name": scene_name,
            "prompt": prompt,
            "model": MODEL_ID,
            "duration_seconds": 8,
            "resolution": "720p portrait",
        },
    }).execute()
    sb.table("freefield_assets").insert({
        "user_id": user_id,
        "job_id": job_id,
        "storage_path": input_path,
        "media_type": image.content_type,
        "file_size_bytes": len(raw),
    }).execute()
    call = generate_scene.spawn(job_id, user_id, input_path, prompt, output_path)
    sb.table("freefield_jobs").update({
        "backend_job_id": str(call.object_id),
        "updated_at": "now()",
    }).eq("id", job_id).execute()
    return JSONResponse({"job_id": job_id, "status": "queued", "model": MODEL_ID})

@web.get("/v1/scenes/{job_id}")
def scene_status(job_id: str, authorization: str | None = Header(default=None)):
    user_id = verify_user(authorization)
    sb = get_supabase()
    result = sb.table("freefield_jobs").select("*").eq("id", job_id).eq("user_id", user_id).limit(1).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Scene job not found.")
    job = result.data[0]
    response = {"job_id": job["id"], "status": job["status"], "error": job.get("error"), "output": job.get("output")}
    if job["status"] == "succeeded" and job.get("output", {}).get("storage_path"):
        signed = sb.storage.from_(BUCKET).create_signed_url(job["output"]["storage_path"], 3600)
        response["signed_url"] = signed.get("signedURL") or signed.get("signedUrl")
    return response

@app.function(image=api_image, secrets=[modal.Secret.from_name("skyreels-supabase")])
@modal.asgi_app()
def fastapi_app():
    return web
