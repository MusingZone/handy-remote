// Injected before the app loads (Playwright addInitScript): a stand-in for the Tauri IPC so the
// real React UI renders in a plain browser. All data here is made up for the README animations.
(() => {
  const callbacks = new Map();
  const listeners = new Map();
  let nextId = 1;

  const binding = (id, name, key) => ({
    id,
    name,
    description: name,
    default_binding: key,
    current_binding: key,
  });

  const STRUCTURE_PROMPT =
    "You are a dictation editor. You receive text produced by speech recognition and return the same text, cleaned up and laid out for writing.\n\nRules:\n1. Same language as the input. Never translate.\n2. Keep the meaning, wording and order.\n5. When the speaker lists points with ordinal words (第一点/第二点, first/second/third), make a numbered list.\n\n${rules}\n\n<transcript>\n${output}\n</transcript>";

  const COPILOT_MODELS = ["example-small-instruct", "example-chat"];

  const providers = [
    { id: "openai", label: "OpenAI", base_url: "https://api.openai.com/v1" },
    {
      id: "ollama",
      label: "Ollama (local)",
      base_url: "http://localhost:11434/v1",
      allow_base_url_edit: true,
    },
    {
      id: "copilot_bridge",
      label: "GitHub Copilot (Copilot bridge)",
      base_url: "http://127.0.0.1:47870/v1",
      allow_base_url_edit: true,
    },
    {
      id: "custom",
      label: "Custom",
      base_url: "http://localhost:8080/v1",
      allow_base_url_edit: true,
    },
  ];

  const settings = {
    bindings: {
      transcribe: binding("transcribe", "Transcribe", "ctrl+space"),
      transcribe_with_post_process: binding(
        "transcribe_with_post_process",
        "Transcribe with post-processing",
        "ctrl+shift+space",
      ),
      cancel: binding("cancel", "Cancel", "escape"),
    },
    shortcut_activation: "toggle",
    hold_threshold_ms: 300,
    audio_feedback: true,
    audio_feedback_volume: 0.6,
    sound_theme: "marimba",
    start_hidden: false,
    autostart_enabled: true,
    update_checks_enabled: true,
    show_whats_new_on_update: false,
    whats_new_last_seen_version: "99.0.0",
    selected_model: "remote",
    onboarding_completed: true,
    always_on_microphone: false,
    selected_microphone: null,
    selected_channel: null,
    clamshell_microphone: null,
    selected_output_device: null,
    translate_to_english: false,
    selected_language: "auto",
    spoken_languages: ["en", "zh"],
    overlay_position: "bottom",
    overlay_style: "live",
    debug_mode: false,
    log_level: "info",
    custom_words: ["Handy Remote", "OpenVINO"],
    model_unload_timeout: "min_15",
    word_correction_threshold: 0.18,
    history_limit: 50,
    recording_retention_period: "preserve_limit",
    paste_method: "ctrl_v",
    clipboard_handling: "dont_modify",
    auto_submit: false,
    auto_submit_key: "enter",
    post_process_enabled: true,
    post_process_always: true,
    post_process_provider_id: "copilot_bridge",
    post_process_providers: providers,
    post_process_api_keys: {},
    post_process_models: { copilot_bridge: "example-small-instruct" },
    post_process_prompts: [
      {
        id: "handy_remote_polish_structure",
        name: "Polish & structure",
        prompt: STRUCTURE_PROMPT,
      },
    ],
    post_process_selected_prompt_id: "handy_remote_polish_structure",
    mute_while_recording: false,
    append_trailing_space: false,
    app_language: "en",
    theme: "light",
    experimental_enabled: false,
    lazy_stream_close: false,
    airpods_mode: false,
    keyboard_implementation: "tauri",
    show_tray_icon: true,
    paste_delay_ms: 60,
    paste_delay_after_ms: 60,
    reliable_paste: false,
    typing_tool: "auto",
    external_script_path: null,
    filler_word_removal_enabled: true,
    custom_filler_words: null,
    chinese_script: "simplified",
    transcribe_accelerator: "auto",
    transcribe_gpu_device: null,
    extra_recording_buffer_ms: 0,
    vad_enabled: true,
    vad_backend: "silero",
    long_form_chunking: false,
    language_detection: "when_needed",
    language_detection_window_secs: 3,
    remote: {
      enabled: true,
      host: "",
      user: "",
      ssh_port: 22,
      identity_file: "",
      ssh_path: "",
      server_port: 47821,
      host_key: "",
      label: "",
      incremental_paste: true,
      rdp_caret: true,
      caret_port: 47823,
    },
    cloud_asr: { enabled: false },
    cloud_asr_api_keys: {},
    refine_prompt_seeded: true,
    refine_rules_file: "",
  };

  let rules = {
    path: "D:\\Sync\\handy\\refine-rules.txt",
    text: "",
    count: 0,
  };

  const PAIRED = {
    host: "studio-pc",
    user: "alice",
    ssh_port: 22,
    server_port: 47821,
    host_key:
      "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleHostKeyForTheReadmeOnly",
    label: "studio-pc",
  };
  const CLIENT_KEY =
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleClientKeyForTheReadmeOnly handy-remote@laptop";

  const delay = (ms, value) =>
    new Promise((r) => setTimeout(() => r(value), ms));
  const countRules = (text) =>
    text.split(/\r?\n/).filter((l) => l.trim() && !l.trim().startsWith("#"))
      .length;

  // Mirrors cloud_asr::providers() in src-tauri/src/cloud_asr.rs.
  const cloudProvider = (id, label, base_url, models, extra = {}) => ({
    id,
    label,
    api: "open_ai",
    base_url,
    base_url_hint: "",
    model: models[0] ?? "",
    models,
    key_url: "",
    key_optional: false,
    max_audio_secs: 0,
    ...extra,
  });
  const CLOUD_PROVIDERS = [
    cloudProvider("openai", "OpenAI", "https://api.openai.com/v1", [
      "gpt-4o-mini-transcribe",
      "gpt-4o-transcribe",
      "whisper-1",
    ]),
    cloudProvider("azure_openai", "Azure OpenAI", "", [], {
      base_url_hint:
        "https://<resource>.openai.azure.com/openai/deployments/<deployment>",
    }),
    cloudProvider(
      "azure_speech",
      "Azure AI Speech",
      "https://eastus.api.cognitive.microsoft.com",
      [],
    ),
    cloudProvider(
      "google",
      "Google Cloud Speech-to-Text",
      "https://speech.googleapis.com/v1",
      ["latest_long", "latest_short", "default"],
    ),
    cloudProvider("deepgram", "Deepgram", "https://api.deepgram.com/v1", [
      "nova-3",
      "nova-2",
    ]),
    cloudProvider("assemblyai", "AssemblyAI", "https://api.assemblyai.com/v2", [
      "universal",
      "slam-1",
    ]),
    cloudProvider(
      "elevenlabs",
      "ElevenLabs Scribe",
      "https://api.elevenlabs.io/v1",
      ["scribe_v1"],
    ),
    cloudProvider("groq", "Groq", "https://api.groq.com/openai/v1", [
      "whisper-large-v3-turbo",
      "whisper-large-v3",
    ]),
    cloudProvider("mistral", "Mistral Voxtral", "https://api.mistral.ai/v1", [
      "voxtral-mini-latest",
    ]),
    cloudProvider(
      "dashscope",
      "Alibaba Qwen3-ASR (DashScope)",
      "https://dashscope.aliyuncs.com/compatible-mode/v1",
      ["qwen3-asr-flash"],
    ),
    cloudProvider(
      "siliconflow",
      "SiliconFlow",
      "https://api.siliconflow.cn/v1",
      ["FunAudioLLM/SenseVoiceSmall", "TeleAI/TeleSpeechASR"],
    ),
    cloudProvider(
      "custom",
      "Custom (OpenAI-compatible)",
      "http://localhost:8000/v1",
      [],
      { key_optional: true, model: "" },
    ),
  ];

  const handlers = {
    get_app_settings: () => settings,
    cloud_asr_providers: () => CLOUD_PROVIDERS,
    change_cloud_asr_settings: ({ cloud, apiKey }) => {
      settings.cloud_asr = cloud;
      if (apiKey)
        settings.cloud_asr_api_keys = {
          ...settings.cloud_asr_api_keys,
          [cloud.provider_id]: "set",
        };
      return null;
    },
    cloud_asr_test: () =>
      delay(800, { elapsed_ms: 640, text: "Testing, one, two, three." }),
    get_default_settings: () => settings,
    remote_detect_ssh: () => "C:\\Windows\\System32\\OpenSSH\\ssh.exe",
    remote_client_key: ({ create }) => (create ? CLIENT_KEY : null),
    remote_apply_pairing: ({ remote }) => ({ ...remote, ...PAIRED }),
    remote_test_connection: () =>
      delay(1000, {
        connect_ms: 182,
        round_trip_ms: 24,
        label: "studio-pc",
        description: "Qwen3-ASR 1.7B on GPU, 0.6B previews on NPU",
        loaded: true,
        openvino: "2026.0",
        old_server: false,
      }),
    change_remote_settings: ({ remote }) => {
      settings.remote = remote;
      return null;
    },
    refine_rules_get: () => rules,
    refine_rules_set: ({ text }) => {
      rules = { ...rules, text, count: countRules(text) };
      return rules;
    },
    refine_detect_local_llms: () =>
      delay(800, [
        {
          provider_id: "copilot_bridge",
          label: "GitHub Copilot (Copilot bridge)",
          base_url: "http://127.0.0.1:47870/v1",
          models: COPILOT_MODELS,
          recommended: "example-small-instruct",
        },
        {
          provider_id: "ollama",
          label: "Ollama (local)",
          base_url: "http://localhost:11434/v1",
          models: ["qwen3:8b", "llama3.2:3b"],
          recommended: "qwen3:8b",
        },
      ]),
    refine_benchmark: () =>
      delay(1500, {
        provider_id: "copilot_bridge",
        model: "example-small-instruct",
        median_ms: 3200,
        runs_ms: [3410, 3200, 3050],
        output:
          "So the release went fine:\n1. The installer is signed.\n2. The updater verifies the signature.\n3. The server deploys with a rollback.",
      }),
    fetch_post_process_models: ({ providerId }) =>
      providerId === "copilot_bridge" ? COPILOT_MODELS : [],
    get_available_microphones: () => [
      { index: "0", name: "Microphone Array", is_default: true },
    ],
    get_available_output_devices: () => [
      { index: "0", name: "Speakers", is_default: true },
    ],
    get_retired_models: () => [],
    get_available_models: () => [],
    get_current_model: () => "remote",
    get_active_notices: () => [],
    get_windows_microphone_permission_status: () => ({
      supported: true,
      overall_access: "allowed",
      device_access: "allowed",
      app_access: "allowed",
      desktop_app_access: "allowed",
    }),
    is_update_checks_locked: () => false,
    check_custom_sounds: () => ({ start: false, stop: false }),
    "plugin:app|version": () => "0.10.1-remote.3",
    "plugin:app|name": () => "Handy Remote",
    "plugin:os|locale": () => "en-US",
  };

  window.__mockCalls = [];

  window.__TAURI_OS_PLUGIN_INTERNALS__ = {
    platform: "windows",
    os_type: "windows",
    family: "windows",
    version: "10.0.26100",
    arch: "x86_64",
    eol: "\r\n",
    exe_extension: "exe",
  };

  window.__TAURI_EVENT_PLUGIN_INTERNALS__ = {
    unregisterListener: (event, id) => {
      const list = listeners.get(event) || [];
      listeners.set(
        event,
        list.filter((h) => h !== id),
      );
    },
  };

  window.__TAURI_INTERNALS__ = {
    metadata: {
      currentWindow: { label: "main" },
      currentWebview: { windowLabel: "main", label: "main" },
    },
    plugins: {},
    transformCallback(cb, once) {
      const id = nextId++;
      callbacks.set(id, (data) => {
        if (once) callbacks.delete(id);
        return cb && cb(data);
      });
      return id;
    },
    unregisterCallback(id) {
      callbacks.delete(id);
    },
    runCallback(id, data) {
      const cb = callbacks.get(id);
      if (cb) cb(data);
    },
    convertFileSrc: (p) => p,
    async invoke(cmd, args = {}) {
      window.__mockCalls.push(cmd);
      if (cmd === "plugin:event|listen") {
        const list = listeners.get(args.event) || [];
        list.push(args.handler);
        listeners.set(args.event, list);
        return args.handler;
      }
      if (cmd === "plugin:event|unlisten") return null;
      if (cmd === "plugin:event|emit" || cmd === "plugin:event|emit_to") {
        window.__emit(args.event, args.payload);
        return null;
      }
      const h = handlers[cmd];
      if (h) return h(args);
      if (/^get_\w*(notices|models|devices|entries|jobs)$/.test(cmd)) return [];
      return null;
    },
  };

  // Drives event listeners (overlay text, phases, mic levels) from the capture script.
  window.__emit = (event, payload) => {
    for (const id of listeners.get(event) || []) {
      const cb = callbacks.get(id);
      if (cb) cb({ event, id, payload });
    }
  };
})();
