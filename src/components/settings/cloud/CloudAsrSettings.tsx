import React, { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { openUrl } from "@tauri-apps/plugin-opener";
import { commands } from "@/bindings";
import type {
  CloudAsrProvider,
  CloudAsrSettings as Cloud,
  CloudAsrTestResult,
} from "@/bindings";
import { useSettings } from "../../../hooks/useSettings";
import { SettingsGroup } from "../../ui/SettingsGroup";
import { SettingContainer } from "../../ui/SettingContainer";
import { ToggleSwitch } from "../../ui/ToggleSwitch";
import { Input } from "../../ui/Input";
import { Button } from "../../ui/Button";
import { Dropdown } from "../../ui/Dropdown";

type Draft = {
  enabled: boolean;
  provider_id: string;
  base_urls: Record<string, string>;
  models: Record<string, string>;
  timeout_secs: number;
};

const DEFAULTS: Draft = {
  enabled: false,
  provider_id: "openai",
  base_urls: {},
  models: {},
  timeout_secs: 60,
};

const complete = (c: Cloud | undefined): Draft =>
  ({
    ...DEFAULTS,
    ...Object.fromEntries(
      Object.entries(c ?? {}).filter(([, v]) => v !== undefined && v !== null),
    ),
  }) as Draft;

type TestState =
  | { kind: "idle" }
  | { kind: "running" }
  | { kind: "ok"; result: CloudAsrTestResult }
  | { kind: "error"; message: string };

export const CloudAsrSettings: React.FC = () => {
  const { t } = useTranslation();
  const { getSetting, refreshSettings } = useSettings();
  const savedJson = JSON.stringify(complete(getSetting("cloud_asr")));
  const saved = useMemo<Draft>(() => JSON.parse(savedJson), [savedJson]);
  const keys = (getSetting("cloud_asr_api_keys") ?? {}) as Record<
    string,
    string | undefined
  >;
  const [providers, setProviders] = useState<CloudAsrProvider[]>([]);
  const [draft, setDraft] = useState<Draft>(saved);
  // A new key typed for the selected provider; null keeps the stored one.
  const [apiKey, setApiKey] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [test, setTest] = useState<TestState>({ kind: "idle" });

  useEffect(() => setDraft(saved), [saved]);
  useEffect(() => {
    commands.cloudAsrProviders().then(setProviders).catch(console.error);
  }, []);

  const id = draft.provider_id;
  const provider = providers.find((p) => p.id === id);
  const hasKey = Boolean(keys[id]);
  const dirty = JSON.stringify(draft) !== savedJson || apiKey !== null;

  const edit = (patch: Partial<Draft>) => {
    setDraft((d) => ({ ...d, ...patch }));
    setMessage(null);
    setTest({ kind: "idle" });
  };
  const setPerProvider = (field: "base_urls" | "models", value: string) =>
    edit({ [field]: { ...draft[field], [id]: value } });

  const save = async (next: Draft) => {
    setBusy(true);
    try {
      const result = await commands.changeCloudAsrSettings(next, apiKey);
      if (result.status === "ok") {
        setApiKey(null);
        setMessage(t("settings.cloudAsr.saved"));
      } else {
        setMessage(result.error);
      }
      // Switching recognition also changes the selected model and the remote setting.
      await refreshSettings();
    } finally {
      setBusy(false);
    }
  };

  const runTest = async () => {
    setTest({ kind: "running" });
    const result = await commands.cloudAsrTest(draft, apiKey);
    setTest(
      result.status === "ok"
        ? { kind: "ok", result: result.data }
        : { kind: "error", message: result.error },
    );
  };

  const testResult = () => {
    switch (test.kind) {
      case "idle":
        return null;
      case "running":
        return <p className="text-sm">{t("settings.cloudAsr.test.running")}</p>;
      case "error":
        return (
          <p className="text-sm text-error whitespace-pre-wrap">
            {t("settings.cloudAsr.test.failed", { error: test.message })}
          </p>
        );
      case "ok":
        return (
          <p className="text-sm">
            {t("settings.cloudAsr.test.ok", {
              ms: test.result.elapsed_ms,
              text: test.result.text || "-",
            })}
          </p>
        );
    }
  };

  return (
    <div className="max-w-3xl w-full mx-auto space-y-6">
      <div className="px-4 text-sm space-y-2">
        <p>{t("settings.cloudAsr.intro")}</p>
        <p>{t("settings.cloudAsr.privacy")}</p>
      </div>

      <SettingsGroup title={t("settings.cloudAsr.groups.recognition")}>
        <ToggleSwitch
          checked={saved.enabled}
          onChange={(enabled) => save({ ...draft, enabled })}
          isUpdating={busy}
          label={t("settings.cloudAsr.enabled.label")}
          description={t("settings.cloudAsr.enabled.description")}
          grouped
        />
      </SettingsGroup>

      <SettingsGroup title={t("settings.cloudAsr.groups.service")}>
        <SettingContainer
          title={t("settings.cloudAsr.provider.label")}
          description={t("settings.cloudAsr.provider.description")}
          grouped
        >
          <Dropdown
            className="min-w-64"
            options={providers.map((p) => ({ value: p.id, label: p.label }))}
            selectedValue={draft.provider_id}
            onSelect={(provider_id) => {
              setApiKey(null);
              edit({ provider_id });
            }}
          />
        </SettingContainer>
        <SettingContainer
          title={t("settings.cloudAsr.base_url.label")}
          description={t("settings.cloudAsr.base_url.description")}
          grouped
          grow
        >
          <Input
            className="w-full"
            value={draft.base_urls[id] ?? ""}
            placeholder={provider?.base_url || provider?.base_url_hint || ""}
            spellCheck={false}
            onChange={(e) => setPerProvider("base_urls", e.target.value)}
          />
        </SettingContainer>
        <SettingContainer
          title={t("settings.cloudAsr.model.label")}
          description={t("settings.cloudAsr.model.description")}
          grouped
          grow
        >
          <Input
            className="w-full"
            value={draft.models[id] ?? ""}
            placeholder={provider?.model || ""}
            spellCheck={false}
            list="cloud-asr-models"
            onChange={(e) => setPerProvider("models", e.target.value)}
          />
          <datalist id="cloud-asr-models">
            {(provider?.models ?? []).map((m) => (
              <option key={m} value={m} />
            ))}
          </datalist>
        </SettingContainer>
        <SettingContainer
          title={t("settings.cloudAsr.api_key.label")}
          description={
            provider?.key_optional
              ? t("settings.cloudAsr.api_key.optional")
              : t("settings.cloudAsr.api_key.description")
          }
          grouped
          grow
        >
          <div className="flex w-full items-center gap-2">
            <Input
              className="flex-1 min-w-0"
              type="password"
              autoComplete="off"
              value={apiKey ?? ""}
              placeholder={
                hasKey
                  ? t("settings.cloudAsr.api_key.saved")
                  : t("settings.cloudAsr.api_key.placeholder")
              }
              spellCheck={false}
              onChange={(e) => {
                setApiKey(e.target.value);
                setMessage(null);
              }}
            />
            {provider?.key_url && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => openUrl(provider.key_url).catch(console.error)}
              >
                {t("settings.cloudAsr.api_key.get")}
              </Button>
            )}
          </div>
        </SettingContainer>
        <SettingContainer
          title={t("settings.cloudAsr.timeout.label")}
          description={t("settings.cloudAsr.timeout.description")}
          grouped
        >
          <Input
            className="w-24"
            inputMode="numeric"
            value={String(draft.timeout_secs)}
            onChange={(e) => {
              const n = Number.parseInt(e.target.value, 10);
              edit({ timeout_secs: Number.isInteger(n) && n > 0 ? n : 60 });
            }}
          />
        </SettingContainer>
        <div className="px-4 py-3 space-y-2">
          <div className="flex flex-wrap gap-2">
            <Button disabled={!dirty || busy} onClick={() => save(draft)}>
              {t("settings.cloudAsr.save")}
            </Button>
            <Button
              variant="secondary"
              disabled={!dirty}
              onClick={() => {
                setDraft(saved);
                setApiKey(null);
                setMessage(null);
              }}
            >
              {t("settings.cloudAsr.revert")}
            </Button>
            <Button
              variant="secondary"
              disabled={test.kind === "running"}
              onClick={runTest}
            >
              {t("settings.cloudAsr.test.button")}
            </Button>
          </div>
          {message && <p className="text-sm">{message}</p>}
          {testResult()}
        </div>
      </SettingsGroup>
    </div>
  );
};
