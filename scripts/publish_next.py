#!/usr/bin/env python3
"""Publish the next queued OTIS short to YouTube.

Reads the oldest queue/<name>.mp4 plus its sidecar queue/<name>.json
(title, description, tags, thumbnail, privacy, category), uploads it via
bin/upload.py, verifies it is publicly visible, then moves the files to
published/ and appends a log line.

Usage:
    python3 scripts/publish_next.py [--dry-run]

--dry-run validates the OAuth token and prints what would be uploaded,
without uploading anything.
"""
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUEUE = os.path.join(ROOT, "queue")
PUBLISHED = os.path.join(ROOT, "published")
UPLOAD_PY = os.path.join(ROOT, "bin", "upload.py")
LOG = os.path.join(PUBLISHED, "log.md")


def oembed_ok(watch_url):
    try:
        req = urllib.request.Request(
            "https://www.youtube.com/oembed?url=%s&format=json" % watch_url,
            headers={"User-Agent": "otis-pipeline/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status == 200
    except Exception:
        return False


def main():
    dry = "--dry-run" in sys.argv
    vids = sorted(f for f in os.listdir(QUEUE) if f.endswith(".mp4"))
    if not vids:
        print("queue empty - nothing to upload")
        return 0
    name = vids[0][:-4]
    video = os.path.join(QUEUE, vids[0])
    sidecar_path = os.path.join(QUEUE, name + ".json")
    if not os.path.isfile(sidecar_path):
        print("missing sidecar: " + sidecar_path, file=sys.stderr)
        return 1
    with open(sidecar_path) as f:
        meta = json.load(f)

    thumb = ""
    if meta.get("thumbnail"):
        cand = os.path.join(QUEUE, meta["thumbnail"])
        if os.path.isfile(cand):
            thumb = cand
        else:
            print("warning: thumbnail missing, uploading without one: " + cand)

    print("next up: " + meta["title"], flush=True)
    print("file: %s (%d bytes)" % (vids[0], os.path.getsize(video)), flush=True)

    cmd = [sys.executable, UPLOAD_PY,
           "--video", video,
           "--title", meta["title"],
           "--description", meta.get("description", ""),
           "--tags", ",".join(meta.get("tags", [])),
           "--privacy", meta.get("privacy", "public"),
           "--category", str(meta.get("category", "15")),
           "--made-for-kids", "false"]
    if thumb:
        cmd += ["--thumbnail", thumb]

    if dry:
        # Validate the OAuth grant refreshes, then stop before uploading.
        sys.path.insert(0, os.path.join(ROOT, "bin"))
        import upload as up
        up.refresh_token(up.load_creds())
        print("dry run OK - token valid, would upload: " + meta["title"])
        return 0

    p = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    print(p.stdout)
    if p.returncode != 0:
        print(p.stderr, file=sys.stderr)
        return p.returncode

    watch_url = ""
    for line in p.stdout.strip().splitlines():
        if "youtube.com/watch?v=" in line:
            watch_url = line.strip().split()[-1]
    if not watch_url:
        print("could not find watch URL in upload output", file=sys.stderr)
        return 1
    if not oembed_ok(watch_url):
        print("uploaded but not publicly visible: " + watch_url, file=sys.stderr)
        return 1
    print("verified public: " + watch_url, flush=True)

    for fn in (vids[0], name + ".json") + ((meta["thumbnail"],) if thumb else ()):
        shutil.move(os.path.join(QUEUE, fn), os.path.join(PUBLISHED, fn))
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with open(LOG, "a") as f:
        f.write("%s | %s -> %s\n" % (stamp, meta["title"], watch_url))
    print("moved to published/, logged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
