"""parse_file_info 表测试: 完整路径上的番号、内容类型、分集、字幕、马赛克、分辨率."""

from typing import NamedTuple

import pytest

from amane.parsing import (
    ContentType,
    canonical_definition,
    detect_cd,
    extract_number,
    infer_content_type,
    is_amateur,
    is_uncensored,
    is_vr,
    normalize_fc2_number,
    parse_file_info,
    split_number,
)


class _Case(NamedTuple):
    path: str
    cd: int | None = None
    has_subtitle: bool = False
    mosaic: str | None = None
    definition: str | None = None
    number: str | None = None
    content_type: ContentType | None = None


CASES: list[object] = [
    # --- 分集 ---
    _Case("MIDV-123-CD1.mp4", cd=1, number="MIDV-123"),
    _Case("MIDV-123-cd2.mp4", cd=2, number="MIDV-123"),
    _Case("MIDV-123-A.mp4", cd=1, number="MIDV-123"),
    _Case("MIDV-123-B.mp4", cd=2, number="MIDV-123"),
    _Case("MIDV-123.part2.mp4", cd=2, number="MIDV-123"),
    _Case("MIDV-123-1.mp4", cd=1, number="MIDV-123"),
    _Case("MIDV-123-2.mp4", cd=2, number="MIDV-123"),
    _Case("MIDV-123-3.mp4", cd=3, number="MIDV-123"),
    _Case("MIDV-123-9.mp4", cd=9, number="MIDV-123"),
    _Case("MIDV-123.mp4", number="MIDV-123"),
    _Case("MIDV-123-0.mp4"),
    _Case("MIDV-123-01.mp4", cd=1),
    _Case("MIDV-123-10.mp4", cd=10),
    _Case("MIDV-123-12.mp4", cd=12),
    # --- 字幕 ---
    _Case("MIDV-123-C.mp4", has_subtitle=True, number="MIDV-123"),
    _Case("MIDV-123-UC.mp4", has_subtitle=True, mosaic="cracked", number="MIDV-123"),
    _Case("[字幕]MIDV-123.mp4", has_subtitle=True, number="MIDV-123"),
    _Case("[中文字幕]MIDV-123.mp4", has_subtitle=True, number="MIDV-123"),
    # --- 马赛克: 文件名标记 ---
    _Case("[無碼]MIDV-123.mp4", mosaic="uncensored", number="MIDV-123"),
    _Case("[无码]MIDV-123.mp4", mosaic="uncensored", number="MIDV-123"),
    _Case("[UNCENSORED]ABC-123.mp4", mosaic="uncensored", number="ABC-123"),
    _Case("ABC-123-uncensored.mp4", mosaic="uncensored", number="ABC-123"),
    _Case("[破解]MIDV-123.mp4", mosaic="cracked", number="MIDV-123"),
    _Case("[克破]MIDV-123.mp4", mosaic="cracked", number="MIDV-123"),
    _Case("[CRACKED]MIDV-123.mp4", mosaic="cracked", number="MIDV-123"),
    _Case("MIDV-123-cracked.mp4", mosaic="cracked", number="MIDV-123"),
    _Case("MIDV-123-cracked-C.mp4", has_subtitle=True, mosaic="cracked", number="MIDV-123"),
    _Case("[流出]MIDV-123.mp4", mosaic="leaked", number="MIDV-123"),
    _Case("MIDV-123-LEAKED.mp4", mosaic="leaked", number="MIDV-123"),
    _Case("MIDV-123流出.mp4", mosaic="leaked", number="MIDV-123"),
    # 同名多标记: 破解优先于流出, 流出优先于无码
    _Case("MIDV-123-無碼流出.mp4", mosaic="leaked", number="MIDV-123"),
    _Case("MIDV-123-無碼破解.mp4", mosaic="cracked", number="MIDV-123"),
    _Case("MIDV-123-破解流出.mp4", mosaic="cracked", number="MIDV-123"),
    # -U / -UC 是破解; -UC 同时是中字. 后面还可跟分片或分辨率
    _Case("MIDV-123-UC-CD1.mp4", cd=1, has_subtitle=True, mosaic="cracked", number="MIDV-123"),
    _Case("MIDV-123-UC-4K.mp4", has_subtitle=True, mosaic="cracked", definition="4K", number="MIDV-123"),
    _Case("MIDV-123-UC-CD1-4K.mp4", cd=1, has_subtitle=True, mosaic="cracked", definition="4K", number="MIDV-123"),
    _Case("MIDV-123-U.mp4", mosaic="cracked", number="MIDV-123"),
    _Case("MIDV-123-C-U.mp4", has_subtitle=True, mosaic="cracked", number="MIDV-123"),
    _Case("MIDV-123-U-CD1.mp4", cd=1, mosaic="cracked", number="MIDV-123"),
    # -LTX 是破解; 后面还可跟中字 / 分辨率. 裸 LTX 不是标记
    _Case("SNIS-576-LTX.mp4", mosaic="cracked", number="SNIS-576"),
    _Case("IPX-750-LTX-4K.mp4", mosaic="cracked", definition="4K", number="IPX-750"),
    _Case("MIDV-123-LTX-C.mp4", has_subtitle=True, mosaic="cracked", number="MIDV-123"),
    _Case("MIDV-123-ltx.mp4", mosaic="cracked", number="MIDV-123"),
    _Case("LTX-123.mp4", number="LTX-123"),
    _Case("HEYZO-123.mp4", number="HEYZO-123"),
    _Case("HEYZO-123-1080p.mp4", definition="1080p", number="HEYZO-123"),
    _Case("HEYZO-123-流出.mp4", mosaic="leaked", number="HEYZO-123"),
    _Case("HEYZO-123-U.mp4", mosaic="cracked", number="HEYZO-123"),
    # --- 分辨率: 点号 / 连字符 ---
    _Case("ABC-123.8K.mp4", definition="8K", number="ABC-123"),
    _Case("ABC-123.4320P.mp4", definition="8K", number="ABC-123"),
    _Case("ABC-123.4320p60.mp4", definition="8K", number="ABC-123"),
    _Case("ABC-123-4K.mp4", definition="4K", number="ABC-123"),
    # 站内命名把 4K 写成 4KS (1dldss00529_4Ks): 同样算 4K.
    _Case("ABC-123.4KS.mp4", definition="4K", number="ABC-123"),
    _Case("ABC-123.2160p.mp4", definition="4K", number="ABC-123"),
    _Case("ABC-123.2160P.mp4", definition="4K", number="ABC-123"),
    _Case("ABC-123.1440p.mp4", definition="1440p", number="ABC-123"),
    _Case("ABC-123.1080p.mp4", definition="1080p", number="ABC-123"),
    _Case("ABC-123-720p.mp4", definition="720p", number="ABC-123"),
    _Case("ABC-123.480p.mp4", definition="480p", number="ABC-123"),
    _Case("ABC-123.576P.mp4", definition="480p", number="ABC-123"),
    _Case("ABC-123.HD.mp4", definition="720p", number="ABC-123"),
    _Case("ABC-123.SD.mp4", definition="480p", number="ABC-123"),
    # 多命中取最高
    _Case("ABC-123.1080p.HD.mp4", definition="1080p", number="ABC-123"),
    _Case("ABC-123.4K.2160p.mp4", definition="4K", number="ABC-123"),
    _Case("ABC-123.8K.HD.mp4", definition="8K", number="ABC-123"),
    _Case("ABC-123.720p.1080p.mp4", definition="1080p", number="ABC-123"),
    # 同档的多种写法 (SD / 576P / 480P) 仍取最高
    _Case("ABC-123.8K.4320P.mp4", definition="8K", number="ABC-123"),
    _Case("ABC-123.SD.480p.mp4", definition="480p", number="ABC-123"),
    # 下划线分隔 (番号剥离已把 _ 当标记分隔符)
    _Case("MIDV-123_4K.mp4", definition="4K", number="MIDV-123"),
    _Case("MIDV-123_4K_无码.mp4", mosaic="uncensored", definition="4K", number="MIDV-123"),
    _Case("MIDV-123_1080p_x.mp4", definition="1080p", number="MIDV-123"),
    _Case("ABC-123_8K_HDR.mp4", definition="8K", number="ABC-123"),
    # 方括号 / 汉字紧贴
    _Case("[4K]MIDV-123.mp4", definition="4K", number="MIDV-123"),
    _Case("[4K無碼]MIDV-123.mp4", mosaic="uncensored", definition="4K", number="MIDV-123"),
    _Case("[1080p無碼]MIDV-123.mp4", mosaic="uncensored", definition="1080p", number="MIDV-123"),
    _Case("MIDV-123-4K無碼.mp4", mosaic="uncensored", definition="4K", number="MIDV-123"),
    _Case("MIDV-123-4K-無碼.mp4", mosaic="uncensored", definition="4K", number="MIDV-123"),
    _Case("[破解]MIDV-123_1080p.mp4", mosaic="cracked", definition="1080p", number="MIDV-123"),
    # 空格分隔
    _Case("ABC-123 4K.mp4", definition="4K", number="ABC-123"),
    _Case("ABC-123 1080p uncensored.mp4", mosaic="uncensored", definition="1080p", number="ABC-123"),
    # 帧率后缀仍识别分辨率
    _Case("ABC-123.1080p60.mp4", definition="1080p", number="ABC-123"),
    _Case("ABC-123.2160p30.mp4", definition="4K", number="ABC-123"),
    # 分辨率数字不得被番号规则吸走: 无短横线的日期号 + 分辨率
    _Case("120115_318-mura-1080p.mp4", definition="1080p", number="120115_318"),
    _Case("120115_318-mura-720p.mp4", definition="720p", number="120115_318"),
    _Case("120115_318-mura-2160p.mp4", definition="4K", number="120115_318"),
    _Case("120115_318-mura-1440p.mp4", definition="1440p", number="120115_318"),
    _Case("120115_318-mura-480p.mp4", definition="480p", number="120115_318"),
    _Case("112912_776-mura-whole1_hd.wmv", definition="720p", number="112912_776"),
    # 分集 + 分辨率
    _Case("MIDV-123-4K-CD1.mp4", cd=1, definition="4K", number="MIDV-123"),
    # --- 分辨率误报: 番号/编码里的字母数字不当作独立标记 ---
    _Case("SKYHD-172.mp4", number="SKYHD-172"),
    _Case("ABC-123.HDTV.mp4", number="ABC-123"),
    _Case("ABC-2160.mp4"),
    _Case("ABC-123.1080.mp4", number="ABC-123"),
    # 带 p 的分辨率片段整段剥除, 数字不并入番号
    _Case("ABC-2160p.mp4", definition="4K", number="ABC"),
    _Case("natsume-1080p.mp4", definition="1080p", number="NATSUME"),
    _Case("ABC-123.mp4", number="ABC-123"),
    _Case("HD-123.mp4"),
    _Case("SD-123.mp4"),
    _Case("HD_123.mp4"),
    _Case("SD_123.mp4"),
    _Case("ABC-123.FHD.mp4", number="ABC-123"),
    _Case("ABC-123.UHD.mp4", number="ABC-123"),
    # 字幕反例: -CD / -CS 不是 -C
    _Case("MIDV-123-CS.mp4", number="MIDV-123"),
    # --- 完整路径: 目录关键词决定内容类型 ---
    _Case("/media/里番/something-123.mp4", content_type=ContentType.HENTAI),
    _Case("/media/裏番/something-123.mp4", content_type=ContentType.HENTAI),
    _Case("/media/getchu/item-123.mp4", content_type=ContentType.HENTAI),
    _Case("/media/GETCHU/item-123.mp4", content_type=ContentType.HENTAI),
    _Case("/media/欧美/MIDV-123.mp4", number="MIDV-123", content_type=ContentType.WESTERN),
    _Case("/mnt/nas/library/欧美/2024/studio/MIDV-123.mp4", number="MIDV-123", content_type=ContentType.WESTERN),
    # 东欧美 不含「欧美」单独命中
    _Case("/media/东欧美/MIDV-123.mp4", number="MIDV-123", content_type=ContentType.CENSORED),
    # getchu 必须整段相等; 子串不分类
    _Case("/media/forgetchu/MIDV-123.mp4", number="MIDV-123", content_type=ContentType.CENSORED),
    _Case("/media/getchu-docs/MIDV-123.mp4", number="MIDV-123", content_type=ContentType.CENSORED),
    _Case("/media/my-getchu/MIDV-123.mp4", number="MIDV-123", content_type=ContentType.CENSORED),
    # 里番/欧美须在段首; 嵌在其它词里不算
    _Case("/media/里番合集/something-123.mp4", content_type=ContentType.HENTAI),
    _Case("/media/欧美片商/MIDV-123.mp4", number="MIDV-123", content_type=ContentType.WESTERN),
    _Case("/media/非欧美/MIDV-123.mp4", number="MIDV-123", content_type=ContentType.CENSORED),
    _Case("/media/这里番号/MIDV-123.mp4", number="MIDV-123", content_type=ContentType.CENSORED),
    # 路径关键词优先于番号分类
    _Case("/media/欧美/HEYZO-123.mp4", number="HEYZO-123", content_type=ContentType.WESTERN),
    _Case("/media/里番/MIDV-123.mp4", number="MIDV-123", content_type=ContentType.HENTAI),
    # 无路径关键词: 按番号
    _Case("/media/MIDV-123.mp4", number="MIDV-123", content_type=ContentType.CENSORED),
    _Case("/media/SSIS-456.mp4", number="SSIS-456", content_type=ContentType.CENSORED),
    _Case("/media/FC2-PPV-1234567.mp4", number="FC2-1234567", content_type=ContentType.FC2),
    _Case("/media/FC2PPV1234567.mp4", number="FC2-1234567", content_type=ContentType.FC2),
    _Case("/media/HEYZO-1234.mp4", number="HEYZO-1234", content_type=ContentType.UNCENSORED),
    _Case("/media/H4610-ki221218.mp4", number="H4610-KI221218", content_type=ContentType.UNCENSORED),
    # heydouga 的两段数字都属于番号; 省略短横线与 4K / 分集尾标不改变结果
    _Case(
        "/media/heydouga-4037-1002-4K.mp4",
        definition="4K",
        number="HEYDOUGA-4037-1002",
        content_type=ContentType.UNCENSORED,
    ),
    _Case("/media/heydouga4037-1002.mp4", number="HEYDOUGA-4037-1002", content_type=ContentType.UNCENSORED),
    _Case("/media/heydouga_4037_1002-CD1.mp4", cd=1, number="HEYDOUGA-4037-1002", content_type=ContentType.UNCENSORED),
    _Case("/media/heydouga-4037.mp4", number="HEYDOUGA-4037", content_type=ContentType.UNCENSORED),
    _Case("/media/S2MBD-006.mp4", number="S2MBD-006", content_type=ContentType.UNCENSORED),
    _Case("/media/SMBD-005.mp4", number="SMBD-005", content_type=ContentType.UNCENSORED),
    _Case("/media/gachig256.mp4", number="GACHIG-256", content_type=ContentType.UNCENSORED),
    _Case("/media/DRGBD-045.mp4", number="DRGBD-045", content_type=ContentType.UNCENSORED),
    _Case("/media/SMDV-028.mp4", number="SMDV-028", content_type=ContentType.UNCENSORED),
    _Case("/media/MD0165-1.mp4", number="MD0165-1", content_type=ContentType.CHINESE),
    _Case("/media/259LUXU-1456.mp4", number="259LUXU-1456", content_type=ContentType.AMATEUR),
    _Case("/media/SIRO-4567.mp4", number="SIRO-4567", content_type=ContentType.AMATEUR),
    _Case("/media/T38-068.mp4", number="T38-068", content_type=ContentType.CENSORED),
    _Case("/media/T3800068.mp4", number="T38-068", content_type=ContentType.CENSORED),
    _Case("/media/T29-001.mp4", number="T29-001", content_type=ContentType.CENSORED),
    _Case("/media/T38-068-CD1.mp4", cd=1, number="T38-068", content_type=ContentType.CENSORED),
    _Case("/media/T38-068/video.mp4", number="T38-068", content_type=ContentType.CENSORED),
    # --- 前缀内含数字: 番号按整段提取, 类型查表 ---
    _Case("/media/R18-292.mp4", number="R18-292", content_type=ContentType.CENSORED),
    _Case("/media/R18-292-CD1.mp4", cd=1, number="R18-292", content_type=ContentType.CENSORED),
    _Case("/media/[4K]R18-292.mp4", definition="4K", number="R18-292", content_type=ContentType.CENSORED),
    _Case("/media/C0930-ki220519.mp4", number="C0930-KI220519", content_type=ContentType.UNCENSORED),
    _Case("/media/H0930-ki190101.mp4", number="H0930-KI190101", content_type=ContentType.UNCENSORED),
    _Case("/media/MCB3D-012.mp4", number="MCB3D-012", content_type=ContentType.UNCENSORED),
    # 纯数字后缀非站内写法 (该族后缀带字母段): 整段保留, 类型仍按无码前缀表
    _Case("/media/H4610-123.mp4", number="H4610-123", content_type=ContentType.UNCENSORED),
    # 单段 TH101 与短 FC2 同样非站内写法: 整段保留, 类型不从形态推断
    _Case("/media/TH101-123.mp4", number="TH101-123", content_type=ContentType.WESTERN),
    _Case("/media/FC2-123.mp4", number="FC2-123", content_type=ContentType.WESTERN),
    _Case("/media/010115-001.mp4", number="010115-001", content_type=ContentType.UNCENSORED),
    _Case("/media/010115_001.mp4", number="010115_001", content_type=ContentType.UNCENSORED),
    _Case("/media/010115_01.mp4", number="010115_01", content_type=ContentType.UNCENSORED),
    _Case("/media/010115-001/video.mp4", number="010115-001", content_type=ContentType.UNCENSORED),
    _Case("/media/38-068.mp4", number="38-068", content_type=ContentType.WESTERN),
    _Case("/media/欧美/T38-068.mp4", number="T38-068", content_type=ContentType.WESTERN),
    _Case("/media/欧美/010115-001.mp4", number="010115-001", content_type=ContentType.WESTERN),
    _Case("/media/SSNI00321.mp4", number="SSNI-321", content_type=ContentType.CENSORED),
    _Case("/media/Mywife No.1234.mp4", number="Mywife No.1234", content_type=ContentType.CENSORED),
    _Case("/media/Vixen.23.04.15.mp4", content_type=ContentType.WESTERN),
    # --- 完整路径: 马赛克可从目录名整段补; 分辨率仍只看文件名 ---
    _Case(
        "/media/uncensored/4K/MIDV-123.mp4",
        mosaic="uncensored",
        number="MIDV-123",
        content_type=ContentType.CENSORED,
    ),
    _Case(
        "/media/cracked/1080p/ABC-123.mp4",
        mosaic="cracked",
        number="ABC-123",
        content_type=ContentType.CENSORED,
    ),
    _Case("/library/censored/HD/MIDV-123.mp4", number="MIDV-123", content_type=ContentType.CENSORED),
    _Case("/media/Uncensored/MIDV-123.mp4", mosaic="uncensored", number="MIDV-123"),
    _Case("/media/无码/MIDV-123.mp4", mosaic="uncensored", number="MIDV-123"),
    _Case("/media/無碼/MIDV-123.mp4", mosaic="uncensored", number="MIDV-123"),
    _Case("/media/LEAKED/MIDV-123.mp4", mosaic="leaked", number="MIDV-123"),
    _Case("/media/破解/MIDV-123.mp4", mosaic="cracked", number="MIDV-123"),
    _Case("/media/流出/MIDV-123.mp4", mosaic="leaked", number="MIDV-123"),
    _Case("/media/[无码]/MIDV-123.mp4", mosaic="uncensored", number="MIDV-123"),
    _Case("/media/【破解】/MIDV-123.mp4", mosaic="cracked", number="MIDV-123"),
    # 文件名优先于目录
    _Case("/media/cracked/MIDV-123-无码.mp4", mosaic="uncensored", number="MIDV-123"),
    _Case("/media/uncensored/MIDV-123-LEAKED.mp4", mosaic="leaked", number="MIDV-123"),
    # 近目录优先
    _Case("/media/uncensored/流出/MIDV-123.mp4", mosaic="leaked", number="MIDV-123"),
    _Case("/media/流出/uncensored/MIDV-123.mp4", mosaic="uncensored", number="MIDV-123"),
    # 马赛克目录反例: 子串 / 复合名 / 非词表 / -UC 当目录
    _Case("/media/uncensored-guide/MIDV-123.mp4", number="MIDV-123"),
    _Case("/media/JAV-uncensored/MIDV-123.mp4", number="MIDV-123"),
    _Case("/media/leaked-info/MIDV-123.mp4", number="MIDV-123"),
    _Case("/media/无码破解/MIDV-123.mp4", number="MIDV-123"),
    _Case("/media/流出物/MIDV-123.mp4", number="MIDV-123"),
    _Case("/media/UC/MIDV-123.mp4", number="MIDV-123"),
    _Case("/media/documentation/MIDV-123.mp4", number="MIDV-123"),
    # 分辨率不从目录读 (盘符/分类夹)
    _Case("/media/4K/MIDV-123.mp4", number="MIDV-123"),
    _Case("/media/1080p/MIDV-123.mp4", number="MIDV-123"),
    _Case("/media/HD/MIDV-123.mp4", number="MIDV-123"),
    _Case("/Volumes/4K/JAV/MIDV-123.mp4", number="MIDV-123"),
    _Case("/media/720p/ABC-123.mp4", number="ABC-123"),
    # 字幕不从目录读 (中文路径尤其不能用字符类)
    _Case("/media/字幕/MIDV-123.mp4", number="MIDV-123"),
    _Case("/media/中文字幕/MIDV-123.mp4", number="MIDV-123"),
    _Case("/media/中文/MIDV-123.mp4", number="MIDV-123"),
    _Case("/media/字幕/MIDV-123-C.mp4", has_subtitle=True, number="MIDV-123"),
    # --- 完整路径: 番号从父目录回退; 文件名已命中则不管目录 ---
    _Case("/media/MIDV-123/video.mp4", number="MIDV-123", content_type=ContentType.CENSORED),
    _Case("/media/SSIS-456/MIDV-123.mp4", number="MIDV-123", content_type=ContentType.CENSORED),
    _Case("/media/FC2-1234567/movie.mkv", number="FC2-1234567", content_type=ContentType.FC2),
    _Case("/media/HEYZO-1234/clip.mp4", number="HEYZO-1234", content_type=ContentType.UNCENSORED),
    _Case("/media/MIDV-123-uncensored/video.mp4", number="MIDV-123"),
    _Case("/media/Studio Name/video.mp4", number="VIDEO", content_type=ContentType.WESTERN),
    _Case("/media/downloads/video.mp4", number="VIDEO", content_type=ContentType.WESTERN),
    _Case("/media/Season02/video.mp4", number="VIDEO", content_type=ContentType.WESTERN),
    _Case("/media/2024-01/video.mp4", number="VIDEO", content_type=ContentType.WESTERN),
    _Case("/media/HDR10/video.mp4", number="VIDEO", content_type=ContentType.WESTERN),
    _Case("/media/DISC01/video.mp4", number="VIDEO", content_type=ContentType.WESTERN),
    _Case("/media/Vol.12/video.mp4", number="VIDEO", content_type=ContentType.WESTERN),
    _Case("/media/FC2-1111111/MIDV-123/video.mp4", number="MIDV-123"),
    _Case("/media/n1234/clip.mp4", number="n1234", content_type=ContentType.UNCENSORED),
    _Case("/media/k1363/clip.mp4", number="k1363", content_type=ContentType.UNCENSORED),
    # k 系要求字母前不是字母: MK1234 仍是普通有码番号
    _Case("/media/MK1234.mp4", number="MK-1234", content_type=ContentType.CENSORED),
    # --- 完整路径: 分集只认直接父目录 CDn/PARTn ---
    _Case("/media/MIDV-123/CD1/video.mp4", cd=1, number="MIDV-123"),
    _Case("/media/MIDV-123/PART2/video.mp4", cd=2, number="MIDV-123"),
    _Case("/media/cd2/MIDV-123.mp4", cd=2, number="MIDV-123"),
    _Case("/media/CD10/MIDV-123.mp4", cd=10, number="MIDV-123"),
    _Case("/media/CD01/MIDV-123.mp4", cd=1, number="MIDV-123"),
    _Case("/media/CD1/MIDV-123-CD2.mp4", cd=2, number="MIDV-123"),
    # 分集目录反例: 祖先 / 连字符 / 盘名 / -A / 裸数字 / CD0
    _Case("/media/CD1/other/MIDV-123.mp4", number="MIDV-123"),
    _Case("/media/CD-1/MIDV-123.mp4", number="MIDV-123"),
    _Case("/media/disc1/MIDV-123.mp4", number="MIDV-123"),
    _Case("/media/A/MIDV-123.mp4", number="MIDV-123"),
    _Case("/media/1/MIDV-123.mp4", number="MIDV-123"),
    _Case("/media/CD0/MIDV-123.mp4", number="MIDV-123"),
    _Case("/media/CDs/MIDV-123.mp4", number="MIDV-123"),
    # --- 完整路径: 目录关键词与文件名标记同时出现 ---
    _Case(
        "/incoming/batch/[無碼]MIDV-123-4K.mp4",
        mosaic="uncensored",
        definition="4K",
        number="MIDV-123",
        content_type=ContentType.CENSORED,
    ),
    _Case(
        "/media/欧美/MIDV-123-4K-無碼.mp4",
        mosaic="uncensored",
        definition="4K",
        number="MIDV-123",
        content_type=ContentType.WESTERN,
    ),
    _Case("/media/里番/foo-123-CD2.mp4", cd=2, content_type=ContentType.HENTAI),
    _Case(
        "/vol/data/library/sub/[破解]MIDV-123.1080p.mp4",
        mosaic="cracked",
        definition="1080p",
        number="MIDV-123",
        content_type=ContentType.CENSORED,
    ),
    _Case(
        "/media/videos/StudioX/MIDV-123-CD1.mp4",
        cd=1,
        number="MIDV-123",
        content_type=ContentType.CENSORED,
    ),
    _Case("/media/SSIS-456 CD2.mp4", cd=2, number="SSIS-456", content_type=ContentType.CENSORED),
    _Case("/media/[HD]SSIS-456.mp4", definition="720p", number="SSIS-456", content_type=ContentType.CENSORED),
    _Case(
        "/media/videos/MIDV-123-UC.mp4",
        has_subtitle=True,
        mosaic="cracked",
        number="MIDV-123",
        content_type=ContentType.CENSORED,
    ),
    _Case(
        "/media/欧美/ABC-123.1080p.mp4",
        definition="1080p",
        number="ABC-123",
        content_type=ContentType.WESTERN,
    ),
    _Case(
        "/media/lib/MIDV-123-UC-CD1.mp4",
        cd=1,
        has_subtitle=True,
        mosaic="cracked",
        number="MIDV-123",
        content_type=ContentType.CENSORED,
    ),
    _Case(
        "/media/SNIS-576-LTX/489155.com@SNIS-576-LTX.mp4",
        mosaic="cracked",
        number="SNIS-576",
        content_type=ContentType.CENSORED,
    ),
    _Case(
        "/incoming/MIDV-123_4K_无码.mp4",
        mosaic="uncensored",
        definition="4K",
        number="MIDV-123",
        content_type=ContentType.CENSORED,
    ),
]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.path)
def test_parse_file_info(case: _Case) -> None:
    info = parse_file_info(case.path)
    assert info.cd == case.cd
    assert info.has_subtitle is case.has_subtitle
    assert info.mosaic == case.mosaic
    assert info.definition == case.definition
    if case.number is not None:
        assert info.number == case.number
    if case.content_type is not None:
        assert info.content_type == case.content_type


