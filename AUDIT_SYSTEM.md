# Audit → Auto Repair → LLM Review 系统说明

> 本文档描述产品化后的完整小说恢复系统。原有 V1/V2 引擎说明见 `README.md`。

## 一、为什么改架构

单靠规则 scorer 无法也不应该解决所有疑难。检测与修复的目标不同：

| 层 | 目标 | 失败代价 |
|---|---|---|
| **Audit / Detection** | **高 Recall** —— 尽量不漏 | 误报可以交给 LLM |
| **Auto Repair** | **高 Precision** —— 只改有把握的 | 误删破坏正文，代价高 |
| **LLM Review** | 在引擎给的候选里做选择 | 不确定性可显式记录 |

因此系统从"一个试图自己解决所有问题的清洗算法"升级为
"高召回审查 + 高精度自动修复 + LLM 疑难处理"。

## 二、链路

```
原始小说 (txt)
   ↓  processor.read_text / t2s.to_simplified
   ↓  AuditLayer.scan_book                       ← 只读，高召回
异常区域 AuditEvent（HIGH / MEDIUM / LOW）
   ↓
   ├─ HIGH + 多证据印证 + 引擎增益达标 → Auto Repair（V3 引擎，冻结）
   ├─ 其余 HIGH / MEDIUM             → Review Queue → LLM Resolver
   └─ LOW                            → 仅记录，不修改
   ↓  在**原文坐标**上一次性落盘
cleaned book
   ↓  再次 Audit（确认重要异常是否还在）
final audit / 审计报告 / review queue / change log
```

## 三、模块

| 文件 | 职责 |
|---|---|
| `adclean/audit.py` | **Audit 层**。只读扫描 → `AuditEvent`（区域级，带 severity 与 evidence） |
| `adclean/review.py` | `ReviewItem` / `CandidateRepair` / `LLMResolver` 协议 / `build_prompt` / `parse_decision` / `apply_decision` |
| `adclean/llm.py` | **OpenAI 兼容 LLM 接入**：配置持久化、拉取模型、`chat/completions`、重试与超时、并发批量解析 |
| `adclean/pipeline.py` | 编排：Audit → Auto Repair → Review → Final Audit，输出三件套 |
| `adclean/server.py` | **零依赖本地 Web UI**（stdlib `http.server`）+ REST API |
| `adclean/static/index.html` | 单文件前端（原生 HTML/CSS/JS，无构建步骤） |
| `adclean/v3engine.py` | **Auto Repair Engine**（冻结）：detector + StructureDetector + Candidate Generator V2 / Beam + clean n-gram + multi-round |
| `adclean/structure.py` | 结构异常检测（冻结） |
| `adclean/v2lm.py` | clean 字符级 n-gram（冻结） |

依赖关系是**单向**的：`pipeline → audit → v3engine`。
Audit 只调用 `v3engine` 的两个只读接口：

```python
V3Engine.suspicion(chars, mask, i) -> float
StructureDetector.detect(chars, mask)   -> [Anomaly(pos, kind, conf, span)]
```

Audit **不**做任何删除决策，Auto Repair **不**参与区域分级。

### 三种修复形态

| 形态 | 谁做 | 说明 |
|---|---|---|
| **删除** | Auto Repair / LLM | 主路径。引擎给出删除集，候选里选 |
| **强证据精确删除** | Auto Repair | 只删 `coverage_gap`/不可见字符命中的位置，不照搬引擎的整个删除集 |
| **替换** | **只有 LLM** | 引擎只做 DELETE。`灰𰰡𰰡的天空` 其实该还原成 `灰蒙蒙的天空`，靠 LLM 的 `final_text` |

实测：`𰰡` 是**被替换掉的真字 `蒙` 的占位**（`灰蒙蒙`/`蒙蔽`/`坑蒙拐骗`/`荷尔蒙`/`蒙面歌后`
全部一致），`𰡻` 是 `瑙`（`红玛瑙`）。用"知道映射的模拟 LLM"跑：

| 书 | 乱码字 | 处理后残留 |
|---|---|---|
| 明日方舟也太真实了吧 | 561 | **0** |
| 人在地错，从捡到精灵少女开始 | 77 | **0** |

## 四、数据结构

