import hashlib
import json
import secrets
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from .everid import discovery as real_discovery
from . import everid
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
        self.assertEqual(self.guest.put("/login/").status_code, 405)
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


PASSWORD = secrets.token_urlsafe(24)
EVERID = dict(
    OIDC_ENABLED=True, OIDC_RP_CLIENT_ID="everdrive-client", OIDC_RP_CLIENT_SECRET="everdrive-secret",
    OIDC_ISSUER="https://id.example.test/application/o/everdrive/", OIDC_ORIGIN="https://id.example.test",
    OIDC_OP_AUTHORIZATION_ENDPOINT="https://id.example.test/application/o/authorize/",
    OIDC_OP_LOGOUT_ENDPOINT="https://id.example.test/application/o/everdrive/end-session/",
    ALLOWED_HOSTS=["testserver"], SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False,
)


DISCOVERY = {
    "issuer": EVERID["OIDC_ISSUER"],
    "authorization_endpoint": "https://id.example.test/application/o/authorize/",
    "token" + "_endpoint": "https://id.example.test/application/o/" + "token/",
    "userinfo_endpoint": "https://id.example.test/application/o/userinfo/",
    "jwks_uri": "https://id.example.test/application/o/everdrive/jwks/",
    "end_session_endpoint": "https://id.example.test/application/o/everdrive/end-session/",
}


