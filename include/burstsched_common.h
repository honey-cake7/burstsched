/* SPDX-License-Identifier: GPL-2.0 */
/*
 * burstsched_common.h — Shared interface between kernel scheduler and
 *                        user-space predictor daemon.
 *
 * This header is the SINGLE meeting point between Track 1 (kernel/BPF)
 * and Track 2 (Python predictor).  Both sides include it so that struct
 * layouts and flag values are always in sync.
 *
 * Build note: when compiling for BPF, define __BPF__ so that libbpf
 * helpers are available.  For the user-space loader, leave it undefined
 * and use standard Linux headers.
 */

#ifndef BURSTSCHED_COMMON_H
#define BURSTSCHED_COMMON_H

/* ------------------------------------------------------------------ */
/*  Standard types                                                     */
/* ------------------------------------------------------------------ */

#ifdef __BPF__
#include "vmlinux.h"           /* kernel types for BPF programs         */
#else
#include <stdint.h>
typedef uint64_t __u64;
typedef uint32_t __u32;
typedef int32_t  __s32;
typedef uint16_t __u16;
typedef uint8_t  __u8;
#endif

/* ------------------------------------------------------------------ */
/*  Ablation feature flags — every row in the proposal's table         */
/*                                                                     */
/*  Flags are powers of two so they can be OR'd together.  The loader  */
/*  CLI sets these in a BPF global variable (or map entry) and the     */
/*  scheduler checks them at runtime.                                  */
/* ------------------------------------------------------------------ */

#define FEAT_REACTIVE_MLFQ     0x00   /* baseline — always on            */
#define FEAT_EWMA              0x01   /* + EWMA predictor                */
#define FEAT_LEARNED           0x02   /* + learned (sklearn) predictor   */
#define FEAT_CONFIDENCE_GATE   0x04   /* + confidence-gated placement    */
#define FEAT_PREDICTED_SLICES  0x08   /* + predicted time slices         */
#define FEAT_WAKEUP_PREEMPT    0x10   /* + wakeup preemption (optional)  */
#define FEAT_PER_CPU           0x20   /* + per-CPU queues (stretch)      */

/* ------------------------------------------------------------------ */
/*  MLFQ constants                                                     */
/* ------------------------------------------------------------------ */

#define BURSTSCHED_NR_QUEUES        4    /* number of priority queues      */
#define BURSTSCHED_MAX_TASKS     4096    /* max tracked tasks              */

/* Default time-slice values in nanoseconds */
#define SLICE_SHORT_NS       (1 * 1000 * 1000)    /* 1 ms  — high prio   */
#define SLICE_MEDIUM_NS      (4 * 1000 * 1000)    /* 4 ms                */
#define SLICE_LONG_NS       (16 * 1000 * 1000)    /* 16 ms — low prio    */
#define SLICE_MAX_NS        (32 * 1000 * 1000)    /* 32 ms — bottom      */

/* Queue-level thresholds (predicted burst in ns) */
#define BURST_THRESH_Q0      (1 * 1000 * 1000)    /* < 1 ms  → Q0        */
#define BURST_THRESH_Q1      (4 * 1000 * 1000)    /* < 4 ms  → Q1        */
#define BURST_THRESH_Q2     (16 * 1000 * 1000)    /* < 16 ms → Q2        */
                                                    /* else     → Q3        */

/* Aging: promote a task after this many ns of accumulated wait */
#define AGING_THRESHOLD_NS  (100 * 1000 * 1000)   /* 100 ms              */

/* ------------------------------------------------------------------ */
/*  Burst event — emitted on the ringbuf every time a task stops       */
/*  running (blocks or is preempted).  The predictor daemon reads      */
/*  these to build history and make predictions.                       */
/* ------------------------------------------------------------------ */

struct burst_event {
    __u32 pid;              /* task PID                                    */
    __u32 tgid;             /* thread-group ID                             */
    __u64 burst_ns;         /* how long the task ran before blocking        */
    __u64 sleep_ns;         /* how long the task slept before this burst    */
    __u64 timestamp_ns;     /* wall-clock time of this event               */
    __u8  preempted;        /* 1 if preempted, 0 if voluntarily blocked    */
    __u8  _pad[7];          /* alignment padding                           */
};

/* ------------------------------------------------------------------ */
/*  Prediction entry — written by the predictor daemon into a BPF map  */
/*  keyed by PID.  The scheduler reads this on every dispatch/enqueue  */
/*  decision.                                                          */
/* ------------------------------------------------------------------ */

struct prediction_entry {
    __u64 predicted_burst_ns;   /* predicted next burst length (ns)        */
    __u64 confidence;           /* 0–1000 (fixed-point, 1000 = perfect)    */
    __u32 queue_hint;           /* suggested queue level (0 = highest)     */
    __u32 slice_hint_ns;        /* suggested time slice (ns), 0 = default  */
    __u8  predictor_type;       /* 0 = EWMA, 1 = learned                  */
    __u8  _pad[7];              /* alignment padding                       */
};

/* ------------------------------------------------------------------ */
/*  Per-task state tracked inside the BPF scheduler                    */
/* ------------------------------------------------------------------ */

struct task_burst_state {
    __u32 pid;
    __u32 current_queue;        /* queue the task is currently in          */
    __u64 last_run_start_ns;    /* timestamp when the task last started    */
    __u64 last_sleep_start_ns;  /* timestamp when the task last blocked    */
    __u64 total_wait_ns;        /* accumulated wait time for aging         */
    __u64 cumulative_runtime_ns;
    __u32 burst_count;          /* number of bursts observed               */
    __u32 _pad;
};

/* ------------------------------------------------------------------ */
/*  Map names (string constants for libbpf skeleton lookups)           */
/* ------------------------------------------------------------------ */

#define MAP_NAME_BURST_EVENTS   "burst_events"      /* ringbuf           */
#define MAP_NAME_PREDICTIONS    "predictions"        /* hash: pid → pred  */
#define MAP_NAME_TASK_STATE     "task_state"          /* hash: pid → state */
#define MAP_NAME_FEATURES       "features"            /* array[1]: flags   */

#endif /* BURSTSCHED_COMMON_H */
