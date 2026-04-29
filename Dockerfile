FROM python:3.12-slim AS builder

# Set working directory
WORKDIR /app

COPY . .

# Install dependencies to a specific folder
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim

# Install packages
RUN apt-get update && apt-get install -y sshpass && rm -rf /var/lib/apt/lists/*

# Set environment variables
ENV APP_DIR=/opt/filament-management \
    PYTHONPATH=/opt/filament-management \
    UI_PORT=8005

WORKDIR $APP_DIR

# Copy Python libs and app code
COPY --from=builder /install /usr/local
COPY --from=builder /app $APP_DIR

# Setup Entrypoint
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Expose the UI port
EXPOSE $UI_PORT

ENTRYPOINT ["/entrypoint.sh"]

# Start the application
CMD uvicorn main:app --host 0.0.0.0 --port $UI_PORT