### AuditEvent（区域级）

```json
{
  "book_id": "...", "chapter_id": "第12章",
  "lines": [1234, 1236], "start": 45210, "end": 45298, "n_chars": 88,
  "severity": "HIGH",
  "evidence": [
    {"type": "statistical_anomaly", "score": 3.2, "detail": "全书仅 4 次/数字同音"},
    {"type": "structure_anomaly", "score": 4.0, "detail": "括号类型不匹配「【…）」"},
    {"type": "homophone_density", "score": 4.0, "detail": "本行数字同音字 8 个"}
  ],
  "n_evidence": 19, "density": 0.38, "max_conf": 4.5,
  "has_structure": true, "has_invisible": false,
  "auto_repair": true, "llm_review": false,
  "candidate_count": 6, "applied_candidate": "candidate_1", "note": ""
}
```

证据类型：`invisible_char` / `statistical_anomaly` / `structure_anomaly` /
`homophone_density`。

### ReviewItem（交给 LLM）

```json
{
  "item_id": "book.txt:L9330", "book_id": "...", "chapter_id": "第12章",
  "lines": [9330, 9330], "start": 2331011, "end": 2331073,
  "severity": "HIGH",
  "context_before": "...", "suspicious_text": "...", "context_after": "...",
  "evidence": [{"type": "statistical_anomaly", "score": 3.2}],
  "candidate_repairs": [
    {"id": "candidate_1", "delete_ranges": [[2331020,2331021]], "score": -398.2, "removed_text": "亿"},
    {"id": "candidate_6", "delete_ranges": [], "score": -484.0}
  ],
  "decision": "repair", "selected_candidate": "candidate_1",
  "confidence": 0.95, "reason": "..."
}
```

**candidate_repairs 全部来自 Auto Repair 引擎真实评估过的候选池**（按整行
n-gram 口径排序），LLM 只做选择，不自由改写。`delete_ranges` 使用绝对字符偏移。

### ChangeLog（每次修改可审计）

```json
{
  "location": {"chapter": "第12章", "line": 9330, "start": 2331011, "end": 2331073},
  "original": "亿li :ng易企武久;镹爸越已", "replacement": "",
  "method": "rule_engine", "confidence": 0.83, "margin": 85.78,
  "evidence": [...], "candidate_id": "candidate_1", "severity": "HIGH", "round": 1
}
```

LLM 修改额外记录 `resolver` / `prompt_version` / `selected_candidate` / `reason`。

## 五、拓扑：V3 无门禁，Audit/Review 只是旁路

> **这一章是 2026-10-07 重写的。** 之前这里写的是一套"门控"（severity + 独立证据
> + 密度 + margin 四道闸），它确实把误删压下去了，但也让 V3 的修复能力大幅缩水：
> 400 条 benchmark 的行级召回从 75.9% 掉到 65.5%，多本真实书的自动修复量腰斩。
> 那套门控已经**全部删除**，理由见下面的铁律。

### 唯一允许的拓扑

```text
                    ┌──→ V3 Auto Repair   主引擎：逐行 clean_line，决策原样生效
原文 ───────────────┤
                    └──→ Audit           只读旁路：异常区域 + 证据（高召回）
                                ↓
                         Review Queue     priority 1/2 送 LLM，3 只记录
                                ↓
                         LLM Review       operations（可自由提出局部修复）
                                ↓
                         Final Audit      有界（最多再 1 轮）
```

### 三条铁律

1. **Audit 永远不能成为 V3 的门禁。** 禁止 `Audit → 决定 V3 是否运行 → V3`，
   也禁止 `Audit 没发现 → V3 不许处理`。V3 不看 severity、不看证据数、
   不看污染密度、不做 top-N 截断、不把删除裁剪到"强证据位置"。
   * 测试：`test_audit_cannot_block_v3`（把 Audit 屏蔽成 0 事件，V3 仍须完成修复）、
     `test_audit_never_gates_engine_even_with_impossible_thresholds`。
2. **默认（不接 LLM）时，主链路输出与"直接用 V3 清洗"逐字节相同。**
   * 测试：`test_v3_only_equals_pipeline_default`；回归脚本里的
     `byte-identical to baseline: YES`。
