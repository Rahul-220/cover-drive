# Next Sprint TODOs

Date: 2026-02-01

Scope: Reliability & Rate Limits, Observability, Testing, UX

## 1) Reliability & Rate Limits
- Add retry with exponential backoff for Gemini 429 responses.
- Add short-term in-memory cache for identical prompts (reduce duplicate calls during testing).
- Add UI debounce / disable double submits while a request is in-flight.

## 4) Observability
- Add structured logs for LLM JSON parsing and error categories.
- Log SQL execution time and include correlation IDs in responses.
- Surface correlation ID in client logs to trace requests end-to-end.

## 5) Testing
- Build a small regression test suite (2021–2025 queries).
- Add a lightweight smoke test script to run 10–20 queries and report failures.

## 6) UX
- Show resolved entity in the UI after clarification selection.
- Improve error messages (rate-limit, timeout, no results) with user-friendly hints.

