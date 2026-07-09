---
name: heartrate-sensor-parser
description: XOSS心率设备BLE调试日志离线解析工具。自动识别报文、计算心率/RR间期、全量HRV时域指标，支持场景自动识别（睡眠/运动）双模式分析，输出Excel/CSV/JSON+HTML可视化报告。
metadata:
  short-description: XOSS心率BLE日志解析、HRV计算、HTML报告生成
version: V2.4.0 纯Python标准库版 + 场景自动识别 + 睡眠/运动双模式HRV分析 + 运动模式细分识别 + HTML报告(Chart.js自包含) + 心内科综合分析
device_support: XOSS X2P/X2PRO 蓝牙心率胸带
protocol: BLE GATT 0x180D / 0x2A37
function: XOSS心率日志解析、RR/心率换算、全量HRV计算、场景自动识别（睡眠/运动）、双模式HRV分析、运动模式细分识别（骑行/跑步/游泳/爬山/混合）、HTML可视化报告、心内科综合分析
output: Excel/CSV/JSON 三套数据表 + 分段运动心律波动分析报告 + HTML可视化报告(Chart.js内联+心内科分析)
agent_created: true
---
# heartrate-sensor-parser 技能使用手册 V2.4.0

## 一、技能概述
专用 XOSS 心率设备 BLE 调试日志离线解析工具。自动识别 2/4/6/8 字节（及扩展长度）全部心率报文，提取设备固件/SN/电量等参数，批量计算真实瞬时心率、RR 间期、全量 HRV 时域指标（SDNN/RMSSD/pNN50/pNN20/SDSD/SDARR/CVRR/HRV三角指数/Tin），**自动识别场景（睡眠/运动）并据此动态适配分析策略和报告内容**，动态划分心率区间（静息/热身/有氧/高强度/极限），检测心律异常（RR 间期突变/疑似早搏），**对运动场景额外做运动模式细分识别（骑行/跑步/游泳/爬山/混合）**，输出标准化 Excel 三表 + CSV + JSON + 分段运动心律波动分析报告 + HTML 可视化报告（**Chart.js 已内联，离线/沙箱预览可直接出图，无需联网**）。

### V2.4.0 核心变更（对比 V2.3.3）
本轮把此前未在文档中沉淀的多项能力补齐并记录，重点三处：

1. **运动模式细分识别（新增文档化）** —— `classify_exercise_mode.py` 提供 9 特征时域启发式分类器，对运动场景估算 跑步/骑行/游泳/爬山/混合 的概率分布 + 置信度。详见「六、运动模式细分识别」。


2. **报告 Chart.js 自包含（离线可用）** —— 原先 `<head>` 用 CDN 加载 Chart.js，内置预览面板（隔离环境无外网）加载失败会导致**全部图表一次性空白**。现已把 `scripts/chart.umd.min.js`（v4.4.0 UMD，205KB）内联进报告，`generate_report.py` 在生成时把 CDN 标签替换为内联 `<script>`，**零网络依赖**。
   - 报告图表数：运动场景 **7 张**（运动负荷分布环形图、报文分类柱状图、HRV-ms、HRV-%、心率趋势、异常事件散点、运动模式概率图）；睡眠场景 **6 张**（无运动模式图）。

3. **缺口率口径修正（Plan A）** —— `compute_features` 中 `real_dur_sec` 由「首末 ch=2A37 报文跨度」改为「**有效 RR 报文首→末跨度**」，剔除运动后静默段 / 无 RR 收尾尾巴。
   - 背景：0701 真实运动段仅 92.6min、缺口 2.2%，但运动后 51min 静默被旧口径算进墙钟，缺口率虚高到 0.371 → 误触发信号门控 → 100% 混合。修正后 0701 正常输出「骑行 0.63（高置信）」，与其他样本（真实中途缺口）兼容。
   - 仍保留对**真实中途掉线**的捕捉：中途长静默同样会拉大（首末跨度 − 累加RR）。

### V2.3.3 优化内容（对比 V2.3.2）
报告 HTML 图表与板块布局重组，使数据呈现更符合阅读逻辑：HRV 两张柱状图上移、HRV 数值面板下移、报告板块顺序梳理（详见文末历史附录）。

### V2.3.2 优化内容
把「心律异常事件」由表格改为散点图（canvas `anomalyChart`，横轴相对时间、纵轴 |ΔHR|、按类型分色）。

