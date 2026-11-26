FROM node:20.18.0-bookworm-slim AS dashboard
WORKDIR /ui
COPY app/dashboard/ ./
RUN npm ci --no-audit --no-fund && \
    VITE_BASE_API=/api/ npm run build -- --outDir /dashboard --assetsDir statics && \
    cp /dashboard/index.html /dashboard/404.html

FROM gozargah/marzban@sha256:8e422c21997e5d2e3fa231eeff73c0a19193c20fc02fa4958e9368abb9623b8d
WORKDIR /code
COPY app/ /code/app/
COPY xray_config.json /code/xray_config.json
RUN rm -rf /code/app/dashboard/build
COPY --from=dashboard /dashboard /code/app/dashboard/build
COPY --chmod=0755 deploy/operations/xray_compat.py /usr/local/bin/xray-panel
LABEL org.opencontainers.image.source="https://github.com/Taozi-succes/marzban-panel"