3. **LLM 不受引擎候选集合的限制。** 它可以 keep / 删字符 / 删连续区间 /
   提出自己的局部替换；唯一硬约束是只能改 Review Region（那一行）内的字符，
   越界 operations 整条拒绝（`_op_apply`）。

### 真实词条为什么会被误删 / 现在怎么解决

**根因不在 scorer，而在语料。** 两个 P0 bug（都已修复）：

| # | Bug | 证据 | 后果 |
|---|---|---|---|
| P0-a | `t2s` 繁→简表把 **4014 个常用字**映射成非 BMP 扩展区字符（`蒙`→`𰰡`、`瑙`→`𰡻`、`讚`→`𬤮`） | 一本 2,076,846 字的书原文 0 个非 BMP，过一遍 `to_simplified` 变成 57 个 | 工具**自己制造乱码**，然后再去"清洗"它们 |
| P0-b | 语言模型是在 P0-a 期间训练的，`蒙`/`瑙` 在 29,169,141 字语料里**词频为 0** | `西蒙`→`蒙`、`红玛瑙般`→`红的`、`灰蒙蒙`→`的死寂`、`病原菌`→`病菌`、`承蒙厚爱`→`蒙大家厚爱` | V3 把真字当"全书从未出现的异常字"删掉，**损坏正文** |

修复方式：
* `t2s` 表重建（`tools/build_t2s.py`）：值必须是 BMP、键≠值、常见简体字必须恒等，
  并内置三组自检（`tests/test_t2s.py` 固化）。
* 语料模型重建：用修好的 t2s 重跑原 14 本书 → 全量模型 A0c →
  同一套行过滤判据 → 生产模型 `.adclean-lm-a5.json`。
  现在 `蒙` 1276 次 / `玛` 8604 次 / `般` 14374 次 / `赞` 1588 次，
  而被过滤掉的只有 18 个真正的注入字（`捌 俬 熘 祁 陕 镹 翏 玐 轳 鹨 峮 镏 箘 厁 儛 邬 帬 裠`）。

### 送审优先级（只影响"是否/先送 LLM"，不影响 V3）

| 优先级 | 判据 | 送 LLM |
|---|---|---|
| 1 | 引擎动过，但删掉了**常用字**（语料词频 > `common_char_freq`，默认 2000），或把整行删掉一半以上且 ≥ 8 字 | 是 |
| 2 | 引擎动过但外部独立证据（不含 `engine_proposal`）少于 `weak_support_evidence`（默认 2）；或引擎没动而审查层仍认为可疑 | 是 |
| 3 | 引擎动过且证据充分（HIGH 置信 → 交给规则引擎，只记录） | 否 |

### 实测：这些数字是怎么来的

* 400 条 benchmark（beam_v2 / merge off）：
  * β=0：row_P 86.3 / row_R 75.9 / row_F1 80.7，char_P 94.7 / char_R 60.6 / char_F1 73.9，FP 7
  * β=2（生产）：char_P 94.4 / char_R 64.4 / **char_F1 76.6**，其余不变
* 三行 hard case：10116 char_R 81.8、10147 与 10395 均 100/100。
* 审计层：157 个事件（HIGH 54 / MEDIUM 87 / LOW 16），脏行召回 98.3%，
  正常行误报 18.4%（故意偏 recall —— 漏掉真污染比多报一个区域严重）。

### ⚠️ 生产入口必须用 `build_engine()`

```python
from adclean.pipeline import build_engine      # ✅ 正确
engine = build_engine()                        # 自动加载 A4 语料 + 冻结配置

from adclean.v3engine import V3Engine          # ❌ 错误
engine = V3Engine(LanguageModel.load(path))    # 会用 V3Config 默认值
```

`V3Config()` 的默认值是 `score_mode="avg"` + `candidate_mode="v1"` +
`insert_prior=0.02`，**与本项目验证过的配置完全不同**。
冻结配置保存在 `adclean.pipeline.FROZEN_V3`，
`tests/test_audit_pipeline.py::test_build_engine_uses_frozen_config` 会守住它。

## 六、用法