### V2.3.1 修复内容
修复「运动负荷评估与实际区间分布不一致」：评估文案改为完全基于 `exercise_segments` 各区间实际占比动态归纳（V2.3.1 前的 `build_exercise_summary` 会写死"有氧+高强度为主"）。

### V2.3.0 新增内容
**场景自动识别 + 睡眠/运动双模式分析引擎**：`detect_scenario()` 四维评分（静息占比/平均心率/记录时长/夜间时段）判定睡眠 or 运动；睡眠场景切换专用 HRV 解读、睡眠结构、信号质量阈值与心内科分析。

1. **场景自动检测** — `CardioAnalyzer.detect_scenario()` 基于四维评分自动判定场景：
   - 静息占比 > 95%（+3分）
   - 平均心率 < 65 bpm（+2分）
   - 记录时长 >= 4小时（+2分）
   - 记录时段为夜间 22:00-10:00（+2分）
   - 总分 >= 5 → 睡眠场景，否则 → 运动场景

2. **睡眠场景专用分析方法**：
   - `interpret_hrv_metrics_sleep()` — HRV指标以睡眠生理参考范围解读（SDNN 60-120ms、RMSSD 30-80ms、pNN50 20-60%）
   - `analyze_sleep_structure()` — 分析NREM/REM期心率特征、心率尖峰簇集性、睡眠连续性评估
   - `analyze_anomalies_sleep()` — 区分体位改变/觉醒引起的V型心率响应与病理性心律失常
   - `generate_conclusions_sleep()` — 包含心脏自主神经评价、睡眠质量间接评估、OSA筛查提示等
   - `assess_signal_quality_sleep()` — 睡眠场景专用的信号质量评估阈值

3. **报告模板动态适配**：
   - 标题自动切换：`心率分析报告` ↔ `睡眠心率 HRV 分析报告`
   - 摘要框切换：`运动负荷评估` ↔ `睡眠心率总体评估`
   - 图表标题切换：`运动负荷分布` ↔ `睡眠心率区间分布`
   - 心内科分析五大部分全部根据场景动态生成

### 前置条件
- Python 3.8+（仅使用标准库，无需安装第三方依赖、无需 pip install）
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
   - **运动模式识别卡片**（位于图表行 1 下方，仅运动场景渲染；含概率条形图 modeChart）
   - HRV 指标数值面板（位于运动模式识别下方）
   - HRV-ms / HRV-% 柱状图、心率趋势折线图（图表行 2）
   - 心律异常事件散点图（与上方 2 张合计：运动场景 7 张 / 睡眠场景 6 张 Chart.js 图表）
   - **心内科综合分析**（五段式：总体评价 → HRV 解读 → 运动负荷分析 → 异常事件分析 → 结论与建议）

> 睡眠场景：不渲染运动模式识别卡片，标题切换为「睡眠心率 HRV 分析报告」，图表数为 6 张。

## 四、支持报文规格
- 2 字节：Flags + HR（无RR）
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
| F6 | drift HR 漂移 | 逐拍 HR 对时间线性回归斜率(bpm/h) | 跑步（平稳上行趋势） |
| F7 | 区间分布 | 透传 exercise_segments 各区间占比 | 多模式 |
| F8 | gap_ratio 连续性 | (有效RR首末跨度 − RR累加时长)/首末跨度，粗略缺口率 | 游泳辅助 |
| F9 | drr_tail_ratio dRR 尾部比率 | 伪迹预筛后 P95(\|dRR\|)/median(\|dRR\|)，RR 差分右尾厚重度 | 游泳/爬山上尾重、骑行/跑步平滑 |

附加量 `double_rr_rate`：RRᵢ ≈ 2×邻拍(±20%) 占比，用于信号门控与 dRR 伪迹预筛（不计入 9 特征打分）。

### 6.2 信号质量门控（低置信兜底）
满足任一即强制 `low_confidence=True`、`dominant="混合"`、`poor_signal=True`，**不输出具体模式概率**（scores 置空、4 模式概率写 0）：
- `gap_ratio > 0.2`（真实中途掉线 / 严重缺口）
- `double_rr_rate > 0.05`（双 RR / 漏搏伪迹过多）

