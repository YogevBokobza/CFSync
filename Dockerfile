FROM python:3.12-slim AS builder

# Set working directory
WORKDIR /app

# Install git only to clone the repo
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

COPY . .

# Install dependencies to a specific folder
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim

# Set environment variables
ENV APP_DIR=/opt/filament-management \
    PYTHONPATH=/opt/filament-management \
    UI_PORT=8005

WORKDIR $APP_DIR

# Copy ONLY the installed libraries from the builder stage
COPY --from=builder /install /usr/local

# Copy ONLY the application code from the builder stage
COPY --from=builder /app $APP_DIR

# Expose the UI port
EXPOSE 8005

# Start the application
CMD printf '{\n  "printer_url": "%s",\n  "filament_diameter_mm": %s,\n  "spoolman_url": "%s",\n  "spoolman_mode": "%s"\n}\n' \
    "$PRINTER_URL" "$FILAMENT_DIAMETER" "$SPOOLMAN_URL" "$SPOOLMAN_MODE" > $APP_DIR/data/config.json && \
    uvicorn main:app --host 0.0.0.0 --port 8005
