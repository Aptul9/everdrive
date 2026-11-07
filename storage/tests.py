import hashlib
import json
import secrets
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from .models import StoredFile
from . import service


class DriveTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.override = override_settings(
            DATA_DIR=root, FILE_DIR=root / "files", INITIAL_QUOTA_BYTES=16 * 1024 * 1024,
            ALLOWED_HOSTS=["testserver", "localhost"], INSECURE_LOCAL=True,
            SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False,
        )
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.free = patch("storage.service.disk_free", return_value=settings.RESERVE_BYTES + 1024 ** 3)
        self.free.start()
        self.addCleanup(self.free.stop)
        self.password = secrets.token_urlsafe(24)
        self.user = get_user_model().objects.create_user(settings.OWNER_EMAIL, settings.OWNER_EMAIL, self.password)
        self.owner = Client()
        self.owner.force_login(self.user)
        self.guest = Client()

    def post(self, client, url, value):
        return client.post(url, json.dumps(value), content_type="application/json")

    def upload(self, client=None, data=b"hello drive", anonymous=False, name="Example.txt"):
        client = client or self.owner
        response = self.post(client, "/api/uploads/", {"name": name, "size": len(data), "anonymous": anonymous})
        self.assertEqual(response.status_code, 201, response.content)
        started = response.json()
        if data:
            response = client.post(f"/api/uploads/{started['id']}/chunk/", data, content_type="application/octet-stream",
                                   HTTP_X_UPLOAD_TOKEN=started["token"], HTTP_X_UPLOAD_OFFSET="0",
                                   HTTP_X_CHUNK_SHA256=hashlib.sha256(data).hexdigest())
            self.assertEqual(response.status_code, 200, response.content)
        response = client.post(f"/api/uploads/{started['id']}/finish/", HTTP_X_UPLOAD_TOKEN=started["token"])
        self.assertEqual(response.status_code, 200, response.content)
        return response.json(), started

    def test_owner_auth_only_and_no_registration(self):
        self.assertIsNotNone(authenticate(username=settings.OWNER_EMAIL, password=self.password))
        get_user_model().objects.create_user("other@example.com", password=self.password)
        self.assertIsNone(authenticate(username="other@example.com", password=self.password))
        for path in ("/register/", "/signup/", "/admin/"):
            self.assertEqual(self.guest.get(path).status_code, 404)

    def test_bootstrap_never_replaces_existing_password(self):
        call_command("bootstrap_owner")
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.password))

    def test_private_round_trip_and_anonymous_denied(self):
        data = secrets.token_bytes(512 * 1024)
        file, _ = self.upload(data=data)
        response = self.owner.get(file["download_url"])
        self.assertEqual(b"".join(response.streaming_content), data)
        self.assertEqual(response["Content-Type"], "application/octet-stream")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertIn("sandbox", response["Content-Security-Policy"])
        self.assertEqual(self.guest.get(file["download_url"]).status_code, 401)
        self.assertEqual(self.guest.get("/api/files/").status_code, 401)
        self.assertIsNone(file["expires_at"])

    def test_guest_cannot_upload_private(self):
        response = self.post(self.guest, "/api/uploads/", {"name": "x", "size": 2, "anonymous": False})
        self.assertEqual(response.status_code, 401)

    def test_anonymous_share_without_login(self):
        file, _ = self.upload(self.guest, anonymous=True)
        self.assertEqual(len(file["share_url"].split("/")[2]), 43)
        self.assertEqual(self.guest.get(file["share_url"]).status_code, 200)
        response = self.guest.get(file["download_url"])
        self.assertEqual(b"".join(response.streaming_content), b"hello drive")
        self.assertEqual(self.owner.get("/api/files/").json()["files"], [])
        self.assertEqual(len(self.owner.get("/api/files/?anonymous=1").json()["files"]), 1)

    def test_guest_list_requires_known_share_tokens(self):
        file, _ = self.upload(self.guest, anonymous=True)
        self.assertEqual(self.post(self.guest, "/api/anonymous-files/", {"tokens": []}).json()["files"], [])
        token = file["share_url"].split("/")[2]
        self.assertEqual(len(self.post(self.guest, "/api/anonymous-files/", {"tokens": [token]}).json()["files"]), 1)

    def test_invalid_share_is_not_available(self):
        self.assertEqual(self.guest.get(f"/s/{secrets.token_urlsafe(32)}/").status_code, 404)

    def test_deadline_is_at_most_48_hours(self):
        file, _ = self.upload(anonymous=True)
        row = StoredFile.objects.get(id=file["id"])
        self.assertLessEqual((row.expires_at - row.created_at).total_seconds(), 172800)

    def test_expired_share_denied_even_without_cleanup(self):
        file, _ = self.upload(anonymous=True)
        StoredFile.objects.filter(id=file["id"]).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.guest.get(file["share_url"]).status_code, 404)
        self.assertEqual(self.guest.get(file["download_url"]).status_code, 404)

    def test_cleanup_removes_disk_and_record_not_private_files(self):
        file, _ = self.upload(anonymous=True)
        private, _ = self.upload()
        row = StoredFile.objects.get(id=file["id"])
        path = service.path_for(row)
        with patch("storage.service.timezone.now", return_value=row.expires_at - timedelta(seconds=20)):
            self.assertEqual(service.cleanup(), 1)
        self.assertFalse(path.exists())
        self.assertFalse(StoredFile.objects.filter(id=file["id"]).exists())
        self.assertTrue(StoredFile.objects.filter(id=private["id"]).exists())

    def test_cleanup_abandoned_upload_and_releases_quota(self):
        file, _ = service.start_upload("interrupted.bin", 1000000, True)
        before = service.storage_stats()["available_bytes"]
        service.path_for(file).write_bytes(b"partial")
        StoredFile.objects.filter(id=file.id).update(touched_at=timezone.now() - timedelta(minutes=16))
        self.assertEqual(service.cleanup(), 1)
        self.assertGreater(service.storage_stats()["available_bytes"], before)

    def test_orphan_expiry_survives_missing_database_row(self):
        service.storage_state()
        orphan = settings.FILE_DIR / "anonymous" / f"{secrets.token_hex(16)}.{int(timezone.now().timestamp()) - 1}"
        orphan.write_bytes(b"expired anonymous data")
        private = settings.FILE_DIR / "private" / secrets.token_hex(16)
        private.write_bytes(b"private data must stay")
        self.assertEqual(service.cleanup(), 1)
        self.assertFalse(orphan.exists())
        self.assertTrue(private.exists())

    def test_allocated_quota_includes_concurrent_pending_uploads(self):
        service.start_upload("first.bin", 10 * 1024 * 1024, False)
        with self.assertRaises(service.DriveError) as failure:
            service.start_upload("second.bin", 10 * 1024 * 1024, True)
        self.assertEqual(failure.exception.status, 507)

    def test_dynamic_free_space_reserve_blocks_start(self):
        with patch("storage.service.disk_free", return_value=settings.RESERVE_BYTES + settings.HEADROOM_BYTES):
            response = self.post(self.owner, "/api/uploads/", {"name": "x", "size": 1})
        self.assertEqual(response.status_code, 507)
        self.assertFalse(StoredFile.objects.exists())

    def test_dynamic_free_space_reserve_blocks_in_progress_chunk(self):
        file, token = service.start_upload("x.bin", 100, False)
        with patch("storage.service.disk_free", return_value=settings.RESERVE_BYTES + settings.HEADROOM_BYTES):
            with self.assertRaises(service.DriveError) as failure:
                service.append_chunk(file.id, token, 0, b"x", hashlib.sha256(b"x").hexdigest())
        self.assertEqual(failure.exception.status, 507)
        self.assertFalse(service.path_for(file).exists())

    def test_live_available_can_decrease_when_other_apps_write(self):
        original = service.storage_stats()["available_bytes"]
        with patch("storage.service.disk_free", return_value=settings.RESERVE_BYTES + settings.HEADROOM_BYTES + 1024 * 1024):
            current = service.storage_stats()["available_bytes"]
        self.assertLess(current, original)

    def test_overflow_and_negative_sizes_rejected(self):
        for size in (-1, True, "100", 10 ** 100):
            response = self.post(self.owner, "/api/uploads/", {"name": "x", "size": size})
            self.assertIn(response.status_code, (400, 507))

    def test_unsafe_filename_is_normalized_and_escaped(self):
        file, _ = self.upload(name="../../<script>alert(1)</script>.txt", anonymous=True)
        page = self.guest.get(file["share_url"]).content.decode()
        self.assertNotIn("<script>alert", page)
        self.assertNotIn("../", file["name"])

    def test_zero_byte_file(self):
        file, _ = self.upload(data=b"")
        response = self.owner.get(file["download_url"])
        self.assertEqual(b"".join(response.streaming_content), b"")

    def test_wrong_offset_wrong_token_and_hash_rejected(self):
        file, token = service.start_upload("x", 5, False)
        digest = hashlib.sha256(b"hello").hexdigest()
        for supplied, offset, checksum, status in (("bad", 0, digest, 404), (token, 1, digest, 409), (token, 0, "bad", 400)):
            with self.assertRaises(service.DriveError) as failure:
                service.append_chunk(file.id, supplied, offset, b"hello", checksum)
            self.assertEqual(failure.exception.status, status)
        file.refresh_from_db()
        self.assertEqual(file.received, 0)

    def test_overrun_and_incomplete_finish_rejected(self):
        file, token = service.start_upload("x", 2, False)
        with self.assertRaises(service.DriveError) as failure:
            service.append_chunk(file.id, token, 0, b"hello", hashlib.sha256(b"hello").hexdigest())
        self.assertEqual(failure.exception.status, 413)
        with self.assertRaises(service.DriveError) as failure:
            service.finish_upload(file.id, token)
        self.assertEqual(failure.exception.status, 409)

    def test_uncommitted_tail_is_truncated_before_retry(self):
        file, token = service.start_upload("x", 5, False)
        service.path_for(file).write_bytes(b"uncommitted bytes")
        service.append_chunk(file.id, token, 0, b"hello", hashlib.sha256(b"hello").hexdigest())
        service.finish_upload(file.id, token)
        self.assertEqual(service.path_for(file).read_bytes(), b"hello")

    def test_finish_is_idempotent(self):
        file, started = self.upload(anonymous=True)
        again = service.finish_upload(started["id"], started["token"])
        self.assertEqual(again.share_token, file["share_url"].split("/")[2])

    def test_guest_cannot_delete_with_share_link(self):
        file, _ = self.upload(anonymous=True)
        response = self.guest.post(f"/api/files/{file['id']}/delete/")
        self.assertEqual(response.status_code, 401)
        response = self.guest.post(f"/api/uploads/{file['id']}/cancel/", HTTP_X_UPLOAD_TOKEN=file["share_url"].split("/")[2])
        self.assertEqual(response.status_code, 404)

    def test_delete_with_owner_or_uploader_token(self):
        file, started = self.upload(self.guest, anonymous=True)
        row = StoredFile.objects.get(id=file["id"])
        path = service.path_for(row)
        response = self.guest.post(f"/api/uploads/{file['id']}/cancel/", HTTP_X_UPLOAD_TOKEN=started["token"])
        self.assertEqual(response.status_code, 200)
        self.assertFalse(path.exists())
        private, _ = self.upload()
        self.assertEqual(self.owner.post(f"/api/files/{private['id']}/delete/").status_code, 200)

    def test_csrf_required_for_guest_and_owner(self):
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(self.post(client, "/api/uploads/", {"name": "x", "size": 1, "anonymous": True}).status_code, 403)
        client.force_login(self.user)
        self.assertEqual(self.post(client, "/api/uploads/", {"name": "x", "size": 1}).status_code, 403)

    def test_anonymous_rate_limit(self):
        with override_settings(MAX_FILES=100):
            for _ in range(30):
                self.assertEqual(self.post(self.guest, "/api/uploads/", {"name": "x", "size": 0, "anonymous": True}).status_code, 201)
            self.assertEqual(self.post(self.guest, "/api/uploads/", {"name": "x", "size": 0, "anonymous": True}).status_code, 429)

    def test_login_rate_limit_and_logout_post_only(self):
        for _ in range(10):
            response = self.guest.post("/login/", {"email": "nobody@example.com", "password": "wrong"})
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.guest.post("/login/", {"email": "nobody@example.com", "password": "wrong"}).status_code, 429)
        self.assertEqual(self.owner.get("/logout/").status_code, 405)

    def test_no_storage_location_or_vm_space_leaked(self):
        data = self.guest.get("/api/storage/").json()
        self.assertEqual(set(data), {"quota_bytes", "used_bytes", "reserved_bytes", "available_bytes"})
        self.assertNotIn(str(settings.DATA_DIR), str(data))

    def test_security_headers_and_live_capacity_on_each_page(self):
        for url in ("/login/", "/anonymous/"):
            response = self.guest.get(url)
            self.assertEqual(response["X-Frame-Options"], "DENY")
            self.assertEqual(response["Cache-Control"], "no-store")
            self.assertEqual(response["Referrer-Policy"], "same-origin")
            self.assertIn("frame-ancestors 'none'", response["Content-Security-Policy"])
            self.assertContains(response, 'data-testid="storage-available"')
