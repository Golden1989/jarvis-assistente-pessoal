# Prompt caching - evaluation (not applied)

Status: **evaluated, deliberately not implemented yet.** Queued after the
memory-injection guard (`pending_note`) and the "serious mode" work.
Evaluated 2026-09-19 against the official prompt-caching docs
(`platform.claude.com/docs/en/build-with-claude/prompt-caching`) and the
`claude-api` skill's caching guide. Token counts are **estimates** from
character counts (no API call was made); confirm with the free
`count_tokens` endpoint or the `usage` fields before relying on them.

## Facts from the docs

| Item | Value |
|---|---|
| Model | `claude-sonnet-5` |
| Minimum cacheable prefix | **1,024 tokens** (tools + system + messages up to the breakpoint all count). Below it, caching silently does nothing - no error. |
| Base input price (from `INPUT_PRICE`) | $2.00 / MTok |
| Cache write, 5-minute TTL | 1.25x -> $2.50 / MTok |
| Cache write, 1-hour TTL | 2x -> $4.00 / MTok |
| Cache read (either TTL) | 0.1x -> $0.20 / MTok |
| TTL refresh | Every read refreshes the entry at no cost; lifetime is measured from the *start* of the request. |
| Break-even | 5 min TTL: 2 requests inside the window (1.25 + 0.1 < 2). 1 h TTL: 3 requests. |
| Breakpoints | Max 4 explicit; prefix order is tools -> system -> messages; any byte change invalidates everything after it. |
| Verification | `usage.cache_creation_input_tokens`, `usage.cache_read_input_tokens`; `usage.input_tokens` is only the uncached tail after the last breakpoint. |
| Side effect | With caching on, server tools (`web_search`) insert their own 5-minute cache write after results. Unmarked, expected, but a possible small surcharge. |

## What the prompt looks like today (estimates)

| Part | Size |
|---|---|
| Fixed part (base + personality + profile + people) | ~7 KB text -> ~1,700-2,000 tokens |
| Tools block (9 tools, JSON) | ~4.4 KB -> ~1,100-1,300 tokens, plus ~300-700 of hidden tool-use overhead (unverified) |
| **Cacheable prefix (tools + fixed part)** | **~3,300 tokens (range 2,900-4,000)** - about 3x the minimum |
| Notes (`notes.txt`) | Variable; grows with `remember`/LOG. Changes on every write, so it must sit *after* the breakpoint. |

## Cost model (prefix only, P = 3,300 tokens)

Per request: uncached $0.0066 - cache read $0.00066 - 5m write $0.00825 - 1h write $0.0132.

| Scenario | No cache | 5 min TTL | 1 h TTL |
|---|---|---|---|
| 1 isolated question, 1 request | $0.0066 | $0.00825 (+25%, costs more) | $0.0132 (+100%) |
| 1 isolated question that triggers a tool (2 requests) | $0.0132 | $0.0089 (-32%) | $0.0139 (+5%) |
| 8 questions in a row, gaps < 5 min | $0.0528 | $0.0129 (-76%) | $0.0178 (-66%) |

Every tool call inside one message is a separate API request, so a single
question often has 2+ requests and already benefits. The 1-hour TTL only wins
if conversations resume 5-60 minutes apart, and even then by fractions of a cent.

## History (10-exchange sliding window)

Once the history exceeds `MAX_HISTORY_EXCHANGES`, its start moves by one
exchange per message, so the messages prefix never repeats: **only tools +
system are cacheable, not the history.** Cutting in blocks (grow to ~15
exchanges, then cut back to 10) would make the history cacheable between cuts;
rough estimate is ~50% off the history cost during long conversations, but it
adds complexity and each cut rewrites everything. Not recommended until real
usage numbers exist.

## Recommendation (minimal version, when we get to it)

1. 5-minute TTL only.
2. One explicit breakpoint on the last block of the fixed system part
   (covers tools + fixed prompt).
3. Notes as a second `system` block **without** cache_control; omit the block
   when notes are empty (the API rejects empty text blocks).
4. Do not cache the history.
5. **Cost tracking must change with it:** `usage.input_tokens` no longer
   includes cached tokens, so `_session_cost` would under-report. Either fold
   the cache fields into an "equivalent input tokens"
   (`input + 1.25*creation + 0.1*read`) or expose the three fields separately.
6. Log `[cache] read=... write=... uncached=...` per request to verify.
7. Verify with two identical requests in a row: the second must show
   `cache_read_input_tokens > 0` (costs roughly $0.02 of real API use).

Not worth it if nearly all conversations are a single question with no tool
call (the cache then costs ~$0.0016 more per conversation). The `[cache]` log
would show the real pattern after a few days of use.

## Code changes required (for later)

- `SystemPromptProvider`: a method returning the two system blocks.
- `call_claude`: pass `system=` as a list; add cache usage fields to the totals.
- Offline tests: block assembly (with/without notes), equivalent-token cost
  from a fake `usage`, byte-identical prefix across calls.
