/**
 * 短边锚定的自适应排布.
 *
 * 横版以高度、竖版以宽度作为短边, 保留图片自然比例. 普通卡片的短边仅由容器宽度决定,
 * 不随每行条目数量或比例变化; 未满行保留右侧空白. 超出容器的单张横图独占一行并缩小至容器宽度.
 */

export interface AdaptiveRow {
  /** 行首在入参数组里的下标. */
  start: number;
  count: number;
  /** 容器统一缩放系数; 仅超宽单图使用更小值. 实际短边 = 基准短边 × scale. */
  scale: number;
}

/**
 * 容器统一缩放的下限.
 *
 * 按竖版宽度计算容器可容纳的列数, 最多缩小 5% 以增加一列; 所有普通行复用同一尺寸,
 * 不为填满行宽而放大卡片. 容器不足单张基准宽度时允许继续缩小.
 */
const MIN_SCALE = 0.95;

/**
 * 基准短边 (竖版海报的宽度): 约 7.2 张一行, 夹在 [150, 240].
 *
 * 上限过低时宽屏一行塞进十几张小卡片, 过高时一张占掉半屏; 下限保证窄屏仍能放下两张.
 */
const SHORT_EDGE_MIN = 150;
const SHORT_EDGE_MAX = 240;
const POSTER_COLUMNS = 7.2;

export function adaptiveShortEdge(containerWidth: number): number {
  return Math.min(Math.max(containerWidth / POSTER_COLUMNS, SHORT_EDGE_MIN), SHORT_EDGE_MAX);
}

/** 以短边为单位的卡片宽度: 横版 = 比例, 竖版 = 1. */
function widthInShortEdges(ratio: number): number {
  return Math.max(ratio, 1);
}

/** 卡片宽度向下保留两位小数以免舍入溢出; 高度由卡片按图片比例撑开. */
export function adaptiveTileWidth(ratio: number, shortEdge: number, scale: number): number {
  return Math.floor(shortEdge * scale * widthInShortEdges(ratio) * 100) / 100;
}

export function layoutAdaptiveRows(
  ratios: readonly number[],
  containerWidth: number,
  gap: number,
  shortEdge: number,
): AdaptiveRow[] {
  const rows: AdaptiveRow[] = [];
  if (containerWidth <= 0) return rows;

  const columns = Math.max(1, Math.floor((containerWidth + gap) / (shortEdge * MIN_SCALE + gap)));
  const scale = Math.min(1, (containerWidth - gap * (columns - 1)) / (columns * shortEdge));

  let start = 0;
  while (start < ratios.length) {
    const firstWidth = adaptiveTileWidth(ratios[start], shortEdge, scale);
    if (firstWidth > containerWidth) {
      rows.push({
        start,
        count: 1,
        scale: containerWidth / (shortEdge * widthInShortEdges(ratios[start])),
      });
      start += 1;
      continue;
    }

    let count = 1;
    let rowWidth = firstWidth;
    while (start + count < ratios.length) {
      const nextWidth = rowWidth + gap + adaptiveTileWidth(ratios[start + count], shortEdge, scale);
      if (nextWidth > containerWidth) break;
      rowWidth = nextWidth;
      count += 1;
    }
    rows.push({ start, count, scale });
    start += count;
  }

  return rows;
}
