<!-- VIEWER-ONLY verbatim slice of engram_paper_mineru.md lines 224-321. NOT authoritative; all gold coordinates point at engram_paper_mineru.md. -->
# 6. Analysis

In this section, we investigate the internal mechanisms of Engram, including its effective depth (Section 6.1), core module design (Section 6.2), and parametric sensitivity (Section 6.3). Additionally, we evaluate the inference throughput with offloading (Section 6.4) and conclude with a case study (Section 6.5).

# 6.1. Is Engram functionally equivalent to increasing the model's depth?

Current LLMs lack a dedicated knowledge lookup primitive and they rely on computation to simulate memory recall. As shown in Table 3, to recognize the entity "Diana, Princess of Wales", an LLM must consume multiple layers of Attention and FFNs to progressively compose features (Ghandeharioun et al., 2024; Jin et al., 2025; Li and Subramani, 2025), a process that could theoretically be identified via a knowledge lookup operation.

Given this, we posit that by equipping the model with an explicit knowledge lookup capability, Engram effectively mimics an increase in model depth by relieving the model of the early stages of feature composition. To validate this hypothesis, we employ two mechanistic interpretability tools: LogitLens (Belrose et al., 2023; nostalgebraist, 2020) and Centered Kernel Alignment analysis (CKA) (Davari et al., 2023; Kornblith et al., 2019).

# 6.1.1. Accelerated Prediction Convergence

We first analyze the evolution of predictions across layers using LogitLens (nostalgebraist, 2020). By projecting each intermediate layer's hidden state with the final LM Head, we compute the Kullback–Leibler divergence (Kullback and Leibler, 1951) between the intermediate output distribution and the model's final output distribution. This metric quantifies how close a latent representation is to being “prediction-ready” (Belrose et al., 2023; Csordás et al., 2025).

Figure 4 (a) reports the layer-wise KL divergence. Compared to the MoE baseline, both Engram variants exhibit systematically smaller KL divergence, with the most pronounced gap

Table 3 | Entity resolution example reproduced from Ghandeharioun et al. (2024). This table illustrates how LLMs gradually integrate context tokens through layers of attention and FFNs to construct the internal representation of the entity: "Diana, Princess of Wales". The "Latent State Translation" column displays the automatically generated text for the last token: "Wales" by PatchScope (Ghandeharioun et al., 2024), while the "Explanation" column presents the manual interpretation provided by the original authors.

<table><tr><td>Layer</td><td>Latent State Translation</td><td>Explanation</td></tr><tr><td>1-2</td><td>: Country in the United Kingdom</td><td>Wales</td></tr><tr><td>3</td><td>: Country in Europe</td><td>Wales</td></tr><tr><td>4</td><td>: Title held by female sovereigns in their own right or by queens consort</td><td>Princess of Wales (unspecific)</td></tr><tr><td>5</td><td>: Title given to the wife of the Prince of Wales (and later King)</td><td>Princess of Wales (unspecific)</td></tr><tr><td>6</td><td>: Diana, Princess of Wales (1961-1997), the first wife of Prince Charles, Prince of Wales, who was famous for her beauty and humanitarian work</td><td>Diana, Princess of Wales</td></tr></table>

appearing in the early blocks. The steeper descent in the Engram curves indicates that the model finishes feature composition much faster. This observation aligns with our hypothesis: by accessing external knowledge explicitly, Engram reduces the computational steps required, thereby reaching high-confidence, valid predictions earlier in the network hierarchy.

# 6.1.2. Representational Alignment and Effective Depth

To further investigate whether Engram layers semantically correspond to deeper layers of the baseline, we employ Centered Kernel Alignment (CKA), a widely established metric for comparing representational structures (Kornblith et al., 2019; Kriegeskorte et al., 2008). Given two sets of representations X and Y (e.g., activations from different models or layers), CKA is defined as:

$$
\mathrm{CKA} (K, L) = \frac {\mathrm{HSIC} (K , L)}{\sqrt {\mathrm{HSIC} (K , K) \mathrm{HSIC} (L , L)}} \tag {8}
$$

where $K = XX^{\top}$ and $L = YY^{\top}$ denote the Gram matrices (using a linear kernel) and HSIC is Hilbert-Schmidt Independence Criterion (Gretton et al., 2005). We employ a minibatch implementation with an unbiased estimator of HSIC (Davari et al., 2023) and evaluate on the Few-NERD dataset (Ding et al., 2021), extracting hidden states corresponding to the final token of named entities.

To rigorously quantify the layer-wise correspondence, we first compute the pairwise CKA similarity matrix $S \in [0,1]^{L \times L}$ , where L is the number of layers. We then introduce a soft alignment index $a_{j}$ , defined as the weighted centroid of the top-k most similar MoE layers for each Engram layer j:

$$
a _ {j} = \frac {\sum_ {i \in \mathcal {I} _ {j}} S _ {i , j} \cdot i}{\sum_ {i \in \mathcal {I} _ {j}} S _ {i , j}}, \quad \text {where} \mathcal {I} _ {j} = \underset {i} {\operatorname{argtop}} k (S _ {i, j}). \tag {9}
$$

Here, $S_{i,j}$ denotes the similarity score between MoE layer i and Engram layer j. The index $a_{j}$

Figure 5 | Architecture ablation results. We compare the 3B MoE baseline against Engram variations in two settings: (1) Layer Sensitivity (dark blue curve): Sweeping the insertion depth of a single Engram module confirms that early injection (Layer 2) is optimal, whereas efficacy degrades in deeper layers. (2) Component Ablation (Right Markers): Removing sub-modules from the reference configuration demonstrates the importance of multi-branch integration, tokenizer compression, and context-aware gating.

serves as a robust proxy for the “effective MoE depth” corresponding to Engram layer j, utilizing top-k filtering (with k = 5) to mitigate low-similarity noise.

Figure 4 (b)–(c) visualize the similarity heatmaps overlayed with the soft alignment curve (dashed white line). We observe a distinct upward shift from the diagonal, meaning that $a_{j} > j$ for a wide range of layers. For instance, the representations formed at layer 5 of Engram-27B align most closely with those at approximately layer 12 of the MoE baseline.

The consistent off-diagonal shift, which aligns with the LogitLens results (Section 6.1.1), confirms that Engram achieves deeper representations at earlier layers. This validates our central hypothesis: by bypassing early-stage feature composition via explicit lookups, Engram is functionally equivalent to increasing the model's effective depth.

# 6.2. Structural Ablation and Layer Sensitivity

In this section, we ablate Engram under a controlled setting to investigate the effectiveness of each key module design. Unless otherwise specified, the backbone is a 12-layer 3B MoE model (0.56B activated parameters) trained for 100B tokens. Figure 5 reports validation loss. The dashed orange line denotes the 3B MoE baseline (Val Loss = 1.808).

Reference configuration. We augment the backbone with a fixed 1.6B-parameter Engram memory. Our reference model uses $\{2,3\}$ -grams and inserts Engram at Layers 2 and 6, achieving Val Loss = 1.768, a substantial improvement over the MoE baseline ( $\Delta = 0.04$ ). All structural ablations below are defined relative to this reference.

Where should memory be injected? To study depth sensitivity, we keep the Engram budget fixed (1.6B) but consolidate it into a single Engram module, and sweep its insertion layer from 1 to 12 (dark blue “Layer Sweep” curve in Figure 5). This experiment exposes an inherent trade-off in Engram placement.

A placement trade-off. Injecting Engram early allows it to offload local pattern reconstruction before the backbone expends computational depth, aligning with the backbone's natural hierarchical processing (Ghandeharioun et al., 2024; Jin et al., 2025; Li and Subramani, 2025; Tenney et al., 2019). However, this incurs a cost in gating precision: early hidden states have not yet aggregated sufficient global context via attention, and the parallel branches lack the representational divergence required for fine-grained modulation (Xie et al., 2025; Zhu et al., 2025). Consequently, optimal placement requires balancing (i) offloading static local patterns early and (ii) utilizing stronger contextual queries for gating later.

The sweep shows that Layer 2 achieves the best single-layer performance (Val Loss = 1.770), outperforming Layer 1 and degrading as the insertion point moves deeper. This indicates that one round of attention is already sufficient to provide a meaningfully contextualized $h_{t}$ for gating, while still being early enough to replace the backbone's bottom-layer local aggregation.

While Layer 2 is optimal under a single injection constraint, we find that dividing the same 1.6B memory into two smaller modules (achieved by reducing the embedding dimension $d_{mem}$ ) and placing them at Layers 2 and 6 performs even better (Val Loss = 1.768). This layered design reconciles the trade-off by combining early intervention with rich, late-stage contextual gating. More importantly, layered insertion also provides a practical system advantage, enabling better utilization of the memory hierarchy as discussed in Section 2.5.

Which components matter? Starting from the reference configuration, we ablate individual design choices while keeping the Engram parameter budget fixed. Results are denoted by markers in Figure 5. We find that three components yield the most significant gains: (i) branch-specific fusion within the multi-branch backbone, (ii) context-aware gating, and (iii) tokenizer compression. Removing any of these causes the largest regressions in validation loss. Specifically, for the “w/o multi branch” ablation, we retain the mHC backbone structure but replace the branch-specific gating with a single Engram fusion applied to the hidden states after the pre-mapping $H^{pre}$ (Xie et al., 2025).

Other changes have smaller effects: removing the lightweight depthwise convolution only marginally degrades performance. Allocating capacity to 4-grams is slightly suboptimal under a fixed 1.6B budget—likely because it dilutes capacity from the more frequent 2/3-gram patterns—though we do not rule out that higher-order N-grams become beneficial at larger memory scales.

