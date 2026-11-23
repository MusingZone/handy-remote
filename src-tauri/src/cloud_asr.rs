//! Fork: speech recognition by a public cloud speech-to-text API.
//!
//! Like the Handy Remote server (`remote.rs`), a cloud service takes the place of the local
//! transcription worker: the worker command becomes this executable in cloud mode
//! (`--cloud-worker`), which speaks the same framed worker protocol on stdin/stdout and sends each
//! recording to the selected provider's HTTP API. Recognition is batch only (no live preview):
//! the recording is uploaded once, when it ends.
//!
//! The provider, endpoint and model come from the settings (`cloud_asr`), the API keys from
//! `cloud_asr_api_keys`. The worker gets them in the `HANDY_CLOUD_ASR` environment variable, so
//! keys never appear on a command line or in the log.

use std::collections::HashMap;
use std::io::Write;
use std::sync::RwLock;
use std::time::{Duration, Instant};

use log::{info, warn};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use specta::Type;
use tauri::AppHandle;

use crate::settings::{get_settings, write_settings};

/// First argument that turns the executable into a cloud recognition worker.
pub const WORKER_FLAG: &str = "--cloud-worker";
const CONFIG_ENV: &str = "HANDY_CLOUD_ASR";
const SAMPLE_RATE: u32 = 16_000;
const USER_AGENT: &str = concat!("Handy-Remote/", env!("CARGO_PKG_VERSION"));

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq, Eq, Type)]
#[serde(default)]
pub struct CloudAsrSettings {
    pub enabled: bool,
    pub provider_id: String,
    /// Endpoint per provider, when it differs from the provider's default.
    pub base_urls: HashMap<String, String>,
    /// Model per provider, when it differs from the provider's default.
    pub models: HashMap<String, String>,
    /// Seconds one recognition may take, upload included.
    pub timeout_secs: u32,
}

impl Default for CloudAsrSettings {
    fn default() -> Self {
        Self {
            enabled: false,
            provider_id: "openai".into(),
            base_urls: HashMap::new(),
            models: HashMap::new(),
            timeout_secs: 60,
        }
    }
}

/// How a provider's API is called.
#[derive(Serialize, Deserialize, Debug, Clone, Copy, PartialEq, Eq, Type)]
#[serde(rename_all = "snake_case")]
pub enum CloudAsrApi {
    /// `POST {base}/audio/transcriptions`, multipart (OpenAI and compatible services).
    OpenAi,
    /// The same with an `api-key` header and `api-version` query (Azure OpenAI deployments).
    AzureOpenAi,
    /// Azure AI Speech fast transcription (`/speechtotext/transcriptions:transcribe`).
    AzureSpeech,
    /// Google Cloud Speech-to-Text v1 `speech:recognize` with an API key.
    Google,
    /// Deepgram pre-recorded audio (`/listen`).
    Deepgram,
    /// AssemblyAI upload, then transcript polling.
    AssemblyAi,
    /// ElevenLabs Scribe (`/speech-to-text`).
    ElevenLabs,
    /// Audio in an OpenAI-compatible chat completion (Alibaba Qwen3-ASR on DashScope).
    ChatAudio,
}

#[derive(Serialize, Debug, Clone, Type)]
pub struct CloudAsrProvider {
    pub id: String,
    pub label: String,
    pub api: CloudAsrApi,
    pub base_url: String,
    /// Shown as the placeholder when the endpoint is account specific.
    pub base_url_hint: String,
    pub model: String,
    pub models: Vec<String>,
    pub key_url: String,
    pub key_optional: bool,
    /// Longest recording one request accepts; longer recordings are split by the app. 0 = no limit.
    pub max_audio_secs: u32,
}

fn provider(
    id: &str,
    label: &str,
    api: CloudAsrApi,
    base_url: &str,
    model: &str,
    models: &[&str],
    key_url: &str,
) -> CloudAsrProvider {
    CloudAsrProvider {
        id: id.into(),
        label: label.into(),
        api,
        base_url: base_url.into(),
        base_url_hint: String::new(),
        model: model.into(),
        models: models.iter().map(|m| m.to_string()).collect(),
        key_url: key_url.into(),
        key_optional: false,
        max_audio_secs: 0,
    }
}

