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

# Render exposes PORT env var, fallback to 5050 for local dev
CMD ["sh", "-c", "python app.py"]
