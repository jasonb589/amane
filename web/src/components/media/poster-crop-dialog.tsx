import { notifications } from "@mantine/notifications";
import { useMutation } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { cropPosterFromThumbMutation } from "@/client/@tanstack/react-query.gen";
import { ImageCropDialog } from "@/components/media/image-crop-dialog";
import { useConfig } from "@/hooks/use-config";
import { extractErrorMessage } from "@/lib/api-error";
import { DEFAULT_POSTER_RATIO } from "@/lib/media/poster-image";
import { posterUpdateNotice } from "@/lib/media/poster-update-notice";

export interface PosterCropDialogProps {
  opened: boolean;
  onClose: () => void;
  metadataId: number;
  imageUrl: string;
  /** 指定当前海报作源时传入; 未指定时后端继续使用默认封面. */
  sourceUrl?: string;
  onSuccess?: () => void;
}

/** 影片海报裁切: 基准为指定候选或默认封面的本地文件像素, 结果替换 poster_urls. */
export function PosterCropDialog({
  opened,
  onClose,
  metadataId,
  imageUrl,
  sourceUrl,
  onSuccess,
}: PosterCropDialogProps) {
  const { t } = useTranslation(["metadata", "common"]);
  const { data: config } = useConfig();
  const configRatio = config?.scraping?.poster_ratio ?? DEFAULT_POSTER_RATIO;

  const mutation = useMutation({
    ...cropPosterFromThumbMutation(),
    onSuccess: (updated) => {
      notifications.show(posterUpdateNotice(updated, t));
      onSuccess?.();
      onClose();
    },
    onError: (err) =>
      notifications.show({
        message: extractErrorMessage(err, t("common:toast.operationFailed")),
        color: "red",
      }),
  });

  return (
    <ImageCropDialog
      opened={opened}
      onClose={onClose}
      imageUrl={imageUrl}
      defaultAspect={configRatio}
      initialAnchor="right"
      submitting={mutation.isPending}
      onSubmit={(box, rotation) =>
        mutation.mutate({
          path: { metadata_id: metadataId },
          body: { ...box, rotation, ...(sourceUrl ? { source_url: sourceUrl } : {}) },
        })
      }
      labels={{
        title: t("detail.cropPoster.title"),
        confirm: t("detail.cropPoster.confirm"),
        needSelection: t("detail.cropPoster.needSelection"),
        operationFailed: t("common:toast.operationFailed"),
        aspect: t("detail.cropPoster.aspect"),
        lockAspect: t("detail.cropPoster.lockAspect"),
        left: t("detail.cropPoster.left"),
        top: t("detail.cropPoster.top"),
        width: t("detail.cropPoster.width"),
        height: t("detail.cropPoster.height"),
        cancel: t("common:actions.cancel"),
        rotateLeft: t("detail.cropPoster.rotateLeft"),
        rotateRight: t("detail.cropPoster.rotateRight"),
        resetRotation: t("detail.cropPoster.resetRotation"),
      }}
    />
  );
}
