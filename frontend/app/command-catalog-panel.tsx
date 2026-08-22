// 命令目录(方案 C·C1c)的界面:来源详情里的入口卡片 + 审阅弹窗。
//
// 形态选择与理由(每一处都有取舍,不是默认值):
//
// · **入口是来源详情里的一张卡片,不是标题栏的图标按钮。** 这个入口要同时承载三种
//   长期状态(未识别 / 识别中+进度 / 已识别+摘要)、一条可能很长的失败文案和一颗取消
//   按钮。图标按钮只画得下一个字形加一句 title,「已处理 3/12 段 · 已识别 5 条」无处
//   可放。卡片放在来源元信息那排标签下面 —— 「这份来源能做什么」本来就读在那里。
//
// · **成本预告用调用方的确认弹窗(page.tsx 的 infoModal),不自造。** 那个弹窗已经是
//   FloatingModalCard、已经有 title/message/sections/actions 这套形状,再造一个居中
//   确认框只会多一套拖动实现(仓库红线明令禁止)。所以本模块把「请弹一个确认」通过
//   `onConfirm` 交回调用方,自己不渲染确认框。
//
// · **审阅是居中浮动弹窗,复用 FloatingModalCard。** 红线要求居中浮动弹窗必须用它。
//   不做全屏视图:审阅是「开着来源详情顺手做」的动作,全屏会把来源详情顶掉、丢掉
//   阅读上下文。不做贴边抽屉:候选一行要放命令名/语法/参数数/出处四段,380px 抽屉
//   里全都要换行。
"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  CATALOG_APPLY_MAX,
  CATALOG_PAGE_SIZE,
  applyCommandCatalog,
  cancelCommandCatalog,
  dismissCommandCatalog,
  fetchCommandCatalogCandidates,
  fetchCommandCatalogJob,
  fetchCommandCatalogPreview,
  startCommandCatalog,
  type CommandCatalogCandidate,
  type CommandCatalogJob,
} from "./command-catalog-api.ts";
import {
  catalogApplyBatch,
  catalogApplyOutcome,
  catalogBlocksRestart,
  catalogDismissOutcome,
  catalogEntryLabel,
  catalogOverflowNote,
  catalogPendingReviewNote,
  catalogPreviewCopy,
  catalogSectionLabel,
  catalogStatusLine,
  dismissReasonText,
  isCatalogBusy,
  isCatalogSettled,
  rejectionText,
  type CatalogApplyOutcome,
} from "./command-catalog-model.ts";
import { FloatingModalCard } from "./floating-modal-card";
import { toUserMessage } from "./errors.ts";

/** 识别任务的轮询间隔。任务是分片跑的,进度以「段」(窗口)为粒度,4s 足够跟上。 */
const POLL_MS = 4000;

/**
 * 有界轮询窗口。识别一份大手册要跑很多次模型调用,10 分钟明显不够;30 分钟之后
 * 仍未落终态就停止**自动**轮询——不再猜、不再兜底放开按钮(P2 修复:旧版猜测
 * 「窗口关了大概率是跑完了」曾经连带把取消入口一起藏起来,而长任务恰恰在这时候
 * 最需要能取消)。停止自动刷新后界面换出一颗手动「刷新」入口(见 refreshNow),
 * 让用户主动要一次最新状态;按钮的可点性任何时候都只认最后一次已知的真实
 * job 状态,不认轮询窗口开没开。
 */
const POLL_WINDOW_MS = 30 * 60 * 1000;

/**
 * 请调用方弹一个确认框(复用 page.tsx 既有的 infoModal,不在本模块自造)。
 *
 * 正文叫 `body` 不叫 `message` 的理由见 command-catalog-model.ts 里 CatalogPreviewCopy
 * 的注释(errors-guard 把每一处 `.message` 读取都当原始错误文本记账)。
 */
export type CatalogConfirmRequest = {
  title: string;
  body: string;
  sections: Array<[string, string[]]>;
  confirmLabel: string;
  onConfirm: () => void;
};

/**
 * 请调用方在 **page 根层** 渲染审阅弹窗(`CommandCatalogReview`),不在本组件的
 * 子树里渲染。
 *
 * 理由是硬的,不是风格偏好:审阅弹窗复用 `FloatingModalCard`,桌面态恒给卡片
 * `translate3d(...)`(见 floating-modal-card.tsx:19-25 的逐字禁令),卡片因此成为
 * 它内部一切 `position:fixed` 后代的包含块。本组件挂在来源详情卡片(同样是
 * `FloatingModalCard`,见 source-detail-window.tsx)的子树里 —— 如果审阅弹窗也在
 * 这棵子树里渲染,920px 宽的它会被 740px 的来源详情卡片 `overflow:hidden` 裁掉,
 * 确认按钮/底栏整段看不见。与成本预告 `onConfirm` 同构:本组件只负责「请求打开」,
 * 由调用方(page.tsx)在根层持有开关状态并渲染真正的弹窗。
 */
export type CatalogReviewRequest = {
  notebookId: string;
  sourceId: string;
  sourceTitle: string;
  jobId: string;
};

type SectionProps = {
  notebookId: string;
  sourceId: string;
  sourceTitle: string;
  /** 只读成员看得到结果,但发起/取消/确认都是 owner-only(后端同口径)。 */
  canEdit: boolean;
  onConfirm: (request: CatalogConfirmRequest) => void;
  onOpenReview: (request: CatalogReviewRequest) => void;
  /**
   * R8 修复(codex PR #412 评审 P1):审阅弹窗每完成一次确认/跳过就 +1 的单调
   * 计数器,由调用方(page.tsx)持有并递增——本组件据它重新拉一次 `.../job`。
   *
   * 为什么必须有这条线:审阅弹窗提升到 page 根层之后(见 CatalogReviewRequest),
   * 它与本卡片之间没有任何共享状态,而本卡片的 job 快照里带着
   * `progress.pending_candidates`——「重新识别」的拦截判据。审阅者把候选全部
   * 确认/跳过完、关掉弹窗,卡片手里仍是那份带着旧 pending 计数的快照,「重新
   * 识别」于是永久禁用,除非重开来源详情。
   *
   * 传计数器而不是直接把响应里的 `pending_remaining` 递上来,是因为重拉的是
   * **整份 job 行**:它是权威的(与轮询、手动刷新、409 兜底走同一条读),而把
   * 一个数字合并进本地快照会造出一份「除了这一格以外都是旧的」的混合状态。
   * 同一条理由在后端 `cancel()` 里已经写过一次:状态只认行,不认哪个分支跑过。
   */
  reviewSeq?: number;
};

