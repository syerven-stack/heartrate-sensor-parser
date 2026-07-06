---
skill_name: heartrate-sensor-parser
version: V2.2.4 纯Python标准库版 + HTML报告生成 + 心内科分析
device_support: XOSS X2P/X2PRO 蓝牙心率胸带
protocol: BLE GATT 0x180D / 0x2A37
function: XOSS心率日志解析、RR/心率换算、全量HRV计算、运动分段分析、HTML可视化报告、心内科综合分析
output: Excel/CSV/JSON 三套数据表 + 分段运动心律波动分析报告 + HTML可视化报告(含Chart.js图表+心内科分析)
agent_created: true
---
# heartrate-sensor-parser 技能使用手册 V2.2.4

## 一、技能概述
专用XOSS心率设备BLE调试日志离线解析工具。自动识别2/4/6/8字节（及扩展长度）全部心率报文，提取设备固件/SN/电量等参数，批量计算真实瞬时心率、RR间期、全量HRV时域指标（SDNN/RMSSD/pNN50/pNN20/SDSD/SDARR/CVRR/HRV三角指数/Tin），动态划分运动负荷区间（静息/热身/有氧/高强度/极限），检测心律异常（RR间期突变/疑似早搏），输出标准化Excel三表+CSV+JSON+分段运动心律波动分析报告+HTML可视化报告（含Chart.js图表和心内科综合分析）。

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

> **排查方法**：用 Node.js `new Function(js)` 对 `<script>` 标签内的 JS 做语法校验，可快速定位语法错误位置。生成的正确报告应包含 5 个 `new Chart` 实例（运动负荷环形图、报文分类柱状图、心率趋势折线图、HRV-ms 柱状图、HRV-% 柱状图）。

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
   - HRV指标数值面板
   - 6张Chart.js图表（负荷分布、报文分类、心率趋势、HRV-ms、HRV-%）
   - 心律异常事件明细表
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
7. **HTML报告全部图表空白（V2.2.2已修复）**：`build_chart_js` 函数曾存在三处JS语法错误（趋势图属性重复+缺options关键字、HRV-ms图缺new Chart声明），任一错误都会导致JS引擎中断、后续所有Chart.js代码不执行。如遇到类似问题，用 `node -e "new Function(require('fs').readFileSync('html','utf-8').match(/<script>([\s\S]*?)<\/script>/)[1])"` 做语法校验，确认生成5个 `new Chart` 实例
8. **报文分类图 X 轴不显示（V2.2.3已修复）**：canvas 被 CSS `height: 100% !important` 锁死后 Chart.js 无法正确计算尺寸，底部刻度被裁。排查时检查 canvas 的 CSS 是否有强制 height 声明，以及 `border: { display: false }` 是否误藏了轴线。修复后 `.chart-wrap-packet` 仅设 `width: 100% !important`，不强制 height。
9. **百分比/数值显示精度溢出（V2.2.4已修复）**：Python f-string 输出浮点数时默认不截断精度，如 `68.42999999999999%`。涉及数值显示时应主动使用 `:.2f` 等格式化说明符。
