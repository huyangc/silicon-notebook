<!-- source.md = VIEWER-ONLY verbatim slice of engram_paper_mineru.md, original lines 31-109.
     Authoritative gold coordinates live in gold.yaml under each atom's source_span (file=engram_paper_mineru.md).
     viewer_span here is optional/debug and may drift if this file is reformatted. -->
# 2. Architecture

# 2.1. Overview

As shown in Figure 1, Engram is a conditional memory module designed to augment the Transformer backbone by structurally separating static pattern storage from dynamic computation. Formally, given an input sequence $X = (x_{1}, \ldots, x_{T})$ and hidden states $\mathbf{H}^{(\ell)} \in \mathbb{R}^{T \times d}$ at layer $\ell$ , the module processes each position t in two functional phases: retrieval and fusion. First, as detailed in Section 2.2, we extract and compress suffix N-grams to deterministically retrieve static embedding vectors via hashing. Subsequently, in Section 2.3, these retrieved embeddings are dynamically modulated by the current hidden state and refined via a lightweight convolution. Finally, we discuss the integration with multi-branch architectures in Section 2.4 and the system-level design in Section 2.5.

# 2.2. Sparse Retrieval via Hashed $N$ -grams

The first phase maps local contexts to static memory entries, involving tokenizer compression and retrieving embeddings via deterministic hashing.

Tokenizer Compression While $N$ -gram models typically operate directly on tokenizer outputs, standard subword tokenizers prioritize lossless reconstruction, often assigning disjoint IDs to semantically equivalent terms (e.g., Apple vs. $\sqcup$ apple) (Kudo and Richardson, 2018; Li et al., 2023b). To maximize semantic density, we implement a vocabulary projection layer. Specifically, we pre-compute a surjective function $\mathcal{P}: V \to V'$ that collapses raw token IDs into canonical

identifiers based on normalized textual equivalence (using NFKC (Whistler, 2025), lowercasing, etc.). In practice, this process achieves a 23% reduction in the effective vocabulary size for a 128k tokenizer (see Appendix C). Formally, for a token at position t, we map its raw ID $x_{t}$ to a canonical ID $x_{t}^{\prime} = \mathcal{P}(x_{t})$ to form the suffix N-gram $g_{t,n} = (x_{t-n+1}^{\prime}, \ldots, x_{t}^{\prime})$ .

Multi-Head Hashing. Directly parameterizing the combinatorial space of all possible N-grams is intractable. Following Tito Svenstrup et al. (2017), we adopt a hashing-based approach. To mitigate collisions, we employ K distinct hash heads for each N-gram order n. Each head k maps the compressed context to an index within an embedding table $E_{n,k}$ (of prime size $M_{n,k}$ ) via a deterministic function $\varphi_{n,k}$ :

$$
z _ {t, n, k} \triangleq \varphi_ {n, k} (g _ {t, n}), \quad \mathbf {e} _ {t, n, k} = \mathbf {E} _ {n, k} [ z _ {t, n, k} ]. \tag {1}
$$

In practice, $\varphi_{n,k}$ is implemented as a lightweight multiplicative-XOR hash. We construct the final memory vector $e_{t} \in R^{d_{mem}}$ by concatenating all retrieved embeddings:

$$
\mathbf {e} _ {t} \triangleq \prod_ {n = 2} ^ {N} \prod_ {k = 1} ^ {K} \mathbf {e} _ {t, n, k}. \tag {2}
$$

# 2.3. Context-aware Gating

The retrieved embeddings $e_{t}$ serve as context-independent priors. Being static, however, they inherently lack contextual adaptability and may suffer from noise due to hash collisions or polysemy (Haber and Poesio, 2024). To enhance expressivity and resolve this ambiguity, we employ a context-aware gating mechanism inspired by Attention (Bahdanau et al., 2015; Vaswani et al., 2017). Specifically, we utilize the current hidden state $h_{t}$ —which has aggregated global context via preceding attention layers—as a dynamic Query, while the retrieved memory $e_{t}$ serves as the source for both Key and Value projections:

$$
\mathbf {k} _ {t} = \mathbf {W} _ {K} \mathbf {e} _ {t}, \quad \mathbf {v} _ {t} = \mathbf {W} _ {V} \mathbf {e} _ {t} \tag {3}
$$

where $W_{K}, W_{V}$ are learnable projection matrices. To ensure gradient stability (Dehghani et al., 2023), we apply RMSNorm (Zhang and Sennrich, 2019) to the Query and Key before computing the scalar gate $\alpha_{t} \in (0,1)$ :

$$
\alpha_ {t} = \sigma \left(\frac {\mathrm{RMSNorm} (\mathbf {h} _ {t}) ^ {\top} \mathrm{RMSNorm} (\mathbf {k} _ {t})}{\sqrt {d}}\right). \tag {4}
$$

The gated output is defined as $\tilde{v}_{t} = \alpha_{t} \cdot v_{t}$ . This design enforces semantic alignment: if the retrieved memory $e_{t}$ contradicts the current context $h_{t}$ , the gate $\alpha_{t}$ tends toward zero, effectively suppressing the noise.

Finally, to expand the receptive field and enhance the model's non-linearity, we introduce a short, depthwise causal convolution (Gu et al., 2022; Peng et al., 2023). Let $\tilde{\mathbf{V}} \in \mathbb{R}^{T \times d}$ denote the sequence of gated values. Using a kernel size $w$ (set to 4), dilation $\delta$ (set to the max $N$ -gram order) and SiLU activation (Elfwing et al., 2018), the final output $\mathbf{Y}$ is computed as:

$$
\mathbf {Y} = \operatorname{SiLU} \left(\operatorname{Conv1D} (\operatorname{RMSNorm} (\tilde {\mathbf {V}}))\right) + \tilde {\mathbf {V}}, \tag {5}
$$

The Engram module is integrated into the backbone via a residual connection: $\mathbf{H}^{(\ell)} \leftarrow \mathbf{H}^{(\ell)} + \mathbf{Y}$ , followed by the standard Attention and MoE. Crucially, Engram is not applied to every layer; its specific placement is governed by the system-level latency constraints detailed in Section 2.5.

(a) Engram at training

(b) Engram at inference
Figure 2 | System implementation of Engram. (a) Training Phase: The massive embedding tables are sharded across available GPUs. An All-to-All communication primitive is employed to retrieve active embedding rows across devices. (b) Inference Phase: Engram tables are offloaded to host memory. By exploiting the deterministic retrieval logic, the host asynchronously prefetches and transfers embeddings, overlapping communication with the on-device computation of preceding Transformer blocks.

# 2.4. Integration with Multi-branch Architecture

In this work, rather than standard single-stream connections (He et al., 2016), we adopt the advanced multi-branch architecture as our default backbone, chosen for its superior modeling capabilities (Larsson et al., 2017; Szegedy et al., 2015; Xie et al., 2025; Zhu et al., 2025). A defining characteristic of this architecture is the expansion of the residual stream into M parallel branches, where information flow is modulated by learnable connection weights.

Although the Engram module is inherently topology-agnostic, adapting it to this multi-branch framework necessitates structural optimization to balance efficiency and expressivity. Specifically, we implement a parameter-sharing strategy: a single sparse embedding table and a Value projection matrix $W_{V}$ are shared across all M branches, whereas M distinct Key projection matrices $\{\mathbf{W}_{K}^{(m)}\}_{m=1}^{M}$ are employed to enable branch-specific gating behaviors. For the m-th branch with hidden state $\mathbf{h}_{t}^{(m)}$ , the branch-specific gating signal is computed as:

$$
\alpha_ {t} ^ {(m)} = \sigma \left(\frac {\mathrm{RMSNorm} (\mathbf {h} _ {t} ^ {(m)}) ^ {\top} \mathrm{RMSNorm} (\mathbf {W} _ {K} ^ {(m)} \mathbf {e} _ {t})}{\sqrt {d}}\right). \tag {6}
$$

The retrieved memory is then modulated by these independent gates applied to the shared value vector: $\mathbf{u}_{t}^{(m)} = \alpha_{t}^{(m)} \cdot (\mathbf{W}_{V} \mathbf{e}_{t})$ . This design allows the linear projections (one $W_{V}$ and M distinct $\mathbf{W}_{K}^{(m)}$ ) to be fused into a single dense FP8 matrix multiplication, maximizing the compute utilization of modern GPUs. Unless otherwise stated, all experiments utilize this integration with Manifold-Constrained Hyper-Connections (M = 4) (Xie et al., 2025).

Figure 3 | Sparsity allocation and Engram scaling. Left: Validation loss across allocation ratios $\rho$ . Two compute budgets are shown (2e20 and 6e20 FLOPs). Both regimes exhibit a U-shape, with hybrid allocation surpassing Pure MoE. Right: Scaling behavior in the infinite-memory regime. Validation loss exhibits a log-linear trend with respect to the number of embeddings.

# 2.5. System Efficiency: Decoupling Compute and Memory

Scaling memory-augmented models is often constrained by the limited capacity of GPU high-bandwidth memory (HBM). However, the deterministic retrieval mechanism of Engram naturally supports the decoupling of parameter storage from computational resources. Unlike MoE, which relies on runtime hidden states for dynamic routing, Engram's retrieval indices depend solely on the input token sequence. This predictability facilitates specialized optimization strategies for both training and inference, as illustrated in Figure 2.

During training, to accommodate large-scale embedding tables, we employ standard model parallelism by sharding the tables across available GPUs. An All-to-All communication primitive is used to gather active rows in the forward pass and dispatch gradients in the backward pass, enabling the total memory capacity to scale linearly with the number of accelerators.

During inference, this deterministic nature enables a prefetch-and-overlap strategy. Since memory indices are known prior to the forward pass, the system can asynchronously retrieve embeddings from abundant host memory via PCIe. To effectively mask communication latency, the Engram module is placed at specific layers within the backbone, leveraging the computation of preceding layers as a buffer to prevent GPU stalls. This necessitates a hardware-algorithm co-design strategy: while placing Engram deeper extends the compute window available for hiding latency, our ablation in Section 6.2 shows that modeling performance favors early intervention to offload local pattern reconstruction. Therefore, the optimal placement must simultaneously satisfy both modeling and system latency constraints.

Furthermore, natural language N-grams inherently follow a Zipfian distribution (Chao and Zipf, 1950; Piantadosi, 2014), where a small fraction of patterns accounts for the vast majority of memory accesses. This statistical property motivates a Multi-Level Cache Hierarchy: frequently accessed embeddings can be cached in faster storage tiers (e.g., GPU HBM or Host DRAM), while the long tail of rare patterns resides in slower, high-capacity media (e.g., NVMe SSD). This stratification allows Engram to scale to massive memory capacities with minimal impact on effective latency.

