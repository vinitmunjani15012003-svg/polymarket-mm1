FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Create required directories
RUN mkdir -p data logs config

# Set python path
ENV PYTHONPATH=/app

# Default command runs dry-run if no args provided
CMD ["python", "-m", "src.main", "--mode", "dry-run", "--headless"]
