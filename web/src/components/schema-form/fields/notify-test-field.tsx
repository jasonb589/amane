import { Alert, Button } from "@mantine/core";
import { useMutation } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { testNotify } from "@/client/sdk.gen";
import { extractErrorMessage } from "@/lib/api-error";
import type { FieldProps } from "../schema";
import { FieldChrome } from "./field-chrome";

function isChannel(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function TestButton({ channel, label }: { channel: unknown; label: string }) {
  const { t } = useTranslation("settings");
  const mutation = useMutation({
    mutationFn: async () => {
      if (!isChannel(channel)) throw new Error(t("notifyTest.invalidChannel"));
      const { data } = await testNotify({ body: channel, throwOnError: true });
      return data;
    },
  });
  const text = mutation.error
    ? extractErrorMessage(mutation.error, t("notifyTest.failed"))
    : mutation.data?.ok
      ? t("notifyTest.success", { message: mutation.data.message })
      : mutation.data?.message;

  return (
    <>
      <Button
        size="xs"
        variant="light"
        loading={mutation.isPending}
        onClick={() => mutation.mutate()}
      >
        {label || t("notifyTest.send")}
      </Button>
      {text ? (
        <Alert
          mt="xs"
          color={mutation.data?.ok && !mutation.error ? "teal" : "red"}
          variant="light"
          role="status"
        >
          {text}
        </Alert>
      ) : null}
    </>
  );
}

export function NotifyTestField({ name, label, description, form, variant }: FieldProps) {
  const parent = name.replace(/(\[[^\]]*\])$|(\.[^.[\]]+)$/, "");
  return (
    <FieldChrome variant={variant} label={label} description={description}>
      <form.Field name={parent}>
        {(field) => <TestButton channel={field.state.value} label={label} />}
      </form.Field>
    </FieldChrome>
  );
}
