#!/usr/bin/env python3
"""Upload a video to the OTIS YouTube channel (YouTube Data API v3).

Stdlib only. Refreshes the OAuth2 access token, performs a resumable
upload with retries and resume-across-runs (offset is tracked from the
server's 308 Range responses, never from an empty-body status query),
sets the custom thumbnail, and prints the watch URL.
"""
import argparse
import hashlib
import http.client
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CREDS_PATH = os.path.expanduser("~/.config/youtube-upload/credentials.json")
STATE_DIR = os.path.expanduser("~/.config/youtube-upload")
CHUNK_SIZE = 4 * 1024 * 1024
MAX_RETRIES = 8
RETRYABLE = (http.client.RemoteDisconnected, http.client.IncompleteRead,
             ConnectionError, TimeoutError, urllib.error.URLError)


def load_creds(path=""):
    env = os.environ
    if env.get("YT_CLIENT_ID") and env.get("YT_CLIENT_SECRET") and env.get("YT_REFRESH_TOKEN"):
        # CI / GitHub Actions: credentials come from repository secrets.
        return {
            "client_id": env["YT_CLIENT_ID"],
            "client_secret": env["YT_CLIENT_SECRET"],
            "refresh_token": env["YT_REFRESH_TOKEN"],
        }
    with open(path or CREDS_PATH) as f:
        return json.load(f)


