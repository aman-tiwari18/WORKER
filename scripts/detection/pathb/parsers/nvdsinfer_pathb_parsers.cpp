// Custom nvinfer bounding-box parsers for Plan B.
//
//  NvDsInferParseYolo26 — YOLO26 end-to-end (NMS-free) ONNX export.
//      One output, per image [N, 6] (N = 300): x1, y1, x2, y2, score, class
//      in network-input pixels (letterboxed input; nvinfer maps back to the frame
//      when maintain-aspect-ratio=1 + symmetric-padding=1).
//
//  NvDsInferParseRFDETR — RF-DETR ONNX export.
//      "dets"   per image [Q, 4]: cx, cy, w, h normalised to 0-1 of the network input
//      "labels" per image [Q, C]: class logits (sigmoid scores)
//
// Both models are NMS-free, so the nvinfer config uses cluster-mode=4 (none).
// Boxes are returned in network-input pixels, clamped to the input size.
//
// Build: make -C pathb/parsers   (needs DeepStream headers, see Makefile)

#include <algorithm>
#include <cmath>
#include <cstring>
#include <iostream>
#include <vector>

#include "nvdsinfer_custom_impl.h"

namespace {

const NvDsInferLayerInfo* findLayer(const std::vector<NvDsInferLayerInfo>& layers, const char* name) {
    for (const auto& l : layers)
        if (l.layerName && std::strcmp(l.layerName, name) == 0) return &l;
    return nullptr;
}

float threshold(const NvDsInferParseDetectionParams& p, unsigned int cls) {
    return cls < p.perClassPreclusterThreshold.size() ? p.perClassPreclusterThreshold[cls] : 1.0f;
}

bool addBox(float x1, float y1, float x2, float y2, float score, unsigned int cls,
            const NvDsInferNetworkInfo& net, std::vector<NvDsInferObjectDetectionInfo>& out) {
    const float W = static_cast<float>(net.width), H = static_cast<float>(net.height);
    x1 = std::clamp(x1, 0.0f, W - 1.0f);
    y1 = std::clamp(y1, 0.0f, H - 1.0f);
    x2 = std::clamp(x2, 0.0f, W - 1.0f);
    y2 = std::clamp(y2, 0.0f, H - 1.0f);
    if (x2 <= x1 || y2 <= y1) return false;
    NvDsInferObjectDetectionInfo o{};
    o.classId = cls;
    o.left = x1;
    o.top = y1;
    o.width = x2 - x1;
    o.height = y2 - y1;
    o.detectionConfidence = score;
    out.push_back(o);
    return true;
}

bool isFloat(const NvDsInferLayerInfo& l, const char* who) {
    if (l.dataType != FLOAT) {
        std::cerr << who << ": output '" << (l.layerName ? l.layerName : "?")
                  << "' must be FP32 (build the engine with FP32 I/O)" << std::endl;
        return false;
    }
    return true;
}

}  // namespace

extern "C" bool NvDsInferParseYolo26(
    std::vector<NvDsInferLayerInfo> const& outputLayersInfo,
    NvDsInferNetworkInfo const& networkInfo,
    NvDsInferParseDetectionParams const& detectionParams,
    std::vector<NvDsInferObjectDetectionInfo>& objectList) {
    if (outputLayersInfo.empty()) {
        std::cerr << "NvDsInferParseYolo26: no output layers" << std::endl;
        return false;
    }
    const NvDsInferLayerInfo* layer = findLayer(outputLayersInfo, "output0");
    if (!layer) layer = &outputLayersInfo[0];
    if (!isFloat(*layer, "NvDsInferParseYolo26")) return false;

    const NvDsInferDims& d = layer->inferDims;
    if (d.numDims != 2 || d.d[1] < 6) {
        std::cerr << "NvDsInferParseYolo26: expected [N, 6] output, got ";
        for (unsigned int k = 0; k < d.numDims; ++k) std::cerr << (k ? "x" : "[") << d.d[k];
        std::cerr << "] — export YOLO26 with its end-to-end (NMS-free) head" << std::endl;
        return false;
    }
    const unsigned int n = d.d[0], stride = d.d[1];
    const float* data = static_cast<const float*>(layer->buffer);
    for (unsigned int i = 0; i < n; ++i) {
        const float* r = data + static_cast<size_t>(i) * stride;
        const float score = r[4];
        const int cls = static_cast<int>(std::lround(r[5]));
        if (cls < 0 || static_cast<unsigned int>(cls) >= detectionParams.numClassesConfigured) continue;
        if (score < threshold(detectionParams, cls)) continue;
        addBox(r[0], r[1], r[2], r[3], score, static_cast<unsigned int>(cls), networkInfo, objectList);
    }
    return true;
}

extern "C" bool NvDsInferParseRFDETR(
    std::vector<NvDsInferLayerInfo> const& outputLayersInfo,
    NvDsInferNetworkInfo const& networkInfo,
    NvDsInferParseDetectionParams const& detectionParams,
    std::vector<NvDsInferObjectDetectionInfo>& objectList) {
    const NvDsInferLayerInfo* dets = findLayer(outputLayersInfo, "dets");
    const NvDsInferLayerInfo* labels = findLayer(outputLayersInfo, "labels");
    if (!dets || !labels) {
        // Fall back to shape: the [Q, 4] tensor is the boxes, the other the logits.
        for (const auto& l : outputLayersInfo) {
            if (l.inferDims.numDims == 2 && l.inferDims.d[1] == 4) dets = &l;
            else if (l.inferDims.numDims == 2) labels = &l;
        }
    }
    if (!dets || !labels) {
        std::cerr << "NvDsInferParseRFDETR: need 'dets' [Q,4] and 'labels' [Q,C] outputs" << std::endl;
        return false;
    }
    if (!isFloat(*dets, "NvDsInferParseRFDETR") || !isFloat(*labels, "NvDsInferParseRFDETR")) return false;

    const unsigned int q = dets->inferDims.d[0];
    const unsigned int c = labels->inferDims.d[1];
    if (labels->inferDims.d[0] != q) {
        std::cerr << "NvDsInferParseRFDETR: query count mismatch" << std::endl;
        return false;
    }
    const float W = static_cast<float>(networkInfo.width), H = static_cast<float>(networkInfo.height);
    const float* b = static_cast<const float*>(dets->buffer);
    const float* lg = static_cast<const float*>(labels->buffer);
    const unsigned int nClasses = std::min(c, detectionParams.numClassesConfigured);
    for (unsigned int i = 0; i < q; ++i) {
        const float* logits = lg + static_cast<size_t>(i) * c;
        unsigned int best = 0;
        float bestLogit = logits[0];
        for (unsigned int k = 1; k < nClasses; ++k)
            if (logits[k] > bestLogit) { bestLogit = logits[k]; best = k; }
        const float score = 1.0f / (1.0f + std::exp(-bestLogit));
        if (score < threshold(detectionParams, best)) continue;
        const float* r = b + static_cast<size_t>(i) * 4;
        const float cx = r[0] * W, cy = r[1] * H, w = r[2] * W, h = r[3] * H;
        addBox(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2, score, best, networkInfo, objectList);
    }
    return true;
}

CHECK_CUSTOM_PARSE_FUNC_PROTOTYPE(NvDsInferParseYolo26);
CHECK_CUSTOM_PARSE_FUNC_PROTOTYPE(NvDsInferParseRFDETR);
