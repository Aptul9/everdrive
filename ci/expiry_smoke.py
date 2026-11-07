import argparse
import hashlib
import json
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from smoke import Browser


def run(base, data_dir, password):
    owner, guest = Browser(base), Browser(base)
    owner.page("/login/")
    form = urllib.parse.urlencode({"email": "aptul4ever@gmail.com", "password": password, "csrfmiddlewaretoken": owner.csrf}).encode()
    status, page = owner.request("/login/", form, {"Content-Type": "application/x-www-form-urlencoded"})
    assert status == 200 and b"Il mio Drive" in page
    owner.page("/")
    guest.page("/anonymous/")
    records = []
    try:
        for anonymous, browser in ((False, owner), (True, guest)):
            payload = b"Drive expiry integration test"
            status, upload = browser.json("/api/uploads/", {"name": "Expiry Test.txt", "size": len(payload), "anonymous": anonymous})
            assert status == 201, upload
            records.append((browser, upload))
            status, _ = browser.request(f"/api/uploads/{upload['id']}/chunk/", payload, {
                "Content-Type": "application/octet-stream", "X-Upload-Token": upload["token"],
                "X-Upload-Offset": "0", "X-Chunk-Sha256": hashlib.sha256(payload).hexdigest(),
            })
            assert status == 200
            status, body = browser.request(f"/api/uploads/{upload['id']}/finish/", b"", {"X-Upload-Token": upload["token"]})
            assert status == 200
            file = json.loads(body)
            if anonymous:
                shared = file
                deadline = datetime.fromisoformat(file["expires_at"])
                path = Path(data_dir) / "files" / "anonymous" / f"{file['id']}.{int(deadline.timestamp())}"
                assert path.is_file(), path
            else:
                private = file
        wait = (deadline - datetime.now(timezone.utc)).total_seconds() + 1
        assert 0 < wait < 90, "Expiry test only runs against an isolated short-retention instance"
        print(f"Anonymous share accessible; waiting {wait:.1f}s for configured expiry", flush=True)
        time.sleep(wait)
        assert guest.request(shared["download_url"])[0] == 404
        assert guest.request(shared["share_url"])[0] == 404
        assert not path.exists(), "Anonymous content still present on disk"
        assert owner.request(private["download_url"])[0] == 200
        print("Expiry smoke: PASS; anonymous link HTTP 404, bytes removed from disk, private file retained")
    finally:
        for browser, upload in records:
            browser.request(f"/api/uploads/{upload['id']}/cancel/", b"", {"X-Upload-Token": upload["token"]})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--data-dir", required=True)
    args = parser.parse_args()
    password = sys.stdin.readline().rstrip("\r\n")
    if not password:
        raise SystemExit("Password required on stdin")
    run(args.url, args.data_dir, password)