# --- 无路径: 同一 parse_file_info 的字段包装 ---

PREFIX_SUFFIX_CASES: list[tuple[str, str, str]] = [
    ("MIDV-123", "MIDV", "123"),
    ("ABS-001", "ABS", "001"),
    ("T28-123", "T28", "123"),
    ("T38-068", "T38", "068"),
    ("FC2-1234567", "FC2", "1234567"),
    ("HEYZO-1234", "HEYZO", "1234"),
    ("HEYDOUGA-4037-1002", "HEYDOUGA", "4037-1002"),
    ("vixen.23.04.15", "VIXEN", "23.04.15"),
    ("MKY-HS-001", "MKY-HS", "001"),
    ("H4610-ki123456", "H4610", "ki123456"),
    ("R18-292", "R18", "292"),
    ("C0930-ki220519", "C0930", "ki220519"),
    # 识别不出番号: 前缀与剩余段都是空串
    ("", "", ""),
    ("12345", "", ""),
    ("HELLO", "", ""),
    ("MIDV", "", ""),
    ("A-1", "", ""),
    ("AA-1", "AA", "1"),
    ("東京-123", "", ""),
    ("---", "", ""),
]


@pytest.mark.parametrize(("number", "prefix", "suffix"), PREFIX_SUFFIX_CASES)
def test_split_number(number: str, prefix: str, suffix: str) -> None:
    assert split_number(number) == (prefix, suffix)


