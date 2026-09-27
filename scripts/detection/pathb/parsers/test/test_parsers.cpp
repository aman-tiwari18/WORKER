// Checks the Plan B parsers on hand-made tensors. Build + run: make -C .. test
#include <cmath>
#include <cstdio>
#include <vector>

#include "nvdsinfer_custom_impl.h"

extern "C" bool NvDsInferParseYolo26(std::vector<NvDsInferLayerInfo> const&, NvDsInferNetworkInfo const&,
                                     NvDsInferParseDetectionParams const&, std::vector<NvDsInferObjectDetectionInfo>&);
extern "C" bool NvDsInferParseRFDETR(std::vector<NvDsInferLayerInfo> const&, NvDsInferNetworkInfo const&,
                                     NvDsInferParseDetectionParams const&, std::vector<NvDsInferObjectDetectionInfo>&);

static int failures = 0;
#define CHECK(cond, msg) do { if (!(cond)) { std::printf("FAIL: %s\n", msg); ++failures; } } while (0)
static bool near(float a, float b) { return std::fabs(a - b) < 1e-3f; }

static NvDsInferLayerInfo layer(const char* name, float* buf, unsigned d0, unsigned d1) {
    NvDsInferLayerInfo l{};
    l.dataType = FLOAT;
    l.inferDims.numDims = 2;
    l.inferDims.d[0] = d0;
    l.inferDims.d[1] = d1;
    l.inferDims.numElements = d0 * d1;
    l.layerName = name;
    l.buffer = buf;
    return l;
}

static void testYolo26() {
    // 4 candidate rows of [x1, y1, x2, y2, score, cls]
    float out[4 * 6] = {
        10, 20, 110, 220, 0.90f, 0,     // person, kept
        50, 50, 90, 60, 0.40f, 0,       // below 0.45 -> dropped
        600, 600, 700, 700, 0.95f, 67,  // phone, clamped to 639
        5, 5, 5, 40, 0.99f, 56,         // zero width -> dropped
    };
    std::vector<NvDsInferLayerInfo> layers{layer("output0", out, 4, 6)};
    NvDsInferNetworkInfo net{640, 640, 3};
    NvDsInferParseDetectionParams p{};
    p.numClassesConfigured = 80;
    p.perClassPreclusterThreshold.assign(80, 0.45f);
    std::vector<NvDsInferObjectDetectionInfo> objs;
    CHECK(NvDsInferParseYolo26(layers, net, p, objs), "yolo26 returns true");
    CHECK(objs.size() == 2, "yolo26 keeps 2 boxes");
    if (objs.size() == 2) {
        CHECK(objs[0].classId == 0 && near(objs[0].left, 10) && near(objs[0].top, 20) &&
              near(objs[0].width, 100) && near(objs[0].height, 200) && near(objs[0].detectionConfidence, 0.9f),
              "yolo26 person box");
        CHECK(objs[1].classId == 67 && near(objs[1].left, 600) && near(objs[1].width, 39), "yolo26 clamp");
    }
    // wrong shape must fail loudly, not return garbage
    float raw[8400 * 4];
    std::vector<NvDsInferLayerInfo> bad{layer("output0", raw, 8400, 4)};
    objs.clear();
    CHECK(!NvDsInferParseYolo26(bad, net, p, objs), "yolo26 rejects non-end2end output");
}

static void testRFDETR() {
    // 3 queries, 3 classes. Boxes cx, cy, w, h in 0-1 of a 560x560 input.
    float dets[3 * 4] = {
        0.5f, 0.5f, 0.2f, 0.4f,   // q0
        0.1f, 0.1f, 0.1f, 0.1f,   // q1
        0.9f, 0.9f, 0.4f, 0.4f,   // q2 (goes past the edge -> clamped)
    };
    float logits[3 * 3] = {
        -4.0f, 3.0f, -1.0f,       // q0 -> class 1, sigmoid(3)=0.9526
        -5.0f, -5.0f, -2.0f,      // q1 -> class 2, sigmoid(-2)=0.119 -> dropped
        2.0f, -1.0f, -3.0f,       // q2 -> class 0, sigmoid(2)=0.8808
    };
    std::vector<NvDsInferLayerInfo> layers{layer("dets", dets, 3, 4), layer("labels", logits, 3, 3)};
    NvDsInferNetworkInfo net{560, 560, 3};
    NvDsInferParseDetectionParams p{};
    p.numClassesConfigured = 3;
    p.perClassPreclusterThreshold.assign(3, 0.5f);
    std::vector<NvDsInferObjectDetectionInfo> objs;
    CHECK(NvDsInferParseRFDETR(layers, net, p, objs), "rfdetr returns true");
    CHECK(objs.size() == 2, "rfdetr keeps 2 boxes");
    if (objs.size() == 2) {
        CHECK(objs[0].classId == 1 && near(objs[0].left, 224) && near(objs[0].top, 168) &&
              near(objs[0].width, 112) && near(objs[0].height, 224) &&
              near(objs[0].detectionConfidence, 0.95257f), "rfdetr q0 box");
        CHECK(objs[1].classId == 0 && near(objs[1].left, 392) && near(objs[1].left + objs[1].width, 559),
              "rfdetr q2 clamped");
    }
    // names missing: falls back to shapes
    std::vector<NvDsInferLayerInfo> anon{layer("x", dets, 3, 4), layer("y", logits, 3, 3)};
    objs.clear();
    CHECK(NvDsInferParseRFDETR(anon, net, p, objs) && objs.size() == 2, "rfdetr shape fallback");
}

int main() {
    testYolo26();
    testRFDETR();
    std::printf(failures ? "%d parser check(s) FAILED\n" : "all parser checks passed\n", failures);
    return failures ? 1 : 0;
}
