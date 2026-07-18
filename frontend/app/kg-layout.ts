// 图谱「类型分带」布局的纯函数层。抽出来是为了可测:page.tsx 是 .tsx,node --test
// 无法 import,而这里的坐标查表正是 PR A 原型链教训的第三处犯案点(见 kgBandTarget)。

export type BandTarget = [number, number];

export interface BandNode {
  type: string;
  x?: number;
  y?: number;
  vx?: number;
  vy?: number;
}

/** 分带向心力强度,与 d3 force 的 alpha 相乘。 */
export const KG_BAND_STRENGTH = 0.035;

/** 四个内置类型的分带锚点(画布相对坐标)。 */
export function kgTypeBandTargets(width: number, height: number): Record<string, BandTarget> {
  return {
    concept: [width * 0.34, height * 0.38],
    claim: [width * 0.66, height * 0.36],
    formula: [width * 0.34, height * 0.72],
    procedure: [width * 0.66, height * 0.72],
  };
}

/**
 * 取某个 node.type 的分带目标坐标;未知/自定义类型回落画布中心。
 *
 * 必须 Object.hasOwn 而非 `targets[type] ?? center`:targets 是普通对象字面量,带
 * Object.prototype。type 为 "constructor" 时 targets["constructor"] 拿到继承来的构造
 * 函数(truthy),`??` 不接管,随后 target[0]/target[1] 都是 undefined,
 * `(undefined - x) * 0.035 * alpha` → NaN 写进 node.vx/vy,节点坐标永久污染。
 * "__proto__" 同理(拿到原型对象)。object_type 是用户可自定义的字符串,这不是理论风险。
 */
export function kgBandTarget(
  targets: Record<string, BandTarget>,
  type: string,
  width: number,
  height: number,
  useTypeBands: boolean,
): BandTarget {
  const center: BandTarget = [width / 2, height / 2];
  if (!useTypeBands) return center;
  return Object.hasOwn(targets, type) ? targets[type] : center;
}

/** 单步向心速度增量。返回值必然有限——前提是 target 由 kgBandTarget 取得。 */
export function kgBandVelocity(
  node: BandNode,
  target: BandTarget,
  alpha: number,
): { vx: number; vy: number } {
  return {
    vx: (node.vx ?? 0) + (target[0] - (node.x ?? 0)) * KG_BAND_STRENGTH * alpha,
    vy: (node.vy ?? 0) + (target[1] - (node.y ?? 0)) * KG_BAND_STRENGTH * alpha,
  };
}
