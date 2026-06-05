FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1
ENV GROCERY_CHROME_PATH=/usr/bin/chromium

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      ca-certificates \
      chromium \
      curl \
      fonts-liberation \
      fonts-noto-color-emoji \
      nodejs \
      npm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --omit=dev

COPY . .
RUN mkdir -p /app/data

EXPOSE 8877

CMD ["python", "-u", "grocery_cockpit.py", "serve", "--host", "0.0.0.0"]
