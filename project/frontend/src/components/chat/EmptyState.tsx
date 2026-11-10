import type { SuggestedQuestion } from "../../types";

interface Props {
  asOfDate?: string;
  suggestions: SuggestedQuestion[];
  onPick: (text: string) => void;
}

export default function EmptyState({ asOfDate, suggestions, onPick }: Props) {
  return (
    <div className="empty">
      <h1>今天想看哪笔账？</h1>
      <p>用中文提问，我来查数、画图，并说明口径。{asOfDate && `数据截止 ${asOfDate}。`}</p>
      {suggestions.length > 0 && (
        <div className="suggestions">
          {suggestions.map((item) => (
            <button key={item.text} type="button" className="suggestion" onClick={() => onPick(item.text)}>
              <span className="suggestion-tag">{item.tag}</span>
              <span>{item.text}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
