import React, { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { commands } from "@/bindings";
import type {
  LocalLlmEndpoint,
  RefineBenchmark,
  RefineRules,
} from "@/bindings";
import { useSettings } from "../../../hooks/useSettings";
import { SettingContainer } from "../../ui/SettingContainer";
import { ToggleSwitch } from "../../ui/ToggleSwitch";
import { Button } from "../../ui/Button";
import { Dropdown } from "../../ui/Dropdown";
import { Input } from "../../ui/Input";
import { Textarea } from "../../ui/Textarea";

/** Fork: the user's own refine rules, kept in a plain text file (one rule per line). */
const RefineRulesEditor: React.FC = () => {
  const { t } = useTranslation();
  const [rules, setRules] = useState<RefineRules | null>(null);
  const [text, setText] = useState("");
  const [file, setFile] = useState("");
  const [status, setStatus] = useState<string | null>(null);

  const load = (r: RefineRules) => {
    setRules(r);
    setText(r.text);
    setFile(r.path);
  };

  useEffect(() => {
    commands
      .refineRulesGet()
      .then((r) => r.status === "ok" && load(r.data))
      .catch(console.error);
  }, []);

  const save = async () => {
    const r = await commands.refineRulesSet(text);
    if (r.status === "ok") {
      load(r.data);
      setStatus(
        t("settings.postProcessing.refine.rules.saved", {
          count: r.data.count,
        }),
      );
    } else setStatus(r.error);
  };

  const applyFile = async () => {
    const r = await commands.changeRefineRulesFileSetting(file.trim());
    if (r.status === "ok") {
      load(r.data);
      setStatus(null);
    } else setStatus(r.error);
  };

  return (
    <SettingContainer
      title={t("settings.postProcessing.refine.rules.label")}
      description={t("settings.postProcessing.refine.rules.description")}
      descriptionMode="tooltip"
      layout="stacked"
      grouped
    >
      <div className="space-y-2">
        <Textarea
          className="w-full font-mono"
          rows={6}
          value={text}
          placeholder={t("settings.postProcessing.refine.rules.placeholder")}
          onChange={(e) => {
            setText(e.target.value);
            setStatus(null);
          }}
        />
        <div className="flex flex-wrap items-center gap-2">
          <Button
            onClick={save}
            variant="primary"
            size="sm"
            disabled={rules !== null && text === rules.text}
          >
            {t("settings.postProcessing.refine.rules.save")}
          </Button>
          {status && <span className="text-sm text-ink-3">{status}</span>}
        </div>
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="text-ink-3">
            {t("settings.postProcessing.refine.rules.file")}
          </span>
          <Input
            variant="compact"
            className="min-w-80 flex-1 font-mono text-xs"
            value={file}
            onChange={(e) => setFile(e.target.value)}
          />
          <Button
            onClick={applyFile}
            variant="secondary"
            size="sm"
            disabled={rules !== null && file === rules.path}
          >
            {t("settings.postProcessing.refine.rules.useFile")}
          </Button>
        </div>
      </div>
    </SettingContainer>
  );
};

/** Fork: refine every dictation, find local LLM servers, and time the selected model. */
export const RefineSettings: React.FC = () => {
  const { t } = useTranslation();
  const {
    getSetting,
    updateSetting,
    isUpdating,
    setPostProcessProvider,
    updatePostProcessBaseUrl,
    updatePostProcessModel,
    fetchPostProcessModels,
    settings,
  } = useSettings();
  const [found, setFound] = useState<LocalLlmEndpoint[] | null>(null);
  const [picked, setPicked] = useState<Record<string, string>>({});
  const [detecting, setDetecting] = useState(false);
  const [bench, setBench] = useState<
    | { kind: "idle" }
    | { kind: "running" }
    | { kind: "ok"; result: RefineBenchmark }
    | { kind: "error"; message: string }
  >({ kind: "idle" });

  const detect = async () => {
    setDetecting(true);
    try {
      const list = await commands.refineDetectLocalLlms();
      setFound(list);
      setPicked(
        Object.fromEntries(
          list.map((e) => [e.base_url, e.recommended ?? e.models[0] ?? ""]),
        ),
      );
    } catch (e) {
      console.error(e);
      setFound([]);
    } finally {
      setDetecting(false);
    }
  };

  const use = async (e: LocalLlmEndpoint) => {
    const model = picked[e.base_url] ?? "";
    await setPostProcessProvider(e.provider_id);
    const current = settings?.post_process_providers?.find(
      (p) => p.id === e.provider_id,
    );
    if (current && current.base_url !== e.base_url) {
      await updatePostProcessBaseUrl(e.provider_id, e.base_url);
    }
    if (model) await updatePostProcessModel(e.provider_id, model);
    void fetchPostProcessModels(e.provider_id);
    setBench({ kind: "idle" });
  };

  const runBenchmark = async () => {
    setBench({ kind: "running" });
    try {
      const r = await commands.refineBenchmark(3);
      setBench(
        r.status === "ok"
          ? { kind: "ok", result: r.data }
          : { kind: "error", message: r.error },
      );
    } catch (e) {
      setBench({ kind: "error", message: String(e) });
    }
  };

  const activeId = settings?.post_process_provider_id;

  return (
    <>
      <ToggleSwitch
        checked={getSetting("post_process_always") ?? false}
        onChange={(v) => updateSetting("post_process_always", v)}
        isUpdating={isUpdating("post_process_always")}
        label={t("settings.postProcessing.refine.always.label")}
        description={t("settings.postProcessing.refine.always.description")}
        descriptionMode="tooltip"
        grouped
      />
      <SettingContainer
        title={t("settings.postProcessing.refine.detect.label")}
        description={t("settings.postProcessing.refine.detect.description")}
        descriptionMode="tooltip"
        layout="stacked"
        grouped
      >
        <div className="space-y-2">
          <Button
            onClick={detect}
            variant="secondary"
            size="md"
            disabled={detecting}
          >
            {detecting
              ? t("settings.postProcessing.refine.detect.running")
              : t("settings.postProcessing.refine.detect.button")}
          </Button>
          {found !== null && found.length === 0 && (
            <p className="text-sm text-ink-3">
              {t("settings.postProcessing.refine.detect.none")}
            </p>
          )}
          {found?.map((e) => (
            <div
              key={e.base_url}
              className="flex flex-wrap items-center gap-2 text-sm"
            >
              <span className="font-semibold">{e.label}</span>
              <code className="text-xs text-ink-3">{e.base_url}</code>
              <Dropdown
                className="min-w-56"
                selectedValue={picked[e.base_url] ?? null}
                options={e.models.map((m) => ({
                  value: m,
                  label:
                    m === e.recommended
                      ? `${m} (${t("settings.postProcessing.refine.detect.recommended")})`
                      : m,
                }))}
                onSelect={(m) =>
                  setPicked((p) => ({ ...p, [e.base_url]: m ?? "" }))
                }
              />
              <Button
                onClick={() => use(e)}
                variant={activeId === e.provider_id ? "secondary" : "primary"}
                size="sm"
              >
                {t("settings.postProcessing.refine.detect.use")}
              </Button>
            </div>
          ))}
        </div>
      </SettingContainer>
      <SettingContainer
        title={t("settings.postProcessing.refine.benchmark.label")}
        description={t("settings.postProcessing.refine.benchmark.description")}
        descriptionMode="tooltip"
        layout="stacked"
        grouped
      >
        <div className="space-y-2">
          <Button
            onClick={runBenchmark}
            variant="secondary"
            size="md"
            disabled={bench.kind === "running"}
          >
            {bench.kind === "running"
              ? t("settings.postProcessing.refine.benchmark.running")
              : t("settings.postProcessing.refine.benchmark.button")}
          </Button>
          {bench.kind === "ok" && (
            <div className="space-y-1 text-sm">
              <p>
                {t("settings.postProcessing.refine.benchmark.result", {
                  model: bench.result.model,
                  ms: bench.result.median_ms,
                  runs: bench.result.runs_ms.join(" / "),
                })}
              </p>
              <pre className="whitespace-pre-wrap rounded-md border border-line bg-wash p-2 text-xs">
                {bench.result.output}
              </pre>
            </div>
          )}
          {bench.kind === "error" && (
            <p className="text-sm text-red-500">{bench.message}</p>
          )}
        </div>
      </SettingContainer>
      <RefineRulesEditor />
    </>
  );
};
