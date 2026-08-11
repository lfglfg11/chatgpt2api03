ARG TARGETARCH

FROM node:22-alpine AS web-build

WORKDIR /app/web-vue

COPY web-vue/package.json web-vue/package-lock.json ./
RUN npm ci

COPY VERSION /app/VERSION
COPY CHANGELOG.md /app/CHANGELOG.md
COPY web-vue ./
RUN npm run build


FROM node:22-bookworm-slim AS image-upscale-build

WORKDIR /app/scripts/image_upscale

COPY scripts/image_upscale/package.json scripts/image_upscale/package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force


FROM python:3.13-slim AS app

ARG TARGETARCH

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    TZ=Asia/Shanghai \
    CHATGPT2API_THREAD_TOKENS=80

WORKDIR /app

# 安装系统依赖
# - git: Git 存储后端需要
# - libpq-dev: PostgreSQL 客户端库
# - gcc: 编译 psycopg2-binary 需要
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    libpq-dev \
    gcc \
    openssl \
    tzdata \
    xvfb \
    xauth \
    libgtk-3-0 \
    libdbus-1-3 \
    libx11-xcb1 \
    libxcb-shm0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libxkbcommon0 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libatspi2.0-0 \
    libnss3 \
    libnspr4 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
# GPTMail2 only starts Camoufox for a short browser-verification refresh.  Keep
# the browser binary in the image so runtime refreshes do not download assets.
RUN uv run --no-sync python -m camoufox fetch

COPY main.py ./
COPY config.example.yaml ./
COPY VERSION ./
COPY api ./api
COPY services ./services
COPY utils ./utils
COPY scripts ./scripts
COPY --from=image-upscale-build /usr/local/bin/node /usr/local/bin/node
COPY --from=image-upscale-build /app/scripts/image_upscale/node_modules ./scripts/image_upscale/node_modules
COPY --from=web-build /app/web-vue/dist ./web_dist

EXPOSE 80

CMD ["uv", "run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "80", "--access-log"]
