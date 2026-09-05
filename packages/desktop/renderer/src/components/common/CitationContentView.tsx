import { useState } from 'react';

/** 引用对照表条目（与后端 citation_ledger/{n: {chunk_id, source, excerpt}} 同构）。 */
export interface LedgerEntry {
  chunk_id?: string;
  source?: string;
  excerpt?: string;
}

export interface CitationLedger {
  [n: string]: LedgerEntry;
}

/** 把【n】锚点渲染为可点按钮，点击展示对应来源（G-2：正文页【n】点查来源联动）。 */
export function CitationContentView({
  content,
  citationLedger,
  maxHeight,
}: {
  content: string;
  citationLedger?: CitationLedger | null;
  maxHeight?: string;
}) {
  const [activeN, setActiveN] = useState<string | null>(null);
  if (!citationLedger || Object.keys(citationLedger).length === 0 || !content) {
    return <div style={{ whiteSpace: 'pre-wrap' }}>{content}</div>;
  }
  // 正文按【n】切分为文本段与锚点按钮（确定性 split，不改变原文）
  const parts = content.split(/(【\d{1,3}】)/);
  const entry = activeN ? citationLedger[activeN] : undefined;
  return (
    <div>
      <div style={{ fontSize: '14px', lineHeight: '1.8', whiteSpace: 'pre-wrap' }}>
        {parts.map((part, i) => {
          const m = part.match(/^【(\d{1,3})】$/);
          if (!m) return <span key={i}>{part}</span>;
          const n = m[1];
          const known = !!citationLedger[n];
          return (
            <button
              key={i}
              onClick={() => setActiveN(n)}
              title={known ? `查看来源【${n}】` : `【${n}】不在引用对照表内`}
              style={{
                border: activeN === n ? '1px solid #2563eb' : '1px solid var(--color-border)',
                background: known ? (activeN === n ? '#dbeafe' : '#eff6ff') : '#fef2f2',
                color: known ? '#1d4ed8' : '#b91c1c',
                borderRadius: '4px',
                padding: '0 3px',
                margin: '0 1px',
                cursor: 'pointer',
                fontSize: '12px',
              }}
            >
              {part}
            </button>
          );
        })}
      </div>
      <div style={{ marginTop: '10px', borderTop: '1px solid var(--color-border)', paddingTop: '8px' }}>
        <div style={{ fontSize: '12px', color: 'var(--color-text-secondary)', marginBottom: '6px' }}>
          引用对照表（{Object.keys(citationLedger).length} 条来源，点击正文【n】可定位）
        </div>
        <div style={{ maxHeight: maxHeight || '180px', overflow: 'auto' }}>
          {Object.entries(citationLedger).map(([n, e]) => (
            <div
              key={n}
              ref={(el) => {
                if (activeN === n && el) el.scrollIntoView({ block: 'nearest' });
              }}
              style={{
                padding: '6px 8px',
                marginBottom: '4px',
                borderRadius: '6px',
                background: activeN === n ? '#dbeafe' : '#f8fafc',
                border: activeN === n ? '1px solid #2563eb' : '1px solid var(--color-border)',
                fontSize: '12px',
              }}
            >
              <div style={{ color: '#1d4ed8', fontWeight: 600 }}>
                【{n}】{e.source || '（来源未标注）'} · chunk_id={e.chunk_id || '-'}
              </div>
              <div style={{ color: 'var(--color-text-secondary)', whiteSpace: 'pre-wrap' }}>{e.excerpt || ''}</div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