```bash
# 单本 / 整目录
python -m adclean "D:\小说" -o "D:\小说_已审查"

# 导出 prompt 供外部 LLM 批处理
python -m adclean "D:\小说" -o out --dump-prompts out/prompts.json

# 把外部 LLM / 人工的决策回填后重跑
python -m adclean "D:\小说" -o out2 --resolve decisions.json
```

每本书输出四个文件：

```
<书名>.txt                  最终恢复文本
<书名>.audit.json           审计报告（区域数、severity 分布、修复统计）
<书名>.review_queue.json    未自动处理的疑难区域（结构化）
<书名>.changelog.json       逐条修改日志（可回滚 / 可训练）
<书名>.events.json          全部异常区域（initial / final，可审计）
```

### 接入真实 LLM

```python
from adclean.review import CallableResolver, build_prompt, parse_decision
from adclean.pipeline import run_files

def my_llm(item):
    prompt = build_prompt(item)          # 已含 上下文 + 异常证据 + 候选方案
    raw = call_your_model(prompt)        # 你自己接
    return parse_decision(raw)           # 强制结构化；解析失败 → uncertain

run_files(["book.txt"], "out", engine, cfg,
          resolver=CallableResolver(my_llm, name="gpt-x"))
```

`parse_decision` 只接受：

```json
{"decision": "repair|keep|uncertain", "selected_candidate": "candidate_2",
 "confidence": 0.96, "reason": "...", "final_text": "仅在需要替换时填写"}
```

非法 JSON / 非法 decision / 无效候选 id → 一律降级为 `uncertain`，**绝不猜**。

`final_text` 用于**替换型**修复（引擎只能删，替换只有 LLM 能做）。
它落地时走 `line_override`（整行覆盖），并在 `changelog` 里记录
`"replacement": "蒙蒙"` 以便审计。纯删除型修复则与该行已有的自动修复
取**并集**，不会被覆盖。

## 七、Web UI 与 LLM 接入

启动（推荐双击项目根目录的 **`start.bat`**，一键启动并自动开浏览器）：

```bash
python -m adclean.server              # 打开 http://127.0.0.1:8777
python -m adclean.server --port 9000 --no-browser
```

`start.bat` 的行为：依次尝试 `py -3` → `python` → `python3`（要求 ≥ 3.9）→
校验 `adclean` 包在同目录 → 校验语料缓存存在（缺失只警告）→
忽略拖进来的文件参数 → 启动；出错时 `pause` 让窗口不闪退。

**两个与 Windows 相关的实现决定**（都被测试固定住）：

* **`start.bat` 只用 ASCII**。带非 ASCII 的 .bat 会按控制台代码页解析，
  而代码页因机器而异（936 / 65001 / 437…），会出现乱码甚至把命令行拆断
  （实测确认）。中文提示一律由 Python 打印 —— Python 用 Unicode 控制台 API，
  不受代码页影响。
* **Windows 上端口独占绑定**。标准库 `HTTPServer.allow_reuse_address = 1`
  会设 `SO_REUSEADDR`，而 Windows 的 `SO_REUSEADDR` **允许两个进程绑同一端口**
  → 双击两次会静默起两个实例、请求随机落到其中一个。
  因此 `AdcleanHTTPServer` 在 Windows 上改用 `SO_EXCLUSIVEADDRUSE`，
  端口被占时直接报 WinError 10048，由 `serve()` 打印友好提示并返回 1。

界面三段：**① 审计 + 自动修复**（不花钱）→ **② LLM 审查**（填自己的连接）→
**旧版引擎**（V2，保留兼容）。

