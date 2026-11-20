import json
import logging
from functools import wraps

import requests
from django.conf import settings
from django.contrib.auth import logout
from django.core.exceptions import SuspiciousOperation
from django.db import connection, transaction
from django.http import Http404, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.http import content_disposition_header, urlencode
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST
from mozilla_django_oidc.views import OIDCAuthenticationCallbackView, OIDCAuthenticationRequestView

from .models import StoredFile
from . import everid, service

LOGGER = logging.getLogger(__name__)


def api(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except service.DriveError as exc:
            return JsonResponse({"error": exc.message}, status=exc.status)
        except (ValueError, TypeError, json.JSONDecodeError):
            return JsonResponse({"error": "Richiesta non valida."}, status=400)
    return wrapped


def body(request):
    if request.content_type != "application/json" or len(request.body) > 16 * 1024:
        raise service.DriveError("Richiesta non valida.")
    value = json.loads(request.body)
    if not isinstance(value, dict):
        raise service.DriveError("Richiesta non valida.")
    return value


def owner_only(request):
    if not request.user.is_authenticated:
        raise service.DriveError("Accedi per visualizzare i tuoi file.", 401)


@ensure_csrf_cookie
@require_GET
def home(request):
    if not request.user.is_authenticated:
        return redirect("login")
    return render(request, "drive.html", {"anonymous_mode": False})


@ensure_csrf_cookie
@require_GET
def anonymous(request):
    return render(request, "drive.html", {"anonymous_mode": True})


def login_page(request, error=None, status=200):
    return render(request, "login.html", {
        "error": error, "sso": settings.OIDC_ENABLED,
    }, status=status)


@ensure_csrf_cookie
def login_view(request):
    if request.user.is_authenticated:
        return redirect("home")
    if request.method != "GET":
        return HttpResponse(status=405)
    if settings.OIDC_ENABLED and not request.COOKIES.get("sso_signed_out"):
        return redirect("/oidc/login/?silent=1")
    return login_page(request)


class EverIDCallbackView(OIDCAuthenticationCallbackView):
    def get(self, request):
        if not settings.OIDC_ENABLED:
            raise Http404
        if request.GET.get("error"):
            state = request.GET.get("state")
            if not state or state not in request.session.get("oidc_states", {}):
                raise SuspiciousOperation("Invalid OIDC callback state")
            request.silent_auth = request.session["oidc_states"][state].get("silent", False)
        return super().get(request)

    def login_success(self):
        response = super().login_success()
        response.delete_cookie("sso_signed_out")
        return response

    def login_failure(self):
        error = self.request.GET.get("error")
        if getattr(self.request, "everid_denied", False) or error == "access_denied":
            return login_page(self.request, "Il tuo account non ha accesso a EverDrive.", 403)
        if getattr(self.request, "silent_auth", False) and error in {"login_required", "interaction_required", "consent_required", "account_selection_required"}:
            return redirect("/oidc/login/")
        return login_page(self.request, "Accesso non disponibile. Riprova.", 400)


@require_GET
def everid_login(request):
    if not settings.OIDC_ENABLED:
        raise Http404
    if request.user.is_authenticated:
        return redirect("home")
    return EverIDRequestView.as_view()(request)


class EverIDRequestView(OIDCAuthenticationRequestView):
    def get(self, request):
        try:
            self.OIDC_OP_AUTH_ENDPOINT = everid.discovery()["authorization_endpoint"]
        except (requests.RequestException, ValueError, KeyError) as exc:
            LOGGER.warning("EverID discovery failed: %s", exc)
            return login_page(request, "Accesso non disponibile. Riprova.", 503)
        response = super().get(request)
        if request.GET.get("silent") == "1":
            from urllib.parse import parse_qs, urlsplit
            state = parse_qs(urlsplit(response["Location"]).query)["state"][0]
            request.session["oidc_states"][state]["silent"] = True
            request.session.modified = True
        return response

    def get_extra_params(self, request):
        return {"prompt": "none"} if request.GET.get("silent") == "1" else {}


def end_session_endpoint():
    try:
        return everid.discovery()["end_session_endpoint"]
    except (requests.RequestException, ValueError, KeyError):
        return settings.OIDC_OP_LOGOUT_ENDPOINT


@require_POST
def logout_view(request):
    id_token = request.session.get("oidc_id_token")
    logout(request)
    if settings.OIDC_ENABLED and id_token:
        query = urlencode({
            "id_token_hint": id_token,
            "post_logout_redirect_uri": request.build_absolute_uri("/"),
        })
        response = redirect(f"{end_session_endpoint()}?{query}")
    else:
        response = redirect("login")
    response.set_cookie("sso_signed_out", "1", max_age=86400, secure=not settings.INSECURE_LOCAL, httponly=True, samesite="Lax")
    return response


@require_GET
def health(request):
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return JsonResponse({"status": "ok"})


@require_GET
@api
def capacity(request):
    with transaction.atomic():
        return JsonResponse(service.storage_stats())


@require_GET
@api
def files(request):
    owner_only(request)
    anonymous_mode = request.GET.get("anonymous") == "1"
    rows = StoredFile.objects.filter(ready=True, anonymous=anonymous_mode)
    if anonymous_mode:
        rows = rows.filter(expires_at__gt=timezone.now())
    return JsonResponse({"files": [service.serialize(file) for file in rows.order_by("-created_at")]})


@require_POST
@api
def anonymous_files(request):
    tokens = body(request).get("tokens", [])
    if not isinstance(tokens, list) or len(tokens) > 200 or any(not isinstance(t, str) or len(t) != 43 for t in tokens):
        raise service.DriveError("Richiesta non valida.")
    rows = StoredFile.objects.filter(ready=True, anonymous=True, share_token__in=tokens, expires_at__gt=timezone.now())
    return JsonResponse({"files": [service.serialize(file) for file in rows.order_by("-created_at")]})


@require_POST
@api
def begin(request):
    value = body(request)
    anonymous_mode = value.get("anonymous") is True
    if not anonymous_mode:
        owner_only(request)
    if not request.user.is_authenticated:
        service.rate_limit(request, "anonymous-upload", 30, 3600)
    file, token = service.start_upload(value.get("name"), value.get("size"), anonymous_mode)
    return JsonResponse({"id": str(file.id), "token": token, "chunk_bytes": settings.CHUNK_BYTES}, status=201)


@require_POST
@api
def chunk(request, file_id):
    length = int(request.META.get("CONTENT_LENGTH") or "0")
    if length <= 0 or length > settings.CHUNK_BYTES:
        raise service.DriveError("Blocco troppo grande o vuoto.", 413)
    data = request.body
    if len(data) != length:
        raise service.DriveError("Blocco incompleto.")
    file = service.append_chunk(file_id, request.headers.get("X-Upload-Token", ""),
                                int(request.headers.get("X-Upload-Offset", "-1")), data,
                                request.headers.get("X-Chunk-Sha256", ""))
    return JsonResponse({"received": file.received})


@require_POST
@api
def finish(request, file_id):
    return JsonResponse(service.serialize(service.finish_upload(file_id, request.headers.get("X-Upload-Token", ""))))


@require_POST
@api
def cancel(request, file_id):
    with transaction.atomic():
        file = service.get_upload(file_id, request.headers.get("X-Upload-Token", ""))
        service.remove_file(file)
    return JsonResponse({"deleted": True})


@require_POST
@api
def delete(request, file_id):
    owner_only(request)
    with transaction.atomic():
        file = StoredFile.objects.filter(id=file_id).first()
        if not file:
            raise service.DriveError("File non trovato.", 404)
        service.remove_file(file)
    return JsonResponse({"deleted": True})


def public_file(token):
    file = StoredFile.objects.filter(share_token=token, anonymous=True, ready=True, expires_at__gt=timezone.now()).first()
    if not file or not service.path_for(file).is_file():
        raise Http404
    return file


@require_GET
@ensure_csrf_cookie
def share(request, token):
    try:
        file = public_file(token)
    except Http404:
        return render(request, "share.html", {"missing": True}, status=404)
    return render(request, "share.html", {"file": file})


def download(file):
    try:
        stream = service.path_for(file).open("rb")
    except FileNotFoundError:
        raise Http404

    def chunks():
        try:
            while not file.expires_at or timezone.now() < file.expires_at:
                part = stream.read(256 * 1024)
                if not part:
                    break
                yield part
        finally:
            stream.close()

    response = StreamingHttpResponse(chunks(), content_type="application/octet-stream")
    response["Content-Length"] = str(file.size)
    response["Content-Disposition"] = content_disposition_header(True, file.name)
    response["Content-Security-Policy"] = "sandbox; default-src 'none'"
    return response


@require_GET
@api
def private_download(request, file_id):
    owner_only(request)
    file = StoredFile.objects.filter(id=file_id, anonymous=False, ready=True).first()
    if not file:
        raise Http404
    return download(file)


@require_GET
def public_download(request, token):
    return download(public_file(token))