export function CommandCatalogSection({
  notebookId,
  sourceId,
  sourceTitle,
  canEdit,
  onConfirm,
  onOpenReview,
  reviewSeq = 0,
}: SectionProps) {
  const [job, setJob] = useState<CommandCatalogJob | null>(null);
  const [starting, setStarting] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [pollUntil, setPollUntil] = useState(0);
  // 有界轮询窗口是否已经过期。它只关掉自动刷新、换出「刷新」手动入口——不再放开
  // 任何按钮(P2 修复:那种兜底曾经把取消入口跟着藏起来,长任务最需要取消的时候
  // 反而找不到)。真判据永远是任务终态,见下面 busyByJob。
  const [pollExpired, setPollExpired] = useState(false);
  // 手动「刷新」按钮的在飞位——GET 本身没有副作用,但两次点击叠一起没有意义。
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  // 异步回填的归属校验:切来源后迟到的响应不得覆盖新来源的状态。
  const scopeRef = useRef(`${notebookId}/${sourceId}`);
  // P1 修复:挂载时的首拉(下面「打开来源就取一次最近任务」那个 effect)可能在
  // 用户确认、startCommandCatalog 真的把新任务建起来之后才回来——scopeRef 只挡得住
  // 「换了来源」这一种迟到,挡不住「同一个来源里,首拉比启动动作慢」这一种。真发生
  // 时,首拉响应的无条件 setJob(latest) 会把刚建的新 job 覆盖成旧值/null,轮询与
  // 取消入口跟着消失,而识别本身仍在后台跑(继续付费)。这里用一个单调递增的请求
  // 代际:beginRun 真的建起新任务时 +1,首拉响应到达时若代际已经变了就认得出自己
  // 过期,静默丢弃而不是覆盖。只挡首拉这一路——轮询 effect、beginRun/requestCancel
  // 自己的 setJob 都不受它约束,那些都是权威来源,不是可能过期的旁路读。
  //
  // ⚠ 代际只能在 scoped() **之内**推进(R4 修复)。这个 ref 是跨来源共用的一个计数
  // 器,而 beginRun 的 POST 可能在用户切到别的来源之后才回来:在 scope 校验之前
  // 推进,等于让来源 A 那次迟到的响应作废来源 B 正在飞的首拉——B 的响应被丢掉,
  // B 明明有个正在跑的任务却显示成「还没识别过」,取消入口一并消失。放进 scoped()
  // 里就自洽了:scope 不匹配时那次响应本来就一个 setState 都不做,没有任何新状态
  // 需要靠代际去保护,自然也不该作废别人的读。
  const jobEpochRef = useRef(0);
  // 确认弹窗的回调活得比本组件长(它挂在 page.tsx 的 infoModal 上)。用户在预告
  // 弹出后关掉来源详情、再点「开始识别」,那次 POST 会真的花钱跑一次识别,而界面
  // 上已经没有任何地方能显示它 —— 卸载后不发起,不做无人看管的付费调用。
  //
  // ⚠ effect 体必须把 `aliveRef.current` 重新置 true(不能只靠 `useRef(true)` 初值):
  // React 18 StrictMode 的 dev 双调用会 mount → unmount → mount,cleanup 已经把它
  // 置成 false;不在 remount 时复位,ref 会永久停在 false,「开始识别」从此哑火
  // (对齐 knowhow-import.tsx 里 mountedRef 的同款写法与同款注释)。
  const aliveRef = useRef(true);
  useEffect(() => {
    aliveRef.current = true;
    return () => { aliveRef.current = false; };
  }, []);

  // 切来源/切库 = 全部状态归零(红线要求的兜底之一)。同时刷新归属键,让在飞的
  // 请求回来时认得出自己已经过期。审阅弹窗的开关不在这里管:它已经提升到 page
  // 根层,由调用方在同一次源切换里一并关闭(见 page.tsx)。
  useEffect(() => {
    scopeRef.current = `${notebookId}/${sourceId}`;
    setJob(null);
    setStarting(false);
    setPreviewing(false);
    setCancelling(false);
    setPollUntil(0);
    setPollExpired(false);
    setError("");
  }, [notebookId, sourceId]);

  const scoped = useCallback(
    (run: () => void) => {
      if (scopeRef.current === `${notebookId}/${sourceId}`) run();
    },
    [notebookId, sourceId],
  );

  // 打开来源就取一次最近任务:刷新页面/重开详情时,后台仍在跑的识别必须重新把
  // 按钮锁住并接上进度,而不是显示成「还没识别过」。
  useEffect(() => {
    let cancelledEffect = false;
    const requestEpoch = jobEpochRef.current;
    fetchCommandCatalogJob(notebookId, sourceId)
      .then(({ job: latest }) => {
        if (cancelledEffect) return;
        // 见 jobEpochRef 的声明:这期间如果真的发起了一次识别,这份首拉响应已经
        // 过期,不能拿它覆盖刚建好的新 job。
        if (jobEpochRef.current !== requestEpoch) return;
        scoped(() => {
          setJob(latest);
          if (latest && !isCatalogSettled(latest.status)) {
            setPollUntil(Date.now() + POLL_WINDOW_MS);
          }
        });
      })
      .catch(() => {
        // 只读失败不该在来源详情里弹错:入口退化成「可以发起」,发起时才真正报错。
      });
    return () => { cancelledEffect = true; };
  }, [notebookId, sourceId, scoped]);

  // R8:审阅弹窗每完成一次确认/跳过就重新拉一次 job(见 reviewSeq 的声明)。
  //
  // `reviewSeq <= 0` 提前 return 有两个作用,都不是可选的:挂载那一轮不重复上面
  // 那次首拉;换来源时调用方把计数器归零,这里跟着跑一遍却什么都不做——否则
  // 「3 → 0」这一跳会给刚切过去的新来源白发一次请求。
  //
  // 与首拉不同,这条不受 jobEpochRef 约束:它不是可能过期的旁路读,而是一次由
  // 明确的写动作触发的权威重读——就像 refreshNow。
  useEffect(() => {
    if (reviewSeq <= 0) return;
    let cancelledEffect = false;
    fetchCommandCatalogJob(notebookId, sourceId)
      .then(({ job: latest }) => {
        if (cancelledEffect) return;
        scoped(() => setJob(latest));
      })
      .catch(() => {
        // 只读失败不改状态:手里那份快照仍然是最后一次已知的真实状态,拿一次
        // 失败的 GET 去清空它只会让界面更糟。
      });
    return () => { cancelledEffect = true; };
  }, [reviewSeq, notebookId, sourceId, scoped]);

  // P2 修复:忙碌态只认「job 实际有没有到终态」,不再被 pollExpired 稀释。旧版
  // `busy = busyByJob && !pollExpired` 曾经在轮询窗口过期后让主按钮和取消按钮一起
  // 假装「没在跑」——取消按钮只在 busy 时渲染,恰恰在长任务最需要取消的时候把入口
  // 藏了起来。现在 busy 就是 busyByJob 本身(留着这个名字只是不用再动 JSX 里那一串
  // 既有引用);pollExpired 只影响下面这个轮询 effect 是否还在自动刷新,以及要不要
  // 显示「已停止自动刷新」的提示 + 手动刷新入口(见 refreshNow)——不再兜底解除
  // 任何按钮。
  const busyByJob = isCatalogBusy(starting, job);
  const busy = busyByJob;

  // 有界轮询。窗口到期只置 pollExpired、停止自动刷新,不清 job、不改变任何按钮的
  // 可点性——已经拿到的进度、取消入口原样保留,只是不再自己刷新;界面改用下面的
  // 「刷新」按钮让用户主动要一次最新状态(而不是像旧版那样悄悄猜它可能已经跑完)。
  //
  // ⚠ 置 pollExpired 的同时必须把 pollUntil 一并归零:这个 effect 依赖 pollUntil,
  // 归零会让它重新跑一遍、在顶部 `pollUntil <= 0` 处提前 return,从而触发上一轮的
  // cleanup 把 interval 清掉。不归零的话,`Date.now() > pollUntil` 分支会在每个
  // POLL_MS 周期反复命中、反复 setPollExpired(true)(状态没变,React 会跳过重渲染,
  // 但 interval 本身仍然常驻空转,直到组件卸载或来源切换才被回收)。
  useEffect(() => {
    if (!busyByJob || pollUntil <= 0) return;
    if (pollUntil <= Date.now()) {
      setPollExpired(true);
      setPollUntil(0);
      return;
    }
    let cancelledEffect = false;
    const timer = window.setInterval(() => {
      if (Date.now() > pollUntil) {
        setPollExpired(true);
        setPollUntil(0);
        return;
      }
      fetchCommandCatalogJob(notebookId, sourceId)
        .then(({ job: latest }) => {
          if (cancelledEffect || !latest) return;
          scoped(() => {
            setJob(latest);
            if (isCatalogSettled(latest.status)) {
              setPollUntil(0);
              setCancelling(false);
            }
          });
        })
        .catch(() => {});
    }, POLL_MS);
    return () => { cancelledEffect = true; window.clearInterval(timer); };
  }, [busyByJob, pollUntil, notebookId, sourceId, scoped]);

  async function beginRun() {
    // 见 aliveRef 的声明:确认回调可能在本组件卸载之后才被点。
    if (!aliveRef.current) return;
    // 确认弹窗(page.tsx 的 infoModal)的 onConfirm 回调是本次 requestRun() 那次渲染
    // 闭包捕获的 beginRun —— 它绑死在预告弹出那一刻的 notebookId/sourceId。用户可能
    // 在预告弹出后、点确认前切到了别的来源(本组件实例被复用,aliveRef 仍 true,
    // scopeRef 却已经被切换来源的 effect 刷新成新来源)。scoped() 只能拦住响应回填
    // (见其声明处注释),拦不住这次 POST 本身——POST 一旦发出就已经向模型付费。这里
    // 在发请求前把回调闭包捕获的 scope 与当前 scopeRef 比一次,不一致就静默放弃、
    // 不弹错:用户已经在看别的来源,这次识别对他不再有意义,报错反而是噪声。
    //
    // page.tsx 的 infoModal 是全应用共用的通用确认框(破坏性操作、索引动作等都在
    // 用同一个 state 槽位),没有按「这是命令目录的预告」打标签的既有形状,来源切换
    // 时无法只关掉「属于命令目录的那个」而不误关别的确认框——所以这里不去 page.tsx
    // 加一条按 sourceDetail?.id 关闭 infoModal 的 effect(那会在无关场景里也吞掉正在
    // 显示的确认框),只靠这一道请求前置校验做纵深防御。
    if (scopeRef.current !== `${notebookId}/${sourceId}`) return;
    setError("");
    setStarting(true);
    try {
      const { job: started } = await startCommandCatalog(notebookId, sourceId);
      scoped(() => {
        // 见 jobEpochRef 的声明:任务真的建起来了、而且这次响应确实还属于当前来源,
        // 才让还没回来的挂载期首拉响应认得出自己已经过期。推进必须在 scoped() 内,
        // 否则会误伤另一个来源正在飞的首拉。
        jobEpochRef.current += 1;
        setJob(started);
        setPollExpired(false);
        setPollUntil(Date.now() + POLL_WINDOW_MS);
      });
    } catch (err) {
      // 409 = 该来源已有任务在跑。响应刻意只带用户可读文案、不带那个任务,所以
      // 这里必须自己去 `.../job` 把它捡回来 —— 否则界面会停在「没有任务」,而后台
      // 正在花钱。其他错误同样先试着捡一次:任何情况下界面都该反映真实状态。
      const message = toUserMessage(err, "无法开始识别命令目录");
      try {
        const { job: running } = await fetchCommandCatalogJob(notebookId, sourceId);
        scoped(() => {
          // 同样让还没回来的挂载期首拉响应过期——这次捡回来的才是权威快照。同样只在
          // scope 校验之后推进(理由见 jobEpochRef 的声明)。
          jobEpochRef.current += 1;
          setJob(running);
          if (running && !isCatalogSettled(running.status)) {
            setPollExpired(false);
            setPollUntil(Date.now() + POLL_WINDOW_MS);
          }
        });
      } catch {
        // 捡不回来就只报原始错误,不编造状态。
      }
      scoped(() => setError(message));
    } finally {
      scoped(() => setStarting(false));
    }
  }

  async function requestRun() {
    // 见 catalogBlocksRestart 的声明:上一轮还有候选没审阅完时,「重新识别」
    // 按钮本身已经 disabled,这里是同一条判据的纵深防御,不是唯一防线。
    if (previewing || busy || catalogBlocksRestart(job)) return;
    setError("");
    setPreviewing(true);
    try {
      const preview = await fetchCommandCatalogPreview(notebookId, sourceId);
      const copy = catalogPreviewCopy(preview);
      scoped(() => onConfirm({ ...copy, onConfirm: () => { void beginRun(); } }));
    } catch (err) {
      scoped(() => setError(toUserMessage(err, "无法读取命令目录的成本预告")));
    } finally {
      scoped(() => setPreviewing(false));
    }
  }

  async function requestCancel() {
    if (cancelling) return;
    setCancelling(true);
    try {
      const result = await cancelCommandCatalog(notebookId, sourceId);
      scoped(() => {
        if (result.job) setJob(result.job);
        // cancelling(worker 会在下一个分片边界停)时保持忙碌态,由轮询接手落终态;
        // 已经落终态或本就没在跑,当场解除。
        if (!result.job || isCatalogSettled(result.job.status)) {
          setCancelling(false);
          setPollUntil(0);
        }
      });
    } catch (err) {
      scoped(() => {
        setError(toUserMessage(err, "取消失败"));
        setCancelling(false);
      });
    }
  }

  // P2 修复:轮询窗口过期后不再兜底猜测,而是给一颗手动入口——GET 本身没有副
  // 作用,拿到什么就照实反映什么。命中终态就彻底停轮询;仍在跑就重开一轮窗口,
  // 继续自动刷新。
  //
  // R11 P2 修复:命中终态(或 latest 缺席)时还必须清 cancelling——否则「轮询窗口
  // 过期 → 点了取消(cancelling=true,由轮询接手落终态)→ worker 真的落了终态,但
  // 窗口已经不再自动刷新 → 用户点手动刷新」这条路径下,取消按钮会永远卡在
  // 「取消中…」,直到重开来源详情。轮询路径(上面的 effect)与立即取消路径
  // (requestCancel)命中终态都会清这面标志,这里补齐第三条落终态的路径,三处
  // 判据保持一致。
  async function refreshNow() {
    if (refreshing) return;
    setRefreshing(true);
    try {
      const { job: latest } = await fetchCommandCatalogJob(notebookId, sourceId);
      scoped(() => {
        setJob(latest);
        setPollExpired(false);
        if (latest && !isCatalogSettled(latest.status)) {
          setPollUntil(Date.now() + POLL_WINDOW_MS);
        } else {
          setPollUntil(0);
          setCancelling(false);
        }
      });
    } catch (err) {
      scoped(() => setError(toUserMessage(err, "刷新状态失败")));
    } finally {
      scoped(() => setRefreshing(false));
    }
  }

  const status = catalogStatusLine(job);
  const entryLabel = catalogEntryLabel(job);
  const canReview = Boolean(job) && isCatalogSettled(job?.status ?? "");
  // 「查看识别结果」不是长任务(只开弹窗),所以它不吃忙碌位;发起才吃。
  const primaryOpensReview = canReview && job?.status === "succeeded";
  // R6 P1:上一轮(不论成功/失败/取消)还有候选没审阅完,重新发起识别的入口先被
  // 这个拦住,不等发起后才在后端吃 409——见 catalogBlocksRestart 的声明。这颗
  // 判据同时管着两个不同的按钮:已成功时是「重新识别」(下方 sort-button 分支)、
  // 失败/取消时是主按钮「识别命令目录」本身(下方 new-pill 分支)。
  const blocksRestart = catalogBlocksRestart(job);

  // 打开审阅:弹窗本身渲染在 page 根层(见 CatalogReviewRequest 的注释),本组件
  // 只负责把打开这件事连同定位它需要的四个字段一起交回调用方。
  function openReview() {
    if (!job) return;
    onOpenReview({ notebookId, sourceId, sourceTitle, jobId: job.id });
  }

  // 只读成员且这份来源还没识别过 —— 卡片里一颗按钮都不会渲染,留下的是一张说明
  // 一件他做不到的事的空卡片。整块不渲染,别在来源详情里摆一段无从行动的噪声。
  if (!canEdit && !job) return null;

  return (
    <section className="catalog-card">
      <div className="catalog-card-head">
        <h3>命令目录</h3>
        <p>
          从这份来源里识别命令、语法与参数，整理成可检索的经验表。
        </p>
      </div>
      {status && (
        <p className={`catalog-status catalog-status--${status.tone}`} role="status">
          {status.text}
        </p>
      )}
      {/* P2 修复:轮询窗口过期不再兜底放开按钮,而是老实说清「自动刷新停了」并给
          一颗手动入口——job 仍未落终态时才需要这条提示,已经落终态就没有可刷新的
          意义。 */}
      {pollExpired && busyByJob && (
        <p className="catalog-status catalog-status--info" role="status">
          已停止自动刷新，点击刷新查看最新状态。
          <button
            type="button"
            className="ghost-button catalog-poll-refresh"
            disabled={refreshing}
            onClick={() => { void refreshNow(); }}
          >
            {refreshing ? "刷新中…" : "刷新"}
          </button>
        </p>
      )}
      {error && <p className="catalog-status catalog-status--danger" role="alert">{error}</p>}
      {/* R6 P1:重新发起识别的入口(「重新识别」或失败/取消后的主按钮)被待审阅
          候选拦住时,老实说清楚为什么——不是一颗莫名其妙 disabled 的按钮。 */}
      {blocksRestart && canEdit && (
        <p className="catalog-status catalog-status--info" role="status">
          {catalogPendingReviewNote(job?.progress.pending_candidates ?? 0)}
        </p>
      )}
      <div className="catalog-card-actions">
        {primaryOpensReview ? (
          <button
            type="button"
            className="new-pill"
            onClick={openReview}
          >
            {entryLabel}
          </button>
        ) : (
          canEdit && (
            <button
              type="button"
              className="new-pill"
              // 长任务红线:点下去到任务落终态之间一律不可点。忙碌位由
              // isCatalogBusy(starting, job) 给,解除靠任务终态这条证据。
              // R6 P1:失败/取消且还有候选没审阅完时,这颗主按钮走的是同一条
              // blocksRestart 判据——见上方声明与它旁边那句解释文案。
              disabled={busy || previewing || blocksRestart}
              onClick={() => { void requestRun(); }}
            >
              {busy ? "识别中…" : previewing ? "读取预告中…" : entryLabel}
            </button>
          )
        )}
        {primaryOpensReview && canEdit && (
          <button
            type="button"
            className="sort-button"
            // R5 P2:上一轮还有候选没审阅完,这颗按钮先被拦住——见上方
            // catalogBlocksRestart 与它旁边那句解释文案。
            disabled={busy || previewing || blocksRestart}
            onClick={() => { void requestRun(); }}
          >
            {busy ? "识别中…" : previewing ? "读取预告中…" : "重新识别"}
          </button>
        )}
        {canReview && !primaryOpensReview && (
          <button type="button" className="sort-button" onClick={openReview}>
            查看识别结果
          </button>
        )}
        {busy && canEdit && (
          <button
            type="button"
            className="sort-button"
            disabled={cancelling}
            onClick={() => { void requestCancel(); }}
          >
            {cancelling ? "取消中…" : "取消"}
          </button>
        )}
      </div>
    </section>
  );
}

