import React from "react";
import clsx from "clsx";

export default function PriceTag({ value, pct, size = "md" }) {
  const up = value >= 0;
  return (
    <span className={clsx("font-mono", up ? "text-terminal-green" : "text-terminal-red")}>
      {up ? "▲" : "▼"} {Math.abs(value).toFixed(2)}
      {pct !== undefined && (
        <span className="ml-1 text-xs opacity-80">
          ({pct >= 0 ? "+" : ""}{pct.toFixed(2)}%)
        </span>
      )}
    </span>
  );
}
