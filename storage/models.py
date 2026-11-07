import uuid
from django.db import models


class StorageState(models.Model):
    quota_bytes = models.PositiveBigIntegerField()


class StoredFile(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=240)
    size = models.PositiveBigIntegerField()
    allocated = models.PositiveBigIntegerField()
    received = models.PositiveBigIntegerField(default=0)
    anonymous = models.BooleanField(default=False, db_index=True)
    ready = models.BooleanField(default=False)
    upload_hash = models.CharField(max_length=64)
    share_token = models.CharField(max_length=43, unique=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    touched_at = models.DateTimeField(auto_now_add=True, db_index=True)
    expires_at = models.DateTimeField(null=True, db_index=True)


class RateLimit(models.Model):
    key = models.CharField(max_length=64, primary_key=True)
    count = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField(db_index=True)
