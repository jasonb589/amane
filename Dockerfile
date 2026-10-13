# syntax=docker/dockerfile:1
# Amane 元数据管理服务

# --- 前端构建阶段 ---
FROM node:26-slim AS web-builder
WORKDIR /app/web
RUN npm install -g pnpm@11
# pnpm-workspace.yaml 含 allowBuilds; 必须在 install 前拷入, 否则 ERR_PNPM_IGNORED_BUILDS
COPY web/package.json web/pnpm-lock.yaml web/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY web/ .
RUN pnpm build

# --- Python 依赖阶段 ---
FROM python:3.14-slim AS base
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv sync --no-dev --frozen --no-editable

# 海报人脸识别 (可选): opencv-headless 有 cp37-abi3 预编译 wheel, Python 3.14 / amd64 / arm64 都无需编译.
# 必须装在 uv sync 之后 (venv 已就绪); 它不进入 lock, 只是镜像内的可选依赖.
RUN uv pip install --python /app/.venv/bin/python "opencv-python-headless>=5.0" \
    && /app/.venv/bin/python -c "import cv2; print('opencv ok:', cv2.__version__)"
# 轻量人脸模型 (YuNet): 下载失败就退回 OpenCV 自带的 Haar 级联, 不影响构建.
RUN mkdir -p /app/models && /app/.venv/bin/python -c "import urllib.request; urllib.request.urlretrieve('https://raw.githubusercontent.com/opencv/opencv_zoo/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx', '/app/models/face_detection_yunet.onnx')" || echo "face model download skipped (will fall back to Haar cascade)"

# --- 最终镜像 ---
FROM base
# libgomp1: 超分二进制的运行时依赖, Real-ESRGAN 与 waifu2x 都动态链接 libgomp.
# postgresql-client: r18.dev dump 导入走 psql -f 子进程 (见 docs/dev/crawlers.md). 不配 r18 时无害.
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 postgresql-client \
    && rm -rf /var/lib/apt/lists/*
COPY alembic.ini ./
COPY --from=web-builder /app/web/dist ./web/dist

EXPOSE 8000
VOLUME ["/data", "/media"]

ENV AMANE_DATA_DIR=/data
ENV AMANE_LOG_DIR=/data/logs
ENV PATH="/app/.venv/bin:$PATH"
ENV UV_NO_SYNC=1

# 版本号来自源码里的 amane/_custom_version.py (发版时跟代码一起提交, 不依赖构建参数/缓存);
# 想临时覆盖面板显示时, 在 compose 里给 AMANE_VERSION 赋值即可.
CMD ["python", "-m", "amane.server"]
