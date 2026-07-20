---
name: heartrate-sensor-parser
description: XOSS心率设备BLE调试日志离线解析工具。自动识别报文、计算心率/RR间期、全量HRV时域指标，支持场景自动识别（睡眠/运动）双模式分析，输出Excel/CSV/JSON+HTML可视化报告。
metadata:
  short-description: XOSS心率BLE日志解析、HRV计算、HTML报告生成
version: V2.5.3 纯Python标准库版 + 场景自动识别 + 睡眠/运动双模式HRV分析 + 睡眠结构与质量评价卡片(hypnogram+分期时长+评分) + 运动模式细分识别(个体化归一化+动态心率区间+HRmax 190 兜底/仓库根 user_meta) + HRV指标悬停解读 + HRV时域指标表格化解读 + HTML报告(Chart.js自包含) + 心内科综合分析
device_support: XOSS X2P/X2PRO 蓝牙心率胸带
protocol: BLE GATT 0x180D / 0x2A37
function: XOSS心率日志解析、RR/心率换算、全量HRV计算、场景自动识别（睡眠/运动）、双模式HRV分析、运动模式细分识别（骑行/跑步/游泳/爬山/混合）、HTML可视化报告、心内科综合分析
output: Excel/CSV/JSON 三套数据表 + 分段运动心律波动分析报告 + HTML可视化报告(Chart.js内联+心内科分析)
agent_created: true
---
# heartrate-sensor-parser 技能使用手册 V2.5.3

## 一、技能概述
专用 XOSS 心率设备 BLE 调试日志离线解析工具。自动识别 2/4/6/8 字节（及扩展长度）全部心率报文，提取设备固件/SN/电量等参数，批量计算真实瞬时心率、RR 间期、全量 HRV 时域指标（SDNN/RMSSD/pNN50/pNN20/SDSD/SDARR/CVRR/HRV三角指数/Tin），**自动识别场景（睡眠/运动）并据此动态适配分析策略和报告内容**，动态划分心率区间（静息/热身/有氧/高强度/极限），检测心律异常（RR 间期突变/疑似早搏），**对运动场景额外做运动模式细分识别（骑行/跑步/游泳/爬山/混合）**，输出标准化 Excel 三表 + CSV + JSON + 分段运动心律波动分析报告 + HTML 可视化报告（**Chart.js 已内联，离线/沙箱预览可直接出图，无需联网**）。

### V2.5.3 核心变更（对比 V2.5.2）

**爬山分类新增 `bimodal_climb` 通道**，覆盖上下山混合段中 drift_pct 未翻负的盲区。

1. **问题**：上下山 session 中当下山段不足以让整体 drift_pct 翻负时，`uphill_extreme`（需 drift_pct≥0.55）和 `downhill_signal`（需 drift_pct≤-0.05）均无法触发，爬山分类落入骑行稳态通道（`steady_cycling`）的死区。0718-ps-2（上下山，drift_pct=0.1955）被误判为骑行 0.54。

2. **`bimodal_climb` 通道**（`classify_exercise_mode.py` `classify_mode()`）—— 三组门控相乘：
   - `bim_drift_comp`：drift_pct 从 0.05 起效、0.30 饱和，排除静息/热身段
   - `bim_drift_upper`：drift_pct 超过 0.55 后衰减、0.70 归零，避免与 `uphill_extreme` 重叠
   - `hp_gate`：high_pct ≥20% 起效、40% 饱和，排除低负荷骑行
   - `bimodal_climb = bim_n² × bim_drift_comp × bim_drift_upper × hp_gate`，爬山打分加 `8.0 × bimodal_climb`

3. **设计依据**：上下山混合段表现为「中等正漂移(0.05~0.55) + 高双峰性(bim≥0.7) + 中高负荷(high_pct≥20%)」。骑行样本双峰性普遍 ≤0.67，上下山样本 ≥0.74，`bim_n²` 放大该分离度。`hp_gate` 替代 `climb`（参考样本表中已确认 climb 对骑行/跑步无区分力）作为安全阀。

