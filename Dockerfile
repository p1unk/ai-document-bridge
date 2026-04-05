FROM python:3.11-slim

WORKDIR /app

# Install dependencies
RUN pip install --no-cache-dir fastapi uvicorn httpx ollama groq python-multipart itsdangerous jinja2

# Copy code and templates
COPY main.py .
COPY templates/ ./templates/

# Create data directory for persistence
RUN mkdir -p /app/data

EXPOSE 8000

CMD ["uvicorn", "main.py", "--host", "0.0.0.0", "--port", "8000"]

# Checks every 30s if the login page is reachable
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:8000/login || exit 1