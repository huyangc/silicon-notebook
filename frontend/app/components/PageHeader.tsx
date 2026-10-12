import { ArrowLeft } from "lucide-react";
import "./page-header.css";

/**
 * 独立页(管理页、许愿墙、Agent 接入等)的顶栏。与主界面同一套外观:淡底 `.topbar` +
 * SN 徽标 + 「silicon-notebook / 页面名」两行品牌,返回动作用主界面同一枚
 * `.back-home-button` 药丸——此前这里是白底 + 蓝色文字链,和主界面像两个产品。
 * 独立页没有账户菜单,右侧那一格就留给返回按钮。
 */
export function PageHeader({ title }: { title: string }) {
  return (
    <header className="topbar page-header">
      <div className="brand">
        <a className="brand-mark page-header-mark" href="/" title="返回主页" aria-label="返回主页">SN</a>
        <div>
          <div className="brand-title">silicon-notebook</div>
          <div className="brand-subtitle page-header-title">{title}</div>
        </div>
      </div>
      <a className="back-home-button" href="/">
        <ArrowLeft size={16} />
        <span>返回主页</span>
      </a>
    </header>
  );
}