# 6.3. Sensitivity Analysis

To characterize the functional contribution of the Engram module, we evaluate the model by completely suppressing the sparse embedding output during inference while keeping the backbone unchanged. Crucially, this post-hoc ablation induces a training-inference inconsistency, potentially introducing noise in complex, mixed-capability tasks. Consequently, we prioritize the analysis of Factual Knowledge and Reading Comprehension—the two extremes of the sensitivity spectrum—which exhibit the highest signal-to-noise ratio under this stress test.

As shown in Figure 6, the results reveal a sharp functional dichotomy. Factual knowledge

Figure 6 | Retained performance under Engram ablation. Factual knowledge relies heavily on the Engram module, whereas reading comprehension is largely preserved by the backbone.

benchmarks suffer a catastrophic collapse, retaining only 29–44% of the original performance (e.g., TriviaQA at 29%), confirming that the Engram module acts as the primary repository for parametric knowledge. Conversely, reading comprehension tasks are remarkably resilient, retaining 81–93% (e.g., C3 at 93%), suggesting that context-grounded tasks rely primarily on the backbone's attention mechanism rather than Engram.

# 6.4. System Efficiency

A pivotal system advantage of Engram over routing-based MoE is that its sparse activations are addressed by explicit, static hash IDs. This yields a strictly deterministic memory access pattern: indices for the next Engram lookup are fixed once the token sequence is known and can be computed before the corresponding layer executes.

Experimental Setup. We implemented an inference harness based on nano-vLLM $^{1}$ —a streamlined prototype of the industry-standard vLLM engine (Kwon et al., 2023). To obtain a clean latency baseline without the confounding communication patterns of Expert Parallel in MoE, we benchmark on two dense backbones (Dense-4B and Dense-8B). We insert a massive 100B-parameter Engram layer into the second Transformer block, with the entire embedding table resident in host DRAM. During inference, the system prefetches embeddings for the Engram layer asynchronously, overlapping the PCIe transfer with the computation of the first block.

Results. As detailed in Table 4, offloading a 100B-parameter embedding table incurs a negligible throughput penalty, peaking at only 2.8% on the 8B backbone. This confirms that the compute intensity of early dense blocks provides a sufficient temporal window to mask the retrieval latency. Crucially, the effective communication volume per step scales with the number of activated slots rather than the total embedding table size.

Crucially, this experiment serves as a conservative baseline. While the hierarchical design in Section 2.5 exploits Zipfian locality to cache frequent items in HBM, our experimental setup forces all retrievals to traverse the PCIe bus from host memory. The fact that this baseline

Table 4 | End-to-end Inference Throughput. We measure infernce throughput with a 100B-parameter Engram layer entirely offloaded to host memory.

<table><tr><td colspan="3">Experimental Setup</td></tr><tr><td>Hardware</td><td></td><td>NVIDIA H800</td></tr><tr><td>Workload</td><td></td><td>512 Sequences</td></tr><tr><td>Sequence Length</td><td></td><td>Uniform(100, 1024)</td></tr><tr><td colspan="3">Throughput Results</td></tr><tr><td>Base Model</td><td>Configuration</td><td>Throughput (tok/s)</td></tr><tr><td rowspan="2">4B-Dense</td><td>Baseline</td><td>9,031.62</td></tr><tr><td>+ 100B Engram (CPU Offload)</td><td>8,858.28</td></tr><tr><td rowspan="2">8B-Dense</td><td>Baseline</td><td>6,315.52</td></tr><tr><td>+ 100B Engram (CPU Offload)</td><td>6,140.02</td></tr></table>

retrieval strategy yields minimal overhead strongly suggests that a fully optimized, locality-aware implementation would incur negligible throughput penalty.

# 6.5. Case Study: Gating Visualization

In Section 2.3, we introduced the context-aware gating mechanism, designed to dynamically modulate the integration of retrieved static memory into the backbone. To empirically validate whether Engram behaves as intended, we visualize the gating scalar $\alpha_{t}$ of Engram-27B $^{2}$ across various samples in Figure 7.

The results demonstrate a distinct pattern of selectivity. The gating mechanism consistently activates (shown in red) upon completing local, static patterns. In English, we observe strong activations on multi-token named entities (e.g., “Alexander the Great”, “the Milky Way”) and formulaic phrases (e.g., “By the way”, “Princess of Wales”). This behavior generalizes effectively across languages. In the Chinese examples, Engram identifies and retrieves distinct idiomatic expressions and historical entities, such as “Four Great Inventions” (四大发明) and “Zhang Zhongjing” (张仲景). These qualitative results confirm that Engram successfully identifies and handles stereotyped linguistic dependencies, effectively relieving the Transformer backbone from memorizing these static associations.

