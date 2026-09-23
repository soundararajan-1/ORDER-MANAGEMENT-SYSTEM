FROM python:3.11-slim

WORKDIR /app

# Copy requirements and install
COPY OrderFlow-AI/backend/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY OrderFlow-AI /app/OrderFlow-AI

ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn OrderFlow-AI.backend.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
