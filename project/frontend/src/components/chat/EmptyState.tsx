const SUGGESTIONS = [
  { tag: "排行", text: "上个月销量最高的 10 个商品是哪些？" },
  { tag: "趋势", text: "最近 30 天 GMV 的趋势如何？" },
  { tag: "投放", text: "最近 30 天各渠道的 ROI 是多少？" },
  { tag: "物流", text: "上个月各承运商的平均签收时长是多少小时？" },
];

interface Props {
  asOfDate?: string;
  onPick: (text: string) => void;
}

export default function EmptyState({ asOfDate, onPick }: Props) {
  return (
    <div className="empty">
      <h1>今天想看哪笔账？</h1>
      <p>用中文提问，我来查数、画图，并说明口径。{asOfDate && `数据截止 ${asOfDate}。`}</p>
      <div className="suggestions">
        {SUGGESTIONS.map((item) => (
          <button key={item.tag} type="button" className="suggestion" onClick={() => onPick(item.text)}>
            <span className="suggestion-tag">{item.tag}</span>
            <span>{item.text}</span>
          </button>
        ))}
      </div>
    </div>
  );
}
