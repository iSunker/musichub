# PT 各站点音乐标签数据库与跨站映射指南 (OpenCD 锚定版)

> **文档定位**：以 **OpenCD（皇后）** 原始数据为真源基准，建立面向 PT 矩阵各目标站（猫站、蟹黄堡、憨憨、柠檬、我爱我比特、天空、家园、学校、春天、杜比等）的音乐种子全生态字段与标签数据库。
> **适用范围**：`opencd2others` 转种自动化、`PTNexus` 跨站发布规则、`easy-upload` 适配器。
> **设计哲学**：
> 1. **真源唯一**：跳过乌托邦等中间站的信息压平损耗，直接以皇后 27 个分类标签 + 17 个音乐流派 + 22 种媒介 + 8 种音频标准为基准元数据字典。
> 2. **语义映射**：不仅映射同名标签，还处理“港台”细分为“香港/台湾”、“日韩”细分为“日本/韩国”、中文声乐推断国语/粤语等深度语义转换。
> 3. **安全红线**：严禁将源站的“禁转”、“自购”、“原创”外溢勾选到任何目标站。

---

## 一、 皇后 (OpenCD) 标签基准元数据字典 (真源)

皇后作为国内历史最悠久、专精音乐分类的 PT 站点，其分类和标签体系最具音乐学完备性。

### 1. 资源主类型 (`type`)
| 选项值 (Value) | 显示名称 | 说明 | 跨站映射标准键 |
|---|---|---|---|
| `408` | 音乐 (Music) | 纯音频唱片、专辑、单曲、原声带 | `category.music` |
| `402` | 演唱会 (Vocal Concert) | 演唱会现场、Live 录像、MV 视频 | `category.mv` / `category.concert` |
| `405` | 戏剧 (Drama) | 戏曲、话剧、相声评书 | `category.drama` / `category.audiobook` |
| `409` | 其他 (Other) | 综合或未分类音频 | `category.other` |

---

### 2. 来源/音乐流派 (`source`) —— 17 种经典流派
| 序号 | 选项值 | 皇后流派名称 | 英文标识 | 跨站推荐映射 / 标签 |
|---|---|---|---|---|
| 1 | `2` | 流行(Pop) | Pop | 流行 / Pop |
| 2 | `3` | 古典(Classical) | Classical | 古典 / Classical |
| 3 | `11` | 器乐(Instrumental) | Instrumental | 纯音乐 / 器乐 |
| 4 | `4` | 原声(OST) | OST | 电影原声 / OST / 动画 / 影视 |
| 5 | `5` | 摇滚(Rock) | Rock | 摇滚 / Rock |
| 6 | `8` | 爵士(Jazz) | Jazz | 爵士 / Jazz |
| 7 | `12` | 新世纪(NewAge) | NewAge | 新世纪 / New Age / 轻音乐 |
| 8 | `13` | 舞曲(Dance) | Dance | 舞曲 / Dance / 电子 |
| 9 | `14` | 电子(Electronic) | Electronic | 电子 / Electronic |
| 10 | `15` | 民谣(Folk) | Folk | 民谣 / Folk |
| 11 | `16` | 独立(Indie) | Indie | 独立音乐 / 流行 |
| 12 | `17` | 嘻哈(Hip Hop) | Hip Hop | 说唱 / Hip-Hop |
| 13 | `18` | 音乐剧(Musical) | Musical | 音乐剧 / 舞台演出 |
| 14 | `19` | 乡村(Country) | Country | 乡村 / Country |
| 15 | `20` | 另类(Alternative) | Alternative | 另类摇滚 / 独立 |
| 16 | `21` | 世界音乐(World) | World | 世界音乐 / 民族 |
| 17 | `9` | 其他(Others) | Others | 其他 |

---

### 3. 分类标签 (`tags`) —— 27 项官方标签体系
分为 **5 大地区分类** 与 **22 大风格流派**。各站若有细分标签或区域字段，以此为直接映射依据：

