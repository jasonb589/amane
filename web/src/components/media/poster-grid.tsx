import {
  AspectRatio,
  Button,
  Center,
  Checkbox,
  Group,
  Modal,
  MultiSelect,
  SimpleGrid,
  Skeleton,
  Stack,
  Text,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { IconRefresh, IconTag, IconTrash, IconX } from "@tabler/icons-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  batchDeleteMetadataMutation,
  batchMetadataUserTagsMutation,
  batchScrapeMetadataMutation,
  listFacetsOptions,
  listMetadataQueryKey,
} from "@/client/@tanstack/react-query.gen";
import { SelectionBar } from "@/components/common/selection-bar";
import { useIdSelection } from "@/hooks/use-id-selection";
import { useRatioRefresh } from "@/hooks/use-ratio-refresh";
import { extractErrorMessage } from "@/lib/api-error";
import { confirm } from "@/lib/confirm";
import { USER_TAG_FACET_LIST } from "@/lib/facets";
import { DEFAULT_POSTER_RATIO, posterEntries } from "@/lib/media/poster-image";
import { AdaptivePosterGrid, type PosterGridBodyProps } from "./adaptive-poster-grid";
import { PosterCard } from "./poster-card";

// sm/md 收窄一列: 48em 起导航栏展开, 内容区反而变窄, 沿用桌面列数会让缩略图小一档.
const GRID_COLS = { base: 2, xs: 3, sm: 3, md: 4, lg: 6, xl: 7 };

interface PosterGridProps extends Omit<PosterGridBodyProps, "selection"> {
  loading?: boolean;
  emptyMessage?: string;
  /** 打开多选: 卡片左上角出现选择圆点, 网格上方出现批量操作条 (片库海报视图). */
  selectable?: boolean;
  /** 列宽相等 (默认, 高度随图片) 或短边锚定 (横版定高、竖版定宽, 视觉大小一致). */
  layout?: "uniform" | "adaptive";
}

/**
 * 片库海报网格.
 *
 * 多选自持一份 `useIdSelection` + `SelectionBar` (与列表视图同模式, 不依赖父页面传参);
 * 批量动作位于布局外层, 两种栅格共用选择状态与操作入口.
 * 不开 `selectable` 的用法 (演员出演墙、分类页影片墙) 保持只读.
 */