INFER_CONTENT_TYPE_CASES: list[tuple[str, str | None, ContentType]] = [
    ("MIDV-123", None, ContentType.CENSORED),
    ("SSIS-456", None, ContentType.CENSORED),
    ("MDVR-0123", None, ContentType.CENSORED),
    ("SSNI00321", None, ContentType.CENSORED),
    ("Mywife No.1234", None, ContentType.CENSORED),
    ("T28-123", None, ContentType.CENSORED),
    ("T38-068", None, ContentType.CENSORED),
    ("T3800068", None, ContentType.CENSORED),
    ("T29-001", None, ContentType.CENSORED),
    ("R18-292", None, ContentType.CENSORED),
    ("R18-315", None, ContentType.CENSORED),
    ("H4610-123", None, ContentType.UNCENSORED),
    ("C0930-ki220519", None, ContentType.UNCENSORED),
    ("H0930-ki190101", None, ContentType.UNCENSORED),
    ("MCB3D-012", None, ContentType.UNCENSORED),
    ("FC2-123", None, ContentType.WESTERN),
    ("010115-001", None, ContentType.UNCENSORED),
    ("010115_001", None, ContentType.UNCENSORED),
    ("38-068", None, ContentType.WESTERN),
    ("KIN8-123", None, ContentType.WESTERN),
    ("TH101-123-12345", None, ContentType.UNCENSORED),
    ("FC2-PPV-1234567", None, ContentType.FC2),
    ("vixen.23.04.15", None, ContentType.WESTERN),
    ("HEYZO-1234", None, ContentType.UNCENSORED),
    ("HEYDOUGA-4037-1002", None, ContentType.UNCENSORED),
    ("CWP-123", None, ContentType.UNCENSORED),
    ("n1234", None, ContentType.UNCENSORED),
    ("k1363", None, ContentType.UNCENSORED),
    ("SIRO-4567", None, ContentType.AMATEUR),
    ("259LUXU-1456", None, ContentType.AMATEUR),
    ("MD-0123", None, ContentType.CHINESE),
    ("MD0165-1", None, ContentType.CHINESE),
    ("MKY-NS-012", None, ContentType.CHINESE),
    ("VIDEO", None, ContentType.WESTERN),
    ("Weekly Update", None, ContentType.WESTERN),
    ("just a movie title", None, ContentType.WESTERN),
    ("MIDV-123", "/media/欧美/MIDV-123.mp4", ContentType.WESTERN),
    ("MIDV-123", "/media/MIDV-123.mp4", ContentType.CENSORED),
]


