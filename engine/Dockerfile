FROM python:3.12-slim

# tesseract is the OCR binary pytesseract shells out to. Without it, scanned
# filings (newspaper result cuttings, signed board-meeting outcomes) silently
# yield no text and are skipped — see pdf_text.py.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        libtesseract-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY engine/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY engine/ /app/

ENV PYTHONUNBUFFERED=1 \
    SCRAPER_BASE_PATH=/app \
    PDF_STORAGE_PATH=/app/storage/pdf

# The image ships both processes; docker-compose picks one per service.
#   agent : python agent.py
#   api   : uvicorn api:app --host 0.0.0.0 --port 8000
CMD ["python", "agent.py"]
