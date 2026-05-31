<!-- VIEWER-ONLY verbatim slice of engram_paper_mineru.md lines 110-157. NOT authoritative; all gold coordinates point at engram_paper_mineru.md. -->
# 3. Scaling Laws and Sparsity Allocation

Engram, as an instantiation of conditional memory, is structurally complementary to the conditional computation provided by MoE experts. This section investigates the scaling properties of this duality and how to optimally allocate sparse capacity. Specifically, two key questions drive our research:

1. Allocation under Finite Constraints. When total parameters and training compute are fixed (Iso-parameter and Iso-FLOPs), how should we split the sparse capacity between MoE experts and Engram embeddings?
2. Infinite Memory Regime. Considering the non-scaling $O(1)$ overhead of Engram, if the memory budget is relaxed or scaled aggressively, what scaling behavior does Engram exhibit by itself?

# 3.1. Optimal Allocation Ratio Between MoE and Engram

Compute-matched formulation. We analyze the trade-off using three parameter metrics:

- $P_{\text{tot}}$ : total trainable parameters, excluding vocabulary embedding and LM head.
- $P_{\text{act}}$ : activated parameters per token. This quantity determines the training cost (FLOPs).
- $P_{\text{sparse}} \triangleq P_{\text{tot}} - P_{\text{act}}$ : the inactive parameters, which represents the “free” parameter budget available for scaling model size without incurring computational cost (e.g., unselected experts or unretrieved embeddings).

We keep $P_{tot}$ and $P_{act}$ fixed within each FLOPs budget, so that models have the same number of parameters and the same per-token FLOPs. For MoE, $P_{act}$ is determined by the top-k selected experts, while the parameters of non-selected experts contribute to $P_{sparse}$ . For Engram, only a constant number of slots are retrieved per token, so scaling the number of embedding slots increases $P_{tot}$ without increasing per-token FLOPs.

Allocation ratio. We define the allocation ratio $\rho \in [0,1]$ as the fraction of the inactive-parameter budget assigned to MoE expert capacity:

$$
P _ {\mathrm{MoE}} ^ {(\text {sparse})} = \rho P _ {\text {sparse}}, \quad P _ {\text {Engram}} = (1 - \rho) P _ {\text {sparse}}. \tag {7}
$$

Intuitively:

- $\rho = 1$ corresponds to a pure MoE model (all inactive parameters are routed experts).
- $\rho < 1$ reduces the number of routed experts and reallocates the freed parameters to Engram embedding slots.

Experimental protocol. We evaluate this trade-off at two compute regimes and maintain a constant sparsity ratio $P_{tot}/P_{act} \approx 10$ across both settings:

- $C = 2 \times 10^{20}$ FLOPs: $P_{\text{tot}} \approx 5.7 \text{B}$ and $P_{\text{act}} = 568 \text{M}$ . The baseline ( $\rho = 1$ ) has a total of 106 experts.
- $C = 6 \times 10^{20}$ FLOPs: $P_{\text{tot}} \approx 9.9\text{B}$ and $P_{\text{act}} = 993\text{M}$ . The baseline ( $\rho = 1$ ) has a total of 99 experts.

For different $\rho$ , we construct the corresponding model only by adjusting the number of routed experts and the number of Engram embedding slots. All runs use the identical training pipeline and optimization hyperparameters.

Results and Analysis. Figure 3 (left) reveals a consistent U-shaped relationship between validation loss and the allocation ratio $\rho$ . Remarkably, the Engram model achieves comparable performance to the pure MoE baseline ( $\rho = 100\%$ ) even when the MoE allocation is reduced to just $\rho \approx 40\%$ (i.e., a total of 46 experts for the 5.7B model and 43 experts for the 9.9B model). Furthermore, the pure MoE baseline proves suboptimal: reallocating roughly 20%–25% of the sparse parameter budget to Engram yields the best performance. Quantitatively, in the 10B regime ( $C = 6 \times 10^{20}$ ), validation loss improves from 1.7248 (at $\rho = 100\%$ ) to 1.7109 near the optimum of $\rho \approx 80\%$ ( $\Delta = 0.0139$ ). Crucially, the location of this optimum is stable across regimes ( $\rho \approx 75\%–80\%$ ), suggesting a robust allocation preference across the examined scales (under fixed sparsity). This observed U-shape confirms the structural complementarity between the two modules:

- MoE-dominated ( $\rho \rightarrow 100\%$ ): The model lacks dedicated memory for static patterns, forcing it to inefficiently reconstruct them through depth and computation.
- Engram-dominated ( $\rho \rightarrow 0\%$ ): The model loses conditional computation capacity, hurting tasks that require dynamic, context-dependent reasoning; memory cannot replace computation in this regime.

# 3.2. Engram under Infinite Memory Regime

In Section 3.1, we optimized the allocation under a fixed parameter budget. We now explore the complementary setting: aggressive memory scaling. This investigation is motivated by Engram's unique ability to decouple storage from compute detailed in Section 2.5.

Experimental protocol. We utilize a fixed MoE backbone with $P_{tot} \approx 3B$ and $P_{act} = 568M$ , trained for 100B tokens to ensure convergence. On top of this backbone, we attach an Engram table and sweep the number of slots M from $2.58 \times 10^{5}$ to $1.0 \times 10^{7}$ (adding up to $\approx 13$ billion parameters). For baselines, we compare against OverEncoding (Huang et al., 2025a), which integrates N-gram embeddings via averaging with the vocabulary embedding. We note that while other work such as SCONE (Yu et al., 2025) also investigates large-scale embeddings, it is primarily inference-focused and includes extra module (f-gram model) and additional training FLOPs, rendering it incompatible with the strict iso-compute constraints of this study.

Results. Figure 3 (right) demonstrates that scaling the number of memory slots yields a clear and consistent improvement in validation loss. Across the explored range, the curve follows a strict power law (linear in log-space), indicating that Engram provides a predictable scaling knob: larger memory continues to pay off without requiring additional computation. Crucially, regarding scaling efficiency: while the direct averaging approach of OverEncoding benefits from larger memory tables, Engram unlocks much larger scaling potential from the same memory budget. Together with the allocation law in Section 3.1, these results validate that conditional memory serves as a distinct, scalable axis of sparse capacity that complements the conditional computation of MoE.

