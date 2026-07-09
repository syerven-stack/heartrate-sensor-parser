---
name: heartrate-sensor-parser
description: XOSS心率设备BLE调试日志离线解析工具。自动识别报文、计算心率/RR间期、全量HRV时域指标，支持场景自动识别（睡眠/运动）双模式分析，输出Excel/CSV/JSON+HTML可视化报告。
metadata:
  short-description: XOSS心率BLE日志解析、HRV计算、HTML报告生成
version: V2.3.3 纯Python标准库版 + 场景自动识别 + 睡眠/运动双模式HRV分析 + HTML报告生成 + 运动负荷评估动态化 + 异常事件散点图 + 报告图表布局重组
device_support: XOSS X2P/X2PRO 蓝牙心率胸带
protocol: BLE GATT 0x180D / 0x2A37
function: XOSS心率日志解析、RR/心率换算、全量HRV计算、场景自动识别（睡眠/运动）、双模式HRV分析、HTML可视化报告、心内科综合分析
output: Excel/CSV/JSON 三套数据表 + 分段运动心律波动分析报告 + HTML可视化报告(含Chart.js图表+心内科分析)
agent_created: true
---
# heartrate-sensor-parser 技能使用手册 V2.3.3

## 一、技能概述
专用XOSS心率设备BLE调试日志离线解析工具。自动识别2/4/6/8字节（及扩展长度）全部心率报文，提取设备固件/SN/电量等参数，批量计算真实瞬时心率、RR间期、全量HRV时域指标（SDNN/RMSSD/pNN50/pNN20/SDSD/SDARR/CVRR/HRV三角指数/Tin），**自动识别场景（睡眠/运动）并据此动态适配分析策略和报告内容**，动态划分心率区间（静息/热身/有氧/高强度/极限），检测心律异常（RR间期突变/疑似早搏），输出标准化Excel三表+CSV+JSON+分段运动心律波动分析报告+HTML可视化报告（含Chart.js图表和双模式心内科综合分析）。

### V2.3.3 优化内容（对比V2.3.2）
报告 HTML 图表与板块布局重组，使数据呈现更符合阅读逻辑：

- **HRV 两张柱状图上移**：`HTML_TEMPLATE` 中 `{hrv_charts}` 与 `{trend_chart}` 占位符顺序对调，HRV 变异性指标 (ms) / (%) 两张柱状图现位于「心率趋势（采样数据）」折线图**上方**。图表完整顺序变为：运动负荷分布环形图 → 报文分类柱状图 → HRV(ms) → HRV(%) → 心率趋势。
- **HRV 数值面板下移**：`{hrv_metrics}` 由「运动负荷评估」摘要下方，移至「运动负荷分布图表行（charts_row1）」正下方，数值面板紧邻上方图表，便于图表与数值对照。
- **报告板块最终顺序**：运动负荷评估 → 图表行1（负荷分布+报文分类）→ HRV 数值面板 → HRV(ms)/HRV(%) → 心率趋势 → 异常事件散点图 → 心内科综合分析。
- 本次为纯布局调整，不改变任何计算逻辑与数值口径；图表总数仍为 6 张。

### V2.3.2 优化内容（对比V2.3.1）
将 HTML 报告中的「心律异常事件」**由表格改为散点图**，更直观地呈现异常的时段分布与剧烈程度：

- **图表形态**：`build_anomaly_table()` 重构为 `build_anomaly_chart()`，输出一张 Chart.js `scatter` 散点图（canvas `anomalyChart`）。
- **数据映射**（直接来自 JSON 的 `anomalies` 数组）：
  - 横轴 `x`：相对时间（自首次异常起，单位分钟），由 `time` 字段 `%d/%m/%y %H:%M:%S:%f` 解析后作差得到；
  - 纵轴 `y`：心率跳变幅度 `|hr_after - hr_before|`（bpm）；
  - 颜色/图例：按 `type` 分色——`RR间期突变`（橙 `#f59e0b`）与 `疑似早搏`（红 `#ef4444`）；
  - 悬停 Tooltip：显示类型、时间、心率变化 `hr_before→hr_after` 与 `detail`（如 `RR变化41.1% (680->400ms)`）。
