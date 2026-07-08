import "./page-header.css";

export function PageHeader({ title }: { title: string }) {
  return (
    <div className="page-header">
      <a className="page-header-back" href="/">← 返回主页</a>
      <span className="page-header-title">{title}</span>
    </div>
  );
}
