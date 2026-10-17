import {
  AspectRatio,
  Button,
  Center,
  Checkbox,
  Group,
  Menu,
  Modal,
  MultiSelect,
  SimpleGrid,
  Skeleton,
  Stack,
  Text,
  useMatches,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { IconEraser, IconRefresh, IconTag, IconUser, IconX } from "@tabler/icons-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocation } from "@tanstack/react-router";
import type { ParseKeys } from "i18next";
import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Virtuoso, type StateSnapshot, type VirtuosoHandle } from "react-virtuoso";
import {
  batchActorUserTagsMutation,
  listActorsQueryKey,
  listFacetsOptions,
} from "@/client/@tanstack/react-query.gen";
import { clearActorPerson, scrapeActor, updateActor } from "@/client/sdk.gen";
import type { ActorGender, ActorResponse, CacheKind } from "@/client/types.gen";
import { SelectionBar } from "@/components/common/selection-bar";
import { ClearPersonNotice } from "@/components/media/clear-person-notice";
import { useIdSelection } from "@/hooks/use-id-selection";
import { extractErrorMessage } from "@/lib/api-error";
import { confirm } from "@/lib/confirm";
import { exhaustiveRecord } from "@/lib/exhaustive";
import { USER_TAG_FACET_LIST } from "@/lib/facets";
import { ActorCard } from "./actor-card";
/**
 * 演员墙的虚拟化状态按历史条目保存: 挂载时还原行尺寸与滚动位置.
 *
 * 路由器在 onRendered 复位窗口滚动, 此刻虚拟列表尚未产生高度, 复位会被浏览器夹回文档顶部;
 * 快照由 Virtuoso 在挂载后自行应用, 不依赖复位时机. 上限只保留最近浏览的若干条目.
 */
const GRID_STATE_CACHE = new Map<string, StateSnapshot>();
const GRID_STATE_LIMIT = 16;

function rememberGridState(key: string, state: StateSnapshot): void {
  GRID_STATE_CACHE.delete(key);
  GRID_STATE_CACHE.set(key, state);
  if (GRID_STATE_CACHE.size > GRID_STATE_LIMIT) {
    const oldest = GRID_STATE_CACHE.keys().next().value;
    if (oldest !== undefined) GRID_STATE_CACHE.delete(oldest);
  }
}

// sm/md 收窄一列: 48em 起导航栏展开, 内容区反而变窄, 沿用桌面列数会让缩略图小一档.
const GRID_COLS = { base: 2, xs: 3, sm: 3, md: 4, lg: 6, xl: 7 } as const;

interface ActorGridProps {
  items: ActorResponse[];
  loading?: boolean;
  emptyMessage?: string;
}

/**
 * 卡片按列数切行: 行是虚拟化的单元, 行高由该行最高的卡片决定.
 *
 * 不使用 `VirtuosoGrid` — 它只测首个渲染元素的尺寸, 并据这一个高度推算全表的位置与文档高度,
 * 卡片高度不等时整表都会算错 (上游 #703 / #1220). 行距写在进行内边距上: margin 不计入
 * Virtuoso 测量的元素尺寸.
 */
function toRows(items: readonly ActorResponse[], cols: number): ActorResponse[][] {
  const rows: ActorResponse[][] = [];
  for (let i = 0; i < items.length; i += cols) {
    rows.push(items.slice(i, i + cols));
  }
  return rows;
}

/** 行键取该行首个演员: 追加分页只补齐末行, 已渲染行的身份不变. */
function actorRowKey(_index: number, row: ActorResponse[]) {
  return row[0].id;
}