4. **回归验证**（全部已知样本）—— **零回归**。骑行样本因 high_pct 低（0715-qx: 0%、0714-qx: 16.6%）被 `hp_gate` 挡下；跑步样本因 bim 低（0714-pb: 0.305）或 drift_pct 低（0701-pb: 0.005）被 `bim_n²` / `drift_comp` 挡下。

### 更早版本变更

V2.5.2 及更早版本的详细变更说明已归档到 [`references/version_history.md`](references/version_history.md)；如需完整技术细节可直接查看该文件，或回溯 git 历史（`git log --oneline SKILL.md`）。

### 前置条件
- **Python 3.8+（仅使用标准库，无需安装第三方依赖、无需 pip install）**
- **HTML 报告已内联 Chart.js，离线/沙箱预览均可正常出图，无需联网、不依赖任何 CDN**

## 二、调用方式

### 1. 完整流程（推荐）
```bash
# Step 1: 解析日志（--out 指定输出目录；产物含 分析结果.json / 心跳明细.csv / *.xlsx / *.txt）
python scripts/parse_heart_rate_log.py --log heart_rate_0701.txt --out output

# Step 2: 生成HTML报告
python scripts/generate_report.py \
  --json output/分析结果.json \
  --csv output/心跳明细.csv \
  --out output/heart_rate_report.html

# Step 3（仅睡眠场景）：注入「睡眠结构与质量评价」卡片，剥离运动图表
python scripts/inject_sleep_structure.py --out-dir output
```

### 2. 极简命令（仅解析，产物落到默认 ./output）
```bash
python scripts/parse_heart_rate_log.py --log heart_rate_0701.txt
```

### 3. 完整参数命令（解析）
```bash
python scripts/parse_heart_rate_log.py \
  --log 日志文件路径 \
  --out 输出文件夹 \
  --csv 1 \
  --json 1
```

### 4. 批量解析文件夹
```bash
python scripts/parse_heart_rate_log.py --batch ./log_folder
```

### 5. 仅生成报告（已有 JSON+CSV）
```bash
python scripts/generate_report.py \
  --json output/分析结果.json \
  --csv output/心跳明细.csv \
  --out output/heart_rate_report.html
```

参数说明：
- `parse_heart_rate_log.py --log`：必填，心率 txt/log 日志路径
- `parse_heart_rate_log.py --out`：输出目录，默认 ./output
- `parse_heart_rate_log.py --csv`：1 输出 CSV（默认 1）
- `parse_heart_rate_log.py --json`：1 输出结构化 JSON（默认 1）
- `parse_heart_rate_log.py --hr-max`：显式覆盖本次运动的 HRmax（bpm），影响运动负荷区间划分。未指定时默认 190 兜底，本次日志真跑到 ≥190 才用 P95 抬高。也可写进 `_user_meta.json` 的 `hr_max` 字段——脚本会按顺序在「日志所在目录及父级 / 输出目录及父级 / 脚本仓库根 (skill 目录) 及父级」查找，第一个命中的生效，方便跨工作区共享用户偏好。
- `parse_heart_rate_log.py --batch`：批量日志文件夹路径
- `generate_report.py --json`：分析结果 JSON 路径（必填）
- `generate_report.py --csv`：心跳明细 CSV 路径（必填）
- `generate_report.py --out`：输出 HTML 路径（默认与 JSON 同目录）

## 三、输出产物说明

### parse_heart_rate_log.py 产物
1. 心率解析汇总.xlsx：3 个 Sheet（报文总表 / RR 明细表 / HRV 汇总表）
2. 报文数据.csv / 心跳明细.csv：CSV 格式原始数据
3. 分析结果.json：结构化 JSON 结果（含 `scenario`、`exercise_mode` 等字段）
4. 分段运动心律波动分析报告.txt：九段式分析报告

