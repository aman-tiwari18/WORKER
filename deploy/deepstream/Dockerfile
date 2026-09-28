# Plan B detection worker: DeepStream (NVDEC decode + TensorRT) + the worker code.
#
#   docker build -f deploy/deepstream/Dockerfile -t cctv-worker:pathb .
#
# DeepStream, pyds and TensorRT versions MUST match each other. Defaults are the
# DeepStream 7.1 set (Ubuntu 22.04, Python 3.10, CUDA 12.6, TensorRT 10.3); it runs
# on the H200 driver 580 (newer drivers run older CUDA images). Moving to a newer
# DeepStream = change all three ARGs together and rebuild the TensorRT engines.
#
# vLLM scene classification is NOT in this image — keep it on the existing worker
# image / GPUs (it reserves ~79 GB VRAM; see docs/SCALING_PATH_B_IMPLEMENTATION.md).

ARG DS_IMAGE=nvcr.io/nvidia/deepstream:7.1-triton-multiarch
FROM ${DS_IMAGE}

ARG DEBIAN_FRONTEND=noninteractive
ENV TZ=Asia/Kolkata
# DeepStream Python bindings — the wheel must be built for this DeepStream + Python.
ARG PYDS_WHEEL=https://github.com/NVIDIA-AI-IOT/deepstream_python_apps/releases/download/v1.2.0/pyds-1.2.0-cp310-cp310-linux_x86_64.whl
# CUDA build of PyTorch matching the image's CUDA. The rules import torch, and
# qp_box's colour check runs on the GPU when CUDA is present (CPU fallback counts
# pixels slightly differently) — keep CUDA torch so results match Path A.
ARG TORCH_INDEX=https://download.pytorch.org/whl/cu126

RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone && \
    apt-get update && apt-get install -y --no-install-recommends \
        python3-pip python3-dev python3-gi python3-gst-1.0 python-gi-dev \
        libgirepository1.0-dev libcairo2-dev \
        ffmpeg tzdata curl git mysql-client nfs-common \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir torch torchvision --index-url ${TORCH_INDEX} && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir "${PYDS_WHEEL}" onnx onnxslim "numpy>=1.24,<2"
# numpy<2 kept explicitly: onnx pulls numpy 2.x otherwise, but requirements.txt
# (OpenCV/ultralytics) and pyds are built against numpy 1.x

COPY . .

# Custom nvinfer box parsers (YOLO26 end-to-end, RF-DETR); parser unit test first.
RUN make -C scripts/detection/pathb/parsers test && \
    make -C scripts/detection/pathb/parsers

RUN python3 fetch_models.py /opt/models

# Same results/ bind-mount point and working dirs as the standard worker image.
RUN rm -rf "/app/scripts/detection/results" "/app/scripts/detection/results copy" \
 && mkdir -p /app/scripts/detection/results \
 && mkdir -p results frames models data logs \
    results/tcs_detections/crowd_detection_threshold \
    results/tcs_detections/zero_person_detection \
    results/tcs_detections/proximity_detection \
    results/tcs_detections/qp_box_missing \
    frames/multi_server

COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
ENV SCALE_PATH=B
# libproxy aborts the process on the first RTSP stream (see pathb/ds_pipeline.py)
ENV GIO_USE_PROXY_RESOLVER=dummy

# Same start-up as the standard worker image; Plan B is selected by SCALE_PATH.
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python3", "run.py"]