type ReviewProps = {
  notebookId: string;
  sourceId: string;
  sourceTitle: string;
  jobId: string;
  canEdit: boolean;
  onClose: () => void;
  onOpenTable: (tableId: string) => void;
  onToast: (message: string) => void;
  /**
   * The review write is durable, but its browser effects belong to the exact
   * source-modal lease that started it. Closing the modal or changing owner
   * must not let a late response toast or refresh a replacement workspace.
   */
  isCurrent?: () => boolean;
  interactive?: boolean;
  zIndex?: number;
  /**
   * R8:一次确认/跳过已经落地,请调用方让入口卡片重新读一次 job(见
   * `CommandCatalogSection` 的 `reviewSeq`)。
   *
   * 失败路径也调,而且是刻意的:apply/dismiss 的 409 里有一种(来源已重新解析)
   * 后端会顺手把这个任务剩下的候选整批置为过期——服务端状态**变了**,只是这次
   * 请求没成功。只在成功时通知,那一种恰好就是拦截守卫已经解除、而卡片永远不
   * 知道的情况。重读一次 job 从不出错,所以宁可多读一次。
   */
  onReviewed?: () => void;
};

type CandidateState = "candidate" | "rejected" | "dismissed";

const TAB_LABEL: Record<CandidateState, string> = {
  candidate: "待审阅",
  rejected: "未通过",
  // 「已跳过」= 确认(apply)时因目标表已有同名行而被拦下,与「未通过」(接地
  // 校验整条拦下)是两种不同性质的落空,不能合并成一个页签。
  dismissed: "已跳过",
};

