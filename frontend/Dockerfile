# syntax=docker/dockerfile:1
FROM node:20-bookworm-slim AS build

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY package.json package-lock.json ./
RUN npm ci

COPY . .

ARG VITE_API_BASE_URL=/legal-ocr/backend
ARG VITE_BASE_PATH=/legal-ocr/
ARG VITE_AI_CITATIONS=false

ENV VITE_API_BASE_URL=${VITE_API_BASE_URL} \
    VITE_BASE_PATH=${VITE_BASE_PATH} \
    VITE_AI_CITATIONS=${VITE_AI_CITATIONS}

RUN npm run build

FROM nginx:1.27-alpine

RUN rm -f /etc/nginx/conf.d/default.conf

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/build /usr/share/nginx/html

RUN sed -i 's/\r$//' /etc/nginx/conf.d/default.conf \
    && chmod 644 /etc/nginx/conf.d/default.conf \
    && chmod -R 755 /usr/share/nginx/html \
    && find /usr/share/nginx/html -type f -exec chmod 644 {} \;

EXPOSE 3061

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1:3061/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
