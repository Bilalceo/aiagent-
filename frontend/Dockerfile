FROM node:20-alpine

WORKDIR /app

# NEXT_PUBLIC_* must be present at build time (Next.js inlines them into the
# client bundle). The browser calls the backend on the host-published port.
ARG NEXT_PUBLIC_API_BASE=http://localhost:8000/api/v1
ENV NEXT_PUBLIC_API_BASE=$NEXT_PUBLIC_API_BASE

# Build context is the repo ROOT (Railway monorepo via RAILWAY_DOCKERFILE_PATH),
# so copy from the frontend/ subdir. node_modules/.next are gitignored => not sent.
COPY frontend/package.json ./
RUN npm install
COPY frontend/ ./
RUN npm run build

EXPOSE 3000
# package.json "start" binds Railway's $PORT (falls back to 3000).
CMD ["npm", "run", "start"]