@pytest.mark.parametrize(("number", "file_path", "expected"), INFER_CONTENT_TYPE_CASES)
def test_infer_content_type(number: str, file_path: str | None, expected: ContentType) -> None:
    assert infer_content_type(number, file_path) == expected


UNCENSORED_CASES: list[tuple[str, bool]] = [
    ("HEYZO-1234", True),
    ("010115-001", True),
    ("010115_001", True),
    ("T38-068", False),
    ("38-068", False),
    ("S2M-001", True),
    ("C0930-ki220519", True),
    ("H0930-ki190101", True),
    ("H4610-123", True),
    ("MCB3D-012", True),
    ("CWP-123", True),
    ("n1234", True),
    ("k1363", True),
    ("BT-123", True),
    ("SKY-001", True),
    ("XXX-AV-12345", True),
    ("MKBD-S120", True),
    ("DRG-123", True),
    ("DRGBD-045", True),
    ("SMDV-028", True),
    ("vixen.23.04.15", False),
    ("MIDV-123", False),
    ("SSIS-456", False),
    ("ABP-789", False),
    ("IPX-001", False),
    ("FC2-1234567", False),
]


@pytest.mark.parametrize(("number", "expected"), UNCENSORED_CASES)
def test_is_uncensored(number: str, expected: bool) -> None:
    assert is_uncensored(number) is expected


