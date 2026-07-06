# heartrate-sensor-parser
XOSS X2Pro 心率传感器蓝牙通信日志解析和心率报告的可视化生成。报告中含HRV心率变异性指标（全量时域）数据及图表分析、运动负荷分布、报文分类统计、心率采样趋势以及根据分析数据综合分析得出的结论和建议。

# 使用步骤：
## 在AI Agent 中添加此skill
1.codex安装：下载此Skill，将heartrate-sensor-parser拷贝到～.code/skills/, 之后在codex对话框中，使用这样的提示词：使用heartrate-sensor-parser技能，分析heart_rate_0703.txt日志，生成HTML可视化报告，heart_rate_0703.txt日志就是XOSS X2Pro 心率传感器蓝牙日志文件，需要上传。

2.claude code安装：下载此Skill，将heartrate-sensor-parser拷贝到～.claude/skills/，之后在claude code对话框中，使用这样的提示词：使用heartrate-sensor-parser技能，分析heart_rate_0703.txt日志，生成HTML可视化报告，heart_rate_0703.txt日志就是XOSS X2Pro 心率传感器蓝牙日志文件，需要上传。

# 提示词：
## 使用heartrate-sensor-parser技能，分析xxx.txt日志，生成HTML可视化报告
