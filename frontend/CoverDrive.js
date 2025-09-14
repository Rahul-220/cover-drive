// Uses global React/ReactDOM (UMD) loaded in index.html
const { useState, useEffect, useMemo } = React;

// Same-origin API (FastAPI serves UI + API on :8000)
const API_BASE =
  window?.FRONTEND_API_BASE ||
  ((location.hostname === "localhost" || location.hostname === "127.0.0.1") ? "" : "");

// --- helper: apply/remove `dark` on <html> so body styles flip too ---
function applyTheme(isDark) {
  document.documentElement.classList.toggle("dark", isDark);
}

function toCSV(columns, rows) {
  const escape = (val) => {
    if (val == null) return "";
    const s = String(val);
    if (/[",\n]/.test(s)) return '"' + s.replace(/"/g, '""') + '"';
    return s;
  };
  const head = columns.map(escape).join(",");
  const body = rows.map((r) => r.map(escape).join(",")).join("\n");
  return head + (body ? "\n" + body : "");
}

const Label = ({ children, htmlFor }) =>
  React.createElement("label", { htmlFor, className: "block text-sm font-medium text-gray-700 dark:text-gray-200 mb-1" }, children);

const Spinner = () => (
  React.createElement("svg", { className: "h-5 w-5 animate-spin", viewBox: "0 0 24 24", fill: "none" },
    React.createElement("circle", { className: "opacity-25", cx: 12, cy: 12, r: 10, stroke: "currentColor", strokeWidth: 4 }),
    React.createElement("path", { className: "opacity-75", fill: "currentColor", d: "M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" })
  )
);

const CopyButton = ({ text }) => {
  const [copied, setCopied] = useState(false);
  return (
    React.createElement("button", {
      type: "button",
      onClick: async () => { await navigator.clipboard.writeText(text); setCopied(true); setTimeout(()=>setCopied(false), 1200); },
      className: "ml-2 rounded-md border px-2 py-1 text-xs hover:bg-gray-50 dark:hover:bg-gray-800 dark:border-gray-700",
      title: "Copy",
    }, copied ? "Copied" : "Copy")
  );
};

const SQLBlock = ({ sql }) => (
  React.createElement("div", { className: "rounded-xl border bg-white dark:bg-gray-900 dark:border-gray-700" },
    React.createElement("div", { className: "flex items-center justify-between px-4 py-2 border-b bg-gray-50 dark:bg-gray-800 dark:border-gray-700 rounded-t-xl" },
      React.createElement("div", { className: "text-sm font-semibold text-gray-800 dark:text-gray-100" }, "Generated SQL"),
      React.createElement(CopyButton, { text: sql })
    ),
    React.createElement("pre", { className: "overflow-x-auto p-4 text-sm leading-relaxed text-gray-900 dark:text-gray-100" },
      React.createElement("code", null, sql)
    )
  )
);

const ResultsTable = ({ columns, rows }) => (
  React.createElement("div", { className: "w-full overflow-x-auto rounded-xl border bg-white dark:bg-gray-900 dark:border-gray-700" },
    React.createElement("table", { className: "min-w-full text-sm" },
      React.createElement("thead", { className: "bg-gray-50 dark:bg-gray-800" },
        React.createElement("tr", null,
          columns.map((c, idx) =>
            React.createElement("th", {
              key: idx,
              className: "px-3 py-2 text-left font-semibold text-gray-700 dark:text-gray-200 whitespace-nowrap border-b dark:border-gray-700"
            }, c)
          )
        )
      ),
      React.createElement("tbody", null,
        rows.length === 0
          ? React.createElement("tr", null,
              React.createElement("td", { colSpan: columns.length, className: "px-3 py-6 text-center text-gray-500 dark:text-gray-400" }, "No rows")
            )
          : rows.map((r, ridx) =>
              React.createElement("tr", { key: ridx, className: "odd:bg-white even:bg-gray-50 dark:odd:bg-gray-900 dark:even:bg-gray-800" },
                r.map((cell, cidx) =>
                  React.createElement("td", {
                    key: cidx,
                    className: "px-3 py-2 align-top border-t dark:border-gray-700 whitespace-nowrap text-gray-800 dark:text-gray-100"
                  }, String(cell))
                )
              )
            )
      )
    )
  )
);

function CoverDrive() {
  const [question, setQuestion] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [data, setData] = useState(null);
  const [dark, setDark] = useState(false);

  // Init from system preference AND apply to <html>
  useEffect(() => {
    const prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    setDark(prefersDark);
    applyTheme(prefersDark);
  }, []);

  // Apply on every toggle to <html>
  useEffect(() => { applyTheme(dark); }, [dark]);

  const canAsk = question.trim().length > 0 && !loading;

  const csvBlob = useMemo(() => {
    if (!data) return null;
    const csv = toCSV(data.columns, data.rows);
    return new Blob([csv], { type: "text/csv;charset=utf-8" });
  }, [data]);

  async function ask() {
    setLoading(true);
    setError(null);
    try {
      const body = { question: question.trim() };
      const res = await fetch(`${API_BASE}/nlq`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const msg = await res.text();
        throw new Error(msg || `HTTP ${res.status}`);
      }
      const json = await res.json();
      setData(json);
    } catch (e) {
      setData(null);
      setError(e?.message || "Something went wrong");
    } finally {
      setLoading(false);
    }
  }

  return (
    React.createElement("div", null,
      React.createElement("div", { className: "min-h-screen bg-gradient-to-br from-gray-50 to-gray-100 text-gray-900 dark:from-gray-950 dark:to-gray-900 dark:text-gray-100" },
        React.createElement("div", { className: "mx-auto max-w-3xl px-4 py-8 flex min-h-screen items-center justify-center" },
          React.createElement("div", { className: "w-full" },
            React.createElement("div", { className: "mb-6 flex items-center justify-between" },
              React.createElement("h1", { className: "text-2xl font-bold tracking-tight" }, "CoverDrive"),
              React.createElement("button", {
                onClick: () => setDark(!dark),
                className: "rounded-xl border px-3 py-1.5 text-sm hover:bg-gray-50 dark:hover:bg-gray-800 dark:border-gray-700",
                "aria-label": "Toggle theme"
              }, dark ? "Light" : "Dark", " mode")
            ),

            React.createElement("div", { className: "rounded-2xl border bg-white dark:bg-gray-900 dark:border-gray-700 p-4 shadow-sm" },
              React.createElement(Label, { htmlFor: "q" }, "Your question"),
              React.createElement("textarea", {
                id: "q",
                value: question,
                onChange: (e) => setQuestion(e.target.value),
                placeholder: "e.g., Total runs scored by Virat Kohli between 2023 and 2025 by season",
                rows: 4,
                className: "w-full resize-y rounded-xl border dark:border-gray-700 bg-white dark:bg-gray-950 text-gray-900 dark:text-gray-100 px-3 py-2 outline-none focus:ring-2 focus:ring-gray-200 dark:focus:ring-gray-800"
              }),
              React.createElement("div", { className: "mt-3 flex justify-end" },
                React.createElement("button", {
                  onClick: ask, disabled: !canAsk,
                  className: "inline-flex items-center justify-center gap-2 rounded-xl bg-black px-4 py-2 font-medium text-white shadow-sm hover:bg-gray-800 disabled:cursor-not-allowed disabled:opacity-50",
                  "aria-busy": loading
                }, loading ? React.createElement(Spinner) : "Ask")
              ),
              error && React.createElement("div", { className: "mt-4 rounded-lg border border-red-200 dark:border-red-700 bg-red-50 dark:bg-red-900/30 p-3 text-sm text-red-700 dark:text-red-300" }, error)
            ),

            data && React.createElement("div", { className: "mt-6 space-y-4" },
              React.createElement(SQLBlock, { sql: data.sql }),
              React.createElement("div", { className: "flex items-center justify-between" },
                React.createElement("div", { className: "text-sm text-gray-600 dark:text-gray-300" }, "Tip: sanity-check the SQL and tweak your question to guide the model."),
                csvBlob && React.createElement("a", {
                  download: "coverdrive-results.csv",
                  href: URL.createObjectURL(csvBlob),
                  className: "rounded-md border px-3 py-1.5 text-sm hover:bg-gray-50 dark:hover:bg-gray-800 dark:border-gray-700"
                }, "Download CSV")
              ),
              React.createElement(ResultsTable, { columns: data.columns, rows: data.rows })
            ),

            React.createElement("footer", { className: "mt-10 text-center text-xs text-gray-500 dark:text-gray-400" },
              "Built with ❤️ using React + DuckDB + FastAPI"
            )
          )
        )
      )
    )
  );
}

// Expose to window for index.html to render
window.CoverDrive = CoverDrive;
