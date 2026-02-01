// Uses global React/ReactDOM (UMD) loaded in index.html
const { useState, useEffect, useMemo } = React;

// Same-origin API (FastAPI serves UI + API)
const API_BASE =
  window?.FRONTEND_API_BASE ||
  (location.hostname === "localhost" || location.hostname === "127.0.0.1"
    ? "http://127.0.0.1:8000" // your local Uvicorn backend
    : "https://her9vflmzj.execute-api.us-west-2.amazonaws.com"); // your API Gateway

// --- helper: apply/remove `dark` on <html> so body styles flip too ---
function applyTheme(isDark) { document.documentElement.classList.toggle("dark", isDark); }

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

// --- Pretty column names for users ---
function titleCase(s) { return s.replace(/\b\w/g, (m) => m.toUpperCase()); }
function humanizeIdent(id) { return titleCase(id.replace(/_/g, " ")); }
function smartPrettyName(name) {
  if (!name) return name;
  const n = String(name).trim();
  const map = { "count(*)": "Count", "season_year": "Season" };
  if (map[n]) return map[n];
  let m = n.match(/^sum\s*\(\s*([^)]+)\s*\)$/i);
  if (m) return `Total ${humanizeIdent(m[1])}`;
  m = n.match(/^avg\s*\(\s*([^)]+)\s*\)$/i);
  if (m) return `Average ${humanizeIdent(m[1])}`;
  m = n.match(/^min\s*\(\s*([^)]+)\s*\)$/i);
  if (m) return `Minimum ${humanizeIdent(m[1])}`;
  m = n.match(/^max\s*\(\s*([^)]+)\s*\)$/i);
  if (m) return `Maximum ${humanizeIdent(m[1])}`;
  if (/extract\s*\(\s*year\s+from/i.test(n)) return "Year";
  if (/^\w+(?:\.\w+)*$/.test(n)) return humanizeIdent(n.split(".").pop());
  return humanizeIdent(n.replace(/\s+/g, " ").replace(/\(.*?\)/g, "").trim());
}

const Label = ({ children, htmlFor }) =>
  React.createElement("label", { htmlFor, className: "block text-sm font-medium text-gray-700 dark:text-gray-200 mb-1" }, children);

const Spinner = () => (
  React.createElement("svg", { className: "h-5 w-5 animate-spin", viewBox: "0 0 24 24", fill: "none" },
    React.createElement("circle", { className: "opacity-25", cx: 12, cy: 12, r: 10, stroke: "currentColor", strokeWidth: 4 }),
    React.createElement("path", { className: "opacity-75", fill: "currentColor", d: "M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" })
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
            }, smartPrettyName(c))
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

// Safely parse JSON to avoid blank page on non-JSON errors
async function safeJson(res) {
  const text = await res.text();
  try { return JSON.parse(text); } catch (_) { return { _raw: text }; }
}

