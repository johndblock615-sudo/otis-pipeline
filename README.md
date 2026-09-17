# OTIS Pipeline

Daily publishing pipeline for the **OTIS** YouTube channel
(`@imotisyt`) — animal-behaviour explainers.

## How it works

1. Finished Shorts (720x1280 MP4 + thumbnail + SEO metadata) are pushed
   into `queue/` by the production VM. Each video needs a sidecar JSON:
   `queue/<name>.json`:

   ```json
   {
     "title": "Why Do Hippos Sweat PINK? 🦛 #Shorts",
     "description": "...",
     "tags": ["hippos", "animal facts", "shorts"],
     "thumbnail": "01-hippos-sweat-pink.jpg",
     "privacy": "public",
     "category": "15"
   }
   ```

2. The **Daily OTIS upload** GitHub Action runs every day at 09:00 PKT
   (also manually via *Run workflow*). It takes the oldest video in
   `queue/`, uploads it to YouTube with its metadata and thumbnail,
   verifies it is publicly visible, then moves the files to `published/`
   and commits the change back.

3. `published/log.md` records every release with its watch URL.

## Secrets

The workflow needs three repository secrets (never committed):

- `YT_CLIENT_ID`
- `YT_CLIENT_SECRET`
- `YT_REFRESH_TOKEN`

## Scripts

- `bin/upload.py` — resumable YouTube Data API v3 uploader (stdlib only).
  Reads credentials from `~/.config/youtube-upload/credentials.json`
  locally, or from `YT_*` env vars in CI.
- `scripts/publish_next.py` — picks the oldest queued video, uploads,
  verifies, archives to `published/`. Supports `--dry-run`.
