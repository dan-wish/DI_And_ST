FROM python:3.11-slim

# Twelve-Factor & ISO/IEC 25010: Unbuffered stdout logging & clean container runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install isolated dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY app.py .

# Self-contained port binding
EXPOSE 5000

CMD ["python", "-u", "app.py"]