/// The supported services. Every endpoint can be changed (regions, proxies, compatible servers).
pub fn providers() -> Vec<CloudAsrProvider> {
    use CloudAsrApi::*;
    let mut list = vec![
        provider(
            "openai",
            "OpenAI",
            OpenAi,
            "https://api.openai.com/v1",
            "gpt-4o-mini-transcribe",
            &["gpt-4o-mini-transcribe", "gpt-4o-transcribe", "whisper-1"],
            "https://platform.openai.com/api-keys",
        ),
        CloudAsrProvider {
            base_url_hint: "https://<resource>.openai.azure.com/openai/deployments/<deployment>"
                .into(),
            ..provider(
                "azure_openai",
                "Azure OpenAI",
                AzureOpenAi,
                "",
                "",
                &[],
                "https://portal.azure.com/#view/Microsoft_Azure_ProjectOxford/CognitiveServicesHub/~/OpenAI",
            )
        },
        CloudAsrProvider {
            base_url_hint: "https://<region>.api.cognitive.microsoft.com".into(),
            ..provider(
                "azure_speech",
                "Azure AI Speech",
                AzureSpeech,
                "https://eastus.api.cognitive.microsoft.com",
                "",
                &[],
                "https://portal.azure.com/#view/Microsoft_Azure_ProjectOxford/CognitiveServicesHub/~/SpeechServices",
            )
        },
        CloudAsrProvider {
            max_audio_secs: 55,
            ..provider(
                "google",
                "Google Cloud Speech-to-Text",
                Google,
                "https://speech.googleapis.com/v1",
                "latest_long",
                &["latest_long", "latest_short", "default"],
                "https://console.cloud.google.com/apis/credentials",
            )
        },
        provider(
            "deepgram",
            "Deepgram",
            Deepgram,
            "https://api.deepgram.com/v1",
            "nova-3",
            &["nova-3", "nova-2"],
            "https://console.deepgram.com/",
        ),
        provider(
            "assemblyai",
            "AssemblyAI",
            AssemblyAi,
            "https://api.assemblyai.com/v2",
            "universal",
            &["universal", "slam-1"],
            "https://www.assemblyai.com/app/api-keys",
        ),
        provider(
            "elevenlabs",
            "ElevenLabs Scribe",
            ElevenLabs,
            "https://api.elevenlabs.io/v1",
            "scribe_v1",
            &["scribe_v1"],
            "https://elevenlabs.io/app/settings/api-keys",
        ),
        provider(
            "groq",
            "Groq",
            OpenAi,
            "https://api.groq.com/openai/v1",
            "whisper-large-v3-turbo",
            &["whisper-large-v3-turbo", "whisper-large-v3"],
            "https://console.groq.com/keys",
        ),
        provider(
            "mistral",
            "Mistral Voxtral",
            OpenAi,
            "https://api.mistral.ai/v1",
            "voxtral-mini-latest",
            &["voxtral-mini-latest"],
            "https://console.mistral.ai/api-keys",
        ),
        provider(
            "dashscope",
            "Alibaba Qwen3-ASR (DashScope)",
            ChatAudio,
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "qwen3-asr-flash",
            &["qwen3-asr-flash"],
            "https://bailian.console.aliyun.com/?apiKey=1",
        ),
        provider(
            "siliconflow",
            "SiliconFlow",
            OpenAi,
            "https://api.siliconflow.cn/v1",
            "FunAudioLLM/SenseVoiceSmall",
            &["FunAudioLLM/SenseVoiceSmall", "TeleAI/TeleSpeechASR"],
            "https://cloud.siliconflow.cn/account/ak",
        ),
    ];
    list.push(CloudAsrProvider {
        key_optional: true,
        base_url_hint: "http://localhost:8000/v1".into(),
        ..provider(
            "custom",
            "Custom (OpenAI-compatible)",
            OpenAi,
            "http://localhost:8000/v1",
            "",
            &[],
            "",
        )
    });
    list
}

pub fn find_provider(id: &str) -> Option<CloudAsrProvider> {
    providers().into_iter().find(|p| p.id == id)
}

/// Everything the worker needs for one provider (the key included).
#[derive(Serialize, Deserialize, Clone)]
pub struct WorkerConfig {
    pub provider_id: String,
    pub label: String,
    pub api: CloudAsrApi,
    pub base_url: String,
    pub model: String,
    pub api_key: String,
    pub timeout_secs: u32,
    pub max_audio_secs: u32,
}

impl std::fmt::Debug for WorkerConfig {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("WorkerConfig")
            .field("provider_id", &self.provider_id)
            .field("base_url", &self.base_url)
            .field("model", &self.model)
            .field(
                "api_key",
                &if self.api_key.is_empty() {
                    ""
                } else {
                    "[REDACTED]"
                },
            )
            .finish()
    }
}

/// The effective configuration, or why the settings are not usable.
pub fn worker_config(
    settings: &CloudAsrSettings,
    keys: &HashMap<String, String>,
) -> Result<WorkerConfig, String> {
    let p = find_provider(&settings.provider_id)
        .ok_or_else(|| format!("unknown cloud provider '{}'", settings.provider_id))?;
    let pick = |map: &HashMap<String, String>, default: &str| {
        map.get(&p.id)
            .map(|v| v.trim().to_string())
            .filter(|v| !v.is_empty())
            .unwrap_or_else(|| default.to_string())
    };
    let base_url = pick(&settings.base_urls, &p.base_url)
        .trim_end_matches('/')
        .to_string();
    if !(base_url.starts_with("https://") || base_url.starts_with("http://")) {
        return Err(format!("{}: set the endpoint URL", p.label));
    }
    let api_key = keys
        .get(&p.id)
        .map(|k| k.trim().to_string())
        .unwrap_or_default();
    if api_key.is_empty() && !p.key_optional {
        return Err(format!("{}: enter an API key", p.label));
    }
    Ok(WorkerConfig {
        label: p.label.clone(),
        api: p.api,
        model: pick(&settings.models, &p.model),
        base_url,
        api_key,
        timeout_secs: settings.timeout_secs.clamp(5, 600),
        max_audio_secs: p.max_audio_secs,
        provider_id: p.id,
    })
}

// ---------------------------------------------------------------- state in the app

static STATE: RwLock<Option<WorkerConfig>> = RwLock::new(None);

fn set_state(config: Option<WorkerConfig>) {
    if let Ok(mut g) = STATE.write() {
        *g = config;
    }
}

/// The worker command while cloud recognition is on.
pub fn worker_spec() -> Option<crate::remote::Spec> {
    let config = STATE.read().ok()?.clone()?;
    Some(crate::remote::Spec {
        program: std::env::current_exe().ok()?,
        args: vec![WORKER_FLAG.into()],
        env: vec![(
            CONFIG_ENV.into(),
            serde_json::to_string(&config).unwrap_or_default(),
        )],
    })
}

pub fn active() -> bool {
    STATE.read().is_ok_and(|g| g.is_some())
}

/// Name of the placeholder model that stands for the cloud service in the model list.
pub fn model_label() -> Option<String> {
    STATE
        .read()
        .ok()?
        .as_ref()
        .map(|c| format!("Cloud: {}", c.label))
}