### generate_report.py 产物
5. heart_rate_report.html：完整 HTML 可视化报告（Chart.js 内联，离线可用），包含：
   - 设备信息展示
   - 数据概览卡片（报文数、心跳数、平均心率、异常事件数）
   - 运动负荷评估摘要
   - 运动负荷分布环形图 + 报文分类柱状图（图表行 1）
   - **运动模式识别卡片**（仅运动场景渲染；含概率条形图modeChart）
   - **睡眠结构卡片**（仅睡眠场景渲染；）
   - HRV 指标数值面板：每个指标卡片**鼠标悬停即在该指标旁弹出其解读**（解读文本取自「心内科综合分析 → 二、HRV 时域指标逐项解读」，按指标名精确匹配;）
   - HRV-ms / HRV-% 柱状图、心率趋势折线图（图表行 2）
   - 心律异常事件散点图
   - **心内科综合分析**（五段式：总体评价 → HRV 表格化逐项解读 → 运动负荷/睡眠结构分析 → 异常事件分析 → 结论与建议）

> 睡眠场景：不渲染运动模式识别卡片，标题切换为「睡眠心率 HRV 分析报告」，图表数为 6 张。

## 四、支持报文规格
- 2 字节：Flags + HR + 0组RR
- 4 字节：Flags + HR + 1组RR
- 6 字节：Flags + HR + 2组RR
- 8 字节：Flags + HR + 3组RR
- 扩展长度（>8 字节）：自动识别多组RR

换算公式：
- RR_raw(小端16位) → RR_ms = RR_raw / 1024 * 1000
- 瞬时心率 = 60 / (RR_raw / 1024)

## 五、HRV 指标说明

| 指标 | 说明 |
|------|------|
| 摘要 | 交感神经只会持续抬高心率，无交替起伏；迷走神经会制造快速、小幅的心跳起伏，产生相邻 RR 长短差 |
| SDNN | 全部 RR 间期的标准差，反映交感+迷走神经总体心率变异性 |
| RMSSD | 相邻 RR 差值平方均值的根号，反映迷走神经短时调节功能，评估副交感最敏感指标 |
| SDSD | 相邻 RR 差值的标准差，反映迷走神经活性 |
| SDARR | 相邻 RR 差值绝对值的标准差，反映逐跳心跳快慢变化幅度的稳定程度 |
| pNN50 | 相邻 RR 差值 >50ms 的百分比 |
| pNN20 | 相邻 RR 差值 >20ms 的百分比 |
| CVRR | SDNN/平均 RR×100%，变异系数 |
| HRV 三角指数 | 总心跳数/最大频数 bin 的值，基于直方图频数分布 |
| Tin | RR 间期中位数，剔除早搏/异常干扰后的真实基础窦性心跳间隔 |

## 六、运动模式细分识别（classify_exercise_mode.py）

> 设计定位：**启发式估算，非硬判定**。单凭逐拍 RR/瞬时心率无法高置信区分运动模式（各模式 HR 区间高度重叠），故输出带置信度、低置信兜底（混合）与说明文案。睡眠/静息场景不调用本分类器（`applicable=False`）。

### 6.1 判别特征（9 个，标准库时域特征）
全部由 rr_ms / inst_hr 序列及汇总字段计算：

