# Scene 01 — SkyReels V2 on Modal + Supabase

This deployment is a private, asynchronous image-to-video service for the Scene 01 portrait workflow.

## What is already prepared

- Modal application: `deploy/modal_app.py`
- Supabase project: `bmhknkiukvtouvhmrozl`
- Private Storage bucket: `skyreels-videos`
- Existing job and asset tables: `public.freefield_jobs` and `public.freefield_assets`
- Dedicated branch: `modal-supabase-scene01`

The API uses the Supabase service-role key only inside Modal Secrets. Never put it in a browser app, GitHub source, or Android client.

## Model and output settings

- Model: `Skywork/SkyReels-V2-I2V-14B-720P`
- Resolution: portrait 720 × 1280
- Frames: 121
- Frame rate: 15 fps
- Final clip target: 8 seconds
- GPU: NVIDIA H100
- Output bucket: private `skyreels-videos`

The official SkyReels V2 I2V 14B 720P checkpoint is supported by the upstream repository. The upstream script's 720P model is designed around 121 frames. This service trims the resulting file to the requested 8 seconds. SkyReels V2 itself does not create the requested spoken dialogue in this pipeline; add a separately generated voice track in post-production if audible dialogue is required.

## One-time setup from Android

Use GitHub Codespaces in Chrome (desktop site if needed) or any computer with Python 3.10+.

1. Open the repository: https://github.com/prokopijkol-vok/SkyReels-V2
2. Switch to branch `modal-supabase-scene01`.
3. Open a Codespace for that branch.
4. In the Codespaces terminal:

   ```bash
   python -m pip install --upgrade modal
   modal setup
   ```

5. In the Modal dashboard, create a Secret named exactly `skyreels-supabase` with these keys:
   - `SUPABASE_URL`: `https://bmhknkiukvtouvhmrozl.supabase.co`
   - `SUPABASE_ANON_KEY`: your project's publishable/anon key
   - `SUPABASE_SERVICE_ROLE_KEY`: your project's service-role secret key

   Keep the service-role key private. Do not paste it into chat or commit it to GitHub.

6. Deploy the API:

   ```bash
   modal deploy deploy/modal_app.py
   ```

7. Modal prints the deployed web endpoint. Check its `/health` route. The first model request may take longer because weights are downloaded and cached in a Modal Volume.

## Submit Scene 01

The calling app must send the signed-in user's Supabase access token as a Bearer token and upload the portrait image as multipart form data.

Example (replace the endpoint and access token):

```bash
curl -X POST "https://YOUR-MODAL-ENDPOINT/v1/scenes" \
  -H "Authorization: Bearer YOUR_SUPABASE_USER_ACCESS_TOKEN" \
  -F "image=@portrait.jpg;type=image/jpeg" \
  -F "scene_name=Scene 01 — The First Meeting" \
  -F 'prompt=Photorealistic first-person POV cinematic portrait. Preserve the exact appearance of the supplied woman. She sits in an elegant modern interior, looks directly into the camera, gently blinks, tilts her head slightly, gives a warm natural smile, her long blonde hair moves subtly, and she leans forward slightly as if greeting the viewer. Slow cinematic push-in, eye-level portrait framing, subtle handheld movement, soft daylight through a large window, warm intimate mood, realistic skin texture, natural facial motion. One continuous shot, no cuts, no subtitles, no text, no extra people.'
```

The response includes a `job_id`. Poll:

```bash
curl "https://YOUR-MODAL-ENDPOINT/v1/scenes/JOB_UUID" \
  -H "Authorization: Bearer YOUR_SUPABASE_USER_ACCESS_TOKEN"
```

When status is `succeeded`, the response contains a short-lived signed URL to the MP4. The MP4 itself remains in the private Supabase Storage bucket, and a metadata row is written to `freefield_assets`.

## Costs and safeguards

- Modal Starter currently advertises $30/month in free compute credits, but GPU usage is still metered and availability/credits depend on the Modal account. Review current GPU pricing and your workspace balance before submitting a generation.
- H100 execution is not free by default. Do not submit a job until you have checked the estimated cost.
- The API is authenticated; every status lookup is scoped to the caller's Supabase user ID.
- Uploaded reference images and MP4 outputs are stored under user-specific paths in a private bucket.
- Do not expose the service-role key or make the bucket public.

## Important current limitation

This repository change prepares the deployment code and storage target; it does not itself deploy the Modal app, create Modal credentials, upload the original portrait, or start a GPU job. Those require access to the user's Modal account and a real authenticated portrait upload.
