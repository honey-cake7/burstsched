# ==================================================================
# BurstSched Makefile
# ==================================================================
#
# Build rules for:
#   1. BPF object (burstsched.bpf.o)      — requires clang + Linux headers
#   2. Skeleton header (burstsched.skel.h) — requires bpftool
#   3. User-space loader (burstsched_loader)
#   4. Mock / Python test targets that work anywhere
#
# Usage:
#   make              — build everything (Linux only)
#   make bpf          — compile BPF object only
#   make skeleton     — generate libbpf skeleton header
#   make loader       — compile user-space loader
#   make mock-test    — run predictor in --mock mode (any OS)
#   make ablation     — run the full ablation study (--mock mode)
#   make clean
# ==================================================================

# ---- Toolchain --------------------------------------------------
CLANG       ?= clang
LLC         ?= llc
BPFTOOL     ?= bpftool
CC          ?= gcc
PYTHON      ?= python3

# ---- Paths ------------------------------------------------------
INCLUDE_DIR  = include
SRC_DIR      = src
PREDICTOR_DIR= predictor
BENCH_DIR    = benchmarks
WORKLOAD_DIR = workloads

BPF_SRC      = $(SRC_DIR)/burstsched.bpf.c
BPF_OBJ      = $(SRC_DIR)/burstsched.bpf.o
SKEL_HDR     = $(SRC_DIR)/burstsched.skel.h
LOADER_SRC   = $(SRC_DIR)/burstsched_loader.c
LOADER_BIN   = burstsched_loader

# ---- Flags -------------------------------------------------------
BPF_CFLAGS   = -target bpf -D__BPF__ \
               -I$(INCLUDE_DIR) \
               -I/usr/include \
               -O2 -g \
               -Wall -Wextra

LOADER_CFLAGS = -I$(INCLUDE_DIR) -Wall -Wextra -O2 -g
LOADER_LDFLAGS = -lbpf -lelf -lz

# ---- vmlinux.h ---------------------------------------------------
# Generate vmlinux.h from the running kernel's BTF info.
# Only needed once per kernel version.
VMLINUX_H    = $(INCLUDE_DIR)/vmlinux.h

.PHONY: all bpf skeleton loader vmlinux clean mock-test ablation help

all: bpf skeleton loader

help:
	@echo "BurstSched build targets:"
	@echo "  make              — build BPF + skeleton + loader (Linux)"
	@echo "  make bpf          — compile BPF object"
	@echo "  make skeleton     — generate libbpf skeleton header"
	@echo "  make loader       — compile user-space loader"
	@echo "  make vmlinux      — generate vmlinux.h from running kernel"
	@echo "  make mock-test    — run predictor daemon in mock mode"
	@echo "  make ablation     — run full ablation study in mock mode"
	@echo "  make clean        — remove build artifacts"

# ---- vmlinux.h generation ----------------------------------------
vmlinux: $(VMLINUX_H)

$(VMLINUX_H):
	$(BPFTOOL) btf dump file /sys/kernel/btf/vmlinux format c > $@

# ---- BPF compilation ---------------------------------------------
bpf: $(BPF_OBJ)

$(BPF_OBJ): $(BPF_SRC) $(INCLUDE_DIR)/burstsched_common.h
	$(CLANG) $(BPF_CFLAGS) -c $< -o $@

# ---- Skeleton generation ------------------------------------------
skeleton: $(SKEL_HDR)

$(SKEL_HDR): $(BPF_OBJ)
	$(BPFTOOL) gen skeleton $< > $@

# ---- User-space loader --------------------------------------------
loader: $(LOADER_BIN)

$(LOADER_BIN): $(LOADER_SRC) $(SKEL_HDR) $(INCLUDE_DIR)/burstsched_common.h
	$(CC) $(LOADER_CFLAGS) -I$(SRC_DIR) $< -o $@ $(LOADER_LDFLAGS)

# ---- Mock / Python test targets -----------------------------------
mock-test:
	$(PYTHON) -m predictor.daemon --mock --duration 30

ablation:
	$(PYTHON) $(BENCH_DIR)/run_ablation.py --mock

# ---- Clean -------------------------------------------------------
clean:
	rm -f $(BPF_OBJ) $(SKEL_HDR) $(LOADER_BIN)
	rm -f $(SRC_DIR)/*.o
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
