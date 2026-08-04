# Single image, used for the "bff", "worker-workflow", and
# "worker-activity" services in docker-compose.yml -- they run the same
# installed package, just different entrypoints (see docker-compose.yml's
# `command:`/`environment:` per service).

FROM python:3.12-slim

WORKDIR /app

# Copy only packaging metadata first so dependency installs are cached
# independently of application code changes.
COPY pyproject.toml .
COPY review_approval/ ./review_approval/

RUN pip install --no-cache-dir .

# No CMD here on purpose -- docker-compose.yml sets the command per service.