- **标题摘要**：卡片标题展示总起数、图表展示的明细条数与各类型计数（共 N 起，下图展示明细 M 条：RR间期突变 A 次 / 疑似早搏 B 次；N 来自 `anomaly_count` 总检测数，M 为 JSON 中 `anomalies` 明细数组长度，二者因明细数组有上限可能不一致）。
- 报告图表总数由 5 张增至 **6 张**（运动负荷环形图、报文分类柱状图、心率趋势折线图、HRV-ms 柱状图、HRV-% 柱状图、异常事件散点图）。

> **排查要点**：散点图坐标由 Python 端解析 `anomalies` 预计算为 `{x,y,...}` 对象数组后 `json.dumps` 注入；时间格式固定为 `DD/MM/YY HH:MM:SS:mmm`，若日志时间格式变化需在 `build_chart_js` 的 `strptime` 处同步调整，否则该异常点会被跳过（不影响其他图表）。

### V2.3.1 修复内容（对比V2.3.0）
修复 `generate_report.py` 中**运动负荷评估与实际区间分布不一致**的问题：

1. **运动负荷评估硬编码误判**：`build_exercise_summary()` 原逻辑为 `if high_pct > 30` 否则直接写死输出 `本次运动以有氧+高强度为主（合计 31.3%）`。该写法忽略了真正占比最高的区间（如热身 62%），且把实际仅 0.36% 的高强度区间错误纳入"为主"描述，任何非高强度日志都会得到"有氧+高强度"的错误结论。
   - **修复**：改为完全依据 `exercise_segments` 各区间占比**动态归纳**：按占比降序累加直至覆盖约 80% 总分布（至少取前两大有效区间）作为"主要区间"；并依据 `高强度及以上合计(high+extreme) ≥ 5%` 动态给出强度定性（高强度/剧烈 ↔ 以有氧为主的中等 ↔ 以热身为主的低-中等 ↔ 以静息为主的低）与高强度描述。
   - 示例（本次 0708 日志）：`本次运动以热身、有氧区间为主（合计 93.23%），整体为以有氧为主的中等运动负荷，未出现有临床意义的高强度区间（高强度及以上合计仅 0.36%）`。

2. **结论建议硬编码**：`generate_conclusions()` 的建议首条原写死 `有氧+高强度混合训练对心肺功能提升效果显著`，与修正后的评估口径矛盾。
   - **修复**：改为按真实高强度占比动态表述（≥5% 保留原 HIIT 表述 / 有氧或热身≥30% 改为有氧耐力训练表述 / 整体偏低 改为建议逐步加量）。

> **排查要点**：报告中凡涉及"运动强度定性"的结论，必须来自 `exercise_segments` 各区间的**实际占比**动态计算，不得用单一阈值分支后写死文本。主导区间应取占比降序累加覆盖 ~80% 分布的集合，而非只看某一个区间，避免漏报占比更高的区间。

### V2.3.0 新增内容（对比V2.2.4）
**核心升级：场景自动识别 + 睡眠/运动双模式分析引擎**

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

### V2.2.4 优化内容（对比V2.2.3）
两项 HTML 报告显示优化：

1. **运动负荷评估百分比浮点精度**：`build_exercise_summary` 和 `analyze_exercise_load` 函数中，运动负荷占比百分比直接用 f-string `{val}%` 输出，浮点数会出现 `68.42999999999999%` 这样的精度溢出。修复：所有占比百分比统一改为 `{val:.2f}%` 格式化，保留2位小数。

2. **HRV 指标区域添加淡蓝色圆角背景**：`.hrv-item` 原先无背景，指标之间视觉区分度低。优化：为每个指标项添加 `background: #d0eaf2; border-radius: 10px; padding: 12px 4px;`，`.hrv-grid` 的 gap 从 12px 调整为 10px 并增加 4px 内边距，让背景块之间留出呼吸空间。

