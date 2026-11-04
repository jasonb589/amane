import { Badge, Card, Group, Progress, Table, Text, Title } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { siteStatsOptions } from "@/client/@tanstack/react-query.gen";

/**
 * 刮削来源成功率.
 *
 * 数据是最近若干条已完成刮削任务里每个来源的结果 (成功 / 失败 / 命中缓存),
 * 与上方的连通性探测无关: 连通只说明「能不能连上」, 这里说明「连上之后有没有刮到东西」.
 * 失败原因按出现次数降序, 只显示前三条, 避免一行被撑爆.
 */
const SAMPLE_LIMIT = 500;
const REASON_LIMIT = 3;

export function SiteStatsPanel() {
  const { t } = useTranslation("networkCheck");
  const { data, isPending } = useQuery(siteStatsOptions({ query: { limit: SAMPLE_LIMIT } }));
  const items = data?.items ?? [];
  const tasks = data?.tasks ?? 0;

  return (
    <Card withBorder radius="md" p="md">
      <Group justify="space-between" mb="xs" align="baseline">
        <Title order={5}>{t("stats.title")}</Title>
        <Text size="xs" c="dimmed">
          {t("stats.samples", { count: tasks })}
        </Text>
      </Group>
      <Text size="xs" c="dimmed" mb="sm">
        {t("stats.hint")}
      </Text>

      {isPending ? (
        <Text size="sm" c="dimmed">
          {t("stats.loading")}
        </Text>
      ) : items.length === 0 ? (
        <Text size="sm" c="dimmed">
          {t("stats.empty")}
        </Text>
      ) : (
        <Table.ScrollContainer minWidth={560}>
          <Table verticalSpacing="xs" highlightOnHover>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>{t("stats.columns.site")}</Table.Th>
                <Table.Th>{t("stats.columns.rate")}</Table.Th>
                <Table.Th>{t("stats.columns.ok")}</Table.Th>
                <Table.Th>{t("stats.columns.failed")}</Table.Th>
                <Table.Th>{t("stats.columns.cache")}</Table.Th>
                <Table.Th>{t("stats.columns.reasons")}</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {items.map((item) => {
                const total = item.ok + item.failed + item.cache_hit;
                const hit = item.ok + item.cache_hit;
                const rate = total > 0 ? Math.round((hit / total) * 100) : 0;
                return (
                  <Table.Tr key={item.site}>
                    <Table.Td>
                      <Text size="sm" fw={500}>
                        {item.site}
                      </Text>
                    </Table.Td>
                    <Table.Td>
                      <Group gap="xs" wrap="nowrap">
                        <Progress
                          value={rate}
                          w={64}
                          size="sm"
                          color={rate >= 90 ? "teal" : rate >= 50 ? "yellow" : "red"}
                          aria-label={`${item.site} ${rate}%`}
                        />
                        <Text size="xs" c="dimmed">
                          {rate}%
                        </Text>
                      </Group>
                    </Table.Td>
                    <Table.Td>
                      <Text size="sm">{hit}</Text>
                    </Table.Td>
                    <Table.Td>
                      <Text size="sm" c={item.failed > 0 ? "red" : "dimmed"}>
                        {item.failed}
                      </Text>
                    </Table.Td>
                    <Table.Td>
                      <Text size="sm" c="dimmed">
                        {item.cache_hit}
                      </Text>
                    </Table.Td>
                    <Table.Td>
                      <Group gap={4}>
                        {(item.reasons ?? []).slice(0, REASON_LIMIT).map((reason) => {
                          const key = reason.reason ?? "unknown";
                          return (
                            <Badge key={key} size="xs" variant="light" color="red">
                              {t(`stats.reasons.${key}` as never)} ×{reason.count}
                            </Badge>
                          );
                        })}
                      </Group>
                    </Table.Td>
                  </Table.Tr>
                );
              })}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      )}
    </Card>
  );
}
