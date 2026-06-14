export function SignalAlert({
  title,
  detail,
  severity = "system",
}: {
  title: string;
  detail: string;
  severity?: "system" | "risk" | "profit";
}) {
  const color =
    severity === "risk"
      ? "border-risk text-risk"
      : severity === "profit"
        ? "border-profit text-profit"
        : "border-system text-system";
  return (
    <div className={`rounded-lg border bg-slate-900/80 p-3 ${color}`}>
      <div className="font-medium">{title}</div>
      <div className="mt-1 text-sm text-slate-400">{detail}</div>
    </div>
  );
}