function CoverDrive() {
  const [question, setQuestion] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [data, setData] = useState(null); // { columns, rows, notice?, meta?, sql? }
  const [dark, setDark] = useState(false);
  const [dev, setDev] = useState(false); // NEW: developer mode to show SQL
  const [clarify, setClarify] = useState(null);

  useEffect(() => {
    const prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    setDark(prefersDark); applyTheme(prefersDark);
  }, []);
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
    console.log("[ASK] sending:", { question: question.trim(), dev });
    try {
      const body = { question: question.trim() };
      const res = await fetch(`${API_BASE}/nlq`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Debug": dev ? "1" : "0",
        },
        body: JSON.stringify(body),
      });

      const json = await safeJson(res);

      if (!res.ok) {
        console.error("[ASK] !res.ok", json);
        throw new Error(json?.message || (json?._raw ? String(json._raw).slice(0, 200) : `HTTP ${res.status}`));
      }

      // NEW: handle clarify
      if (json?.status === "needs_clarification") {
        console.log("[ASK] needs_clarification branch:", json);
        setData(null);
        setClarify({
          entity: json.entity,           // e.g., "player"
          mention: json.mention,         // e.g., "Pandya"
          options: json.options || [],   // e.g., ["Hardik Pandya", "Krunal Pandya"]
          originalQuestion: question.trim(),
        });
        return;
      }

      // Handle normal success (status: "ok")
      if (json?.status === "ok") {
        console.log("[ASK] ok branch hit:", json);
        setClarify(null);
        setData({
          columns: json.columns || [],
          rows: json.rows || [],
          notice: json.notice || null,
          meta: json.meta || {},
          sql: json.sql || null,
        });
        return;
      }

      // Handle backend error envelope
      if (json?.status === "error") {
        throw new Error(json?.message || "We’re working on this type of query. Try rephrasing or narrowing it.");
      }

      // Anything else is unexpected
      console.warn("[ASK] unexpected shape:", json);
      throw new Error("Unexpected response. Please try again.");
    } catch (e) {
      setData(null);
      setError(e?.message || "Something went wrong");
    } finally {
      setLoading(false);
    }
  }


  async function handleClarifiedChoice(choice) {
    if (!clarify) return;

    // Our clarify state is either { question, payload: { type, phrase, choices } }
    // or (fallback) flat { entity, mention, options, originalQuestion }.
    const entity =
      (clarify.payload && (clarify.payload.type || "").toLowerCase()) ||
      (clarify.entity || "player");
    const mention =
      (clarify.payload && clarify.payload.phrase) ||
      clarify.mention ||
      "";
    const originalQuestion =
      clarify.question ||
      clarify.originalQuestion ||
      question;

    setClarify(null);
    setLoading(true);
    setError(null);

    try {
      const res = await fetch(`${API_BASE}/nlq`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Debug": dev ? "1" : "0",
        },
        body: JSON.stringify({
          question: originalQuestion,
          selections: { [entity]: choice }, // 👈 key bit
          mention,                          // 👈 tells server what word was ambiguous
        }),
      });

      const json = await safeJson(res);
      if (!res.ok) throw new Error(json?.message || "Server error");

      if (json?.status === "ok") {
        setData({
          columns: json.columns || [],
          rows: json.rows || [],
          notice: json.notice || null,
          meta: json.meta || {},
          sql: json.sql || null,
        });
      } else if (json?.status === "needs_clarification") {
        // Edge case: still ambiguous → normalize into our {question, payload} shape
        setClarify({
          question: originalQuestion,
          payload: {
            type: json.entity,
            phrase: json.mention,
            choices: json.options || [],
          },
        });
      } else {
        console.warn("[CLARIFY] unexpected shape:", json);
        throw new Error(json?.message || "Unexpected error");
      }
    } catch (e) {
      console.error("[CLARIFY] catch:", e);
      setData(null);
      setError(e?.message || "Something went wrong");
    } finally {
      setLoading(false);
    }
  }



  const heading = useMemo(() => {
    if (!data || !data.columns || data.columns.length === 0) return "Results";
    if (data.columns.length === 1) return smartPrettyName(data.columns[0]);
    return "Results";
  }, [data]);

  const ClarifyPrompt = ({ entity, mention, options, onChoose }) => {
    return (
      React.createElement("div", { className: "mt-6 rounded-xl border bg-yellow-50 dark:bg-yellow-900/30 dark:border-yellow-800 p-4" },
        React.createElement("p", { className: "mb-2 font-medium text-yellow-800 dark:text-yellow-100" },
          `Which ${entity} did you mean by "${mention}"?`
        ),
        React.createElement("div", { className: "flex flex-wrap gap-2" },
          (options || []).map((opt, idx) =>
            React.createElement("button", {
              key: idx,
              onClick: () => onChoose(opt),
              className: "px-3 py-1.5 rounded-md border bg-white hover:bg-yellow-100 dark:bg-gray-950 dark:hover:bg-yellow-900 text-sm dark:text-yellow-100"
            }, opt)
          )
        )
      )
    );
  };



  return (
    React.createElement("div", null,
      React.createElement("div", { className: "min-h-screen bg-gradient-to-br from-gray-50 to-gray-100 text-gray-900 dark:from-gray-950 dark:to-gray-900 dark:text-gray-100" },
        React.createElement("div", { className: "mx-auto max-w-3xl px-4 py-8 flex min-h-screen items-center justify-center" },
          React.createElement("div", { className: "w-full" },

            // Header with DEV + Dark toggles
            React.createElement("div", { className: "mb-6 flex items-center justify-between" },
              React.createElement("h1", { className: "text-2xl font-bold tracking-tight" }, "CoverDrive"),
              React.createElement("div", { className: "flex items-center gap-2" },
                React.createElement("button", {
                  onClick: () => setDev(!dev),
                  className: `rounded-xl border px-3 py-1.5 text-sm dark:border-gray-700 ${dev ? "bg-black text-white hover:bg-gray-800" : "hover:bg-gray-50 dark:hover:bg-gray-800"}`,
                  "aria-label": "Toggle developer mode"
                }, `DEV: ${dev ? "ON" : "OFF"}`),
                React.createElement("button", {
                  onClick: () => setDark(!dark),
                  className: "rounded-xl border px-3 py-1.5 text-sm hover:bg-gray-50 dark:hover:bg-gray-800 dark:border-gray-700",
                  "aria-label": "Toggle theme"
                }, dark ? "Light" : "Dark", " mode")
              )
            ),

            // Ask card
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

            // NEW: Clarification prompt
            clarify && React.createElement("div", { className: "mt-6" },
              React.createElement(ClarifyPrompt, {
                entity: clarify.entity,
                mention: clarify.mention,
                options: clarify.options,
                onChoose: handleClarifiedChoice
              })
            ),

            // Results + SQL
            data && React.createElement("div", { className: "mt-6 space-y-3" },
              React.createElement("div", { className: "flex items-center justify-between" },
                React.createElement("h2", { className: "text-lg font-semibold text-gray-800 dark:text-gray-100" }, heading),
                csvBlob && React.createElement("a", {
                  download: "coverdrive-results.csv",
                  href: URL.createObjectURL(csvBlob),
                  className: "rounded-md border px-3 py-1.5 text-sm hover:bg-gray-50 dark:hover:bg-gray-800 dark:border-gray-700"
                }, "Download CSV")
              ),

              // NEW: SQL viewer when DEV is ON and backend provided SQL
              (dev && data?.sql) && React.createElement("div", { className: "rounded-xl border dark:border-gray-700 bg-gray-50 dark:bg-gray-900 p-3" },
                React.createElement("div", { className: "mb-2 flex items-center justify-between" },
                  React.createElement("span", { className: "text-sm font-semibold text-gray-700 dark:text-gray-200" }, "Generated SQL"),
                  React.createElement("button", {
                    onClick: async () => { try { await navigator.clipboard.writeText(data.sql); } catch(_){} },
                    className: "text-xs rounded-md border px-2 py-1 hover:bg-gray-100 dark:hover:bg-gray-800 dark:border-gray-700"
                  }, "Copy")
                ),
                React.createElement("pre", { className: "overflow-x-auto text-xs leading-relaxed text-gray-900 dark:text-gray-100" },
                  React.createElement("code", null, data.sql)
                )
              ),

              (data.rows.length === 0) && React.createElement("div", {
                className: "rounded-lg border border-blue-200 dark:border-blue-900 bg-blue-50/70 dark:bg-blue-900/30 p-3 text-sm text-blue-800 dark:text-blue-200"
              }, data.notice || "No results found. Try broadening the query or checking player/team spelling."),

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
