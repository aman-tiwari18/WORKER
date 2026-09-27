# Builds libnvdsinfer_pathb.so for nvinfer (custom-lib-path in configs/).
# Run inside the DeepStream container:  make -C scripts/detection/pathb/parsers
# `make test` compiles against a stub header and checks the parser maths (no DeepStream needed).

DS_ROOT   ?= /opt/nvidia/deepstream/deepstream
CUDA_HOME ?= /usr/local/cuda
CXX       ?= g++
CXXFLAGS  := -O2 -std=c++17 -Wall -Wextra -fPIC

TARGET := libnvdsinfer_pathb.so

all: $(TARGET)

$(TARGET): nvdsinfer_pathb_parsers.cpp
	$(CXX) $(CXXFLAGS) -shared -I$(DS_ROOT)/sources/includes -I$(CUDA_HOME)/include $< -o $@

test: nvdsinfer_pathb_parsers.cpp test/test_parsers.cpp test/stub/nvdsinfer_custom_impl.h
	$(CXX) $(CXXFLAGS) -Itest/stub nvdsinfer_pathb_parsers.cpp test/test_parsers.cpp -o test/test_parsers
	./test/test_parsers

clean:
	rm -f $(TARGET) test/test_parsers

.PHONY: all test clean
