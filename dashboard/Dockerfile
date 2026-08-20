# ---- build ----------------------------------------------------------------
FROM node:22-alpine AS build

WORKDIR /app

# Copy manifests first so `npm ci` is cached until dependencies actually change.
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci

COPY dashboard/ ./

# Empty on purpose: nginx proxies /api on the same origin (see nginx.conf), so
# the bundle makes same-origin requests and needs no absolute API URL baked in.
ENV VITE_API_BASE=""
RUN npm run build

# ---- serve ----------------------------------------------------------------
# Static files only - no node runtime in the final image, so this container
# idles at essentially zero CPU.
FROM nginx:1.27-alpine

COPY --from=build /app/dist /usr/share/nginx/html
COPY dashboard/nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -qO- http://127.0.0.1/healthz || exit 1
