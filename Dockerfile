FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy package files
COPY . /app

# Install package dependencies
RUN pip install --no-cache-dir .

EXPOSE 8080

# Command to run on container start
CMD ["plexamp-avr"]