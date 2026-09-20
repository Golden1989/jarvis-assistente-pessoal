# Serious mode - notes to remember before adding another model

Written 2026-09-19 while planning the serious mode (`claude-opus-5`, with
`claude-sonnet-5` as the normal mode and as the refusal fallback). Sources: the
official docs for thinking, preserved thinking, effort and refusals.

## Thinking-block verification: Fable 5.1 and later

Starting with Claude Fable 5.1, the API checks the `signature` of every
`thinking` / `redacted_thinking` block that comes back in a request, for two
things:

1. **Model check (applies to every account).** A model reads its own thinking
   blocks and those of *earlier* models, never those of a newer one. A block the
   current model cannot read is dropped from that request, silently and unbilled.
   The API never edits our `messages`, so the block stays in our history and is
   readable again if the conversation goes back to the newer model. Switching
   *down* (for example after a refusal fallback) loses that reasoning for the
   one request; switching *up* keeps it.
2. **Prefix check.** A block is valid only while the top-level `system`, the
   `tools` and every message before it are unchanged from when the block was
   produced. If anything earlier differs, that block and every later thinking
   block are invalid: the API answers 400 (`Invalid signature in thinking
   block ... bound to a different conversation`) or, with the beta header
   `thinking-binding-controls-2026-08-01` and
   `thinking.block_binding.prefix_mismatch_behavior: "drop_block"`, drops them.
   Enforced by default for accounts created on or after 2026-08-31; older
   accounts only when the setting is sent. The docs say **later models will
   enforce it for all accounts**.

As documented, this is not enforced for `claude-opus-5` / `claude-sonnet-5`
(the check starts at Fable 5.1), so today's code is fine.

## What in Jarvis edits the prefix (would break a Fable mode)

- **`trim_history` (10-exchange sliding window):** the start of `messages`
  moves by one exchange per message, so every earlier thinking block sits
  after a different prefix than when it was produced.
- **`SystemPromptProvider` (notes.txt reload):** `system` changes whenever
  `remember` / `update_notes` rewrites the notes.
- Any future per-request text injected into `system`, or a changing `tools`
  list.

## Before adding a Fable (or any later) mode

1. Make the history **append-only**: send every assistant turn back exactly as
   received and add new messages only at the end.
2. Replace the edits above with API features the docs list for this purpose:
   server-side compaction / context editing instead of the client-side sliding
   window; a mid-conversation system message (not on Sonnet 5) instead of
   editing `system`; `tool_addition` / `tool_removal` blocks instead of
   changing tools.
3. Or opt into `block_binding.prefix_mismatch_behavior: "drop_block"` (beta
   header above), store the choice with the session, and count responses whose
   `input_transformations` contains `prefix_binding_mismatch` so a silent
   regression shows up.
4. Fable 5.1 always thinks (`thinking: disabled` is a 400) and is priced at
   $10 / $50 per MTok, so it needs its own row in the `MODES` table and its own
   cost alert.

## Other facts worth keeping

- Refusals arrive as HTTP 200 with `stop_reason: "refusal"` and empty
  `content`. `stop_details.category` is one of `cyber`, `bio`,
  `frontier_llm`, `reasoning_extraction`, `general_harms` or `null`. Benign
  security work can trigger `cyber` on Opus 5 - relevant for forensics topics.
- Thinking tokens count toward `max_tokens` and are billed as output tokens;
  `usage.output_tokens_details.thinking_tokens` shows how many.
- Changing `effort` between requests invalidates cache breakpoints (matters
  when prompt caching is added later).
