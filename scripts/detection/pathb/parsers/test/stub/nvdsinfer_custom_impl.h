// Minimal stand-in for DeepStream's nvdsinfer_custom_impl.h — only the types the
// Plan B parsers use, with DeepStream's field names — so the parser maths can be
// compiled and tested without the SDK. The real header is used for the .so build.
#pragma once
#include <vector>

#define NVDSINFER_MAX_DIMS 8

typedef enum { FLOAT = 0, HALF = 1, INT8 = 2, INT32 = 3 } NvDsInferDataType;

typedef struct {
    unsigned int numDims;
    unsigned int d[NVDSINFER_MAX_DIMS];
    unsigned int numElements;
} NvDsInferDims;

typedef struct {
    NvDsInferDataType dataType;
    NvDsInferDims inferDims;
    int bindingIndex;
    const char* layerName;
    void* buffer;
    int isInput;
} NvDsInferLayerInfo;

typedef struct {
    unsigned int width;
    unsigned int height;
    unsigned int channels;
} NvDsInferNetworkInfo;

typedef struct {
    unsigned int numClassesConfigured;
    std::vector<float> perClassPreclusterThreshold;
    std::vector<float> perClassPostclusterThreshold;
} NvDsInferParseDetectionParams;

typedef struct {
    unsigned int classId;
    float left;
    float top;
    float width;
    float height;
    float detectionConfidence;
} NvDsInferObjectDetectionInfo;

typedef bool (*NvDsInferParseCustomFunc)(std::vector<NvDsInferLayerInfo> const&,
                                         NvDsInferNetworkInfo const&,
                                         NvDsInferParseDetectionParams const&,
                                         std::vector<NvDsInferObjectDetectionInfo>&);

#define CHECK_CUSTOM_PARSE_FUNC_PROTOTYPE(customParseFunc)                         \
    static void checkFunc_##customParseFunc(NvDsInferParseCustomFunc = customParseFunc) {} \
    static void (*checkFuncPtr_##customParseFunc)(NvDsInferParseCustomFunc) __attribute__((unused)) = \
        checkFunc_##customParseFunc;
