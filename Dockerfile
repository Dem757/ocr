FROM python:3.11-slim
# Tesseract és függőségeinek telepítése
RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    libtesseract-dev \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
# Feltöltési mappa létrehozása
RUN mkdir -p uploads

CMD ["python", "app.py"]