| # | 特征 | 计算 | 指向模式 |
|---|------|------|----------|
| F1 | intermittency 间歇指数 | 1Hz HR 序列中短窗(≤12s) HR 下跌 >15bpm 事件数 / 10min | 爬山/间歇 |
| F2 | spike_rate 尖峰率 | 短窗(≤12s) HR 上涨 >15bpm 事件数 / 10min | 爬山/间歇 |
| F3 | variability 平稳度 | 30s 滚动窗 HR 标准差均值(bpm)，越低越稳态 | 骑行 |
| F4 | bimodality 双峰性 | 2-means 方差缩减比(0~1)，越高越双峰 | 爬山 |
| F5 | mean_load 平均负荷 | 平均瞬时心率(bpm) | 游泳（偏低且平稳） |
| F6 | drift HR 漂移 | **分段斜率**：1Hz 序列分 5 段取均值再线性回归(bpm/h)，抗秒级噪声、反映整体趋势；原整段斜率保留为 `drift_raw` 供调试 | 跑步（平稳上行趋势） |
| F7 | 区间分布 | 透传 exercise_segments 各区间占比 | 多模式 |
| F8 | gap_ratio 连续性 | (有效RR首末跨度 − RR累加时长)/首末跨度，粗略缺口率 | 游泳辅助 |
| F9 | drr_tail_ratio dRR 尾部比率 | 伪迹预筛后 P95(\|dRR\|)/median(\|dRR\|)，RR 差分右尾厚重度 | 游泳/爬山上尾重、骑行/跑步平滑 |
| F10 | hr_std_of_std 二阶变异 (V2.4.6) | 30s 滚动窗 HR std 序列（每 5s 步进）的 std，衡量"变异幅度的变异性" | 跑步 ≥5、稳态骑行 ≤3.5，作为跑步 drift_n / sustained_high 的 sostd_gate |

附加量（`double_rr_rate` / `climb` / `hr_std_cv / rr_diff_ac1 / dhr_zcr`）及历史加权回退决策详见 [`references/exercise_mode_samples.md`](references/exercise_mode_samples.md)（"附加量与历史设计决策"节）。

### 6.2 信号质量门控（低置信兜底）
满足任一即强制 `low_confidence=True`、`dominant="混合"`、`poor_signal=True`，**不输出具体模式概率**（scores 置空、4 模式概率写 0）：
- `gap_ratio > 0.2`（真实中途掉线 / 严重缺口）
- `double_rr_rate > 0.05`（双 RR / 漏搏伪迹过多）

### 6.3 打分与概率
- 9 特征经各模式加权打分（登山重结构分+双峰+`bimodal_climb`中等漂移双峰通道、骑行重负荷+低间歇+`steady_cycling`、跑步重负荷+漂移、游泳反负荷反平稳+高缺口），`softmax` 归一化为 5 模式概率。
- 低置信触发：`margin < 0.12`（最大两模式概率差过小）或 `p_max < 0.34`（最高概率过低）→ `low_confidence=True`、`dominant="混合"`。
- 置信度等级：高 / 中 / 低。报告徽章配色与 `MODE_COLORS` 一致（跑步红 / 骑行蓝 / 游泳绿 / 爬山橙 / 混合紫）。
- **间歇运动难分标记 `interval_ambiguous`**：当样本呈「高间歇 + 高尖峰 + 无漂移」形态（典型如高强度间歇跑/骑行，二者心率形态高度重叠）时置 True。此时即便概率 ≥0.6，报告徽章也从「高置信」**降级为「中置信(难分)」**，避免武断高置信判骑行。

### 6.4 报告卡片呈现规则

卡片位置、标题、描述句、`modeChart` 图表、橙色 GPS 提示、蓝色难分提示的完整文案与呈现细节详见 [`references/exercise_mode_samples.md`](references/exercise_mode_samples.md)（"6.4 报告卡片呈现规则"节）。

### 6.5 已知样本表现（浓缩）

每类模式列一个代表性样本，完整 24 样本表移至 [references/exercise_mode_samples.md](references/exercise_mode_samples.md)（含 drift_pct / high_pct / var / sostd / tail / endH 全量指标）。