export function PosterGrid({
  loading = false,
  emptyMessage,
  selectable = false,
  layout = "uniform",
  ...body
}: PosterGridProps) {
  const { t } = useTranslation(["metadata", "common"]);
  const queryClient = useQueryClient();
  const pageIds = body.items.map((item) => item.id);
  const { selected, selectedIds, toggleOne, toggleAll, isAllSelected, clear } = useIdSelection();
  const [tagModalOpen, setTagModalOpen] = useState(false);
  const [tagIds, setTagIds] = useState<string[]>([]);

  const { data: userTags } = useQuery({
    ...listFacetsOptions(USER_TAG_FACET_LIST),
    enabled: selectable,
  });

  const invalidate = () => void queryClient.invalidateQueries({ queryKey: listMetadataQueryKey() });

  const batchScrape = useMutation({
    ...batchScrapeMetadataMutation(),
    onSuccess: (res) => {
      notifications.show({
        message: t("common:toast.batchScrapeStarted", { count: res.submitted }),
        color: "blue",
      });
    },
    onError: (err) =>
      notifications.show({
        message: extractErrorMessage(err, t("common:toast.batchScrapeFailed")),
        color: "red",
      }),
  });

  const batchDelete = useMutation({
    ...batchDeleteMetadataMutation(),
    onSuccess: () => {
      notifications.show({ message: t("common:toast.metadataDeleted"), color: "blue" });
      clear();
      invalidate();
    },
    onError: (err) =>
      notifications.show({
        message: extractErrorMessage(err, t("common:toast.operationFailed")),
        color: "red",
      }),
  });

  const batchTags = useMutation({
    ...batchMetadataUserTagsMutation(),
    onSuccess: (res) => {
      notifications.show({
        message: t("common:toast.userTagBatchUpdated", { count: res.changed }),
        color: "blue",
      });
      setTagModalOpen(false);
      setTagIds([]);
      invalidate();
    },
    onError: (err) =>
      notifications.show({
        message: extractErrorMessage(err, t("common:toast.operationFailed")),
        color: "red",
      }),
  });

  const busy = batchScrape.isPending || batchDelete.isPending || batchTags.isPending;
  const tagOptions = useMemo(
    () => (userTags?.items ?? []).map((tag) => ({ value: String(tag.id), label: tag.name })),
    [userTags],
  );
  const allSelected = isAllSelected(pageIds);
  const selectionCount = selected.size;
  const selectActive = selectionCount > 0;
  const selection = selectable ? { selected, toggleOne, selectActive } : undefined;

  async function handleBatchDelete() {
    const ok = await confirm({
      title: t("actions.confirmBatchDeleteTitle"),
      message: t("actions.confirmBatchDeleteDesc", { count: selectedIds.length }),
      confirmLabel: t("common:actions.delete"),
    });
    if (!ok) return;
    batchDelete.mutate({ body: { ids: selectedIds } });
  }

  if (loading) {
    return (
      <SimpleGrid cols={GRID_COLS} spacing="md">
        {Array.from({ length: 12 }, (_, i) => (
          <Stack key={i} gap={2}>
            <AspectRatio ratio={DEFAULT_POSTER_RATIO}>
              <Skeleton radius="md" />
            </AspectRatio>
            <Skeleton h={12} w="70%" />
          </Stack>
        ))}
      </SimpleGrid>
    );
  }

  if (body.items.length === 0) {
    return (
      <Center py="xl">
        <Text c="dimmed" size="sm">
          {emptyMessage}
        </Text>
      </Center>
    );
  }

  return (
    <Stack gap="sm">
      {selectable && (
        <SelectionBar count={selectionCount}>
          {/* 网格没有表头, 全选 / 清空与批量动作同排: 不额外占一行, 网格不会被顶下去. */}
          <Checkbox
            size="xs"
            checked={allSelected}
            indeterminate={!allSelected && selectionCount > 0}
            onChange={() => toggleAll(pageIds)}
            label={t("common:batch.selectAll")}
          />
          <Button
            size="xs"
            variant="light"
            leftSection={<IconRefresh size={14} />}
            loading={busy}
            disabled={selectionCount === 0}
            onClick={() => batchScrape.mutate({ body: { ids: selectedIds } })}
          >
            {t("actions.batchScrape")}
          </Button>
          <Button
            size="xs"
            variant="light"
            leftSection={<IconTag size={14} />}
            loading={busy}
            disabled={selectionCount === 0}
            onClick={() => setTagModalOpen(true)}
          >
            {t("detail.userTags")}
          </Button>
          <Button
            size="xs"
            variant="light"
            color="red"
            leftSection={<IconTrash size={14} />}
            loading={busy}
            disabled={selectionCount === 0}
            onClick={() => void handleBatchDelete()}
          >
            {selectionCount > 1 ? t("actions.batchDelete") : t("common:actions.delete")}
          </Button>
          <Button
            size="xs"
            variant="subtle"
            color="gray"
            leftSection={<IconX size={14} />}
            disabled={selectionCount === 0}
            onClick={clear}
          >
            {t("common:batch.clearSelection")}
          </Button>
        </SelectionBar>
      )}

      {layout === "adaptive" ? (
        <AdaptivePosterGrid {...body} selection={selection} />
      ) : (
        <UniformPosterGrid {...body} selection={selection} />
      )}

      {selectable && (
        <Modal
          opened={tagModalOpen}
          onClose={() => setTagModalOpen(false)}
          title={t("detail.userTags")}
          centered
        >
          <Stack gap="md">
            <MultiSelect
              data={tagOptions}
              value={tagIds}
              onChange={setTagIds}
              searchable
              placeholder={t("detail.selectUserTag")}
            />
            <Group justify="flex-end">
              <Button
                variant="light"
                disabled={tagIds.length === 0}
                loading={batchTags.isPending}
                onClick={() =>
                  tagIds.length > 0 &&
                  batchTags.mutate({
                    body: {
                      ids: selectedIds,
                      user_tag_ids: tagIds.map(Number),
                      action: "attach",
                    },
                  })
                }
              >
                {t("common:actions.add")}
              </Button>
              <Button
                variant="light"
                color="red"
                disabled={tagIds.length === 0}
                loading={batchTags.isPending}
                onClick={() =>
                  tagIds.length > 0 &&
                  batchTags.mutate({
                    body: {
                      ids: selectedIds,
                      user_tag_ids: tagIds.map(Number),
                      action: "detach",
                    },
                  })
                }
              >
                {t("common:actions.remove")}
              </Button>
            </Group>
          </Stack>
        </Modal>
      )}
    </Stack>
  );
}

/** 列宽相等: 盒子按各自的图片比例定高, 因此横版变矮而不是被裁掉, 列位置不因图片到达而移动. */
function UniformPosterGrid({ actorBirthday, selection, ...source }: PosterGridBodyProps) {
  const refresh = useRatioRefresh();
  return (
    <SimpleGrid cols={GRID_COLS} spacing="md">
      {posterEntries(source).map(({ item, url, ratio }) => (
        <PosterCard
          key={item.id}
          item={item}
          actorBirthday={actorBirthday}
          selected={selection?.selected.has(item.id)}
          onToggleSelect={selection?.toggleOne}
          selectActive={selection?.selectActive}
          imageUrl={url}
          imageRatio={ratio}
          onImageRatioChange={refresh}
        />
      ))}
    </SimpleGrid>
  );
}