| 皇后 Tag ID | 标签名称 | 维度 | 英文/特征 | 目标站常见字段对应 |
|---|---|---|---|---|
| `1` | **大陆** | 地区 | Mainland, CHN | 产地/地区: 大陆/CN；语言推断: 国语 |
| `2` | **欧美** | 地区 | Western, EU/US | 产地/地区: 欧美/Western |
| `3` | **港台** | 地区 | HK/TW | 产地/地区: 港台；可按歌手/曲目细分香港/台湾 |
| `4` | **日韩** | 地区 | JP/KR | 产地/地区: 日韩；可按语种细分日本/韩国 |
| `5` | **其它地区** | 地区 | Other | 产地/地区: 其它/Other |
| `6` | **流行(Pop)** | 流派 | Pop | 标签/流派: 流行 |
| `7` | **发烧(HiFi)** | 属性 | Hi-Fi / Audiophile | 标签: 发烧 / 无损 / Hi-Fi / 高品质 |
| `8` | **汽车(garage)** | 场景 | Car Audio | 标签: 车载 / 汽车音乐 |
| `9` | **古典(Classical)**| 流派 | Classical | 标签/流派: 古典 |
| `10` | **民族(National)** | 流派 | Folk / National | 标签/流派: 民族 / 民乐 |
| `11` | **摇滚(rock)** | 流派 | Rock | 标签/流派: 摇滚 |
| `12` | **原声(OST)** | 流派 | Soundtrack / OST | 标签: 原声 / 影视 / 动画(若含ACG) |
| `13` | **民间(Folk)** | 流派 | Traditional Folk | 标签/流派: 民谣 / 民间 |
| `14` | **乡村(Country)** | 流派 | Country | 标签/流派: 乡村 |
| `15` | **天籁(Soul)** | 流派 | Soul / Pure Vocal | 标签: 天籁 / 纯净女声 / 灵魂乐 |
| `16` | **新世纪(NewAge)** | 流派 | New Age | 标签: 新世纪 / 轻音乐 |
| `17` | **蓝调(Blues)** | 流派 | Blues | 标签/流派: 蓝调 / 布鲁斯 |
| `18` | **爵士(Jazz)** | 流派 | Jazz | 标签/流派: 爵士 |
| `19` | **金属(Metal)** | 流派 | Heavy Metal | 标签/流派: 重金属 |
| `20` | **朋克(Punk)** | 流派 | Punk | 标签/流派: 朋克 |
| `21` | **电子(Electronic)**| 流派 | Electronic / EDM | 标签/流派: 电子 |
| `22` | **儿童(Children's)**| 受众 | Children's | 标签: 儿童 / 儿歌 / 少儿 |
| `23` | **宗教(Religion)** | 流派 | Religious / Gospel| 标签: 宗教 / 赞美诗 / 佛教 |
| `24` | **雷鬼(Reggae)** | 流派 | Reggae | 标签/流派: 雷鬼 |
| `25` | **贝斯(Drum Bass)**| 流派 | Drum & Bass | 标签: 贝斯 / 低音 / 电音 |
| `26` | **说唱(Rap)** | 流派 | Hip-Hop / Rap | 标签/流派: 说唱 / 嘻哈 |
| `27` | **音乐剧(musical)** | 流派 | Musical / Theater | 标签: 音乐剧 / 歌剧 / 舞台演出 |

---

### 4. 媒介介质 (`medium`) —— 22 种专业规格
| 选项值 | 皇后介质名称 | 说明 | 跨站标准化映射 (`medium.*`) |
|---|---|---|---|
| `1` | CD | 标准红皮书音频光盘 (16bit/44.1kHz) | `medium.cd` |
| `2` | 24KCD | 24K 纯金镀膜发烧 CD | `medium.cd` |
| `3` | DSD | Direct Stream Digital 直录音频 | `medium.sacd` / `medium.other` |
| `4` | LPCD | 雨果发烧 LPCD 技术压制 | `medium.cd` |
| `5` | HDCD | 高清兼容数字 CD (High Definition Compatible Digital) | `medium.cd` |
| `6` | SACD | 超音频 CD (Super Audio CD / DSD 编码) | `medium.sacd` |
| `7` | SRCD | 超级发烧 CD | `medium.cd` |
| `8` | K2CD | JVC K2 发烧母带处理 CD | `medium.cd` |
| `9` | DTS | 多声道 DTS-CD 光盘 | `medium.cd` / `medium.dvd` |
| `10` | DAT | 数字录音带 (Digital Audio Tape) | `medium.track` / `medium.other` |
| `11` | Blu-ray | 蓝光高清音频 / 纯音频蓝光 (Pure Audio Blu-ray) | `medium.bluray` |
| `12` | HD DVD | 高清 DVD 音频 | `medium.hddvd` |
| `13` | HDTV | 电视播送音频抓轨 | `medium.hdtv` |
| `14` | DVD | DVD-Audio / DVD 音轨提取 | `medium.dvd` |
| `16` | HQCD | 高品质 CD (High Quality CD) | `medium.cd` |
| `17` | XRCD | JVC 扩展分辨率 CD (Extended Resolution CD) | `medium.cd` |
| `18` | SHM-CD | 超高材质 CD (Super High Material CD) | `medium.cd` |
| `19` | Blu-spec | 索尼蓝光技术制作 CD (Blu-spec CD) | `medium.cd` |
| `20` | Vinyl | 黑胶密纹唱片 (LP/EP 模拟抓轨) | `medium.vinyl` / `medium.other` |
| `21` | Web | 线上流媒体数字版 (Apple Music, Qobuz, Tidal, Mora) | `medium.webdl` |
| `22` | HI-RES | 高解析度音频母带 (24bit/96kHz 及以上) | `medium.lossless_music` / `medium.other` |
| `15` | Other | 其他未知或特殊介质 | `medium.other` |

---

### 5. 音频编码与标准 (`standard`) —— 8 种核心格式
| 选项值 | 皇后标准名称 | 说明 | 跨站音频编码 (`audio.*`) |
|---|---|---|---|
| `1` | 镜像(Mirror) | ISO 镜像 / NRG / MDS 原盘镜像文件 | `medium.iso` / 对应音频无损编码 |
| `2` | WAV | 微软与 IBM 线性 PCM 无损未压缩格式 | `audio.wav` |
| `4` | FLAC | 自由无损音频压缩编码 (主流通用) | `audio.flac` |
| `15` | DTS | 环绕声数字音频系统 (5.1/7.1 声道) | `audio.dts` |
| `17` | DFF | DSD 流文件格式 (Philips 标准，无 ID3 标签) | `audio.dsd` |
| `18` | DSF | 索尼 DSD 流文件格式 (支持原生 ID3 标签) | `audio.dsd` |
| `19` | DST | DSD Direct Stream Transfer 无损压缩格式 | `audio.dsd` |
| `10` | 其它(Other) | APE / TTA / ALAC / MP3 等其它音频格式 | `audio.other` |

---

## 二、 各大 PT 站点音乐标签详细数据库

以下涵盖当前 PT 生态中纳管或转种常去的各大 PT 站点，详细收录其音乐分类、表单字段标识、选项取值字典及从皇后转入时的映射规则。

---

### 1. PTerClub (猫站)
- **架构体系**：NexusPHP (高度定制表单)
- **核心定位**：对音乐有独立规范管理，媒介以音频编码/格式为主，语言有独立复选框。
- **表单字段定义**：
  - 分类: `type`
  - 媒介 (实际放音频/发行格式): `source_sel`
  - 产地/地区: `team_sel`
  - 标签复选框: `guoyu`, `yueyu`, `zhongzi`, `ensub`, `diy`, `pr`, `bim`, `jinzhuan`, `guanfang`, `mp`
- **取值字典**：
  - **分类 (`type`)**：`406` (音乐), `413` (音乐短片/MV), `418` (舞台演出)
  - **媒介 (`source_sel`)**：
    - `8`: FLAC
    - `9`: WAV
    - `5`: WEB-DL (数字专辑/流媒体)
    - `10`: ISO (原盘镜像)
    - `2`: BD Discs / `1`: UHD Discs / `7`: DVD Discs / `15`: Other
  - **地区 (`team_sel`)**：
    - `1`: 大陆 (Mainland,CHN)
    - `2`: 香港 (HKG,CHN)
    - `3`: 台湾 (TWN,CHN)
    - `4`: 欧美 (Western)
    - `5`: 韩国 (KOR)
    - `6`: 日本 (JPN)
    - `7`: 印度 (IND)
    - `8`: 其它 (Other)
  - **特色标签复选框**：
    - `guoyu="yes"`: 国语演唱/华语音乐
    - `yueyu="yes"`: 粤语演唱/香港经典
    - `mp="yes"`: MV 母盘
- **从皇后 (OpenCD) 映射规则**：
  1. 媒介映射：优先看音频格式与媒介，`FLAC` → `8`，`WAV` → `9`，`Web` → `5`，`镜像` → `10`，其余落 `15`。
  2. 地区映射：皇后 `大陆(1)` → `1`；`港台(3)` 结合歌手/标题语义区分 `香港(2)` / `台湾(3)`；`日韩(4)` 结合语种细分 `韩国(5)` / `日本(6)`；`欧美(2)` → `4`；`其它(5)` → `8`。
  3. 语言复选框：中文声乐专辑自动勾选 `guoyu="yes"`；粤曲/粤语歌自动勾选 `yueyu="yes"`；纯器乐(Instrumental)不勾选。
  4. **安全红线**：绝不勾选 `jinzhuan` (禁转)、`pr` (原创)、`bim` (自购)。

---

### 2. CrabPT (蟹黄堡)
- **架构体系**：NexusPHP (分组模式，分组编号 `[4]`)
- **核心定位**：全功能综合站，音乐在 `type=408` 专用槽位，字段独立完备。
- **表单字段定义**：
  - 分类: `type`
  - 媒介: `source_sel[4]` (注意：蟹黄堡没有 `medium_sel`)
  - 音频编码: `audiocodec_sel[4]`
  - 制作地区: `processing_sel[4]`
  - 标签多选: `tags[4][]`
- **取值字典**：
  - **分类 (`type`)**：`408` (音乐), `406` (MV)
  - **媒介 (`source_sel[4]`)**：
    - `8`: CD
    - `6`: WEB-DL
    - `7`: HDTV
    - `2`: BluRay
    - `1`: Other (实测：除明确的 WEB-DL 外，常规 CD 抓轨推荐 Other 兜底)
  - **音频编码 (`audiocodec_sel[4]`)**：
    - `10`: FLAC
    - `11`: WAV
    - `12`: APE
    - `16`: ALAC
    - `9`: MPEG (MP3)
    - `2`: AAC
    - `5`: DTS / `15`: DTS-HD / `17`: DTS-HD MA
    - `7`: LPCM
    - `6`: TrueHD
    - `23`: DSD
    - `26`: Atmos
    - `1`: Other
  - **制作地区 (`processing_sel[4]`)**：
    - `2`: 中国大陆 (CN)
    - `3`: 港台 (HK/TW)
    - `4`: 欧美 (EU/US)
    - `5`: 日本 (JP)
    - `6`: 韩国 (KR)
    - `7`: 印度 (India)
    - `1`: 其他 (Other)
  - **标签 (`tags[4][]`)**：
    - `5`: 国语
    - `6`: 粤语
    - `7`: 中字
    - `4`: 原盘DIY
    - `8`: 完结 / `19`: 合集
    - `50`: ASMR (特色音频分类)
    - `37`: 动画 (ACG / OST 适用)
- **从皇后 (OpenCD) 映射规则**：
  1. 媒介映射：皇后 `Web` → `6` (WEB-DL)；其余（CD、Hi-Res、Vinyl）→ `1` (Other)。
  2. 音频编码：直接按格式映射，`FLAC` → `10`，`WAV` → `11`，`APE` → `12`，`DFF/DSF` → `23`。
  3. 地区映射：`大陆` → `2`，`港台` → `3`，`欧美` → `4`，`日韩`中日本 → `5`，韩国 → `6`，未分出日韩 → `1` (其他)。

---

### 3. LemonHD (柠檬)
- **架构体系**：NexusPHP
- **表单字段定义**：
  - 分类: `type`
  - 媒介: `medium_sel`
  - 音频编码: `audiocodec_sel`
  - 地区/来源: `team_sel` (在柠檬的下拉中，产地映射为 team_sel)
  - 标签: `tags[]`
- **取值字典**：
  - **分类 (`type`)**：`411` (Music 音乐), `406` (Music Videos MV)
  - **媒介 (`medium_sel`)**：
    - `10`: WEB-DL
    - `1`: Blu-ray
    - `2`: DVD
    - `5`: Other (音频 CD 抓轨统一归 Other)
  - **音频编码 (`audiocodec_sel`)**：
    - `10`: FLAC
    - `11`: WAV
    - `12`: APE
    - `8`: AAC
    - `9`: LPCM
    - `6`: AC3/DD
    - `100`: Other
  - **地区 (`team_sel` / source)**：
    - `3`: CN 中国大陆
    - `2`: HK/TW 港台
    - `1`: EU/US 欧美
    - `4`: JP/KO 日韩
    - `5`: IN 印度
    - `10`: Other 其他
  - **标签 (`tags[]`)**：
    - 国语 / 粤语 / 中字 / DIY / 禁转 / untouch (柠檬爱原盘)

---

### 4. OurBits (我爱我比特)
- **架构体系**：NexusPHP
- **表单字段定义**：
  - 分类: `type`
  - 媒介: `medium_sel`
  - 音频编码: `audiocodec_sel`
  - 地区: `processing_sel`
  - 制作组: `team_sel`
  - 标签: `tags[]`
- **取值字典**：
  - **分类 (`type`)**：`Music` (音乐分类代码), `Concert` (演唱会), `Music-Video` (音乐视频)
  - **媒介 (`medium_sel`)**：
    - `CD`: CD 抓轨
    - `WEB-DL`: 数字流媒体
    - `FHD Blu-ray`: 蓝光音频
    - `DVD`: DVD 音乐
  - **音频编码 (`audiocodec_sel`)**：
    - `FLAC`, `WAV`, `APE`, `LPCM`, `AAC`, `AC3`, `MPEG` (MP3), `OPUS`, `DTS-HDMA`, `TrueHD`
  - **地区 (`processing_sel`)**：
    - `CN(中国大陆)`, `HK/TW(港台)`, `US/EU(欧美)`, `JP(日)`, `KR(韩)`, `OT(其他)`
  - **标签 (`tags[]`)**：
    - `国语`, `中字`, `DIY`, `禁转`

---

### 5. HDSky (天空)
- **架构体系**：NexusPHP
- **表单字段定义**：
  - 分类: `type`
  - 媒介: `medium_sel`
  - 音频编码: `audiocodec_sel`
  - 标签: `tags[]`
- **取值字典**：
  - **分类 (`type`)**：`HQ Audio/无损音乐` (专用音乐分类), `Music Videos/音乐MV`
  - **媒介 (`medium_sel`)**：
    - `CD`: 传统音频 CD
    - `SACD`: 超音频 SACD
    - `WEB-DL`: 数字流媒体 / Web
    - `Track`: 单轨录音
    - `Blu-ray`: 蓝光无损
    - `MiniBD` / `DVDR`
  - **音频编码 (`audiocodec_sel`)**：
    - `FLAC`, `WAV`, `APE`, `DSD`, `LPCM`, `PCM`, `MP3`, `AAC`, `OGG`, `Opus`, `ALAC`, `Other`
  - **制作组 (`team_sel`)**：
    - `HDSCD/无损音乐小组`, `Original/自制原创资源`, `Other/其他制作组或转发资源`
  - **标签 (`tags[]`)**：
    - `国语`, `粤语`, `中字`, `DIY`, `特效`, `次时代国语`, `禁转`, `限转`

---

### 6. HDHome (家园)
- **架构体系**：NexusPHP
- **表单字段定义**：
  - 分类: `type`
  - 媒介: `medium_sel`
  - 音频编码: `audiocodec_sel`
  - 地区/产地: `source_sel`
  - 标签: `tags[]`
- **取值字典**：
  - **分类 (`type`)**：`Musics` (无损音乐), `TVMusic` (演唱会与音乐视频)
  - **媒介 (`medium_sel`)**：
    - `CD`, `WEB-DL`, `Blu-ray`, `MiniBD`, `Remux`
  - **音频编码 (`audiocodec_sel`)**：
    - `FLAC`, `WAV`, `APE`, `AAC`, `AC3/DD`, `DTS`, `LPCM`, `DTS-HD`, `TrueHD`, `Other`
  - **标签 (`tags[]`)**：
    - `国语`, `粤语`, `中字`, `首发`, `保种`, `DIY`, `禁转`, `限转`

---

### 7. BTSchool (学校)
- **架构体系**：NexusPHP
- **表单字段定义**：
  - 分类: `type`
  - 媒介: `medium_sel`
  - 音频编码: `audiocodec_sel`
  - 标签: `span[]`
- **取值字典**：
  - **分类 (`type`)**：`409` (音乐/Music)
  - **媒介 (`medium_sel`)**：
    - `8`: CD
    - `10`: WEB-DL
    - `1`: Blu-ray
    - `11`: Other
  - **音频编码 (`audiocodec_sel`)**：
    - `1`: FLAC
    - `2`: APE
    - `6`: AAC
    - `4`: MP3
    - `5`: LPCM
    - `7`: Other (注：WAV 在 BTSchool 表单通常无独立槽，建议落 7 Other)

---

### 8. SSD (春天 / SpringSunday)
- **架构体系**：NexusPHP
- **表单字段定义**：
  - 分类: `type`
  - 媒介: `medium_sel`
  - 音频编码: `audiocodec_sel`
  - 地区/产地: `source_sel`
  - 标签: `tags[]`
- **取值字典**：
  - **分类 (`type`)**：`Music(音乐)` / `音乐`, `MV(音乐视频)` / `音乐视频`, `Audio(有声)`
  - **媒介 (`medium_sel`)**：
    - `CD`, `WEB-DL`, `WEBRip`, `Blu-ray`, `Remux`, `Other`
  - **音频编码 (`audiocodec_sel`)**：
    - `FLAC`, `APE`, `WAV`, `MP3`, `AAC`, `LPCM`, `OPUS`, `DTS`, `Other`
  - **产地 (`source_sel`)**：
    - `Mainland(大陆)`, `Hongkong(香港)`, `Taiwan(台湾)`, `West(欧美)`, `Japan(日本)`, `Korea(韩国)`, `Other(其他地区)`
  - **标签 (`tags[]`)**：
    - `国配` (国语), `中字`, `原生`, `禁转`, `合集`

---

### 9. HDDolby (杜比)
- **架构体系**：NexusPHP
- **表单字段定义**：
  - 分类: `type`
  - 媒介: `medium_sel`
  - 音频编码: `audiocodec_sel`
  - 标签: `tags[]`
- **取值字典**：
  - **分类 (`type`)**：`HQ Audio音乐` (映射为 `408`)
  - **媒介 (`medium_sel`)**：
    - `9`: CD
    - `6`: WEB-DL
    - `2`: Blu-ray
    - `7`: Other
  - **音频编码 (`audiocodec_sel`)**：
    - `7`: FLAC
    - `9`: WAV
    - `8`: APE
    - `10`: MP3
    - `6`: AAC
    - `11`: M4A
    - `3`: LPCM
    - `12`: Other
  - **标签 (`tags[]`)**：
    - `gy`: 国语
    - `yy`: 粤语
    - `zz`: 中字
    - `tx`: 特效
    - `diy`: DIY

---

### 10. CarPT (车PT)
- **架构体系**：NexusPHP
- **表单字段定义**：
  - 分类: `type`
  - 媒介: `medium_sel[4]`
  - 音频编码: `audiocodec_sel[4]`
- **取值字典**：
  - **分类 (`type`)**：`406` (音乐/Music)
  - **媒介 (`medium_sel[4]`)**：
    - `5`: CD
    - `2`: WEB
    - `7`: Blu-ray
    - `6`: Other
  - **音频编码 (`audiocodec_sel[4]`)**：
    - `5`: FLAC
    - `10`: wav
    - `8`: APE
    - `6`: mp3
    - `7`: AAC
    - `4`: LPCM
    - `9`: Other

---

### 11. UBits
- **架构体系**：NexusPHP
- **表单字段定义**：
  - 分类: `type`
  - 媒介: `medium_sel[4]`
  - 音频编码: `audiocodec_sel[4]`
  - 地区: `source_sel[4]`
  - 标签: `tags[4][]`
- **取值字典**：
  - **分类 (`type`)**：`406` (音乐CD(Music CD Tracker)), 演唱会(Misic Videos)
  - **媒介 (`medium_sel[4]`)**：
    - `Lossless Music` (无损音乐)
    - `Track` (分轨)
    - `流媒体(WEB-DL)`
    - `蓝光原盘(Blu-ray)`
  - **音频编码 (`audiocodec_sel[4]`)**：
    - `1`: FLAC
    - `2`: APE
    - `MP3`, `AAC`, `LPCM`, `Other`
  - **地区 (`source_sel[4]`)**：
    - `中国大陆(China Mainland)`, `中国香港(China HK)`, `中国台湾(China Taiwan)`, `欧美(Euro/American)`, `日本(Japanese)`, `韩国(Korea)`, `其它(Other)`
  - **标签 (`tags[4][]`)**：
    - `国语`, `粤语`, `中字`, `DIY`, `禁转`

---

### 12. HHanClub (憨憨)
- **架构体系**：NexusPHP (新版现代 Web 前端)
- **表单字段定义**：
  - 分类: `type`
  - 媒介: `medium_sel`
  - 音频编码: `audiocodec_sel`
  - 产地: `source_sel`
  - 标签: `tag`
- **取值字典**：
  - **分类 (`type`)**：`category.music` (音乐/无损音乐)
  - **媒介 (`medium_sel`)**：`CD`, `Track`, `WEB-DL`, `FHD Blu-ray`, `UHD Blu-ray`, `Encode`
  - **音频编码 (`audiocodec_sel`)**：`FLAC`, `APE`, `WAV`, `LPCM/PCM`, `MP2/3`, `AAC`, `OGG`, `Opus`, `ALAC`, `Other`
  - **产地 (`source_sel`)**：`大陆剧/中国`, `港剧`, `台剧`, `美剧/欧美`, `日剧`, `韩剧`, `其他`
  - **标签 (`tag`)**：`国语`, `粤语`, `中字`, `特效`, `DIY`, `转载` (注：憨憨支持转载标签 `tag.转载`), `原创`, `首发`, `禁转`

---

### 13. CHDBits (彩虹岛 / 瓷片)
- **架构体系**：NexusPHP
- **表单字段定义**：
  - 分类: `type`
  - 媒介: `medium_sel`
  - 音频编码: `audiocodec_sel`
  - 标签: `tag`
- **取值字典**：
  - **分类 (`type`)**：`Music` (音乐), `HQ Audio` (高保真音频)
  - **媒介 (`medium_sel`)**：`CD`, `WEB-DL`, `Blu-ray`, `Other`
  - **音频编码 (`audiocodec_sel`)**：`FLAC`, `WAV`, `APE`, `ALAC`, `LPCM`, `DTS`, `True-HD`, `AAC`, `AC3`, `Other`
  - **标签 (`tag`)**：`首发`, `独占`, `限转`, `国语`, `DIY`, `中字`, `特效`

---

## 三、 皇后 27 标签跨站横向全景对照矩阵

以下为 OpenCD 的 27 个分类标签转入各大 PT 站点时的核心映射对照表：

| 皇后标签 | 皇后 ID | PTerClub (猫站) | CrabPT (蟹黄堡) | LemonHD (柠檬) | SSD (春天) | HDDolby (杜比) | OurBits / CHDBits |
|---|---|---|---|---|---|---|---|
| **大陆** | 1 | `team_sel=1` (大陆) + `guoyu="yes"` | `processing_sel[4]=2` (中国大陆) + `tags[]=5` (国语) | `team_sel=3` (大陆) + `tags[]=国语` | `source_sel=Mainland` + `tags[]=国配` | `tags[]=gy` (国语) | `processing_sel=CN` + `tag=国语` |
| **欧美** | 2 | `team_sel=4` (欧美) | `processing_sel[4]=4` (欧美) | `team_sel=1` (欧美) | `source_sel=West` | — | `processing_sel=US/EU` |
| **港台** | 3 | 细分: 香港 `team_sel=2` / 台湾 `team_sel=3` | `processing_sel[4]=3` (港台) | `team_sel=2` (港台) | 细分: `Hongkong` / `Taiwan` | 粤曲勾 `tags[]=yy` | `processing_sel=HK/TW` |
| **日韩** | 4 | 细分: 日本 `team_sel=6` / 韩国 `team_sel=5` | 细分: 日本 `5` / 韩国 `6` (未分落 `1`) | `team_sel=4` (日韩) | 细分: `Japan` / `Korea` | — | 细分: `JP` / `KR` |
| **其它地区** | 5 | `team_sel=8` (其它) | `processing_sel[4]=1` (其他) | `team_sel=10` (其他) | `source_sel=Other` | — | `processing_sel=OT` |
| **流行(Pop)** | 6 | — (标题保留 Pop) | — | — | — | — | — |
| **发烧(HiFi)** | 7 | — | — | — | — | — | 属性标注 |
| **汽车(garage)**| 8 | — | — | — | — | — | — |
| **古典(Classical)**| 9 | — | — | — | — | — | — |
| **民族(National)**| 10 | — | — | — | — | — | — |
| **摇滚(rock)** | 11 | — | — | — | — | — | — |
| **原声(OST)** | 12 | 若为动画可加相关说明 | `tags[]=37` (动画，若为ACG) | — | — | — | — |
| **民间(Folk)** | 13 | — | — | — | — | — | — |
| **乡村(Country)** | 14 | — | — | — | — | — | — |
| **天籁(Soul)** | 15 | — | — | — | — | — | — |
| **新世纪(NewAge)**| 16 | — | — | — | — | — | — |
| **蓝调(Blues)** | 17 | — | — | — | — | — | — |
| **爵士(Jazz)** | 18 | — | — | — | — | — | — |
| **金属(Metal)** | 19 | — | — | — | — | — | — |
| **朋克(Punk)** | 20 | — | — | — | — | — | — |
| **电子(Electronic)**| 21 | — | — | — | — | — | — |
| **儿童(Children's)**| 22 | — | — | — | — | — | — |
| **宗教(Religion)**| 23 | — | — | — | — | — | — |
| **雷鬼(Reggae)** | 24 | — | — | — | — | — | — |
| **贝斯(Drum Bass)**| 25 | — | — | — | — | — | — |
| **说唱(Rap)** | 26 | — | — | — | — | — | — |
| **音乐剧(musical)**| 27 | `type=418` (舞台演出) | — | — | — | — | — |

> 📌 **注**：PT 综合站（如猫站、蟹黄堡、柠檬、天空等）的复选框大多侧重“影视属性”（如中字、国语、粤语、特效、HDR、DIY），而缺少 22 种小众音乐流派独立勾选项。
> 因此转种策略为：
> 1. **地区与语言标签**（大陆、港台、日韩、欧美）：100% 精确映射到目标站的地区选择器（`team_sel`, `processing_sel`, `source_sel`）与语种复选框（`guoyu`, `yueyu`）。
> 2. **音乐风格与流派**（古典、爵士、摇滚、流行等）：自动融入种子副标题（`small_descr`）或简介头部（`[b]音乐流派：[/b] 摇滚(Rock)`），确保资源元数据零丢失且易于全文检索。

---

## 四、 媒介介质与音频格式跨站映射速查表

| 皇后格式/媒介 | PTerClub (猫站) | CrabPT (蟹黄堡) | LemonHD (柠檬) | HDDolby (杜比) | CarPT (车PT) | BTSchool (学校) |
|---|---|---|---|---|---|---|
| **FLAC** | `source_sel=8` | `audiocodec=10` | `audiocodec=10` | `audiocodec=7` | `audiocodec=5` | `audiocodec=1` |
| **WAV** | `source_sel=9` | `audiocodec=11` | `audiocodec=11` | `audiocodec=9` | `audiocodec=10` | `audiocodec=7` (Other) |
| **APE** | `source_sel=15` (Other) | `audiocodec=12` | `audiocodec=12` | `audiocodec=8` | `audiocodec=8` | `audiocodec=2` |
| **DSD (DFF/DSF)**| `source_sel=15` (Other) | `audiocodec=23` | `audiocodec=100` | `audiocodec=12` | `audiocodec=9` | `audiocodec=7` (Other) |
| **MP3** | `source_sel=15` (Other) | `audiocodec=9` (MPEG) | `audiocodec=100` | `audiocodec=10` | `audiocodec=6` | `audiocodec=4` |
| **CD (介质)** | — (按格式归 `source_sel`) | `source_sel=1` (Other/8) | `medium_sel=5` (Other) | `medium_sel=9` (CD) | `medium_sel=5` (CD) | `medium_sel=8` (CD) |
| **Web (数字发行)**| `source_sel=5` (WEB-DL) | `source_sel=6` (WEB-DL) | `medium_sel=10` (WEB) | `medium_sel=6` (WEB) | `medium_sel=2` (WEB) | `medium_sel=10` (WEB) |
| **镜像 (ISO)** | `source_sel=10` (ISO) | `source_sel=1` (Other) | `medium_sel=5` (Other) | `medium_sel=7` (Other) | `medium_sel=6` (Other) | `medium_sel=11` (Other) |

---

## 五、 转种安全与防污染铁律

在从皇后向其它 PT 站点转种时，必须遵守以下五条铁律：
1. **绝对禁止勾选原创 (`pr` / `原创`)**：资源非转种者本人录制或抓轨，假冒原创会遭受发种违规封禁。
2. **绝对禁止勾选自购 (`bim` / `自购`)**：非转种者真金白银自费购买，严禁冒认。
3. **绝对禁止勾选禁转 (`jinzhuan` / `独占` / `禁转`)**：转种资源严禁在目标站打上“禁转”标记（除非源站原作者有明确授权协议要求限转）。
4. **外链图片安全规范**：严禁直接引用 OpenCD 站内图片链接（`open.cd/attachments/...`），受防盗链和鉴权影响会导致目标站图片裂图；必须经过本地图床缓存（如 Lsky Pro）转换后再行发布。
5. **全生态强制匿名发布铁律 (`uplver="yes"`)**：所有向外转种发布行为必须无条件默认勾选匿名发布（NexusPHP 标准表单参数 `uplver="yes"`）。严禁以具名方式向任何目标站发种，杜绝暴露 NAS 账号、运维者个人主页与跨站身份关联。