function ActorGridRow({
  items,
  cols,
  selected,
  selectActive,
  onToggleSelect,
}: {
  items: ActorResponse[];
  cols: number;
  selected: Set<number>;
  selectActive: boolean;
  onToggleSelect: (id: number) => void;
}) {
  return (
    <div
      style={{
        display: "grid",
        columnGap: "var(--mantine-spacing-md)",
        paddingBottom: "var(--mantine-spacing-md)",
        gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))`,
      }}
    >
      {items.map((item) => (
        <ActorCard
          key={item.id}
          item={item}
          selected={selected.has(item.id)}
          selectActive={selectActive}
          onToggleSelect={onToggleSelect}
        />
      ))}
    </div>
  );
}

/**
 * 非虚拟化的演员网格, 给数量有界且与其它内容同页的场景使用 (虚拟化会接管窗口滚动).
 * 列数与演员墙同源, 不要另写一份断点.
 */
export function ActorCardGrid({ items }: { items: ActorResponse[] }) {
  const cols = useMatches(GRID_COLS);
  return (
    <SimpleGrid cols={cols} spacing="md">
      {items.map((item) => (
        <ActorCard key={item.id} item={item} />
      ))}
    </SimpleGrid>
  );
}

/**
 * 窗口滚动虚拟化, 避免无限滚动把全部卡片留在 DOM.
 *
 * `restoreStateFrom` 只在挂载时生效, 变更时会重新应用快照 —
 * 因此快照在挂载时读取一次, 身份在整个挂载期内保持稳定.
 *
 * 列表没有 Grid 的 `stateChanged`, 快照只能经句柄取; 卸载是唯一需要的时机 (快照只服务于
 * 下一次挂载), 而父组件的清理函数先于子树的 ref 解绑执行, 句柄此时仍可用.
 */
interface ActorPosterWallProps {
  items: ActorResponse[];
  restoreKey: string;
  selected: Set<number>;
  onToggleSelect: (id: number) => void;
  /** 已有任意选中时圆点常显 (隐式多选: 首次靠悬停露出, 之后全部常显). */
  selectActive: boolean;
}

function ActorPosterWall({ items, restoreKey, selected, onToggleSelect, selectActive }: ActorPosterWallProps) {
  const cols = useMatches(GRID_COLS);
  const rows = useMemo(() => toRows(items, cols), [items, cols]);
  const virtuosoRef = useRef<VirtuosoHandle>(null);
  const [restoreState] = useState(() => GRID_STATE_CACHE.get(restoreKey));
  // 选中态经闭包进入行; ActorCard 已 memo, 切换一格只重渲染该行.
  const renderRow = useCallback(
    (_index: number, row: ActorResponse[]) => (
      <ActorGridRow
        items={row}
        cols={cols}
        selected={selected}
        selectActive={selectActive}
        onToggleSelect={onToggleSelect}
      />
    ),
    [cols, selected, selectActive, onToggleSelect],
  );

  useEffect(() => {
    const handle = virtuosoRef;
    return () => {
      handle.current?.getState((state) => rememberGridState(restoreKey, state));
    };
  }, [restoreKey]);

  return (
    <Virtuoso
      ref={virtuosoRef}
      useWindowScroll
      data={rows}
      itemContent={renderRow}
      computeItemKey={actorRowKey}
      restoreStateFrom={restoreState}
      increaseViewportBy={600}
    />
  );
}

/**
 * 演员海报网格.
 *
 * 多选自持一份 `useIdSelection` + `SelectionBar` (与列表视图同模式, 不依赖父页面传参);
 * 批量刮削 / 批量性别 / 用户标签 / 批量清空信息的动作、确认弹窗与 toast 文案照抄 actor-table.
 * 虚拟列表内不渲染合并指引: 网格没有「目标行合并图标」, `manage.mergeGuide` 在此无对应操作.
 */
export const ActorGrid = memo(function ActorGrid({
  items,
  loading = false,
  emptyMessage,
}: ActorGridProps) {
  // 历史条目 key 与路由器自身的滚动缓存同源: 新进入的条目没有快照, 不会继承上一次的浏览位置.
  const restoreKey = useLocation({ select: (l) => l.state.key ?? l.href });
  const { t } = useTranslation(["metadata", "common"]);
  const queryClient = useQueryClient();
  const pageIds = items.map((item) => item.id);
  const { selected, selectedIds, toggleOne, toggleAll, isAllSelected, clear } = useIdSelection();

  const [batchScraping, setBatchScraping] = useState(false);
  const [batchGendering, setBatchGendering] = useState(false);
  const [batchClearing, setBatchClearing] = useState(false);
  const [tagModalOpen, setTagModalOpen] = useState(false);
  const [tagIds, setTagIds] = useState<string[]>([]);

  const { data: userTags } = useQuery(listFacetsOptions(USER_TAG_FACET_LIST));
  const tagOptions = (userTags?.items ?? []).map((tag) => ({
    value: String(tag.id),
    label: tag.name,
  }));

  const batchTags = useMutation({
    ...batchActorUserTagsMutation(),
    onSuccess: (_res, variables) => {
      const attached = variables.body.action === "attach";
      notifications.show({
        message: attached ? t("common:toast.userTagAttached") : t("common:toast.userTagDetached"),
        color: "blue",
      });
      setTagModalOpen(false);
      setTagIds([]);
      clear();
      void queryClient.invalidateQueries({ queryKey: listActorsQueryKey() });
    },
    onError: (err) =>
      notifications.show({
        message: extractErrorMessage(err, t("common:toast.operationFailed")),
        color: "red",
      }),
  });

  async function scrapeIds(ids: number[], useCache: CacheKind[]) {
    if (ids.length === 0) return;
    setBatchScraping(true);
    const results = await Promise.allSettled(
      ids.map((id) =>
        scrapeActor({ path: { actor_id: id }, body: { use_cache: useCache }, throwOnError: true }),
      ),
    );
    setBatchScraping(false);
    const failed = results.filter((r) => r.status === "rejected").length;
    const ok = ids.length - failed;
    if (ok > 0) {
      notifications.show({
        message: t("common:toast.batchScrapeStarted", { count: ok }),
        color: "blue",
      });
    }
    if (failed > 0) {
      notifications.show({
        message: t("common:toast.batchScrapeFailed", { count: failed }),
        color: "red",
      });
    }
  }

  async function handleBatchScrape(useCache: CacheKind[]) {
    await scrapeIds(selectedIds, useCache);
    clear();
  }

  /** 网格的「本页」= 已加载的卡片 (无限滚动), 与列表视图的单页范围不同. */
  async function handleScrapeMissing(useCache: CacheKind[]) {
    const ids = items
      .filter((i) => !i.birthday && !i.height && !i.image_urls?.length)
      .map((i) => i.id);
    if (ids.length === 0) {
      notifications.show({ message: t("actors.noneMissing"), color: "gray" });
      return;
    }
    await scrapeIds(ids, useCache);
  }

  async function handleBatchGender(gender: ActorGender) {
    if (selectedIds.length === 0) return;
    setBatchGendering(true);
    const results = await Promise.allSettled(
      selectedIds.map((id) =>
        updateActor({ path: { actor_id: id }, body: { gender }, throwOnError: true }),
      ),
    );
    setBatchGendering(false);
    const failed = results.filter((r) => r.status === "rejected").length;
    const ok = selectedIds.length - failed;
    if (ok > 0) {
      notifications.show({
        message: t("actors.batchGenderDone", {
          count: ok,
          gender: t(GENDER_I18N_KEY[gender]),
        }),
        color: "blue",
      });
      clear();
      void queryClient.invalidateQueries({ queryKey: listActorsQueryKey() });
    }
    if (failed > 0) {
      notifications.show({
        message: t("actors.batchGenderFailed", { count: failed }),
        color: "red",
      });
    }
  }

  async function handleClearPerson(
    ids: number[],
    opts?: { name?: string; clearSelection?: boolean },
  ) {
    if (ids.length === 0) return;
    const ok = await confirm({
      title: t("actors.clearPerson"),
      message: (
        <ClearPersonNotice
          first={
            ids.length === 1 && opts?.name
              ? t("actors.clearPersonFields", { name: opts.name })
              : t("actors.batchClearPersonFields", { count: ids.length })
          }
        />
      ),
      confirmLabel: t("actors.clearPerson"),
    });
    if (!ok) return;
    setBatchClearing(true);
    const results = await Promise.allSettled(
      ids.map((id) => clearActorPerson({ path: { actor_id: id }, throwOnError: true })),
    );
    setBatchClearing(false);
    const failed = results.filter((r) => r.status === "rejected").length;
    const succeeded = ids.length - failed;
    if (succeeded > 0) {
      notifications.show({
        message:
          ids.length === 1
            ? t("actors.clearPersonDone")
            : t("actors.batchClearPersonDone", { count: succeeded }),
        color: "blue",
      });
      if (opts?.clearSelection) clear();
      void queryClient.invalidateQueries({ queryKey: listActorsQueryKey() });
    }
    if (failed > 0) {
      notifications.show({
        message: t("actors.batchClearPersonFailed", { count: failed }),
        color: "red",
      });
    }
  }

  const allSelected = isAllSelected(pageIds);
  const selectionCount = selected.size;
  const busy = batchScraping || batchGendering || batchClearing || batchTags.isPending;

  if (loading) {
    return (
      <SimpleGrid cols={GRID_COLS} spacing="md">
        {Array.from({ length: 12 }, (_, i) => (
          <Stack key={i} gap={2}>
            <AspectRatio ratio={0.75}>
              <Skeleton radius="md" />
            </AspectRatio>
            <Skeleton h={12} w="70%" />
          </Stack>
        ))}
      </SimpleGrid>
    );
  }

  if (items.length === 0) {
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
      <SelectionBar count={selectionCount}>
        {/* 网格没有表头, 全选 / 清空与批量动作同排: 不额外占一行, 网格不会被顶下去. */}
        <Checkbox
          size="xs"
          checked={allSelected}
          indeterminate={!allSelected && selectionCount > 0}
          onChange={() => toggleAll(pageIds)}
          label={t("common:batch.selectAll")}
        />
        <Menu shadow="md" position="bottom-start">
          <Menu.Target>
            <Button size="xs" variant="light" loading={busy}>
              {t("actors.scrapeMissingOnPage")}
            </Button>
          </Menu.Target>
          <Menu.Dropdown>
            <Menu.Item onClick={() => void handleScrapeMissing(["metadata", "trans"])}>
              <Text size="sm">{t("common:actions.scrapeNormal")}</Text>
              <Text size="xs" c="dimmed">
                {t("common:actions.scrapeNormalDesc")}
              </Text>
            </Menu.Item>
            <Menu.Item onClick={() => void handleScrapeMissing([])}>
              <Text size="sm">{t("common:actions.scrapeForce")}</Text>
              <Text size="xs" c="dimmed">
                {t("common:actions.scrapeForceDesc")}
              </Text>
            </Menu.Item>
          </Menu.Dropdown>
        </Menu>
        <Menu shadow="md" position="bottom-start">
          <Menu.Target>
            <Button
              size="xs"
              variant="light"
              leftSection={<IconRefresh size={14} />}
              loading={busy}
              disabled={selectionCount === 0}
            >
              {t("actions.batchScrape")}
            </Button>
          </Menu.Target>
          <Menu.Dropdown>
            <Menu.Item onClick={() => void handleBatchScrape(["metadata", "trans"])}>
              <Text size="sm">{t("common:actions.scrapeNormal")}</Text>
              <Text size="xs" c="dimmed">
                {t("common:actions.scrapeNormalDesc")}
              </Text>
            </Menu.Item>
            <Menu.Item onClick={() => void handleBatchScrape([])}>
              <Text size="sm">{t("common:actions.scrapeForce")}</Text>
              <Text size="xs" c="dimmed">
                {t("common:actions.scrapeForceDesc")}
              </Text>
            </Menu.Item>
          </Menu.Dropdown>
        </Menu>
        <Menu shadow="md" position="bottom-start">
          <Menu.Target>
            <Button
              size="xs"
              variant="light"
              leftSection={<IconUser size={14} />}
              loading={busy}
              disabled={selectionCount === 0}
            >
              {t("actors.batchGender")}
            </Button>
          </Menu.Target>
          <Menu.Dropdown>
            {(["female", "male", "unknown"] as const).map((g) => (
              <Menu.Item key={g} onClick={() => void handleBatchGender(g)}>
                {t(GENDER_I18N_KEY[g])}
              </Menu.Item>
            ))}
          </Menu.Dropdown>
        </Menu>
        <Button
          size="xs"
          variant="light"
          leftSection={<IconTag size={14} />}
          loading={batchTags.isPending}
          disabled={selectionCount === 0}
          onClick={() => setTagModalOpen(true)}
        >
          {t("detail.userTags")}
        </Button>
        <Button
          size="xs"
          variant="light"
          leftSection={<IconEraser size={14} />}
          loading={busy}
          disabled={selectionCount === 0}
          onClick={() => void handleClearPerson(selectedIds, { clearSelection: true })}
        >
          {t("actors.batchClearPerson")}
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

      <ActorPosterWall
        items={items}
        restoreKey={restoreKey}
        selected={selected}
        onToggleSelect={toggleOne}
        selectActive={selectionCount > 0}
      />

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
    </Stack>
  );
});
