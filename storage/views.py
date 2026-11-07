import json
from functools import wraps

from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.db import connection, transaction
from django.http import Http404, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.http import content_disposition_header
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from .models import StoredFile
from . import service


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


@ensure_csrf_cookie
def login_view(request):
    if request.user.is_authenticated:
        return redirect("home")
    error = None
    status = 200
    if request.method == "POST":
        try:
            service.rate_limit(request, "login", 10, 15 * 60)
            user = authenticate(request, username=request.POST.get("email", ""), password=request.POST.get("password", ""))
            if user:
                login(request, user)
                return redirect("home")
            error = "Email o password non corretti."
            status = 400
        except service.DriveError as exc:
            error, status = exc.message, exc.status
    elif request.method != "GET":
        return HttpResponse(status=405)
    return render(request, "login.html", {"error": error}, status=status)


@require_POST
def logout_view(request):
    logout(request)
    return redirect("login")


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
