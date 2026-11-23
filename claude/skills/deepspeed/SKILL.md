---
name: deepspeed
description: Expert guidance for distributed training with DeepSpeed - ZeRO optimization stages, pipeline parallelism, FP16/BF16/FP8, 1-bit Adam, sparse attention
version: 1.0.0
author: Orchestra Research
license: MIT
tags: [DeepSpeed, Distributed Training, ZeRO, Pipeline Parallelism, Mixed Precision, Optimization, Microsoft, Large-Scale Training, FP16, FP8]
dependencies: [deepspeed, torch, transformers, accelerate]
---

# DeepSpeed Skill

Expert guidance for distributed training with DeepSpeed (ZeRO-1/2/3, ZeRO-Offload/Infinity, Pipeline/3D Parallelism, FP16/BF16/FP8, MoE, DeepNVMe, and 1-bit Adam).

## When to Use This Skill
- Configuring ZeRO stages (`zero_optimization`), activation checkpointing, or ZeRO-Offload / NVMe offloading
- Scaling PyTorch/Megatron training across multi-GPU / multi-node clusters
- Tuning LRRT, 1Cycle, MoE hyperparameters, or diagnosing DeepSpeed OOM / communication bottlenecks

## Reference Files (Read On Demand)
All full tutorials and extracted pattern guides remain intact under `references/`:
- **`references/full-guide.md`** — Complete pattern reference (DeepNVMe, NLG+MoE, PR-MoE/MoS, Learning Rate Range Test, Feature Overview, ZeRO & Parallelism examples)
- **`references/tutorials.md`** — Full DeepSpeed tutorials (getting started, ZeRO, Megatron, pipeline, MoE, data efficiency)
- **`references/other.md`** — Detailed feature overview & configuration reference
- **`references/08.md` / `09.md` / `2020.md` / `2023.md` / `mii.md` / `assets.md`** — Release notes, DeepSpeed-MII serving, and assets
