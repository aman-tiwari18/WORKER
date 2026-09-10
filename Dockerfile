# Script Server Dockerfile - Optimized for H200 GPU
FROM nvidia/cuda:12.2.2-cudnn8-runtime-ubuntu22.04

# tzdata's postinst opens an interactive debconf menu ("select the geographic
# area") unless the frontend is preset, which hangs a TTY-less CI build forever.
# Presetting the zone here also gives the image IST without relying on the
# compose bind-mounts of /etc/localtime.
ARG DEBIAN_FRONTEND=noninteractive
ENV TZ=Asia/Kolkata

# Install Python 3.10 and system dependencies
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone && \
    apt-get update && apt-get install -y \
    python3.10 \
    python3-pip \
    python3.10-dev \
    ffmpeg \
    tesseract-ocr \
    tesseract-ocr-eng \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgomp1 \
    libglib2.0-0 \
    libgl1-mesa-glx \
    mysql-client \
    tzdata \
    curl \
    git \
    nfs-common \
    && rm -rf /var/lib/apt/lists/*

# Set Python 3.10 as default
RUN update-alternatives --install /usr/bin/python python /usr/bin/python3.10 1 && \
    update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.10 1 && \
    update-alternatives --install /usr/bin/pip pip /usr/bin/pip3 1

# Set working directory
WORKDIR /app

# Copy requirements file
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Pin vLLM to the version proven stable on the big 224-core hosts (w5/6).
# requirements.txt intentionally does NOT pin vllm; install it here via uv so a
# newer (broken) release can't sneak in. openai + huggingface_hub are needed by
# the vLLM OpenAI-compatible server + weight download.
RUN pip install --no-cache-dir uv && \
    uv pip install --system "vllm==0.20.2" openai huggingface_hub

# Copy application code
COPY . .

# Apply the vLLM stability patch (deep_gemm / flashinfer guard). WITHOUT this,
# vLLM crashes on the 224-core / runtime-image hosts (no nvcc). Makes the SAME
# image usable on worker-0..3 AND the big vLLM hosts w5/6.
RUN python3 patch_vllm.py && chmod +x /app/start_vllm.sh

# The detection results/ dir is a BIND-MOUNT POINT for frames. Prod had it as a
# symlink to the NFS mount, which makes `docker run -v ...` fail ("not a directory").
# Force a real directory here so the same image mounts cleanly on every host.
RUN rm -rf "/app/scripts/detection/results" "/app/scripts/detection/results copy" \
 && mkdir -p /app/scripts/detection/results


# Create necessary directories
RUN mkdir -p results frames models data logs \
    results/tcs_detections/crowd_detection_threshold \
    results/tcs_detections/zero_person_detection \
    results/tcs_detections/proximity_detection \
    results/tcs_detections/qp_box_missing \
    frames/multi_server

# Copy and set permissions for entrypoint script
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Set Python path
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# Use entrypoint script
ENTRYPOINT ["docker-entrypoint.sh"]

# Headless agent by default (WORKER_MODE=agent); no HTTP API / socket. Same image
# on every host — per-host differences are env only.
CMD ["python", "run.py"]
