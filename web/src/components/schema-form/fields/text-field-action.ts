import type { ReactNode } from "react";

export interface TextFieldActionProps {
  value: string;
  getValue: () => unknown;
  onChange: (value: string) => void;
  resetVersion: number;
}

export type RenderTextFieldAction = (props: TextFieldActionProps) => ReactNode;
