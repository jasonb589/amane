import { Alert, Button, Stack } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { IconPhotoCheck } from "@tabler/icons-react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import {
  getMetadataQueryKey,
  listMetadataQueryKey,
  selectPosterFromExtrafanartMutation,
} from "@/client/@tanstack/react-query.gen";
import { FanartStrip } from "@/components/media/fanart-lightbox";
import { extractErrorMessage } from "@/lib/api-error";
import { posterUpdateNotice } from "@/lib/media/poster-update-notice";

interface MetadataFanartStripProps {
  metadataId: number;
  images: string[];
}

/** 只有影片详情剧照提供海报写入, 演员与其他图片预览保持只读. */
export function MetadataFanartStrip({ metadataId, images }: MetadataFanartStripProps) {
  const { t } = useTranslation(["metadata", "common"]);
  const queryClient = useQueryClient();
  const mutation = useMutation({
    ...selectPosterFromExtrafanartMutation(),
    onSuccess: (updated, variables) => {
      notifications.show(posterUpdateNotice(updated, t));
      void queryClient.invalidateQueries({
        queryKey: getMetadataQueryKey({ path: { metadata_id: variables.path.metadata_id } }),
      });
      void queryClient.invalidateQueries({ queryKey: listMetadataQueryKey() });
    },
  });

  return (
    <FanartStrip
      images={images}
      renderActions={(url) => {
        const current = mutation.variables?.body.url === url;
        const notice = current && mutation.isSuccess ? posterUpdateNotice(mutation.data, t) : null;
        return (
          <Stack gap="xs" align="center">
            {current && mutation.isError && (
              <Alert color="red" variant="filled" w="100%">
                {extractErrorMessage(mutation.error, t("common:toast.operationFailed"))}
              </Alert>
            )}
            {notice && (
              <Alert
                color={notice.color}
                variant="filled"
                w="100%"
                style={{ whiteSpace: "pre-line" }}
              >
                {notice.message}
              </Alert>
            )}
            <Button
              leftSection={<IconPhotoCheck size={16} />}
              loading={current && mutation.isPending}
              disabled={mutation.isPending}
              onClick={() => mutation.mutate({ path: { metadata_id: metadataId }, body: { url } })}
            >
              {t("detail.selectPoster.action")}
            </Button>
          </Stack>
        );
      }}
    />
  );
}