### 6.3 打分与概率
- 9 特征经各模式加权打分（登山重结构分+双峰、骑行重负荷+低间歇、跑步重负荷+漂移、游泳反负荷反平稳+高缺口），`softmax` 归一化为 5 模式概率。
- 低置信触发：`margin < 0.12`（最大两模式概率差过小）或 `p_max < 0.34`（最高概率过低）→ `low_confidence=True`、`dominant="混合"`。
- 置信度等级：高 / 中 / 低。报告徽章配色与 `MODE_COLORS` 一致（跑步红 / 骑行蓝 / 游泳绿 / 爬山橙 / 混合紫）。

### 6.4 报告卡片呈现规则
- **位置**：运动负荷分布下方（运动场景才有，睡眠不渲染）。
- **标题**：`运动模式识别：{模式} {可信度徽章}`（模式标签位于标题与徽章之间）。
- **描述句**：`基于逐拍 RR 间期 / 瞬时心率的时域形态学特征（间歇度、尖峰率、平稳度、双峰性、平均负荷、HR 漂移等）做启发式估算。`（不再带「主导模式：XX」）。
- **图表**：概率条形图 `modeChart`（5 模式真实概率条）。
- **橙色 GPS 提示**：低置信 + 中置信显示（高置信不显示）。文案：「各模式概率接近或信号不足，区分度有限，结果仅供参考。真正判定需结合 GPS / 海拔 / 踏频 / 加速度计等传感器数据。」
- **已移除**：图表下方逐行概率表格、灰色 caveat 小字。

### 6.5 已知样本表现（用户标注对照）
| 日志 | 用户标注 | 分类结果 | 说明 |
|------|----------|----------|------|
| 0701 | 骑行+跑步 | 骑行 0.63（高置信） | gap 修正后门控放行，与标注吻合 |
| 0709-2 | 骑行 | 骑行 0.576（中置信） | 诚实输出，未过度自信 |
| 0709-1 | 骑行 | 混合 0.45（低置信） | 特征区分度有限，诚实降级 |
| 0629 | — | 混合 0.45（低置信） | 区分度有限，诚实降级 |

> 启发式分类器对骑行的召回受限于真实骑行摆动手腕导致 HR 波动；若需高可信判定，必须融合 GPS/踏频/加速度计等外部传感器。

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
5. **HTML 图表不显示（V2.4.0 已内联，通常不再发生）**：报告已内联 Chart.js，离线/沙箱预览应直接出图。若仍空白，先排查是否旧报告（含 `cdn.jsdelivr` 外链）；新报告应是 0 个 CDN 引用、含内联 `<script>window.Chart=...</script>`。空白多为 JS 语法错误导致整段中断——用 `node -e "new Function(html.match(/<script>([\s\S]*?)<\/script>/g)...) "` 做语法校验，确认运动场景应有 7 个 `new Chart` 实例、睡眠 6 个。
6. HTML 报告心内科分析为空：检查 JSON 是否含完整 `hrv_metrics` 字段
7. **运动负荷评估文案与实际区间不符（V2.3.1 已修复）**：评估应完全基于 `exercise_segments` 各区间实际占比动态归纳
8. **运动模式 100% 混合 / 无四模式概率（Plan A 已修复）**：旧口径 `gap_ratio` 用「首末 ch=2A37 报文」作墙钟，会把运动后静默/无 RR 收尾算进缺口，虚高触发信号门控。V2.4.0 改为「有效 RR 报文首→末跨度」作墙钟（剔除前后静默）。若仍见异常高的缺口率导致全员混合，先核对 `compute_features` 的 `real_dur_sec` 是否取自 `valid_rows` 首末 `time` 的 `_parse_two_times` 结果。
9. **运动模式概率偏低/误判骑行为混合**：属启发式分类器固有局限（各模式 HR 区间重叠大）；低置信属诚实降级，最终判定须融合 GPS/踏频/海拔/加速度计。
10. 百分比/数值显示精度溢出（V2.2.4 已修复）：数值显示统一 `:.2f` 格式化。

## 九、历史变更附录（精简）
- **V2.3.3**：报告图表与板块布局重组（HRV 柱状图上移、HRV 数值面板下移）。
- **V2.3.2**：异常事件由表格改散点图（anomalyChart），图表数 5→6。
- **V2.3.1**：运动负荷评估动态化（基于区间实际占比，去除"有氧+高强度为主"硬写）。
- **V2.3.0**：场景自动识别 + 睡眠/运动双模式分析引擎。
- **V2.2.x**：运动负荷百分比精度、报文分类图 CSS 修复、build_chart_js 三处 JS 语法修复、generate_report.py 引入 Chart.js 图表与心内科分析引擎。
