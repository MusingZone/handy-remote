import React, { useState } from "react";
import { open } from "@tauri-apps/plugin-dialog";
import { useTranslation } from "react-i18next";
import {
  commands,
  type PostProcessAuth,
  type PostProcessProvider,
} from "@/bindings";
import { useSettings } from "../../../hooks/useSettings";
import { notify } from "@/stores/noticeStore";
import { Dropdown, SettingContainer } from "@/components/ui";
import { Button } from "../../ui/Button";

export const isUserEndpoint = (id: string | undefined) =>
  id === "custom" || (id?.startsWith("custom_") ?? false);

export const ImportEndpointsButton: React.FC = () => {
  const { t } = useTranslation();
  const { refreshSettings } = useSettings();
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<string | null>(null);

  const importFile = async () => {
    setBusy(true);
    setStatus(null);
    try {
      const path = await open({
        multiple: false,
        filters: [{ name: "JSON", extensions: ["json"] }],
      });
      if (!path) return;
      const result = await commands.importPostProcessEndpoints(path);
      if (result.status === "error") throw new Error(result.error);
      await refreshSettings();
      setStatus(
        t("settings.postProcessing.api.endpoint.imported", {
          count: result.data,
        }),
      );
    } catch (error) {
      notify({
        title: t("settings.postProcessing.api.endpoint.importFailed"),
        detail: String(error),
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Button variant="secondary" size="sm" onClick={importFile} disabled={busy}>
      {status ?? t("settings.postProcessing.api.endpoint.import")}
    </Button>
  );
};

const AUTH_OPTIONS: PostProcessAuth[] = ["auto", "bearer", "api_key", "none"];

export const EndpointAuth: React.FC<{ provider: PostProcessProvider }> = ({
  provider,
}) => {
  const { t } = useTranslation();
  const { refreshSettings } = useSettings();
  const [busy, setBusy] = useState(false);

  const change = async (value: string | null) => {
    const auth = AUTH_OPTIONS.find((option) => option === value);
    if (!auth) return;
    setBusy(true);
    try {
      const result = await commands.changePostProcessAuthSetting(
        provider.id,
        auth,
      );
      if (result.status === "error") throw new Error(result.error);
      await refreshSettings();
    } catch (error) {
      notify({
        title: t("settings.postProcessing.api.endpoint.authFailed"),
        detail: String(error),
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <SettingContainer
      title={t("settings.postProcessing.api.endpoint.auth")}
      description={t("settings.postProcessing.api.endpoint.authDescription")}
      descriptionMode="tooltip"
      grouped
    >
      <Dropdown
        selectedValue={provider.auth ?? "auto"}
        options={AUTH_OPTIONS.map((value) => ({
          value,
          label: t(`settings.postProcessing.api.endpoint.authOptions.${value}`),
        }))}
        onSelect={change}
        disabled={busy}
      />
    </SettingContainer>
  );
};
