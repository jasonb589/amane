import { ActionIcon, Button, Divider, Group, Paper, Stack, Text } from "@mantine/core";
import { IconPlus, IconTrash } from "@tabler/icons-react";
import type { AnyFieldApi } from "@tanstack/react-form";
import { useTranslation } from "react-i18next";
import type { ArrayFieldProps, ObjectJSONSchema } from "../schema";
import { FieldRouter } from "./field-router";

export function ObjectArrayField({
  name,
  label,
  description,
  itemSchema,
  schema,
  form,
  i18nPath,
  i18nPrefix,
}: ArrayFieldProps<ObjectJSONSchema>) {
  const { t } = useTranslation("common");
  const readonly = schema["x-readonly"] ?? false;

  const properties = itemSchema.properties ?? {};

  const createDefaultItem = (): Record<string, unknown> => {
    const item: Record<string, unknown> = {};
    for (const [key, fieldSchema] of Object.entries(properties)) {
      item[key] = fieldSchema.default ?? null;
    }
    return item;
  };

  return (
    <form.Field name={name}>
      {(field: AnyFieldApi) => {
        const items = Array.isArray(field.state.value) ? field.state.value : [];

        return (
          <Stack gap="xs" py="xs">
            <Group justify="space-between" align="flex-start" wrap="nowrap">
              <Stack gap={2}>
                <Text size="sm" fw={500}>
                  {label}
                </Text>
                {description && (
                  <Text size="xs" c="dimmed">
                    {description}
                  </Text>
                )}
              </Stack>
              {!readonly && (
                <Button
                  type="button"
                  size="xs"
                  variant="outline"
                  leftSection={<IconPlus size={14} />}
                  onClick={() => field.handleChange([...items, createDefaultItem()])}
                >
                  {t("actions.add")}
                </Button>
              )}
            </Group>

            <Stack gap="sm">
              {items.length === 0 && (
                <Text size="sm" c="dimmed" ta="center" py="md">
                  No items
                </Text>
              )}

              {items.map((_, index) => (
                <Paper key={index} withBorder radius="md" p="sm">
                  <Group justify="space-between" mb="xs">
                    <Text size="sm" fw={500}>
                      Item {index + 1}
                    </Text>
                    {!readonly && (
                      <ActionIcon
                        variant="subtle"
                        color="gray"
                        size="sm"
                        aria-label={`Remove item ${index + 1}`}
                        onClick={() => {
                          const next = [...items];
                          next.splice(index, 1);
                          field.handleChange(next);
                        }}
                      >
                        <IconTrash size={14} />
                      </ActionIcon>
                    )}
                  </Group>
                  <Divider mb="xs" />
                  <Stack gap={0} pl="xs">
                    {Object.entries(properties).map(([fieldName, fieldSchema]) => (
                      <FieldRouter
                        key={fieldName}
                        name={`${name}[${index}].${fieldName}`}
                        schema={fieldSchema}
                        form={form}
                        i18nPath={`${i18nPath}.${fieldName}`}
                        i18nPrefix={i18nPrefix}
                      />
                    ))}
                  </Stack>
                </Paper>
              ))}
            </Stack>
          </Stack>
        );
      }}
    </form.Field>
  );
}
