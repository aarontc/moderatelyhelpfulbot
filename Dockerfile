# User and group ID specification
ARG PGID=1000
ARG PUID=1000


FROM python:3.11-alpine
ARG PGID
ARG PUID
ENV PATH="${PATH}:/app/.local/bin"

RUN \
	addgroup -g "${PGID}" app && \
	adduser -h /app -D -G app -u "${PUID}" -s /bin/sh app && \
	apk add \
		--no-cache \
		gcc \
		libffi-dev \
		libpq \
		libpq-dev \
		musl-dev \
		postgresql-dev

USER app:app
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./

CMD ["python", "main.py"]
