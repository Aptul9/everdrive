import hashlib
import hmac
import logging
import secrets
import shutil
import unicodedata
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .models import RateLimit, StorageState, StoredFile

logger = logging.getLogger(__name__)
FILE_OVERHEAD = 16 * 1024


class DriveError(Exception):
    def __init__(self, message, status=400):
        self.message = message
        self.status = status


def allocated_bytes(size):
    return max(4096, ((size + 4095) // 4096) * 4096) + FILE_OVERHEAD


def clean_name(name):
    if not isinstance(name, str):
        raise DriveError("Nome file non valido.")
    name = name.replace("\\", "/").split("/")[-1]
    name = "".join(c for c in name if not unicodedata.category(c).startswith("C")).strip()
    if not name or name in (".", "..") or len(name) > 240:
        raise DriveError("Nome file non valido o troppo lungo.")
    return name


def path_for(file):
    if file.anonymous:
        return settings.FILE_DIR / "anonymous" / f"{file.id}.{int(file.expires_at.timestamp())}"
    return settings.FILE_DIR / "private" / str(file.id)


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def check_token(file, token):
    return isinstance(token, str) and hmac.compare_digest(file.upload_hash, token_hash(token))


def disk_free():
    return shutil.disk_usage(settings.DATA_DIR).free


def storage_state():
    settings.FILE_DIR.mkdir(parents=True, exist_ok=True)
    (settings.FILE_DIR / "anonymous").mkdir(exist_ok=True)
    (settings.FILE_DIR / "private").mkdir(exist_ok=True)
    state = StorageState.objects.filter(pk=1).first()
    if state:
        return state
    free = max(0, disk_free() - settings.RESERVE_BYTES - settings.HEADROOM_BYTES)
    quota = min(settings.INITIAL_QUOTA_BYTES or free, free)
    return StorageState.objects.create(pk=1, quota_bytes=quota)


def storage_stats():
    state = storage_state()
    totals = StoredFile.objects.aggregate(allocated=Sum("allocated"), received=Sum("received"))
    allocated = totals["allocated"] or 0
    written = totals["received"] or 0
    outstanding = max(0, allocated - written)
    disk_available = disk_free() - settings.RESERVE_BYTES - settings.HEADROOM_BYTES - outstanding
    available = max(0, min(state.quota_bytes - allocated, disk_available))
    return {"quota_bytes": state.quota_bytes, "used_bytes": written,
            "reserved_bytes": max(0, allocated - written), "available_bytes": available}


def rate_limit(request, purpose, limit, seconds):
    address = request.META.get("HTTP_X_REAL_IP") or request.META.get("REMOTE_ADDR", "unknown")
    key = hmac.new(settings.SECRET_KEY.encode(), f"{purpose}:{address}".encode(), hashlib.sha256).hexdigest()
    now = timezone.now()
    with transaction.atomic():
        row, _ = RateLimit.objects.get_or_create(key=key, defaults={"expires_at": now + timedelta(seconds=seconds)})
        if row.expires_at <= now:
            row.count = 0
            row.expires_at = now + timedelta(seconds=seconds)
        if row.count >= limit:
            raise DriveError("Troppi tentativi. Riprova più tardi.", 429)
        row.count += 1
        row.save()


def start_upload(name, size, anonymous):
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise DriveError("Dimensione file non valida.")
    name = clean_name(name)
    token = secrets.token_urlsafe(32)
    with transaction.atomic():
        stats = storage_stats()
        allocation = allocated_bytes(size)
        if allocation > stats["available_bytes"]:
            raise DriveError("Spazio Drive insufficiente. Caricamento non avviato.", 507)
        if StoredFile.objects.count() >= settings.MAX_FILES:
            raise DriveError("Limite del numero di file raggiunto.", 507)
        file = StoredFile.objects.create(
            name=name, size=size, allocated=allocation, anonymous=anonymous, upload_hash=token_hash(token),
            expires_at=timezone.now() + timedelta(seconds=settings.ANONYMOUS_TTL_SECONDS) if anonymous else None,
        )
    return file, token


def get_upload(file_id, token):
    file = StoredFile.objects.filter(id=file_id).first()
    if not file or not check_token(file, token):
        raise DriveError("Caricamento non trovato.", 404)
    if file.expires_at and file.expires_at <= timezone.now():
        raise DriveError("Condivisione scaduta.", 410)
    return file


def append_chunk(file_id, token, offset, data, digest):
    if not data or len(data) > settings.CHUNK_BYTES:
        raise DriveError("Blocco di caricamento non valido.", 413)
    if not isinstance(digest, str) or not hmac.compare_digest(hashlib.sha256(data).hexdigest(), digest):
        raise DriveError("Verifica del blocco fallita. Riprova il caricamento.")
    with transaction.atomic():
        file = get_upload(file_id, token)
        if file.ready or offset != file.received:
            raise DriveError("Posizione del caricamento non valida.", 409)
        if file.received + len(data) > file.size:
            raise DriveError("La dimensione dichiarata è stata superata.", 413)
        if disk_free() - len(data) < settings.RESERVE_BYTES + settings.HEADROOM_BYTES:
            raise DriveError("Spazio Drive insufficiente. Caricamento interrotto.", 507)
        path = path_for(file)
        before = file.received
        try:
            with path.open("r+b" if path.exists() else "w+b") as stream:
                stream.truncate(before)
                stream.seek(before)
                stream.write(data)
                stream.flush()
                import os
                os.fsync(stream.fileno())
            file.received += len(data)
            file.touched_at = timezone.now()
            file.save(update_fields=["received", "touched_at"])
        except OSError as exc:
            logger.error("Upload storage failure: %s", type(exc).__name__)
            if path.exists():
                with path.open("r+b") as stream:
                    stream.truncate(before)
            raise DriveError("Scrittura non riuscita. Caricamento interrotto.", 507) from exc
    return file


def finish_upload(file_id, token):
    with transaction.atomic():
        file = get_upload(file_id, token)
        if file.ready:
            return file
        if file.received != file.size:
            raise DriveError("Caricamento incompleto.", 409)
        path = path_for(file)
        if file.size == 0:
            if disk_free() < settings.RESERVE_BYTES + settings.HEADROOM_BYTES:
                raise DriveError("Spazio Drive insufficiente.", 507)
            path.touch(exist_ok=True)
        if not path.exists() or path.stat().st_size != file.size:
            raise DriveError("File incompleto sul disco.", 409)
        file.ready = True
        file.share_token = secrets.token_urlsafe(32) if file.anonymous else None
        file.touched_at = timezone.now()
        file.save(update_fields=["ready", "share_token", "touched_at"])
    return file


def remove_file(file):
    path_for(file).unlink(missing_ok=True)
    file.delete()


def cleanup():
    now = timezone.now()
    # Delete slightly early so the normal sweep cannot extend the 48-hour retention.
    due = StoredFile.objects.filter(expires_at__lte=now + timedelta(seconds=30))
    idle = StoredFile.objects.filter(ready=False, touched_at__lte=now - timedelta(seconds=settings.UPLOAD_IDLE_SECONDS))
    ids = set(due.values_list("id", flat=True)) | set(idle.values_list("id", flat=True))
    removed = 0
    for file_id in ids:
        with transaction.atomic():
            file = StoredFile.objects.filter(id=file_id).first()
            if not file:
                continue
            expired = file.expires_at and file.expires_at <= now + timedelta(seconds=30)
            abandoned = not file.ready and file.touched_at <= now - timedelta(seconds=settings.UPLOAD_IDLE_SECONDS)
            if not (expired or abandoned):
                continue
            try:
                remove_file(file)
                removed += 1
            except OSError:
                logger.exception("Cleanup failed for file %s; retry on next sweep", file_id)
    RateLimit.objects.filter(expires_at__lte=now).delete()
    # Expiry in the anonymous filename survives an interrupted database commit.
    anonymous_dir = settings.FILE_DIR / "anonymous"
    if anonymous_dir.exists():
        for path in anonymous_dir.iterdir():
            try:
                file_id, deadline = path.name.rsplit(".", 1)
                uuid.UUID(file_id)
                deadline = int(deadline)
            except (ValueError, AttributeError):
                continue
            if deadline <= int((now + timedelta(seconds=30)).timestamp()) and not StoredFile.objects.filter(id=file_id).exists():
                try:
                    path.unlink(missing_ok=True)
                    removed += 1
                except OSError:
                    logger.exception("Anonymous orphan cleanup failed; retry on next sweep")
    from django.contrib.sessions.models import Session
    Session.objects.filter(expire_date__lte=now).delete()
    return removed


def serialize(file):
    return {
        "id": str(file.id), "name": file.name, "size": file.size, "anonymous": file.anonymous,
        "created_at": file.created_at.isoformat(),
        "expires_at": file.expires_at.isoformat() if file.expires_at else None,
        "share_url": f"/s/{file.share_token}/" if file.share_token else None,
        "download_url": f"/s/{file.share_token}/download/" if file.anonymous else f"/api/files/{file.id}/download/",
    }
