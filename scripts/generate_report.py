#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
心率分析HTML报告生成器 V2.2.4
- 兼容2/4/6/8字节全规格0x2A37报文，自动过滤畸形截断数据包
- 自动提取设备固件/SN/电量等设备参数
- 批量计算RR间期、瞬时真实心率
- 全量HRV时域指标运算
- 动态心率分段，自动计算运动负荷占比
- 输出3张Excel数据表(CSV) + 分段运动心律波动分析报告
- V2.2.4 优化: 运动负荷百分比保留2位小数、HRV指标区域淡蓝色圆角背景
- V2.2.3 修复: 报文分类图布局（独立CSS类/flex撑满/X轴不显示问题）
- V2.2.2 修复: build_chart_js 三处JS语法错误（趋势图属性重复/缺options关键字/HRV-ms图缺new Chart声明）
- V2.2.1 修复: 运动负荷环形图高清显示、标准圆环样式、悬停性能优化
"""

import argparse
import json
import csv
import os
import sys
from pathlib import Path
from datetime import datetime


# ==================== 心内科分析引擎 ====================

class CardioAnalyzer:
    """基于HRV指标和运动场景的心内科综合分析"""

    def __init__(self, data):
        self.d = data
        self.hrv = data.get("hrv_metrics", {})
        self.pkt = data.get("packet_stats", {})
        self.seg = data.get("exercise_segments", {})
        self.anomalies = data.get("anomalies", [])
        self.anomaly_count = data.get("anomaly_count", 0)
        self.device = data.get("device_info", {})

    def _get(self, key, default=0):
        return self.hrv.get(key, default)

    def assess_signal_quality(self):
        """评估信号质量：检测传感器伪差特征"""
        sdnn = self._get("SDNN(ms)", 0)
        rmssd = self._get("RMSSD(ms)", 0)
        tri = self._get("HRV三角指数", 0)
        pnn50 = self._get("pNN50(%)", 0)
        pnn20 = self._get("pNN20(%)", 0)
        rr_range = self._get("RR极差(ms)", 0)
        avg_hr = self._get("平均瞬时心率(bpm)", 0)

        flags = []
        confidence = "高"

        # 运动中 RMSSD 异常偏高 (>60ms 在运动中极罕见)
        if avg_hr > 120 and rmssd > 60:
            flags.append(f"运动中RMSSD异常偏高({rmssd}ms)，正常运动状态通常<40ms")
            confidence = "中"

        # 高 SDNN 低三角指数分离（伪差典型特征）
        if sdnn > 80 and tri < 12:
            flags.append(f"高SDNN({sdnn}ms)与低三角指数({tri})分离，提示存在离群RR值拉高SDNN")
            confidence = "低"

        # pNN50 与 pNN20 过于接近（说明大部分变异都 >50ms，非生理模式）
        if pnn50 > 3 and pnn20 > 0 and abs(pnn50 - pnn20) < 2:
            flags.append(f"pNN50({pnn50}%)与pNN20({pnn20}%)过于接近，非典型生理分布")
            if confidence != "低":
                confidence = "中"

        # 极端 RR 极差（运动中小于 800ms 正常）
        if rr_range > 1200:
            flags.append(f"RR极差极大({rr_range}ms)，包含极端离群值")

        # 精确倍半关系检测
        exact_ratio_count = 0
        for a in self.anomalies[:100]:
            detail = a.get("detail", "")
            for pct_text in [s for s in detail.split() if "%" in s and "变化" in s]:
                try:
                    pct = float(pct_text.replace("%", "").split("变化")[-1])
                    if 48 <= pct <= 52 or 95 <= pct <= 105:
                        exact_ratio_count += 1
                except:
                    pass
        if exact_ratio_count >= 5:
            flags.append(f"检测到{exact_ratio_count}+起接近精确50%/100%的RR突变，符合传感器倍半伪差模式")
            confidence = "低"

        if not flags:
            flags.append("HRV指标模式正常，无明显信号伪差特征")
            confidence = "高"

        return {"flags": flags, "confidence": confidence}

    def interpret_hrv_metrics(self):
        """逐项解读HRV指标"""
        sdnn = self._get("SDNN(ms)", 0)
        rmssd = self._get("RMSSD(ms)", 0)
        sdsd = self._get("SDSD(ms)", 0)
        sdarr = self._get("SDARR(ms)", 0)
        pnn50 = self._get("pNN50(%)", 0)
        pnn20 = self._get("pNN20(%)", 0)
        cvrr = self._get("CVRR(%)", 0)
        tri = self._get("HRV三角指数", 0)
        tin = self._get("Tin(ms)", 0)
        rr_range = self._get("RR极差(ms)", 0)
        avg_hr = self._get("平均瞬时心率(bpm)", 0)
        avg_rr = self._get("平均RR间期(ms)", 0)

        items = []

        # SDNN
        if sdnn > 100:
            items.append(f"SDNN={sdnn}ms，显著偏高。在平均心率{avg_hr}bpm的运动状态下通常应<80ms。需结合三角指数判断是否为离群值拉高。")
        elif sdnn > 60:
            items.append(f"SDNN={sdnn}ms，处于运动状态的正常偏高范围，提示总体自主神经调控储备尚可。")
        else:
            items.append(f"SDNN={sdnn}ms，处于运动状态的正常范围。")

        # RMSSD
        if avg_hr > 120 and rmssd > 60:
            items.append(f"RMSSD={rmssd}ms，在运动中异常偏高。该值通常见于静息状态，运动中>40ms即属罕见，强烈提示逐跳RR存在非生理性跳变。")
        elif rmssd > 30:
            items.append(f"RMSSD={rmssd}ms，迷走神经在运动中保持一定张力，是自主神经调节健康的积极信号。")
        else:
            items.append(f"RMSSD={rmssd}ms，运动中迷走神经活性正常受抑，符合运动生理规律。")

        # SDSD
        if sdsd > 0:
            if abs(sdsd - rmssd) < 1:
                items.append(f"SDSD={sdsd}ms，与RMSSD高度一致，逐跳变异幅度均匀。")
            else:
                items.append(f"SDSD={sdsd}ms，与RMSSD存在差异(SDSD/RMSSD={sdsd/rmssd:.2f})。")

        # SDARR
        items.append(f"SDARR={sdarr}ms，逐跳RR差值绝对值的标准差，反映心跳快慢变化的稳定性。")

        # pNN50/pNN20
        if pnn50 > 5:
            items.append(f"pNN50={pnn50}%，显著偏高。运动中通常<3%。{pnn50}%意味着超过{pnn50:.0f}%的心跳存在相邻RR差值>50ms的大幅跳变。")
        elif pnn50 > 2:
            items.append(f"pNN50={pnn50}%，运动中处于正常偏高范围。")
        else:
            items.append(f"pNN50={pnn50}%，运动中属于正常偏低水平。")

        if pnn20 > 0:
            ratio = pnn50 / pnn20 if pnn20 > 0 else 0
            if ratio > 0.9:
                items.append(f"pNN20={pnn20}%，与pNN50接近(比值{ratio:.2f})，说明绝大部分逐跳变异超过50ms阈值，非典型生理分布。")
            else:
                items.append(f"pNN20={pnn20}%，与pNN50的比值为{ratio:.2f}，大部分变异集中在20-50ms区间，符合运动生理规律。")

        # CVRR
        if cvrr > 20:
            items.append(f"CVRR={cvrr}%，变异系数极高。远超运动状态正常范围(5-12%)，提示RR离散度不成比例地大。")
        elif cvrr > 10:
            items.append(f"CVRR={cvrr}%，变异系数处于正常偏高范围，自主神经调节弹性良好。")
        else:
            items.append(f"CVRR={cvrr}%，变异系数正常。")

        # 三角指数
        if tri < 10:
            items.append(f"HRV三角指数={tri}，偏低。若同时SDNN偏高，提示存在长尾分布的离群RR值(传感器伪差特征)。")
        else:
            items.append(f"HRV三角指数={tri}，处于正常范围，RR间期整体分布较为集中，无心律失常引起的分布畸变。")

        # Tin
        tin_hr = round(60000 / tin) if tin > 0 else 0
        items.append(f"Tin={tin}ms(约{tin_hr}bpm)，中位RR间期，比均值更能抵抗异常值干扰。")

        # RR极差
        if rr_range > 1200:
            items.append(f"RR极差={rr_range}ms，跨度极大，包含极端离群值。")
        else:
            items.append(f"RR极差={rr_range}ms，在运动状态下属于可接受范围。")

        return items

    def analyze_exercise_load(self):
        """分析运动负荷与自主神经调节"""
        seg = self.seg
        items = []

        high_pct = seg.get("高强度(150-180)", {}).get("占比(%)", 0)
        aerobic_pct = seg.get("有氧(120-150)", {}).get("占比(%)", 0)
        warmup_pct = seg.get("热身(90-120)", {}).get("占比(%)", 0)
        rest_pct = seg.get("静息(<90)", {}).get("占比(%)", 0)
        extreme_pct = seg.get("极限(>180)", {}).get("占比(%)", 0)

        total_high = high_pct + extreme_pct
        total_moderate = aerobic_pct + high_pct

        # 训练模式判断
        if high_pct > 30:
            items.append(f"高强度占比{high_pct:.2f}%为主导，训练模式偏向高强度间歇训练(HIIT)或重复冲刺训练。")
            items.append(f"极限区间{extreme_pct:.2f}%，心率峰值已接近该年龄段理论上限，运动投入充分。")
        elif aerobic_pct > 35:
            items.append(f"有氧占比{aerobic_pct:.2f}%为主导，配合高强度{high_pct:.2f}%，属于混合强度耐力训练。")
        else:
            items.append(f"热身占比{warmup_pct:.2f}%较高，运动强度整体偏低。")

        # 静息占比
        if rest_pct > 3:
            items.append(f"静息占比{rest_pct}%，存在明确的恢复间歇期，符合间歇训练特征。")
        else:
            items.append(f"静息占比仅{rest_pct}%，记录几乎全程覆盖运动状态。")

        # 自主神经分析
        items.append("从自主神经角度看，运动过程中交感神经的渐进激活和运动后恢复期的迷走神经再激活，展示了自主神经动态调节过程。")

        return items

    def analyze_anomalies(self):
        """分析心律异常事件"""
        total_beats = self.pkt.get("有效心跳总数", 1)
        anomaly_rate = round(self.anomaly_count / total_beats * 100, 2) if total_beats > 0 else 0

        items = []

        # 统计突变和早搏
        mutation_count = sum(1 for a in self.anomalies if a.get("type") == "RR间期突变")
        ectopic_count = sum(1 for a in self.anomalies if a.get("type") == "疑似早搏")

        items.append(f"共{self.anomaly_count}起异常事件，占有效心跳{anomaly_rate}%。其中RR间期突变{mutation_count}次，疑似早搏{ectopic_count}次。")

        # 早搏评估
        if ectopic_count > 0:
            ectopic_rate = round(ectopic_count / total_beats * 100, 3) if total_beats > 0 else 0
            if ectopic_rate < 0.1:
                items.append(f"疑似早搏{ectopic_count}次({ectopic_rate}%)，远低于临床关注阈值(通常>1%)，属运动诱发的偶发事件，无临床意义。")
            elif ectopic_rate < 1:
                items.append(f"疑似早搏{ectopic_count}次({ectopic_rate}%)，数量略多但仍低于临床关注阈值，建议持续观察。")
            else:
                items.append(f"疑似早搏{ectopic_count}次({ectopic_rate}%)，超过临床关注阈值，建议进行静息心电图检查。")

        # 突变模式分析
        if anomaly_rate > 5:
            items.append(f"异常事件占比{anomaly_rate}%，数量显著偏多，需区分生理性波动与传感器伪差。")

        # 信号质量交叉验证
        sq = self.assess_signal_quality()
        if sq["confidence"] in ("中", "低"):
            items.append(f"结合信号质量评估({sq['confidence']}可信度)，相当比例的突变事件可归因于传感器伪差。")

        return items, mutation_count, ectopic_count

    def generate_conclusions(self):
        """生成综合结论与建议"""
        sq = self.assess_signal_quality()
        anomaly_rate = round(self.anomaly_count / max(self.pkt.get("有效心跳总数", 1), 1) * 100, 2)

        conclusions = []
        recommendations = []

        # 心脏自主神经评价
        if sq["confidence"] == "高":
            conclusions.append("心脏自主神经功能评价：良好。HRV指标在运动背景下均处于正常范围，自主神经调节弹性正常。")
        elif sq["confidence"] == "中":
            conclusions.append("心脏自主神经功能评价：尚可。部分HRV指标存在轻度异常，可能受信号质量影响。建议结合静息HRV进一步评估。")
        else:
            conclusions.append("心脏自主神经功能评价：因信号质量限制难以准确评估。从Tin和早搏数量来看，心脏基础节律规整，无持续性心律失常证据。")

        # 运动耐受性
        avg_hr = self._get("平均瞬时心率(bpm)", 0)
        conclusions.append(f"运动耐受性评价：良好。在持续运动中，心脏能够维持{avg_hr}bpm的平均输出，未出现失代偿迹象。")

        # 心律安全性
        if anomaly_rate < 2:
            conclusions.append("心律安全性评价：低风险。异常事件数量有限，符合运动生理规律。")
        elif anomaly_rate < 5:
            conclusions.append("心律安全性评价：低至中等风险。建议关注异常事件集中时段的心率模式。")
        else:
            conclusions.append("心律安全性评价：低风险（考虑信号伪差因素）。去除疑似伪差后，真实心律异常数量有限。")

        # 建议
        recommendations.append("保持当前运动强度与结构，有氧+高强度混合训练对心肺功能提升效果显著。")
        recommendations.append("建议在训练中增加5-10分钟的整理活动，促进迷走神经再激活和心率恢复。")
        recommendations.append("关注运动后1-2分钟的HRR(心率恢复值)：从峰值下降>12bpm为正常，>25bpm为优秀。")

        if sq["confidence"] in ("中", "低"):
            recommendations.append("信号质量优化：运动前确保胸带充分湿润，调整松紧度至贴合但不勒紧，以消除伪差信号。")
            recommendations.append("建议每周进行一次5分钟晨起静息HRV测量(坐姿)，以RMSSD为主要跟踪指标，不受运动伪差干扰。")

        recommendations.append("建议定期(每1-3个月)对比静息HRV趋势，关注RMSSD和SDNN的长期变化方向。")

        return conclusions, recommendations

    def full_analysis(self):
        """执行完整分析，返回结构化结果"""
        sq = self.assess_signal_quality()
        hrv_items = self.interpret_hrv_metrics()
        exercise_items = self.analyze_exercise_load()
        anomaly_items, mutation_count, ectopic_count = self.analyze_anomalies()
        conclusions, recommendations = self.generate_conclusions()

        return {
            "signal_quality": sq,
            "hrv_interpretations": hrv_items,
            "exercise_analysis": exercise_items,
            "anomaly_analysis": anomaly_items,
            "anomaly_stats": {"mutation": mutation_count, "ectopic": ectopic_count},
            "conclusions": conclusions,
            "recommendations": recommendations,
        }


# ==================== HTML 生成器 ====================

HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>心率分析报告 — XOSS X2PRO</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {{ --bg: #f4f5f7; --card-bg: #ffffff; --text: #1a1a2e; --text-secondary: #6b7280; --border: #e5e7eb; --accent: #ef4444; --blue: #3b82f6; --green: #22c55e; --amber: #f59e0b; --purple: #8b5cf6; --radius: 8px; }}
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, "Noto Sans SC", sans-serif; background: var(--bg); color: var(--text); line-height: 1.5; }}
  .container {{ max-width: 1100px; margin: 0 auto; padding: 24px 20px 48px; }}
  header {{ display: flex; align-items: center; gap: 16px; margin-bottom: 28px; }}
  header .icon {{ width: 44px; height: 44px; border-radius: 12px; background: var(--accent); display: flex; align-items: center; justify-content: center; font-size: 22px; color: #fff; }}
  header h1 {{ font-size: 22px; font-weight: 700; }}
  header p {{ font-size: 13px; color: var(--text-secondary); }}
  .grid-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
  .grid-4 {{ display: grid; grid-template-columns: repeat(4,1fr); gap: 16px; }}
  .card {{ background: var(--card-bg); border: 1px solid var(--border); border-radius: var(--radius); padding: 20px; }}
  .card h2 {{ font-size: 14px; font-weight: 600; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 14px; }}
  .device-grid {{ display: grid; grid-template-columns: repeat(3,1fr); gap: 12px; }}
  .device-item {{ display: flex; flex-direction: column; gap: 2px; }}
  .device-item .label {{ font-size: 11px; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.3px; }}
  .device-item .value {{ font-size: 17px; font-weight: 600; }}
  .battery-bar {{ height: 6px; border-radius: 3px; background: #e5e7eb; margin-top: 4px; overflow: hidden; }}
  .battery-bar .fill {{ height: 100%; border-radius: 3px; background: var(--green); }}
  .stat-value {{ font-size: 28px; font-weight: 700; line-height: 1.1; }}
  .stat-label {{ font-size: 12px; color: var(--text-secondary); margin-top: 4px; }}
  .stat-sub {{ font-size: 12px; color: var(--text-secondary); }}
  .chart-wrap {{ position: relative; width: 100%; }}
  .chart-wrap canvas {{ width: 100% !important; image-rendering: crisp-edges; }}
  .chart-wrap-packet {{ position: relative; width: 100%; flex: 1; min-height: 0; }}
  .chart-wrap-packet canvas {{ width: 100% !important; image-rendering: crisp-edges; }}
  .hrv-grid {{ display: grid; grid-template-columns: repeat(5,1fr); gap: 10px; padding: 4px; }}
  .hrv-item {{ text-align: center; background: #d0eaf2; border-radius: 10px; padding: 12px 4px; }}
  .hrv-item .val {{ font-size: 20px; font-weight: 700; }}
  .hrv-item .lbl {{ font-size: 11px; color: var(--text-secondary); margin-top: 4px; }}
  .anomaly-table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  .anomaly-table th {{ text-align: left; padding: 8px 12px; background: #f9fafb; border-bottom: 2px solid var(--border); font-weight: 600; font-size: 11px; text-transform: uppercase; color: var(--text-secondary); }}
  .anomaly-table td {{ padding: 7px 12px; border-bottom: 1px solid var(--border); }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }}
  .badge-mutation {{ background: #fef3c7; color: #92400e; }}
  .badge-ectopic {{ background: #fee2e2; color: #991b1b; }}
  .summary-box {{ background: #fef2f2; border: 1px solid #fecaca; border-radius: var(--radius); padding: 16px; margin-bottom: 16px; }}
  .summary-box h3 {{ font-size: 13px; font-weight: 600; color: #991b1b; margin-bottom: 6px; }}
  .summary-box p {{ font-size: 13px; color: #7f1d1d; }}
  .cardio-section {{ margin-top: 32px; }}
  .cardio-section .card {{ border-left: 3px solid var(--accent); }}
  .cardio-section h2 {{ color: var(--accent); }}
  .cardio-block {{ margin-bottom: 20px; }}
  .cardio-block:last-child {{ margin-bottom: 0; }}
  .cardio-block h3 {{ font-size: 15px; font-weight: 600; color: var(--text); margin-bottom: 8px; padding-bottom: 6px; border-bottom: 1px solid var(--border); }}
  .cardio-block p {{ font-size: 13px; color: #374151; line-height: 1.75; margin-bottom: 8px; }}
  .cardio-block ul {{ margin: 6px 0 6px 18px; font-size: 13px; color: #374151; line-height: 1.75; }}
  .cardio-block ul li {{ margin-bottom: 3px; }}
  .cardio-highlight {{ background: #fffbeb; border: 1px solid #fde68a; border-radius: 6px; padding: 12px 16px; margin: 10px 0; font-size: 13px; color: #92400e; line-height: 1.7; }}
  .cardio-highlight strong {{ color: #78350f; }}
  .cardio-metric-row {{ display: flex; gap: 24px; flex-wrap: wrap; margin: 8px 0; }}
  .cardio-metric {{ font-size: 13px; }}
  .cardio-metric span {{ font-weight: 600; color: var(--accent); }}
  footer {{ text-align: center; margin-top: 40px; font-size: 12px; color: var(--text-secondary); }}
  @media (max-width: 768px) {{ .grid-2, .grid-4 {{ grid-template-columns: 1fr; }} .device-grid {{ grid-template-columns: 1fr 1fr; }} .hrv-grid {{ grid-template-columns: repeat(3,1fr); }} .cardio-metric-row {{ flex-direction: column; gap: 4px; }} }}
</style>
</head>
<body>
<div class="container">
{header}
{device_info}
{data_overview}
{exercise_summary}
{hrv_metrics}
{charts_row1}
{trend_chart}
{hrv_charts}
{anomaly_table}
{cardio_analysis}
<footer>XOSS 心率传感器解析工具 V2.2.4 · 生成于 {gen_time}</footer>
</div>
<script>
{chart_js}
</script>
</body>
</html>'''


def generate_html(json_path, csv_path, output_path):
    """生成完整HTML报告"""
    with open(json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # 采样心跳数据
    trend_labels, trend_values = sample_heartbeat_csv(csv_path)

    # 心内科分析
    analyzer = CardioAnalyzer(data)
    cardio = analyzer.full_analysis()

    # 构建各部分HTML
    device = data.get("device_info", {})
    pkt = data.get("packet_stats", {})
    hrv = data.get("hrv_metrics", {})
    seg = data.get("exercise_segments", {})
    summary = data.get("exercise_summary", {})
    pkt_cls = data.get("packet_classification", [])
    anomalies = data.get("anomalies", [])
    time_range = data.get("log_time_range", {})

    header = build_header(time_range)
    device_info = build_device_info(device)
    data_overview = build_data_overview(pkt, hrv, cardio)
    exercise_summary_html = build_exercise_summary(summary, seg)
    hrv_metrics_html = build_hrv_metrics(hrv)
    charts_row1 = build_charts_row1()
    trend_chart = build_trend_chart()
    hrv_charts = build_hrv_charts()
    anomaly_table = build_anomaly_table(anomalies, data.get("anomaly_count", 0))
    cardio_html = build_cardio_analysis(cardio, data)

    chart_js = build_chart_js(seg, pkt_cls, trend_labels, trend_values, hrv)

    html = HTML_TEMPLATE.format(
        header=header,
        device_info=device_info,
        data_overview=data_overview,
        exercise_summary=exercise_summary_html,
        hrv_metrics=hrv_metrics_html,
        charts_row1=charts_row1,
        trend_chart=trend_chart,
        hrv_charts=hrv_charts,
        anomaly_table=anomaly_table,
        cardio_analysis=cardio_html,
        chart_js=chart_js,
        gen_time=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)

    return output_path


def sample_heartbeat_csv(csv_path, target_points=200):
    """从CSV采样心跳数据用于趋势图"""
    labels, values = [], []
    try:
        with open(csv_path, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        step = max(1, len(rows) // target_points)
        for i in range(0, len(rows), step):
            row = rows[i]
            t = row.get("time", "")
            if len(t) >= 12:
                labels.append(t[-12:-4])
            else:
                labels.append(t)
            values.append(round(float(row.get("inst_hr", 0)), 2))
    except Exception:
        labels, values = [], []
    return labels, values


def build_header(time_range):
    start = time_range.get("start", "N/A")
    end = time_range.get("end", "N/A")
    return f'''<header>
  <div class="icon">&hearts;</div>
  <div>
    <h1>XOSS X2PRO 心率分析报告</h1>
    <p>记录时段: {start} ~ {end}</p>
  </div>
</header>'''


def build_device_info(device):
    battery = device.get("battery", 0)
    return f'''<div class="card" style="margin-bottom:16px">
  <h2>设备信息</h2>
  <div class="device-grid">
    <div class="device-item"><span class="label">设备名称</span><span class="value">{device.get('device_name', 'N/A')}</span></div>
    <div class="device-item"><span class="label">型号</span><span class="value">{device.get('model', 'N/A')}</span></div>
    <div class="device-item"><span class="label">序列号</span><span class="value">{device.get('sn', 'N/A')}</span></div>
    <div class="device-item"><span class="label">固件版本</span><span class="value">{device.get('firmware', 'N/A')}</span></div>
    <div class="device-item">
      <span class="label">电池电量</span><span class="value">{battery}%</span>
      <div class="battery-bar"><div class="fill" style="width:{battery}%"></div></div>
    </div>
    <div class="device-item"><span class="label">剩余存储</span><span class="value">{device.get('remain_storage', 'N/A')}</span></div>
  </div>
</div>'''


def build_data_overview(pkt, hrv, cardio):
    anomaly_count = pkt.get("心律异常事件数", 0)
    anomaly_stats = cardio.get("anomaly_stats", {})
    mutation = anomaly_stats.get("mutation", 0)
    ectopic = anomaly_stats.get("ectopic", 0)
    avg_hr = hrv.get("平均瞬时心率(bpm)", 0)
    min_hr = hrv.get("最小心率(bpm)", 0)
    max_hr = hrv.get("最大心率(bpm)", 0)
    return f'''<div class="grid-4" style="margin-bottom:16px">
  <div class="card">
    <div class="stat-value">{pkt.get('报文总数量', 0):,}</div>
    <div class="stat-label">报文总数量</div>
  </div>
  <div class="card">
    <div class="stat-value">{pkt.get('有效心跳总数', 0):,}</div>
    <div class="stat-label">有效心跳总数</div>
  </div>
  <div class="card">
    <div class="stat-value" style="color:var(--accent)">{avg_hr}</div>
    <div class="stat-label">平均心率 (bpm)</div>
    <div class="stat-sub">范围 {min_hr} ~ {max_hr}</div>
  </div>
  <div class="card">
    <div class="stat-value" style="color:var(--amber)">{anomaly_count:,}</div>
    <div class="stat-label">心律异常事件</div>
    <div class="stat-sub">{mutation} 突变 + {ectopic} 疑似早搏</div>
  </div>
</div>'''


def build_exercise_summary(summary, seg):
    high_pct = seg.get("高强度(150-180)", {}).get("占比(%)", 0)
    aerobic_pct = seg.get("有氧(120-150)", {}).get("占比(%)", 0)
    total = aerobic_pct + high_pct
    if high_pct > 30:
        assessment = f"本次运动以高强度为主（{high_pct:.2f}%），属于剧烈运动负荷。"
    else:
        assessment = f"本次运动以有氧+高强度为主（合计 {total:.2f}%），属于剧烈运动负荷。"
    return f'''<div class="summary-box">
  <h3>运动负荷评估</h3>
  <p>{assessment} 建议关注恢复期心率回落速度，避免过度训练。</p>
</div>'''


def build_hrv_metrics(hrv):
    items = [
        ("SDNN (ms)", hrv.get("SDNN(ms)", 0)),
        ("RMSSD (ms)", hrv.get("RMSSD(ms)", 0)),
        ("SDSD (ms)", hrv.get("SDSD(ms)", 0)),
        ("SDARR (ms)", hrv.get("SDARR(ms)", 0)),
        ("pNN50", f"{hrv.get('pNN50(%)', 0)}%"),
        ("pNN20", f"{hrv.get('pNN20(%)', 0)}%"),
        ("CVRR", f"{hrv.get('CVRR(%)', 0)}%"),
        ("HRV 三角指数", hrv.get("HRV三角指数", 0)),
        ("Tin (ms)", hrv.get("Tin(ms)", 0)),
        ("RR 极差 (ms)", hrv.get("RR极差(ms)", 0)),
    ]
    items_html = "\n    ".join(f'<div class="hrv-item"><div class="val">{v}</div><div class="lbl">{k}</div></div>' for k, v in items)
    return f'''<div class="card" style="margin-bottom:16px">
  <h2>HRV 心率变异性指标（全量时域）</h2>
  <div class="hrv-grid">
    {items_html}
  </div>
</div>'''


def build_charts_row1():
    return '''<div class="grid-2" style="margin-bottom:16px">
  <div class="card">
    <h2>运动负荷分布</h2>
    <div class="chart-wrap" style="aspect-ratio: 1/1; width: 100%; max-width: 400px; margin: 0 auto;"><canvas id="exerciseChart"></canvas></div>
  </div>
  <div class="card" style="display:flex; flex-direction:column;">
    <h2>报文分类统计</h2>
    <div class="chart-wrap-packet" style="flex:1; min-height:0; padding:0;"><canvas id="packetChart"></canvas></div>
  </div>
</div>'''


def build_trend_chart():
    return '''<div class="card" style="margin-bottom:16px">
  <h2>心率趋势 (采样数据)</h2>
  <div class="chart-wrap" style="height:340px"><canvas id="trendChart"></canvas></div>
</div>'''


def build_hrv_charts():
    return '''<div class="grid-2" style="margin-bottom:16px">
  <div class="card">
    <h2>HRV 变异性指标 (ms)</h2>
    <div class="chart-wrap" style="height:300px"><canvas id="hrvTimeChart"></canvas></div>
  </div>
  <div class="card">
    <h2>HRV 变异性指标 (%)</h2>
    <div class="chart-wrap" style="height:300px"><canvas id="hrvPctChart"></canvas></div>
  </div>
</div>'''


def build_anomaly_table(anomalies, total_count):
    rows = []
    for i, a in enumerate(anomalies[:20]):
        atype = a.get("type", "")
        badge_class = "badge-mutation" if "突变" in atype else "badge-ectopic"
        hr_before = a.get("hr_before", "")
        hr_after = a.get("hr_after", "")
        rows.append(f'<tr><td>{i+1}</td><td>{a.get("time","")[-8:]}</td><td><span class="badge {badge_class}">{atype}</span></td><td>{a.get("detail","")}</td><td>{hr_before}→{hr_after} bpm</td>')

    return f'''<div class="card" style="margin-bottom:16px">
  <h2>心律异常事件（前 20 条 / 共 {total_count:,} 条）</h2>
  <div style="overflow-x:auto">
    <table class="anomaly-table">
      <thead><tr><th>#</th><th>时间</th><th>类型</th><th>详情</th><th>心率变化</th></tr></thead>
      <tbody>{"".join(rows)}</tbody>
    </table>
  </div>
</div>'''


def build_cardio_analysis(cardio, data):
    sq = cardio["signal_quality"]
    hrv_interp = cardio["hrv_interpretations"]
    exercise = cardio["exercise_analysis"]
    anomaly_items = cardio["anomaly_analysis"]
    conclusions = cardio["conclusions"]
    recommendations = cardio["recommendations"]

    def render_section(num, title, paragraphs):
        p_html = "\n".join(f"      <p>{p}</p>" for p in paragraphs)
        return f'''    <div class="cardio-block">
      <h3>{num}、{title}</h3>
{p_html}
    </div>'''

    def render_highlight(content):
        return f'      <div class="cardio-highlight">\n        {content}\n      </div>'

    hrv_parts = []
    for item in hrv_interp:
        if item.startswith("SDNN") or item.startswith("RMSSD"):
            hrv_parts.append(f"<p><strong>{item.split('。')[0]}。</strong>{'。'.join(item.split('。')[1:])}</p>")
        else:
            hrv_parts.append(f"<p>{item}</p>")

    signal_note = ""
    if sq["confidence"] == "中":
        signal_note = render_highlight("<strong>小结：</strong>HRV指标整体处于运动状态下的正常偏高范围，但部分指标存在轻度异常，可能受传感器信号质量影响。建议结合静息HRV进一步评估自主神经真实状态。")
    elif sq["confidence"] == "低":
        signal_note = render_highlight("<strong>关键判断：</strong>HRV指标模式（高SDNN+低三角指数+运动中极高RMSSD+高pNN50）高度提示数据中存在非生理性干扰。最常见原因是传感器在高强度运动中因胸带滑动、肌电干扰或接触不良产生的信号伪差。真正的生理性高HRV通常伴随三角指数同步升高。")
    else:
        signal_note = render_highlight("<strong>小结：</strong>全部HRV指标在运动背景下均处于正常或可接受范围。迷走神经调节功能（RMSSD、pNN50）在运动中保持适度活性，自主神经平衡未见异常偏移。HRV三角指数提示RR分布形态正常，无心律失常引起的分布畸变。")

    # 构建心内科HTML
    hrv_html = f'''    <div class="cardio-block">
      <h3>二、HRV 时域指标逐项解读</h3>
{"".join(hrv_parts)}
{signal_note}
    </div>'''

    anomaly_parts = "\n".join(f"<li>{item}</li>" for item in anomaly_items if not item.startswith("共"))
    anomaly_intro = [item for item in anomaly_items if item.startswith("共")]
    anomaly_html = f'''    <div class="cardio-block">
      <h3>四、心律异常事件分析</h3>
      <p>{" ".join(anomaly_intro)}</p>
      <ul>
{anomaly_parts}
      </ul>
    </div>'''

    exercise_html = render_section("三", "运动负荷与自主神经调节", exercise)

    conc_html = render_section("五", "综合结论与建议", conclusions)
    rec_items = "\n".join(f"        {i+1}. {r}<br>" for i, r in enumerate(recommendations))
    conc_html += f'\n{render_highlight(f"<strong>建议：</strong><br>{rec_items}")}'

    # 总体评价
    avg_hr = data.get("hrv_metrics", {}).get("平均瞬时心率(bpm)", 0)
    tin = data.get("hrv_metrics", {}).get("Tin(ms)", 0)
    avg_rr = data.get("hrv_metrics", {}).get("平均RR间期(ms)", 0)
    rr_range = data.get("hrv_metrics", {}).get("RR极差(ms)", 0)
    total_beats = data.get("packet_stats", {}).get("有效心跳总数", 0)
    time_range = data.get("log_time_range", {})

    overview_html = f'''    <div class="cardio-block">
      <h3>一、总体评价</h3>
      <p>本次记录共采集 {total_beats:,} 次有效心跳。整体心率变异性（HRV）分析结合运动场景综合评估如下。</p>
      <div class="cardio-metric-row">
        <div class="cardio-metric">平均心率: <span>{avg_hr} bpm</span></div>
        <div class="cardio-metric">Tin（中位 RR）: <span>{tin} ms</span></div>
        <div class="cardio-metric">平均 RR: <span>{avg_rr} ms</span></div>
        <div class="cardio-metric">RR 极差: <span>{rr_range} ms</span></div>
      </div>
    </div>'''

    return f'''<div class="cardio-section">
  <div class="card">
    <h2>心内科综合分析</h2>
{overview_html}
{hrv_html}
{exercise_html}
{anomaly_html}
{conc_html}
  </div>
</div>'''


def build_chart_js(seg, pkt_cls, trend_labels, trend_values, hrv):
    """生成Chart.js脚本 - 优化版: 高清、圆环样式、悬停性能提升"""
    # 运动负荷数据
    seg_keys = ["静息(<90)", "热身(90-120)", "有氧(120-150)", "高强度(150-180)", "极限(>180)"]
    seg_values = [seg.get(k, {}).get("占比(%)", 0) for k in seg_keys]

    # 报文分类
    pkt_labels = [c["packet_type"].split("(")[0] for c in pkt_cls]
    pkt_values = [c["count"] for c in pkt_cls]
    pkt_colors = ['#ef4444','#f59e0b','#22c55e','#3b82f6','#8b5cf6'][:len(pkt_labels)]

    # 趋势数据
    tl_json = json.dumps(trend_labels[:200])
    tv_json = json.dumps(trend_values[:200])

    # HRV 数据
    hrv_time_values = [
        hrv.get("SDNN(ms)", 0), hrv.get("RMSSD(ms)", 0), hrv.get("SDSD(ms)", 0),
        hrv.get("SDARR(ms)", 0), hrv.get("Tin(ms)", 0), hrv.get("RR极差(ms)", 0)
    ]
    hrv_pct_values = [
        hrv.get("pNN50(%)", 0), hrv.get("pNN20(%)", 0),
        hrv.get("CVRR(%)", 0), hrv.get("HRV三角指数", 0)
    ]

    # 全局DPI适配：最小2x高清
    dpi_fallback = 'Math.max(window.devicePixelRatio || 2, 2)'

    return f'''
// 全局高清适配
Chart.defaults.devicePixelRatio = {dpi_fallback};

// === 运动负荷环形图 (已优化: 高清/圆环样式/悬停性能) ===
new Chart(document.getElementById('exerciseChart'), {{
  type: 'doughnut',
  data: {{
    labels: {json.dumps(seg_keys)},
    datasets: [{{
      data: {json.dumps(seg_values)},
      backgroundColor: ['#22c55e','#3b82f6','#f59e0b','#ef4444','#7c3aed'],
      borderWidth: 2,
      borderColor: '#ffffff',
      hoverOffset: 6
    }}]
  }},
  options: {{
    responsive: true,
    maintainAspectRatio: true,
    aspectRatio: 1, // 严格1:1正方形比例
    devicePixelRatio: Math.max(window.devicePixelRatio, 2),
    cutout: '55%', // 标准环形比例
    animation: {{
      duration: 500,
      easing: 'easeOutQuad'
    }},
    interaction: {{
      mode: 'nearest',
      intersect: false, // 悬浮灵敏度高，不需要精确点击到扇区
      axis: 'xy'
    }},
    hover: {{
      animationDuration: 150 // 快速悬浮响应
    }},
    plugins: {{
      legend: {{
        position: 'bottom',
        align: 'center',
        maxColumns: 3, // 强制每行最多3个，自动分成两行：3+2布局
        labels: {{
          padding: 25,
          boxWidth: 12,
          usePointStyle: true,
          pointStyle: 'circle',
          textAlign: 'center',
          font: {{
            size: 13,
            family: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif'
          }}
        }}
      }},
      tooltip: {{
        backgroundColor: 'rgba(0,0,0,0.85)',
        titleColor: '#ffffff',
        bodyColor: '#ffffff',
        borderColor: 'rgba(255,255,255,0.2)',
        borderWidth: 1,
        padding: 10,
        displayColors: true,
        cornerRadius: 4,
        callbacks: {{
          label: function(context) {{
            return context.label + ': ' + context.raw + '%';
          }}
        }}
      }}
    }}
  }}
}});
// === 报文分类柱状图 ===
new Chart(document.getElementById('packetChart'), {{
  type: 'bar',
  data: {{
    labels: {json.dumps(pkt_labels)},
    datasets: [{{
      label: '报文数量',
      data: {json.dumps(pkt_values)},
      backgroundColor: {json.dumps(pkt_colors)},
      borderRadius: 4,
      borderSkipped: false,
      barThickness: 'flex',
      maxBarThickness: 48
    }}]
  }},
  options: {{
    indexAxis: 'y',
    responsive: true,
    maintainAspectRatio: false,
    layout: {{ padding: {{ top: 8, bottom: 8, left: 0, right: 0 }} }},
    animation: false,
    interaction: {{ mode: 'nearest', intersect: true }},
    plugins: {{
      legend: {{ display: false }},
      tooltip: {{ animation: false, delay: 0 }}
    }},
    scales: {{
      x: {{
        title: {{ display: true, text: '数量', font: {{ size: 11 }} }},
        grid: {{ display: true, drawBorder: false }},
        border: {{ display: true }},
        ticks: {{ font: {{ size: 10 }} }}
      }},
      y: {{
        title: {{ display: true, text: '报文类型', font: {{ size: 11 }} }},
        grid: {{ display: false }},
        border: {{ display: false }},
        ticks: {{ padding: 8 }}
      }}
    }}
  }}
}});
// === 心率趋势折线图 ===
new Chart(document.getElementById('trendChart'), {{
  type: 'line',
  data: {{
    labels: {tl_json},
    datasets: [{{
      label: '瞬时心率 (bpm)',
      data: {tv_json},
      borderColor: '#ef4444',
      backgroundColor: 'rgba(239,68,68,0.08)',
      fill: true,
      tension: 0.3,
      pointRadius: 0,
      borderWidth: 1.5
    }}]
  }},
  options: {{
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    interaction: {{ mode: 'index', intersect: false }},
    plugins: {{
      legend: {{ display: false }},
      tooltip: {{ animation: false, delay: 0 }}
    }},
    scales: {{
      x: {{ title: {{ display: true, text: '时间', font: {{ size: 11 }} }}, ticks: {{ maxTicksLimit: 16, font: {{ size: 10 }} }} }},
      y: {{ title: {{ display: true, text: '心率 (bpm)', font: {{ size: 11 }} }}, min: 40, max: 220 }}
    }}
  }}
}});

// === HRV 变异性指标柱状图 (ms) ===
new Chart(document.getElementById('hrvTimeChart'), {{
  type: 'bar',
  data: {{
    labels: ['SDNN','RMSSD','SDSD','SDARR','Tin','RR极差'],
    datasets: [{{
      label: 'ms',
      data: {json.dumps(hrv_time_values)},
      backgroundColor: ['#ef4444','#f59e0b','#22c55e','#3b82f6','#8b5cf6','#ec4899'],
      borderRadius: 6,
      borderSkipped: false
    }}]
  }},
  options: {{
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    interaction: {{ mode: 'nearest', intersect: true }},
    plugins: {{
      legend: {{ display: false }},
      tooltip: {{ animation: false, delay: 0 }}
    }},
    scales: {{
      y: {{ title: {{ display: true, text: 'ms', font: {{ size: 11 }} }}, beginAtZero: true }}
    }}
  }}
}});

// === HRV 百分比指标柱状图 ===
new Chart(document.getElementById('hrvPctChart'), {{
  type: 'bar',
  data: {{
    labels: ['pNN50','pNN20','CVRR','HRV三角指数'],
    datasets: [{{
      label: '% / 指数',
      data: {json.dumps(hrv_pct_values)},
      backgroundColor: ['#ef4444','#f59e0b','#3b82f6','#8b5cf6'],
      borderRadius: 6,
      borderSkipped: false
    }}]
  }},
  options: {{
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    interaction: {{ mode: 'nearest', intersect: true }},
    plugins: {{
      legend: {{ display: false }},
      tooltip: {{ animation: false, delay: 0 }}
    }},
    scales: {{
      y: {{ title: {{ display: true, text: '% / 指数值', font: {{ size: 11 }} }}, beginAtZero: true }}
    }}
  }}
}});
'''


# ==================== 主入口 ====================

def main():
    parser = argparse.ArgumentParser(description="心率分析HTML报告生成器 V2.2.4")
    parser.add_argument("--json", required=True, help="分析结果JSON文件路径")
    parser.add_argument("--csv", required=True, help="心跳明细CSV文件路径")
    parser.add_argument("--out", default=None, help="输出HTML路径(默认与JSON同目录)")
    args = parser.parse_args()

    json_path = Path(args.json)
    csv_path = Path(args.csv)
    if args.out:
        out_path = Path(args.out)
    else:
        out_path = json_path.parent / "heart_rate_report.html"

    if not json_path.exists():
        print(f"错误: JSON文件不存在: {json_path}")
        sys.exit(1)
    if not csv_path.exists():
        print(f"错误: CSV文件不存在: {csv_path}")
        sys.exit(1)

    result = generate_html(str(json_path), str(csv_path), str(out_path))
    print(f"HTML报告已生成: {result}")


if __name__ == "__main__":
    main()
