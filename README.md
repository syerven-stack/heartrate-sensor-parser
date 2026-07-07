# heartrate-sensor-parser
专用XOSS心率设备BLE调试日志离线解析工具。自动识别2/4/6/8字节（及扩展长度）全部心率报文，提取设备固件/SN/电量等参数，批量计算真实瞬时心率、RR间期、全量HRV时域指标（SDNN/RMSSD/pNN50/pNN20/SDSD/SDARR/CVRR/HRV三角指数/Tin），自动识别场景（睡眠/运动）并据此动态适配分析策略和报告内容，动态划分心率区间（静息/热身/有氧/高强度/极限），检测心律异常（RR间期突变/疑似早搏），输出标准化Excel三表+CSV+JSON+分段运动心律波动分析报告+HTML可视化报告（含Chart.js图表和双模式心内科综合分析）。

# 安装heartrate-sensor-parser技能：

## 在Codex中添加此skill

- 方法1：下载此Skill，将heartrate-sensor-parser拷贝到～.codex/skills/

- 方法2：对话框中，输入帮我安装这个https://github.com/syerven-stack/heartrate-sensor-parser.git技能

![image](https://github.com/syerven-stack/heartrate-sensor-parser/blob/main/images/codex-1.png)

- 使用方法: 对话框中，输入提示词：使用heartrate-sensor-parser技能，分析heart_rate_0701.txt日志，生成HTML可视化报告

![image](https://github.com/syerven-stack/heartrate-sensor-parser/blob/main/images/codex-2.png)

![image](https://github.com/syerven-stack/heartrate-sensor-parser/blob/main/images/codex-3.png)


## 在claude code中添加此skill：

- 方法1：下载此Skill，将heartrate-sensor-parser拷贝到～.claude/skills/

- 方法2：对话框中，输入帮我安装这个https://github.com/syerven-stack/heartrate-sensor-parser.git技能

![image](https://github.com/syerven-stack/heartrate-sensor-parser/blob/main/images/claude-1.png)

![image](https://github.com/syerven-stack/heartrate-sensor-parser/blob/main/images/claude-2.png)

- 使用方法: 对话框中，输入提示词：使用heartrate-sensor-parser技能，分析heart_rate_0701.txt日志，生成HTML可视化报告

![image](https://github.com/syerven-stack/heartrate-sensor-parser/blob/main/images/claude-3.png)

## 在WorkBuddy中添加此skill：

- 点击“专家-技能-连接器”，选择"技能"页签，点击“+添加技能”，选择“上传技能”

![image](https://github.com/syerven-stack/heartrate-sensor-parser/blob/main/images/workbuddy-1.png)

- 拖拽或点击上传，下载的heartrate-sensor-parser.zip文件

![image](https://github.com/syerven-stack/heartrate-sensor-parser/blob/main/images/workbuddy-2.png)

- 等待安全检测，预计需要1分钟左右，如果不想等待，请勾选“非高风险自动安装”或点击“跳过检测，直接安装”

![image](https://github.com/syerven-stack/heartrate-sensor-parser/blob/main/images/workbuddy-3.png)

- 安装成功后，在“我安装的”技能中能查看此技能，可以管理开启或关闭，也可以卸载

![image](https://github.com/syerven-stack/heartrate-sensor-parser/blob/main/images/workbuddy-4.png)

- 使用heartrate-sensor-parser技能，分析xxx.txt日志，生成HTML可视化报告

![image](https://github.com/syerven-stack/heartrate-sensor-parser/blob/main/images/workbuddy-5.png)


# 示例txt蓝牙日志文件

- /demo/heart_rate_0701.txt

# 示例提示词：

- 使用heartrate-sensor-parser技能，分析heart_rate_0701.txt日志，生成HTML可视化报告

# 示例心率报告图：

![image](https://github.com/syerven-stack/heartrate-sensor-parser/blob/main/images/xoss-1.png)
![image](https://github.com/syerven-stack/heartrate-sensor-parser/blob/main/images/xoss-2.png)
![image](https://github.com/syerven-stack/heartrate-sensor-parser/blob/main/images/xoss-3.png)
![image](https://github.com/syerven-stack/heartrate-sensor-parser/blob/main/images/xoss-4.png)
![image](https://github.com/syerven-stack/heartrate-sensor-parser/blob/main/images/xoss-5.png)
![image](https://github.com/syerven-stack/heartrate-sensor-parser/blob/main/images/xoss-6.png)
![image](https://github.com/syerven-stack/heartrate-sensor-parser/blob/main/images/xoss-7.png)
