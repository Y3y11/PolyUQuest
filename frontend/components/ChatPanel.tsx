"use client";

import { useId, useState } from "react";
import { motion } from "framer-motion";
import { Check, Copy } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const handle = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // clipboard write blocked — silently no-op; nothing useful to show the user
    }
  };
  return (
    <button
      type="button"
      onClick={handle}
      className="opacity-0 group-hover:opacity-100 focus:opacity-100 transition-opacity inline-flex items-center gap-1 text-[11px] font-mono text-text-muted hover:text-text-main px-1.5 py-0.5 rounded border border-border bg-surface hover:bg-surface-alt"
      aria-label={copied ? "Copied" : "Copy answer"}
      title={copied ? "Copied" : "Copy answer"}
    >
      {copied ? <Check size={11} /> : <Copy size={11} />}
      <span>{copied ? "Copied" : "Copy"}</span>
    </button>
  );
}

function CitationBadge({
  number,
  onClick,
  onHoverEnter,
  onHoverLeave,
}: {
  number: string;
  onClick?: (n: number) => void;
  onHoverEnter?: (n: number) => void;
  onHoverLeave?: () => void;
}) {
  const n = parseInt(number, 10);
  return (
    <span
      className="citation-ref"
      data-cite={number}
      onClick={() => onClick?.(n)}
      onPointerEnter={() => onHoverEnter?.(n)}
      onPointerLeave={() => onHoverLeave?.()}
    >
      {number}
    </span>
  );
}

export default function ChatPanel({
  messages,
  isLoading,
  isStreaming = false,
  onCitationClick,
  onCitationHover,
}: {
  messages: ChatMessage[];
  isLoading: boolean;
  isStreaming?: boolean;
  onCitationClick?: (idx: number) => void;
  onCitationHover?: (idx: number | null) => void;
}) {
  const baseId = useId();

  const renderContent = (content: string, messageIndex: number) => {
    const citationProps = {
      onClick: onCitationClick,
      onHoverEnter: (n: number) => onCitationHover?.(n),
      onHoverLeave: () => onCitationHover?.(null),
    };
    const keyPrefix = `${baseId}-m${messageIndex}`;
    return (
      <div className="prose-answer">
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          components={{
            p: ({ children }) => (
              <p>{processCitations(children, keyPrefix, citationProps)}</p>
            ),
            li: ({ children }) => (
              <li>{processCitations(children, keyPrefix, citationProps)}</li>
            ),
          }}
        >
          {content}
        </ReactMarkdown>
      </div>
    );
  };

  return (
    <div className="flex flex-col gap-4 py-4">
      {messages.map((msg, i) => {
        const isAssistant = msg.role === "assistant";
        const isLast = i === messages.length - 1;
        const hasContent = (msg.content || "").trim().length > 0;
        // Only mount the Copy button on completed assistant messages.
        // Toggling a sibling on the first streamed token next to ReactMarkdown
        // causes React reconciliation failures (insertBefore). Wait until
        // streaming is finished before introducing the new node.
        const showCopy =
          isAssistant && hasContent && !(isStreaming && isLast);
        return (
          <motion.div
            key={i}
            initial={{ opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: i * 0.05 }}
            className={`flex ${isAssistant ? "justify-start" : "justify-end"}`}
          >
            <div
              className={
                isAssistant
                  ? "group max-w-[90%] px-4 py-3 text-sm leading-relaxed"
                  : "max-w-[80%] px-4 py-3 rounded-2xl rounded-br-md bg-primary text-text-inverse text-sm"
              }
            >
              {isAssistant ? renderContent(msg.content, i) : msg.content}
              {showCopy && (
                <div className="mt-2 flex justify-end">
                  <CopyButton text={msg.content} />
                </div>
              )}
            </div>
          </motion.div>
        );
      })}
      {isLoading && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          className="flex items-center gap-2 text-text-muted text-sm px-4"
        >
          <span className="inline-block w-2 h-2 rounded-full bg-primary animate-pulse" />
          <span className="inline-block w-2 h-2 rounded-full bg-primary animate-pulse" style={{ animationDelay: "0.15s" }} />
          <span className="inline-block w-2 h-2 rounded-full bg-primary animate-pulse" style={{ animationDelay: "0.3s" }} />
          <span className="ml-2">Searching knowledge graph...</span>
        </motion.div>
      )}
    </div>
  );
}

interface CitationProps {
  onClick?: (n: number) => void;
  onHoverEnter: (n: number) => void;
  onHoverLeave: () => void;
}

function processCitations(
  children: React.ReactNode,
  keyPrefix: string,
  props: CitationProps
): React.ReactNode {
  if (!Array.isArray(children)) {
    if (typeof children === "string") {
      return splitCitations(children, keyPrefix, props);
    }
    return children;
  }

  return children.flatMap((child, idx) => {
    if (typeof child === "string") {
      return splitCitations(child, `${keyPrefix}-${idx}`, props);
    }
    return child;
  });
}

function splitCitations(
  text: string,
  keyPrefix: string,
  props: CitationProps
): React.ReactNode[] {
  const parts: React.ReactNode[] = [];
  const regex = /\[(\d+)\]/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  let citeOrdinal = 0;

  while ((match = regex.exec(text)) !== null) {
    if (match.index > lastIndex) {
      parts.push(text.slice(lastIndex, match.index));
    }
    parts.push(
      <CitationBadge
        key={`${keyPrefix}-c${citeOrdinal++}`}
        number={match[1]}
        onClick={props.onClick}
        onHoverEnter={props.onHoverEnter}
        onHoverLeave={props.onHoverLeave}
      />
    );
    lastIndex = regex.lastIndex;
  }

  if (lastIndex < text.length) {
    parts.push(text.slice(lastIndex));
  }

  return parts.length > 0 ? parts : [text];
}
