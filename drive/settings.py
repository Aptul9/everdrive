import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]
DEBUG = False
ALLOWED_HOSTS = os.getenv("DRIVE_ALLOWED_HOSTS", "drive.everbright.it").split(",")
OWNER_EMAIL = "aptul4ever@gmail.com"
DATA_DIR = Path(os.getenv("DRIVE_DATA_DIR", "/data"))
FILE_DIR = DATA_DIR / "files"
RESERVE_BYTES = int(os.getenv("DRIVE_RESERVE_BYTES", "20000000000"))
HEADROOM_BYTES = 64 * 1024 * 1024
INITIAL_QUOTA_BYTES = int(os.getenv("DRIVE_QUOTA_BYTES", "0"))
CHUNK_BYTES = 4 * 1024 * 1024
ANONYMOUS_TTL_SECONDS = int(os.getenv("DRIVE_ANONYMOUS_TTL_SECONDS", "172800"))
UPLOAD_IDLE_SECONDS = 15 * 60
MAX_FILES = 10000
if not 10 <= ANONYMOUS_TTL_SECONDS <= 172800:
    raise ValueError("Anonymous retention must be between 10 seconds and 48 hours")
if RESERVE_BYTES < 20_000_000_000:
    raise ValueError("VM reserve must be at least 20 GB")

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "storage",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "storage.middleware.PrivateResponsesMiddleware",
]
ROOT_URLCONF = "drive.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
    ]},
}]
DATABASES = {"default": {
    "ENGINE": "django.db.backends.sqlite3",
    "NAME": DATA_DIR / "drive.sqlite3",
    "OPTIONS": {"timeout": 30, "transaction_mode": "IMMEDIATE"},
}}
AUTHENTICATION_BACKENDS = ["storage.auth.OwnerBackend"]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]
LANGUAGE_CODE = "it-it"
TIME_ZONE = "Europe/Rome"
USE_I18N = True
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
DATA_UPLOAD_MAX_MEMORY_SIZE = CHUNK_BYTES + 1024
DATA_UPLOAD_MAX_NUMBER_FIELDS = 20
SESSION_COOKIE_AGE = 7 * 24 * 60 * 60
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"
INSECURE_LOCAL = os.getenv("DRIVE_INSECURE_LOCAL") == "1"
SESSION_COOKIE_SECURE = not INSECURE_LOCAL
CSRF_COOKIE_SECURE = not INSECURE_LOCAL
SESSION_COOKIE_NAME = "drive_session" if INSECURE_LOCAL else "__Host-drive_session"
CSRF_COOKIE_NAME = "drive_csrf" if INSECURE_LOCAL else "__Host-drive_csrf"
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = not INSECURE_LOCAL
SECURE_HSTS_SECONDS = 31536000 if not INSECURE_LOCAL else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
# Browser preload enrollment is a separate domain operation.
SILENCED_SYSTEM_CHECKS = ["security.W021"]
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
LOGIN_URL = "/login/"
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}