> **排查要点**：Python f-string 输出浮点数时默认不截断精度，涉及百分比/数值显示时应主动使用 `:.2f` 或 `:.3f` 格式化说明符，避免出现 `68.42999999999999%` 这样的精度溢出。

### V2.2.3 修复内容（对比V2.2.2）
修复报文分类统计图表的布局问题，共三个改动点：

1. **报文分类图 CSS 全局污染**：此前修改报文分类图布局时复用了全局 `.chart-wrap` 类，导致运动负荷环形图等其他图表布局被连带影响。修复：新增独立 `.chart-wrap-packet` 类仅作用于报文分类图，`.chart-wrap` 还原为原始样式，其他图表不受影响。
2. **报文分类图未撑满显示区域**：报文分类图无图例，固定 `height: 280px` 导致底部留白。修复：卡片改为 `display:flex; flex-direction:column;`，图表容器用 `flex:1; min-height:0;` 吃掉标题以下全部剩余空间，与旁边的运动负荷图卡片等高对齐。
3. **报文分类图 X 轴不显示**：canvas 强制 `height: 100% !important` 覆盖了 Chart.js 内部尺寸计算，导致底部 X 轴刻度被裁掉；同时 x 轴 `border: { display: false }` 藏了轴线。修复：移除 canvas 的 `height` 强制声明（仅保留 `width`），让 Chart.js 自行管理 canvas 高度；x 轴 `border` 改回 `display: true`，补上 `ticks` 字体配置。

> **排查要点**：Chart.js 横向柱状图（`indexAxis: 'y'`）出现 X 轴消失时，优先检查：(1) canvas 是否被 CSS `height: 100% !important` 锁死；(2) `maintainAspectRatio: false` 下父容器是否给了有效高度（flex 子项需 `min-height: 0`）；(3) `border: { display: false }` 是否误藏了轴线。

### V2.2.2 修复内容（对比V2.2.1）
修复 `generate_report.py` 的 `build_chart_js` 函数中三处 JavaScript 语法错误，这些错误会导致 HTML 报告中全部6张图表无法渲染：

1. **心率趋势折线图 — dataset 属性重复**：`borderColor`/`backgroundColor`/`fill`/`tension`/`pointRadius`/`borderWidth` 各出现两次，且第一组末尾缺少逗号，导致 JS 解析中断。修复：删除重复属性，保留单组完整属性。
2. **心率趋势折线图 — 缺少 `options:` 关键字**：`data` 对象的 `}}]` 闭合后直接写了 `responsive: true` 等配置项，缺少 `options: {{` 包裹。修复：补全 `options: {{ ... }}` 结构。
3. **HRV ms 柱状图 — 缺少 `new Chart` 声明**：整段代码缺少 `new Chart(document.getElementById('hrvTimeChart'), {{` 和 `type: 'bar'` 声明，只有孤立的 `data:` 和 `options:` 对象。修复：补全完整的 `new Chart(...)` 调用结构。

> **排查方法**：用 Node.js `new Function(js)` 对 `<script>` 标签内的 JS 做语法校验，可快速定位语法错误位置。生成的正确报告应包含 6 个 `new Chart` 实例（运动负荷环形图、报文分类柱状图、心率趋势折线图、HRV-ms 柱状图、HRV-% 柱状图、异常事件散点图）。

### V2.2 升级内容（对比V2.1）
1. 新增 `generate_report.py` — HTML可视化报告生成器，内置Chart.js图表和心内科综合分析引擎
2. 心内科综合分析引擎 `CardioAnalyzer`：
   - 信号质量评估：检测传感器伪差特征（高SDNN低三角指数分离、精确50%/100%倍半关系、运动中RMSSD异常偏高等）
   - HRV指标逐项解读：10项指标逐一从心内科角度解析，结合运动场景给出正常/异常判断
   - 运动负荷与自主神经调节分析：识别训练模式（HIIT/稳态有氧），分析交感-迷走神经动态响应
   - 心律异常事件分析：区分生理性波动、运动伪差与真实异常，评估临床风险
   - 综合结论与建议：心脏自主神经功能评价、运动耐受性评价、心律安全性评价、可操作建议
