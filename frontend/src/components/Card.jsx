import React from "react";
import clsx from "clsx";

export default function Card({ title, children, className }) {
  return (
    <div className={clsx("bg-terminal-surface border border-terminal-border rounded", className)}>
      {title && (
        <div className="px-4 py-2 border-b border-terminal-border text-terminal-accent text-xs tracking-widest font-bold">
          {title}
        </div>
      )}
      <div className="p-4">{children}</div>
    </div>
  );
}
