import argparse
import concurrent.futures
import hashlib
import http.cookiejar
import json
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


class Browser:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.csrf = ""

    def request(self, path, data=None, headers=None):
        values = {"Origin": self.base, "Referer": self.base + "/", **(headers or {})}
        if data is not None:
            values["X-CSRFToken"] = self.csrf
        request = urllib.request.Request(self.base + path, data=data, headers=values)
        try:
            response = self.opener.open(request, timeout=30)
        except urllib.error.HTTPError as response:
            return response.code, response.read()
        with response:
            return response.status, response.read()

    def json(self, path, value):
        status, body = self.request(path, json.dumps(value).encode(), {"Content-Type": "application/json"})
        return status, json.loads(body)

    def page(self, path):
        status, body = self.request(path)
        assert status == 200, (status, body[:200])
        match = re.search(rb'name="csrf-token" content="([^"]+)"', body)
        self.csrf = match.group(1).decode()
        return body


def run(base, password):
    owner, guest = Browser(base), Browser(base)
    for attempt in range(30):
        try:
            owner.page("/login/")
            break
        except (OSError, AssertionError):
            if attempt == 29:
                raise
            time.sleep(1)
    form = urllib.parse.urlencode({"email": "aptul4ever@gmail.com", "password": password, "csrfmiddlewaretoken": owner.csrf}).encode()
    status, page = owner.request("/login/", form, {"Content-Type": "application/x-www-form-urlencoded"})
    assert status == 200 and b"Il mio Drive" in page, (status, page[:200])
    owner.page("/")
    guest.page("/anonymous/")
    made = []
    try:
        for anonymous, browser in ((False, owner), (True, guest)):
            payload = secrets.token_bytes(5 * 1024 * 1024 + 37)
            status, upload = browser.json("/api/uploads/", {"name": "HTTP Smoke.bin", "size": len(payload), "anonymous": anonymous})
            assert status == 201, (status, upload)
            made.append((browser, upload))
            for offset in range(0, len(payload), upload["chunk_bytes"]):
                part = payload[offset:offset + upload["chunk_bytes"]]
                status, _ = browser.request(f"/api/uploads/{upload['id']}/chunk/", part, {
                    "Content-Type": "application/octet-stream", "X-Upload-Token": upload["token"],
                    "X-Upload-Offset": str(offset), "X-Chunk-Sha256": hashlib.sha256(part).hexdigest(),
                })
                assert status == 200, status
            status, body = browser.request(f"/api/uploads/{upload['id']}/finish/", b"", {"X-Upload-Token": upload["token"]})
            assert status == 200, body
            file = json.loads(body)
            status, downloaded = browser.request(file["download_url"])
            assert status == 200 and hashlib.sha256(downloaded).digest() == hashlib.sha256(payload).digest()
            if not anonymous:
                assert guest.request(file["download_url"])[0] == 401
            else:
                fresh = Browser(base)
                assert fresh.request(file["share_url"])[0] == 200
                assert fresh.request(file["download_url"])[1] == payload
            print(f'{"Anonymous" if anonymous else "Private"} multi-chunk round trip: SHA-256 match ({len(payload)} bytes)')
        assert guest.request("/api/files/")[0] == 401
        _, stats = owner.request("/api/storage/")
        available = json.loads(stats)["available_bytes"]
        assert owner.json("/api/uploads/", {"name": "Too Large.bin", "size": available + 1})[0] == 507
        print("Over-quota reservation: HTTP 507; private list without login: HTTP 401")
        # Reserve, never write, more than half the free quota twice at once.
        size = available // 2 + 1
        def reserve(_):
            return owner.json("/api/uploads/", {"name": "Concurrent.bin", "size": size})
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(reserve, range(2)))
        for status, value in outcomes:
            if status == 201:
                made.append((owner, value))
        assert sorted(status for status, _ in outcomes) == [201, 507], outcomes
        print("Concurrent reservations: one HTTP 201, one HTTP 507")
    finally:
        for browser, upload in made:
            browser.request(f"/api/uploads/{upload['id']}/cancel/", b"", {"X-Upload-Token": upload["token"]})
    print("HTTP smoke: PASS; test uploads removed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    args = parser.parse_args()
    password = sys.stdin.readline().rstrip("\r\n")
    if not password:
        raise SystemExit("Password required on stdin")
    run(args.url, password)