@override_settings(**EVERID)
class EverIDTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(settings.OWNER_EMAIL, settings.OWNER_EMAIL, PASSWORD)
        self.client = Client()
        discovery = patch("storage.everid.discovery", return_value=DISCOVERY)
        discovery.start()
        self.addCleanup(discovery.stop)

    def form_login(self, username, result=None):
        """POST the app's own form; EverID is replaced by a stub that answers like the real sign-in."""
        seen = {}

        def fake_sign_in(user, password, authorize_url):
            seen.update(user=user, password=password, url=authorize_url)
            if result is not None:
                return result
            query = parse_qs(urlsplit(authorize_url).query)
            return query["redirect_uri"][0] + "?" + urlencode({"code": "code", "state": query["state"][0]})

        with patch("storage.everid.sign_in", side_effect=fake_sign_in):
            response = self.client.post("/login/", {"email": username, "password": "the-password"})
        return response, seen

    def everid_login(self, email, groups, iss=EVERID["OIDC_ISSUER"], aud="everdrive-client"):
        start, seen = self.form_login(email)
        self.assertEqual(start.status_code, 302)
        self.assertEqual(seen["password"], "the-password")
        self.assertTrue(seen["url"].startswith("https://id.example.test/application/o/authorize/?"))
        query = parse_qs(urlsplit(seen["url"]).query)
        self.assertEqual(query["redirect_uri"], ["http://testserver/oidc/callback"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["scope"], ["openid email profile"])
        self.assertNotIn("prompt", query)
        callback = urlsplit(start["Location"])
        self.assertEqual(callback.path, "/oidc/callback")
        id_token = {"iss": iss, "aud": aud, "nonce": query["nonce"][0], "email": email}
        claims = {"email": email, "groups": groups}
        base = "mozilla_django_oidc.auth.OIDCAuthenticationBackend."
        with patch(base + "get_token", return_value={"id_token": "id-token", "access_token": "access"}), \
                patch(base + "verify_token", return_value=id_token), \
                patch(base + "get_userinfo", return_value=claims):
            return self.client.get("/oidc/callback", dict(parse_qs(callback.query)))

    def test_group_allowed_matches_existing_owner_by_email(self):
        response = self.everid_login(settings.OWNER_EMAIL.upper(), ["app-drive", "everid-admins"])
        self.assertRedirects(response, "/", fetch_redirect_response=False)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.owner.pk)
        self.assertEqual(self.client.get("/api/files/").status_code, 200)
        self.assertEqual(get_user_model().objects.count(), 1)

    def test_new_user_created_and_sees_shared_files(self):
        StoredFile.objects.create(name="Owner.txt", size=1, allocated=1, ready=True, upload_hash="0" * 64)
        response = self.everid_login("newcomer@example.com", ["app-drive"])
        self.assertEqual(response.status_code, 302)
        user = get_user_model().objects.get(email="newcomer@example.com")
        self.assertFalse(user.has_usable_password())
        names = [f["name"] for f in self.client.get("/api/files/").json()["files"]]
        self.assertEqual(names, ["Owner.txt"])

    def test_group_denied_403_and_no_session(self):
        response = self.everid_login("outsider@example.com", ["app-push"])
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "non ha accesso a EverDrive", status_code=403)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertFalse(get_user_model().objects.filter(email="outsider@example.com").exists())
        self.assertEqual(self.client.get("/api/files/").status_code, 401)

    def test_wrong_issuer_or_audience_rejected(self):
        for kwargs in ({"iss": "https://evil.test/"}, {"aud": "other-client"}):
            self.client = Client()
            response = self.everid_login(settings.OWNER_EMAIL, ["app-drive"], **kwargs)
            self.assertEqual(response.status_code, 400)
            self.assertNotIn("_auth_user_id", self.client.session)

    def test_wrong_password_generic_message(self):
        response, _ = self.form_login(settings.OWNER_EMAIL, everid.WRONG)
        self.assertContains(response, "Email o password non corretti.", status_code=400)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_everid_refuses_app_403(self):
        response, _ = self.form_login("outsider@example.com", everid.DENIED)
        self.assertContains(response, "non ha accesso a EverDrive", status_code=403)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_login_rate_limited(self):
        for _ in range(10):
            self.form_login("x", everid.WRONG)
        response, seen = self.form_login("x", everid.WRONG)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(seen, {})

    def test_csrf_required_on_form(self):
        strict = Client(enforce_csrf_checks=True)
        self.assertEqual(strict.post("/login/", {"email": "a", "password": "b"}).status_code, 403)

    def test_logout_is_local_and_shows_the_form(self):
        self.everid_login(settings.OWNER_EMAIL, ["app-drive"])
        response = self.client.post("/logout/")
        self.assertRedirects(response, "/login/", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/api/files/").status_code, 401)
        self.assertContains(self.client.get("/login/"), 'data-testid="login-password"')

    def test_discovery_failure_fails_closed_to_login_page(self):
        with patch("storage.everid.discovery", side_effect=ValueError("down")):
            response = self.client.post("/login/", {"email": "a", "password": "b"})
        self.assertEqual(response.status_code, 503)

    def test_discovery_rejects_foreign_issuer(self):
        response = Mock(json=lambda: {**DISCOVERY, "issuer": "https://evil.test/"})
        with patch("storage.everid._config", {}), patch("storage.everid.requests.get", return_value=response):
            with self.assertRaises(ValueError):
                real_discovery()

    def test_login_page_is_the_apps_own_form(self):
        response = self.client.get("/login/")
        page = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn('data-testid="login-password"', page)
        self.assertNotIn("EverID", page)
        self.assertNotIn("id.example.test", page)
        self.assertNotIn("/oidc/login/", page)

    @override_settings(LEGACY_LOGIN=False)
    def test_local_password_never_checked(self):
        self.assertIsNone(authenticate(username=settings.OWNER_EMAIL, password=PASSWORD))
        response, _ = self.form_login(settings.OWNER_EMAIL, everid.WRONG)
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_legacy_session_dropped_when_legacy_turned_off(self):
        self.client.force_login(self.owner, backend="storage.auth.OwnerBackend")
        self.assertEqual(self.client.get("/api/files/").status_code, 200)
        with override_settings(LEGACY_LOGIN=False):
            self.assertEqual(self.client.get("/api/files/").status_code, 401)

    def test_public_share_routes_need_no_login(self):
        self.assertEqual(self.client.get("/anonymous/").status_code, 200)
        self.assertEqual(self.client.get("/s/" + "a" * 43 + "/").status_code, 404)

    @override_settings(OIDC_ENABLED=False)
    def test_everid_routes_absent_when_not_configured(self):
        self.assertEqual(self.client.post("/login/", {"email": "a", "password": "b"}).status_code, 503)
        self.assertEqual(self.client.get("/oidc/callback", {"code": "x", "state": "y"}).status_code, 404)


class SignInTests(TestCase):
    """everid.sign_in against a fake EverID: stages, wrong password, MFA, refused app."""

    AUTHORIZE = "https://id.example.test/application/o/authorize/?" + urlencode(
        {"client_id": "everdrive-client", "redirect_uri": "https://drive.test/oidc/callback", "state": "s1"})

    def run_flow(self, steps, authorize_location):
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        session.headers = {}
        session.cookies = {"authentik_csrf": "c"}
        brand = Mock(json=lambda: {"flow_authentication": "accesso"})
        first, *rest = steps
        session.get.side_effect = [brand, Mock(json=lambda: first), Mock(headers={"Location": authorize_location})]
        session.post.side_effect = [Mock(json=(lambda s=s: s)) for s in rest]
        with override_settings(**EVERID), patch("storage.everid.requests.Session", return_value=session):
            return everid.sign_in("aptul", "pw", self.AUTHORIZE), session

    def test_success_returns_app_callback(self):
        callback = "https://drive.test/oidc/callback?code=abc&state=s1"
        result, session = self.run_flow([{"component": "ak-stage-identification"}, {"component": "ak-stage-password"},
                                         {"component": "xak-flow-redirect"}], callback)
        self.assertEqual(result, callback)
        self.assertEqual(session.post.call_args_list[0].kwargs["json"]["uid_field"], "aptul")
        self.assertEqual(session.post.call_args_list[1].kwargs["headers"], {"X-authentik-CSRF": "c"})

    def test_wrong_password(self):
        result, _ = self.run_flow([{"component": "ak-stage-identification"}, {"component": "ak-stage-password"},
                                   {"component": "ak-stage-password", "response_errors": {"password": ["x"]}}], "")
        self.assertEqual(result, everid.WRONG)

    def test_mfa_stage_refused(self):
        result, _ = self.run_flow([{"component": "ak-stage-identification"},
                                   {"component": "ak-stage-authenticator-validate"}], "")
        self.assertEqual(result, everid.WRONG)

    def test_app_refused_after_good_credentials(self):
        result, _ = self.run_flow([{"component": "ak-stage-identification"}, {"component": "xak-flow-redirect"}],
                                  "/if/flow/accesso/?denied")
        self.assertEqual(result, everid.DENIED)
