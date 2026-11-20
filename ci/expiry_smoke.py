import argparse
import hashlib
import json
import sys
import subprocess
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from smoke import Browser


def run(base, data_dir, session, container=None):
    def exists(path):
        if not container:
            return path.is_file()
        result = subprocess.run([
            "docker", "exec", container, "python", "-c",
            "import json,pathlib,sys; print(json.dumps(pathlib.Path(sys.argv[1]).is_file()))", str(path),
        ], check=True, capture_output=True, text=True)
        return json.loads(result.stdout)

    owner, guest = Browser(base), Browser(base)
    owner.session = session
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
                root = PurePosixPath(data_dir) if container else Path(data_dir)
                path = root / "files" / "anonymous" / f"{file['id']}.{int(deadline.timestamp())}"
                assert exists(path), path
            else:
                private = file
        wait = (deadline - datetime.now(timezone.utc)).total_seconds() + 1
        assert 0 < wait < 90, "Expiry test only runs against an isolated short-retention instance"
        print(f"Anonymous share accessible; waiting {wait:.1f}s for configured expiry", flush=True)
        time.sleep(wait)
        assert guest.request(shared["download_url"])[0] == 404
        assert guest.request(shared["share_url"])[0] == 404
        assert not exists(path), "Anonymous content still present on disk"
        assert owner.request(private["download_url"])[0] == 200
        print("Expiry smoke: PASS; anonymous link HTTP 404, bytes removed from disk, private file retained")
    finally:
        for browser, upload in records:
            browser.request(f"/api/uploads/{upload['id']}/cancel/", b"", {"X-Upload-Token": upload["token"]})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--container")
    args = parser.parse_args()
    session = sys.stdin.readline().rstrip("\r\n")
    if not session:
        raise SystemExit("Test session required on stdin")
    run(args.url, args.data_dir, session, args.container)