AMATEUR_CASES: list[tuple[str, bool]] = [
    ("SIRO-1234", True),
    ("LUXU-1234", True),
    ("259LUXU-1456", True),
    ("ARA-423", True),
    ("MIDV-123", False),
]


@pytest.mark.parametrize(("number", "expected"), AMATEUR_CASES)
def test_is_amateur(number: str, expected: bool) -> None:
    assert is_amateur(number) is expected


EXTRACT_NUMBER_CASES: list[tuple[str, str | None]] = [
    ("[4K] MIDV-123 标题", "MIDV-123"),
    ("SSIS-456 FHD", "SSIS-456"),
    ("FC2-PPV-1234567 新作", "FC2-1234567"),
    ("HEYZO-1234", "HEYZO-1234"),
    ("heydouga-4037-1002 4K", "HEYDOUGA-4037-1002"),
    ("T38-068 新作", "T38-068"),
    ("[4K] R18-292 妹の爆乳は一見にしかず", "R18-292"),
    ("[4K] T38-068 标题", "T38-068"),
    # TMA 番号须从词边界起算: 字母串里的 T00122 不是 TMA, 交给 DMM 连写规则.
    ("1drpt00122", "DRPT-122"),
    ("010115-001", "010115-001"),
    ("010115_001", "010115_001"),
    ("259LUXU-1456", "259LUXU-1456"),
    # 站内写法带数字段而文件名常省略, 按标签补齐; 标签须从番号开头起算 (GARA 不含 ARA)
    ("MIUM-1000", "300MIUM-1000"),
    ("ARA-423", "261ARA-423"),
    ("261ARA-423", "261ARA-423"),
    ("GARA-006", "GARA-006"),
    ("今週の新作をお届けします", None),
    ("Weekly Update", None),
    ("", None),
    ("FC2 配信開始", None),
    ("just a movie title", None),
]


@pytest.mark.parametrize(("text", "expected"), EXTRACT_NUMBER_CASES)
def test_extract_number(text: str, expected: str | None) -> None:
    assert extract_number(text) == expected


@pytest.mark.parametrize(
    "number",
    [
        "FC2-4982441",
        "FC2PPV4982441",
        "FC2-PPV-4982441",
        "FC24982441",
        "fc2 ppv 4982441",
        "FC2 - PPV - 4982441",
        "FC2.PPV.4982441",
        "FC2_PPV_4982441",
        "FC2－4982441",
        "FC2‐4982441",
        "FC2‑4982441",
        "FC2–4982441",
        "FC2—4982441",
        "FC2−4982441",
        "ＦＣ２－ＰＰＶ－４９８２４４１",
        "FC２-4982441",
        "FC2-４９８２４４１",
        "FC4982441",
        "FC-4982441",
        "FC_4982441",
    ],
)
def test_fc2_written_variants_keep_product_id_and_route(number: str) -> None:
    assert normalize_fc2_number(number) == "FC2-4982441"
    assert extract_number(number) == "FC2-4982441"
    assert infer_content_type(number) is ContentType.FC2
    info = parse_file_info(f"/media/{number}.mp4")
    assert (info.number, info.prefix, info.content_type) == ("FC2-4982441", "FC2", ContentType.FC2)