fn apply(settings: &crate::settings::AppSettings) {
    if !settings.cloud_asr.enabled {
        set_state(None);
        return;
    }
    match worker_config(&settings.cloud_asr, &settings.cloud_asr_api_keys) {
        Ok(config) => {
            info!(
                "cloud recognition: {} {} model '{}'",
                config.provider_id, config.base_url, config.model
            );
            set_state(Some(config));
        }
        Err(e) => {
            warn!("cloud recognition is on but not usable: {e}");
            set_state(None);
        }
    }
}

pub fn init(app: &AppHandle) {
    apply(&get_settings(app));
}

// ---------------------------------------------------------------- audio and HTTP helpers

/// 16-bit mono WAV of 16 kHz samples.
pub fn wav_bytes(pcm: &[f32]) -> Vec<u8> {
    let data_len = (pcm.len() * 2) as u32;
    let mut out = Vec::with_capacity(44 + data_len as usize);
    out.extend_from_slice(b"RIFF");
    out.extend_from_slice(&(36 + data_len).to_le_bytes());
    out.extend_from_slice(b"WAVEfmt ");
    out.extend_from_slice(&16u32.to_le_bytes());
    out.extend_from_slice(&1u16.to_le_bytes());
    out.extend_from_slice(&1u16.to_le_bytes());
    out.extend_from_slice(&SAMPLE_RATE.to_le_bytes());
    out.extend_from_slice(&(SAMPLE_RATE * 2).to_le_bytes());
    out.extend_from_slice(&2u16.to_le_bytes());
    out.extend_from_slice(&16u16.to_le_bytes());
    out.extend_from_slice(b"data");
    out.extend_from_slice(&data_len.to_le_bytes());
    for s in pcm {
        let v = (s.clamp(-1.0, 1.0) * 32767.0).round() as i16;
        out.extend_from_slice(&v.to_le_bytes());
    }
    out
}

struct Multipart {
    boundary: String,
    body: Vec<u8>,
}

impl Multipart {
    fn new() -> Self {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or_default();
        Self {
            boundary: format!("----handy-remote-{nanos:x}"),
            body: Vec::new(),
        }
    }

    fn text(mut self, name: &str, value: &str) -> Self {
        let _ = write!(
            self.body,
            "--{}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n",
            self.boundary
        );
        self
    }

    fn file(mut self, name: &str, file_name: &str, content_type: &str, bytes: &[u8]) -> Self {
        let _ = write!(
            self.body,
            "--{}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{file_name}\"\r\nContent-Type: {content_type}\r\n\r\n",
            self.boundary
        );
        self.body.extend_from_slice(bytes);
        self.body.extend_from_slice(b"\r\n");
        self
    }

    fn finish(mut self) -> (String, Vec<u8>) {
        let _ = write!(self.body, "--{}--\r\n", self.boundary);
        (
            format!("multipart/form-data; boundary={}", self.boundary),
            self.body,
        )
    }
}

/// Base language of an app language code: `zh-Hans` → `zh`, `auto`/empty → None.
fn base_language(language: Option<&str>) -> Option<String> {
    let l = language?.trim();
    if l.is_empty() || l.eq_ignore_ascii_case("auto") {
        return None;
    }
    Some(l.split(['-', '_']).next()?.to_ascii_lowercase())
}

/// BCP-47 locale for services that want one (Azure Speech, Google).
fn locale(language: Option<&str>) -> Option<String> {
    let full = language?.trim().to_ascii_lowercase();
    let base = base_language(language)?;
    let tag = match (base.as_str(), full.as_str()) {
        ("zh", f) if f.contains("hant") || f.ends_with("tw") => "zh-TW",
        ("zh", f) if f.ends_with("hk") => "zh-HK",
        ("zh", _) => "zh-CN",
        ("yue", _) => "zh-HK",
        ("en", _) => "en-US",
        ("ja", _) => "ja-JP",
        ("ko", _) => "ko-KR",
        ("pt", _) => "pt-BR",
        ("ar", _) => "ar-SA",
        ("hi", _) => "hi-IN",
        ("vi", _) => "vi-VN",
        ("uk", _) => "uk-UA",
        ("he", _) => "he-IL",
        ("cs", _) => "cs-CZ",
        ("da", _) => "da-DK",
        ("sv", _) => "sv-SE",
        ("ca", _) => "ca-ES",
        ("ne", _) => "ne-NP",
        _ => return Some(format!("{base}-{}", base.to_ascii_uppercase())),
    };
    Some(tag.into())
}

fn client(timeout: Duration) -> Result<reqwest::Client, String> {
    reqwest::Client::builder()
        .user_agent(USER_AGENT)
        .connect_timeout(Duration::from_secs(10))
        .timeout(timeout)
        .build()
        .map_err(|e| e.to_string())
}

async fn json_reply(label: &str, response: reqwest::Response) -> Result<Value, String> {
    let status = response.status();
    let text = response.text().await.map_err(|e| e.to_string())?;
    if !status.is_success() {
        let detail: String = serde_json::from_str::<Value>(&text)
            .ok()
            .and_then(|v| {
                [
                    "/error/message",
                    "/error",
                    "/message",
                    "/detail/message",
                    "/detail",
                    "/err_msg",
                ]
                .iter()
                .find_map(|p| v.pointer(p).and_then(Value::as_str).map(str::to_owned))
            })
            .unwrap_or_else(|| text.chars().take(300).collect());
        return Err(format!(
            "{label}: HTTP {} {}",
            status.as_u16(),
            detail.trim()
        ));
    }
    serde_json::from_str(&text).map_err(|e| format!("{label}: unexpected reply ({e})"))
}

fn str_at<'a>(v: &'a Value, pointer: &str) -> Option<&'a str> {
    v.pointer(pointer).and_then(Value::as_str)
}