const TAB_ORDER: CandidateState[] = ["candidate", "rejected", "dismissed"];

/** 展开面板里默认直接显示的示例条数,其余折在「显示全部」后面。 */
const EXAMPLE_PREVIEW = 3;

const TAB_EMPTY_TEXT: Record<CandidateState, string> = {
  candidate: "没有待审阅的命令。",
  rejected: "没有被拦下的条目。",
  dismissed: "没有被跳过的条目。",
};

export function CommandCatalogReview({
  notebookId,
  sourceId,
  sourceTitle,
  jobId,
  canEdit,
  onClose,
  onOpenTable,
  onToast,
  isCurrent = () => true,
  onReviewed,
  interactive = true,
  zIndex,
}: ReviewProps) {
  const [tab, setTab] = useState<CandidateState>("candidate");
  const [items, setItems] = useState<CommandCatalogCandidate[]>([]);
  const [cursor, setCursor] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());
  // 「示例」区块的二级展开位。示例条数与单条长度后端已经截过(8 条 / 500 字),
  // 这里再收一次是版面问题不是安全问题:一条命令 8 段等宽代码会把整张卡撑成一屏,
  // 而审阅时真正要扫的是命令名和参数表。默认给前 EXAMPLE_PREVIEW 条,其余按需展开。
  const [allExamples, setAllExamples] = useState<ReadonlySet<string>>(new Set());
  const [applying, setApplying] = useState(false);
  // R7:显式跳过——「重新识别」被待审阅候选拦住时唯一的放弃出口(apply 只在
  // 冲突时才自动 dismiss)。独立于 applying 的在飞位,但两者互相排斥(见下面
  // runApply/runDismiss 里各自的守卫)——同一批候选不能被两个写路径同时处理。
  const [dismissing, setDismissing] = useState(false);
  const [outcome, setOutcome] = useState<CatalogApplyOutcome | null>(null);
  const [appliedTableId, setAppliedTableId] = useState("");
  const [error, setError] = useState("");
  const reviewAliveRef = useRef(true);
  useEffect(() => {
    reviewAliveRef.current = true;
    return () => { reviewAliveRef.current = false; };
  }, []);
  // 每次「重新从头加载」都换一代,让上一代迟到的分页响应认得出自己已经作废
  // (确认会改候选 state,旧游标指向的集合已经变了,append 上去就是错行)。
  const epochRef = useRef(0);
  const operationIsCurrent = () => reviewAliveRef.current && isCurrent();

  const loadPage = useCallback(
    async (state: CandidateState, from: number, append: boolean) => {
      const epoch = append ? epochRef.current : ++epochRef.current;
      setLoading(true);
      setError("");
      try {
        const page = await fetchCommandCatalogCandidates(notebookId, sourceId, {
          jobId,
          state,
          cursor: from,
          limit: CATALOG_PAGE_SIZE,
        });
        if (epoch !== epochRef.current) return;
        setItems((previous) => (append ? [...previous, ...page.items] : page.items));
        setCursor(page.next_cursor);
        setHasMore(page.has_more);
        setCounts(page.counts);
      } catch (err) {
        if (epoch !== epochRef.current) return;
        setError(toUserMessage(err, "无法加载识别结果"));
      } finally {
        if (epoch === epochRef.current) setLoading(false);
      }
    },
    [notebookId, sourceId, jobId],
  );

  useEffect(() => {
    setItems([]);
    setCursor(0);
    setHasMore(false);
    setSelected(new Set());
    setExpanded(new Set());
    setAllExamples(new Set());
    // 切页签时确认结果卡不该留在新页签下——它是上一次确认动作的摘要,与当前
    // 页签内容无关,留着看着像是刚才那次确认发生在这个页签里。
    setOutcome(null);
    void loadPage(tab, 0, false);
  }, [tab, loadPage]);

  const pageIds = useMemo(() => items.map((item) => item.id), [items]);
  const allSelected = pageIds.length > 0 && pageIds.every((id) => selected.has(id));
  const pendingCount = counts.candidate ?? 0;
  const rejectedCount = counts.rejected ?? 0;
  const appliedCount = counts.applied ?? 0;
  const dismissedCount = counts.dismissed ?? 0;

  function toggle(id: string) {
    setSelected((previous) => {
      const next = new Set(previous);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }

  function toggleAll() {
    setSelected((previous) => {
      if (pageIds.every((id) => previous.has(id))) {
        const next = new Set(previous);
        for (const id of pageIds) next.delete(id);
        return next;
      }
      return new Set([...previous, ...pageIds]);
    });
  }

  function toggleExpanded(id: string) {
    setExpanded((previous) => {
      const next = new Set(previous);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }

  function showAllExamples(id: string) {
    setAllExamples((previous) => new Set([...previous, id]));
  }

  async function runApply(body: { candidate_ids?: string[]; all_pending?: boolean }) {
    // 与 dismiss 互斥:同一批候选不能被两个写路径同时处理(后端也在同一把
    // per-target 锁下序列化,这里是纵深防御,不是唯一防线)。
    if (applying || dismissing) return;
    setApplying(true);
    setError("");
    try {
      const result = await applyCommandCatalog(notebookId, sourceId, body, jobId);
      if (!operationIsCurrent()) return;
      const summary = catalogApplyOutcome(result);
      setOutcome(summary);
      setAppliedTableId(result.table_id);
      if (result.rows_added > 0) onToast(summary.headline);
      // 确认改了候选的 state,旧游标指向的集合已经变了 —— 整个列表从头重拉,
      // 顺带把各档计数刷新成新的。选中集一并清掉:已确认的那些不该还勾着。
      setSelected(new Set());
      await loadPage(tab, 0, false);
    } catch (err) {
      if (operationIsCurrent()) setError(toUserMessage(err, "确认失败"));
    } finally {
      const current = operationIsCurrent();
      if (current) setApplying(false);
      // R8:无论成败都通知入口卡片重读 job——见 onReviewed 的声明(失败路径里
      // 有一种服务端状态确实变了的情况)。放在 finally 而不是两个分支各写一遍,
      // 是因为这正是「这次动作结束了」的唯一一处。
      if (current) onReviewed?.();
    }
  }

  function applySelected() {
    const batch = catalogApplyBatch(selected, items, CATALOG_APPLY_MAX);
    if (batch.length === 0) return;
    void runApply({ candidate_ids: batch });
  }

  /**
   * R7:显式跳过。与 runApply 同构(同一份选择契约、同一种「结束后从头重拉」
   * 收尾),只是落到 dismiss 端点而不是 apply——跳过从不写表,所以没有
   * appliedTableId 要更新。
   */
  async function runDismiss(body: { candidate_ids?: string[]; all_pending?: boolean }) {
    if (applying || dismissing) return;
    setDismissing(true);
    setError("");
    try {
      const result = await dismissCommandCatalog(notebookId, sourceId, body, jobId);
      if (!operationIsCurrent()) return;
      const summary = catalogDismissOutcome(result);
      setOutcome(summary);
      if (result.dismissed.length > 0) onToast(summary.headline);
      setSelected(new Set());
      await loadPage(tab, 0, false);
    } catch (err) {
      if (operationIsCurrent()) setError(toUserMessage(err, "跳过失败"));
    } finally {
      const current = operationIsCurrent();
      if (current) {
        setDismissing(false);
        onReviewed?.();  // 同 runApply,理由见 onReviewed 的声明。
      }
    }
  }

  function dismissSelected() {
    const batch = catalogApplyBatch(selected, items, CATALOG_APPLY_MAX);
    if (batch.length === 0) return;
    void runDismiss({ candidate_ids: batch });
  }

  const selectedCount = selected.size;
  // 后端一次最多确认一页,多选超出时先提交前 100 条 —— 提示必须写在按钮旁边,
  // 否则用户会以为 150 条都写进去了。
  const overCap = selectedCount > CATALOG_APPLY_MAX;

  return (
    <section
      className="utility-modal utility-modal-top"
      role="dialog"
      aria-modal={interactive}
      aria-hidden={!interactive}
      inert={interactive ? undefined : true}
      aria-label="命令目录识别结果"
      style={{ zIndex }}
      onClick={(event) => { if (event.currentTarget === event.target) onClose(); }}
    >
      <FloatingModalCard storageKey="command-catalog.window" className="utility-modal-card catalog-review-card">
        {(floating) => (<>
          <div className="source-modal-header" {...floating.dragHandleProps}>
            <div>
              <h2>命令目录识别结果</h2>
              <p title={sourceTitle}>{sourceTitle}</p>
            </div>
            <button type="button" className="icon-button" onClick={onClose} title="关闭" aria-label="关闭识别结果">×</button>
          </div>
          <div className="catalog-review-body">
            <div className="catalog-review-counts">
              <span className="tag">待审阅 {pendingCount}</span>
              <span className="tag">未通过 {rejectedCount}</span>
              <span className="tag">已确认 {appliedCount}</span>
              <span className="tag">已跳过 {dismissedCount}</span>
            </div>
            <div className="chat-tabs catalog-review-tabs" role="tablist">
              {TAB_ORDER.map((key) => (
                <button
                  key={key}
                  type="button"
                  role="tab"
                  aria-selected={tab === key}
                  className={`chat-tab ${tab === key ? "active" : ""}`}
                  // apply/dismiss 在飞时不许切页签:runApply/runDismiss 结束时会用它
                  // 开始那一刻捕获的 tab(闭包)重拉列表,若这期间页签已经切走,重拉
                  // 会用旧 tab 的结果覆盖新页签正在看的内容——串台。切页签本身不是
                  // 长任务,但这里的忙碌位是为了保护另一个正在飞的长任务。
                  disabled={applying || dismissing}
                  onClick={() => setTab(key)}
                >
                  {TAB_LABEL[key]}（{counts[key] ?? 0}）
                </button>
              ))}
            </div>
            {error && <p className="catalog-status catalog-status--danger" role="alert">{error}</p>}
            {tab === "candidate" && canEdit && items.length > 0 && (
              <label className="catalog-select-all">
                <input type="checkbox" checked={allSelected} onChange={toggleAll} />
                {/* 「加载更多」是往同一个视图追加,不是翻到新的一页——叫「全选本页」
                    会让人以为只勾了当前这一屏,「全选已加载」才对得上 pageIds 的
                    真实范围(items 里已经取到的全部)。 */}
                全选已加载（{pageIds.length} 条）
              </label>
            )}
            {/* 有 role="tab" 就必须有它指向的 tabpanel,否则屏幕阅读器读到一个
                指不到任何内容的页签。列表整体就是那个面板。 */}
            <div className="catalog-review-list" role="tabpanel" aria-label={TAB_LABEL[tab]}>
              {items.map((item) => {
                // R5 P2:拦截账目(逐字段落空)与参数说明(超长被截)各自有独立
                // 的溢出上界——`rejections_overflow`/`desc_overflow` 此前落了库
                // 却在审阅面板无处可看。desc_overflow 只可能出现在**通过**的
                // 候选上(只有合并后的候选行才累积它,见后端 `_merge_entry`),
                // 所以只在非「未通过」页签算这句。
                const overflowNote = tab !== "rejected"
                  ? catalogOverflowNote(item.rejections_overflow, item.desc_overflow)
                  : "";
                return (
                  <article className="catalog-item" key={item.id}>
                    <div className="catalog-item-head">
                      {tab === "candidate" && canEdit && (
                        <input
                          type="checkbox"
                          checked={selected.has(item.id)}
                          onChange={() => toggle(item.id)}
                          aria-label={`选择 ${item.command_name || "未命名条目"}`}
                        />
                      )}
                      <span className="catalog-item-name" title={item.command_name}>
                        {item.command_name || "未命名条目"}
                      </span>
                      <span className="tag catalog-item-section" title={catalogSectionLabel(item.section_path)}>
                        {catalogSectionLabel(item.section_path) || "未标注出处"}
                      </span>
                      {tab === "candidate" && (
                        <span className="tag">参数 {item.args.length}</span>
                      )}
                      {item.suspect_related && (
                        <span className="tag catalog-item-suspect" title="这一段提到了这条命令，但标题与用法行里没有它，可能只是被提及">
                          疑似仅被提及
                        </span>
                      )}
                      <button
                        type="button"
                        className="ghost-button catalog-item-toggle"
                        aria-expanded={expanded.has(item.id)}
                        onClick={() => toggleExpanded(item.id)}
                      >
                        {expanded.has(item.id) ? "收起" : "展开"}
                      </button>
                    </div>
                    {item.syntax && <pre className="catalog-syntax">{item.syntax}</pre>}
                    {tab === "rejected" && item.rejections.length > 0 && (
                      <ul className="catalog-reject-list">
                        {item.rejections.map((reject, index) => (
                          <li key={`${reject.field}-${reject.value}-${index}`}>
                            {rejectionText(reject.field, reject.reason, reject.value)}
                          </li>
                        ))}
                      </ul>
                    )}
                    {tab === "dismissed" && item.dismiss_reason && (
                      <ul className="catalog-reject-list">
                        <li>{dismissReasonText(item.dismiss_reason)}</li>
                      </ul>
                    )}
                    {expanded.has(item.id) && (
                      <div className="catalog-item-detail">
                        {item.description && <p className="catalog-item-desc">{item.description}</p>}
                        {item.args.length > 0 && (
                          <table className="catalog-arg-table">
                            <thead>
                              <tr><th>参数</th><th>必填</th><th>默认值</th><th>说明</th></tr>
                            </thead>
                            <tbody>
                              {item.args.map((arg, index) => (
                                <tr key={`${arg.name}-${index}`}>
                                  <td><code>{arg.name}</code></td>
                                  <td>{arg.required ? "是" : "否"}</td>
                                  <td>{arg.default || "—"}</td>
                                  <td>{arg.desc || "—"}</td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        )}
                        {/* 一条**通过**的候选也会带逐字段的落空:某个参数写法对不上
                            原文、或者这一批里问了却没回来(后端 R2 修复新记的账)。
                            上面那份列表只在「未通过」页签渲染,所以在待审阅页签里
                            这些记录此前无处可看——而它们恰恰是「这一行虽然能用,但
                            少了什么」的唯一线索,审阅时最该看到。
                            语气降一档:整条被拦下才是 --danger,一条能用的候选掉了
                            个别字段是提醒,不是错误。 */}
                        {tab !== "rejected" && item.rejections.length > 0 && (
                          <>
                            <h3 className="catalog-detail-title">未采纳的内容</h3>
                            <ul className="catalog-reject-list catalog-reject-list--notice">
                              {item.rejections.map((reject, index) => (
                                <li key={`d-${reject.field}-${reject.value}-${index}`}>
                                  {rejectionText(reject.field, reject.reason, reject.value)}
                                </li>
                              ))}
                            </ul>
                          </>
                        )}
                        {/* R5 P2:`rejections_overflow`/`desc_overflow` 是有界记账,
                            不是静默丢弃——独立于上面那份列表是否非空(一条候选可以
                            零逐字段落空、却仍有参数说明因聚合预算被截)。 */}
                        {overflowNote && (
                          <p className="catalog-item-note">{overflowNote}</p>
                        )}
                        {/* 示例是**刻意不接地**的:散文和整行调用无法逐字比对原文
                            (与「说明」同因),所以它不像命令名/参数/语法那样有校验
                            背书。不渲染就等于把这件事藏起来,渲染而不说明则等于让人
                            把模型自撰的调用当成原文抄来的——所以区块必须带这句小字,
                            且走中性 info 口径:它不是异常,不上红。 */}
                        {item.examples.length > 0 && (
                          <div className="catalog-example-block">
                            {/* 弹窗标题是 h2,这里是它下面的第一层小节标题,所以是
                                h3 —— 跳级会让屏幕阅读器的标题大纲断档。 */}
                            <h3 className="catalog-detail-title">示例</h3>
                            <p className="catalog-item-note">
                              示例为模型生成，未经原文校验
                            </p>
                            {(allExamples.has(item.id)
                              ? item.examples
                              : item.examples.slice(0, EXAMPLE_PREVIEW)
                            ).map((example, index) => (
                              <pre className="catalog-syntax" key={`ex-${index}`}>{example}</pre>
                            ))}
                            {!allExamples.has(item.id)
                              && item.examples.length > EXAMPLE_PREVIEW && (
                              <button
                                type="button"
                                className="ghost-button catalog-item-toggle"
                                onClick={() => showAllExamples(item.id)}
                              >
                                显示全部 {item.examples.length} 条示例
                              </button>
                            )}
                          </div>
                        )}
                        {item.rejections.some((reject) => reject.window) && (
                          <div className="catalog-excerpt-stack">
                            {item.rejections.filter((reject) => reject.window).map((reject, index) => (
                              <pre className="catalog-excerpt" key={`w-${index}`}>{reject.window}</pre>
                            ))}
                          </div>
                        )}
                        {item.excerpt && <pre className="catalog-excerpt">{item.excerpt}</pre>}
                      </div>
                    )}
                  </article>
                );
              })}
              {items.length === 0 && !loading && (
                <p className="catalog-empty">{TAB_EMPTY_TEXT[tab]}</p>
              )}
            </div>
            {hasMore && (
              <button
                type="button"
                className="sort-button catalog-more"
                disabled={loading}
                onClick={() => { void loadPage(tab, cursor, true); }}
              >
                {loading ? "加载中…" : "加载更多"}
              </button>
            )}
            {outcome && (
              <div className="catalog-outcome" role="status">
                <p className="catalog-outcome-headline">{outcome.headline}</p>
                {outcome.conflictNote && <p className="catalog-outcome-note">{outcome.conflictNote}</p>}
                {outcome.remainingNote && <p className="catalog-outcome-note">{outcome.remainingNote}</p>}
                {outcome.canOpenTable && (
                  <button
                    type="button"
                    className="sort-button"
                    onClick={() => { onOpenTable(appliedTableId); onClose(); }}
                  >
                    去看这张表
                  </button>
                )}
              </div>
            )}
          </div>
          {tab === "candidate" && canEdit && (
            <div className="catalog-review-footer">
              <span className="catalog-selected-count">
                已选 {selectedCount} 条
                {overCap && `（单次最多确认 ${CATALOG_APPLY_MAX} 条，将先确认前 ${CATALOG_APPLY_MAX} 条，其余需重新勾选）`}
              </span>
              <button
                type="button"
                className="sort-button"
                disabled={applying || dismissing || selectedCount === 0}
                onClick={applySelected}
              >
                {applying ? "确认中…" : "确认所选"}
              </button>
              {/* R7:显式跳过——「重新识别」被待审阅候选拦住时唯一的放弃出口。视觉
                  次级于「确认所选」(同为 sort-button,但排在其后),不抢确认动作
                  的注意力。 */}
              <button
                type="button"
                className="sort-button"
                disabled={applying || dismissing || selectedCount === 0}
                onClick={dismissSelected}
              >
                {dismissing ? "跳过中…" : "跳过所选"}
              </button>
              <button
                type="button"
                className="new-pill"
                disabled={applying || dismissing || pendingCount === 0}
                onClick={() => { void runApply({ all_pending: true }); }}
              >
                {applying ? "确认中…" : "确认全部待审阅"}
              </button>
              {/* 同上,视觉次级于「确认全部待审阅」(sort-button 对 new-pill)。 */}
              <button
                type="button"
                className="sort-button"
                disabled={applying || dismissing || pendingCount === 0}
                onClick={() => { void runDismiss({ all_pending: true }); }}
              >
                {dismissing ? "跳过中…" : "跳过全部待审阅"}
              </button>
            </div>
          )}
        </>)}
      </FloatingModalCard>
    </section>
  );
}
