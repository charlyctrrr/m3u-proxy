FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

COPY app.py .

ENV PORT=8000
EXPOSE 8000
CMD ["gunicorn", "-w", "1", "-k", "gthread", "-t", "0", "-b", "0.0.0.0:8000", "app:app"]
