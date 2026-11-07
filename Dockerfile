FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home drive
COPY drive ./drive
COPY storage ./storage
COPY templates ./templates
COPY static ./static
COPY manage.py .
USER 10001:10001
EXPOSE 8000
CMD ["gunicorn", "drive.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "4", "--timeout", "120", "--max-requests", "2000", "--max-requests-jitter", "200", "--access-logfile", "/dev/null", "--error-logfile", "-", "--no-control-socket"]