/// Recognize one recording. `vocabulary`: custom words, used as hints where the API has them.
pub async fn transcribe(
    config: &WorkerConfig,
    pcm: &[f32],
    language: Option<&str>,
    vocabulary: &[String],
) -> Result<String, String> {
    let http = client(Duration::from_secs(config.timeout_secs as u64))?;
    let wav = wav_bytes(pcm);
    let base = config.base_url.as_str();
    let label = config.label.as_str();
    let key = config.api_key.as_str();
    let lang = base_language(language);
    let hints = vocabulary
        .iter()
        .map(|w| w.trim())
        .filter(|w| !w.is_empty())
        .take(100)
        .collect::<Vec<_>>();
    match config.api {
        CloudAsrApi::OpenAi | CloudAsrApi::AzureOpenAi => {
            let mut form = Multipart::new().file("file", "audio.wav", "audio/wav", &wav);
            if !config.model.is_empty() {
                form = form.text("model", &config.model);
            }
            if let Some(l) = &lang {
                form = form.text("language", l);
            }
            if !hints.is_empty() {
                form = form.text("prompt", &hints.join(", "));
            }
            let (content_type, body) = form.text("response_format", "json").finish();
            let request = if config.api == CloudAsrApi::AzureOpenAi {
                let url = if base.contains("api-version=") {
                    base.replacen('?', "/audio/transcriptions?", 1)
                } else {
                    format!("{base}/audio/transcriptions?api-version=2024-10-21")
                };
                http.post(url).header("api-key", key)
            } else {
                let r = http.post(format!("{base}/audio/transcriptions"));
                if key.is_empty() {
                    r
                } else {
                    r.bearer_auth(key)
                }
            };
            let v = json_reply(
                label,
                request
                    .header(reqwest::header::CONTENT_TYPE, content_type)
                    .body(body)
                    .send()
                    .await
                    .map_err(|e| format!("{label}: {e}"))?,
            )
            .await?;
            Ok(str_at(&v, "/text").unwrap_or_default().to_string())
        }
        CloudAsrApi::AzureSpeech => {
            let mut definition = json!({ "profanityFilterMode": "None" });
            if let Some(l) = locale(language) {
                definition["locales"] = json!([l]);
            }
            if !hints.is_empty() {
                definition["phraseList"] = json!({ "phrases": hints });
            }
            let (content_type, body) = Multipart::new()
                .file("audio", "audio.wav", "audio/wav", &wav)
                .text("definition", &definition.to_string())
                .finish();
            let v = json_reply(
                label,
                http.post(format!(
                    "{base}/speechtotext/transcriptions:transcribe?api-version=2024-11-15"
                ))
                .header("Ocp-Apim-Subscription-Key", key)
                .header(reqwest::header::CONTENT_TYPE, content_type)
                .body(body)
                .send()
                .await
                .map_err(|e| format!("{label}: {e}"))?,
            )
            .await?;
            Ok(str_at(&v, "/combinedPhrases/0/text")
                .unwrap_or_default()
                .to_string())
        }
        CloudAsrApi::Google => {
            let mut cfg = json!({
                "encoding": "LINEAR16",
                "sampleRateHertz": SAMPLE_RATE,
                "languageCode": locale(language).unwrap_or_else(|| "en-US".into()),
                "enableAutomaticPunctuation": true,
            });
            if !config.model.is_empty() {
                cfg["model"] = json!(config.model);
            }
            if !hints.is_empty() {
                cfg["speechContexts"] = json!([{ "phrases": hints }]);
            }
            let body = json!({
                "config": cfg,
                "audio": { "content": base64(&wav) },
            });
            let v = json_reply(
                label,
                http.post(format!("{base}/speech:recognize"))
                    .query(&[("key", key)])
                    .json(&body)
                    .send()
                    .await
                    .map_err(|e| format!("{label}: {e}"))?,
            )
            .await?;
            let parts = v
                .get("results")
                .and_then(Value::as_array)
                .map(|results| {
                    results
                        .iter()
                        .filter_map(|r| str_at(r, "/alternatives/0/transcript"))
                        .map(str::trim)
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default();
            Ok(join_parts(&parts))
        }
        CloudAsrApi::Deepgram => {
            let mut query: Vec<(&str, String)> = vec![
                ("smart_format", "true".into()),
                ("punctuate", "true".into()),
            ];
            if !config.model.is_empty() {
                query.push(("model", config.model.clone()));
            }
            match &lang {
                Some(l) => query.push(("language", l.clone())),
                None => query.push(("detect_language", "true".into())),
            }
            if config.model.starts_with("nova-3") {
                query.extend(hints.iter().take(50).map(|h| ("keyterm", h.to_string())));
            } else {
                query.extend(hints.iter().take(50).map(|h| ("keywords", h.to_string())));
            }
            let v = json_reply(
                label,
                http.post(format!("{base}/listen"))
                    .query(&query)
                    .header(reqwest::header::AUTHORIZATION, format!("Token {key}"))
                    .header(reqwest::header::CONTENT_TYPE, "audio/wav")
                    .body(wav)
                    .send()
                    .await
                    .map_err(|e| format!("{label}: {e}"))?,
            )
            .await?;
            Ok(str_at(&v, "/results/channels/0/alternatives/0/transcript")
                .unwrap_or_default()
                .to_string())
        }
        CloudAsrApi::AssemblyAi => {
            let deadline = Instant::now() + Duration::from_secs(config.timeout_secs as u64);
            let up = json_reply(
                label,
                http.post(format!("{base}/upload"))
                    .header(reqwest::header::AUTHORIZATION, key)
                    .header(reqwest::header::CONTENT_TYPE, "application/octet-stream")
                    .body(wav)
                    .send()
                    .await
                    .map_err(|e| format!("{label}: {e}"))?,
            )
            .await?;
            let audio_url = str_at(&up, "/upload_url")
                .ok_or_else(|| format!("{label}: the upload returned no URL"))?;
            let mut request =
                json!({ "audio_url": audio_url, "punctuate": true, "format_text": true });
            if !config.model.is_empty() {
                request["speech_model"] = json!(config.model);
            }
            match &lang {
                Some(l) => request["language_code"] = json!(l),
                None => request["language_detection"] = json!(true),
            }
            if !hints.is_empty() {
                request["word_boost"] = json!(hints);
            }
            let job = json_reply(
                label,
                http.post(format!("{base}/transcript"))
                    .header(reqwest::header::AUTHORIZATION, key)
                    .json(&request)
                    .send()
                    .await
                    .map_err(|e| format!("{label}: {e}"))?,
            )
            .await?;
            let id = str_at(&job, "/id")
                .ok_or_else(|| format!("{label}: no transcript id"))?
                .to_string();
            loop {
                let v = json_reply(
                    label,
                    http.get(format!("{base}/transcript/{id}"))
                        .header(reqwest::header::AUTHORIZATION, key)
                        .send()
                        .await
                        .map_err(|e| format!("{label}: {e}"))?,
                )
                .await?;
                match str_at(&v, "/status") {
                    Some("completed") => {
                        return Ok(str_at(&v, "/text").unwrap_or_default().to_string())
                    }
                    Some("error") => {
                        return Err(format!(
                            "{label}: {}",
                            str_at(&v, "/error").unwrap_or("recognition failed")
                        ))
                    }
                    _ => {}
                }
                if Instant::now() >= deadline {
                    return Err(format!(
                        "{label}: no result within {} s",
                        config.timeout_secs
                    ));
                }
                tokio::time::sleep(Duration::from_millis(300)).await;
            }
        }
        CloudAsrApi::ElevenLabs => {
            let mut form = Multipart::new()
                .text(
                    "model_id",
                    if config.model.is_empty() {
                        "scribe_v1"
                    } else {
                        &config.model
                    },
                )
                .text("tag_audio_events", "false")
                .file("file", "audio.wav", "audio/wav", &wav);
            if let Some(l) = &lang {
                form = form.text("language_code", l);
            }
            let (content_type, body) = form.finish();
            let v = json_reply(
                label,
                http.post(format!("{base}/speech-to-text"))
                    .header("xi-api-key", key)
                    .header(reqwest::header::CONTENT_TYPE, content_type)
                    .body(body)
                    .send()
                    .await
                    .map_err(|e| format!("{label}: {e}"))?,
            )
            .await?;
            Ok(str_at(&v, "/text").unwrap_or_default().trim().to_string())
        }
        CloudAsrApi::ChatAudio => {
            let data = format!("data:audio/wav;base64,{}", base64(&wav));
            let mut messages = Vec::new();
            if !hints.is_empty() {
                // Qwen3-ASR reads context text (names, terms) from the system message.
                messages
                    .push(json!({ "role": "system", "content": [{ "text": hints.join(", ") }] }));
            }
            messages.push(json!({
                "role": "user",
                "content": [{ "type": "input_audio", "input_audio": { "data": data } }],
            }));
            let mut asr_options = json!({ "enable_itn": false });
            if let Some(l) = &lang {
                asr_options["language"] = json!(l);
            }
            let body = json!({
                "model": config.model,
                "messages": messages,
                "stream": false,
                "asr_options": asr_options,
            });
            let request = http.post(format!("{base}/chat/completions"));
            let request = if key.is_empty() {
                request
            } else {
                request.bearer_auth(key)
            };
            let v = json_reply(
                label,
                request
                    .json(&body)
                    .send()
                    .await
                    .map_err(|e| format!("{label}: {e}"))?,
            )
            .await?;
            let content = v.pointer("/choices/0/message/content");
            Ok(match content {
                Some(Value::String(s)) => s.trim().to_string(),
                Some(Value::Array(parts)) => join_parts(
                    &parts
                        .iter()
                        .filter_map(|p| p.get("text").and_then(Value::as_str))
                        .collect::<Vec<_>>(),
                ),
                _ => String::new(),
            })
        }
    }
}

/// Standard base64 with padding.
fn base64(bytes: &[u8]) -> String {
    const ABC: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let n = chunk
            .iter()
            .enumerate()
            .fold(0u32, |acc, (i, b)| acc | (*b as u32) << (16 - 8 * i));
        for i in 0..4 {
            if i <= chunk.len() {
                out.push(ABC[(n >> (18 - 6 * i) & 63) as usize] as char);
            } else {
                out.push('=');
            }
        }
    }
    out
}

/// Join recognized pieces: no space between CJK text, one space elsewhere.
fn join_parts(parts: &[&str]) -> String {
    let mut out = String::new();
    for part in parts.iter().map(|p| p.trim()).filter(|p| !p.is_empty()) {
        let cjk = |c: Option<char>| c.is_some_and(|c| c as u32 >= 0x2E80);
        if !out.is_empty() && !(cjk(out.chars().last()) && cjk(part.chars().next())) {
            out.push(' ');
        }
        out.push_str(part);
    }
    out
}

// ---------------------------------------------------------------- worker process

/// `--cloud-worker` as the first argument.
pub fn is_worker_invocation() -> bool {
    std::env::args().nth(1).as_deref() == Some(WORKER_FLAG)
}

fn loaded_info(config: &WorkerConfig) -> Value {
    json!({
        "arch": "cloud",
        "variant": format!("{}:{}", config.provider_id, config.model),
        "backend": "cloud",
        "device": config.label,
        "on_gpu": false,
        "capabilities": {
            "native_sample_rate": SAMPLE_RATE,
            "languages": crate::remote::STUB_LANGUAGES,
            "translate_target_languages": [],
            "max_timestamp_kind": "None",
            "supports_language_detect": true,
            "supports_translate": false,
            "supports_streaming": false,
            "supports_spec_decode": false,
            "max_audio_ms": config.max_audio_secs as u64 * 1000,
        },
        "supports_initial_prompt": false,
        "supports_long_form": config.max_audio_secs == 0,
        "supports_vocabulary": true,
    })
}

fn transcript(text: &str, samples: usize) -> Value {
    json!({ "Transcript": {
        "text": text, "raw_text": text, "language": null, "timestamp_kind": "None",
        "segments": [{ "t0_ms": 0, "t1_ms": (samples as u64 * 1000) / SAMPLE_RATE as u64,
                       "first_word": 0, "n_words": 0, "first_token": 0, "n_tokens": 0,
                       "text": text, "speaker_id": 0 }],
    }})
}

/// Answer one worker request.
pub fn handle(config: &WorkerConfig, request: &Value, pcm: &[f32]) -> Value {
    let (kind, body) = match request {
        Value::String(s) => (s.as_str(), Value::Null),
        Value::Object(m) if m.len() == 1 => {
            let (k, v) = m.iter().next().expect("one entry");
            (k.as_str(), v.clone())
        }
        _ => return json!({ "Error": "malformed request" }),
    };
    match kind {
        "Hello" => {
            let devices = body
                .get("list_devices")
                .and_then(Value::as_bool)
                .unwrap_or(false)
                .then(|| {
                    json!([{
                        "name": "cloud", "description": format!("{} (cloud)", config.label),
                        "kind": "remote", "device_type": "Cpu", "device_id": null,
                        "memory_total": 0, "index": 0, "key": r#"["remote","name","cloud"]"#,
                    }])
                });
            json!({ "Hello": { "devices": devices } })
        }
        "Load" => json!({ "Loaded": loaded_info(config) }),
        "Run" => {
            let options = body.get("options").cloned().unwrap_or(Value::Null);
            let language = options.get("language").and_then(Value::as_str);
            let vocabulary: Vec<String> = options
                .get("vocabulary")
                .and_then(Value::as_array)
                .map(|a| {
                    a.iter()
                        .filter_map(|v| v.as_str().map(str::to_owned))
                        .collect()
                })
                .unwrap_or_default();
            if pcm.len() < (SAMPLE_RATE / 10) as usize {
                return transcript("", pcm.len());
            }
            let started = Instant::now();
            let result =
                tauri::async_runtime::block_on(transcribe(config, pcm, language, &vocabulary));
            match result {
                Ok(text) => {
                    eprintln!(
                        "[cloud] {} {:.1}s audio in {} ms",
                        config.provider_id,
                        pcm.len() as f32 / SAMPLE_RATE as f32,
                        started.elapsed().as_millis()
                    );
                    transcript(&text, pcm.len())
                }
                Err(e) => json!({ "Error": e }),
            }
        }
        "StreamReset" => json!("Ok"),
        "StreamBegin" | "Feed" | "Finalize" => {
            json!({ "Error": "cloud recognition has no live streaming" })
        }
        other => json!({ "Error": format!("unknown request {other}") }),
    }
}

/// Worker main loop; returns the process exit code.
pub fn run_worker() -> i32 {
    use crate::engine_supervisor::protocol::{read_message, write_message};
    let config: WorkerConfig = match std::env::var(CONFIG_ENV)
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
    {
        Some(c) => c,
        None => {
            eprintln!("[cloud] {CONFIG_ENV} is missing or invalid");
            return 2;
        }
    };
    let mut input = std::io::stdin().lock();
    let mut output = std::io::stdout().lock();
    loop {
        let (request, pcm) = match read_message::<Value>(&mut input) {
            Ok(Some(m)) => m,
            Ok(None) => return 0,
            Err(e) => {
                eprintln!("[cloud] bad frame: {e}");
                return 1;
            }
        };
        let reply = handle(&config, &request, &pcm);
        if write_message(&mut output, &reply, None).is_err() {
            return 0;
        }
    }
}

// ---------------------------------------------------------------- commands

#[derive(Serialize, Debug, Clone, Type)]
pub struct CloudAsrTestResult {
    pub elapsed_ms: u32,
    pub text: String,
}

#[tauri::command]
#[specta::specta]
pub fn cloud_asr_providers() -> Vec<CloudAsrProvider> {
    providers()
}

/// Save the cloud settings and, with `api_key`, the key of the selected provider; switch
/// recognition over (or back).
#[tauri::command]
#[specta::specta]
pub fn change_cloud_asr_settings(
    app: AppHandle,
    cloud: CloudAsrSettings,
    api_key: Option<String>,
) -> Result<(), String> {
    let mut settings = get_settings(&app);
    if let Some(key) = api_key {
        settings
            .cloud_asr_api_keys
            .insert(cloud.provider_id.clone(), key.trim().to_string());
    }
    if cloud.enabled {
        worker_config(&cloud, &settings.cloud_asr_api_keys)?;
    }
    let was_on = settings.cloud_asr.enabled;
    settings.cloud_asr = cloud.clone();
    if cloud.enabled && settings.remote.enabled {
        // One recognition service at a time.
        settings.remote.enabled = false;
    }
    let remote = settings.remote.clone();
    write_settings(&app, settings.clone());
    apply(&settings);
    crate::remote::switch_recognition(&app, remote, cloud.enabled || was_on)
}

/// Recognize a short generated sound with unsaved settings: checks the endpoint and the key.
#[tauri::command]
#[specta::specta]
pub async fn cloud_asr_test(
    app: AppHandle,
    cloud: CloudAsrSettings,
    api_key: Option<String>,
) -> Result<CloudAsrTestResult, String> {
    let mut keys: HashMap<String, String> = (*get_settings(&app).cloud_asr_api_keys).clone();
    if let Some(key) = api_key {
        keys.insert(cloud.provider_id.clone(), key);
    }
    let config = worker_config(&cloud, &keys)?;
    // One second of a quiet tone: enough for every API to accept the request.
    let pcm: Vec<f32> = (0..SAMPLE_RATE)
        .map(|i| 0.05 * (i as f32 * 440.0 * std::f32::consts::TAU / SAMPLE_RATE as f32).sin())
        .collect();
    let started = Instant::now();
    let text = transcribe(&config, &pcm, None, &[]).await?;
    Ok(CloudAsrTestResult {
        elapsed_ms: started.elapsed().as_millis() as u32,
        text,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{BufRead, BufReader, Read};
    use std::net::TcpListener;
    use std::sync::mpsc;

    fn settings(provider: &str, base: &str) -> (CloudAsrSettings, HashMap<String, String>) {
        let mut s = CloudAsrSettings {
            enabled: true,
            provider_id: provider.into(),
            ..Default::default()
        };
        s.base_urls.insert(provider.into(), base.into());
        let keys = HashMap::from([(provider.to_string(), "test-key".to_string())]);
        (s, keys)
    }

    struct Seen {
        request_line: String,
        headers: Vec<String>,
        body: Vec<u8>,
    }

    /// One-shot HTTP server: answers each request with the next reply, reports what it saw.
    fn mock(replies: Vec<&'static str>) -> (String, mpsc::Receiver<Seen>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let (tx, rx) = mpsc::channel();
        std::thread::spawn(move || {
            for reply in replies {
                let (stream, _) = listener.accept().unwrap();
                let mut reader = BufReader::new(stream.try_clone().unwrap());
                let mut request_line = String::new();
                reader.read_line(&mut request_line).unwrap();
                let mut headers = Vec::new();
                let mut length = 0usize;
                loop {
                    let mut line = String::new();
                    reader.read_line(&mut line).unwrap();
                    let line = line.trim_end().to_string();
                    if line.is_empty() {
                        break;
                    }
                    if let Some(v) = line.to_ascii_lowercase().strip_prefix("content-length:") {
                        length = v.trim().parse().unwrap();
                    }
                    headers.push(line);
                }
                let mut body = vec![0u8; length];
                reader.read_exact(&mut body).unwrap();
                // Record the request before replying, so the test sees it once the call returns.
                let _ = tx.send(Seen {
                    request_line: request_line.trim().into(),
                    headers,
                    body,
                });
                let mut stream = stream;
                let _ = write!(
                    stream,
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{reply}",
                    reply.len()
                );
            }
        });
        (base, rx)
    }

    fn run(
        provider: &str,
        replies: Vec<&'static str>,
        language: Option<&str>,
    ) -> (String, Vec<Seen>) {
        let (base, rx) = mock(replies);
        let (s, keys) = settings(provider, &base);
        let config = worker_config(&s, &keys).unwrap();
        let text = tauri::async_runtime::block_on(transcribe(
            &config,
            &[0.1; 1600],
            language,
            &["Handy".to_string()],
        ))
        .unwrap();
        (text, rx.try_iter().collect())
    }

    fn has_header(seen: &Seen, header: &str) -> bool {
        seen.headers
            .iter()
            .any(|h| h.to_ascii_lowercase() == header.to_ascii_lowercase())
    }

    #[test]
    fn openai_compatible_multipart() {
        let (text, seen) = run("openai", vec![r#"{"text":"你好"}"#], Some("zh-Hans"));
        assert_eq!(text, "你好");
        assert_eq!(seen[0].request_line, "POST /audio/transcriptions HTTP/1.1");
        assert!(has_header(&seen[0], "authorization: Bearer test-key"));
        let body = String::from_utf8_lossy(&seen[0].body);
        assert!(body.contains("name=\"model\"\r\n\r\ngpt-4o-mini-transcribe"));
        assert!(body.contains("name=\"language\"\r\n\r\nzh\r\n"));
        assert!(body.contains("name=\"prompt\"\r\n\r\nHandy"));
        assert!(body.contains("RIFF"));
    }

    #[test]
    fn azure_openai_uses_api_key_header() {
        let (text, seen) = run("azure_openai", vec![r#"{"text":"ok"}"#], None);
        assert_eq!(text, "ok");
        assert!(seen[0]
            .request_line
            .starts_with("POST /audio/transcriptions?api-version="));
        assert!(has_header(&seen[0], "api-key: test-key"));
    }

    #[test]
    fn azure_speech_fast_transcription() {
        let (text, seen) = run(
            "azure_speech",
            vec![r#"{"combinedPhrases":[{"text":"Hello."}]}"#],
            Some("en"),
        );
        assert_eq!(text, "Hello.");
        assert!(seen[0]
            .request_line
            .starts_with("POST /speechtotext/transcriptions:transcribe?api-version="));
        assert!(has_header(&seen[0], "ocp-apim-subscription-key: test-key"));
        assert!(String::from_utf8_lossy(&seen[0].body).contains("\"locales\":[\"en-US\"]"));
    }

    #[test]
    fn google_joins_results() {
        let (text, seen) = run(
            "google",
            vec![
                r#"{"results":[{"alternatives":[{"transcript":"第一句"}]},{"alternatives":[{"transcript":"第二句"}]}]}"#,
            ],
            Some("zh"),
        );
        assert_eq!(text, "第一句第二句");
        assert!(seen[0]
            .request_line
            .starts_with("POST /speech:recognize?key=test-key"));
        let body: Value = serde_json::from_slice(&seen[0].body).unwrap();
        assert_eq!(body["config"]["languageCode"], "zh-CN");
        assert_eq!(body["config"]["speechContexts"][0]["phrases"][0], "Handy");
    }

    #[test]
    fn deepgram_raw_audio_with_keyterms() {
        let (text, seen) = run(
            "deepgram",
            vec![r#"{"results":{"channels":[{"alternatives":[{"transcript":"hi"}]}]}}"#],
            None,
        );
        assert_eq!(text, "hi");
        assert!(seen[0].request_line.contains("detect_language=true"));
        assert!(seen[0].request_line.contains("keyterm=Handy"));
        assert!(has_header(&seen[0], "authorization: Token test-key"));
        assert_eq!(&seen[0].body[..4], b"RIFF");
    }

    #[test]
    fn assemblyai_uploads_then_polls() {
        let (text, seen) = run(
            "assemblyai",
            vec![
                r#"{"upload_url":"https://cdn.example/a"}"#,
                r#"{"id":"t1","status":"queued"}"#,
                r#"{"id":"t1","status":"processing"}"#,
                r#"{"id":"t1","status":"completed","text":"done"}"#,
            ],
            None,
        );
        assert_eq!(text, "done");
        assert_eq!(seen[0].request_line, "POST /upload HTTP/1.1");
        assert_eq!(seen[1].request_line, "POST /transcript HTTP/1.1");
        assert_eq!(seen[3].request_line, "GET /transcript/t1 HTTP/1.1");
        assert!(has_header(&seen[1], "authorization: test-key"));
    }

    #[test]
    fn elevenlabs_scribe() {
        let (text, seen) = run("elevenlabs", vec![r#"{"text":" hey "}"#], Some("en"));
        assert_eq!(text, "hey");
        assert_eq!(seen[0].request_line, "POST /speech-to-text HTTP/1.1");
        assert!(has_header(&seen[0], "xi-api-key: test-key"));
        assert!(String::from_utf8_lossy(&seen[0].body).contains("scribe_v1"));
    }

    #[test]
    fn dashscope_audio_in_chat() {
        let (text, seen) = run(
            "dashscope",
            vec![r#"{"choices":[{"message":{"content":"欢迎使用"}}]}"#],
            None,
        );
        assert_eq!(text, "欢迎使用");
        assert_eq!(seen[0].request_line, "POST /chat/completions HTTP/1.1");
        let body: Value = serde_json::from_slice(&seen[0].body).unwrap();
        assert_eq!(body["model"], "qwen3-asr-flash");
        assert!(body["messages"][1]["content"][0]["input_audio"]["data"]
            .as_str()
            .unwrap()
            .starts_with("data:audio/wav;base64,UklGR"));
    }

    #[test]
    fn http_errors_carry_the_service_message() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        std::thread::spawn(move || {
            let (mut s, _) = listener.accept().unwrap();
            let mut buf = [0u8; 65536];
            let _ = s.read(&mut buf);
            let body = r#"{"error":{"message":"Incorrect API key provided"}}"#;
            let _ = write!(
                s,
                "HTTP/1.1 401 Unauthorized\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            );
        });
        let (s, keys) = settings("groq", &base);
        let config = worker_config(&s, &keys).unwrap();
        let err = tauri::async_runtime::block_on(transcribe(&config, &[0.0; 1600], None, &[]))
            .unwrap_err();
        assert!(
            err.contains("401") && err.contains("Incorrect API key"),
            "{err}"
        );
    }

    #[test]
    fn config_requires_key_and_endpoint() {
        let s = CloudAsrSettings {
            provider_id: "deepgram".into(),
            ..Default::default()
        };
        assert!(worker_config(&s, &HashMap::new())
            .unwrap_err()
            .contains("API key"));
        let s = CloudAsrSettings {
            provider_id: "azure_openai".into(),
            ..Default::default()
        };
        let keys = HashMap::from([("azure_openai".to_string(), "k".to_string())]);
        assert!(worker_config(&s, &keys).unwrap_err().contains("endpoint"));
        let s = CloudAsrSettings {
            provider_id: "custom".into(),
            ..Default::default()
        };
        assert!(worker_config(&s, &HashMap::new()).is_ok());
        assert!(!format!("{:?}", worker_config(&s, &keys)).contains("\"k\""));
    }

    #[test]
    fn worker_protocol_answers() {
        let s = CloudAsrSettings {
            provider_id: "custom".into(),
            ..Default::default()
        };
        let config = worker_config(&s, &HashMap::new()).unwrap();
        let hello = handle(&config, &json!({"Hello": {"list_devices": true}}), &[]);
        assert_eq!(hello["Hello"]["devices"][0]["kind"], "remote");
        let loaded = handle(&config, &json!({"Load": {"path": "x"}}), &[]);
        assert_eq!(
            loaded["Loaded"]["capabilities"]["supports_streaming"],
            false
        );
        // Parsing as the app does proves the shapes match the protocol types.
        let _: crate::engine_supervisor::protocol::Response =
            serde_json::from_value(loaded).unwrap();
        let empty = handle(&config, &json!({"Run": {"options": {}}}), &[0.0; 100]);
        let _: crate::engine_supervisor::protocol::Response =
            serde_json::from_value(empty).unwrap();
        assert_eq!(handle(&config, &json!("StreamReset"), &[]), json!("Ok"));
        assert!(handle(&config, &json!({"Feed": null}), &[])["Error"].is_string());
    }

    #[test]
    fn locales_and_languages() {
        assert_eq!(locale(Some("zh-Hant")).as_deref(), Some("zh-TW"));
        assert_eq!(locale(Some("de")).as_deref(), Some("de-DE"));
        assert_eq!(locale(Some("auto")), None);
        assert_eq!(base_language(Some("zh-Hans")).as_deref(), Some("zh"));
        assert_eq!(
            join_parts(&["Hello", "world", "你好", "世界"]),
            "Hello world 你好世界"
        );
        assert_eq!(base64(b"Ma"), "TWE=");
        assert_eq!(base64(b"Man!"), "TWFuIQ==");
    }
}