@pytest.mark.parametrize(
    ("number", "expected"),
    [
        ("FC2-12345", "FC2-12345"),
        ("FC2PPV12345", "FC2-12345"),
        ("FC2-PPV-00001", "FC2-00001"),
        ("FC2_12345678", "FC2-12345678"),
        ("  fc2＿ppv＿123456789  ", "FC2-123456789"),
        ("FC2--PPV--12345", "FC2-12345"),
        ("FC-12345", "FC2-12345"),
        ("FC12345", "FC2-12345"),
        ("FC-24982441", "FC2-24982441"),
        ("FC24982441", "FC2-4982441"),
        ("FC50001234", "FC2-50001234"),
    ],
)
def test_fc2_normalization_keeps_all_product_digits(number: str, expected: str) -> None:
    assert normalize_fc2_number(number) == expected
    assert extract_number(number) == expected
    assert infer_content_type(number) is ContentType.FC2


@pytest.mark.parametrize(
    ("path", "cd", "subtitle"),
    [
        ("/media/FC2－PPV－4982441-CD2.mp4", 2, False),
        ("/media/FC2 - PPV - 4982441-C.mp4", None, True),
        ("/media/ＦＣ２－４９８２４４１/video.mp4", None, False),
        ("/media/FC2.PPV.4982441/CD2/video.mp4", 2, False),
        ("/media/FC2-4982441 2026.11.08.mp4", None, False),
        ("/media/gx/正常/4K688.com@fc4982441_1.mp4", 1, False),
        ("/media/FC-4982441/video.mp4", None, False),
    ],
)
def test_fc2_file_markers_and_upload_dates_do_not_replace_number(path: str, cd: int | None, subtitle: bool) -> None:
    info = parse_file_info(path)
    assert (info.number, info.prefix, info.content_type) == ("FC2-4982441", "FC2", ContentType.FC2)
    assert info.cd == cd
    assert info.has_subtitle is subtitle


@pytest.mark.parametrize(
    ("filename", "cd"),
    [
        ("4K688.com@fc4982441_1.mp4", 1),
        ("4K688.com@fc4982441_2.mp4", 2),
        ("FC2-4982441_1.mp4", 1),
        ("FC2-PPV-4982441_2.mp4", 2),
        ("FC2PPV4982441_1.mp4", 1),
        ("FC24982441_2.mp4", 2),
        ("FC-4982441_01.mp4", 1),
        ("FC_4982441_09.mp4", 9),
        ("FC2_4982441_10.mp4", 10),
        ("FC2.PPV.4982441_99.mp4", 99),
        ("ＦＣ２－４９８２４４１＿２.mp4", 2),
        ("FC2-4982441_2-C.mp4", 2),
        ("FC2-4982441_2-UC-4K.mp4", 2),
        ("FC2-4982441_2_1080p60.mp4", 2),
        ("FC2-4982441_2.2160p.HEVC.mp4", 2),
        ("FC2-4982441_2-中文字幕.mp4", 2),
        ("FC2-4982441_2-CRACKED.mp4", 2),
        ("FC2-4982441_2_LEAKED_HD.mp4", 2),
        ("fc4982441_1.srt", 1),
        ("FC2-PPV-4982441_2.ass", 2),
        ("fc4982441_1.chs.srt", 1),
        ("FC2-4982441_2.en.srt", 2),
        ("FC2-4982441_2.zh-CN.ass", 2),
        ("FC2-4982441_2.zh-Hant.srt", 2),
        ("FC2-4982441_2.ja.srt", 2),
    ],
)
def test_fc2_underscore_parts_keep_number_and_match_video_or_subtitle(filename: str, cd: int) -> None:
    for info in (parse_file_info(f"/media/{filename}"), parse_file_info(text=filename)):
        assert (info.number, info.prefix, info.content_type) == ("FC2-4982441", "FC2", ContentType.FC2)
        assert info.cd == cd


@pytest.mark.parametrize(
    "filename",
    [
        "FC2-4982441.mp4",
        "FC2_4982441.mp4",
        "FC_4982441.mp4",
        "FC2-1234_1.mp4",
        "XFC2-4982441_1.mp4",
        "0FC2-4982441_1.mp4",
        "AFC4982441_1.mp4",
        "FC2-4982441_0.mp4",
        "FC2-4982441_00.mp4",
        "FC2-4982441_001.mp4",
        "FC2-4982441_100.mp4",
        "FC2-4982441_999.mp4",
        "FC2-4982441_1080p.mp4",
        "FC2-4982441_2026.mp4",
        "FC2-4982441_2A.mp4",
        "FC2-4982441_2COPY.mp4",
        "FC2-4982441_2_COPY.mp4",
        "FC2-4982441_2_Preview.mp4",
        "FC2-4982441_2_Trailer.mp4",
        "FC2-4982441_2_2026.11.08.mp4",
        "FC2-4982441_2_1.mp4",
        "FC2-4982441_2.5.mp4",
        "FC2-4982441_2_.mp4",
        "FC2-4982441_2_CXX.mp4",
        "FC2-4982441_2_HDTV.mp4",
        "FC2-4982441_2_H264B.mp4",
        "FC2-4982441_2.English.srt",
        "HEYDOUGA_4037_1.mp4",
        "1pon_010616_001.mp4",
    ],
)
def test_fc2_underscore_parts_reject_other_numbers_and_unknown_suffixes(filename: str) -> None:
    assert parse_file_info(f"/media/{filename}").cd is None
    assert parse_file_info(text=filename).cd is None


@pytest.mark.parametrize(
    ("path", "cd"),
    [
        ("/media/CD1/FC2-4982441_2.mp4", 2),
        ("/media/FC2-4982441_2/movie.mp4", None),
        ("/media/FC2-4982441_2-CD3.mp4", 3),
        ("/media/FC2-4982441_2-PART3.mp4", 3),
    ],
)
def test_fc2_underscore_parts_use_file_markers_without_changing_explicit_cd_priority(path: str, cd: int | None) -> None:
    assert parse_file_info(path).cd == cd


