# scripts/autotune.sh — 按核数设置「必须早于 numpy/BLAS 导入」的核绑定线程变量。
# 被 dev.sh / prod.sh 在 `source .env` 之后 source。原则:显式已设的一律不动;
# AUTOTUNE=0 整体关闭;CORES 可注入以覆盖核数探测(测试用)。
# 只设 BLAS/OpenMP 线程——ANN 建索引线程数由 config.py 的 kg_cluster_ann_threads
# 按核解析(进程内,同时覆盖离线 CLI);服务端并发旋钮刻意不按核缩放。
if [ "${AUTOTUNE:-1}" = "1" ]; then
  if command -v nproc >/dev/null 2>&1; then
    _at_cores="$(nproc)"
  else
    _at_cores="$(sysctl -n hw.ncpu 2>/dev/null || echo 1)"
  fi
  _at_cores="${CORES:-$_at_cores}"

  # GEMV/SpMV ~2-4 线程即压满带宽;封顶 8,既够用又给请求级并发留核。
  if [ "$_at_cores" -lt 8 ]; then _at_blas="$_at_cores"; else _at_blas=8; fi

  for _at_v in OMP_NUM_THREADS OPENBLAS_NUM_THREADS MKL_NUM_THREADS NUMEXPR_NUM_THREADS; do
    eval "_at_cur=\${$_at_v-}"
    if [ -z "$_at_cur" ]; then export "$_at_v=$_at_blas"; fi
  done

  echo "autotune: cores=${_at_cores} → BLAS(OMP/OPENBLAS/MKL/NUMEXPR)=${_at_blas}; ANN 线程由 config 按核解析(见后端首行日志); 模型端旋钮(EXTRACT/JOB/EMBED)不随核数变。" >&2
  unset _at_cores _at_blas _at_v _at_cur
fi
