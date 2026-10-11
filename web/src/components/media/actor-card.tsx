import { AspectRatio, Box, Card, Center, Group, Stack, Text } from "@mantine/core";
import {
  IconCalendar,
  IconCheck,
  IconGenderFemale,
  IconGenderMale,
  IconMovie,
} from "@tabler/icons-react";
import { Link } from "@tanstack/react-router";
import { memo, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ActorResponse } from "@/client/types.gen";
import { OverlayChip, OverlayChipLabel } from "@/components/media/overlay-chip";
import { ProxyImage } from "@/components/media/proxy-image";
import { ageFromBirthday } from "@/lib/format-birthday";
import { proxyImageUrl } from "@/lib/utils";
import classes from "./actor-card.module.css";

/** 选择圆点的占位宽度 (圆点 22px + 间距): 左上角性别角标按它右移, 避免叠在一起. */
const SELECT_TOGGLE_RESERVE = 28;

interface ActorCardProps {
  item: ActorResponse;
  /** 选中态 (演员海报视图多选). */
  selected?: boolean;
  /** 传入时左上角出现选择圆点: 点击只切换选中, 不打开详情. 不传即纯展示卡片. */
  onToggleSelect?: (id: number) => void;
}

/** 海报墙性别符号角标: 仅已知性别渲染, unknown 不显示 (避免满墙灰色符号噪音). */
const GENDER_CHIP = {
  female: {
    Icon: IconGenderFemale,
    color: "var(--mantine-color-pink-3)",
    label: "browse.person.genderFemale",
  },
  male: {
    Icon: IconGenderMale,
    color: "var(--mantine-color-blue-3)",
    label: "browse.person.genderMale",
  },
} as const;

export const ActorCard = memo(function ActorCard({
  item,
  selected = false,
  onToggleSelect,
}: ActorCardProps) {
  const { t } = useTranslation(["metadata", "common"]);
  const [errored, setErrored] = useState(false);
  const selectable = onToggleSelect != null;
  const imageUrl = proxyImageUrl(item.image_urls?.[0]);
  const showImage = Boolean(imageUrl) && !errored;
  const age = ageFromBirthday(item.birthday);
  const birthdayLabel = item.birthday
    ? age != null
      ? t("actors.birthdayWithAge", { date: item.birthday, age })
      : item.birthday
    : null;
  const heightLabel = item.height != null ? t("browse.person.cm", { value: item.height }) : null;
  const metaBits = [heightLabel, item.cup ? t("actors.cupShort", { cup: item.cup }) : null].filter(
    (v): v is string => Boolean(v),
  );
  const genderChip =
    item.gender === "female" || item.gender === "male" ? GENDER_CHIP[item.gender] : null;

  return (
    <Link
      to="/actors/$actorId"
      params={{ actorId: String(item.id) }}
      style={{ textDecoration: "none", color: "inherit", display: "block" }}
    >
      <Card
        padding={0}
        radius="md"
        withBorder
        className={classes.card}
        data-selected={selected ? "true" : undefined}
        style={{ overflow: "hidden" }}
      >
        <Box pos="relative">
          <AspectRatio ratio={0.75}>
            {showImage ? (
              <ProxyImage
                src={imageUrl}
                alt={item.name}
                referrerPolicy="no-referrer"
                style={{ display: "block", width: "100%", height: "100%", objectFit: "cover" }}
                onError={() => setErrored(true)}
              />
            ) : (
              <Center bg="var(--mantine-color-default-hover)" h="100%">
                <Text size="sm" c="dimmed" ta="center" px="xs" lineClamp={2}>
                  {item.name}
                </Text>
              </Center>
            )}
          </AspectRatio>
          {selectable && (
            /* 圆点叠在 `<Link>` 上: 两个都要拦 — stopPropagation 挡住 Link 的 onClick,
               preventDefault 挡住锚点的浏览器默认跳转. */
            <button
              type="button"
              className={classes.toggle}
              data-selected={selected ? "true" : undefined}
              aria-pressed={selected}
              aria-label={t("common:batch.toggleSelect", { name: item.name })}
              onClick={(event) => {
                event.preventDefault();
                event.stopPropagation();
                onToggleSelect?.(item.id);
              }}
            >
              {selected && <IconCheck size={13} stroke={3} />}
            </button>
          )}
          <Box pos="absolute" top={6} right={6}>
            <OverlayChip>
              <IconMovie size={11} color="var(--mantine-color-grape-3)" />
              <OverlayChipLabel>{item.count}</OverlayChipLabel>
            </OverlayChip>
          </Box>
          {genderChip != null && (
            <Box pos="absolute" top={6} left={selectable ? 6 + SELECT_TOGGLE_RESERVE : 6}>
              <OverlayChip title={t(genderChip.label)} aria-label={t(genderChip.label)}>
                <genderChip.Icon size={12} color={genderChip.color} />
              </OverlayChip>
            </Box>
          )}
          {age != null && (
            <Box pos="absolute" bottom={6} right={6}>
              <OverlayChip>
                <IconCalendar size={12} color="var(--mantine-color-cyan-3)" />
                <OverlayChipLabel ff="monospace" lts={0.3}>
                  {t("actors.ageShort", { age })}
                </OverlayChipLabel>
              </OverlayChip>
            </Box>
          )}
        </Box>
        <Stack gap={4} p="xs">
          <Text size="sm" fw={600} lineClamp={1} title={item.name}>
            {item.name}
          </Text>
          {birthdayLabel && (
            <Group gap={5} wrap="nowrap" align="center" title={birthdayLabel}>
              <Box
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  flexShrink: 0,
                  width: 18,
                  height: 18,
                  borderRadius: "var(--mantine-radius-sm)",
                  background: "var(--mantine-color-cyan-filled)",
                  color: "var(--mantine-color-white)",
                }}
              >
                <IconCalendar size={11} stroke={2.2} />
              </Box>
              <Text size="xs" fw={600} lineClamp={1} style={{ minWidth: 0, flex: 1 }} c="dimmed">
                {birthdayLabel}
              </Text>
            </Group>
          )}
          {metaBits.length > 0 && (
            <Text size="xs" c="dimmed" lineClamp={1}>
              {metaBits.join(" · ")}
            </Text>
          )}
        </Stack>
      </Card>
    </Link>
  );
});