3. HTML报告包含6张图表：
   - 运动负荷分布（环形图）
   - 报文分类统计（横向柱状图）
   - 心率趋势（折线图，自动采样）
   - HRV变异性指标-ms（柱状图）：SDNN/RMSSD/SDSD/SDARR/Tin/RR极差
   - HRV变异性指标-%（柱状图）：pNN50/pNN20/CVRR/HRV三角指数
4. 全流程一键化：解析 + 报告生成可在两条命令内完成

### 前置条件
- Python 3.8+（仅使用标准库，无需安装第三方依赖）
- 无需pip install任何包
- HTML报告需要网络加载Chart.js CDN（浏览器端）

## 二、调用方式

### 1. 完整流程（推荐）
```bash
# Step 1: 解析日志
python scripts/parse_heart_rate_log.py --log heart_rate_0701.txt --csv 1 --json 1

# Step 2: 生成HTML报告
python scripts/generate_report.py \
  --json output/分析结果.json \
  --csv output/心跳明细.csv \
  --out output/heart_rate_report.html
```

### 2. 极简命令（仅解析）
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

### 5. 仅生成报告（已有JSON+CSV）
```bash
python scripts/generate_report.py \
  --json output/分析结果.json \
  --csv output/心跳明细.csv
```

参数说明：
- `--log`：必填，心率txt日志路径
- `--out`：输出目录，默认./output
- `--csv`：1输出CSV，0仅Excel
- `--json`：1输出结构化JSON
- `--batch`：批量日志文件夹路径
- `generate_report.py --json`：分析结果JSON路径
- `generate_report.py --csv`：心跳明细CSV路径
- `generate_report.py --out`：输出HTML路径（默认与JSON同目录）

## 三、输出产物说明

### parse_heart_rate_log.py 产物
1. 心率解析汇总.xlsx：3个Sheet
   - 报文总表：每条BLE心率通知原始数据、报文类型、Flags、HR、RR数量
   - RR明细表：单心跳逐行，RR毫秒、瞬时心率、负荷区间标签
   - HRV汇总表：设备信息+报文统计+报文分类+全量HRV指标+运动负荷分段
2. 报文数据.csv / 心跳明细.csv：CSV格式原始数据
3. 分析结果.json：结构化JSON结果
4. 分段运动心律波动分析报告.txt：九段式分析报告

### generate_report.py 产物
5. heart_rate_report.html：完整HTML可视化报告，包含：
   - 设备信息展示
   - 数据概览卡片（报文数、心跳数、平均心率、异常事件数）
   - 运动负荷评估摘要
   - 运动负荷分布环形图 + 报文分类柱状图（图表行1）
   - HRV指标数值面板（位于图表行1下方，便于与图表对照）
   - HRV-ms / HRV-% 柱状图、心率趋势折线图（图表行2）
   - 心律异常事件散点图（横轴相对时间、纵轴|ΔHR|、按类型分色；与上方2张合计6张 Chart.js 图表）
   - **心内科综合分析**（五段式：总体评价→HRV解读→运动负荷分析→异常事件分析→结论与建议）

## 四、支持报文规格
- 2字节：Flags+HR（无RR）
- 4字节：Flags+HR+1组RR
- 6字节：Flags+HR+2组RR
- 8字节：Flags+HR+3组RR
- 扩展长度（>8字节）：自动识别多组RR

换算公式：
- RR_raw(小端16位) → RR_ms = RR_raw / 1024 * 1000
- 瞬时心率 = 60 / (RR_raw / 1024)

## 五、HRV指标说明

