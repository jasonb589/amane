import { Button } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { IconLanguage } from "@tabler/icons-react";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { translateMetadataText } from "@/client/sdk.gen";
import type {
  RenderTextFieldAction,
  TextFieldActionProps,
} from "@/components/schema-form/fields/text-field-action";
import { extractErrorMessage } from "@/lib/api-error";

export function TranslateFieldButton({
  field,
  value,
  getValue,
  onChange,
  resetVersion,
}: TextFieldActionProps & { field: "title" | "plot" }) {
  const { t } = useTranslation("metadata");
  const [loading, setLoading] = useState(false);
  const controller = useRef<AbortController | null>(null);
  const draft = useRef({ value, resetVersion, revision: 0, onChange, getValue });
  useLayoutEffect(() => {
    if (draft.current.value !== value || draft.current.resetVersion !== resetVersion) {
      draft.current.revision += 1;
    }
    draft.current.value = value;
    draft.current.resetVersion = resetVersion;
    draft.current.onChange = onChange;
    draft.current.getValue = getValue;
  }, [value, resetVersion, onChange, getValue]);

  useEffect(() => () => controller.current?.abort(), []);

  async function translate() {
    if (controller.current || !value.trim()) return;
    const request = new AbortController();
    controller.current = request;
    const revision = draft.current.revision;
    const original = value;
    setLoading(true);
    try {
      const { data } = await translateMetadataText({
        body: { field, text: original },
        signal: request.signal,
        throwOnError: true,
      });
      if (request.signal.aborted) return;
      // 用户修改、重置或关闭编辑窗口后, 旧请求不得覆盖新草稿.
      if (draft.current.revision !== revision || draft.current.getValue() !== original) {
        notifications.show({ message: t("translate.draftChanged"), color: "orange" });
        return;
      }
      if (data.changed) {
        draft.current.onChange(data.text);
        notifications.show({ message: t("translate.ready"), color: "blue" });
      } else {
        notifications.show({ message: t("translate.unchanged"), color: "blue" });
      }
    } catch (error) {
      if (!request.signal.aborted) {
        notifications.show({
          message: extractErrorMessage(error, t("translate.failed")),
          color: "red",
        });
      }
    } finally {
      if (!request.signal.aborted) setLoading(false);
      if (controller.current === request) controller.current = null;
    }
  }

  return (
    <Button
      type="button"
      variant="light"
      size="compact-sm"
      leftSection={<IconLanguage size={14} />}
      loading={loading}
      disabled={!value.trim()}
      onClick={() => void translate()}
    >
      {t("translate.button")}
    </Button>
  );
}

export const METADATA_TRANSLATION_ACTIONS: Readonly<Record<string, RenderTextFieldAction>> = {
  title: (props) => <TranslateFieldButton {...props} field="title" />,
  plot: (props) => <TranslateFieldButton {...props} field="plot" />,
};
