# Cloud speech services and LLM refinement

Handy Remote can recognize speech in three places: on this computer (upstream Handy models), on a
[Handy Remote server](SERVER-SETUP.md), or with a **public cloud speech-to-text API**. Independently of
where recognition runs, a **language model** can polish and structure the text before it is pasted.

中文版：[zh-CN/CLOUD-AND-LLM.md](zh-CN/CLOUD-AND-LLM.md)

## Cloud speech recognition

**Settings → Cloud**. Turn on **Use cloud recognition**, choose a provider, enter its API key, press
**Test** (it uploads one second of a quiet tone and reports the round trip), then **Save**. Turning on
remote recognition turns cloud recognition off, and the other way round.

| Provider                      | API used                                       | Default model                 |
| ----------------------------- | ---------------------------------------------- | ----------------------------- |
| OpenAI                        | `/audio/transcriptions`                        | `gpt-4o-mini-transcribe`      |
| Azure OpenAI                  | deployment `/audio/transcriptions` (`api-key`) | your deployment               |
| Azure AI Speech               | fast transcription API (`<region>` endpoint)   | —                             |
| Google Cloud Speech-to-Text   | `speech:recognize` with an API key (≤ 55 s)    | `latest_long`                 |
| Deepgram                      | `/listen`                                      | `nova-3`                      |
| AssemblyAI                    | upload, then poll the transcript               | `universal`                   |
| ElevenLabs Scribe             | `/speech-to-text`                              | `scribe_v1`                   |
| Groq                          | OpenAI-compatible                              | `whisper-large-v3-turbo`      |
| Mistral Voxtral               | OpenAI-compatible                              | `voxtral-mini-latest`         |
| Alibaba Qwen3-ASR (DashScope) | chat completions with `input_audio`            | `qwen3-asr-flash`             |
| SiliconFlow                   | OpenAI-compatible                              | `FunAudioLLM/SenseVoiceSmall` |
| Custom                        | any OpenAI-compatible server (key optional)    | —                             |

- Endpoint and model are remembered per provider; leave them empty for the defaults. Change the
  endpoint for another region, a proxy or a self-hosted compatible server.
- The selected recognition language is passed to the service when it accepts one; "auto"
  lets the service detect it.
- Recognition is batch only: the recording is uploaded once, when you stop. There is no live preview
  and no sentence-by-sentence paste.
- **Privacy:** your audio leaves the computer and is processed under the provider's terms. API keys
  are stored only in this computer's settings file; the recognition worker receives them in an
  environment variable, never on a command line or in the log.

## LLM refinement (polish and structure)

Turn on **Post-processing** in **Settings → Advanced**; a **Post-processing** page appears in the
sidebar. It builds on upstream Handy's post-processing and adds:

- **Refine every dictation**: every dictation goes through the selected model and prompt before it is
  pasted, not only the post-processing shortcut. Sentence-by-sentence paste is turned off while this
  is on (the text is pasted once, after the rewrite).
- **Skip it for one dictation**: while a dictation will be refined, the overlay shows a sparkle next
  to the cancel button. Click it to paste this dictation as recognised (click again to change your
  mind while still recording). Clicking it while the rewrite is running pastes the transcript at
  once. The next dictation is refined again.
- **Polish & structure** prompt, added once and selected when no prompt was selected. It keeps the
  language and meaning, fixes punctuation and obvious recognition errors, drops fillers and
  self-corrections, writes numbers as digits, and turns spoken structure into layout:
  "第一点…第二点…" / "first… second…" become a numbered list (`1.` on new lines), spoken
  "换行 / new line" a line break, "新段落 / new paragraph" a blank line, spoken punctuation names the
  symbols. Edit it like any other prompt; deleting it is permanent.
- **More providers**: local OpenAI-compatible servers (Ollama, LM Studio, llama.cpp server, Foundry
  Local, OpenVINO Model Server, Lemonade) with editable addresses and no key needed, and cloud
  services DeepSeek, Alibaba Qwen (DashScope), Moonshot Kimi, Zhipu GLM, SiliconFlow, Google Gemini and
  Mistral, besides upstream's OpenAI, Anthropic, Groq, Cerebras, OpenRouter, Z.AI and others.
