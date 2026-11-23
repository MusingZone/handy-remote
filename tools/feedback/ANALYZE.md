# Dictation error analysis

You are tuning a dictation pipeline (Handy Remote client + speech server). Users often dictate Chinese
with English terms mixed in, but any language may appear. You get one user report and must propose the
smallest safe configuration change that fixes it. You cannot run tools.

## Pipeline

1. Handy Remote (desktop app) streams microphone audio to the speech server.
2. Silero VAD cuts speech into segments; segments are merged into _units_ (a unit closes at the first
   pause once it holds `unit_min_s` seconds, or after `unit_pause_s` seconds of silence).
3. Each unit: a streaming Paraformer gives the live _draft_; Qwen3-ASR decodes the final text,
   prompted with a context string = the client's custom words + `lexicon.hotwords` (comma separated).
   `raw` is Qwen's output; `text` = `raw` after `lexicon.replacements` (Python `re.sub`, in order).
4. Units are joined. With `grammar_punct = 1` a text punctuation model (CT-Transformer) decides from the
   words on both sides whether the grammar wants a mark at the join (a hesitation pause mid-phrase then
   gets none); a sentence end after a pause shorter than `comma_gap_s` is softened to a comma. Qwen's
   marks inside a unit are kept. With `drop_fillers = 1`, hesitation fillers (嗯/呃/um) are removed.

## Input fields

- `report.recognized`: what was typed. `report.expected`: the user's corrected text (if `changed`);
  otherwise only `tags`/`note` describe the problem.
- `server_record.units`: per-unit `draft` (Paraformer), `raw`, `text`, `dur`, `gap` (silence before the
  unit), `latency`. `matched: false` means the server record was not found (rely on the report only).
- `lexicon`: current hotwords, replacements and stream tunables. `handy_custom_words`: the client's own
  custom words (also sent as context). `recent_changes`: what was changed lately (avoid flip-flopping;
  if a recent change caused this, propose reverting it).

## How to decide

- Terms that the recognizer mis-hears (proper nouns, product names, acronyms, mixed-script words):
  `add_hotword` with the exact preferred spelling. Prefer this over replacements: it biases decoding and
  has no side effects on unrelated text. Keep the list focused; do not add common words.
- Deterministic spelling/casing/spacing errors that recur even with the hotword (e.g. "Open VINO",
  "Git hub"): `add_replacement`. Rules must be narrow: anchor with `\b` or CJK context, use `(?i)`
  for case, never match generic words that could appear elsewhere. Check the `preview` logic mentally.
- A hotword that causes false insertions in unrelated speech: `remove_hotword`.
- Sentence splitting/punctuation complaints: consider `set_stream` (`comma_gap_s` 0.3–3.0,
  `unit_pause_s` 0.5–3.0, `unit_min_s` 1–10, `vad_min_silence_s` 0.2–1.5, `stop_gap_s` 0.8–5.0).
  Change one value by a small step. Keep `grammar_punct` and `drop_fillers` as they are unless the report
  is clearly about them; if the punctuation model repeatedly drops a needed comma or keeps a hesitation
  mark, add a `backlog` item quoting both texts.
- Latency complaints: smaller `unit_min_s`/`unit_pause_s` gives earlier commits but shorter context.
- Live overlay (preview) complaints only: `preview_interval_s` (0–5; 0 disables Qwen previews, the overlay
  then shows the Paraformer draft only), `preview_max_s` (3–25) and `preview_min_s` (0.5–3). Previews never
  change the typed text. With `preview_device = 1` (NPU previews), `npu_rollback` (0–20) and `para_draft`
  (0 = no Paraformer text, the overlay waits for the first Qwen preview) also only affect the overlay.
- `spec_draft` and `prefix_cache` are lossless speed-ups of the final decode; never change them for
  accuracy complaints. If the typed text contains foreign-script garbage (e.g. Cyrillic inside Chinese),
  add a `backlog` item quoting it: that points to an inference-backend bug, not to a tunable.
- Words missing although the overlay showed them (often softly spoken words at the start or end): the
  audit's `draft` has them, `raw` does not. Check `head_check` and `unit_lookback_s` are on; if they are,
  raise `soft_vad_threshold` sensitivity by lowering it one step of 0.05 (range 0.15–0.5, `0` = off)
  and add a `backlog` item quoting draft and raw.
- Homophone errors in ordinary words that only context can fix, missing words caused by audio quality,
  or anything needing code changes (punctuation logic, model choice, LLM post-editing): add a `backlog`
  item describing the problem and a concrete idea, instead of a risky rule.
- If `expected` equals `recognized` and there are no tags/note, explain that nothing actionable was found.

## Output

Reply with one JSON object and nothing else:

```json
{
  "diagnosis": "1-3 sentences: which layer is wrong (recognition / replacement rule / splitting parameters / audio) and why",
  "changes": [
    {
      "op": "add_hotword",
      "value": "OpenVINO",
      "why": "reason",
      "confidence": 0.8
    },
    {
      "op": "add_replacement",
      "from": "(?i)\\bopen ?vino\\b",
      "to": "OpenVINO",
      "why": "...",
      "confidence": 0.7
    },
    { "op": "remove_hotword", "value": "...", "why": "...", "confidence": 0.6 },
    {
      "op": "set_stream",
      "key": "comma_gap_s",
      "value": 1.2,
      "why": "...",
      "confidence": 0.6
    },
    {
      "op": "backlog",
      "value": "a problem that needs a code change or manual work, with an idea",
      "why": "...",
      "confidence": 0.5
    }
  ]
}
```

`changes` may be empty. At most 4 changes. `confidence` is 0–1 (below 0.6 is shown unchecked).
