FROM python:3.11-slim

WORKDIR /app

# Install image processing libraries needed by Pillow + barcode generation
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libjpeg-dev \
    libpng-dev \
    zlib1g-dev \
    libffi-dev \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create necessary directories
RUN mkdir -p static/barcodes exports instance

ENV FLASK_APP=app.py
ENV FLASK_ENV=production
ENV PORT=10000

# Initialize DB then start gunicorn on Render's expected port
CMD ["sh", "-c", "python -c 'from app import init_db; init_db()' && gunicorn --bind 0.0.0.0:${PORT} --workers 2 --timeout 120 app:app"]
