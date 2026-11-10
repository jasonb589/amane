import type { TFunction } from "i18next";
import type { MetadataResponse } from "@/client/types.gen";

/** 数据库海报已保存时仍呈现文件写回结果, 避免把部分同步失败显示为完整成功. */
export function posterUpdateNotice(
  updated: MetadataResponse,
  t: TFunction<["metadata", "common"]>,
): { message: string; color: "orange" | "blue" } {
  const nfo = updated.nfo_sync;
  const poster = updated.poster_sync;
  const messages = [t("metadata:detail.cropPoster.success")];
  if (poster) {
    messages.push(
      poster.unlocated
        ? t("metadata:posterSync.unlocated")
        : t("metadata:posterSync.result", poster),
    );
  }
  if (nfo) {
    messages.push(
      nfo.unlocated ? t("metadata:nfoSync.unlocated") : t("metadata:nfoSync.result", nfo),
    );
  }
  return {
    message: messages.join("\n"),
    color: [poster, nfo].some(
      (sync) => sync && ((sync.failed ?? 0) > 0 || (sync.skipped ?? 0) > 0 || sync.unlocated),
    )
      ? "orange"
      : "blue",
  };
}
