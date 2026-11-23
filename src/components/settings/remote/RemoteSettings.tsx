import React, { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { openUrl } from "@tauri-apps/plugin-opener";
import { commands } from "@/bindings";
import type { RemoteSettings as Remote, RemoteTestResult } from "@/bindings";
import { useSettings } from "../../../hooks/useSettings";
import { useOsType } from "../../../hooks/useOsType";
import { SettingsGroup } from "../../ui/SettingsGroup";
import { SettingContainer } from "../../ui/SettingContainer";
import { ToggleSwitch } from "../../ui/ToggleSwitch";
import { Input } from "../../ui/Input";
import { Button } from "../../ui/Button";

const SETUP_GUIDE_URL =
  "https://github.com/MusingZone/handy-remote/blob/main/docs/remote/CLIENT-SETUP.md";

type Draft = Required<Remote>;

const DEFAULTS: Draft = {
  enabled: false,
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
  rdp_dictation: false,
};

const complete = (r: Remote | undefined): Draft => ({
  ...DEFAULTS,
  ...Object.fromEntries(
    Object.entries(r ?? {}).filter(([, v]) => v !== undefined && v !== null),
  ),
});

const parsePort = (text: string, fallback: number) => {
  const n = Number.parseInt(text, 10);
  return Number.isInteger(n) && n > 0 && n < 65536 ? n : fallback;
};

type TestState =
  | { kind: "idle" }
  | { kind: "running" }
  | { kind: "ok"; result: RemoteTestResult }
  | { kind: "error"; message: string };

export const RemoteSettings: React.FC = () => {
  const { t } = useTranslation();
  const { getSetting, updateSetting, isUpdating } = useSettings();
  const osType = useOsType();
  const savedJson = JSON.stringify(complete(getSetting("remote")));
  const saved = useMemo<Draft>(() => JSON.parse(savedJson), [savedJson]);
  const [draft, setDraft] = useState<Draft>(saved);
  const [pairing, setPairing] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [test, setTest] = useState<TestState>({ kind: "idle" });
  const [detectedSsh, setDetectedSsh] = useState<string | null>(null);
  const [clientKey, setClientKey] = useState<string | null | undefined>(
    undefined,
  );
  const [copied, setCopied] = useState(false);

  useEffect(() => setDraft(saved), [saved]);

  useEffect(() => {
    commands.remoteDetectSsh().then(setDetectedSsh).catch(console.error);
    commands
      .remoteClientKey(saved, false)
      .then((r) => setClientKey(r.status === "ok" ? r.data : null))
      .catch(console.error);
    // The key lives in the app data folder; later edits don't change it.
  }, []);

  const dirty = JSON.stringify(draft) !== savedJson;
  const updating = isUpdating("remote");
  const edit = (patch: Partial<Draft>) => {
    setDraft((d) => ({ ...d, ...patch }));
    setMessage(null);
  };
  // Toggles save the whole draft, so pending edits are kept rather than reset.
  const save = async (next: Draft) => {
    await updateSetting("remote", next);
    setMessage(t("settings.remote.saved"));
  };

  const applyPairing = async () => {
    const result = await commands.remoteApplyPairing(draft, pairing.trim());
    if (result.status === "ok") {
      setDraft(complete(result.data));
      setPairing("");
      setMessage(t("settings.remote.pairing.applied"));
    } else {
      setMessage(t("settings.remote.pairing.invalid", { error: result.error }));
    }
  };

  const runTest = async () => {
    setTest({ kind: "running" });
    const result = await commands.remoteTestConnection(draft);
    setTest(
      result.status === "ok"
        ? { kind: "ok", result: result.data }
        : { kind: "error", message: result.error },
    );
  };

  const createKey = async () => {
    const result = await commands.remoteClientKey(draft, true);
    if (result.status === "ok") setClientKey(result.data);
    else setMessage(result.error);
  };

  const authorizeCommand = clientKey
    ? `& "$env:LOCALAPPDATA\\handy-remote-server\\.venv\\Scripts\\python.exe" doctor.py authorize "${clientKey}"`
    : "";
  const copyAuthorize = async () => {
    await navigator.clipboard.writeText(authorizeCommand);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  const textField = (
    key: "host" | "user" | "identity_file" | "ssh_path",
    placeholder?: string,
  ) => (
    <SettingContainer
      title={t(`settings.remote.${key}.label`)}
      description={t(`settings.remote.${key}.description`)}
      grouped
      grow
    >
      <Input
        className="w-full"
        value={draft[key]}
        placeholder={placeholder}
        spellCheck={false}
        onChange={(e) => edit({ [key]: e.target.value } as Partial<Draft>)}
      />
    </SettingContainer>
  );

  const portField = (key: "ssh_port" | "server_port" | "caret_port") => (
    <SettingContainer
      title={t(`settings.remote.${key}.label`)}
      description={t(`settings.remote.${key}.description`)}
      grouped
    >
      <Input
        className="w-24"
        inputMode="numeric"
        value={String(draft[key])}
        onChange={(e) =>
          edit({
            [key]: parsePort(e.target.value, DEFAULTS[key]),
          } as Partial<Draft>)
        }
      />
    </SettingContainer>
  );

  const testResult = () => {
    switch (test.kind) {
      case "idle":
        return null;
      case "running":
        return <p className="text-sm">{t("settings.remote.test.running")}</p>;
      case "error":
        return (
          <p className="text-sm text-error whitespace-pre-wrap">
            {t("settings.remote.test.failed", { error: test.message })}
          </p>
        );
      case "ok": {
        const r = test.result;
        const lines = [
          t("settings.remote.test.connected", {
            connect: r.connect_ms,
            roundTrip: r.round_trip_ms ?? "?",
          }),
        ];
        if (r.old_server) lines.push(t("settings.remote.test.oldServer"));
        if (r.label || r.description) {
          lines.push(
            [r.label, r.description].filter(Boolean).join(" — ") +
              (r.openvino ? ` (OpenVINO ${r.openvino})` : ""),
          );
        }
        if (r.loaded === true) lines.push(t("settings.remote.test.loaded"));
        if (r.loaded === false) lines.push(t("settings.remote.test.sleeping"));
        return (
          <div className="text-sm space-y-1">
            {lines.map((line) => (
              <p key={line}>{line}</p>
            ))}
          </div>
        );
      }
    }
  };

  return (
    <div className="max-w-3xl w-full mx-auto space-y-6">
      <div className="px-4 text-sm space-y-2">
        <p>{t("settings.remote.intro")}</p>
        <button
          className="text-logo-primary underline cursor-pointer"
          onClick={() => openUrl(SETUP_GUIDE_URL).catch(console.error)}
        >
          {t("settings.remote.guide")}
        </button>
      </div>

      <SettingsGroup title={t("settings.remote.groups.recognition")}>
        <ToggleSwitch
          checked={saved.enabled}
          onChange={(enabled) => save({ ...draft, enabled })}
          isUpdating={updating}
          label={t("settings.remote.enabled.label")}
          description={t("settings.remote.enabled.description")}
          grouped
        />
        <ToggleSwitch
          checked={saved.incremental_paste}
          onChange={(incremental_paste) =>
            save({ ...draft, incremental_paste })
          }
          isUpdating={updating}
          label={t("settings.remote.incremental_paste.label")}
          description={t("settings.remote.incremental_paste.description")}
          grouped
        />
        {osType === "windows" && (
          <ToggleSwitch
            checked={saved.rdp_caret}
            onChange={(rdp_caret) => save({ ...draft, rdp_caret })}
            isUpdating={updating}
            label={t("settings.remote.rdp_caret.label")}
            description={t("settings.remote.rdp_caret.description")}
            grouped
          />
        )}
        {osType === "windows" && saved.rdp_caret && (
          <ToggleSwitch
            checked={saved.rdp_dictation}
            onChange={(rdp_dictation) => save({ ...draft, rdp_dictation })}
            isUpdating={updating}
            label={t("settings.remote.rdp_dictation.label")}
            description={t("settings.remote.rdp_dictation.description")}
            grouped
          />
        )}
      </SettingsGroup>

      <SettingsGroup title={t("settings.remote.groups.connection")}>
        <SettingContainer
          title={t("settings.remote.pairing.label")}
          description={t("settings.remote.pairing.description")}
          grouped
          grow
        >
          <div className="flex w-full gap-2">
            <Input
              className="flex-1 min-w-0"
              value={pairing}
              placeholder="handy-remote-pair:…"
              spellCheck={false}
              onChange={(e) => setPairing(e.target.value)}
            />
            <Button
              variant="secondary"
              disabled={!pairing.trim()}
              onClick={applyPairing}
            >
              {t("settings.remote.pairing.apply")}
            </Button>
          </div>
        </SettingContainer>
        {textField("host", "localhost")}
        {textField("user")}
        {portField("ssh_port")}
        {portField("server_port")}
        {textField("identity_file", t("settings.remote.automatic"))}
        {textField(
          "ssh_path",
          detectedSsh ?? t("settings.remote.ssh_path.missing"),
        )}
        <SettingContainer
          title={t("settings.remote.host_key.label")}
          description={t("settings.remote.host_key.description")}
          grouped
          grow
        >
          <div className="flex w-full min-w-0 items-center justify-end gap-2">
            <span className="text-xs truncate" title={draft.host_key}>
              {draft.host_key || t("settings.remote.host_key.none")}
            </span>
            {draft.host_key && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => edit({ host_key: "" })}
              >
                {t("settings.remote.host_key.clear")}
              </Button>
            )}
          </div>
        </SettingContainer>
        {osType === "windows" && draft.rdp_caret && portField("caret_port")}
        <div className="px-4 py-3 space-y-2">
          <div className="flex flex-wrap gap-2">
            <Button disabled={!dirty || updating} onClick={() => save(draft)}>
              {t("settings.remote.save")}
            </Button>
            <Button
              variant="secondary"
              disabled={!dirty}
              onClick={() => {
                setDraft(saved);
                setMessage(null);
              }}
            >
              {t("settings.remote.revert")}
            </Button>
            <Button
              variant="secondary"
              disabled={test.kind === "running"}
              onClick={runTest}
            >
              {t("settings.remote.test.button")}
            </Button>
          </div>
          {message && <p className="text-sm">{message}</p>}
          {testResult()}
        </div>
      </SettingsGroup>

      <SettingsGroup title={t("settings.remote.groups.key")}>
        <SettingContainer
          title={t("settings.remote.client_key.label")}
          description={t("settings.remote.client_key.description")}
          grouped
          layout="stacked"
          descriptionMode="inline"
        >
          {clientKey ? (
            <div className="space-y-2">
              <code className="block text-xs break-all p-2 bg-wash rounded select-text">
                {authorizeCommand}
              </code>
              <Button variant="secondary" size="sm" onClick={copyAuthorize}>
                {copied
                  ? t("settings.remote.client_key.copied")
                  : t("settings.remote.client_key.copy")}
              </Button>
            </div>
          ) : (
            <div className="flex items-center gap-2">
              <span className="text-sm">
                {t("settings.remote.client_key.none")}
              </span>
              <Button
                variant="secondary"
                size="sm"
                disabled={clientKey === undefined}
                onClick={createKey}
              >
                {t("settings.remote.client_key.create")}
              </Button>
            </div>
          )}
        </SettingContainer>
      </SettingsGroup>
    </div>
  );
};