def refresh_token(creds):
    data = urllib.parse.urlencode({
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "refresh_token": creds["refresh_token"],
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["access_token"]


def raw_request(method, url, token, body=None, headers=None, timeout=180):
    h = {"Authorization": "Bearer " + token}
    if headers:
        h.update(headers)
    if isinstance(body, dict):
        body = json.dumps(body).encode()
        h["Content-Type"] = "application/json; charset=UTF-8"
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def state_file(path, size):
    h = hashlib.sha1(("%s:%d" % (os.path.abspath(path), size)).encode()).hexdigest()[:12]
    return os.path.join(STATE_DIR, "resume-%s.json" % h)


def save_state(sf, session_uri, confirmed):
    os.makedirs(os.path.dirname(sf), exist_ok=True)
    json.dump({"session_uri": session_uri, "confirmed": confirmed}, open(sf, "w"))
    os.chmod(sf, 0o600)


def find_video_by_title(token, title):
    """Fallback: locate an uploaded video by exact title via the uploads playlist."""
    st, _, body = raw_request(
        "GET", "https://www.googleapis.com/youtube/v3/channels?part=contentDetails&mine=true", token)
    if st != 200:
        return None
    items = json.loads(body).get("items", [])
    if not items:
        return None
    pl = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    st, _, body = raw_request(
        "GET", "https://www.googleapis.com/youtube/v3/playlistItems"
               "?part=snippet&playlistId=%s&maxResults=5" % pl, token)
    if st != 200:
        return None
    for it in json.loads(body).get("items", []):
        if it["snippet"]["title"] == title:
            return it["snippet"]["resourceId"]["videoId"]
    return None


def resumable_upload(token, path, metadata, title):
    size = os.path.getsize(path)
    sf = state_file(path, size)
    session_uri, confirmed = None, 0
    if os.path.isfile(sf):
        try:
            st = json.load(open(sf))
            session_uri, confirmed = st["session_uri"], st.get("confirmed", 0)
            print("resuming previous upload session from byte %d..." % confirmed, flush=True)
        except Exception:
            session_uri, confirmed = None, 0

    def new_session():
        init_url = ("https://www.googleapis.com/upload/youtube/v3/videos"
                    "?uploadType=resumable&part=snippet,status")
        status, headers, body = raw_request("POST", init_url, token, body=metadata, headers={
            "X-Upload-Content-Length": str(size),
            "X-Upload-Content-Type": "video/mp4",
        })
        if status != 200:
            raise RuntimeError("upload init failed: %s %s" % (status, body[:500]))
        uri = headers.get("Location")
        if not uri:
            raise RuntimeError("no upload session URI returned")
        save_state(sf, uri, 0)
        return uri

    if not session_uri:
        session_uri = new_session()

    def drop_state():
        if os.path.isfile(sf):
            os.remove(sf)

    offset = confirmed
    fail_offset, fail_count = -1, 0
    with open(path, "rb") as f:
        while offset < size:
            f.seek(offset)
            chunk = f.read(CHUNK_SIZE)
            end = offset + len(chunk) - 1
            try:
                st, hd, bd = raw_request("PUT", session_uri, token, body=chunk, headers={
                    "Content-Length": str(len(chunk)),
                    "Content-Range": "bytes %d-%d/%d" % (offset, end, size),
                }, timeout=300)
            except RETRYABLE as e:
                fail_count = fail_count + 1 if offset == fail_offset else 1
                fail_offset = offset
                if fail_count > MAX_RETRIES:
                    raise RuntimeError("network kept failing at offset %d" % offset)
                print("network blip (%s), retrying from byte %d..." %
                      (type(e).__name__, confirmed), flush=True)
                time.sleep(min(2 ** fail_count, 30))
                offset = confirmed
                continue
            fail_offset, fail_count = -1, 0
            if st in (200, 201):
                drop_state()
                return json.loads(bd)["id"]
            if st == 308:
                rng = hd.get("Range", "")
                confirmed = int(rng.split("-")[1]) + 1 if "-" in rng else end + 1
                save_state(sf, session_uri, confirmed)
                offset = confirmed
                if confirmed % (32 * 1024 * 1024) < CHUNK_SIZE:
                    print("... %d/%d MB" % (confirmed // 1048576, size // 1048576), flush=True)
                continue
            if st in (400, 404, 410):
                print("session expired, starting a fresh one...", flush=True)
                drop_state()
                session_uri = new_session()
                confirmed, offset = 0, 0
                continue
            if st >= 500 or st in (429, 408):
                time.sleep(5)
                continue
            raise RuntimeError("upload chunk failed: %s %s" % (st, bd[:500]))

    # Loop ended without a 200/201 (e.g. final response lost): verify by title.
    vid = find_video_by_title(token, title)
    if vid:
        drop_state()
        return vid
    raise RuntimeError("upload incomplete and video not found on channel")


def set_thumbnail(token, video_id, thumb_path):
    with open(thumb_path, "rb") as f:
        img = f.read()
    ctype = mimetypes.guess_type(thumb_path)[0] or "image/jpeg"
    boundary = "----yt-upload-boundary-7d4a6"
    head = ("--%s\r\nContent-Type: application/json\r\n\r\n{}\r\n"
            "--%s\r\nContent-Type: %s\r\n\r\n" % (boundary, boundary, ctype)).encode()
    tail = ("\r\n--%s--\r\n" % boundary).encode()
    status, _, body = raw_request(
        "POST",
        "https://www.googleapis.com/upload/youtube/v3/thumbnails/set?videoId=" + video_id,
        token, body=head + img + tail,
        headers={"Content-Type": "multipart/related; boundary=%s" % boundary})
    if status != 200:
        raise RuntimeError("thumbnail set failed: %s %s" % (status, body[:500]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--description", default="")
    ap.add_argument("--description-file", default="")
    ap.add_argument("--tags", default="")
    ap.add_argument("--thumbnail", default="")
    ap.add_argument("--privacy", default="unlisted", choices=["public", "unlisted", "private"])
    ap.add_argument("--publish-at", default="",
                    help="ISO 8601 UTC timestamp, e.g. 2026-09-10T15:00:00Z. "
                         "Schedules the video: uploads as private and YouTube "
                         "publishes it at that time.")
    ap.add_argument("--category", default="15")
    ap.add_argument("--made-for-kids", default="false", choices=["true", "false"])
    ap.add_argument("--creds", default="",
                    help="Path to an alternate OAuth credentials JSON (default: "
                         "~/.config/youtube-upload/credentials.json). Use for a "
                         "second channel, e.g. credentials-bygone.json.")
    a = ap.parse_args()

    if not os.path.isfile(a.video):
        sys.exit("video not found: " + a.video)
    description = a.description
    if a.description_file:
        with open(a.description_file) as f:
            description = f.read()
    tags = [t.strip() for t in a.tags.split(",") if t.strip()]

    creds = load_creds(a.creds)
    token = refresh_token(creds)
    status = {
        "privacyStatus": a.privacy,
        "madeForKids": a.made_for_kids == "true",
    }
    if a.publish_at:
        # Scheduled publishing: upload as private, YouTube flips it public at publishAt.
        status["privacyStatus"] = "private"
        status["publishAt"] = a.publish_at
        print("scheduled for: " + a.publish_at, flush=True)
    metadata = {
        "snippet": {
            "title": a.title,
            "description": description,
            "tags": tags,
            "categoryId": a.category,
        },
        "status": status,
    }
    print("uploading %s (%d bytes)..." % (a.video, os.path.getsize(a.video)), flush=True)
    video_id = resumable_upload(token, a.video, metadata, a.title)
    print("uploaded, video id: " + video_id, flush=True)
    if a.thumbnail:
        if not os.path.isfile(a.thumbnail):
            sys.exit("thumbnail not found: " + a.thumbnail)
        set_thumbnail(token, video_id, a.thumbnail)
        print("thumbnail set", flush=True)
    print("https://www.youtube.com/watch?v=" + video_id)


if __name__ == "__main__":
    main()
