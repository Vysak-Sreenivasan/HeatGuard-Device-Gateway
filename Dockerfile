FROM python:3.11-slim

WORKDIR /app

# Install uv package manager
RUN pip install uv

# Copy dependency files and README needed for package build
COPY pyproject.toml uv.lock README.md ./

# Sync project dependencies
RUN uv sync --frozen --no-dev

# Copy application code and seed fleet data
COPY src ./src
COPY seed ./seed

ENV PORT=8001
EXPOSE 8001

CMD ["uv", "run", "uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8001"]