| 类别 | 代表样本 | 结果 | 关键指标 | 说明 |
|------|----------|------|----------|------|
| 跑步-高变异 | 0701-pb | 跑步 0.55 | sostd 9.93 / var 13.28 | 高变异跑，sostd 主导 |
| 跑步-稳态高负荷 | 0714-pb | 跑步 0.56 | high_pct 87.4 / endH 1.00 | endurance_high 独立通道救回 |
| 爬山-上山 | 0712_ps-1 | 爬山 0.97 | drift_pct 0.800 / uphill_extreme | uphill+endurance 双满档 |
| 爬山-上下山 | 0712_ps-2 | 爬山 0.71 | drift_pct -0.234 双峰 | downhill_signal 触发 |
| 爬山-上下山(未翻负) | 0718-ps-2 | 爬山 0.69 | drift_pct 0.196 / bim 0.76 / high_pct 39.4 | bimodal_climb 通道救回 |
| 骑行-稳态 | 0702-qx-2 | 骑行 0.57 | sostd 1.29 / var 2.53 / high_pct 2.4 | steady_cycling=0.73 |
| 骑行-间歇 | 0715-qx | 骑行 0.47 | sostd 1.69 / high_pct 0.0 | steady_cycling=0.40 |
| 睡眠 | 0706-sp | 场景=睡眠不进分类器 | 静息占比 98.7% / HR 55.5 | detect_scenario 四维评分全命中 |
| 诚实降级 | 0709-qx-2 | 混合 0.45 (LC+IA) | tail 60.7 高间歇 | steady_cycling=0，无救回条件 |

> 边界与经验教训详见 references/exercise_mode_samples.md 末尾三段（个体化归一化的适用边界、死结案例 0709-qx-1 的破局思路、骑行结构对称化 V2.4.8）。

## 七、心内科分析引擎说明
`CardioAnalyzer` 类内置于 `generate_report.py`：

### 信号质量评估
自动检测传感器伪差特征：运动中 RMSSD 异常偏高（>60ms）、高 SDNN 低三角指数分离（SDNN>80 且三角指数<12）、pNN50 与 pNN20 过于接近（比值>0.9）、精确 50%/100% RR 突变比例（倍半伪差模式）。输出可信度评级：高/中/低。

### HRV 指标解读
结合平均心率水平，对每项指标给出运动场景下的正常/异常判断（运动中 SDNN 通常 30-80ms、RMSSD 通常 <40ms、pNN50 通常 <3%、CVRR 通常 5-12%）。

### 异常事件分析
区分生理性 RR 波动（运动强度转换）与传感器伪差（精确倍半关系）；早搏临床风险评估（阈值 >1% 或 >100 次/小时）；异常事件时间集中性分析。

## 八、常见问题排查
1. 无 ch=2A37 数据：日志不含 XOSS 心率 BLE.RX 记录
2. 心率数值异常：脚本自动过滤 30~220bpm 外噪声
3. 固件版本未提取：日志中需包含 `fw=` 或 `firmware=` 字段
4. xlsx 打开乱码：确保使用 Excel 或 WPS 打开，非文本编辑器
5. **HTML 图表不显示（V2.4.0 已内联，通常不再发生）**：报告已内联 Chart.js，离线/沙箱预览应直接出图。若仍空白，先排查是否旧报告（含 `cdn.jsdelivr` 外链）；新报告应是 0 个 CDN 引用、含内联 `<script>window.Chart=...</script>`。空白多为 JS 语法错误导致整段中断——用 `node -e "new Function(html.match(/<script>([\s\S]*?)<\/script>/g)...) "`做语法校验，确认运动场景应有 7 个 `new Chart` 实例、睡眠 6 个。
6. **HTML 报告心内科分析为空**：检查 JSON 是否含完整 `hrv_metrics` 字段
7. **运动模式概率偏低/误判骑行为混合**：属启发式分类器固有局限（各模式 HR 区间重叠大）；低置信属诚实降级，最终判定须融合 GPS/踏频/海拔/加速度计。

历史已修复条目（运动负荷动态化、Plan A 缺口率、sustained_high 四门控、interval_ambiguous、极限档 HRmax 三级策略、drift_pct 个体化归一化、sostd_gate、endurance_high、steady_cycling、bimodal_climb、百分比精度等）详见 [`references/version_history.md`](references/version_history.md)，按版本号检索即可。

## 九、历史变更附录

完整版本历史（V2.5.3 → V2.2.x，含每版设计动机、门控公式、回归结果）已归档到 [`references/version_history.md`](references/version_history.md)。SKILL 主文档只保留当前版本 V2.5.3 详解；历史版本细节按需查该文件或 `git log --oneline SKILL.md`。