- **Find local models**: probes the usual addresses (and `foundry service status`) at once and lists
  the running servers with their models; the recommended model is a small instruction-tuned one (3–9 B,
  Qwen first, no embedding, speech, vision, coder or thinking models). **Use** selects the server and
  model.
- **Speed test**: runs the selected provider, model and prompt on a sample dictation (one warm-up, then
  three timed runs) and shows the median time and the output, so you can judge whether the model is
  fast and good enough to wait for on every dictation.
- **GitHub Copilot** through the [Copilot bridge](../../tools/copilot-bridge/README.md): a small
  loopback server that serves your Copilot plan's models as an OpenAI-compatible API. **Find local
  models** lists its available models. Choose one from your own plan and run the speed test;
  no account-specific model is preferred. Dictated text then goes to GitHub Copilot.
- **My rules**: your own instructions for the model, one per line in a plain text file
  (`refine-rules.txt` in the app data folder, or any file you choose, for example in a synced folder
  so several PCs share it). They are added to the selected prompt on every dictation (at `${rules}`,
  or before the transcript paragraph in prompts without it) and take priority over the prompt's own
  rules. Lines starting with `#` are comments. The file is read on every dictation, so edits in any
  editor apply at once. Your **custom words** are added as a rule to keep them exactly as written.
- **Authentication** is configured per custom endpoint: automatic (Bearer for OpenAI, `x-api-key`
  for Anthropic), Bearer, `api-key`, or none. Only the selected authentication header is sent.

### Private endpoint configuration

Copy [endpoints.example.json](endpoints.example.json) to a private folder **outside the checkout**.
Edit labels, URLs, optional model IDs, API formats and authentication modes for your own services.
Use **Import endpoints** beside the provider selector to load the file into this installation's
settings. Imported entries appear alongside built-in providers; each has its own key and model.

- IDs must be unique `custom_<name>` identifiers. Keep an ID stable when changing its label or URL.
- Supported `api_format` values: `openai` (default), `anthropic`.
- Supported `auth` values: `auto` (default), `bearer`, `api_key`, `none`.
- The optional `model` selects that endpoint's model; omitting it preserves the current selection.
- Files must be UTF-8 JSON, version `1`, at most 128 KiB and at most 100 entries.
- Re-import updates entries with the same ID. It does not remove other entries, change the active
  provider or overwrite API keys. An invalid file changes nothing.
- Changing an endpoint's URL while it has a saved key is rejected. Clear its API Key field first,
  import the new destination, then enter that service's key; credentials are never transferred
  to a new destination by importing configuration.
- **Never put keys in the import file.** Unknown fields (including key fields) are rejected.
  Enter keys using the app's API Key field; they remain in the local settings store, which is not
  encrypted. Do not copy that store into source control or shared storage.
- Import does not contact the services. **Find local models** probes only loopback URLs; it never
  probes imported remote hosts or sends keys to discovered servers.
- Discovery and LLM requests do not follow HTTP redirects. Configure the final API base URL.

Reasoning is switched off for local servers and the added cloud services: it costs seconds and does
not help with this task.

### Writing rules

Models differ in small style choices (三楼 or 3楼, 三个问题 or 3个问题, reordered phrases). Rules pin
these down. What worked in testing:

- One concern per rule, with a short example: `楼层用阿拉伯数字（三楼→3楼）`.
- When a rule has exceptions, list them in the same rule, with the wrong form:
  `数量和时长保留汉字：三个问题、五分钟，不要改成3个问题、5分钟`. Two separate rules ("times as digits",
  "counts as words") made models turn 五分钟 into 5分钟.
- Keep a few test dictations with the expected result and re-run them after changing a rule, with a
  dictation that is _not_ used as an example in the rules.

Tips:

- Use **Speed test** with your own provider and model, then try representative dictations. Latency
  and formatting quality depend on the model, hardware, provider and personal rules; no benchmark
  result from a maintainer's machine is a default or a guarantee.
- Heavy models are rarely worth it for this: they are several times slower and costlier without
  fixing more than rules do.
- When the model fails or does not answer within 60 s, Handy pastes the unrefined text.
- For long dictations keep **Refine every dictation** off and use the post-processing shortcut only
  when you want a rewrite.
