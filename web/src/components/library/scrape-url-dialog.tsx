import { Button, Group, Input, Modal, Stack, TextInput } from "@mantine/core";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { ContentType, MediaFileResponse } from "@/client/types.gen";
import { EnumToggle } from "@/components/common/enum-toggle";
import { CONTENT_TYPES } from "@/lib/exhaustive-maps";

const CONTENT_TYPE_OPTIONS = ["", ...CONTENT_TYPES] as const;

type ContentTypeChoice = (typeof CONTENT_TYPE_OPTIONS)[number];

interface ScrapeUrlDialogProps {
  target: MediaFileResponse | null;
  saving: boolean;
  onClose: () => void;
  onSubmit: (number: string, url: string, contentType: ContentType | undefined) => void;
}

export function ScrapeUrlDialog({ target, saving, onClose, onSubmit }: ScrapeUrlDialogProps) {
  const { t } = useTranslation(["library", "common"]);

  return (
    <Modal opened={target != null} onClose={onClose} title={t("scrapeUrl.title")} size="lg">
      {target != null && (
        <ScrapeUrlForm
          key={target.id}
          initialNumber={target.number ?? ""}
          saving={saving}
          onClose={onClose}
          onSubmit={onSubmit}
        />
      )}
    </Modal>
  );
}

function ScrapeUrlForm({
  initialNumber,
  saving,
  onClose,
  onSubmit,
}: {
  initialNumber: string;
  saving: boolean;
  onClose: () => void;
  onSubmit: (number: string, url: string, contentType: ContentType | undefined) => void;
}) {
  const { t } = useTranslation(["library", "common"]);
  const [number, setNumber] = useState(initialNumber);
  const [url, setUrl] = useState("");
  const [contentType, setContentType] = useState<ContentTypeChoice>("");
  const trimmedNumber = number.trim();
  const trimmedUrl = url.trim();
  const urlValid = /^https?:\/\/\S+$/i.test(trimmedUrl);

  return (
    <Stack>
      <Input.Wrapper label={t("scrapeUrl.url")} description={t("scrapeUrl.urlHint")}>
        <TextInput
          placeholder={t("scrapeUrl.urlPlaceholder")}
          value={url}
          onChange={(event) => setUrl(event.currentTarget.value)}
          data-autofocus
          error={trimmedUrl !== "" && !urlValid ? t("scrapeUrl.urlInvalid") : undefined}
        />
      </Input.Wrapper>
      <Input.Wrapper label={t("scrapeUrl.number")} description={t("scrapeUrl.numberHint")}>
        <TextInput
          placeholder={t("scrapeUrl.numberPlaceholder")}
          value={number}
          onChange={(event) => setNumber(event.currentTarget.value)}
          error={trimmedNumber === "" ? t("scrapeUrl.numberRequired") : undefined}
        />
      </Input.Wrapper>
      <Input.Wrapper label={t("scrapeUrl.contentType")} description={t("scrapeUrl.contentTypeHint")}>
        <EnumToggle
          fullWidth
          options={CONTENT_TYPE_OPTIONS}
          value={contentType}
          onChange={setContentType}
          getLabel={(value) =>
            value === "" ? t("scrapeUrl.contentTypeAuto") : t(`scrapeUrl.contentTypes.${value}`)
          }
        />
      </Input.Wrapper>
      <Group justify="flex-end">
        <Button variant="default" onClick={onClose} disabled={saving}>
          {t("common:actions.cancel")}
        </Button>
        <Button
          loading={saving}
          disabled={trimmedNumber === "" || !urlValid}
          onClick={() => onSubmit(trimmedNumber, trimmedUrl, contentType === "" ? undefined : contentType)}
        >
          {t("scrapeUrl.submit")}
        </Button>
      </Group>
    </Stack>
  );
}
