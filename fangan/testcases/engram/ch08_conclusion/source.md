<!-- source.md = VIEWER-ONLY verbatim slice of engram_paper_mineru.md, original lines 341-349.
     Authoritative gold coordinates live in gold.yaml under each atom's source_span (file=engram_paper_mineru.md).
     viewer_span here is optional/debug and may drift if this file is reformatted. -->
# 8. Conclusion

In this work, we introduce conditional memory as a complementary sparsity axis to the prevailing conditional computation paradigm (MoE), aiming to resolve the inefficiency of simulating knowledge retrieval through dynamic computation. We instantiate this concept via Engram, a module that modernizes classic N-gram embeddings to enable scalable, constant-time $O(1)$ lookups for static patterns

By formulating the Sparsity Allocation problem, we uncover a U-shaped scaling law, demonstrating that a hybrid allocation of sparse capacity between MoE experts and Engram memory strictly outperforms pure MoE baselines. Guided by this law, we scale Engram to 27B parameters, achieving superior performance across diverse domains. Notably, while the memory module intuitively aids knowledge retrieval, we observe even larger gains in general reasoning, code, and mathematics.

Our mechanistic analysis reveals that Engram effectively “deepen” the network by relieving early layers from static reconstruction tasks, thereby freeing up attention capacity to focus

on global context and complex reasoning. This architectural shift translates into substantial improvements in long-context capabilities, as evidenced by performance gains in LongPPL and RULER. Finally, Engram advocates for infrastructure-aware efficiency as a first-class design principle. Its deterministic addressing allows for the decoupling of storage and compute, enabling the offloading of massive parameter tables to host memory with negligible inference overhead. We envision conditional memory functions as an indispensable modeling primitive for next-generation sparse models.
