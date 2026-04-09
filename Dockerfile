FROM python:3.11-slim

WORKDIR /app

# Install Node.js for Convex CLI
RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Node dependencies (for Convex)
COPY package.json .
RUN npm install --omit=dev

# Copy application code
COPY . .

EXPOSE 8080

CMD ["python", "-m", "src.main"]