### REST API

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/llm/config` | 读配置（**只回掩码，不回明文 Key**） |
| POST | `/api/llm/config` | 存配置；不带 `api_key` 则沿用已存的，`clear_key` 显式清除 |
| POST | `/api/llm/models` | 拉取模型列表（免费） |
| POST | `/api/llm/probe` | 最小连通性测试（消耗极少 token，需主动触发） |
| POST | `/api/pipeline/clean` | 审计 + 自动修复，产出审查队列 |
| POST | `/api/pipeline/review` | 接 LLM 审查队列并写回，保留 `*.auto.txt` |
| POST | `/api/job/cancel` | 取消运行中的任务 |
| GET | `/api/job?id=` | 任务进度（`percent` / `stage` / `message`） |
| GET | `/api/download?path=` | 下载任务产出（**仅限本任务产出的文件**） |

### LLM 接入层（`adclean/llm.py`）

任何 OpenAI Chat Completions 兼容的服务都能用。地址归一化会自动补版本段：

```
https://api.x.com                    → https://api.x.com/v1/chat/completions
https://api.x.com/v1                 → https://api.x.com/v1/chat/completions
https://api.x.com/v1/chat/completions→ 原样
http://localhost:11434/v1            → http://localhost:11434/v1/chat/completions
```

`GET {base}/models` 用于拉模型列表；`response_format={"type":"json_object"}`
在遇到不支持它的服务（400/404/422）时会自动去掉重试。

**并发怎么拿到的**：pipeline 仍按行串行；`run_book` 在建好队列后会调用
`resolver.prepare(items)`（若存在），由 resolver 用线程池并发问完并缓存，
之后逐条 `resolve(item)` 只读缓存。这样既拿到并发，又不必改动串行结构。

### 已确认的调用参数（默认值）

| 项 | 默认 | 位置 |
|---|---|---|
| 单请求超时 | 60 s | `LLMConfig.timeout` |
| 并发 | 2 | `LLMConfig.concurrency` |
| 失败重试 | 2 次（退避 1s / 2s） | `LLMConfig.retries` |
| **单次上限** | **50 项** | `LLMConfig.max_items`（0 = 不限） |

超出单次上限的条目**不发请求**，决策记为 `uncertain`（保持原文），
`stats.skipped` 会如实上报。

### 密钥处理

* 配置存 `<用户目录>\.adclean\llm.json`（`ADCLEAN_HOME` 可改位置），权限尽力收到 0600。
* `as_public_dict()` 不含 `api_key`，只给 `has_key` 与 `key_masked`。
* `redact()` 在**任何**落盘/回传文本里把密钥整体替换成 `***`（错误消息、报告、changelog）。
* `save_key=False` 时不落盘。
* 单元测试与端到端脚本都用 `ADCLEAN_HOME` 指向临时目录，**不会污染真实配置**。

### 结果落地

LLM 决策自动写回：

* 纯删除 → 与该行已有的自动修复取**并集**
* 替换/插入（`final_text`）→ 整行覆盖，changelog 记 `replacement`
* 同时把上一阶段的文本另存为 `*.auto.txt`（未应用 LLM），供对比与回滚

### 安全边界

* 绑定 `127.0.0.1`，不加任何 CORS 头
* POST 拒绝跨站 `Origin`（放行同源 localhost —— 浏览器对同源 POST 也会带 Origin）
* `/api/download`、`/api/reveal` 只允许访问本次任务产出的文件

## 八、实测指标（400 条 benchmark，最终门控）

| 配置 | row_P | row_R | row_F1 | char_P | char_R | char_F1 | 误删 |
|---|---|---|---|---|---|---|---|
| baseline（V3 直接清洗，无 Audit/LLM） | 86.3% | 75.9% | 80.7 | 94.7% | 60.6% | 73.9 | 7 (2.0%) |
| Audit + Auto Repair（无 LLM） | **100.0%** | 65.5% | 79.2 | **97.8%** | 56.0% | 71.2 | **0 (0.0%)** |
| **Audit + Auto + LLM 上限** | **100.0%** | **84.5%** | **91.6** | 97.8% | **62.3%** | **76.1** | **0 (0.0%)** |

分工很清楚：**Auto Repair 负责精度（char_P 94.7%→97.8%、误删 2.0%→0%）**，
**Audit+LLM 负责召回（row_R 65.5%→84.5%）**。

**Audit 能力**：真异常区域召回 **98.3%**（58 条脏行标出 57 条，漏的 1 条是
`邹`→`皱` 的 REPLACE 型，DELETE-only 引擎天然覆盖不到）；
normal 区域误报 22.2%（其中只有 5 条 HIGH，其余 MEDIUM → 进 LLM 队列）。
severity 分布：HIGH 58 / MEDIUM 98 / LOW 42。

**错误分析**：漏修 1 条（同上 REPLACE 型）；**误删 0 条**；
队列 106 项（脏行 25 / 正常行 81），其中 91 项带 ≥1 个可删候选供 LLM 选择。

"LLM 上限"= 假设 LLM 总能在候选里选中最优的那个（oracle resolver），
它衡量的是**交接天花板**，不是实际 LLM 表现。

### 全量实跑（14 本，30.4M 字）

```
30,366,073 字符 · 14 本
异常事件     33,831 → 32,986（降 845）
自动修复      1,349 行 / 9,462 字
LLM 队列     15,110 项
输出          14 本 × 5 文件 = 70 个，151.5 MB
```

| 书 | 异常事件 | auto 行/字 | 队列 |
|---|---|---|---|
| 综漫，我真的只想肝词条 | 2207→1812 | **721 / 6302** | 752 |
| 哪有人的…魔女审判 | 3521→3229 | **267 / 2540** | 1809 |
| 明日方舟也太真实了吧 | 2188→2127 | 127 / 164 | 898 |
| 精灵：开局小碎钻… | 3904→3868 | 60 / 134 | 1710 |
| 苦主竟是我妻子 | 1205→1195 | 50 / 165 | 614 |
| 人在地错… | 3927→3918 | 31 / 32 | 1568 |
| 其余 8 本 | — | 5~18 行 | 384~1772 |

自动修复高度集中在**重污染书**（两本占 93% 的修复字数），
而"强证据只删命中字符"这条规则让**稀疏污染书也能安全地清掉**
非 BMP 垃圾字（`人在地错` 0 → 31 行，每行只删 1 个垃圾字）。

## 九、已知限制（诚实记录）

1. **规则引擎的固有极限**：广告字与真字在单字层面不可分（`夯`/`钙`/`焖`/`荷`
   vs `霓`/`仪`/`另`），因此自动修复会偶发漏删/误删。这正是 LLM 队列存在的理由。
   **实测**：稀疏污染书上，引擎提的 22 个修复里有 9 个是删真词
   （`淘米焖饭`、`芝士玉米焗饭`、`荷尔蒙`）。加密度门槛后降到 1 个
   （L5179 `荷尔𰰡` 因同时满足印证门槛而走了完整删除分支）。
2. **Audit 的证据质量有假阳性**（`weak-bi` 通道会把广告两侧的真字一起标记，
   约 30%）。Audit 故意容忍误报 —— 误报进 LLM 队列，代价可接受；漏报才是严重问题。
3. **队列规模大**：全量 14 本产生 15,386 个 review item，需 LLM 批处理能力
   （每项约 60 字上下文 + 最多 6 个候选）。可用 `PipelineConfig.max_llm_items` 限流。
4. **REPLACE 场景**：引擎只做 DELETE，被广告**替换**掉的真字无法由规则引擎恢复
   （`冷静`→`冷就算`、`邹`→`皱`、`灰蒙蒙`→`灰𰰡𰰡`）。
   现在已支持由 **LLM** 通过 `final_text` 完成替换修复，实测两本书的乱码字残留归零；
   但规则引擎自动修复仍只能删除。这是当前系统最主要的能力缺口。
5. **颜文字**：含组合符号的颜文字行会被 `has_invisible` 放行而自动清理
   （实测 `₍^˶⦁༝⦁˶^₎◞̑̑` → `₍^˶⦁༝⦁˶^`）。改成"只删强证据命中字符"后
   只删掉组合符号 `̑̑`，保留了颜文字主体。
6. **门槛跨语料需重标**：benchmark 的脏行是极端样本（density / margin 都高），
   真实书的 density 中位只有 0.14~0.40。换语料应重跑 `_ad_probe/density_sweep.py`。
7. **`charclass.is_cjk` 不覆盖 CJK 扩展 B–H**（非 BMP），因此 `suspicion()` 对
   `𰡻`/`𰰡` 这类字符给 0 分。Audit 用 `coverage_gap` 证据补上了这个盲区，
   `strong_positions` 让自动修复能精确删掉它们；但**引擎本身仍看不见它们** ——
   引擎是靠 span 把邻近锚点扩进去才删到的。
8. **LLM 只测了上限**：真实 LLM 未接入，`LLM 修复成功率 / 保留正确率 /
   再次误删率` 三项指标需要接入后才能统计。当前测的是 oracle 天花板
   （row_F1 91.6 / char_F1 75.6）。