@pytest.mark.parametrize(
    ("filename", "number", "cd"),
    [
        ("ABC-123_1.mp4", "ABC-123", 1),
        ("ABC-123_02.mp4", "ABC-123", 2),
        ("ABC-123_10.mp4", "ABC-123", 10),
        ("ABC-123-99.mp4", "ABC-123", 99),
        ("ABC-123-A.mp4", "ABC-123", 1),
        ("ABC-123_B.mp4", "ABC-123", 2),
        ("ABC-123-1-C.mp4", "ABC-123", 1),
        ("ABC-123_2-UC-1080p.mp4", "ABC-123", 2),
        ("ABC-123-1.chs.srt", "ABC-123", 1),
        ("ABC-123_B.en.srt", "ABC-123", 2),
        ("ABC-123-CD1.mp4", "ABC-123", 1),
        ("ABC-123 CD 2.mp4", "ABC-123", 2),
        ("ABC-123.Part1.mp4", "ABC-123", 1),
        ("ABC-123-Part2.mp4", "ABC-123", 2),
        ("ABC-123 Part 10.chs.srt", "ABC-123", 10),
        ("ABC-123-CD10.en.srt", "ABC-123", 10),
        ("R18-292_2.mp4", "R18-292", 2),
        ("T38-068_2.mp4", "T38-068", 2),
        ("T3800068_2.mp4", "T38-068", 2),
        ("SSNI00321_2.mp4", "SSNI-321", 2),
        ("SSIS123-1.mp4", "SSIS-123", 1),
        ("SSIS123-A.mp4", "SSIS-123", 1),
        ("261ARA-423_2.mp4", "261ARA-423", 2),
        ("ARA-423_2.mp4", "261ARA-423", 2),
        ("FC-1234_1.mp4", "FC-1234", 1),
        ("HEYDOUGA_4037_1_2.mp4", "HEYDOUGA-4037-1", 2),
        ("HEYDOUGA-4037-1-2.mp4", "HEYDOUGA-4037-1", 2),
        ("HEYDOUGA-4037-01_2.chs.srt", "HEYDOUGA-4037-01", 2),
        ("010616_01_2.mp4", "010616_01", 2),
        ("010616-01-2.mp4", "010616-01", 2),
        ("MD0165-1_2.mp4", "MD0165-1", 2),
        ("MD0165-1-CD2.mp4", "MD0165-1", 2),
        ("th101-123-12345_2.mp4", "th101-123-12345", 2),
        ("HEYZO1234-A.mp4", "HEYZO-1234", 1),
        ("N1234-1.mp4", "n1234", 1),
        ("K1363-B.mp4", "k1363", 2),
        ("Vixen.23.04.15-A.mp4", "Vixen.23.04.15", 1),
        ("H4610-ki221218-A.mp4", "H4610-KI221218", 1),
        ("H4610-KI221218_02.mp4", "H4610-KI221218", 2),
        ("H4610-KI221218_02.chs.srt", "H4610-KI221218", 2),
        ("ABC-123456_02.mp4", "ABC-123456", 2),
        ("ABC-123456_02.chs.srt", "ABC-123456", 2),
        ("MCB3D-012-B.mp4", "MCB3D-012", 2),
        ("Mywife No.1234-1.mp4", "Mywife No.1234", 1),
        ("BCD-1-CD2.mp4", "BCD-1", 2),
        ("BCD-1-CD2.srt", "BCD-1", 2),
        ("BCD-1_2.mp4", "BCD-1", 2),
    ],
)
def test_catalog_parts_keep_the_complete_product_number(filename: str, number: str, cd: int) -> None:
    for info in (parse_file_info(f"/media/{filename}"), parse_file_info(text=filename)):
        assert info.number == number
        assert info.cd == cd


@pytest.mark.parametrize(
    ("filename", "number"),
    [
        ("HEYDOUGA_4037_1.mp4", "HEYDOUGA-4037-1"),
        ("HEYDOUGA-4037-1.mp4", "HEYDOUGA-4037-1"),
        ("HEYDOUGA-4037-01.mp4", "HEYDOUGA-4037-01"),
        ("010616_01.mp4", "010616_01"),
        ("010616-01.mp4", "010616-01"),
        ("010616_001.mp4", "010616_001"),
        ("MD0165-1.mp4", "MD0165-1"),
        ("MD0165-1.chs.srt", "MD0165-1"),
        ("BCD-1.mp4", "BCD-1"),
        ("BCD-2.mp4", "BCD-2"),
        ("BCD-1.srt", "BCD-1"),
        ("BCD-2.chs.srt", "BCD-2"),
        ("CD-1.mp4", "CD-1"),
        ("CD-1.srt", "CD-1"),
        ("MIDV-123-CD-1.mp4", "MIDV-123"),
        ("MIDV-123-CD-1.chs.srt", "MIDV-123"),
        ("MIDV-123-PART-1.mp4", "MIDV-123"),
        ("CD1-123.mp4", "CD1-123"),
        ("PART1-123.mp4", "PART1-123"),
        ("ABC-123-C.mp4", "ABC-123"),
        ("ABC-123-UC.mp4", "ABC-123"),
        ("ABC-123-D.mp4", "ABC-123"),
        ("ABC-123_0.mp4", "ABC-123"),
        ("ABC-123_100.mp4", "ABC-123"),
        ("ABC-123_2_COPY.mp4", "ABC-123"),
        ("ABC-123_2A.mp4", "ABC-123"),
        ("ABC-123_2_HDTV.mp4", "ABC-123"),
        ("ABC-123_2.English.srt", "ABC-123"),
        ("ABC-123-CD0.mp4", "ABC-123"),
        ("ABC-123-CD00.mp4", "ABC-123"),
        ("ABC-123-CD123.mp4", "ABC-123"),
        ("ABC-123-CD01A.mp4", "ABC-123"),
        ("ABC-123-Part100.mp4", "ABC-123"),
        ("ABC-123-Part 0.mp4", "ABC-123"),
        ("ABC-123-Part 1Trailer.mp4", "ABC-123"),
    ],
)
def test_catalog_product_segments_and_invalid_markers_are_not_parts(filename: str, number: str) -> None:
    for info in (parse_file_info(f"/media/{filename}"), parse_file_info(text=filename)):
        assert info.number == number
        assert info.cd is None


@pytest.mark.parametrize(
    ("filename", "cd"),
    [
        ("a-CD1.srt", 1),
        ("b-Part 2.ass", 2),
        ("CD1.srt", 1),
        ("CD 10.srt", 10),
        ("a_1.srt", None),
        ("a-B.srt", None),
    ],
)
def test_unparsed_subtitles_only_accept_explicit_part_markers(filename: str, cd: int | None) -> None:
    assert parse_file_info(text=filename).cd == cd


@pytest.mark.parametrize(
    ("text", "number", "cd"),
    [
        ("BCD-1", "BCD-1", None),
        ("BCD-2", "BCD-2", None),
        ("CD-1", "CD-1", None),
        ("ABC-1", "ABC-1", None),
        ("XYZ-2", "XYZ-2", None),
        ("ACD-2", "ACD-2", None),
        ("PART-1", "PART-1", None),
        ("FC-1", "FC-1", None),
        ("PLAIN-1", "PLAIN-1", None),
        ("BCD-1-CD2", "BCD-1", 2),
    ],
)
def test_single_digit_catalog_text_is_not_a_part(text: str, number: str, cd: int | None) -> None:
    for info in (
        parse_file_info(text=text),
        parse_file_info(f"/media/{text}.mp4"),
        parse_file_info(text=f"{text}.srt"),
    ):
        assert info.number == number
        assert info.content_type is ContentType.WESTERN
        assert info.cd == cd


@pytest.mark.parametrize("number", ["ABCD-123", "ABC-01", "XYZ-234", "ACD-12", "PART-123", "FC-123"])
def test_catalog_numbers_keep_all_digits_without_a_part(number: str) -> None:
    for info in (parse_file_info(text=number), parse_file_info(f"/media/{number}.mp4")):
        assert info.number == number
        assert info.cd is None


@pytest.mark.parametrize(
    "number",
    [
        "",
        "FC2",
        "4982441",
        "FC-001",
        "FC-1234",
        "FC1234",
        "FC21234",
        "FC-4982441A",
        "XFC-4982441",
        "AFC4982441",
        "FC-4982441-title",
        "FC2-1234",
        "FC2-4982441-title",
        "FC2-4982441A",
        "XFC2-4982441",
        "0FC2-4982441",
        "FC22-4982441",
        "FC2PPVPPV4982441",
        "FC2-4982441-CD2",
        "FC2-4982441?query=1",
        "FC2/4982441",
        "FC2-PPV-4982441.mp4",
        "https://example.com/FC2-4982441",
    ],
)
def test_fc2_normalization_requires_a_complete_unambiguous_product_number(number: str) -> None:
    assert normalize_fc2_number(number) is None


