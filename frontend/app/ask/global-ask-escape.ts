// 全局问答浮窗里「此刻有没有**更内层**的弹层正在接管 Esc」。
//
// 与 `citation-card.tsx` 的 `citationPopoverHoldsEscape()` 同一条契约、同一个理由：
// 内层弹层在 **window 捕获期**拦 Esc（`preventDefault` + `stopPropagation` + 关掉
// 自己），于是一次 Esc 只收一层、浮窗的冒泡期监听根本收不到这次按键。剩下的唯一
// 缺口是原生 `<dialog>` 的 close request——捕获期 `preventDefault()` 能否掐掉它各
// 浏览器不一致，所以 launcher 的 `onCancel` 读这个计数兜底：有内层接管就不关窗。
//
// 单独成一个**零依赖的叶子模块**，而不是把计数放在工作区里：launcher 用 `lazy()`
// 加载工作区，从工作区取一个具名导出会把整条组件树（markdown、katex…）拖进首屏。
//
// 计数只在监听真的装上时才加、卸载即减，所以「计数 > 0」与「这次 Esc 会被内层吃掉」
// 是同一件事，不是两份可能漂移的判断。
let holders = 0;

/** 装上内层 Esc 拦截时调用；返回释放函数（重复调用幂等，避免 StrictMode 下双减）。 */
export function holdGlobalAskEscape(): () => void {
  holders += 1;
  let released = false;
  return () => {
    if (released) return;
    released = true;
    holders -= 1;
  };
}

export function globalAskLayerHoldsEscape(): boolean {
  return holders > 0;
}
