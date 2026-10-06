FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy package files
COPY . /app

# Install package dependencies
RUN pip install --no-cache-dir .

EXPOSE 8080

# Config store for settings made in the web UI (webhooks); mount a volume here
VOLUME ["/data"]

# Command to run on container start
CMD ["plexamp-avr"]