@pytest.mark.parametrize("number", ["FC-001", "FC-1234"])
def test_short_fc_catalog_is_not_rewritten(number: str) -> None:
    assert extract_number(number) == number
    assert infer_content_type(number) is ContentType.CENSORED
    assert parse_file_info(f"/media/{number}.mp4").number == number


@pytest.mark.parametrize("number", ["FC-1", "FC-ABC", "FC-UNKNOWN"])
def test_unknown_fc_filename_does_not_become_fc2(number: str) -> None:
    info = parse_file_info(f"/media/{number}.mp4")
    assert info.number == number
    assert info.content_type is ContentType.WESTERN


def test_fc2_normalization_retains_path_keyword_override() -> None:
    info = parse_file_info("/media/欧美/FC2－4982441.mp4")
    assert info.number == "FC2-4982441"
    assert info.content_type is ContentType.WESTERN


EXTRACT_VS_PATH_CASES: list[tuple[str, str]] = [
    ("just a movie title", "just a movie title.mp4"),
]


@pytest.mark.parametrize(("text", "path"), EXTRACT_VS_PATH_CASES)
def test_extract_number_skips_filename_fallback(text: str, path: str) -> None:
    assert extract_number(text) is None
    assert parse_file_info(path).number


DETECT_CD_CASES: list[tuple[str, int | None]] = [
    ("MIDV-123-CD2.srt", 2),
    ("MIDV-123-PART2.ass", 2),
    ("MIDV-123-1.srt", 1),
    ("MIDV-123.srt", None),
    ("/media/CD2/MIDV-123.srt", None),
]


@pytest.mark.parametrize(("filename", "expected"), DETECT_CD_CASES)
def test_detect_cd(filename: str, expected: int | None) -> None:
    assert detect_cd(filename) == expected


ESCAPE_STRING_CASES: list[tuple[str, list[str], str]] = [
    ("/media/MIDV-123-4K.mp4", ["something"], "MIDV-123"),
    # 4k688 的前导 "1" 与末尾 4Ks 不影响识别 (同 DLDSS / DANDYA 的站内命名).
    ("/media/正常/4K688.com@1drpt00122_4Ks.mp4", ["4k688.com@"], "DRPT-122"),
    ("/media/正常/4K688.com@1dldss00529_4Ks.mp4", ["4k688.com@"], "DLDSS-529"),
    ("/media/正常/4K688.com@1dandya00052_4Ks.mp4", ["4k688.com@"], "DANDYA-052"),
    # 站名尾巴 + 双点日期: 之前 _WESTERN_PARSE 只允许单个分隔符, 于是落进兜底规则被当成 COM-17.
    (
        "/media/gx/正常/[中文字幕]BLACKED..17.04.15.AJ.APPLEGATE.4K-C/"
        "489155.com@[中文字幕]BLACKED..17.04.15.AJ.APPLEGATE.4K-C.mp4",
        ["489155.com@"],
        "Blacked.17.04.15",
    ),
    ("/media/Blacked..17.04.15.AJ.APPLEGATE.4K-C.mp4", [], "Blacked.17.04.15"),
]


@pytest.mark.parametrize(("path", "extra", "number"), ESCAPE_STRING_CASES)
def test_parse_file_info_escape_strings(path: str, extra: list[str], number: str) -> None:
    assert parse_file_info(path, escape_strings=extra).number == number


MOSAIC_CANONICAL_ROUNDTRIP: list[tuple[str, str]] = [
    ("MIDV-123-U.mp4", "cracked"),
    ("MIDV-123-無碼.mp4", "uncensored"),
    ("MIDV-123-流出.mp4", "leaked"),
]


@pytest.mark.parametrize(("source", "canonical"), MOSAIC_CANONICAL_ROUNDTRIP)
def test_mosaic_canonical_filename_roundtrip(source: str, canonical: str) -> None:
    info = parse_file_info(source)
    assert info.mosaic == canonical
    again = parse_file_info(f"MIDV-123-{canonical}.mp4")
    assert again.mosaic == canonical


VR_CASES: list[tuple[list[str], bool]] = [
    ([], False),
    (["単体作品", "ハイビジョン", "中出し"], False),
    (["VR"], True),
    (["vr"], True),
    (["  VR専用  "], True),
    (["8KVR"], True),
    (["ハイクオリティVR"], True),
    (["VR作品"], True),
    (["VR専用作品"], True),
    (["単体作品", "vr専用"], True),
    # 含 VR 字样的其它标签不命中
    (["VRっぽい"], False),
    (["8K VR"], False),
    (["ハイクオリティ VR"], False),
    (["ＶＲ"], False),
    # 番号不参与判定: 厂商前缀里的 VR 与含 VR 的非 VR 番号都无法区分
    (["MDVR-123"], False),
    (["SVR-123"], False),
]


@pytest.mark.parametrize(("tags", "expected"), VR_CASES)
def test_is_vr(tags: list[str], expected: bool) -> None:
    assert is_vr(tags) is expected


CJK_FALLBACK_CASES: list[tuple[str, ContentType]] = [
    # 无番号、纯中日文的标题: 兜底按里番, 而不是让"欧美"那一套源全部落空
    ("/media/gx/正常/義母 前編.mp4", ContentType.HENTAI),
    ("/media/inbox/母娘 後編.mkv", ContentType.HENTAI),
    ("/media/inbox/巨乳女教師.mp4", ContentType.HENTAI),
    # 含字母或数字的仍按欧美兜底
    ("/media/inbox/Big Tits 8.mp4", ContentType.WESTERN),
    ("/media/inbox/My Wife Scene.mp4", ContentType.WESTERN),
]


@pytest.mark.parametrize(("path", "expected"), CJK_FALLBACK_CASES)
def test_unmatched_cjk_title_falls_back_to_hentai(path: str, expected: ContentType) -> None:
    assert parse_file_info(path).content_type is expected


def test_definition_ignores_4k_inside_garbage_prefix() -> None:
    """水印里的 4K 不算分辨率: 剥掉 4k688.com@ 之后名字里没有 4K 标记."""
    info = parse_file_info("/media/正常/4K688.com@ABC-123.mp4", escape_strings=["4k688.com@"])

    assert info.number == "ABC-123"
    assert info.definition is None


def test_definition_keeps_real_4k_marker_after_garbage_prefix() -> None:
    """剥掉水印后仍然存在的 4K / 4Ks 才算: 该加后缀时照样加."""
    for name in ("4K688.com@1drpt00122_4Ks.mp4", "4K688.com@1dldss00529_4K.mp4"):
        info = parse_file_info(f"/media/正常/{name}", escape_strings=["4k688.com@"])

        assert info.definition == "4K", name


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("480p", "480p"),
        ("480P", "480p"),
        ("8k", "8K"),
        ("SD", "480p"),
        ("sd", "480p"),
        ("Hd", "720p"),
        ("2160P", None),
        ("", None),
        ("bogus", None),
    ],
)
def test_canonical_definition(value: str, expected: str | None) -> None:
    assert canonical_definition(value) == expected
