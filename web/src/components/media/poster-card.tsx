import { AspectRatio, Box, Card, Center, Group, Stack, Text } from "@mantine/core";
import { IconCalendar, IconCheck, IconStar, IconUser } from "@tabler/icons-react";
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { MetadataResponse } from "@/client/types.gen";
import { FilePhaseOverlay } from "@/components/media/file-phase-badges";
import { OverlayChip, OverlayChipLabel } from "@/components/media/overlay-chip";
import { ProxyImage } from "@/components/media/proxy-image";
import { VrOverlayChip } from "@/components/media/vr-badge";
import { ageAtRelease } from "@/lib/format-birthday";
import { formatReleaseYearMonth } from "@/lib/format-release";
import { proxyImageUrl } from "@/lib/utils";
import classes from "./poster-card.module.css";


interface PosterCardProps {
  item: MetadataResponse;
  /** 传入时按影片发行日标注演员当时年龄 (演员详情出演墙). */
  actorBirthday?: string | null;
  /** 选中态 (片库海报视图多选). */
  selected?: boolean;
  /** 传入时左上角出现选择圆点: 点击只切换选中, 不打开发详情. 不传即纯展示卡片. */
  onToggleSelect?: (id: number) => void;
  /** 已有任意选中时圆点常显 (隐式多选: 首次靠悬停露出, 之后全部常显). */
  selectActive?: boolean;
}

/** 加载失败退化为番号占位块. */
export function PosterCard({
  item,
  actorBirthday,
  selected = false,
  onToggleSelect,
  selectActive = false,
}: PosterCardProps) {
  const { t } = useTranslation(["metadata", "common"]);
  const [errored, setErrored] = useState(false);
  const selectable = onToggleSelect != null;
  const posterUrl = proxyImageUrl(item.poster_url ?? item.poster_urls?.[0]);
  const showImage = posterUrl && !errored;
  const actors = item.actors ?? [];
  const actorsLabel = actors.length > 0 ? actors.join(" / ") : null;
  const releaseLabel = formatReleaseYearMonth(item.release);
  const ageThen = ageAtRelease(actorBirthday, item.release);

  return (
    <Link
      to="/meta/$metadataId"
      params={{ metadataId: String(item.id) }}
      style={{ textDecoration: "none", color: "inherit", display: "block" }}
    >
      <Card
        padding={0}
        radius="md"
        withBorder
        className={classes.card}
        data-selected={selected ? "true" : undefined}
        data-selectable={selectable ? "true" : undefined}
        data-select-active={selectActive ? "true" : undefined}
        style={{ overflow: "hidden" }}
      >
        <Box pos="relative">
          <AspectRatio ratio={0.7}>
            {showImage ? (
              <ProxyImage
                src={posterUrl}
                alt={item.number}
                loading="lazy"
                referrerPolicy="no-referrer"
                style={{ display: "block" }}
                onError={() => setErrored(true)}
              />
            ) : (
              <Center bg="var(--mantine-color-default-hover)" h="100%">
                <Text size="sm" ff="monospace" c="dimmed" ta="center" px="xs">
                  {item.number}
                </Text>
              </Center>
            )}
          </AspectRatio>
          <FilePhaseOverlay
            phase={item.file_phase}
            bottomExtra={
              ageThen != null ? (
                <OverlayChip>
                  <IconUser size={12} color="var(--mantine-color-grape-3)" />
                  <OverlayChipLabel>{t("actors.ageShort", { age: ageThen })}</OverlayChipLabel>
                </OverlayChip>
              ) : undefined
            }
          />
          {selectable && (
            /* 圆点叠在 `<Link>` 里, 两个都要拦: stopPropagation 拦住 Link 的 onClick,
               preventDefault 拦住锚点的浏览器默认跳转. */
            <button
              type="button"
              className={classes.toggle}
              data-selected={selected ? "true" : undefined}
              data-active={selectActive ? "true" : undefined}
              aria-pressed={selected}
              aria-label={t("common:batch.toggleSelect", { name: item.number })}
              onClick={(event) => {
                event.preventDefault();
                event.stopPropagation();
                onToggleSelect?.(item.id);
              }}
            >
              {selected && <IconCheck size={13} stroke={3} />}
            </button>
          )}
          {/* 与评分同角成组: 左上角被相位水印 (马赛克标记) 占用, 单独放会与它重叠. */}
          {(item.vr || item.score != null) && (
            <Group pos="absolute" top={6} right={6} gap={4} wrap="nowrap">
              {item.vr && <VrOverlayChip />}
              {item.score != null && (
                <OverlayChip>
                  <IconStar
                    size={11}
                    color="var(--mantine-color-yellow-4)"
                    fill="var(--mantine-color-yellow-4)"
                  />
                  <OverlayChipLabel>{item.score.toFixed(1)}</OverlayChipLabel>
                </OverlayChip>
              )}
            </Group>
          )}
          {releaseLabel && (
            <Box pos="absolute" bottom={6} right={6}>
              <OverlayChip>
                <IconCalendar size={12} color="var(--mantine-color-cyan-3)" />
                <OverlayChipLabel ff="monospace" lts={0.3}>
                  {releaseLabel}
                </OverlayChipLabel>
              </OverlayChip>
            </Box>
          )}
        </Box>
        <Stack gap={4} p="xs">
          <Text size="sm" fw={600} ff="monospace" lineClamp={1}>
            {item.number}
          </Text>
          {item.title && (
            <Text size="xs" c="dimmed" lineClamp={2} title={item.title}>
              {item.title}
            </Text>
          )}
          {actorsLabel && (
            <Group gap={5} wrap="nowrap" align="center" title={actorsLabel}>
              <Box
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  flexShrink: 0,
                  width: 18,
                  height: 18,
                  borderRadius: "var(--mantine-radius-sm)",
                  background: "var(--mantine-color-grape-filled)",
                  color: "var(--mantine-color-white)",
                }}
              >
                <IconUser size={11} stroke={2.2} />
              </Box>
              <Text
                size="xs"
                fw={600}
                lineClamp={1}
                style={{
                  minWidth: 0,
                  flex: 1,
                  color: "light-dark(var(--mantine-color-grape-7), var(--mantine-color-grape-3))",
                }}
              >
                {actorsLabel}
              </Text>
            </Group>
          )}
        </Stack>
      </Card>
    </Link>
  );
}