| 指标 | 说明 |
|------|------|
| 摘要 | 交感神经只会持续抬高心率，无交替起伏；迷走神经会制造这种快速、小幅的心跳起伏，产生相邻RR长短差 |
| SDNN | 全部RR间期的标准差，反映交感+迷走神经总体心率变异性 |
| RMSSD | 相邻RR差值平方均值的根号，反映迷走神经短时调节功能，是评估副交感最敏感指标 |
| SDSD | 相邻RR差值的标准差，反应迷走神经活性 |
| SDARR | 相邻RR差值绝对值的标准差，反应逐跳心跳快慢变化幅度的稳定程度 |
| pNN50 | 相邻RR差值>50ms的百分比，统计大幅逐跳心跳起伏发生频率 |
| pNN20 | 相邻RR差值>20ms的百分比，统计轻微逐跳心跳起伏发生频率 |
| CVRR | SDNN/平均RR×100%，变异系数，交感+迷走全套自主神经综合调节弹性 |
| HRV三角指数 | 总心跳数/最大频数bin的值，基于直方图频数分布，侧重数据聚集度（统计分布形态） |
| Tin | RR间期中位数，剔除早搏、异常心跳干扰后的真实基础窦性心跳间隔，用来客观判断静息心率快慢，比均值更稳定可靠 |

## 六、心内科分析引擎说明

`CardioAnalyzer` 类内置于 `generate_report.py`，对解析结果进行多维度临床分析：

### 信号质量评估
自动检测传感器伪差特征：
- 运动中RMSSD异常偏高（>60ms）→ 提示非生理性跳变
- 高SDNN低三角指数分离（SDNN>80且三角指数<12）→ 伪差典型特征
- pNN50与pNN20过于接近（比值>0.9）→ 非典型生理分布
- 精确50%/100% RR突变比例 → 传感器倍半伪差模式
- 输出可信度评级：高/中/低

### HRV指标解读
结合平均心率水平，对每项指标给出运动场景下的正常/异常判断：
- 运动中SDNN通常30-80ms，RMSSD通常<40ms
- pNN50运动中通常<3%
- CVRR运动中通常5-12%

### 异常事件分析
- 区分生理性RR波动（运动强度转换）与传感器伪差（精确倍半关系）
- 早搏临床风险评估（阈值>1%或>100次/小时）
- 异常事件时间集中性分析

## 七、常见问题排查
1. 无ch=2A37数据：日志不含XOSS心率BLE.RX记录
2. 心率数值异常：脚本自动过滤30~220bpm外噪声
3. 固件版本未提取：日志中需包含`fw=`或`firmware=`字段
4. xlsx打开乱码：确保使用Excel或WPS打开，非文本编辑器
5. HTML图表不显示：确保浏览器可访问 cdn.jsdelivr.net（Chart.js CDN）
6. HTML报告心内科分析为空：检查JSON文件是否包含完整的hrv_metrics字段
7. **运动负荷评估文案与实际区间不符（V2.3.1已修复）**：旧版 `build_exercise_summary` 在非高强度日志下会写死输出"本次运动以有氧+高强度为主"，忽略占比更高的热身区间且误报高强度。修复后评估完全基于 `exercise_segments` 各区间实际占比动态归纳主导区间与强度。若仍发现文案与图表分布矛盾，检查 `build_exercise_summary` 的降序累加逻辑（阈值 80%、至少前两大区间）是否被改动。
8. **HTML报告全部图表空白（V2.2.2已修复）**：`build_chart_js` 函数曾存在三处JS语法错误（趋势图属性重复+缺options关键字、HRV-ms图缺new Chart声明），任一错误都会导致JS引擎中断、后续所有Chart.js代码不执行。如遇到类似问题，用 `node -e "new Function(require('fs').readFileSync('html','utf-8').match(/<script>([\s\S]*?)<\/script>/)[1])"` 做语法校验，确认生成6个 `new Chart` 实例
9. **报文分类图 X 轴不显示（V2.2.3已修复）**：canvas 被 CSS `height: 100% !important` 锁死后 Chart.js 无法正确计算尺寸，底部刻度被裁。排查时检查 canvas 的 CSS 是否有强制 height 声明，以及 `border: { display: false }` 是否误藏了轴线。修复后 `.chart-wrap-packet` 仅设 `width: 100% !important`，不强制 height。
10. **百分比/数值显示精度溢出（V2.2.4已修复）**：Python f-string 输出浮点数时默认不截断精度，如 `68.42999999999999%`。涉及数值显示时应主动使用 `:.2f` 等格式化说明符。
